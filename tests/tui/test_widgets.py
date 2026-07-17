from rich.table import Table

from logitech_flow_kvm.tui.widgets import ClientStatus
from logitech_flow_kvm.tui.widgets import DeviceStatus
from logitech_flow_kvm.tui.widgets import ServerStatus
from logitech_flow_kvm.tui.widgets import render_client_status
from logitech_flow_kvm.tui.widgets import render_server_status


def _column_values(table: Table, column: int) -> list[str]:
    return [str(cell) for cell in table.columns[column].cells]


class TestRenderServerStatus:
    def test_produces_a_table(self):
        status = ServerStatus(host_number=1, binding_interface="0.0.0.0", port=24801)

        renderable = render_server_status(status)

        assert isinstance(renderable, Table)

    def test_includes_leader_and_follower_rows(self):
        status = ServerStatus(
            host_number=1,
            binding_interface="0.0.0.0",
            port=24801,
            leader=DeviceStatus(id="LEADER01", label="Keyboard", connected=True),
            followers=[
                DeviceStatus(id="FOLLOW01", label="Mouse", connected=False),
            ],
        )

        renderable = render_server_status(status)

        labels = _column_values(renderable, 0)
        assert "Leader" in labels
        assert "Follower" in labels

    def test_connected_guests_default_to_a_placeholder(self):
        status = ServerStatus(host_number=1, binding_interface="0.0.0.0", port=24801)

        renderable = render_server_status(status)

        values = _column_values(renderable, 1)
        assert "-" in values

    def test_lists_connected_guests_when_present(self):
        status = ServerStatus(
            host_number=1,
            binding_interface="0.0.0.0",
            port=24801,
            connected_guests=["2", "3"],
        )

        renderable = render_server_status(status)

        values = _column_values(renderable, 1)
        assert "2, 3" in values


class TestRenderClientStatus:
    def test_produces_a_table(self):
        status = ClientStatus(host_number=2, server="myserver")

        renderable = render_client_status(status)

        assert isinstance(renderable, Table)

    def test_includes_follower_rows(self):
        status = ClientStatus(
            host_number=2,
            server="myserver",
            followers=[DeviceStatus(id="FOLLOW01", label="Mouse", connected=True)],
        )

        renderable = render_client_status(status)

        labels = _column_values(renderable, 0)
        assert "Follower" in labels
