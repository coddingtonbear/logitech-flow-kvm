from argparse import ArgumentParser
from typing import Any

from logitech_receiver import Device
from logitech_receiver.settings_templates import check_feature_setting
from solaar.cli.config import select_choice

from . import LogitechFlowKvmCommand
from ..exceptions import CannotChangeHost, ChangeHostFailed
from ..util import get_device_by_id


class SwitchToHost(LogitechFlowKvmCommand):
    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("device")
        parser.add_argument("host")

    def get_setting(self, device: Device) -> Any:
        setting = check_feature_setting(device, "change-host")
        if setting:
            return setting

        if device.descriptor and device.descriptor.settings:
            for setting_class in device.descriptor.settings:
                if setting_class.register and setting_class.name == "change-host":
                    return setting_class.build(device)

    def handle(self) -> None:
        device = get_device_by_id(self.options.device)

        setting = self.get_setting(device)
        if not setting:
            raise CannotChangeHost(self.options.device)

        target_value = select_choice(self.options.host, setting.choices, setting, None)
        result = setting.write(target_value, save=False)

        if not result:
            raise ChangeHostFailed()
