import threading
from collections.abc import Callable

from .models import Notification
from .protocol import make_notification
from .transport import HidRawIO

# How long each read blocks before checking whether the thread should stop.
READ_POLL_INTERVAL = 1.0


class NotificationListener(threading.Thread):
    """Streams HID++ notifications (e.g. device connect/disconnect) from a receiver.

    Opens its own file descriptor to the receiver's hidraw node, independent of
    any descriptor used for request/reply calls -- a hidraw node broadcasts
    incoming reports to every open reader, but a single reader cannot safely be
    shared between a blocking listener loop and synchronous request() calls
    from another thread, since either could consume the other's report.
    """

    def __init__(self, receiver_path: str, callback: Callable[[Notification], None]):
        super().__init__(daemon=True)
        self._receiver_path = receiver_path
        self._callback = callback
        self._active = threading.Event()

    def run(self) -> None:
        self._active.set()
        try:
            with HidRawIO(self._receiver_path) as io:
                while self._active.is_set():
                    try:
                        reply = io.read(READ_POLL_INTERVAL)
                    except OSError:
                        break
                    if reply is None:
                        continue
                    report_id, devnumber, data = reply
                    notification = make_notification(report_id, devnumber, data)
                    if notification is not None:
                        self._callback(notification)
        finally:
            self._active.clear()

    def stop(self) -> None:
        self._active.clear()
