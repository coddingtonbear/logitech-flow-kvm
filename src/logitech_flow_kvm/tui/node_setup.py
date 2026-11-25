from __future__ import annotations

from collections.abc import Callable

from textual.app import App
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button
from textual.widgets import Checkbox
from textual.widgets import Footer
from textual.widgets import Input
from textual.widgets import Label
from textual.widgets import Select
from textual.widgets import SelectionList

from ..constants import MAX_HOST_NUMBER
from ..monitors import Ddcutil
from ..monitors import DdcutilError
from ..monitors import Monitor
from ..monitors import MonitorCapabilities
from ..node_config import MonitorInputConfig
from ..node_config import NodeConfig
from ..node_protocol import DeviceAdvertisement


class NodeSetupApp(App[NodeConfig | None]):
    TITLE = "Logitech Flow KVM setup"
    CSS = """
    VerticalScroll { padding: 1 2; }
    Label { margin-top: 1; }
    #problem { color: $error; height: auto; }
    Button { margin-top: 1; }
    """

    def __init__(
        self,
        discover: Callable[[], list[DeviceAdvertisement]],
        current: NodeConfig | None = None,
        ddcutil: Ddcutil | None = None,
    ):
        super().__init__()
        self.discover = discover
        self.current = current
        self.ddcutil = ddcutil
        self.monitors: list[tuple[Monitor, MonitorCapabilities]] = []
        self.monitor_problem: str | None = None
        self.monitor_select_ids: dict[str, str] = {}
        self.discovery_problem: str | None = None
        try:
            self.devices = discover()
        except Exception as error:
            self.devices = []
            self.discovery_problem = f"Device discovery failed: {error}"
        self._discover_monitors()

    def compose(self) -> ComposeResult:
        options = self._options()
        selected_followers = set(self.current.follower_ids if self.current else [])
        with VerticalScroll():
            yield Label(
                "Configure this node. Switch a device to this host and choose "
                "Refresh if it is not listed."
            )
            yield Label("Host number")
            yield Input(
                value=str(self.current.host_number) if self.current else "",
                placeholder=f"Easy-Switch channel, 1-{MAX_HOST_NUMBER}",
                id="host-number",
                type="integer",
            )
            yield Label("Leader")
            yield Select(
                options,
                value=self.current.leader_id if self.current else Select.NULL,
                prompt="Choose the keyboard whose host button you press",
                id="leader",
            )
            yield Label("Followers")
            yield SelectionList(
                *[
                    (label, value, value in selected_followers)
                    for label, value in options
                ],
                id="followers",
            )
            yield Checkbox(
                "Synchronize clipboard",
                value=self.current.clipboard_enabled if self.current else True,
                id="clipboard",
            )
            yield Label("Monitor input switching")
            if self.monitors:
                for index, (monitor, capabilities) in enumerate(self.monitors):
                    select_id = f"monitor-input-{index}"
                    self.monitor_select_ids[select_id] = monitor.id
                    current_source = self._current_monitor_source(monitor.id)
                    yield Label(
                        f"{monitor.description} — {monitor.connector} — "
                        f"MCCS {capabilities.mccs_version}"
                    )
                    yield Select(
                        [
                            (f"0x{source.value:02x}: {source.name}", source.value)
                            for source in capabilities.input_sources
                        ],
                        value=(
                            current_source
                            if current_source is not None
                            else Select.NULL
                        ),
                        prompt="Do not switch this monitor",
                        id=select_id,
                    )
            else:
                yield Label(self.monitor_problem or "No DDC/CI monitors detected")
            yield Label(
                " | ".join(
                    problem
                    for problem in [self.discovery_problem, self.monitor_problem]
                    if problem
                ),
                id="problem",
            )
            yield Button("Refresh devices", id="refresh")
            yield Button("Save and start", variant="primary", id="save")
        yield Footer()

    @staticmethod
    def _label(device: DeviceAdvertisement) -> str:
        state = "connected" if device.connected else "paired"
        return f"{device.name} — {device.id} ({state})"

    def _options(self) -> list[tuple[str, str]]:
        options = [(self._label(device), device.id) for device in self.devices]
        known = {value for _, value in options}
        if self.current is not None:
            configured = [self.current.leader_id, *self.current.follower_ids]
            options.extend(
                (f"{device_id} (not currently detected)", device_id)
                for device_id in configured
                if device_id not in known
            )
        return options

    def _discover_monitors(self) -> None:
        if self.ddcutil is None:
            return
        try:
            for monitor in self.ddcutil.detect():
                try:
                    capabilities = self.ddcutil.capabilities(monitor)
                except DdcutilError as error:
                    self.monitor_problem = str(error)
                    continue
                self.monitors.append((monitor, capabilities))
        except DdcutilError as error:
            self.monitor_problem = str(error)

    def _current_monitor_source(self, monitor_id: str) -> int | None:
        if self.current is None:
            return None
        return next(
            (
                item.input_source
                for item in self.current.monitor_inputs
                if item.monitor_id == monitor_id
            ),
            None,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            self._refresh()
        elif event.button.id == "save":
            self._save()

    def _refresh(self) -> None:
        try:
            self.devices = self.discover()
        except Exception as error:
            self.query_one("#problem", Label).update(
                f"Device discovery failed: {error}"
            )
            return
        options = self._options()
        leader = self.query_one("#leader", Select)
        leader_value = leader.value
        leader.set_options(options)
        if leader_value in {value for _, value in options}:
            leader.value = leader_value
        followers = self.query_one("#followers", SelectionList)
        selected = set(followers.selected)
        followers.clear_options()
        followers.add_options(
            (label, value, value in selected) for label, value in options
        )
        self.query_one("#problem", Label).update(
            f"Found {len(self.devices)} device(s)."
        )

    def _save(self) -> None:
        problem = self.query_one("#problem", Label)
        try:
            host_number = int(self.query_one("#host-number", Input).value)
            if not 1 <= host_number <= MAX_HOST_NUMBER:
                raise ValueError
        except ValueError:
            problem.update(
                f"Host number must be this host's Easy-Switch channel "
                f"(1-{MAX_HOST_NUMBER})."
            )
            return
        leader = self.query_one("#leader", Select).value
        if leader is Select.NULL:
            problem.update("Choose one leader device.")
            return
        followers = list(self.query_one("#followers", SelectionList).selected)
        if str(leader) in followers:
            problem.update("The leader cannot also be a follower.")
            return
        if not followers:
            problem.update("Choose at least one follower device.")
            return
        monitor_inputs: list[MonitorInputConfig] = []
        detected_monitor_ids = {monitor.id for monitor, _ in self.monitors}
        for select_id, monitor_id in self.monitor_select_ids.items():
            value = self.query_one(f"#{select_id}", Select).value
            if isinstance(value, int):
                monitor_inputs.append(MonitorInputConfig(monitor_id, value))
        if self.current is not None:
            monitor_inputs.extend(
                item
                for item in self.current.monitor_inputs
                if item.monitor_id not in detected_monitor_ids
            )
        self.exit(
            NodeConfig(
                host_number=host_number,
                leader_id=str(leader),
                follower_ids=[str(value) for value in followers],
                clipboard_enabled=self.query_one("#clipboard", Checkbox).value,
                monitor_inputs=monitor_inputs,
            )
        )
