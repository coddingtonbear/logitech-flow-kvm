import threading
import time
from types import SimpleNamespace

import pytest

from logitech_flow_kvm.exceptions import DeviceNotFound
from logitech_flow_kvm.hidpp import manager as manager_module
from logitech_flow_kvm.hidpp.manager import ReceiverManager


def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("condition never became true")


@pytest.fixture
def env(monkeypatch):
    """Fakes for everything the manager touches, with a shared event log.

    `env.discovered` is what `find_receivers()` will report; entries are
    plain path strings, which `FakeReceiver` accepts in place of a
    `ReceiverInfo`.
    """
    env = SimpleNamespace(log=[], listeners=[], discovered=[])

    class FakeReceiver:
        def __init__(self, path):
            self.path = path
            env.log.append(("open", path))

        def enable_connection_notifications(self):
            env.log.append(("enable", self.path))

        def notify_devices(self):
            env.log.append(("notify", self.path))

        def close(self):
            env.log.append(("close", self.path))

    class FakeListener:
        def __init__(self, path, callback, on_disconnect=None):
            self.path = path
            self.on_disconnect = on_disconnect
            env.listeners.append(self)

        def start(self):
            env.log.append(("listener-start", self.path))

        def stop(self):
            env.log.append(("listener-stop", self.path))

    monkeypatch.setattr(manager_module, "Receiver", FakeReceiver)
    monkeypatch.setattr(manager_module, "NotificationListener", FakeListener)
    monkeypatch.setattr(manager_module, "find_receivers", lambda: list(env.discovered))
    env.FakeReceiver = FakeReceiver
    return env


def make_manager(env, receivers, rebind=None, interval=0.01) -> ReceiverManager:
    return ReceiverManager(
        receivers,
        rebind=rebind or (lambda rs: None),
        callback=lambda receiver, notification: None,
        rediscovery_interval=interval,
    )


def stop_and_join(manager: ReceiverManager) -> None:
    manager.stop()
    manager.join(timeout=5)
    assert not manager.is_alive()


class TestActivation:
    def test_notifies_devices_only_after_the_listener_is_running(self, env):
        manager = make_manager(env, [env.FakeReceiver("r0")])

        manager.start()
        wait_for(lambda: ("notify", "r0") in env.log)
        stop_and_join(manager)

        assert env.log.index(("enable", "r0")) < env.log.index(("listener-start", "r0"))
        assert env.log.index(("listener-start", "r0")) < env.log.index(("notify", "r0"))


class TestRecovery:
    def test_rebuilds_and_rebinds_after_a_listener_disconnect(self, env):
        rebinds: list[list] = []
        manager = make_manager(env, [env.FakeReceiver("r0")], rebind=rebinds.append)

        manager.start()
        wait_for(lambda: ("notify", "r0") in env.log)

        # The receiver comes back on a *different* hidraw path, as it
        # typically does after a USB replug.
        env.discovered = ["r1"]
        env.listeners[0].on_disconnect()

        wait_for(lambda: ("notify", "r1") in env.log)
        stop_and_join(manager)

        assert ("listener-stop", "r0") in env.log
        assert ("close", "r0") in env.log
        assert len(rebinds) == 1
        assert [receiver.path for receiver in rebinds[0]] == ["r1"]

    def test_retries_until_the_wanted_devices_are_back(self, env):
        attempts = []
        recovered = threading.Event()

        def rebind(receivers):
            attempts.append(receivers)
            if len(attempts) < 3:
                raise DeviceNotFound("FOLLOW01")
            recovered.set()

        env.discovered = ["r1"]
        manager = make_manager(env, [env.FakeReceiver("r0")], rebind=rebind)

        manager.start()
        wait_for(lambda: ("notify", "r0") in env.log)
        env.listeners[0].on_disconnect()

        assert recovered.wait(timeout=5)
        wait_for(lambda: ("notify", "r1") in env.log)
        stop_and_join(manager)

        assert len(attempts) == 3
        # Receivers opened by the failed attempts were closed again.
        assert env.log.count(("close", "r1")) >= 2

    def test_a_disconnect_during_recovery_triggers_another_recovery(self, env):
        rebinds: list[list] = []
        manager = make_manager(env, [env.FakeReceiver("r0")], rebind=rebinds.append)

        manager.start()
        wait_for(lambda: ("notify", "r0") in env.log)

        env.discovered = ["r1"]
        env.listeners[0].on_disconnect()
        wait_for(lambda: ("notify", "r1") in env.log)

        # The freshly rebuilt receiver dies too.
        env.discovered = ["r2"]
        env.listeners[-1].on_disconnect()
        wait_for(lambda: ("notify", "r2") in env.log)
        stop_and_join(manager)

        assert len(rebinds) == 2


class TestStop:
    def test_stop_closes_receivers_and_stops_listeners(self, env):
        manager = make_manager(env, [env.FakeReceiver("r0")])

        manager.start()
        wait_for(lambda: ("notify", "r0") in env.log)
        stop_and_join(manager)

        assert ("listener-stop", "r0") in env.log
        assert ("close", "r0") in env.log
