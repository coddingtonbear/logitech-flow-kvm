from logitech_flow_kvm.device_matching import match_device
from logitech_flow_kvm.device_matching import normalize_device_id
from logitech_flow_kvm.node_protocol import DeviceAdvertisement


def device(id: str, product: str = "B02A", kind: str = "mouse"):
    return DeviceAdvertisement(id, product, "M750", kind, True)


def test_normalizes_bluetooth_ids():
    assert normalize_device_id("d7:8e:d4:61:21:ec") == "D78ED46121EC"


def test_matches_host_specific_suffix_by_longest_prefix():
    local = device("D7:8E:D4:61:21:EB")
    assert match_device(device("D7:8E:D4:61:21:EC"), [local]) == local


def test_rejects_a_different_product_or_kind():
    remote = device("D7:8E:D4:61:21:EC")
    assert match_device(remote, [device("D7:8E:D4:61:21:EB", "OTHER")]) is None
    assert match_device(remote, [device("D7:8E:D4:61:21:EB", kind="keyboard")]) is None


def test_does_not_guess_when_best_matches_are_ambiguous():
    remote = device("D7:8E:D4:61:21:EC")
    local = [device("D7:8E:D4:61:21:EA"), device("D7:8E:D4:61:21:EB")]
    assert match_device(remote, local) is None


def test_bluetooth_unknown_kind_matches_receiver_reported_kind():
    remote = device("694821BA", "B369", "unknown")
    local = device("694821BA", "B369", "keyboard")
    assert match_device(remote, [local]) == local
