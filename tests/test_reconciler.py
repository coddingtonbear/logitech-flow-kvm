from hidpp_fakes import ScriptedTransport
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
