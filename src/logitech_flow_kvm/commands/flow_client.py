import os
import random
import string
import time
from argparse import ArgumentParser
from functools import partial
from typing import Literal

import pyperclip
import requests
import urllib3
from logitech_receiver import Device
from logitech_receiver import Receiver
from logitech_receiver.base import _HIDPP_Notification
from logitech_receiver.base import receivers
from logitech_receiver.listener import EventsListener
from rich.console import Console
from rich.table import Table
from urllib3.exceptions import InsecureRequestWarning

from .. import constants
from .. import exceptions
from ..util import change_device_host
from ..util import get_host_certificate_path
from ..util import parse_connection_status
from . import LogitechFlowKvmCommand


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()


class FlowClient(LogitechFlowKvmCommand):
    leader_id: str
    follower_ids: list[str]
    cert: str | None = None

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

                clipboard_response = self.request("GET", self.build_url("clipboard"))
                if clipboard_response.ok:
                    pyperclip.copy(clipboard_response.text)
            else:
                self.console.print(f":x: [bold]Device {device.id} disconnected")

                if device.id == self.leader_id:
                    clipboard_data = pyperclip.paste()
                    clipboard_response = self.request(
                        "PUT", self.build_url("clipboard"), data=clipboard_data
                    )
                    if clipboard_response.ok:
                        self.console.print(
                            "Clipboard contents set on server with "
                            f"{len(clipboard_data)} bytes of data"
                        )

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
        if "verify" not in kwargs:
            kwargs["verify"] = self.cert

        return requests.request(method, url, **kwargs)

    def pair(self) -> None:
        urllib3.disable_warnings(InsecureRequestWarning)

        self.console.print(f"[magenta]Pairing with new server {self.options.server}...")
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
        if not response.ok:
            raise exceptions.PairingFailed()

        cert_path = get_host_certificate_path(self.options.server)
        with open(cert_path, "w") as outf:
            outf.write(response.text)

    def get_certificate_path(self) -> str:
        cert_path = get_host_certificate_path(self.options.server)
        if os.path.exists(cert_path):
            try:
                self.request("OPTIONS", self.build_url("pairing"), verify=cert_path)
            except requests.exceptions.SSLError:
                self.pair()
            except requests.exceptions.RequestException:
                raise exceptions.ServerNotAvailable()
        else:
            self.pair()

        return cert_path

    def handle(self):
        self.console = Console()

        self.cert = self.get_certificate_path()

        self.console.print(f"[bold]Connecting to server at {self.build_url()}...")
        result = self.request("GET", self.build_url("configuration"))
        result.raise_for_status()

        response = result.json()
        self.leader_id = response["leader"]
        self.follower_ids = response["followers"]

        for receiver_info in receivers():
            receiver = Receiver.open(receiver_info)

            listener = Listener(receiver, partial(self.callback, receiver))
            listener.start()

        table = Table()
        table.add_column("Setting Name")
        table.add_column("Setting Value")

        table.add_row("Server URL", self.build_url())
        table.add_row("Certificate", self.cert)
        table.add_row("Leader Serial", self.leader_id)
        table.add_row("Follower Serials", "\n".join(self.follower_ids))

        self.console.print(table)

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
