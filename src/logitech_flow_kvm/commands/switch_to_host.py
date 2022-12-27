from argparse import ArgumentParser

from . import LogitechFlowKvmCommand
from ..util import get_device_by_id, change_device_host


class SwitchToHost(LogitechFlowKvmCommand):
    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("device")
        parser.add_argument("host", type=int)

    def handle(self) -> None:
        device = get_device_by_id(self.options.device)

        change_device_host(device, self.options.host)
