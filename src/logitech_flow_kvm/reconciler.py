"""Drives a set of "follower" devices toward a desired host, retrying until converged.

`change_device_host` (`util.py`) is fire-and-forget -- the device never
confirms it switched. The only way to get an actual guarantee out of that is
to keep retrying on a timer until we observe (via a real connect
notification) that the device landed where we wanted it. That's what this
does, replacing the old disconnect-triggered, one-shot "sleep and hope".
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .hidpp import PairedDevice
from .util import change_device_host

# Coarse safety-net interval -- normal operation wakes the loop immediately
# via `poke()`/`observe()` instead of waiting for this to elapse. It only
# matters for cases like a device that's asleep or mid-roam, where nobody
# has observed it connecting anywhere yet.
RECONCILE_INTERVAL = 2.0


class Reconciler(threading.Thread):
    """Continuously nudges `devices` toward whatever `get_desired_host()` returns.

    A device can only be commanded while it's actually connected to *this*
    receiver -- that's the only state a receiver can observe about a device
    it doesn't currently hold a radio link to (never where it went instead).
    So the reconciliation rule is simply: if a device is connected here, and
    here isn't the desired host, tell it to leave. It's safe to repeat that
    command every tick for as long as the mismatch persists, since it's
    idempotent and retries are the whole point.
    """

    def __init__(
        self,
        devices: list[PairedDevice],
        get_desired_host: Callable[[], int | None],
        host_number: int,
        on_error: Callable[[PairedDevice, Exception], None] | None = None,
    ):
        super().__init__(daemon=True)
        self._devices = devices
        self._get_desired_host = get_desired_host
        self._host_number = host_number
        self._on_error = on_error
        self._connected: dict[PairedDevice, bool] = dict.fromkeys(devices, False)
        self._wake = threading.Event()
        self._stop = threading.Event()

    def observe(self, device: PairedDevice, connected: bool) -> None:
        """Record positive evidence of whether `device` is connected here."""
        if device not in self._connected:
            return
        self._connected[device] = connected
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
        for device in self._devices:
            if not self._connected[device]:
                continue
            try:
                change_device_host(device, desired_host)
            except Exception as error:
                # A device can easily be unreachable for the instant this
                # live HID++ round-trip takes -- e.g. it's already mid-roam
                # to somewhere else. That's normal, not fatal: the whole
                # guarantee this loop provides comes from retrying forever,
                # so one device's transient failure must never kill this
                # thread (which would silently stop reconciling *every*
                # device, forever) or skip the rest of this tick's devices.
                if self._on_error is not None:
                    self._on_error(device, error)
