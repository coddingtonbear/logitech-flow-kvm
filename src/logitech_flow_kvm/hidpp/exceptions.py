class HidppError(Exception):
    """Base class for all errors raised by the hidpp package."""


class ProtocolError(HidppError):
    """The receiver or device returned a HID++ error reply."""

    def __init__(self, error_code: int):
        self.error_code = error_code
        super().__init__(f"HID++ error 0x{error_code:02X}")


class NoSuchDevice(HidppError):
    """No device is paired at the requested device number."""


class DeviceUnreachable(HidppError):
    """The device is paired but not reachable (e.g. asleep, or on another host)."""


class ReceiverNotFound(HidppError):
    """No receiver matched the requested path or criteria."""
