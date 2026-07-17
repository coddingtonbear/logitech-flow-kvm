from __future__ import annotations

import dataclasses

from .exceptions import ProtocolError
from .models import DEVICE_KIND
from .models import ChangeHostInfo
from .models import ReceiverInfo
from .protocol import RECEIVER_DEVNUMBER
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

    receiver: Receiver
    number: int
    wpid: str
    kind: str
    serial: str | None
    codename: str | None

    @property
    def id(self) -> str:
        return self.serial or self.wpid

    @property
    def path(self) -> str:
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
