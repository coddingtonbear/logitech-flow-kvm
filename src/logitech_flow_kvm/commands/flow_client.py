import random
import string
import time
from argparse import ArgumentParser
from functools import partial
from typing import Literal

import requests
from logitech_receiver import Device
from logitech_receiver import Receiver
from logitech_receiver.base import _HIDPP_Notification
from logitech_receiver.base import receivers
from logitech_receiver.listener import EventsListener
from rich.console import Console
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from .. import constants
from .. import exceptions
from ..util import change_device_host
from ..util import get_certificate_key_path
from ..util import get_valid_filename
from ..util import parse_connection_status
from . import LogitechFlowKvmCommand


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()


class FlowClient(LogitechFlowKvmCommand):
    leader_id: str
    follower_ids: list[str]
    cert_and_key: tuple[str, str] | None = None

    device_status: dict[Receiver, dict[Device, int]] = {}

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("server")
        parser.add_argument("--sleep-time", "-s", default=0.25, type=float)
        parser.add_argument("--port", "-p", default=constants.DEFAULT_PORT, type=int)

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
                response = self.request(
                    "PUT",
                    self.build_url("device", device.id),
                    data=str(self.options.host_number),
                )
                response.raise_for_status()
            else:
                self.console.print(f":x: [bold]Device {device.id} disconnected")

                if device.id == self.leader_id:
                    time.sleep(self.options.sleep_time)

                    response = self.request("GET", self.build_url("device", device.id))
                    response.raise_for_status()
                    target_host = int(response.content)

                    for known_device_status in self.device_status.values():
                        for known_device in known_device_status.keys():
                            if known_device.id in self.follower_ids:
                                self.console.print(
                                    f"Asking follower {known_device} to "
                                    f" switch to {target_host}"
                                )
                                change_device_host(known_device, target_host)

    def build_url(self, *route_segments: str) -> str:
        return (
            f"https://{self.options.server}:{self.options.port}"
            f"/{'/'.join(route_segments)}"
        )

    def request(
        self, method: Literal["GET", "PUT", "OPTIONS", "POST"], url: str, **kwargs
    ) -> requests.Response:
        try:
            return requests.request(method, url, cert=self.cert_and_key, **kwargs)
        except requests.exceptions.SSLError:
            raise exceptions.ServerNotPaired()

    def get_certificate_key_path(self) -> tuple[str, str]:
        try:
            cert, key = get_certificate_key_path(
                get_valid_filename(self.options.server)
            )
            return cert, key
        except exceptions.NoCertificateAvailable:
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

            self.console.print(
                f"[magenta]Pairing with new server {self.options.server}..."
            )
            response = self.request("OPTIONS", self.build_url("pairing"), verify=False)
            if not response.ok:
                raise exceptions.ServerNotAvailable(self.options.server)

            pairing_code = "".join(random.choices(string.digits, k=6))
            self.console.print(
                f"[magenta]Pairing code: [bold][bright_magenta]{pairing_code}"
            )
            self.console.print(
                "[magenta]To complete the pairing process, enter the above code "
                "into the server console running `flow-server` when requested."
            )

            response = self.request(
                "POST", self.build_url("pairing"), verify=False, data=pairing_code
            )
            if response.ok:
                data = response.json()
                cert, key = get_certificate_key_path(
                    get_valid_filename(self.options.server),
                    create=True,
                    data=(data["certificate"], data["key"]),
                )
                return cert, key
            else:
                raise exceptions.PairingFailed()

    def handle(self):
        self.console = Console()

        self.cert_and_key = self.get_certificate_key_path()

        self.console.print(f"[bold]Connecting to server at {self.build_url()}...")
        result = self.request("GET", self.build_url("configuration"))
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

        self.console.print("Ready.")

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
