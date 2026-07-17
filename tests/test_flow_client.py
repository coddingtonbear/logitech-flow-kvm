import argparse
import threading
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
    client.console = Mock()
    for key, value in attrs.items():
        setattr(client, key, value)
    return client


def connection_notification(devnumber: int, *, connected: bool) -> Notification:
    data = b"\x00\x00\x00" if connected else b"\x40\x00\x00"
    return Notification(
        report_id=0x10, devnumber=devnumber, sub_id=0x41, address=0, data=data
    )


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

        assert sent["method"] == "PUT"
        assert sent["url"] == client.build_url("clipboard")
        assert sent["data"] == b"local-clip"

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
        monkeypatch.setattr(flow_client.time, "sleep", lambda s: client._stop.set())

        client._consume_events()

        assert client.leader_host == 5
        fake_receiver.notify_devices.assert_called_once()

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

        monkeypatch.setattr(flow_client.time, "sleep", fake_sleep)

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

        monkeypatch.setattr(flow_client.time, "sleep", fake_sleep)

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

        monkeypatch.setattr(flow_client.time, "sleep", fake_sleep)

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
