from argparse import ArgumentParser
from functools import partial
import time

from logitech_receiver import Receiver
from logitech_receiver.base import receivers, _HIDPP_Notification
from logitech_receiver.listener import EventsListener
import requests
from rich.console import Console

from . import LogitechFlowKvmCommand
from ..util import parse_connection_status


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()


class FlowClient(LogitechFlowKvmCommand):
    watched_ids: list[str] = []

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("server")
        parser.add_argument("--port", "-p", default=24801, type=int)

    def callback(self, receiver: Receiver, msg: _HIDPP_Notification) -> None:
        if msg.sub_id == 0x41:
            result = parse_connection_status(msg.data)

            device = receiver[msg.devnumber]
            self.console.print("")
            if result["link_status"] == 0:
                if device.id not in self.watched_ids:
                    self.console.print(
                        f":white_heavy_check_mark: [bold]Device {device.id} connected"
                    )
                    result = requests.put(
                        self.build_url(f"devices", device.id),
                        data=self.options.host_number,
                    )
                    result.raise_for_status()
                else:
                    self.console.print(
                        f":white_heavy_check_mark: [grey]Device {device.id} connected (ignored)"
                    )
            else:
                if device.id not in self.watched_ids:
                    self.console.print(f":x: [bold]Device {device.id} disconnected")
                else:
                    self.console.print(
                        f":x: [grey]Device {device.id} disconnected (ignored)"
                    )

    def build_url(self, *route_segments: str) -> str:
        return f"http://{self.options.server}:{self.options.port}/{'/'.join(route_segments)}"

    def handle(self):
        result = requests.get(self.build_url("device"))
        result.raise_for_status()

        console = Console()
        console.print(f"[bold]Connected to server at {self.build_url()}")

        for id in result.json().keys():
            self.watched_ids.append(id)
            console.print(
                f"Listening for connection status of device with serial number {id}"
            )

        for receiver_info in receivers():
            receiver = Receiver.open(receiver_info)

            listener = Listener(receiver, partial(self.callback, receiver))
            listener.start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass