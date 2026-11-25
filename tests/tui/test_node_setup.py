import asyncio
from unittest.mock import Mock

from textual.widgets import Label
from textual.widgets import Select
from textual.widgets import SelectionList

from logitech_flow_kvm.monitors import Ddcutil
from logitech_flow_kvm.monitors import InputSource
from logitech_flow_kvm.monitors import Monitor
from logitech_flow_kvm.monitors import MonitorCapabilities
from logitech_flow_kvm.node_config import MonitorInputConfig
from logitech_flow_kvm.node_config import NodeConfig
from logitech_flow_kvm.node_protocol import DeviceAdvertisement
from logitech_flow_kvm.tui.node_setup import NodeSetupApp


def run(coro):
    return asyncio.run(coro)


def device(device_id: str, name: str) -> DeviceAdvertisement:
    return DeviceAdvertisement(device_id, "B369", name, "unknown", True)


def test_remembers_configured_devices_that_are_temporarily_absent():
    async def body():
        current = NodeConfig(2, "KEYS", ["MOUSE"])
        app = NodeSetupApp(lambda: [], current)
        async with app.run_test():
            assert app.query_one("#leader", Select).value == "KEYS"
            assert app.query_one("#followers", SelectionList).selected == ["MOUSE"]

    run(body())


def test_discovery_failure_is_explained_in_the_ui():
    def fail():
        raise PermissionError("cannot open /dev/hidraw9")

    async def body():
        app = NodeSetupApp(fail)
        async with app.run_test():
            problem = str(app.query_one("#problem", Label).render())
            assert "cannot open /dev/hidraw9" in problem

    run(body())


def test_monitor_mccs_and_feature_60_values_are_selectable():
    monitor = Monitor("PHL@HDMI-5", 1, 13, "HDMI-5", "Philips 278E1")
    ddcutil = Mock(spec=Ddcutil)
    ddcutil.detect.return_value = [monitor]
    ddcutil.capabilities.return_value = MonitorCapabilities(
        "2.2",
        [InputSource(0x11, "HDMI-1"), InputSource(0x0F, "DisplayPort-1")],
    )

    async def body():
        current = NodeConfig(
            2,
            "KEYS",
            ["MOUSE"],
            monitor_inputs=[MonitorInputConfig(monitor.id, 0x11)],
        )
        app = NodeSetupApp(lambda: [], current, ddcutil)
        async with app.run_test():
            labels = " ".join(str(label.render()) for label in app.query(Label))
            assert "MCCS 2.2" in labels
            assert app.query_one("#monitor-input-0", Select).value == 0x11

    run(body())


def test_host_number_must_be_an_easy_switch_channel():
    async def body():
        current = NodeConfig(58405840584058405840, "KEYS", ["MOUSE"])
        app = NodeSetupApp(lambda: [], current)
        async with app.run_test():
            app._save()
            problem = str(app.query_one("#problem", Label).render())
            assert "Easy-Switch channel" in problem
            assert app.return_value is None

    run(body())
