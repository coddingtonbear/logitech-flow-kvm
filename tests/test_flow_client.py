import argparse
import queue
import threading
import time
import types
from typing import Any
from unittest.mock import Mock

import platformdirs
import pytest
import requests

from logitech_flow_kvm import exceptions
from logitech_flow_kvm.commands import flow_client
from logitech_flow_kvm.commands.flow_client import FlowClient
from logitech_flow_kvm.hidpp.models import Notification
from logitech_flow_kvm.util import set_host_certificate_and_token


class FakeResponse:
    """Stands in for `requests.Response` across the handful of attributes/methods
    flow_client actually touches."""

    def __init__(
        self,
        *,
        ok: bool = True,
        status_code: int | None = None,
        json_data=None,
        text: str = "",
        lines: list[str] | None = None,
    ):
        self.ok = ok
        self.status_code = (
            status_code if status_code is not None else (200 if ok else 400)
        )
        self._json = json_data
        self.text = text
        self._lines = lines or []

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError("request failed")

    def iter_lines(self, decode_unicode: bool = True):
        return iter(self._lines)


@pytest.fixture
def user_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        platformdirs, "user_data_dir", lambda *args, **kwargs: str(tmp_path)
    )
    return tmp_path


def make_client(**attrs) -> FlowClient:
    options = argparse.Namespace(host_number=2, server="myserver", port=24801)
    client = FlowClient(options=options)
    for key, value in attrs.items():
        setattr(client, key, value)
    return client


def connection_notification(devnumber: int, *, connected: bool) -> Notification:
    data = b"\x00\x00\x00" if connected else b"\x40\x00\x00"
    return Notification(
        report_id=0x10, devnumber=devnumber, sub_id=0x41, address=0, data=data
    )


def run_queued_http_tasks(client: FlowClient) -> None:
    """Synchronously run whatever `callback` queued for the HTTP worker."""
    while True:
        try:
            task = client._http_tasks.get_nowait()
        except queue.Empty:
            return
        task()


class TestBuildUrl:
    def test_joins_server_port_and_segments(self):
        client = make_client()

        assert client.build_url("a", "b") == "https://myserver:24801/a/b"

    def test_with_no_segments(self):
        client = make_client()

        assert client.build_url() == "https://myserver:24801/"


class TestRequest:
    def test_defaults_verify_to_cert(self, monkeypatch):
        client = make_client(cert="/path/to/cert")
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x")

        assert captured["kwargs"]["verify"] == "/path/to/cert"

    def test_does_not_override_an_explicit_verify(self, monkeypatch):
        client = make_client(cert="/path/to/cert")
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x", verify=False)

        assert captured["kwargs"]["verify"] is False

    def test_adds_bearer_token_when_missing(self, monkeypatch):
        client = make_client(token="tok123")
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x")

        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer tok123"

    def test_does_not_override_an_explicit_authorization_header(self, monkeypatch):
        client = make_client(token="tok123")
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x", headers={"Authorization": "Bearer other"})

        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer other"

    def test_no_authorization_header_without_a_token(self, monkeypatch):
        client = make_client(token=None)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x")

        assert "Authorization" not in captured["kwargs"]["headers"]

    def test_applies_a_default_timeout(self, monkeypatch):
        client = make_client()
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x")

        assert captured["kwargs"]["timeout"] == flow_client.DEFAULT_HTTP_TIMEOUT

    def test_does_not_override_an_explicit_timeout(self, monkeypatch):
        client = make_client()
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda method, url, **kw: captured.update(kwargs=kw) or FakeResponse(),
        )

        client.request("GET", "https://x", timeout=(1, 2))

        assert captured["kwargs"]["timeout"] == (1, 2)


class TestPair:
    def test_success_stores_certificate_and_token(self, monkeypatch):
        client = make_client()
        responses = iter(
            [
                FakeResponse(ok=True),  # OPTIONS /pairing
                FakeResponse(
                    ok=True, json_data={"certificate": "PEM-DATA", "token": "abc123"}
                ),  # POST /pairing
            ]
        )
        monkeypatch.setattr(
            flow_client.requests, "request", lambda *a, **k: next(responses)
        )
        stored: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client,
            "set_host_certificate_and_token",
            lambda name, cert, token: stored.update(name=name, cert=cert, token=token),
        )

        token = client.pair()

        assert token == "abc123"
        assert stored == {"name": "myserver", "cert": "PEM-DATA", "token": "abc123"}

    def test_options_failure_raises_server_not_available(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(
            flow_client.requests, "request", lambda *a, **k: FakeResponse(ok=False)
        )

        with pytest.raises(exceptions.ServerNotAvailable):
            client.pair()

    def test_post_failure_raises_pairing_failed(self, monkeypatch):
        client = make_client()
        responses = iter([FakeResponse(ok=True), FakeResponse(ok=False)])
        monkeypatch.setattr(
            flow_client.requests, "request", lambda *a, **k: next(responses)
        )

        with pytest.raises(exceptions.PairingFailed):
            client.pair()

    def test_post_waits_long_enough_for_a_human_to_type_the_code(self, monkeypatch):
        client = make_client()
        responses = iter(
            [
                FakeResponse(ok=True),  # OPTIONS /pairing
                FakeResponse(
                    ok=True, json_data={"certificate": "PEM-DATA", "token": "abc123"}
                ),  # POST /pairing
            ]
        )
        captured: list[dict[str, Any]] = []

        def fake_request(method, url, **kw):
            captured.append(kw)
            return next(responses)

        monkeypatch.setattr(flow_client.requests, "request", fake_request)
        monkeypatch.setattr(
            flow_client, "set_host_certificate_and_token", lambda *a: None
        )

        client.pair()

        post_kwargs = captured[1]
        _, read_timeout = post_kwargs["timeout"]
        assert read_timeout == flow_client.PAIRING_READ_TIMEOUT


class TestGetCertificatePathAndToken:
    def test_pairs_when_no_certificate_is_cached(self, user_data_dir, monkeypatch):
        client = make_client()
        pair_mock = Mock(return_value="newtok")
        monkeypatch.setattr(client, "pair", pair_mock)

        _, token = client.get_certificate_path_and_token()

        pair_mock.assert_called_once()
        assert token == "newtok"

    def test_reuses_the_cached_certificate_when_configuration_succeeds(
        self, user_data_dir, monkeypatch
    ):
        client = make_client()
        set_host_certificate_and_token("myserver", "PEM-DATA", "cachedtok")
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda *a, **k: FakeResponse(ok=True, status_code=200),
        )
        monkeypatch.setattr(
            client, "pair", Mock(side_effect=AssertionError("should not re-pair"))
        )

        _, token = client.get_certificate_path_and_token()

        assert token == "cachedtok"

    def test_repairs_when_the_cached_token_is_rejected(
        self, user_data_dir, monkeypatch
    ):
        client = make_client()
        set_host_certificate_and_token("myserver", "PEM-DATA", "staletok")
        monkeypatch.setattr(
            flow_client.requests,
            "request",
            lambda *a, **k: FakeResponse(ok=False, status_code=401),
        )
        pair_mock = Mock(return_value="freshtok")
        monkeypatch.setattr(client, "pair", pair_mock)

        _, token = client.get_certificate_path_and_token()

        pair_mock.assert_called_once()
        assert token == "freshtok"

    def test_repairs_when_the_cached_certificate_fails_tls_verification(
        self, user_data_dir, monkeypatch
    ):
        client = make_client()
        set_host_certificate_and_token("myserver", "PEM-DATA", "staletok")

        def raise_ssl_error(*args, **kwargs):
            raise requests.exceptions.SSLError("unknown certificate")

        monkeypatch.setattr(flow_client.requests, "request", raise_ssl_error)
        pair_mock = Mock(return_value="freshtok")
        monkeypatch.setattr(client, "pair", pair_mock)

        _, token = client.get_certificate_path_and_token()

        pair_mock.assert_called_once()
        assert token == "freshtok"

    def test_raises_server_not_available_on_other_connection_errors(
        self, user_data_dir, monkeypatch
    ):
        client = make_client()
        set_host_certificate_and_token("myserver", "PEM-DATA", "staletok")

        def raise_connection_error(*args, **kwargs):
            raise requests.exceptions.ConnectionError("host unreachable")

        monkeypatch.setattr(flow_client.requests, "request", raise_connection_error)
        monkeypatch.setattr(
            client, "pair", Mock(side_effect=AssertionError("should not re-pair"))
        )

        with pytest.raises(exceptions.ServerNotAvailable):
            client.get_certificate_path_and_token()


class TestCallback:
    def test_leader_connect_reports_leader_host_and_pulls_the_clipboard(
        self, monkeypatch
    ):
        reconciler = Mock()
        client = make_client(leader_id="LEADER01", reconciler=reconciler)
        receiver = Mock()
        receiver.get_device.return_value = types.SimpleNamespace(id="LEADER01")
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url))
            if method == "PUT":
                return FakeResponse(ok=True)
            return FakeResponse(ok=True, text="clip-from-server")

        monkeypatch.setattr(client, "request", fake_request)
        copied: dict[str, Any] = {}
        monkeypatch.setattr(
            flow_client.pyperclip, "copy", lambda text: copied.update(text=text)
        )

        client.callback(receiver, connection_notification(1, connected=True))
        # The callback itself makes no requests -- they're queued for the
        # HTTP worker so the notification listener never blocks on network.
        assert calls == []

        run_queued_http_tasks(client)

        assert ("PUT", client.build_url("leader-host")) in calls
        assert ("GET", client.build_url("clipboard")) in calls
        assert copied["text"] == "clip-from-server"
        reconciler.observe.assert_not_called()

    def test_leader_disconnect_pushes_the_local_clipboard(self, monkeypatch):
        reconciler = Mock()
        client = make_client(leader_id="LEADER01", reconciler=reconciler)
        receiver = Mock()
        receiver.get_device.return_value = types.SimpleNamespace(id="LEADER01")
        monkeypatch.setattr(flow_client.pyperclip, "paste", lambda: "local-clip")
        sent: dict[str, Any] = {}

        def fake_request(method, url, **kwargs):
            sent.update(method=method, url=url, data=kwargs.get("data"))
            return FakeResponse(ok=True)

        monkeypatch.setattr(client, "request", fake_request)

        client.callback(receiver, connection_notification(1, connected=False))
        run_queued_http_tasks(client)

        assert sent["method"] == "PUT"
        assert sent["url"] == client.build_url("clipboard")
        assert sent["data"] == b"local-clip"

    def test_leader_connect_skips_the_clipboard_when_disabled(self, monkeypatch):
        reconciler = Mock()
        client = make_client(
            leader_id="LEADER01", reconciler=reconciler, clipboard_enabled=False
        )
        receiver = Mock()
        receiver.get_device.return_value = types.SimpleNamespace(id="LEADER01")
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url))
            return FakeResponse(ok=True)

        monkeypatch.setattr(client, "request", fake_request)
        copy_mock = Mock(side_effect=AssertionError("should not be called"))
        monkeypatch.setattr(flow_client.pyperclip, "copy", copy_mock)

        client.callback(receiver, connection_notification(1, connected=True))
        run_queued_http_tasks(client)

        assert ("PUT", client.build_url("leader-host")) in calls
        assert ("GET", client.build_url("clipboard")) not in calls
        copy_mock.assert_not_called()

    def test_leader_disconnect_skips_the_clipboard_when_disabled(self, monkeypatch):
        reconciler = Mock()
        client = make_client(
            leader_id="LEADER01", reconciler=reconciler, clipboard_enabled=False
        )
        receiver = Mock()
        receiver.get_device.return_value = types.SimpleNamespace(id="LEADER01")
        paste_mock = Mock(side_effect=AssertionError("should not be called"))
        monkeypatch.setattr(flow_client.pyperclip, "paste", paste_mock)
        request_mock = Mock(side_effect=AssertionError("should not be called"))
        monkeypatch.setattr(client, "request", request_mock)

        client.callback(receiver, connection_notification(1, connected=False))
        run_queued_http_tasks(client)

        paste_mock.assert_not_called()
        request_mock.assert_not_called()

    def test_follower_connect_is_observed_by_the_reconciler(self, monkeypatch):
        reconciler = Mock()
        client = make_client(leader_id="LEADER01", reconciler=reconciler)
        receiver = Mock()
        device = types.SimpleNamespace(id="FOLLOW01")
        receiver.get_device.return_value = device
        monkeypatch.setattr(
            client, "request", lambda *a, **k: FakeResponse(ok=True, text="")
        )
        monkeypatch.setattr(flow_client.pyperclip, "copy", lambda text: None)

        client.callback(receiver, connection_notification(2, connected=True))
        run_queued_http_tasks(client)

        reconciler.observe.assert_called_once_with(device, True)

    def test_follower_disconnect_is_observed_by_the_reconciler(self):
        reconciler = Mock()
        client = make_client(leader_id="LEADER01", reconciler=reconciler)
        receiver = Mock()
        device = types.SimpleNamespace(id="FOLLOW01")
        receiver.get_device.return_value = device

        client.callback(receiver, connection_notification(2, connected=False))

        reconciler.observe.assert_called_once_with(device, False)

    def test_ignores_non_connection_sub_ids(self):
        client = make_client(leader_id="LEADER01", reconciler=Mock())
        receiver = Mock()
        notification = Notification(
            report_id=0x10, devnumber=1, sub_id=0x40, address=0, data=b"\x00\x00\x00"
        )

        client.callback(receiver, notification)

        receiver.get_device.assert_not_called()

    def test_ignores_unknown_devices(self):
        reconciler = Mock()
        client = make_client(leader_id="LEADER01", reconciler=reconciler)
        receiver = Mock()
        receiver.get_device.return_value = None

        client.callback(receiver, connection_notification(9, connected=True))

        reconciler.observe.assert_not_called()


class TestRunHttpTasks:
    def test_a_failing_task_does_not_stop_later_tasks(self):
        client = make_client()
        client._stop = threading.Event()
        ran = []

        def failing():
            ran.append("failing")
            raise RuntimeError("network blip")

        def succeeding():
            ran.append("succeeding")
            client._stop.set()

        client._http_tasks.put(failing)
        client._http_tasks.put(succeeding)

        client._run_http_tasks()

        assert ran == ["failing", "succeeding"]

    def test_exits_promptly_once_stopped(self):
        client = make_client()
        client._stop = threading.Event()
        client._stop.set()

        client._run_http_tasks()  # must return without blocking


class TestHandleEvent:
    def test_leader_host_updates_state_and_pokes_the_reconciler(self):
        reconciler = Mock()
        client = make_client(reconciler=reconciler)

        client._handle_event("leader-host", "3")

        assert client.leader_host == 3
        reconciler.poke.assert_called_once()

    def test_host_connected_only_prints(self):
        reconciler = Mock()
        client = make_client(reconciler=reconciler)

        client._handle_event("host-connected", "2")

        assert client.leader_host is None
        reconciler.poke.assert_not_called()


class TestConsumeEvents:
    """Covers the SSE reconnect loop: the part of flow_client that can't be
    exercised by hitting real endpoints, since it's specifically about
    recovering when the connection *isn't* there."""

    def test_processes_events_and_renotifies_local_receivers(self, monkeypatch):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        fake_receiver = Mock()
        client.local_receivers = [fake_receiver]
        stream = FakeResponse(ok=True, lines=["event: leader-host", "data: 5", ""])
        monkeypatch.setattr(client, "request", Mock(return_value=stream))
        monkeypatch.setattr(
            client, "_sleep_between_retries", lambda s: client._stop.set()
        )

        client._consume_events()

        assert client.leader_host == 5
        fake_receiver.notify_devices.assert_called_once()

    def test_stream_uses_a_read_timeout_that_outlives_keepalives(self, monkeypatch):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        client.local_receivers = []
        stream = FakeResponse(ok=True, lines=[])
        request_mock = Mock(return_value=stream)
        monkeypatch.setattr(client, "request", request_mock)
        monkeypatch.setattr(
            client, "_sleep_between_retries", lambda s: client._stop.set()
        )

        client._consume_events()

        _, read_timeout = request_mock.call_args.kwargs["timeout"]
        assert read_timeout == flow_client.EVENTS_READ_TIMEOUT
        # A dead connection must be detected, not waited on forever: the
        # server keepalives every 15s, so a healthy stream never idles
        # this long.
        assert read_timeout is not None

    def test_retries_with_growing_backoff_while_the_connection_stays_down(
        self, monkeypatch
    ):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        client.local_receivers = []
        monkeypatch.setattr(
            client, "request", Mock(side_effect=requests.exceptions.ConnectionError())
        )
        sleeps: list[float] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                client._stop.set()

        monkeypatch.setattr(client, "_sleep_between_retries", fake_sleep)

        client._consume_events()

        assert sleeps == [
            flow_client.EVENTS_MIN_BACKOFF,
            flow_client.EVENTS_MIN_BACKOFF * 2,
            flow_client.EVENTS_MIN_BACKOFF * 4,
        ]

    def test_backoff_caps_at_the_configured_maximum(self, monkeypatch):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        client.local_receivers = []
        monkeypatch.setattr(
            client, "request", Mock(side_effect=requests.exceptions.ConnectionError())
        )
        sleeps: list[float] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 8:
                client._stop.set()

        monkeypatch.setattr(client, "_sleep_between_retries", fake_sleep)

        client._consume_events()

        assert max(sleeps) == flow_client.EVENTS_MAX_BACKOFF
        assert sleeps[-1] == flow_client.EVENTS_MAX_BACKOFF

    def test_backoff_resets_after_a_successful_reconnect(self, monkeypatch):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        client.local_receivers = []
        good_stream = FakeResponse(ok=True, lines=[])
        outcomes = iter(
            [requests.exceptions.ConnectionError(), good_stream, good_stream]
        )

        def fake_request(*args, **kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(client, "request", fake_request)
        sleeps: list[float] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                client._stop.set()

        monkeypatch.setattr(client, "_sleep_between_retries", fake_sleep)

        client._consume_events()

        # Had the failed first attempt's backoff carried over, the second
        # sleep would be EVENTS_MIN_BACKOFF * 2 instead.
        assert sleeps == [
            flow_client.EVENTS_MIN_BACKOFF,
            flow_client.EVENTS_MIN_BACKOFF,
        ]

    def test_stops_immediately_without_making_a_request_once_stop_is_set(
        self, monkeypatch
    ):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        client._stop.set()
        request_mock = Mock(side_effect=AssertionError("should not be called"))
        monkeypatch.setattr(client, "request", request_mock)

        client._consume_events()

        request_mock.assert_not_called()

    def test_connected_to_server_is_true_only_while_the_stream_is_up(self, monkeypatch):
        client = make_client(reconciler=Mock())
        client._stop = threading.Event()
        client.local_receivers = []
        observed: list[bool] = []
        monkeypatch.setattr(
            client,
            "_publish_status",
            lambda: observed.append(client._connected_to_server),
        )
        stream = FakeResponse(ok=True, lines=[])
        monkeypatch.setattr(client, "request", Mock(return_value=stream))
        monkeypatch.setattr(
            client, "_sleep_between_retries", lambda s: client._stop.set()
        )

        client._consume_events()

        assert observed == [True, False]


class TestBuildStatus:
    def test_reflects_leader_host_and_follower_connection_state(self):
        # A hashable stand-in -- `_connected` is keyed by device, and
        # `SimpleNamespace` (unlike `Mock`) isn't hashable.
        follower = Mock(id="FOLLOW01", codename="Mouse", kind="mouse")
        reconciler = Mock()
        reconciler._connected = {follower: True}
        client = make_client(
            reconciler=reconciler,
            leader_host=3,
            follower_devices=[follower],
            _connected_to_server=True,
        )

        status = client._build_status()

        assert status.host_number == 2
        assert status.server == "myserver"
        assert status.connected_to_server is True
        assert status.leader_host == 3
        assert len(status.followers) == 1
        assert status.followers[0].id == "FOLLOW01"
        assert status.followers[0].connected is True

    def test_defaults_follower_connection_to_false_when_unobserved(self):
        follower = Mock(id="FOLLOW01", codename=None, kind="mouse")
        reconciler = Mock()
        reconciler._connected = {}
        client = make_client(reconciler=reconciler, follower_devices=[follower])

        status = client._build_status()

        assert status.followers[0].connected is False


class TestPublishStatus:
    def test_does_nothing_without_a_tui(self):
        client = make_client(reconciler=Mock(), follower_devices=[])

        # Would raise if it tried to build/render a status without a tui.
        client._publish_status()

    def test_updates_the_tui_when_present(self):
        tui = Mock()
        client = make_client(reconciler=Mock(), follower_devices=[], tui=tui)

        client._publish_status()

        tui.update_status.assert_called_once()


class TestStartBackgroundThreads:
    def test_starts_the_reconciler_manager_and_events_consumer(self, monkeypatch):
        reconciler = Mock()
        manager = Mock()
        client = make_client(reconciler=reconciler, manager=manager)
        thread_targets: list[object] = []

        class FakeThread:
            def __init__(self, target=None, daemon=None):
                thread_targets.append(target)

            def start(self):
                pass

        monkeypatch.setattr(flow_client.threading, "Thread", FakeThread)

        client.start_background_threads()

        reconciler.start.assert_called_once()
        manager.start.assert_called_once()
        assert thread_targets == [client._run_http_tasks, client._consume_events]


class TestBindReceivers:
    def test_swaps_in_rediscovered_devices(self):
        reconciler = Mock()
        client = make_client(
            reconciler=reconciler,
            follower_ids=["FOLLOW01"],
            follower_devices=[],
            local_receivers=[],
        )
        new_follower = Mock(serial="FOLLOW01", id="FOLLOW01")
        receiver = Mock()
        receiver.enumerate_devices.return_value = [new_follower]

        client._bind_receivers([receiver])

        assert client.follower_devices == [new_follower]
        assert client.local_receivers == [receiver]
        reconciler.set_devices.assert_called_once_with([new_follower])

    def test_raises_while_a_follower_is_still_missing(self):
        client = make_client(
            reconciler=Mock(),
            follower_ids=["FOLLOW01"],
            follower_devices=[],
            local_receivers=[],
        )
        receiver = Mock()
        receiver.enumerate_devices.return_value = []

        with pytest.raises(exceptions.DeviceNotFound):
            client._bind_receivers([receiver])


class TestCallbackFollowerMatching:
    """Regression coverage for the stuck-forever reconciler: follower
    notifications used to be resolved by re-reading the pairing registers
    (`receiver.get_device`), so a read that failed or came back with a
    differing codename/serial produced a `PairedDevice` that wasn't the key
    `Reconciler` stores connection state under -- and the observation was
    silently dropped. Losing a disconnect that way left the reconciler
    driving a device that had already left, forever."""

    def test_a_follower_is_matched_without_re_reading_the_device(self):
        reconciler = Mock()
        receiver = Mock()
        follower = Mock(id="FOLLOW01", receiver=receiver, number=2)
        client = make_client(
            leader_id="LEADER01", reconciler=reconciler, follower_devices=[follower]
        )
        receiver.get_device = Mock(side_effect=AssertionError("should not be re-read"))

        client.callback(receiver, connection_notification(2, connected=False))

        reconciler.observe.assert_called_once_with(follower, False)
        receiver.get_device.assert_not_called()

    def test_a_follower_is_observed_even_when_the_registers_stop_answering(self):
        # `get_device` returning None used to drop the notification entirely.
        reconciler = Mock()
        receiver = Mock()
        receiver.get_device.return_value = None
        follower = Mock(id="FOLLOW01", receiver=receiver, number=2)
        client = make_client(
            leader_id="LEADER01", reconciler=reconciler, follower_devices=[follower]
        )

        client.callback(receiver, connection_notification(2, connected=False))

        reconciler.observe.assert_called_once_with(follower, False)

    def test_the_exact_object_the_reconciler_keys_on_is_observed(self):
        # The follower the reconciler was constructed with, not a value-equal
        # rebuild of it -- that distinction is the whole bug.
        reconciler = Mock()
        receiver = Mock()
        follower = Mock(id="FOLLOW01", receiver=receiver, number=2)
        receiver.get_device.return_value = Mock(id="FOLLOW01")
        client = make_client(
            leader_id="LEADER01", reconciler=reconciler, follower_devices=[follower]
        )

        client.callback(receiver, connection_notification(2, connected=True))

        observed_device, _ = reconciler.observe.call_args.args
        assert observed_device is follower

    def test_a_follower_on_another_receiver_is_not_matched(self):
        reconciler = Mock()
        receiver = Mock()
        other_receiver = Mock()
        follower = Mock(id="FOLLOW01", receiver=other_receiver, number=2)
        rebuilt = Mock(id="OTHER01")
        receiver.get_device.return_value = rebuilt
        client = make_client(
            leader_id="LEADER01", reconciler=reconciler, follower_devices=[follower]
        )

        client.callback(receiver, connection_notification(2, connected=True))

        reconciler.observe.assert_called_once_with(rebuilt, True)

    def test_the_leader_is_still_identified_by_a_live_lookup(self):
        # The client never resolves the leader up front, so that path stays.
        reconciler = Mock()
        receiver = Mock()
        receiver.get_device.return_value = types.SimpleNamespace(id="LEADER01")
        client = make_client(
            leader_id="LEADER01",
            reconciler=reconciler,
            clipboard_enabled=False,
            follower_devices=[Mock(id="FOLLOW01", receiver=receiver, number=2)],
        )

        client.callback(receiver, connection_notification(1, connected=True))
        queued = []
        while True:
            try:
                queued.append(client._http_tasks.get_nowait())
            except queue.Empty:
                break

        assert queued == [client._report_leader_host_here]
        reconciler.observe.assert_not_called()

    def test_follower_devices_default_to_empty_before_resolution(self):
        # `callback` can fire before `handle()` has resolved anything.
        client = make_client()

        assert client.follower_devices == []


class TestGetDesiredHost:
    """`leader_host` is a belief with no expiry, formed from one connect
    notification relayed by the server. Acting on it once the server is
    unreachable means repeatedly shoving the user's devices onto a machine
    that may be unplugged -- and since a receiver can only push a device
    away, never pull one back, only the user can undo that, by hand, every
    time."""

    def test_acts_on_the_leader_host_while_the_server_is_reachable(self):
        client = make_client(leader_host=1, _connected_to_server=True)

        assert client._get_desired_host() == 1

    def test_holds_once_the_server_has_been_unreachable_past_the_grace_period(
        self, monkeypatch
    ):
        client = make_client(leader_host=1, _connected_to_server=False)
        client._disconnected_since = 100.0
        monkeypatch.setattr(
            flow_client.time,
            "monotonic",
            lambda: 100.0 + flow_client.SERVER_GRACE_PERIOD + 1,
        )

        assert client._get_desired_host() is None

    def test_keeps_acting_during_a_brief_outage(self, monkeypatch):
        # A wifi blip or a server restart is common and self-healing;
        # freezing instantly would stall a switch the user just asked for.
        client = make_client(leader_host=1, _connected_to_server=False)
        client._disconnected_since = 100.0
        monkeypatch.setattr(
            flow_client.time,
            "monotonic",
            lambda: 100.0 + flow_client.SERVER_GRACE_PERIOD - 1,
        )

        assert client._get_desired_host() == 1

    def test_holds_before_the_first_connection_ever_succeeds(self):
        client = make_client(leader_host=1, _connected_to_server=False)

        assert client._disconnected_since is None
        assert client._get_desired_host() is None

    def test_nothing_to_do_when_no_leader_host_is_known(self):
        client = make_client(leader_host=None, _connected_to_server=True)

        assert client._get_desired_host() is None

    def test_the_leader_being_here_outranks_a_stale_server_belief(self):
        # Direct physical evidence beats hearsay: on reconnect the server
        # may hand us its pre-outage snapshot before our own re-announcement
        # has corrected it, and acting on that would fling the followers at
        # the host the leader just left.
        client = make_client(leader_host=1, _connected_to_server=True)
        client._leader_here = True

        assert client._get_desired_host() == client.options.host_number

    def test_the_leader_being_here_wins_even_while_holding(self):
        client = make_client(leader_host=1, _connected_to_server=False)
        client._leader_here = True

        assert client._get_desired_host() == client.options.host_number

    def test_resumes_once_the_server_comes_back(self, monkeypatch):
        client = make_client(leader_host=1, _connected_to_server=False)
        client._disconnected_since = 100.0
        monkeypatch.setattr(
            flow_client.time,
            "monotonic",
            lambda: 100.0 + flow_client.SERVER_GRACE_PERIOD + 1,
        )
        assert client._get_desired_host() is None

        client._connected_to_server = True

        assert client._get_desired_host() == 1

    def test_holding_is_announced_once_rather_than_every_tick(
        self, monkeypatch, caplog
    ):
        client = make_client(leader_host=1, _connected_to_server=False)
        client._disconnected_since = 100.0
        monkeypatch.setattr(
            flow_client.time,
            "monotonic",
            lambda: 100.0 + flow_client.SERVER_GRACE_PERIOD + 1,
        )

        with caplog.at_level("WARNING"):
            for _ in range(5):
                client._get_desired_host()

        assert len(caplog.records) == 1

    def test_a_held_host_shows_in_the_status(self, monkeypatch):
        client = make_client(leader_host=1, _connected_to_server=False)
        client.follower_devices = []
        client._disconnected_since = 100.0
        monkeypatch.setattr(
            flow_client.time,
            "monotonic",
            lambda: 100.0 + flow_client.SERVER_GRACE_PERIOD + 1,
        )

        assert client._build_status().holding is True

    def test_our_own_host_number_is_never_reported_as_held(self, monkeypatch):
        # Nothing is being withheld: the followers already belong here.
        client = make_client(
            leader_host=2, _connected_to_server=False, follower_devices=[]
        )
        client._disconnected_since = 100.0
        monkeypatch.setattr(
            flow_client.time,
            "monotonic",
            lambda: 100.0 + flow_client.SERVER_GRACE_PERIOD + 1,
        )

        assert client.options.host_number == 2
        assert client._build_status().holding is False


class TestLeaderHereTracking:
    def test_a_leader_connect_records_that_the_leader_is_here(self):
        client = make_client(reconciler=Mock(), leader_id="LEAD", follower_devices=[])
        client.clipboard_enabled = False
        device = types.SimpleNamespace(id="LEAD", receiver=None, number=1)
        receiver = Mock()
        receiver.get_device.return_value = device

        client.callback(receiver, connection_notification(1, connected=True))

        assert client._leader_here is True

    def test_a_leader_disconnect_records_that_it_has_gone(self):
        client = make_client(reconciler=Mock(), leader_id="LEAD", follower_devices=[])
        client.clipboard_enabled = False
        device = types.SimpleNamespace(id="LEAD", receiver=None, number=1)
        receiver = Mock()
        receiver.get_device.return_value = device

        client.callback(receiver, connection_notification(1, connected=True))
        client.callback(receiver, connection_notification(1, connected=False))

        assert client._leader_here is False

    def test_a_leader_notification_wakes_the_reconciler(self):
        # The desired host changes as a result, so waiting out the next
        # coarse tick would leave devices pointed at the old answer.
        reconciler = Mock()
        client = make_client(
            reconciler=reconciler, leader_id="LEAD", follower_devices=[]
        )
        client.clipboard_enabled = False
        device = types.SimpleNamespace(id="LEAD", receiver=None, number=1)
        receiver = Mock()
        receiver.get_device.return_value = device

        client.callback(receiver, connection_notification(1, connected=True))

        reconciler.poke.assert_called_once()


class TestHandleEventClearsLeaderHost:
    def test_leader_host_unknown_means_the_server_no_longer_knows(self):
        client = make_client(reconciler=Mock(), leader_host=1)

        client._handle_event("leader-host-unknown", "1")

        assert client.leader_host is None

    def test_an_unrecognised_event_is_ignored(self):
        # The contract that makes mixed versions safe in both directions:
        # an event type you don't know changes nothing.
        reconciler = Mock()
        client = make_client(reconciler=reconciler, leader_host=1)

        client._handle_event("something-from-the-future", "42")

        assert client.leader_host == 1
        reconciler.poke.assert_not_called()

    def test_clearing_wakes_the_reconciler(self):
        reconciler = Mock()
        client = make_client(reconciler=reconciler, leader_host=1)

        client._handle_event("leader-host-unknown", "1")

        reconciler.poke.assert_called_once()


class TestResync:
    def test_re_announces_local_devices(self):
        client = make_client()
        receiver = Mock()
        client.local_receivers = [receiver]

        client.resync()

        receiver.notify_devices.assert_called_once()

    def test_cuts_short_the_reconnect_backoff(self):
        client = make_client()
        client.local_receivers = []

        client.resync()

        # `_sleep_between_retries` returns immediately rather than waiting.
        started = time.monotonic()
        client._sleep_between_retries(30.0)
        assert time.monotonic() - started < 1.0

    def test_the_shortcut_only_applies_once(self):
        client = make_client()
        client.local_receivers = []
        client.resync()
        client._sleep_between_retries(0.0)

        assert not client._resync.is_set()

    def test_a_dead_receiver_does_not_break_the_resync(self):
        client = make_client()
        dead = Mock()
        dead.notify_devices.side_effect = OSError("gone")
        alive = Mock()
        client.local_receivers = [dead, alive]

        client.resync()

        alive.notify_devices.assert_called_once()
