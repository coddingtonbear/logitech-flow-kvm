from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from ..util import get_devices
from ..util import get_theoretical_max_device_count
from . import LogitechFlowKvmCommand


class ListDevices(LogitechFlowKvmCommand):
    def handle(self) -> None:
        table = Table()

        table.add_column("ID")
        table.add_column("Product")
        table.add_column("Name")
        table.add_column("Path")

        with Progress(transient=True) as progress:
            enumerate_task = progress.add_task(
                "Finding devices...", total=get_theoretical_max_device_count()
            )

            for possible_device in get_devices():
                progress.advance(enumerate_task)
                if possible_device is not None:
                    table.add_row(
                        possible_device.serial or "",
                        possible_device.wpid,
                        possible_device.codename or "",
                        possible_device.path,
                    )

        console = Console()
        console.print(table)
