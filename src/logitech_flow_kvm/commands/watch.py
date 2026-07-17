import subprocess
from argparse import ArgumentParser

from rich.console import Console

from ..hidpp import Notification
from ..hidpp import NotificationListener
from ..hidpp import PairedDevice
from ..util import get_device_by_path
from ..util import parse_connection_status
from . import LogitechFlowKvmCommand


class Watch(LogitechFlowKvmCommand):
    device: PairedDevice | None
    console: Console = Console()

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("--on-disconnect-execute", "-d", nargs="*")
        parser.add_argument("--on-connect-execute", "-c", nargs="*")
        parser.add_argument("device")

    def execute_commands(self, option) -> None:
        if option:
            for cmd in option:
                status = subprocess.check_call(
                    cmd,
                    shell=True,
                )
                self.console.print(f"[cyan]Executed '{cmd}'; status {status}.")

    def callback(self, notification: Notification) -> None:
        if not self.device:
            return

        if notification.devnumber != self.device.number:
            # This message is for a different device
            return

        if notification.sub_id == 0x41:
            result = parse_connection_status(notification.data)

            self.console.print("")
            if result["link_status"] == 0:
                self.console.print(":white_heavy_check_mark: [bold]Device connected")
                self.execute_commands(self.options.on_connect_execute)
            else:
                self.console.print(":x: [bold]Device disconnected")
                self.execute_commands(self.options.on_disconnect_execute)

    def handle(self) -> None:
        device = get_device_by_path(self.options.device)
        self.device = device

        self.console.print(
            "Listening for connection events for "
            f"[italic]{self.options.device}[/italic]"
        )
        self.console.print("[bold]Press CTRL+C to exit")

        device.receiver.enable_connection_notifications()
        listener = NotificationListener(device.receiver.path, self.callback)
        listener.start()
        try:
            listener.join()
        except KeyboardInterrupt:
            listener.stop()
