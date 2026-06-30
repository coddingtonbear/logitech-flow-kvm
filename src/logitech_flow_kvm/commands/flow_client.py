import json
import os
import random
import string
import subprocess
import sys
import threading
import time
from argparse import ArgumentParser
from functools import partial
from typing import Literal

import logitech_receiver.base as _lr_base
import pyperclip
import requests
import urllib3
from logitech_receiver.device import Device, create_device
from logitech_receiver.receiver import Receiver, create_receiver
from logitech_receiver.base import HIDPPNotification as _HIDPP_Notification
from logitech_receiver.base import receivers, receivers_and_devices
from logitech_receiver.listener import EventsListener
from rich.console import Console
from rich.table import Table
from urllib3.exceptions import InsecureRequestWarning

from .. import constants
from .. import exceptions
from ..util import change_device_host
from ..util import get_host_certificate_path_and_token
from ..util import parse_connection_status
from ..util import set_host_certificate_and_token
from . import LogitechFlowKvmCommand


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()


class BluetoothPoller(threading.Thread):
    """Polls for connect/disconnect events on directly-connected HID devices.

    Complements the receiver-based Listener for devices that are paired directly
    (e.g. via Bluetooth) rather than through a USB receiver. Works on any
    platform where hidapi enumerates directly-connected devices.
    """

    def __init__(self, client: "FlowClient", poll_interval: float = 1.0):
        super().__init__(daemon=True)
        self.client = client
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._connected: dict[str, Device] = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        tracked = {self.client.leader_id, *self.client.follower_ids}
        while not self._stop.wait(self.poll_interval):
            try:
                current: dict[str, Device] = {}
                for device_info in receivers_and_devices():
                    if not device_info.isDevice:
                        continue
                    device = create_device(_lr_base, device_info)
                    if device and device.id in tracked:
                        current[device.id] = device
            except Exception:
                continue  # skip this cycle if enumeration itself fails

            for device_id, device in current.items():
                if device_id not in self._connected:
                    try:
                        self.client._on_device_connected(device)
                    except Exception:
                        pass

            for device_id, device in list(self._connected.items()):
                if device_id not in current:
                    try:
                        self.client._on_device_disconnected(device)
                    except Exception:
                        pass

            self._connected = current


class ServerStatePoller(threading.Thread):
    """Polls the server for leader device host changes to trigger follower switching.

    Local HID disconnect events can be delayed or missed — Bluetooth stacks may
    keep a device enumerated briefly after it switches channels, and
    device_status is empty when there is no USB receiver attached. This poller
    uses the server's authoritative record instead: the remote host updates
    GET /device/<id> as soon as it detects the leader connecting via HID++.

    Only fires when the leader moves AWAY from this host
    (prev_host == this_host, current_host != this_host) so it does not
    double-switch with BluetoothPoller on the inbound direction.
    """

    def __init__(self, client: "FlowClient", poll_interval: float = 1.0):
        super().__init__(daemon=True)
        self.client = client
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._last_host: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                response = self.client.request(
                    "GET", self.client.build_url("device", self.client.leader_id)
                )
                if not response.ok:
                    continue
                current_host = int(response.content)
                prev_host = self._last_host
                self._last_host = current_host

                if (
                    prev_host == self.client.options.host_number
                    and current_host != self.client.options.host_number
                ):
                    self._switch_followers(current_host)
            except Exception:
                pass

    def _switch_followers(self, target_host: int) -> None:
        tracked = set(self.client.follower_ids)
        for device_info in receivers_and_devices():
            if not device_info.isDevice:
                continue
            device = create_device(_lr_base, device_info)
            if device and device.id in tracked:
                self.client.console.print(
                    f"[yellow]Leader moved to host {target_host}, "
                    f"switching follower {device.id}"
                )
                try:
                    change_device_host(device, target_host)
                except Exception as exc:
                    self.client.console.print(
                        f"[red]Failed to switch follower {device.id}: {exc}"
                    )


class FlowClient(LogitechFlowKvmCommand):
    leader_id: str
    follower_ids: list[str]
    cert: str | None = None
    token: str | None = None

    device_status: dict[Receiver | None, dict[Device, int]] = {}

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("server")
        parser.add_argument("--sleep-time", "-s", default=0.25, type=float)
        parser.add_argument("--port", "-p", default=constants.DEFAULT_PORT, type=int)

    def _on_device_connected(self, device: Device) -> None:
        key = device.receiver
        if key not in self.device_status:
            self.device_status[key] = {}
        self.device_status[key][device] = self.options.host_number
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

    def _on_device_disconnected(self, device: Device) -> None:
        self.console.print(f":x: [bold]Device {device.id} disconnected")

        if device.id == self.leader_id:
            clipboard_data = pyperclip.paste()
            clipboard_response = self.request(
                "PUT",
                self.build_url("clipboard"),
                data=clipboard_data.encode("utf-8"),
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

    def callback(self, receiver: Receiver, msg: _HIDPP_Notification) -> None:
        if msg.sub_id == 0x41:
            result = parse_connection_status(msg.data)
            try:
                device = receiver[msg.devnumber]
            except IndexError:
                return

            if result["link_status"] == 0:
                self._on_device_connected(device)
            else:
                self._on_device_disconnected(device)

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

        headers = kwargs.pop("headers", {})
        if self.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.token}"

        return requests.request(method, url, headers=headers, **kwargs)

    def _show_pairing_code_gui(self, code: str) -> None:
        """Show the pairing code in a GUI dialog while POST /pairing is in flight.

        Runs in a background thread so it doesn't block the HTTP request.
        On macOS uses osascript; on Linux tries zenity then kdialog then
        notify-send.
        """
        title = "Logitech Flow KVM — Pairing"

        def _run():
            try:
                if sys.platform == "darwin":
                    # AppleScript uses & return & for newlines in string literals.
                    script = (
                        f'display dialog '
                        f'"Pairing code for {self.options.server}:" & return & return & '
                        f'"    {code}" & return & return & '
                        f'"Enter this into the server dialog." '
                        f'with title "{title}" '
                        f'buttons {{"OK"}} default button "OK" giving up after 120'
                    )
                    subprocess.run(["osascript", "-e", script], timeout=125)
                else:
                    text = (
                        f"Pairing code for {self.options.server}:\n\n"
                        f"    {code}\n\n"
                        f"Enter this into the server dialog."
                    )
                    for cmd in [
                        ["zenity", "--info", f"--title={title}", f"--text={text}"],
                        ["kdialog", "--msgbox", text, "--title", title],
                        ["notify-send", "--urgency=critical", "--expire-time=60000", title, f"Code: {code}"],
                    ]:
                        try:
                            subprocess.run(cmd, timeout=125)
                            break
                        except FileNotFoundError:
                            continue
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def pair(self) -> str:
        urllib3.disable_warnings(InsecureRequestWarning)

        self.console.print(f"[magenta]Pairing with new server {self.options.server}...")
        response = self.request("OPTIONS", self.build_url("pairing"), verify=False)
        if not response.ok:
            raise exceptions.ServerNotAvailable(self.options.server)

        pairing_code = "".join(random.choices(string.digits, k=6))
        self.console.print(
            f"[magenta]Pairing code: [bold][bright_magenta]{pairing_code}"
        )
        self._show_pairing_code_gui(pairing_code)

        response = self.request(
            "POST",
            self.build_url("pairing"),
            verify=False,
            data=json.dumps(
                {"name": self.options.host_number, "pairing_code": pairing_code}
            ),
            headers={"Content-type": "application/json"},
        )
        if not response.ok:
            raise exceptions.PairingFailed()

        response_data = response.json()
        set_host_certificate_and_token(
            self.options.server, response_data["certificate"], response_data["token"]
        )

        return response_data["token"]

    def get_certificate_path_and_token(self) -> tuple[str, str | None]:
        cert_path, token = get_host_certificate_path_and_token(self.options.server)
        if os.path.exists(cert_path):
            try:
                response = self.request(
                    "GET",
                    self.build_url("configuration"),
                    verify=cert_path,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401:
                    raise exceptions.ServerNotPaired()
            except (requests.exceptions.SSLError, exceptions.ServerNotPaired):
                token = self.pair()
            except requests.exceptions.RequestException:
                raise exceptions.ServerNotAvailable()
        else:
            token = self.pair()

        return cert_path, token

    def handle(self):
        self.console = Console()

        self.cert, self.token = self.get_certificate_path_and_token()

        self.console.print(f"[bold]Connecting to server at {self.build_url()}...")
        result = self.request("GET", self.build_url("configuration"))
        result.raise_for_status()

        response = result.json()
        self.leader_id = response["leader"]
        self.follower_ids = response["followers"]

        for receiver_info in receivers():
            receiver = create_receiver(_lr_base, receiver_info)
            if receiver:
                listener = Listener(receiver, partial(self.callback, receiver))
                listener.start()

        bt_poller = BluetoothPoller(self)
        bt_poller.start()

        server_poller = ServerStatePoller(self)
        server_poller.start()

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
            bt_poller.stop()
            server_poller.stop()
