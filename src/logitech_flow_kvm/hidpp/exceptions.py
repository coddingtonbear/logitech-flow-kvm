# HID++1.0 error codes, as returned in a 0x8F error reply. See section 3.4 of
# the Unifying receiver specification (and solaar's `hidpp10.ERROR`).
ERROR_INVALID_SUBID = 0x01
ERROR_UNKNOWN_DEVICE = 0x08
ERROR_RESOURCE_ERROR = 0x09

# The two errors a receiver answers with on a device's behalf when it holds no
# radio link to that device -- it's asleep, out of range, or (the case this
# project cares about) has switched to another host. Unlike the rest, these say
# nothing about whether the request itself was valid.
UNREACHABLE_ERRORS = frozenset({ERROR_UNKNOWN_DEVICE, ERROR_RESOURCE_ERROR})


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
