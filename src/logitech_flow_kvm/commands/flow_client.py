from argparse import ArgumentParser
from functools import partial
import time

from logitech_receiver import Device, Receiver
from logitech_receiver.base import receivers, _HIDPP_Notification
from logitech_receiver.listener import EventsListener
import requests
from rich.console import Console

from . import LogitechFlowKvmCommand
from ..util import parse_connection_status, change_device_host


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()


class FlowClient(LogitechFlowKvmCommand):
    leader_id: str
    follower_ids: list[str]

    device_status: dict[Receiver, dict[Device, int]] = {}

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("server")
        parser.add_argument("--sleep-time", "-s", default=0.25, type=float)
        parser.add_argument("--port", "-p", default=24801, type=int)

    def callback(self, receiver: Receiver, msg: _HIDPP_Notification) -> None:
        if msg.sub_id == 0x41:
            result = parse_connection_status(msg.data)

            if receiver not in self.device_status:
                self.device_status[receiver] = {}

            try:
                device = receiver[msg.devnumber]
            except IndexError:
                return

            self.console.print("")
            if result["link_status"] == 0:
                self.device_status[receiver][device] = self.options.host_number
                self.console.print(
                    f":white_heavy_check_mark: [bold]Device {device.id} connected"
                )
                response = requests.put(
                    self.build_url(f"device", device.id),
                    data=str(self.options.host_number),
                )
                response.raise_for_status()
            else:
                self.console.print(f":x: [bold]Device {device.id} disconnected")

                if device.id == self.leader_id:
                    time.sleep(self.options.sleep_time)

                    response = requests.get(self.build_url("device", device.id))
                    response.raise_for_status()
                    target_host = int(response.content)

                    for known_device_status in self.device_status.values():
                        for known_device in known_device_status.keys():
                            if known_device.id in self.follower_ids:
                                self.console.print(
                                    f"Asking follower {known_device} to switch to {target_host}"
                                )
                                change_device_host(known_device, target_host)

    def build_url(self, *route_segments: str) -> str:
        return f"http://{self.options.server}:{self.options.port}/{'/'.join(route_segments)}"

    def handle(self):
        self.console = Console()
        self.console.print(f"[bold]Connecting to server at {self.build_url()}...")
        result = requests.get(self.build_url("configuration"))
        result.raise_for_status()

        response = result.json()
        self.console.print(f"Leader device serial number: {response['leader']}")
        self.leader_id = response["leader"]
        self.console.print(
            f"Follower device serial numbers: {', '.join(response['followers'])}"
        )
        self.follower_ids = response["followers"]

        for receiver_info in receivers():
            receiver = Receiver.open(receiver_info)

            listener = Listener(receiver, partial(self.callback, receiver))
            listener.start()

        self.console.print(f"Ready.")

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
