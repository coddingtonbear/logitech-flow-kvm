from argparse import ArgumentParser
import subprocess

from logitech_receiver import Device
from logitech_receiver.base import _HIDPP_Notification
from logitech_receiver.listener import EventsListener
from rich.console import Console

from . import LogitechFlowKvmCommand
from ..util import get_device_by_id, parse_connection_status


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()


class Watch(LogitechFlowKvmCommand):
    device: Device | None
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

    def callback(self, msg: _HIDPP_Notification) -> None:
        if msg.devnumber != self.device.number:
            # This message is for a different device
            return

        if msg.sub_id == 0x41:
            result = parse_connection_status(msg.data)

            self.console.print("")
            if result["link_status"] == 0:
                self.console.print(":white_heavy_check_mark: [bold]Device connected")
                self.execute_commands(self.options.on_connect_execute)
            else:
                self.console.print(":x: [bold]Device disconnected")
                self.execute_commands(self.options.on_disconnect_execute)

    def handle(self) -> None:
        device = get_device_by_id(self.options.device)
        self.device = device

        self.console.print(
            f"Listening for connection events for [italic]{self.options.device}[/italic]"
        )
        self.console.print("[bold]Press CTRL+C to exit")

        listener = Listener(device.receiver, self.callback)
        listener.start()
        try:
            listener.join()
        except KeyboardInterrupt:
            pass
