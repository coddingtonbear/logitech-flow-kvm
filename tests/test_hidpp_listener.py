import threading

import pytest

from logitech_flow_kvm.hidpp import listener as listener_module
from logitech_flow_kvm.hidpp.listener import NotificationListener


class ScriptedHidRawIO:
    """Stands in for HidRawIO: replays scripted reports, then raises OSError
    (as a real hidraw read does once the receiver is unplugged)."""

    reports: list[tuple[int, int, bytes]] = []

    def __init__(self, path: str):
        self._pending = list(self.reports)

    def __enter__(self) -> "ScriptedHidRawIO":
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass

    def read(self, timeout: float) -> tuple[int, int, bytes] | None:
        if self._pending:
            return self._pending.pop(0)
        raise OSError("receiver disconnected")


CONNECT_REPORT = (0x10, 0x01, b"\x41\x04\x61\x10\x00")


@pytest.fixture
def scripted_io(monkeypatch):
    monkeypatch.setattr(listener_module, "HidRawIO", ScriptedHidRawIO)
    return ScriptedHidRawIO


def test_delivers_notifications_to_callback(scripted_io):
    scripted_io.reports = [CONNECT_REPORT, CONNECT_REPORT]
    received = []

    listener = NotificationListener("/dev/hidraw-test", received.append)
    listener.start()
    listener.join(timeout=5)

    assert not listener.is_alive()
    assert len(received) == 2
    assert all(notification.sub_id == 0x41 for notification in received)


def test_on_disconnect_fires_when_the_receiver_vanishes(scripted_io):
    scripted_io.reports = [CONNECT_REPORT]
    disconnected = []

    listener = NotificationListener(
        "/dev/hidraw-test", lambda n: None, on_disconnect=lambda: disconnected.append(1)
    )
    listener.start()
    listener.join(timeout=5)

    assert disconnected == [1]


def test_on_disconnect_fires_when_the_node_cannot_be_opened(monkeypatch):
    class UnopenableIO:
        def __init__(self, path):
            raise OSError("no such device")

    monkeypatch.setattr(listener_module, "HidRawIO", UnopenableIO)
    disconnected = []

    listener = NotificationListener(
        "/dev/hidraw-test", lambda n: None, on_disconnect=lambda: disconnected.append(1)
    )
    listener.start()
    listener.join(timeout=5)

    assert disconnected == [1]


def test_on_disconnect_does_not_fire_when_stopped(monkeypatch):
    reading = threading.Event()

    class IdleIO:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def read(self, timeout):
            reading.set()
            return None

    monkeypatch.setattr(listener_module, "HidRawIO", IdleIO)
    disconnected = []

    listener = NotificationListener(
        "/dev/hidraw-test", lambda n: None, on_disconnect=lambda: disconnected.append(1)
    )
    listener.start()
    assert reading.wait(timeout=5)
    listener.stop()
    listener.join(timeout=5)

    assert not listener.is_alive()
    assert disconnected == []


def test_callback_exception_does_not_kill_listener(scripted_io, caplog):
    scripted_io.reports = [CONNECT_REPORT, CONNECT_REPORT]
    calls = []

    def failing_callback(notification):
        calls.append(notification)
        if len(calls) == 1:
            raise RuntimeError("simulated network failure")

    listener = NotificationListener("/dev/hidraw-test", failing_callback)
    listener.start()
    listener.join(timeout=5)

    assert not listener.is_alive()
    # The second notification was still delivered despite the first
    # callback invocation raising.
    assert len(calls) == 2
    assert "Notification callback failed" in caplog.text
