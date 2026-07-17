import threading
import time
from unittest.mock import Mock

import platformdirs
import pytest

from hidpp_fakes import ScriptedReply
from hidpp_fakes import ScriptedTransport
from hidpp_fakes import register_matcher
from logitech_flow_kvm.commands import flow_server
from logitech_flow_kvm.commands.flow_server import FlowServerAPI
from logitech_flow_kvm.commands.flow_server import bind_routes
from logitech_flow_kvm.hidpp.models import Notification
from logitech_flow_kvm.hidpp.models import ReceiverInfo
from logitech_flow_kvm.hidpp.receiver import PairedDevice
from logitech_flow_kvm.hidpp.receiver import Receiver
from logitech_flow_kvm.reconciler import Reconciler

RECEIVER_INFO = ReceiverInfo(
    path="/dev/hidraw4", product_id=0xC548, kind="bolt", interface=2
)

ENABLE_NOTIFICATIONS_FLAGS = (0x100000 | 0x000100 | 0x000800).to_bytes(3, "big")


def _receiver_transport() -> ScriptedTransport:
    return ScriptedTransport(
        [
            ScriptedReply(
                register_matcher(0xFF, ENABLE_NOTIFICATIONS_FLAGS),
                ENABLE_NOTIFICATIONS_FLAGS,
            ),
            ScriptedReply(register_matcher(0xFF, bytes([0x02])), bytes([0x02])),
        ]
    )


def make_device(number: int, serial: str) -> PairedDevice:
    receiver = Receiver(RECEIVER_INFO, transport=_receiver_transport())
    return PairedDevice(
        receiver=receiver,
        number=number,
        wpid="0000",
        kind="mouse",
        serial=serial,
        codename=None,
    )


def connect_notification(device: PairedDevice) -> Notification:
    # link_status=0 (connected) in the top bit-field byte; see
    # util.parse_connection_status.
    return Notification(
        report_id=0x10,
        devnumber=device.number,
        sub_id=0x41,
        address=0,
        data=b"\x00\x00\x00",
    )


def disconnect_notification(device: PairedDevice) -> Notification:
    return Notification(
        report_id=0x10,
        devnumber=device.number,
        sub_id=0x41,
        address=0,
        data=b"\x40\x00\x00",
    )


class DummyListener:
    """Stands in for NotificationListener so `__init__` doesn't spawn real threads
    trying to open fake hidraw paths."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def no_background_threads(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_server, "NotificationListener", DummyListener)
    monkeypatch.setattr(Reconciler, "start", lambda self: None)
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda *a, **k: str(tmp_path))


@pytest.fixture
def leader_device():
    return make_device(1, "LEADER01")


@pytest.fixture
def follower_device():
    return make_device(1, "FOLLOW01")


@pytest.fixture
def app(leader_device, follower_device):
    api = FlowServerAPI(
        __name__,
        host_number=1,
        leader_device=leader_device,
        follower_devices=[follower_device],
        hostnames=[],
        binding_interface="0.0.0.0",
        port=24801,
    )
    bind_routes(api)
    api.config["TESTING"] = True
    return api


class TestReportLeaderHost:
    def test_updates_desired_host(self, app):
        app.report_leader_host(2)

        assert app._get_desired_host() == 2

    def test_pokes_the_reconciler(self, app):
        app.reconciler._wake.clear()

        app.report_leader_host(2)

        assert app.reconciler._wake.is_set()

    def test_desired_host_is_none_before_any_report(self, app):
        assert app._get_desired_host() is None


class TestCallback:
    def test_leader_connect_reports_leader_host(self, app, leader_device):
        app.callback(leader_device.receiver, connect_notification(leader_device))

        assert app._get_desired_host() == app.host_number

    def test_leader_disconnect_does_not_report_anything(self, app, leader_device):
        app.callback(leader_device.receiver, disconnect_notification(leader_device))

        assert app._get_desired_host() is None

    def test_follower_connect_is_observed_by_the_reconciler(self, app, follower_device):
        app.callback(follower_device.receiver, connect_notification(follower_device))

        assert app.reconciler._connected[follower_device] is True

    def test_follower_disconnect_is_observed_by_the_reconciler(
        self, app, follower_device
    ):
        app.callback(follower_device.receiver, connect_notification(follower_device))
        app.callback(follower_device.receiver, disconnect_notification(follower_device))

        assert app.reconciler._connected[follower_device] is False

    def test_ignores_notifications_for_unrelated_devices(self, app, leader_device):
        unrelated = Notification(
            report_id=0x10, devnumber=99, sub_id=0x41, address=0, data=b"\x00\x00\x00"
        )

        app.callback(leader_device.receiver, unrelated)

        assert app._get_desired_host() is None

    def test_ignores_non_connection_sub_ids(self, app, leader_device):
        other = Notification(
            report_id=0x10,
            devnumber=leader_device.number,
            sub_id=0x40,
            address=0,
            data=b"\x00\x00\x00",
        )

        app.callback(leader_device.receiver, other)

        assert app._get_desired_host() is None

    def test_leader_connect_and_disconnect_publish_status(self, app, leader_device):
        app.tui = Mock()

        app.callback(leader_device.receiver, connect_notification(leader_device))
        assert app.tui.update_status.call_count == 1

        app.callback(leader_device.receiver, disconnect_notification(leader_device))
        assert app.tui.update_status.call_count == 2

    def test_follower_connect_and_disconnect_publish_status(self, app, follower_device):
        app.tui = Mock()

        app.callback(follower_device.receiver, connect_notification(follower_device))
        assert app.tui.update_status.call_count == 1

        app.callback(follower_device.receiver, disconnect_notification(follower_device))
        assert app.tui.update_status.call_count == 2

    def test_does_not_publish_status_without_a_tui(self, app, follower_device):
        assert app.tui is None

        # Would raise if it tried to call `update_status` on `None`.
        app.callback(follower_device.receiver, connect_notification(follower_device))


class TestBuildStatus:
    def test_reflects_leader_and_follower_connection_state(
        self, app, leader_device, follower_device
    ):
        app.callback(leader_device.receiver, connect_notification(leader_device))
        app.callback(follower_device.receiver, connect_notification(follower_device))

        status = app._build_status()

        assert status.leader is not None
        assert status.leader.id == leader_device.id
        assert status.leader.connected is True
        assert len(status.followers) == 1
        assert status.followers[0].id == follower_device.id
        assert status.followers[0].connected is True

    def test_defaults_to_disconnected_before_any_notification(self, app, leader_device):
        status = app._build_status()

        assert status.leader is not None
        assert status.leader.connected is False
        assert status.followers[0].connected is False

    def test_includes_static_configuration(self, app):
        status = app._build_status()

        assert status.host_number == app.host_number
        assert status.binding_interface == app.binding_interface
        assert status.port == app.port

    def test_desired_host_reflects_reported_leader_host(self, app):
        app.report_leader_host(3)

        assert app._build_status().desired_host == 3


class TestStartBackgroundThreads:
    def test_starts_the_reconciler_and_every_listener(self, app):
        app.reconciler.start = Mock()
        app.listeners = [Mock(), Mock()]

        app.start_background_threads()

        app.reconciler.start.assert_called_once()
        for listener in app.listeners:
            listener.start.assert_called_once()


def _auth_headers(app, name: str) -> dict[str, str]:
    token = app.create_new_auth_token(name)
    return {"Authorization": f"Bearer {token}"}


class TestConfigurationRoute:
    def test_returns_leader_and_follower_ids(self, app, leader_device, follower_device):
        client = app.test_client()

        response = client.get("/configuration", headers=_auth_headers(app, "1"))

        assert response.status_code == 200
        assert response.json == {
            "leader": leader_device.id,
            "followers": [follower_device.id],
        }

    def test_requires_authentication(self, app):
        client = app.test_client()

        response = client.get("/configuration")

        assert response.status_code == 401


class TestLeaderHostRoute:
    def test_put_updates_desired_host(self, app):
        client = app.test_client()

        response = client.put(
            "/leader-host", data=b"2", headers=_auth_headers(app, "2")
        )

        assert response.status_code == 200
        assert app._get_desired_host() == 2


class TestEventsRoute:
    def test_new_subscriber_immediately_receives_the_current_state(self, app):
        app.report_leader_host(2)
        client = app.test_client()

        response = client.get("/events", headers=_auth_headers(app, "A"))
        first_chunk = next(response.response)

        assert first_chunk == b"event: leader-host\ndata: 2\n\n"
        response.response.close()

    def test_a_second_subscriber_triggers_a_host_connected_broadcast(self, app):
        app.report_leader_host(2)
        client = app.test_client()

        response_a = client.get("/events", headers=_auth_headers(app, "A"))
        next(response_a.response)  # consume A's initial snapshot

        response_b = client.get("/events", headers=_auth_headers(app, "B"))

        second_chunk_for_a = next(response_a.response)
        assert second_chunk_for_a == b"event: host-connected\ndata: B\n\n"

        response_a.response.close()
        response_b.response.close()

    def test_subsequent_leader_host_changes_are_broadcast(self, app):
        app.report_leader_host(2)
        client = app.test_client()

        response = client.get("/events", headers=_auth_headers(app, "A"))
        next(response.response)  # consume the initial snapshot

        app.report_leader_host(3)

        assert next(response.response) == b"event: leader-host\ndata: 3\n\n"
        response.response.close()

    def test_closing_the_stream_unsubscribes_it(self, app):
        app.report_leader_host(2)
        client = app.test_client()

        response = client.get("/events", headers=_auth_headers(app, "A"))
        # Advance the generator past its first (non-blocking, since state is
        # already set) yield so it's actually suspended inside the try/finally
        # before closing it -- closing a never-started generator wouldn't run
        # the `finally: app.events.unsubscribe(...)` cleanup at all.
        next(response.response)
        response.response.close()


class TestConnectedGuests:
    # Every test here sets the leader host before opening /events, exactly
    # like TestEventsRoute above -- with no state set yet, the stream's
    # first chunk blocks on the 15s keepalive wait, and Flask's test client
    # drives at least one chunk of the generator during `client.get()`
    # itself (not just on an explicit `next()`), so skipping this makes
    # every test in this class slow instead of just wrong.

    def test_subscribing_adds_the_authenticated_host_to_the_roster(self, app):
        app.report_leader_host(2)
        client = app.test_client()

        response = client.get("/events", headers=_auth_headers(app, "2"))

        assert app.events.subscriber_names == ["2"]
        response.response.close()

    def test_unsubscribing_removes_the_host_from_the_roster(self, app):
        app.report_leader_host(2)
        client = app.test_client()
        response = client.get("/events", headers=_auth_headers(app, "2"))
        next(response.response)  # advance past the initial snapshot

        response.response.close()

        assert app.events.subscriber_names == []

    def test_subscribing_and_unsubscribing_publish_status(self, app):
        app.report_leader_host(2)
        app.tui = Mock()
        client = app.test_client()

        response = client.get("/events", headers=_auth_headers(app, "2"))
        assert app.tui.update_status.call_count == 1
        next(response.response)  # advance past the initial snapshot

        response.response.close()

        assert app.tui.update_status.call_count == 2

    def test_build_status_reflects_connected_guests(self, app):
        app.report_leader_host(2)
        client = app.test_client()

        response = client.get("/events", headers=_auth_headers(app, "2"))

        assert app._build_status().connected_guests == ["2"]
        response.response.close()


class TestPairingRoute:
    def test_concurrent_pairing_requests_are_serialized(self, app, monkeypatch):
        call_count = 0
        release_first = threading.Event()

        def fake_ask(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Hold the lock until the test says the first "operator" has
                # finished typing in the pairing code.
                release_first.wait(timeout=2)
            return "000000"

        monkeypatch.setattr(flow_server.Prompt, "ask", fake_ask)

        def do_pairing(name):
            with app.test_request_context(
                "/pairing",
                method="POST",
                json={"pairing_code": "000000", "name": name},
            ):
                app.view_functions["pair"]()

        first = threading.Thread(target=do_pairing, args=("host-a",))
        first.start()
        while call_count < 1:
            time.sleep(0.01)

        second = threading.Thread(target=do_pairing, args=("host-b",))
        second.start()

        # The second request should be blocked waiting on the lock -- not
        # yet inside its own Prompt.ask() -- for as long as we withhold it.
        time.sleep(0.1)
        assert call_count == 1

        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert call_count == 2
