import logging
import threading
from collections.abc import Callable
from functools import partial

from .hidpp import DeviceEndpoint
from .hidpp import DirectDevice
from .hidpp import Notification
from .hidpp import NotificationListener
from .hidpp import PairedDevice
from .hidpp import Receiver
from .hidpp import find_direct_devices
from .hidpp import find_receivers
from .node_protocol import DeviceAdvertisement
from .udev_monitor import HidrawUdevMonitor
from .util import change_device_host
from .util import parse_connection_status

logger = logging.getLogger(__name__)
UDEV_SETTLE_DELAY = 0.2


def discover_device_advertisements() -> list[DeviceAdvertisement]:
    endpoints: list[DeviceEndpoint] = []
    devices: list[DeviceAdvertisement] = []
    try:
        endpoints.extend(Receiver(info) for info in find_receivers())
        endpoints.extend(DirectDevice(info) for info in find_direct_devices())
        for endpoint in endpoints:
            if isinstance(endpoint, DirectDevice):
                found = endpoint.get_device()
                paired = [found] if found is not None else []
            else:
                paired = list(endpoint.enumerate_devices())
            devices.extend(
                DeviceAdvertisement(
                    id=device.id,
                    product=device.wpid,
                    name=device.codename or device.kind,
                    kind=device.kind,
                    connected=isinstance(endpoint, DirectDevice),
                )
                for device in paired
            )
        return devices
    finally:
        for endpoint in endpoints:
            endpoint.close()


class NodeDeviceManager:
    """Maintain the set of devices that exists *now*, including Bluetooth."""

    def __init__(
        self,
        on_change: Callable[[], None],
        on_connected: Callable[[str], None],
        on_disconnected: Callable[[str], None] | None = None,
        on_problem: Callable[[str], None] | None = None,
    ):
        self.on_change = on_change
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected or (lambda _device_id: None)
        self.on_problem = on_problem or (lambda _problem: None)
        self._lock = threading.RLock()
        self._endpoints: list[DeviceEndpoint] = []
        self._listeners: list[NotificationListener] = []
        self._devices: dict[str, PairedDevice] = {}
        self._connected: set[str] = set()
        self._direct_ids: set[str] = set()
        # Every identifier a device has been seen under -> its current id.
        # Deliberately kept across refreshes: a configuration written with a
        # Bluetooth address must still recognise the device's disconnection
        # after its hidraw node (and so the device) has disappeared.
        self._aliases: dict[str, str] = {}
        self._udev = HidrawUdevMonitor(self._udev_event)
        self._refresh_timer: threading.Timer | None = None

    def start(self) -> None:
        try:
            self.refresh()
        except Exception as error:
            problem = f"Initial device discovery failed: {error}"
            logger.exception(problem)
            self.on_problem(problem)
        try:
            self._udev.start()
        except OSError as error:
            problem = f"Dynamic hidraw monitoring could not start: {error}"
            logger.exception(problem)
            self.on_problem(problem)

    def stop(self) -> None:
        self._udev.stop()
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        with self._lock:
            self._teardown()

    def advertisements(self) -> list[DeviceAdvertisement]:
        with self._lock:
            return [
                DeviceAdvertisement(
                    id=device.id,
                    product=device.wpid,
                    name=device.codename or device.kind,
                    kind=device.kind,
                    connected=device.id in self._connected,
                )
                for device in self._devices.values()
            ]

    def canonical_id(self, device_id: str) -> str:
        """Return the id this manager reports for `device_id`.

        Configuration may name a device by an older identifier (a Bluetooth
        address rather than the unit ID shared with Bolt receivers).
        """
        with self._lock:
            return self._aliases.get(device_id.upper(), device_id)

    def switch_connected(self, follower_ids: list[str], target_host: int) -> None:
        with self._lock:
            for configured_id in follower_ids:
                follower_id = self.canonical_id(configured_id)
                device = self._devices.get(follower_id)
                if device is None or follower_id not in self._connected:
                    continue
                try:
                    change_device_host(device, target_host)
                    logger.info("Switched %s to host %s", follower_id, target_host)
                except Exception:
                    logger.exception(
                        "Could not switch connected device %s to host %s",
                        follower_id,
                        target_host,
                    )

    def refresh(self) -> None:
        newly_connected: list[str] = []
        newly_disconnected: list[str] = []
        with self._lock:
            previously_connected = set(self._connected)
            previous_direct_ids = set(self._direct_ids)
            self._teardown()
            for receiver_info in find_receivers():
                self._endpoints.append(Receiver(receiver_info))
            for direct_info in find_direct_devices():
                self._endpoints.append(DirectDevice(direct_info))

            for endpoint in self._endpoints:
                if isinstance(endpoint, DirectDevice):
                    device = endpoint.get_device()
                    devices = [device] if device is not None else []
                else:
                    devices = list(endpoint.enumerate_devices())
                for device in devices:
                    self._devices[device.id] = device
                    for alias in (device.id, *device.aliases):
                        self._aliases[alias.upper()] = device.id
                    if isinstance(endpoint, DirectDevice):
                        self._direct_ids.add(device.id)
                        self._connected.add(device.id)
                        if device.id not in previously_connected:
                            newly_connected.append(device.id)

                if not isinstance(endpoint, DirectDevice):
                    endpoint.enable_connection_notifications()
                listener = NotificationListener(
                    endpoint.path, partial(self._notification, endpoint)
                )
                listener.start()
                self._listeners.append(listener)
                if not isinstance(endpoint, DirectDevice):
                    endpoint.notify_devices()
            newly_disconnected = list(previous_direct_ids - self._direct_ids)
        for device_id in newly_disconnected:
            self.on_disconnected(device_id)
        for device_id in newly_connected:
            self.on_connected(device_id)
        self.on_change()

    def _teardown(self) -> None:
        for listener in self._listeners:
            listener.stop()
        self._listeners.clear()
        for endpoint in self._endpoints:
            endpoint.close()
        self._endpoints.clear()
        self._devices.clear()
        self._connected.clear()
        self._direct_ids.clear()

    def _udev_event(self, action: str, path: str) -> None:
        logger.info("hidraw device %s: %s", action, path)
        with self._lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            self._refresh_timer = threading.Timer(
                UDEV_SETTLE_DELAY, self._refresh_after_udev
            )
            self._refresh_timer.daemon = True
            self._refresh_timer.start()

    def _refresh_after_udev(self) -> None:
        try:
            self.refresh()
        except Exception:
            problem = "Could not refresh devices after a hidraw change"
            logger.exception(problem)
            self.on_problem(problem)

    def _notification(
        self, endpoint: DeviceEndpoint, notification: Notification
    ) -> None:
        if notification.sub_id != 0x41:
            return
        with self._lock:
            device = next(
                (
                    value
                    for value in self._devices.values()
                    if value.receiver is endpoint
                    and value.number == notification.devnumber
                ),
                None,
            )
            if device is None:
                return
            status = parse_connection_status(notification.data)
            connected = status["link_status"] == 0
            was_connected = device.id in self._connected
            if connected:
                self._connected.add(device.id)
            else:
                self._connected.discard(device.id)
        if connected and not was_connected:
            self.on_connected(device.id)
        elif not connected and was_connected:
            self.on_disconnected(device.id)
        self.on_change()
