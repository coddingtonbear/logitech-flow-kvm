import logging
import threading
from collections.abc import Callable

from .models import Notification
from .protocol import make_notification
from .transport import HidRawIO

logger = logging.getLogger(__name__)

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
                        try:
                            self._callback(notification)
                        except Exception:
                            # A callback failure (e.g. a network hiccup while
                            # reporting a connect event upstream) must never
                            # kill this thread: it is the only source of
                            # connect/disconnect events, and nothing restarts
                            # it. Log and keep listening.
                            logger.exception(
                                "Notification callback failed; continuing to listen"
                            )
        finally:
            self._active.clear()

    def stop(self) -> None:
        self._active.clear()
