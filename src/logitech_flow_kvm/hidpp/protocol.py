import random
import struct
import threading
import time
from typing import Protocol

from .exceptions import ProtocolError
from .models import Notification

RECEIVER_DEVNUMBER = 0xFF
ROOT_FEATURE_INDEX = 0x00

_ERROR_INVALID_SUBID = 0x01
_ERROR_UNKNOWN_DEVICE = 0x08
_ERROR_RESOURCE_ERROR = 0x09

DEFAULT_TIMEOUT = 2.0


class Transport(Protocol):
    """The subset of `HidRawIO` this layer depends on, so tests can inject a fake."""

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None: ...

    def read(self, timeout: float) -> tuple[int, int, bytes] | None: ...

    def drain(self) -> None: ...


def make_notification(
    report_id: int, devnumber: int, data: bytes
) -> Notification | None:
    """Classify an incoming report as a notification, or None if it's a request reply.

    Mirrors solaar's `base.make_notification`: HID++1.0 register replies and
    HID++2.0 error replies have bit 0x80 set on the sub_id and are not notifications.
    """
    if len(data) < 2:
        return None
    sub_id = data[0]
    if sub_id & 0x80 == 0x80:
        return None

    address = data[1]
    if sub_id == 0x00 and address & 0x0F == 0x00:
        return None

    is_notification = (
        sub_id >= 0x40
        or (sub_id in (0x07, 0x0D) and len(data) == 5 and data[4:5] == b"\x00")
        or (sub_id == 0x17 and len(data) == 5)
        or (address & 0x0F == 0x00)
    )
    if is_notification:
        return Notification(
            report_id=report_id,
            devnumber=devnumber,
            sub_id=sub_id,
            address=address,
            data=data[2:],
        )
    return None


class HidppConnection:
    """Request/reply and ping logic for HID++1.0/2.0, layered over a raw transport."""

    def __init__(self, transport: Transport):
        self._transport = transport
        # Serializes drain+write+read cycles across threads sharing this
        # connection (e.g. a Flask request thread and another caller both
        # acting on the same receiver) so one call can't consume another's
        # reply. This does not protect against a second, unrelated OS
        # process also talking to the same physical receiver.
        self._lock = threading.Lock()

    def request(
        self,
        devnumber: int,
        request_id: int,
        params: bytes = b"",
        *,
        no_reply: bool = False,
        long_message: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        randomize_software_id: bool = True,
    ) -> bytes | None:
        """Make a request and wait for its matching reply.

        :raises ProtocolError: if the receiver/device returned an error reply.
        :returns: the reply payload (without the echoed request header), or
            None if no reply was expected or none arrived within `timeout`.
        """
        if randomize_software_id and request_id < 0x8000:
            request_id = (request_id & 0xFFF0) | 0x08 | random.getrandbits(3)

        request_header = struct.pack("!H", request_id)

        with self._lock:
            self._transport.drain()
            self._transport.write(devnumber, request_header + params, long_message)

            if no_reply:
                return None

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                reply = self._transport.read(remaining)
                if reply is None:
                    continue
                report_id, reply_devnumber, reply_data = reply
                if reply_devnumber != devnumber:
                    continue

                if reply_data[:1] == b"\x8f" and reply_data[1:3] == request_header:
                    raise ProtocolError(reply_data[3])
                if reply_data[:1] == b"\xff" and reply_data[1:3] == request_header:
                    raise ProtocolError(reply_data[3])
                if reply_data[:2] == request_header:
                    return reply_data[2:]

    def read_register(
        self,
        devnumber: int,
        register: int,
        params: bytes = b"",
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes | None:
        request_id = 0x8100 | (register & 0x2FF)
        return self.request(devnumber, request_id, params, timeout=timeout)

    def write_register(
        self,
        devnumber: int,
        register: int,
        value: bytes,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes | None:
        request_id = 0x8000 | (register & 0x2FF)
        return self.request(devnumber, request_id, value, timeout=timeout)

    def ping(self, devnumber: int, *, timeout: float = DEFAULT_TIMEOUT) -> float | None:
        """Check whether a device is reachable.

        :returns: the HID++ protocol version supported by the device (e.g. 4.5),
            or None if it is not currently reachable.
        """
        marker = random.getrandbits(8)
        request_id = 0x0018 | random.getrandbits(3)
        request_header = struct.pack("!H", request_id)
        params = bytes([0, 0, marker])

        with self._lock:
            self._transport.drain()
            self._transport.write(
                devnumber, request_header + params, long_message=False
            )

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                reply = self._transport.read(remaining)
                if reply is None:
                    continue
                _report_id, reply_devnumber, reply_data = reply
                if reply_devnumber != devnumber:
                    continue

                if reply_data[:2] == request_header and reply_data[4:5] == bytes(
                    [marker]
                ):
                    return reply_data[2] + reply_data[3] / 10.0

                if reply_data[:1] == b"\x8f" and reply_data[1:3] == request_header:
                    error = reply_data[3]
                    if error == _ERROR_INVALID_SUBID:  # a valid HID++1.0 device replied
                        return 1.0
                    if error in (_ERROR_RESOURCE_ERROR, _ERROR_UNKNOWN_DEVICE):
                        return None

    def get_feature_index(self, devnumber: int, feature_id: int) -> int | None:
        reply = self.request(
            devnumber, ROOT_FEATURE_INDEX << 8, struct.pack("!H", feature_id)
        )
        if not reply:
            return None
        index = reply[0]
        return index or None
