from typing import cast
from unittest.mock import Mock

from logitech_flow_kvm.monitors import Monitor
from logitech_flow_kvm.node import FlowNode
from logitech_flow_kvm.node_config import MonitorInputConfig
from logitech_flow_kvm.node_config import NodeConfig
from logitech_flow_kvm.node_protocol import NodeAdvertisement


def make_node() -> FlowNode:
    node = FlowNode(NodeConfig(2, "KEYS-LOCAL", ["MOUSE-LOCAL"]), "secret")
    node.devices = Mock()
    node.transport = Mock()
    node._transport_started = True
    return node


def test_local_leader_connection_announces_this_host():
    node = make_node()

    node._device_connected("KEYS-LOCAL")

    sent = cast(Mock, node.transport.send).call_args.args[0]
    assert sent.target_host == 2


def test_leader_configured_by_bluetooth_address_matches_its_unit_id():
    node = FlowNode(NodeConfig(2, "D7:C3:34:36:65:E2", ["MOUSE"]), "secret")
    node.devices = Mock()
    node.devices.canonical_id.side_effect = lambda device_id: (
        "694821BA" if device_id == "D7:C3:34:36:65:E2" else device_id
    )
    node.transport = Mock()
    node._transport_started = True

    node._device_connected("694821BA")

    sent = cast(Mock, node.transport.send).call_args.args[0]
    assert sent.target_host == 2


def test_host_number_outside_easy_switch_range_is_reported():
    node = make_node()
    node.config = NodeConfig(58405840, "KEYS-LOCAL", ["MOUSE-LOCAL"])
    node._announcer = Mock()

    node.start()

    assert node.last_problem is not None
    assert "58405840" in node.last_problem


def test_follower_connection_does_not_announce_a_host_change():
    node = make_node()

    node._device_connected("MOUSE-LOCAL")

    cast(Mock, node.transport.send).assert_not_called()


def test_remote_target_switches_only_configured_local_followers():
    node = make_node()
    message = NodeAdvertisement("birch", "birch", 3, [], "KEYS", target_host=3)

    node._message(message, "192.168.0.33")

    cast(Mock, node.devices.switch_connected).assert_called_once_with(
        ["MOUSE-LOCAL"], 3
    )
    assert node.peers["birch"].address == "192.168.0.33"


def test_own_broadcast_is_ignored():
    node = make_node()
    message = NodeAdvertisement(
        node.node_id, node.hostname, 2, [], "KEYS", target_host=2
    )

    node._message(message, "192.168.0.32")

    cast(Mock, node.devices.switch_connected).assert_not_called()


def test_invalid_secret_problem_is_visible_in_status():
    changed = Mock()
    node = FlowNode(NodeConfig(2, "KEYS", ["MOUSE"]), "secret", on_change=changed)

    node._invalid_packet("192.168.0.33", ValueError("shared secrets differ"))

    assert "shared secrets differ" in (node.last_problem or "")
    changed.assert_called_once()


def test_configured_monitor_is_switched_to_its_local_input():
    ddcutil = Mock()
    monitor = Monitor("PHL@HDMI-5", 1, 13, "HDMI-5", "Philips")
    ddcutil.detect.return_value = [monitor]
    node = FlowNode(
        NodeConfig(
            2,
            "KEYS",
            ["MOUSE"],
            monitor_inputs=[MonitorInputConfig(monitor.id, 0x11)],
        ),
        "secret",
        ddcutil=ddcutil,
    )

    node._switch_monitors()

    ddcutil.set_input.assert_called_once_with(monitor, 0x11)
