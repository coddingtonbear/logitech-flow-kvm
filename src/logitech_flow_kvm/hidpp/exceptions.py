# HID++1.0 error codes, as returned in a 0x8F error reply. See section 3.4 of
# the Unifying receiver specification (and solaar's `hidpp10.ERROR`).
ERROR_INVALID_SUBID = 0x01
ERROR_CONNECTION_REQUEST_FAILED = 0x04
ERROR_UNKNOWN_DEVICE = 0x08
ERROR_RESOURCE_ERROR = 0x09

# The errors a receiver answers with on a device's behalf when it holds no radio
# link to that device -- it's asleep, out of range, or (the case this project
# cares about) has switched to another host. Unlike the rest, these say nothing
# about whether the request itself was valid.
#
# Which code you get depends on the receiver family, not on the device or the
# condition. Observed directly, with both devices away and both pinging as
# unreachable: a Bolt receiver (0xC548, MX Keys Mini) answers 0x04 on every
# attempt, while a Unifying receiver (0xC52B, MX Anywhere 2S) answers 0x09 for
# the identical situation. Recognising only one family's code would leave the
# other's devices being driven at forever.
UNREACHABLE_ERRORS = frozenset(
    {
        ERROR_CONNECTION_REQUEST_FAILED,
        ERROR_UNKNOWN_DEVICE,
        ERROR_RESOURCE_ERROR,
    }
)


class HidppError(Exception):
    """Base class for all errors raised by the hidpp package."""


class ProtocolError(HidppError):
    """The receiver or device returned a HID++ error reply."""

    def __init__(self, error_code: int):
        self.error_code = error_code
        super().__init__(f"HID++ error 0x{error_code:02X}")

    @property
    def means_unreachable(self) -> bool:
        """Whether this error says "no link to that device" rather than
        "your request was bad" -- i.e. whether it is evidence about *where
        the device is* rather than about the request."""
        return self.error_code in UNREACHABLE_ERRORS


class NoSuchDevice(HidppError):
    """No device is paired at the requested device number."""


class DeviceUnreachable(HidppError):
    """The device is paired but not reachable (e.g. asleep, or on another host)."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        super().__init__(f"device {device_id} is not reachable through its receiver")


class ReceiverNotFound(HidppError):
    """No receiver matched the requested path or criteria."""
