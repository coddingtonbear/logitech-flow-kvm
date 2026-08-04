"""Drives a set of "follower" devices toward a desired host, retrying until converged.

`change_device_host` (`util.py`) is fire-and-forget -- the device never
confirms it switched. The only way to get an actual guarantee out of that is
to keep retrying on a timer until we observe (via a real connect
notification) that the device landed where we wanted it. That's what this
does, replacing the old disconnect-triggered, one-shot "sleep and hope".
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .hidpp import DeviceUnreachable
from .hidpp import PairedDevice
from .util import change_device_host

logger = logging.getLogger(__name__)

# Coarse safety-net interval -- normal operation wakes the loop immediately
# via `poke()`/`observe()` instead of waiting for this to elapse. It only
# matters for cases like a device that's asleep or mid-roam, where nobody
# has observed it connecting anywhere yet.
RECONCILE_INTERVAL = 2.0

# How often to re-check, by pinging it, a device we believe *isn't* here while
# a mismatch persists. Connection state otherwise comes only from connect/
# disconnect notifications, and a lost notification would wedge the loop
# permanently: it would either hammer a device that left (the failure mode
# this exists to bound) or ignore one that arrived. Deliberately much coarser
# than RECONCILE_INTERVAL -- it is a repair mechanism for a rare event, not
# the normal path, and a ping to a sleeping device blocks until it times out.
PROBE_INTERVAL = 30.0


class Reconciler(threading.Thread):
    """Continuously nudges `devices` toward whatever `get_desired_host()` returns.

    A device can only be commanded while it's actually connected to *this*
    receiver -- that's the only state a receiver can observe about a device
    it doesn't currently hold a radio link to (never where it went instead).
    So the reconciliation rule is simply: if a device is connected here, and
    here isn't the desired host, tell it to leave. It's safe to repeat that
    command every tick for as long as the mismatch persists, since it's
    idempotent and retries are the whole point.

    Which devices are connected here is learned from connect/disconnect
    notifications (`observe()`), but never *only* from them: a lost
    notification would otherwise leave this loop with a permanently wrong
    belief and no way to correct it. So the loop also treats the receiver
    answering "no link to that device" as evidence of absence, and re-pings
    devices it believes absent every `PROBE_INTERVAL` in case it missed their
    arrival. `on_observation(device, connected)` reports the belief changes
    this loop works out for itself (as opposed to the ones it was told about
    via `observe()`, which the caller already knows about).
    """

    def __init__(
        self,
        devices: list[PairedDevice],
        get_desired_host: Callable[[], int | None],
        host_number: int,
        on_error: Callable[[PairedDevice, Exception], None] | None = None,
        on_observation: Callable[[PairedDevice, bool], None] | None = None,
    ):
        super().__init__(daemon=True)
        self._devices = devices
        self._get_desired_host = get_desired_host
        self._host_number = host_number
        self._on_error = on_error
        self._on_observation = on_observation
        self._connected: dict[PairedDevice, bool] = dict.fromkeys(devices, False)
        # The last failure reported for each device, so a failure that
        # persists across ticks is reported once rather than every tick.
        self._errors: dict[PairedDevice, str] = {}
        self._last_probe = time.monotonic()
        self._wake = threading.Event()
        self._stop = threading.Event()

    def observe(self, device: PairedDevice, connected: bool) -> None:
        """Record positive evidence of whether `device` is connected here."""
        if device not in self._connected:
            return
        self._connected[device] = connected
        self._errors.pop(device, None)
        self.poke()

    def set_devices(self, devices: list[PairedDevice]) -> None:
        """Replace the reconciled device set (e.g. after a receiver was
        rediscovered post-replug and its `PairedDevice`s were rebuilt).

        Connection state resets to unobserved; the receiver's re-announced
        connection notifications repopulate it almost immediately.
        """
        self._connected = dict.fromkeys(devices, False)
        self._errors = {}
        self._devices = devices
        self.poke()

    def poke(self) -> None:
        """Wake the loop immediately instead of waiting for the next tick."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.reconcile_once()
            self._wake.wait(RECONCILE_INTERVAL)
            self._wake.clear()

    def reconcile_once(self) -> None:
        desired_host = self._get_desired_host()
        if desired_host is None or desired_host == self._host_number:
            return

        probing = self._probe_is_due()
        for device in self._devices:
            # `.get`: `set_devices` may swap `_devices`/`_connected` from
            # another thread between the two reads.
            if not self._connected.get(device, False):
                if not probing or not self._probe(device):
                    continue
            self._drive(device, desired_host)

    def _probe_is_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_probe < PROBE_INTERVAL:
            return False
        self._last_probe = now
        return True

    def _probe(self, device: PairedDevice) -> bool:
        """Ping a device we believe isn't here, in case its connect
        notification never reached us. Returns whether it answered."""
        try:
            reachable = device.receiver.ping_device(device.number) is not None
        except Exception as error:
            # The receiver itself is in trouble (e.g. unplugged mid-ping);
            # that's the ReceiverManager's problem, not this loop's.
            logger.debug("Could not probe %s: %s", device.id, error)
            return False
        if reachable:
            self._record(device, True)
        return reachable

    def _drive(self, device: PairedDevice, desired_host: int) -> None:
        try:
            change_device_host(device, desired_host)
        except DeviceUnreachable:
            # The receiver answered on the device's behalf: it holds no link
            # to it. That is positive evidence of absence -- exactly what a
            # disconnect notification would have told us, and available even
            # when that notification was lost. Believing it is what stops the
            # loop from hammering a device that already left (and saying so
            # in the log every couple of seconds, forever). A connect
            # notification, or the periodic probe, re-arms us.
            self._record(device, False)
        except Exception as error:
            # A device can easily be unreachable for the instant this
            # live HID++ round-trip takes -- e.g. it's already mid-roam
            # to somewhere else. That's normal, not fatal: the whole
            # guarantee this loop provides comes from retrying forever,
            # so one device's transient failure must never kill this
            # thread (which would silently stop reconciling *every*
            # device, forever) or skip the rest of this tick's devices.
            self._report_error(device, error)
        else:
            self._errors.pop(device, None)

    def _record(self, device: PairedDevice, connected: bool) -> None:
        """Record a connection state this loop worked out for itself."""
        if device not in self._connected or self._connected[device] == connected:
            return
        self._connected[device] = connected
        self._errors.pop(device, None)
        if self._on_observation is not None:
            self._on_observation(device, connected)

    def _report_error(self, device: PairedDevice, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        if self._errors.get(device) == message:
            # The same failure as last tick. We are still retrying, and will
            # keep retrying, but repeating that at WARNING every couple of
            # seconds for as long as it lasts is noise, not information.
            logger.debug("Still cannot switch %s: %s", device.id, error)
            return
        self._errors[device] = message
        if self._on_error is not None:
            self._on_error(device, error)
