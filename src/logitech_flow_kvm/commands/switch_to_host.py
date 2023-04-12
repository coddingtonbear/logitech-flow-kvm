from argparse import ArgumentParser

from ..util import change_device_host
from ..util import get_device_by_path
from . import LogitechFlowKvmCommand


class SwitchToHost(LogitechFlowKvmCommand):
    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("device")
        parser.add_argument("host", type=int)

    def handle(self) -> None:
        device = get_device_by_path(self.options.device)

        change_device_host(device, self.options.host)
