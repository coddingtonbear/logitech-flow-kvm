from . import LogitechFlowKvmCommand
from ..util import get_devices


class ListDevices(LogitechFlowKvmCommand):
    def handle(self):
        for device in get_devices():
            print(device)
