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
