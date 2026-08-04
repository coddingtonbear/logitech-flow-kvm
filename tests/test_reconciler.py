from unittest.mock import Mock

from hidpp_fakes import ScriptedTransport
from logitech_flow_kvm import reconciler as reconciler_module
from logitech_flow_kvm.hidpp.exceptions import ERROR_RESOURCE_ERROR
from logitech_flow_kvm.hidpp.exceptions import DeviceUnreachable
from logitech_flow_kvm.hidpp.models import ReceiverInfo
from logitech_flow_kvm.hidpp.receiver import PairedDevice
from logitech_flow_kvm.hidpp.receiver import Receiver
from logitech_flow_kvm.reconciler import Reconciler

RECEIVER_INFO = ReceiverInfo(
    path="/dev/hidraw4", product_id=0xC548, kind="bolt", interface=2
)


def make_device(number: int = 1) -> PairedDevice:
    receiver = Receiver(RECEIVER_INFO, transport=ScriptedTransport())
    return PairedDevice(
        receiver=receiver,
        number=number,
        wpid="0000",
        kind="mouse",
        serial=f"SERIAL{number}",
        codename=None,
    )


class TestReconcileOnce:
    def test_does_nothing_when_desired_host_is_unknown(self, monkeypatch):
        device = make_device()
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: None, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()

        assert calls == []

    def test_does_nothing_when_desired_host_is_already_here(self, monkeypatch):
        device = make_device()
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 1, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()

        assert calls == []

    def test_does_nothing_for_a_device_not_connected_here(self, monkeypatch):
        device = make_device()
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        # never observed as connected

        reconciler.reconcile_once()

        assert calls == []

    def test_pushes_a_connected_device_toward_the_desired_host(self, monkeypatch):
        device = make_device()
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()

        assert calls == [(device, 2)]

    def test_stops_pushing_once_the_device_disconnects(self, monkeypatch):
        device = make_device()
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)
        reconciler.observe(device, connected=False)

        reconciler.reconcile_once()

        assert calls == []

    def test_only_pushes_devices_it_was_given(self, monkeypatch):
        managed = make_device(1)
        unmanaged = make_device(2)
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([managed], get_desired_host=lambda: 2, host_number=1)

        # observe() on a device this reconciler doesn't manage is a no-op,
        # not an error -- it's simply not tracked.
        reconciler.observe(unmanaged, connected=True)
        reconciler.reconcile_once()

        assert calls == []

    def test_retries_every_tick_while_still_mismatched(self, monkeypatch):
        # `change_device_host` never confirms a switch -- the guarantee comes
        # entirely from retrying on every tick until convergence is observed.
        device = make_device()
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()
        reconciler.reconcile_once()

        assert calls == [(device, 2)] * 3


class TestReconcileOnceSurvivesFailures:
    # Regression coverage: a live HID++ round-trip can fail for reasons that
    # are entirely transient (e.g. the device is mid-roam and briefly
    # unreachable) -- `change_device_host` raising must never kill the loop,
    # since the whole guarantee this class provides comes from retrying
    # forever. Losing the background thread to an uncaught exception here
    # means "silently stop reconciling everything, permanently."

    def test_a_failure_does_not_raise_out_of_reconcile_once(self, monkeypatch):
        device = make_device()

        def raise_cannot_change(d, h):
            raise RuntimeError("device briefly unreachable")

        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", raise_cannot_change
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()  # must not raise

    def test_a_failure_on_one_device_does_not_stop_the_others(self, monkeypatch):
        failing = make_device(1)
        working = make_device(2)
        calls = []

        def change(d, h):
            if d is failing:
                raise RuntimeError("device briefly unreachable")
            calls.append((d, h))

        monkeypatch.setattr("logitech_flow_kvm.reconciler.change_device_host", change)
        reconciler = Reconciler(
            [failing, working], get_desired_host=lambda: 2, host_number=1
        )
        reconciler.observe(failing, connected=True)
        reconciler.observe(working, connected=True)

        reconciler.reconcile_once()

        assert calls == [(working, 2)]

    def test_reconciliation_keeps_retrying_after_a_failure(self, monkeypatch):
        device = make_device()
        attempts = []

        def flaky_then_working(d, h):
            attempts.append(d)
            if len(attempts) == 1:
                raise RuntimeError("device briefly unreachable")

        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", flaky_then_working
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()  # fails
        reconciler.reconcile_once()  # succeeds

        assert attempts == [device, device]

    def test_calls_on_error_with_the_device_and_exception(self, monkeypatch):
        device = make_device()
        error = RuntimeError("device briefly unreachable")

        def raise_error(d, h):
            raise error

        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", raise_error
        )
        seen = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: seen.append((d, e)),
        )
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()

        assert seen == [(device, error)]

    def test_on_error_is_optional(self, monkeypatch):
        device = make_device()

        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(RuntimeError("unreachable")),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()  # must not raise despite no on_error given


class TestSetDevices:
    def test_swapped_in_devices_are_reconciled_after_observation(self, monkeypatch):
        old_device = make_device(1)
        new_device = make_device(2)
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([old_device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(old_device, connected=True)

        reconciler.set_devices([new_device])
        reconciler.observe(new_device, connected=True)
        reconciler.reconcile_once()

        # Only the new device is reconciled; the old one is gone entirely.
        assert calls == [(new_device, 2)]

    def test_connection_state_resets_to_unobserved(self, monkeypatch):
        device = make_device(1)
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        # Even for the same device object, a swap means its old "connected"
        # observation belonged to a receiver that no longer exists.
        reconciler.set_devices([device])
        reconciler.reconcile_once()

        assert calls == []

    def test_pokes_the_loop(self):
        reconciler = Reconciler([], get_desired_host=lambda: None, host_number=1)
        reconciler._wake.clear()

        reconciler.set_devices([make_device(1)])

        assert reconciler._wake.is_set()


class TestObserve:
    def test_poke_wakes_a_waiting_run_loop(self):
        device = make_device()
        reconciler = Reconciler([device], get_desired_host=lambda: None, host_number=1)

        reconciler.poke()

        assert reconciler._wake.is_set()

    def test_observe_pokes_the_loop(self):
        device = make_device()
        reconciler = Reconciler([device], get_desired_host=lambda: None, host_number=1)

        reconciler.observe(device, connected=True)

        assert reconciler._wake.is_set()

    def test_stop_sets_both_stop_and_wake(self):
        device = make_device()
        reconciler = Reconciler([device], get_desired_host=lambda: None, host_number=1)

        reconciler.stop()

        assert reconciler._stop.is_set()
        assert reconciler._wake.is_set()


class TestUnreachableDevicesAreBelieved:
    """The bug this guards against: `_connected` used to be written *only* by
    connect/disconnect notifications, so a single lost disconnect left the
    loop driving a device that had already left -- one failed HID++ round-trip
    and one WARNING every tick, forever, with nothing able to correct it.
    A receiver answering "no link to that device" is the same evidence the
    lost notification carried, so the loop now acts on it."""

    def test_an_unreachable_device_stops_being_driven(self, monkeypatch):
        device = make_device()
        attempts = []

        def unreachable(d, h):
            attempts.append(d)
            raise DeviceUnreachable(d.id)

        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", unreachable
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()
        reconciler.reconcile_once()

        # Tried once, learned it isn't here, and left it alone thereafter.
        assert attempts == [device]

    def test_unreachable_is_not_reported_as_an_error(self, monkeypatch):
        # It isn't a failure to warn about -- it's an answer.
        device = make_device()
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(DeviceUnreachable(d.id)),
        )
        errors = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: errors.append((d, e)),
        )
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()

        assert errors == []

    def test_unreachable_is_surfaced_as_an_observation(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(DeviceUnreachable(d.id)),
        )
        observations = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_observation=lambda d, c: observations.append((d, c)),
        )
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()

        # Reported once, on the transition -- not on every subsequent tick.
        assert observations == [(device, False)]

    def test_a_later_connect_notification_re_arms_the_device(self, monkeypatch):
        device = make_device()
        attempts = []

        def unreachable_then_fine(d, h):
            attempts.append(h)
            if len(attempts) == 1:
                raise DeviceUnreachable(d.id)

        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", unreachable_then_fine
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()  # unreachable; belief cleared
        reconciler.reconcile_once()  # skipped
        reconciler.observe(device, connected=True)  # it came back
        reconciler.reconcile_once()

        assert attempts == [2, 2]

    def test_on_observation_is_optional(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(DeviceUnreachable(d.id)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()  # must not raise despite no on_observation


class TestProbingForMissedArrivals:
    """The other half of not trusting notifications blindly: if a *connect*
    notification is lost (or the loop cleared a belief for a device that was
    only briefly unreachable), nothing would ever drive that device again.
    So devices believed absent get re-pinged on a coarse interval."""

    def test_a_device_believed_absent_is_probed_and_then_driven(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(device.receiver, "ping_device", lambda number: 4.5)
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)

        reconciler.reconcile_once()
        assert calls == []  # not probe time yet

        reconciler._last_probe -= reconciler_module.PROBE_INTERVAL
        reconciler.reconcile_once()

        assert calls == [(device, 2)]

    def test_a_successful_probe_is_surfaced_as_an_observation(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(device.receiver, "ping_device", lambda number: 4.5)
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", lambda d, h: None
        )
        observations = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_observation=lambda d, c: observations.append((d, c)),
        )

        reconciler._last_probe -= reconciler_module.PROBE_INTERVAL
        reconciler.reconcile_once()

        assert observations == [(device, True)]

    def test_a_device_that_does_not_answer_is_left_alone(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(device.receiver, "ping_device", lambda number: None)
        calls = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: calls.append((d, h)),
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)

        reconciler._last_probe -= reconciler_module.PROBE_INTERVAL
        reconciler.reconcile_once()

        assert calls == []

    def test_a_failing_probe_does_not_kill_the_loop(self, monkeypatch):
        device = make_device()

        def raise_oserror(number):
            raise OSError("receiver went away mid-ping")

        monkeypatch.setattr(device.receiver, "ping_device", raise_oserror)
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: None,
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)

        reconciler._last_probe -= reconciler_module.PROBE_INTERVAL
        reconciler.reconcile_once()  # must not raise

    def test_connected_devices_are_never_probed(self, monkeypatch):
        # Probing is repair, not the normal path: a device we already believe
        # is here must not pay for a ping on every probe tick.
        device = make_device()
        ping_mock = Mock(side_effect=AssertionError("should not be probed"))
        monkeypatch.setattr(device.receiver, "ping_device", ping_mock)
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host", lambda d, h: None
        )
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)

        reconciler._last_probe -= reconciler_module.PROBE_INTERVAL
        reconciler.reconcile_once()

        ping_mock.assert_not_called()

    def test_no_probing_happens_without_a_mismatch_to_resolve(self, monkeypatch):
        device = make_device()
        ping_mock = Mock(side_effect=AssertionError("should not be probed"))
        monkeypatch.setattr(device.receiver, "ping_device", ping_mock)
        reconciler = Reconciler([device], get_desired_host=lambda: 1, host_number=1)

        reconciler._last_probe -= reconciler_module.PROBE_INTERVAL
        reconciler.reconcile_once()

        ping_mock.assert_not_called()


class TestRepeatedFailuresAreReportedOnce:
    """A failure that persists is still retried every tick, but saying so at
    WARNING every couple of seconds forever is noise, not information."""

    def test_an_unchanged_failure_is_reported_only_once(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(RuntimeError("device asleep")),
        )
        errors = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: errors.append(str(e)),
        )
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()
        reconciler.reconcile_once()

        assert errors == ["device asleep"]

    def test_a_different_failure_is_reported_again(self, monkeypatch):
        device = make_device()
        messages = iter(["device asleep", "device asleep", "receiver busy"])
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(RuntimeError(next(messages))),
        )
        errors = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: errors.append(str(e)),
        )
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()
        reconciler.reconcile_once()

        assert errors == ["device asleep", "receiver busy"]

    def test_a_recurrence_after_a_success_is_reported_again(self, monkeypatch):
        device = make_device()
        outcomes = iter(
            [RuntimeError("device asleep"), None, RuntimeError("device asleep")]
        )

        def change(d, h):
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome

        monkeypatch.setattr("logitech_flow_kvm.reconciler.change_device_host", change)
        errors = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: errors.append(str(e)),
        )
        reconciler.observe(device, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()
        reconciler.reconcile_once()

        assert errors == ["device asleep", "device asleep"]

    def test_each_device_is_tracked_separately(self, monkeypatch):
        first = make_device(1)
        second = make_device(2)
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: (_ for _ in ()).throw(RuntimeError("device asleep")),
        )
        errors = []
        reconciler = Reconciler(
            [first, second],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: errors.append(d),
        )
        reconciler.observe(first, connected=True)
        reconciler.observe(second, connected=True)

        reconciler.reconcile_once()
        reconciler.reconcile_once()

        assert errors == [first, second]


class TestTheReportedFailure:
    """Reproduces the reported symptom end-to-end -- through the real
    `change_device_host`, a real `Receiver`, and a transport answering exactly
    what the user's receiver answered:

        Could not switch F262458A to the desired host yet (HID++ error 0x09);
        will retry            (once every 2s, forever)
    """

    @staticmethod
    def _mouse_that_has_left() -> tuple[PairedDevice, list[int]]:
        """A mouse whose receiver answers 0x09 -- i.e. it has switched away."""
        attempts: list[int] = []

        def respond(devnumber, payload, long_message):
            attempts.append(devnumber)
            return b"\x8f" + payload[:2] + bytes([ERROR_RESOURCE_ERROR])

        receiver = Receiver(RECEIVER_INFO, transport=ScriptedTransport(respond=respond))
        device = PairedDevice(
            receiver=receiver,
            number=1,
            wpid="0000",
            kind="mouse",
            serial="F262458A",
            codename="MX Anywhere 2S",
        )
        return device, attempts

    def test_the_loop_stops_and_says_so_once(self):
        device, attempts = self._mouse_that_has_left()
        warnings = []
        observations = []
        reconciler = Reconciler(
            [device],
            get_desired_host=lambda: 2,
            host_number=1,
            on_error=lambda d, e: warnings.append(str(e)),
            on_observation=lambda d, c: observations.append(c),
        )
        # The last thing we heard was a connect notification; the disconnect
        # that should have followed never arrived.
        reconciler.observe(device, connected=True)

        for _ in range(10):
            reconciler.reconcile_once()

        assert len(attempts) == 1  # not ten HID++ round-trips
        assert warnings == []  # not ten WARNINGs
        assert observations == [False]  # said once: it isn't here
        assert reconciler._connected[device] is False  # and the UI agrees

    def test_it_resumes_when_the_mouse_comes_back(self, monkeypatch):
        device, _ = self._mouse_that_has_left()
        reconciler = Reconciler([device], get_desired_host=lambda: 2, host_number=1)
        reconciler.observe(device, connected=True)
        reconciler.reconcile_once()
        assert reconciler._connected[device] is False

        # It's back, and this time the receiver can reach it.
        switched = []
        monkeypatch.setattr(
            "logitech_flow_kvm.reconciler.change_device_host",
            lambda d, h: switched.append((d.id, h)),
        )
        reconciler.observe(device, connected=True)
        reconciler.reconcile_once()

        assert switched == [("F262458A", 2)]
