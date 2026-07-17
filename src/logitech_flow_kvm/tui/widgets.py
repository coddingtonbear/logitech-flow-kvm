from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from rich.table import Table
from textual.widgets import Static


@dataclass
class DeviceStatus:
    id: str
    label: str
    connected: bool


@dataclass
class ServerStatus:
    host_number: int
    binding_interface: str
    port: int
    hostnames: list[str] = field(default_factory=list)
    leader: DeviceStatus | None = None
    followers: list[DeviceStatus] = field(default_factory=list)
    desired_host: int | None = None
    connected_guests: list[str] = field(default_factory=list)


@dataclass
class ClientStatus:
    host_number: int
    server: str
    connected_to_server: bool = False
    leader_host: int | None = None
    followers: list[DeviceStatus] = field(default_factory=list)


def _device_cell(device: DeviceStatus) -> str:
    state = "[green]connected[/]" if device.connected else "[red]disconnected[/]"
    return f"{device.label} ({device.id}) -- {state}"


def render_server_status(status: ServerStatus) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold")
    table.add_column()

    table.add_row("Host", str(status.host_number))
    table.add_row("Listening", f"{status.binding_interface}:{status.port}")
    if status.hostnames:
        table.add_row("Hostnames", ", ".join(status.hostnames))
    table.add_row(
        "Desired host",
        str(status.desired_host) if status.desired_host is not None else "-",
    )

    if status.leader is not None:
        table.add_row("Leader", _device_cell(status.leader))
    for follower in status.followers:
        table.add_row("Follower", _device_cell(follower))

    table.add_row(
        "Connected guests",
        ", ".join(status.connected_guests) if status.connected_guests else "-",
    )

    return table


def render_client_status(status: ClientStatus) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold")
    table.add_column()

    table.add_row("Host", str(status.host_number))
    table.add_row("Server", status.server)
    table.add_row(
        "Connection",
        "[green]connected[/]" if status.connected_to_server else "[red]disconnected[/]",
    )
    table.add_row(
        "Leader host",
        str(status.leader_host) if status.leader_host is not None else "-",
    )
    for follower in status.followers:
        table.add_row("Follower", _device_cell(follower))

    return table


class StatusPanel(Static):
    """Top panel: a live-updating view of whatever `ServerStatus`/
    `ClientStatus` renderable it's last been given."""
