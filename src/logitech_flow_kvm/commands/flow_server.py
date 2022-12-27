from argparse import ArgumentParser
from functools import partial

import pyperclip
from flask import Flask
from flask import abort
from flask import request
from logitech_receiver import Device
from logitech_receiver import Receiver
from logitech_receiver.base import _HIDPP_Notification
from logitech_receiver.listener import EventsListener
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .. import constants
from ..util import change_device_host
from ..util import get_certificate_key_path
from ..util import get_device_by_id
from ..util import parse_connection_status
from . import LogitechFlowKvmCommand


class Listener(EventsListener):
    def has_started(self):
        self.receiver.enable_connection_notifications()
        self.receiver.notify_devices()


class UnknownDevice(Exception):
    pass


class FlowServerAPI(Flask):
    host_number: int

    listeners: list[Listener]
    leader_device: Device
    follower_devices: list[Device]

    device_status: dict[Receiver, dict[Device, int]] = {}

    console: Console = Console()

    def __init__(
        self,
        *args,
        host_number: int,
        leader_device: Device,
        follower_devices: list[Device],
        **kwargs,
    ):
        self.host_number = host_number
        self.leader_device = leader_device
        self.follower_devices = follower_devices

        # Listen to change events for all relevant devices
        self.listeners = [
            Listener(
                self.leader_device.receiver,
                partial(self.callback, self.leader_device.receiver),
            )
        ]
        for follower_device in self.follower_devices:
            if follower_device.receiver not in [
                listener.receiver for listener in self.listeners
            ]:
                self.listeners.append(
                    Listener(
                        follower_device.receiver,
                        partial(self.callback, follower_device.receiver),
                    )
                )

        for listener in self.listeners:
            listener.start()

        super().__init__(*args, **kwargs)

    def callback(self, receiver: Receiver, msg: _HIDPP_Notification) -> None:
        if msg.sub_id == 0x41:
            result = parse_connection_status(msg.data)

            if receiver not in self.device_status:
                self.device_status[receiver] = {}

            try:
                device = receiver[msg.devnumber]
            except IndexError:
                return

            if device.id not in [
                self.leader_device.id,
                *[follower_device.id for follower_device in self.follower_devices],
            ]:
                return

            if result["link_status"] == 0:
                self.device_status[receiver][device] = self.host_number
                self.console.print(
                    f":white_heavy_check_mark: [bold]Device {device.id} connected"
                )
            else:
                self.console.print(f":x: [bold]Device {device.id} disconnected")

    def remote_device_status_change(self, id: str, new_host: int) -> None:
        found_device = False

        for devices in self.device_status.values():
            for device in devices.keys():
                if device.id == id:
                    devices[device] = int(request.data)
                    found_device = True

        if not found_device:
            raise UnknownDevice()

        if self.leader_device.id == id:
            for device in self.follower_devices:
                self.console.print(f"Asking follower {device} to switch to {new_host}")
                change_device_host(device, new_host)


def bind_routes(app: FlowServerAPI) -> None:
    @app.get("/configuration")
    def configuration():
        response: dict = {}

        response["leader"] = app.leader_device.id
        response["followers"] = [device.id for device in app.follower_devices]

        return response

    @app.get("/device")
    def device_status():
        response: dict = {}

        for devices in app.device_status.values():
            for device, status in devices.items():
                response[device.id] = status

        return response

    @app.route("/device/<id>", methods=["GET", "PUT"])
    def device_status_detail(id: str):
        if request.method == "GET":
            for devices in app.device_status.values():
                for device, status in devices.items():
                    if device.id == id:
                        return str(status)
            abort(404)
        elif request.method == "PUT":
            try:
                app.remote_device_status_change(id, int(request.data))
                return ""
            except UnknownDevice:
                abort(404)
        abort(405)

    @app.route("/pairing", methods=["POST"])
    def pair():
        console = Console()

        console.print(
            f"[magenta]Received pairing request from {request.remote_addr}; "
            "a pairing code has been printed to the console running `flow-client` "
            "enter that code below to complete the pairing process."
        )
        typed_pairing_code = Prompt.ask("[bright_magenta]Pairing code")

        if (
            typed_pairing_code.strip().upper()
            == request.data.decode("utf-8").strip().upper()
        ):
            console.print("[magenta]Paired successfully")
            cert_path, _ = get_certificate_key_path("server", create=True)

            with open(cert_path, "r") as inf:
                return inf.read()

        console.print("[red][bold]Pairing code did not match; pairing failed!")
        abort(401)

    @app.route("/clipboard", methods=["PUT", "GET"])
    def clipboard():
        console = Console()

        if request.method == "GET":
            return pyperclip.paste()
        elif request.method == "PUT":
            pyperclip.copy(request.data.decode("utf-8"))
            console.print(
                f"Clipboard set from client with {len(request.data)} bytes of data"
            )
            return ""

        abort(405)


class FlowServer(LogitechFlowKvmCommand):
    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("leader_device")
        parser.add_argument("follower_devices", nargs="+")
        parser.add_argument("--binding-interface", "-b", default="0.0.0.0", type=str)
        parser.add_argument("--port", "-p", default=constants.DEFAULT_PORT, type=int)

    def handle(self) -> None:
        leader_device = get_device_by_id(self.options.leader_device)
        follower_devices = []
        for device in self.options.follower_devices:
            follower_devices.append(get_device_by_id(device))

        cert_path, key_path = get_certificate_key_path("server", create=True)

        console = Console()

        table = Table()
        table.add_column("Setting Name")
        table.add_column("Setting Value")

        table.add_row("Leader", str(get_device_by_id(self.options.leader_device)))
        table.add_row(
            "Followers",
            "\n".join(
                [str(get_device_by_id(dev)) for dev in self.options.follower_devices]
            ),
        )
        table.add_row("Certificate", cert_path)
        table.add_row("Key", key_path)
        table.add_row("Binding Interface", self.options.binding_interface)
        table.add_row("Port", str(self.options.port))

        console.print(table)

        console.print("Press [red]CTRL+C[/red] to exit")

        app = FlowServerAPI(
            __name__,
            host_number=self.options.host_number,
            leader_device=leader_device,
            follower_devices=follower_devices,
        )

        bind_routes(app)

        try:
            app.run(
                port=self.options.port,
                host=self.options.binding_interface,
                ssl_context=(cert_path, key_path),
            )
        except KeyboardInterrupt:
            pass
