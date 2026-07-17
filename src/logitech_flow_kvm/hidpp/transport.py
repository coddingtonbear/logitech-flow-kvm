import os
import select
import time

SHORT_MESSAGE_ID = 0x10
LONG_MESSAGE_ID = 0x11
SHORT_REPORT_SIZE = 7
LONG_REPORT_SIZE = 20
MAX_READ_SIZE = 32

_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY = 0.1


class HidRawIO:
    """Raw read/write access to a single ``/dev/hidraw*`` node.

    A receiver's hidraw node broadcasts every incoming report to all open file
    descriptors, so each thread that needs to block on reads (e.g. a
    notification listener) should open its own ``HidRawIO`` rather than share
    one across threads -- otherwise a blocking read on one thread can steal
    the reply another thread's request is waiting for.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd = os.open(path, os.O_RDWR)

    def close(self) -> None:
        os.close(self._fd)

    def __enter__(self) -> "HidRawIO":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None:
        if long_message or len(payload) > SHORT_REPORT_SIZE - 2:
            body = payload.ljust(LONG_REPORT_SIZE - 2, b"\x00")
            report = bytes([LONG_MESSAGE_ID, devnumber]) + body
        else:
            body = payload.ljust(SHORT_REPORT_SIZE - 2, b"\x00")
            report = bytes([SHORT_MESSAGE_ID, devnumber]) + body

        written = 0
        for attempt in range(_WRITE_RETRIES):
            try:
                written = os.write(self._fd, report)
            except BrokenPipeError:
                if attempt == _WRITE_RETRIES - 1:
                    raise
                time.sleep(_WRITE_RETRY_DELAY)
                continue
            break

        if written != len(report):
            raise OSError(f"short write: {written}/{len(report)} bytes")

    def read(self, timeout: float) -> tuple[int, int, bytes] | None:
        """Read one report, or None if nothing arrived within `timeout` seconds."""
        rlist, _, _ = select.select([self._fd], [], [], timeout)
        if not rlist:
            return None
        data = os.read(self._fd, MAX_READ_SIZE)
        if not data:
            return None
        return data[0], data[1], data[2:]

    def drain(self) -> None:
        """Discard any reports already waiting in the kernel buffer."""
        while self.read(0) is not None:
            pass
