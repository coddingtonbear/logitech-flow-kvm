"""Supervises receivers so a USB unplug/replug doesn't require a restart.

Everything else in this package treats a receiver as immortal: `Receiver`
opens its hidraw node once, `PairedDevice` is frozen with a reference to that
`Receiver`, and `NotificationListener` dies silently when its node vanishes.
`ReceiverManager` is the piece that makes all of that survivable: it watches
for a listener dying, then closes everything, re-runs discovery until the
receivers are back (their hidraw paths usually change across a replug), and
asks the application -- via its `rebind` callback -- to re-resolve its
devices against the freshly opened receivers.
"""

import logging
import threading
from collections.abc import Callable
from functools import partial

from .discovery import find_receivers
from .listener import NotificationListener
from .models import Notification
from .receiver import Receiver

logger = logging.getLogger(__name__)

# How long to wait between rediscovery attempts while receivers or devices
# are missing.
REDISCOVERY_INTERVAL = 2.0


class ReceiverManager(threading.Thread):
    """Owns the receivers' post-startup lifecycle: notification listeners,
    connection-notification setup, and recovery after a receiver drops off
    the bus.

    `receivers` are the already-open receivers resolved at application
    startup; the manager takes over responsibility for (eventually) closing
    them.

    `rebind(receivers)` is called with freshly opened receivers after every
    rebuild. It should re-resolve `PairedDevice` references and swap them
    into whatever holds them (reconciler, status maps, ...), raising -- e.g.
    `DeviceNotFound` -- if something it needs is missing; the manager then
    closes the receivers, waits, and retries discovery.

    `callback(receiver, notification)` receives notifications from every
    supervised receiver, exactly as a directly-constructed
    `NotificationListener` would deliver them.
    """

    def __init__(
        self,
        receivers: list[Receiver],
        rebind: Callable[[list[Receiver]], None],
        callback: Callable[[Receiver, Notification], None],
        rediscovery_interval: float = REDISCOVERY_INTERVAL,
    ):
        super().__init__(daemon=True)
        self._receivers = receivers
        self._rebind = rebind
        self._callback = callback
        self._rediscovery_interval = rediscovery_interval
        self._listeners: list[NotificationListener] = []
        self._lost = threading.Event()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        self._lost.set()

    def run(self) -> None:
        try:
            self._activate()
        except Exception:
            logger.exception("Could not start listening to receivers; recovering")
            self._lost.set()

        while True:
            self._lost.wait()
            if self._stop_event.is_set():
                break
            self._lost.clear()
            logger.warning(
                "Lost contact with a receiver; waiting for it to come back..."
            )
            self._recover()

        self._teardown()

    def _activate(self) -> None:
        """Start listening on the current receivers.

        Listeners are started *before* `notify_devices()` asks each receiver
        to re-announce its devices' connection status, so none of those
        notifications can be missed.
        """
        for receiver in self._receivers:
            receiver.enable_connection_notifications()
            listener = NotificationListener(
                receiver.path,
                partial(self._callback, receiver),
                on_disconnect=self._lost.set,
            )
            listener.start()
            self._listeners.append(listener)
        for receiver in self._receivers:
            receiver.notify_devices()

    def _teardown(self) -> None:
        for listener in self._listeners:
            listener.stop()
        self._listeners = []
        for receiver in self._receivers:
            try:
                receiver.close()
            except OSError:
                pass
        self._receivers = []

    def _recover(self) -> None:
        """Rebuild receivers, devices, and listeners; retry until it works.

        Any failure along the way (receivers still absent, a wanted device
        missing from the ones that are back, an I/O error against a receiver
        that vanished again mid-rebuild) just means: clean up, wait, retry.
        """
        while not self._stop_event.is_set():
            self._teardown()
            try:
                for info in find_receivers():
                    self._receivers.append(Receiver(info))
                self._rebind(list(self._receivers))
                self._activate()
            except Exception as error:
                logger.debug("Rediscovery attempt failed: %s", error)
                self._stop_event.wait(self._rediscovery_interval)
                continue
            logger.info(
                "Receiver(s) rediscovered: %s",
                ", ".join(receiver.path for receiver in self._receivers) or "none",
            )
            return
