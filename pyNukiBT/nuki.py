import asyncio
import hashlib
import logging
import hmac
import time
import construct
from asyncio import CancelledError, TimeoutError
from typing import Callable
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.characteristic import BleakGATTCharacteristic

import async_timeout

# from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS

import nacl.utils
import nacl.secret
from nacl.bindings.crypto_box import crypto_box_beforenm
from bleak import BleakClient, BleakError
from bleak.exc import BleakDBusError

from .const import NukiErrorException, NukiLockConst, NukiOpenerConst, NukiConst, crcCalc

logger = logging.getLogger(__name__)

class NukiDevice:
    def __init__(
        self,
        address,
        auth_id,
        nuki_public_key,
        bridge_public_key,
        bridge_private_key,
        app_id,
        name,
        client_type: NukiConst.NukiClientType = NukiConst.NukiClientType.BRIDGE,
        ble_device=None,
    ):
        self._address = address
        self._auth_id = auth_id
        self._nuki_public_key = nuki_public_key
        self._bridge_public_key = bridge_public_key
        self._bridge_private_key = bridge_private_key
        self._app_id = app_id
        self._name = name
        self._client_type = client_type

        self.rssi = None
        self.last_state = None
        self.config = {}
        self._poll_needed = False
        self.last_action_status = None
        self._device_type = None

        self._pairing_handle = None
        self._client = None
        self._expected_response: NukiConst.NukiCommand = None
        self._aggregate_messages = list(),
        self.retry = 5
        self.connection_timeout = 40
        self.command_response_timeout = 20

        self._send_cmd_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._update_state_lock = asyncio.Lock()
        self._update_config_lock = asyncio.Lock()
        self._notify_future = None
        self._messages = []

        self._callbacks = []

        if nuki_public_key and bridge_private_key:
            self._create_shared_key()

        self._last_ibeacon = None

        if ble_device:
            self.set_ble_device(ble_device)

    def parse_advertisement_data(self, device, advertisement_data):
        if device.address == self._address:
            manufacturer_data = advertisement_data.manufacturer_data.get(76, None)
            if manufacturer_data is None:
                logger.info(
                    f"No manufacturer_data (76) in advertisement_data: {advertisement_data}"
                )
                return
            if manufacturer_data[0] != 0x02:
                # Ignore HomeKit advertisement
                return
            logger.info(f"Nuki: {device.address}, RSSI: {advertisement_data.rssi}")
            tx_p = manufacturer_data[-1]
            if self.just_got_beacon:
                logger.info(f"Ignoring duplicate beacon from Nuki {device.address}")
                return
            self.set_ble_device(device, advertisement_data)
            self.rssi = advertisement_data.rssi
            if not self.last_state or tx_p & 0x1:
                self._poll_needed = True
            return
        else:
            logger.error(f"called with invalid address {device.address}")

    def poll_needed(self, seconds_since_last_poll=None):
        return self._poll_needed

    @property
    def just_got_beacon(self):
        if self._last_ibeacon is None:
            self._last_ibeacon = time.time()
            return False
        seen_recently = time.time() - self._last_ibeacon <= 1
        if not seen_recently:
            self._last_ibeacon = time.time()
        return seen_recently

    @property
    def device_type(self):
        return self._device_type

    # @device_type.setter
    # def device_type(self, device_type: NukiConst.NukiDeviceType):
    #     if device_type == NukiConst.NukiDeviceType.OPENER:
    #         self._const = NukiOpenerConst
    #     else:
    #         self._const = NukiLockConst
    #     self._device_type = device_type
    #     logger.info(f"Device type: {self._device_type}")

    def _create_shared_key(self):
        self._shared_key = crypto_box_beforenm(
            self._nuki_public_key, self._bridge_private_key
        )
        self._box = nacl.secret.SecretBox(self._shared_key)

    @property
    def is_battery_critical(self):
        return bool(self.last_state["critical_battery_state"] & 1)

    @property
    def is_battery_charging(self):
        return bool(self.last_state["critical_battery_state"] & 2)

    @property
    def battery_percentage(self):
        return ((self.last_state["critical_battery_state"] & 252) >> 2) * 2

    @property
    def keyturner_state(self):
        return self.last_state

    def get_keyturner_state(self):
        return self.last_state

    def get_config(self):
        return self.config

    @staticmethod
    def _prepare_command(cmd: NukiConst.NukiCommand, payload=bytes()):
        message = NukiConst.NukiCommand.build(cmd) + payload
        crc = crcCalc.calc(message).to_bytes(2, "little")
        message += crc
        return message

    async def _send_encrtypted_command(
        self,
        cmd: NukiConst.NukiCommand,
        payload: dict,
        aggregate_messages = list(),
        expected_response: NukiConst.NukiCommand = None,
    ):
        unencrypted = self._const.NukiMessage.build(
            {
                "auth_id": self._auth_id,
                "command": cmd,
                "payload": payload,
            }
        )
        nonce = nacl.utils.random(24)
        encrypted = self._box.encrypt(unencrypted, nonce)[24:]
        length = len(encrypted).to_bytes(2, "little")
        message = nonce + self._auth_id + length + encrypted
        logger.info(f"sending encrypted command {cmd}")
        return await self._send_command(
            self._const.BLE_CHAR, message, aggregate_messages=aggregate_messages, expected_response=expected_response,
        )

    def _decrypt_message(self, data: bytes):
        msg = self._const.NukiEncryptedMessage.parse(data)
        decrypted = self._box.decrypt(msg.nonce + msg.encrypted)
        return decrypted

    def _parse_message(self, data: bytes, encrypted: bool):
        msg_sz = None
        try:
            if encrypted:
                msg = self._const.NukiMessage.parse(self._decrypt_message(data))
            else:
                msg = self._const.NukiUnencryptedMessage.parse(data)
            # keyturner_state usually has crc=0. if we got crc=0 in other command we want to know about it.
            if msg.crc == 0 and msg.command != self._const.NukiCommand.KEYTURNER_STATES:
                logger.warning(f"got message with crc=0. cmd:{msg.command}")
                try:
                    msg = self._const.NukiMessage2.parse(data)
                    msg_sz = len(self._const.NukiMessage2.build(msg))
                except TypeError:
                    pass
        except construct.core.ChecksumError as ex:
            logger.warning(f"parse error {ex}")
            try:
                msg = self._const.NukiMessage2.parse(data)
                msg_sz = len(self._const.NukiMessage2.build(msg))
            except TypeError:
                msg_sz = 0
            if msg_sz == len(data):
                # If we got the len we expected, this is probably a real crc error.
                # Otherwise it is probably not a real crc error, we are just missing some fields in the message format
                raise

        if msg_sz and msg_sz != len(data):
            logger.warning(
                f"Got unexpected message length for command {msg.command}. got length:{len(data)} expecting length:{msg_sz}"
            )
            unhandled_bytes = len(data) - msg_sz
            logger.warning(
                f"Got {unhandled_bytes} unknown bytes with value: {data[-unhandled_bytes-2:-2]}"
            )

        return msg

    def _fire_callbacks(self) -> None:
        """Fire callbacks."""
        logger.debug("%s: Fire callbacks", self._name)
        for callback in self._callbacks:
            callback()

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to device notifications."""
        self._callbacks.append(callback)

        def _unsub() -> None:
            """Unsubscribe from device notifications."""
            self._callbacks.remove(callback)

        return _unsub

    def set_ble_device(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData = None
    ):
        if not self._client:
            self._client = BleakClient(ble_device, timeout=self.connection_timeout)
        if (not self._device_type or not self._const) and advertisement_data:
            if NukiOpenerConst.BLE_PAIRING_CHAR in advertisement_data.service_uuids:
                self._device_type = NukiConst.NukiDeviceType.OPENER
                self._const = NukiOpenerConst
            else:
                self._device_type = NukiConst.NukiDeviceType.SMARTLOCK_1_2
                self._const = NukiLockConst

        return self._client

    async def _notification_handler(self, sender: BleakGATTCharacteristic, data):
        logger.debug(f"Notification handler: {sender}, data: {data}")

        # The pairing handler is not encrypted
        encrypted = sender.uuid != self._const.BLE_PAIRING_CHAR
        msg = self._parse_message(bytes(data), encrypted)

        if msg.command == self._const.NukiCommand.ERROR_REPORT:
            if msg.payload.error_code == self._const.ErrorCode.P_ERROR_NOT_PAIRING:
                logger.error("UNPAIRED! Put Nuki in pairing mode by pressing the button 6 seconds, Then try again")
            else:
                logger.error(
                    f"Error {msg.payload.error_code}, command {msg.payload.command_identifier}"
                )
            ex = NukiErrorException(
                error_code=msg.payload.error_code,
                command=msg.payload.command_identifier,
            )
            if self._notify_future and not self._notify_future.done():
                self._notify_future.set_exception(ex)
                return
            else:
                raise ex

        elif msg.command == self._const.NukiCommand.STATUS:
            logger.debug(f"Last action: {msg.payload.status}")
            self.last_action_status = msg.payload.status

        if self._notify_future and not self._notify_future.done():
            if msg.command == self._expected_response:
                self._notify_future.set_result(msg.payload)
                return
            if msg.command in self._aggregate_messages:
                self._messages.append(msg.payload)
                return

        if msg.command == self._const.NukiCommand.KEYTURNER_STATES:
            update_config = not self.config or (
                self.last_state["config_update_count"]
                != msg.payload["config_update_count"]
            )
            self.last_state = msg.payload
            logger.debug(f"State: {self.last_state}")
            if update_config:
                # todo: update config directly?
                self.poll_needed = True
            self._fire_callbacks()

        elif msg.command == self._const.NukiCommand.STATUS:
            logger.info(f"Last action: {msg.payload.status}")

        else:
            logger.error("%s: Received unsolicited notification: %s", self._name, msg)
            logger.error("was expecting %s", self._expected_response)

    async def _send_command(
        self, characteristic, command,
        aggregate_messages = list(),
        expected_response: NukiConst.NukiCommand = None
    ):
        async with self._send_cmd_lock:
            self._notify_future = asyncio.Future()
            self._aggregate_messages = aggregate_messages
            self._expected_response = expected_response
            self._messages = list()
            msg = None

            # Sometimes the connection to the smartlock fails, retry 3 times
            _characteristic = characteristic
            for i in range(1, self.retry + 1):
                logger.info(f"Trying to send data. Attempt {i}")
                try:
                    await self.connect()
                    if _characteristic is None:
                        _characteristic = self._const.BLE_CHAR
                    logger.info(f"Sending data to Nuki")
                    await self._client.write_gatt_char(_characteristic, command, response=True)
                except (TimeoutError, CancelledError):
                    logger.error(f"Timeout while sending data on attempt {i}")
                    await asyncio.sleep(0.2)
                except BleakDBusError as ex:
                    logger.error(f"DBus Error {ex}")
                    await asyncio.sleep(0.2)
                # except BLEAK_RETRY_EXCEPTIONS as ex:
                #     logger.error(f'Bleak retry error {ex}')
                #     await asyncio.sleep(0.2)
                except BleakError as exc:
                    logger.error(f"Bleak Error while sending data on attempt {i}")
                    logger.exception(exc)
                    await asyncio.sleep(0.7)
                except Exception as exc:
                    logger.error(f"Error while sending data on attempt {i}")
                    logger.exception(exc)
                    await asyncio.sleep(0.2)
                else:
                    logger.info(f"Data sent on attempt {i}")
                    break
        if expected_response:
            async with async_timeout.timeout(self.command_response_timeout):
                msg = await self._notify_future
        self._notify_future = None
        self._expected_response = None
        return msg

    async def _safe_start_notify(self, *args):
        try:
            await self._client.start_notify(*args)
        # This exception might occur due to Bluez downgrade required for Pi 3B+ and Pi 4. See this comment:
        # https://github.com/dauden1184/RaspiNukiBridge/issues/1#issuecomment-1103969957
        # Haven't researched further the reason and consequences of this exception
        except EOFError:
            logger.info("EOFError during notification")

    async def connect(self):
        async with self._connect_lock:
            if not self._client:
                self._client = BleakClient(
                    BLEDevice(address=self._address, details=None, name=self._name, rssi=self.rssi),
                    timeout=self.connection_timeout
                )
            if self._client.is_connected:
                logger.info("Connected")
                return
            await self._client.connect()
            logger.debug(f"Services {[str(s) for s in self._client.services]}")
            logger.debug(
                f"Characteristics {[str(v) for v in self._client.services.characteristics.values()]}"
            )
            if (not self._device_type or not self._const):
                services = self._client.services
                if services.get_characteristic(NukiOpenerConst.BLE_PAIRING_CHAR):
                    self._device_type = NukiConst.NukiDeviceType.OPENER
                    self._const = NukiOpenerConst
                else:
                    self._device_type = NukiConst.NukiDeviceType.SMARTLOCK_1_2
                    self._const = NukiLockConst
            await self._safe_start_notify(
                self._const.BLE_PAIRING_CHAR, self._notification_handler
            )
            await self._safe_start_notify(
                self._const.BLE_CHAR, self._notification_handler
            )
            logger.info("Connected")

    async def disconnect(self):
        if self._client and self._client.is_connected:
            logger.info(f"Nuki disconnecting...")
            try:
                await self._client.disconnect()
                logger.info("Nuki disconnected")
            except Exception as e:
                logger.error(f"Error while disconnecting")
                logger.exception(e)

    async def update_state(self):
        logger.info("Querying Nuki state")
        if self._update_state_lock.locked():
            logger.info("update state already in progress. ignoring")
            return
        async with self._update_state_lock, self._operation_lock:
            await self.connect() # connect so we can identify the device type and update self._const accordingly
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_DATA,
                {"command": self._const.NukiCommand.KEYTURNER_STATES},
                expected_response=self._const.NukiCommand.KEYTURNER_STATES,
            )
            update_config = not self.config or (
                self.last_state["config_update_count"]
                != msg["config_update_count"]
            )
            self.last_state = msg
            logger.debug(f"State: {self.last_state}")
            self._poll_needed = False
        if update_config:
            await self.update_config()

    async def lock(self):
        return await self.lock_action(
            self._const.LockAction.LOCK, self._const.LockState.LOCKING
        )

    async def unlock(self):
        return await self.lock_action(
            self._const.LockAction.UNLOCK, self._const.LockState.UNLOCKING
        )

    async def unlatch(self):
        return await self.lock_action(
            self._const.LockAction.UNLATCH, self._const.LockState.UNLATCHING
        )

    async def lock_action(
        self, action: NukiConst.LockAction, new_lock_state: NukiConst.LockState = None, name_suffix: str = None
    ):
        logger.info(f"Lock action {action}")
        async with self._operation_lock:
            if new_lock_state:
                self.last_state["lock_state"] = new_lock_state
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_DATA,
                {"command": self._const.NukiCommand.CHALLENGE},
                expected_response=self._const.NukiCommand.CHALLENGE,
            )
            payload = {
                "lock_action": action,
                "app_id": self._app_id,
                "flags": 0,
                "name_suffix": name_suffix,
                "nonce": msg.nonce,
            }
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.LOCK_ACTION,
                payload,
                expected_response=self._const.NukiCommand.STATUS,
            )
            logger.info(f"{msg.status}")
        return msg

    async def update_config(self):
        logger.info("Retrieve nuki configuration")
        if self._update_config_lock.locked():
            logger.info("get config already in progress")
            return
        async with self._operation_lock, self._update_config_lock:
            await self.connect() # connect so we can identify the device type and update self._const accordingly
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_DATA,
                {"command": self._const.NukiCommand.CHALLENGE},
                expected_response=self._const.NukiCommand.CHALLENGE,
            )
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_CONFIG,
                {"nonce": msg["nonce"]},
                expected_response=self._const.NukiCommand.CONFIG,
            )
            self.config = msg
            logger.debug(f"Config: {self.config}")

    async def pair(self):
        async with self._operation_lock:
            await self.connect() # connect so we can identify the device type and update self._const accordingly
            payload = self._const.NukiCommand.build(self._const.NukiCommand.PUBLIC_KEY)
            cmd = self._prepare_command(self._const.NukiCommand.REQUEST_DATA, payload)
            msg = await self._send_command(
                self._const.BLE_PAIRING_CHAR, cmd, expected_response=self._const.NukiCommand.PUBLIC_KEY
            )
            self._nuki_public_key = msg["public_key"]
            self._create_shared_key()
            logger.info(f"Nuki {self._address} public key: {self._nuki_public_key.hex()}")
            cmd = self._prepare_command(
                self._const.NukiCommand.PUBLIC_KEY, self._bridge_public_key
            )
            msg = await self._send_command(
                self._const.BLE_PAIRING_CHAR, cmd, expected_response=self._const.NukiCommand.CHALLENGE
            )
            value_r = (
                self._bridge_public_key + self._nuki_public_key + msg["nonce"]
            )
            payload = hmac.new(
                self._shared_key, msg=value_r, digestmod=hashlib.sha256
            ).digest()
            cmd = self._prepare_command(
                self._const.NukiCommand.AUTHORIZATION_AUTHENTICATOR, payload
            )
            msg = await self._send_command(
                self._const.BLE_PAIRING_CHAR, cmd, expected_response=self._const.NukiCommand.CHALLENGE
            )
            app_id = self._app_id.to_bytes(4, "little")
            type_id = self._const.NukiClientType.build(self._client_type)
            name = self._name.encode("utf-8").ljust(32, b"\0")
            nonce = nacl.utils.random(32)
            value_r = type_id + app_id + name + nonce + msg["nonce"]
            payload = hmac.new(
                self._shared_key, msg=value_r, digestmod=hashlib.sha256
            ).digest()
            payload += type_id + app_id + name + nonce
            cmd = self._prepare_command(self._const.NukiCommand.AUTHORIZATION_DATA, payload)
            msg = await self._send_command(
                self._const.BLE_PAIRING_CHAR,
                cmd,
                expected_response=self._const.NukiCommand.AUTHORIZATION_ID,
            )
            self._auth_id = msg["auth_id"]
            value_r = self._auth_id + msg["nonce"]
            payload = hmac.new(
                self._shared_key, msg=value_r, digestmod=hashlib.sha256
            ).digest()
            payload += self._auth_id
            cmd = self._prepare_command(
                self._const.NukiCommand.AUTHORIZATION_ID_CONFIRMATION, payload
            )
            msg = await self._send_command(
                self._const.BLE_PAIRING_CHAR, cmd, expected_response=self._const.NukiCommand.STATUS
            )
            await self.disconnect()
        return {"nuki_public_key": self._nuki_public_key, "auth_id": self._auth_id}

    async def verify_security_pin(self, security_pin):
        logger.info(f"verify security PIN")
        async with self._operation_lock:
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_DATA,
                {"command": self._const.NukiCommand.CHALLENGE},
                expected_response=self._const.NukiCommand.CHALLENGE,
            )
            payload = {
                "nonce": msg["nonce"],
                "security_pin": security_pin,
            }
            try:
                msg = await self._send_encrtypted_command(
                    self._const.NukiCommand.VERIFY_SECURITY_PIN,
                    payload,
                    expected_response=self._const.NukiCommand.STATUS,
                )
            except NukiErrorException as ex:
                if ex.error_code == self._const.ErrorCode.K_ERROR_BAD_PIN:
                    return False
                else:
                    raise
            return msg.status == self._const.StatusCode.COMPLETED

    async def request_log_entries(self, security_pin, sort_order=0x01, count=1, start_index=0):
        logger.info(f"request {count} log entries, start={start_index}")
        async with self._operation_lock:
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_DATA,
                {"command": self._const.NukiCommand.CHALLENGE},
                expected_response=self._const.NukiCommand.CHALLENGE,
            )
            payload = {
                "start_index": start_index,
                "count": count,
                "sort_order": sort_order,
                "total_count": 0,
                "nonce": msg.nonce,
                "security_pin": security_pin,
            }
            msg = await self._send_encrtypted_command(
                self._const.NukiCommand.REQUEST_LOG_ENTRIES,
                payload,
                aggregate_messages=[self._const.NukiCommand.LOG_ENTRY,],
                expected_response=self._const.NukiCommand.STATUS,
            )
            logger.debug(msg)
            logger.debug(self._messages)
            ret = self._messages
        return ret
