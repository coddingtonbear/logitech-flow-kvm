from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from ..util import get_device_path
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
                        possible_device.serial,
                        possible_device.wpid or possible_device.product_id,
                        possible_device.name or possible_device.codename or "",
                        get_device_path(possible_device),
                    )

        console = Console()
        console.print(table)
