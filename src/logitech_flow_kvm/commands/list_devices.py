from typing import cast, Iterable

from logitech_receiver import Device, Receiver
from logitech_receiver.base import receivers, NoSuchDevice
from hidapi.udev import DeviceInfo

from rich.progress import Progress
from rich.table import Table
from rich.console import Console

from ..util import get_device_id
from . import LogitechFlowKvmCommand


def get_theoretical_max_device_count() -> int:
    max_count = 0

    for device_info in cast(Iterable[DeviceInfo], receivers()):
        receiver = Receiver.open(device_info)
        max_count += receiver.max_devices

    return max_count


def get_devices() -> Iterable[Device | None]:
    for device_info in cast(Iterable[DeviceInfo], receivers()):
        receiver = Receiver.open(device_info)
        for idx in range(receiver.max_devices):
            try:
                yield Device(receiver, idx + 1)
            except NoSuchDevice:
                yield None


class ListDevices(LogitechFlowKvmCommand):
    def handle(self) -> None:
        table = Table()

        table.add_column("ID")
        table.add_column("Product")
        table.add_column("Name")
        table.add_column("Serial")

        with Progress() as progress:
            enumerate_task = progress.add_task(
                "Finding devices...", total=get_theoretical_max_device_count()
            )

            for possible_device in get_devices():
                progress.advance(enumerate_task)
                if possible_device is not None:
                    table.add_row(
                        get_device_id(possible_device),
                        possible_device.wpid or possible_device.product_id,
                        possible_device.name or possible_device.codename or "",
                        possible_device.serial,
                    )

        console = Console()
        console.print(table)
