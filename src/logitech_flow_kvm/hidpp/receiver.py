from __future__ import annotations

import dataclasses
import struct

from .exceptions import ProtocolError
from .models import DEVICE_KIND
from .models import ChangeHostInfo
from .models import DirectDeviceInfo
from .models import ReceiverInfo
from .protocol import RECEIVER_DEVNUMBER
from .protocol import ROOT_FEATURE_INDEX
from .protocol import HidppConnection
from .protocol import Transport
from .transport import HidRawIO

RECEIVER_INFO_REGISTER = 0x2B5
NOTIFICATIONS_REGISTER = 0x00
RECEIVER_CONNECTION_REGISTER = 0x02

SUB_RECEIVER_INFORMATION = 0x03
SUB_UNIFYING_PAIRING_INFO = 0x20
SUB_UNIFYING_EXTENDED_PAIRING_INFO = 0x30
SUB_UNIFYING_DEVICE_NAME = 0x40
SUB_BOLT_PAIRING_INFO = 0x50
SUB_BOLT_DEVICE_NAME = 0x60

DEFAULT_MAX_DEVICES = 6

# battery_status | wireless | software_present, see solaar's hidpp10.NOTIFICATION_FLAG
CONNECTION_NOTIFICATION_FLAGS = 0x100000 | 0x000100 | 0x000800

FEATURE_CHANGE_HOST = 0x1814
CHANGE_HOST_READ_FUNCTION = 0x00
CHANGE_HOST_WRITE_FUNCTION = 0x10

# DEVICE_FW_INFO.getDeviceInfo returns the device's unit ID in bytes 1..4.  A
# Bolt receiver reports that same unit ID as the device's serial number, so
# reading it over Bluetooth gives a device one identity on every host,
# whichever way each host is connected to it.  Unlike the Bluetooth address,
# the unit ID also survives re-pairing.
FEATURE_DEVICE_FW_INFO = 0x0003
DEVICE_FW_INFO_GET_DEVICE_INFO = 0x00
UNIT_ID_TIMEOUT = 1.0
UNIT_ID_ATTEMPTS = 2

# Bluetooth address -> unit ID.  A device's unit ID never changes, and caching
# it keeps a transient read failure on a later reconnect from flipping the
# device's identity back to its Bluetooth address.
_unit_id_cache: dict[str, str] = {}


def _decode_codename(raw: bytes, *, length_offset: int, text_offset: int) -> str | None:
    length = raw[length_offset]
    text = raw[text_offset : text_offset + length]
    try:
        return text.decode("ascii") or None
    except UnicodeDecodeError:
        return None


@dataclasses.dataclass(frozen=True)
class PairedDevice:
    """A device paired with a receiver, as reported by its pairing registers."""

    receiver: Receiver | DirectDevice
    number: int
    wpid: str
    kind: str
    serial: str | None
    codename: str | None
    # Other identifiers this same device has been known by, such as the
    # Bluetooth address that identified a Bluetooth device before its unit ID
    # was used.  Configuration written with an alias keeps matching.
    aliases: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.serial or self.wpid

    def has_id(self, device_id: str) -> bool:
        wanted = device_id.upper()
        return any(
            candidate.upper() == wanted for candidate in (self.id, *self.aliases)
        )

    @property
    def path(self) -> str:
        if self.receiver.kind == "bluetooth":
            return self.receiver.path
        return f"{self.receiver.path}:{self.number}"


class Receiver:
    """An open connection to a single Logitech receiver."""

    def __init__(self, info: ReceiverInfo, *, transport: Transport | None = None):
        """`transport` is a test seam; production always opens a real `HidRawIO`."""
        self.path = info.path
        self.kind = info.kind
        self.product_id = info.product_id
        self._io: HidRawIO | None = None
        if transport is None:
            self._io = HidRawIO(info.path)
            transport = self._io
        self._conn = HidppConnection(transport)
        self.max_devices = self._detect_max_devices()

    def close(self) -> None:
        if self._io is not None:
            self._io.close()

    def __enter__(self) -> Receiver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _detect_max_devices(self) -> int:
        if self.kind == "bolt":
            return DEFAULT_MAX_DEVICES
        try:
            info = self._conn.read_register(
                RECEIVER_DEVNUMBER,
                RECEIVER_INFO_REGISTER,
                bytes([SUB_RECEIVER_INFORMATION]),
            )
        except ProtocolError:
            return DEFAULT_MAX_DEVICES
        if info and len(info) >= 7 and 0 < info[6] <= DEFAULT_MAX_DEVICES:
            return info[6]
        return DEFAULT_MAX_DEVICES

    def _device_codename(self, number: int) -> str | None:
        try:
            if self.kind == "bolt":
                raw = self._conn.read_register(
                    RECEIVER_DEVNUMBER,
                    RECEIVER_INFO_REGISTER,
                    bytes([SUB_BOLT_DEVICE_NAME + number, 0x01]),
                )
                if not raw or len(raw) < 3:
                    return None
                return _decode_codename(raw, length_offset=2, text_offset=3)
            raw = self._conn.read_register(
                RECEIVER_DEVNUMBER,
                RECEIVER_INFO_REGISTER,
                bytes([SUB_UNIFYING_DEVICE_NAME + (number - 1)]),
            )
            if not raw or len(raw) < 2:
                return None
            return _decode_codename(raw, length_offset=1, text_offset=2)
        except ProtocolError:
            return None

    def _bolt_paired_device(self, number: int) -> PairedDevice | None:
        try:
            info = self._conn.read_register(
                RECEIVER_DEVNUMBER,
                RECEIVER_INFO_REGISTER,
                bytes([SUB_BOLT_PAIRING_INFO + number]),
            )
        except ProtocolError:
            return None
        if not info or len(info) < 8:
            return None

        wpid = f"{info[3]:02X}{info[2]:02X}"
        kind = DEVICE_KIND.get(info[1] & 0x0F, "unknown")
        serial = info[4:8].hex().upper()
        return PairedDevice(
            receiver=self,
            number=number,
            wpid=wpid,
            kind=kind,
            serial=serial,
            codename=self._device_codename(number),
        )

    def _unifying_paired_device(self, number: int) -> PairedDevice | None:
        try:
            info = self._conn.read_register(
                RECEIVER_DEVNUMBER,
                RECEIVER_INFO_REGISTER,
                bytes([SUB_UNIFYING_PAIRING_INFO + (number - 1)]),
            )
        except ProtocolError:
            return None
        if not info or len(info) < 8:
            return None

        wpid = f"{info[3]:02X}{info[4]:02X}"
        kind = DEVICE_KIND.get(info[7] & 0x0F, "unknown")

        serial = None
        try:
            ext = self._conn.read_register(
                RECEIVER_DEVNUMBER,
                RECEIVER_INFO_REGISTER,
                bytes([SUB_UNIFYING_EXTENDED_PAIRING_INFO + (number - 1)]),
            )
            if ext and len(ext) >= 5:
                serial = ext[1:5].hex().upper()
        except ProtocolError:
            pass

        return PairedDevice(
            receiver=self,
            number=number,
            wpid=wpid,
            kind=kind,
            serial=serial,
            codename=self._device_codename(number),
        )

    def get_device(self, number: int) -> PairedDevice | None:
        if self.kind == "bolt":
            return self._bolt_paired_device(number)
        return self._unifying_paired_device(number)

    def enumerate_devices(self) -> list[PairedDevice]:
        devices = []
        for number in range(1, self.max_devices + 1):
            device = self.get_device(number)
            if device is not None:
                devices.append(device)
        return devices

    def ping_device(self, number: int, *, timeout: float = 1.5) -> float | None:
        return self._conn.ping(number, timeout=timeout)

    def get_change_host_info(self, number: int) -> ChangeHostInfo | None:
        feature_index = self._conn.get_feature_index(number, FEATURE_CHANGE_HOST)
        if feature_index is None:
            return None
        reply = self._conn.request(
            number, (feature_index << 8) | CHANGE_HOST_READ_FUNCTION
        )
        if not reply or len(reply) < 2:
            return None
        return ChangeHostInfo(
            feature_index=feature_index, num_hosts=reply[0], current_host=reply[1]
        )

    def set_current_host(self, number: int, feature_index: int, host: int) -> None:
        """Switch a paired device to another host. `host` is 0-indexed on the wire.

        The device does not reply once it starts switching, so this call does
        not wait for (or guarantee) confirmation that the switch happened.
        """
        self._conn.request(
            number,
            (feature_index << 8) | CHANGE_HOST_WRITE_FUNCTION,
            bytes([host]),
            no_reply=True,
        )

    def enable_connection_notifications(self) -> None:
        self._conn.write_register(
            RECEIVER_DEVNUMBER,
            NOTIFICATIONS_REGISTER,
            CONNECTION_NOTIFICATION_FLAGS.to_bytes(3, "big"),
        )

    def notify_devices(self) -> None:
        """Ask the receiver to resend a connection notification per paired device."""
        self._conn.write_register(
            RECEIVER_DEVNUMBER, RECEIVER_CONNECTION_REGISTER, bytes([0x02])
        )


class DirectDevice:
    """A directly-connected Bluetooth HID++ device.

    Direct devices have no receiver pairing table.  HID++ addresses the device
    as 0xFF and Bluetooth exposes the long report only.
    """

    def __init__(
        self, info: DirectDeviceInfo, *, transport: Transport | None = None
    ) -> None:
        self.path = info.path
        self.kind = "bluetooth"
        self.product_id = info.product_id
        self.max_devices = 1
        self._io: HidRawIO | None = None
        self._conn = (
            HidppConnection(transport, force_long=info.hidpp_long)
            if transport is not None
            else None
        )
        self._force_long = info.hidpp_long
        self._info = info
        self._device: PairedDevice | None = None

    def close(self) -> None:
        if self._io is not None:
            self._io.close()

    def _connection(self) -> HidppConnection:
        if self._conn is None:
            self._io = HidRawIO(self.path)
            self._conn = HidppConnection(self._io, force_long=self._force_long)
        return self._conn

    def __enter__(self) -> DirectDevice:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _read_unit_id(self) -> str | None:
        """Read the unit ID that a Bolt receiver would report as the serial."""
        connection = self._connection()
        for _attempt in range(UNIT_ID_ATTEMPTS):
            try:
                index_reply = connection.request(
                    RECEIVER_DEVNUMBER,
                    ROOT_FEATURE_INDEX << 8,
                    struct.pack("!H", FEATURE_DEVICE_FW_INFO),
                    timeout=UNIT_ID_TIMEOUT,
                )
                if not index_reply:
                    continue
                if not index_reply[0]:
                    return None
                reply = connection.request(
                    RECEIVER_DEVNUMBER,
                    (index_reply[0] << 8) | DEVICE_FW_INFO_GET_DEVICE_INFO,
                    timeout=UNIT_ID_TIMEOUT,
                )
            except ProtocolError:
                return None
            if not reply or len(reply) < 5:
                continue
            unit_id = reply[1:5]
            if unit_id in (b"\x00" * 4, b"\xff" * 4):
                return None
            return unit_id.hex().upper()
        return None

    def _build_device(self) -> PairedDevice:
        address = self._info.serial.upper() if self._info.serial else None
        unit_id = _unit_id_cache.get(address) if address else None
        if unit_id is None:
            try:
                unit_id = self._read_unit_id()
            except OSError:
                unit_id = None
            if unit_id is not None and address is not None:
                _unit_id_cache[address] = unit_id
        return PairedDevice(
            receiver=self,
            number=RECEIVER_DEVNUMBER,
            wpid=f"{self.product_id:04X}",
            kind="unknown",
            serial=unit_id or address,
            codename=self._info.name,
            aliases=(address,) if unit_id and address else (),
        )

    def get_device(self, number: int = RECEIVER_DEVNUMBER) -> PairedDevice | None:
        if number not in (1, RECEIVER_DEVNUMBER):
            return None
        if self._device is None:
            self._device = self._build_device()
        return self._device

    def enumerate_devices(self) -> list[PairedDevice]:
        device = self.get_device()
        return [device] if device is not None else []

    def ping_device(
        self, number: int = RECEIVER_DEVNUMBER, *, timeout: float = 1.5
    ) -> float | None:
        return self._connection().ping(number, timeout=timeout)

    def get_change_host_info(self, number: int) -> ChangeHostInfo | None:
        connection = self._connection()
        feature_index = connection.get_feature_index(number, FEATURE_CHANGE_HOST)
        if feature_index is None:
            return None
        reply = connection.request(
            number, (feature_index << 8) | CHANGE_HOST_READ_FUNCTION
        )
        if not reply or len(reply) < 2:
            return None
        return ChangeHostInfo(
            feature_index=feature_index, num_hosts=reply[0], current_host=reply[1]
        )

    def set_current_host(self, number: int, feature_index: int, host: int) -> None:
        self._connection().request(
            number,
            (feature_index << 8) | CHANGE_HOST_WRITE_FUNCTION,
            bytes([host]),
            no_reply=True,
        )


DeviceEndpoint = Receiver | DirectDevice
