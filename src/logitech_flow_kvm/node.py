import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

import pyperclip

from . import constants
from .device_matching import match_device
from .monitors import Ddcutil
from .monitors import DdcutilError
from .node_config import NodeConfig
from .node_devices import NodeDeviceManager
from .node_protocol import BroadcastTransport
from .node_protocol import DeviceAdvertisement
from .node_protocol import NodeAdvertisement

logger = logging.getLogger(__name__)

ANNOUNCE_INTERVAL = 2.0
LEADER_EVENT_DEDUPLICATION_WINDOW = 5.0
PEER_EXPIRY = ANNOUNCE_INTERVAL * 3
PEER_DISCOVERY_WARNING = 10.0
NO_PEERS_MESSAGE = (
    "No peers discovered. Start flow-node on another host with the same secret; "
    "if it is running, check LAN broadcast and firewall settings."
)


@dataclass
class PeerStatus:
    hostname: str
    address: str
    host_number: int
    devices: list[DeviceAdvertisement] = field(default_factory=list)
    last_seen: float = field(default_factory=time.monotonic)


class FlowNode:
    def __init__(
        self,
        config: NodeConfig,
        secret: str,
        *,
        port: int = constants.DEFAULT_PORT,
        on_change: Callable[[], None] | None = None,
        ddcutil: Ddcutil | None = None,
    ):
        self.config = config
        self.secret = secret
        self.hostname = socket.gethostname()
        self.node_id = self.hostname
        self.on_change = on_change or (lambda: None)
        self.ddcutil = ddcutil or Ddcutil()
        self.peers: dict[str, PeerStatus] = {}
        self.device_matches: dict[tuple[str, str], str] = {}
        self._peer_lock = threading.Lock()
        self.last_problem: str | None = None
        self._stop = threading.Event()
        self.devices = NodeDeviceManager(
            self._changed,
            self._device_connected,
            self._device_disconnected,
            self._device_problem,
        )
        self.transport = BroadcastTransport(
            secret=secret,
            port=port,
            on_message=self._message,
            on_invalid_packet=self._invalid_packet,
        )
        self._announcer = threading.Thread(target=self._announce_loop, daemon=True)
        self._remote_clipboard: str | None = None
        self._last_leader_announcement = 0.0
        self._started_at = time.monotonic()
        self._transport_started = False

    def start(self) -> None:
        if not 1 <= self.config.host_number <= constants.MAX_HOST_NUMBER:
            self.last_problem = (
                f"Host number {self.config.host_number} is not an Easy-Switch "
                f"channel (1-{constants.MAX_HOST_NUMBER}); peers cannot switch "
                "devices to this host. Fix it in setup."
            )
            logger.warning(self.last_problem)
        try:
            self.transport.start()
            self._transport_started = True
        except OSError as error:
            self.last_problem = f"LAN broadcast could not start: {error}"
            logger.exception(self.last_problem)
        self.devices.start()
        self._announcer.start()
        self.announce()

    def stop(self) -> None:
        self._stop.set()
        self.devices.stop()
        if self._transport_started:
            self.transport.stop()
        self._announcer.join(timeout=2)

    def advertisement(
        self, target_host: int | None = None, clipboard: str | None = None
    ) -> NodeAdvertisement:
        return NodeAdvertisement(
            node_id=self.node_id,
            hostname=self.hostname,
            host_number=self.config.host_number,
            devices=self.devices.advertisements(),
            leader_id=self.config.leader_id,
            target_host=target_host,
            clipboard=clipboard,
        )

    def announce(
        self, target_host: int | None = None, clipboard: str | None = None
    ) -> None:
        if not self._transport_started:
            return
        self.transport.send(self.advertisement(target_host, clipboard))

    def peer_statuses(self) -> list[PeerStatus]:
        with self._peer_lock:
            return list(self.peers.values())

    def _is_leader(self, device_id: str) -> bool:
        leader_id = self.config.leader_id
        return (
            device_id == leader_id or self.devices.canonical_id(leader_id) == device_id
        )

    def _device_connected(self, device_id: str) -> None:
        if not self._is_leader(device_id):
            return
        now = time.monotonic()
        if now - self._last_leader_announcement < LEADER_EVENT_DEDUPLICATION_WINDOW:
            logger.info("Ignoring duplicate leader connection for %s", device_id)
            return
        self._last_leader_announcement = now
        logger.info(
            "Leader %s connected here; announcing host %s",
            device_id,
            self.config.host_number,
        )
        if self.config.clipboard_enabled and self._remote_clipboard is not None:
            try:
                pyperclip.copy(self._remote_clipboard)
            except pyperclip.PyperclipException as error:
                self.last_problem = f"Could not set clipboard: {error}"
                logger.warning(self.last_problem)
        if self.config.monitor_inputs:
            threading.Thread(target=self._switch_monitors, daemon=True).start()
        self.announce(self.config.host_number)

    def _switch_monitors(self) -> None:
        try:
            monitors = {monitor.id: monitor for monitor in self.ddcutil.detect()}
            for configured in self.config.monitor_inputs:
                monitor = monitors.get(configured.monitor_id)
                if monitor is None:
                    logger.warning(
                        "Configured monitor is not currently detected: %s",
                        configured.monitor_id,
                    )
                    continue
                try:
                    self.ddcutil.set_input(monitor, configured.input_source)
                except DdcutilError as error:
                    self.last_problem = (
                        f"Could not switch {monitor.description}: {error}"
                    )
                    logger.warning(self.last_problem)
                    self._changed()
                else:
                    logger.info(
                        "Switched %s to input source 0x%02x",
                        monitor.description,
                        configured.input_source,
                    )
        except DdcutilError as error:
            self.last_problem = f"Could not switch monitor input: {error}"
            logger.warning(self.last_problem)
            self._changed()

    def _device_disconnected(self, device_id: str) -> None:
        if not self._is_leader(device_id) or not self.config.clipboard_enabled:
            return
        try:
            clipboard = pyperclip.paste()
            self.announce(clipboard=clipboard)
            logger.info("Shared %d clipboard character(s)", len(clipboard))
        except (pyperclip.PyperclipException, OSError, ValueError) as error:
            self.last_problem = f"Could not share clipboard: {error}"
            logger.warning(self.last_problem)
            self._changed()

    def _message(self, message: NodeAdvertisement, address: str) -> None:
        if message.node_id == self.node_id:
            return
        with self._peer_lock:
            self.peers[message.node_id] = PeerStatus(
                hostname=message.hostname,
                address=address,
                host_number=message.host_number,
                devices=message.devices,
            )
            if message.host_number == self.config.host_number:
                self.last_problem = (
                    f"Host number {self.config.host_number} is also used by "
                    f"{message.hostname} ({address}); change it in setup."
                )
            local_devices = self.devices.advertisements()
            for remote_device in message.devices:
                matched = match_device(remote_device, local_devices)
                key = (message.node_id, remote_device.id)
                if matched is None:
                    self.device_matches.pop(key, None)
                else:
                    self.device_matches[key] = matched.id
        if self.last_problem == NO_PEERS_MESSAGE:
            self.last_problem = None
        if message.clipboard is not None:
            self._remote_clipboard = message.clipboard
        if message.target_host is not None:
            logger.info(
                "Node %s reports leader on host %s",
                message.hostname,
                message.target_host,
            )
            self.devices.switch_connected(self.config.follower_ids, message.target_host)
        self._changed()

    def _invalid_packet(self, address: str, error: Exception) -> None:
        self.last_problem = f"Ignored packet from {address}: {error}"
        logger.warning(self.last_problem)
        self._changed()

    def _device_problem(self, problem: str) -> None:
        self.last_problem = problem
        self._changed()

    def _changed(self) -> None:
        self.on_change()

    def _announce_loop(self) -> None:
        while not self._stop.wait(ANNOUNCE_INTERVAL):
            cutoff = time.monotonic() - PEER_EXPIRY
            with self._peer_lock:
                expired = [
                    node_id
                    for node_id, peer in self.peers.items()
                    if peer.last_seen < cutoff
                ]
                for node_id in expired:
                    del self.peers[node_id]
            if expired:
                self._changed()
            if (
                not self.peer_statuses()
                and time.monotonic() - self._started_at >= PEER_DISCOVERY_WARNING
                and self.last_problem is None
            ):
                self.last_problem = NO_PEERS_MESSAGE
                logger.warning(NO_PEERS_MESSAGE)
                self._changed()
            try:
                self.announce()
            except OSError:
                logger.exception("Could not broadcast node status")
