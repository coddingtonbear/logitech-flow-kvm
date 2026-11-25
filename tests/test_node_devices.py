from unittest.mock import Mock

from logitech_flow_kvm.node_devices import NodeDeviceManager


def manager() -> NodeDeviceManager:
    return NodeDeviceManager(lambda: None, lambda _device_id: None)


def test_missing_follower_is_a_normal_noop(monkeypatch):
    devices = manager()
    switch = Mock()
    monkeypatch.setattr("logitech_flow_kvm.node_devices.change_device_host", switch)

    devices.switch_connected(["MOUSE-ON-ANOTHER-HOST"], 2)

    switch.assert_not_called()


def test_only_a_connected_local_follower_is_switched(monkeypatch):
    devices = manager()
    mouse = Mock(id="MOUSE")
    devices._devices = {"MOUSE": mouse}
    devices._connected = {"MOUSE"}
    switch = Mock()
    monkeypatch.setattr("logitech_flow_kvm.node_devices.change_device_host", switch)

    devices.switch_connected(["MOUSE", "ABSENT"], 3)

    switch.assert_called_once_with(mouse, 3)


def test_follower_configured_by_bluetooth_address_resolves_to_unit_id(monkeypatch):
    devices = manager()
    mouse = Mock(id="D39B7B5F")
    devices._devices = {"D39B7B5F": mouse}
    devices._connected = {"D39B7B5F"}
    devices._aliases = {"D39B7B5F": "D39B7B5F", "D7:8E:D4:61:21:F2": "D39B7B5F"}
    switch = Mock()
    monkeypatch.setattr("logitech_flow_kvm.node_devices.change_device_host", switch)

    devices.switch_connected(["d7:8e:d4:61:21:f2"], 1)

    switch.assert_called_once_with(mouse, 1)


def test_canonical_id_leaves_unknown_ids_alone():
    devices = manager()

    assert devices.canonical_id("SOMETHING") == "SOMETHING"
