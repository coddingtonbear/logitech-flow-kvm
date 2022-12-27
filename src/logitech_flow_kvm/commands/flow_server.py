from argparse import ArgumentParser
from functools import partial

from flask import Flask, request, abort
from logitech_receiver import Device, Receiver
from logitech_receiver.base import _HIDPP_Notification
from logitech_receiver.listener import EventsListener
from rich.console import Console

from . import LogitechFlowKvmCommand
from ..util import get_device_by_id, change_device_host, parse_connection_status


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

            device = receiver[msg.devnumber]
            if not device.id in [
                self.leader_device.id,
                *[follower_device.id for follower_device in self.follower_devices],
            ]:
                return

            if result["link_status"] == 0:
                self.device_status[receiver][device] = self.host_number

    def remote_device_status_change(self, id: str, new_host: int) -> None:
        for devices in self.device_status.values():
            for device in devices.keys():
                if device.id == id:
                    devices[device] = int(request.data)
                    return

        if self.leader_device.id == id:
            for device in self.follower_devices:
                change_device_host(device, new_host)

        raise UnknownDevice()


def bind_routes(app: FlowServerAPI) -> None:
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
                        return status
            abort(404)
        elif request.method == "PUT":
            try:
                app.remote_device_status_change(id, int(request.data))
                return ""
            except UnknownDevice:
                abort(404)
        abort(405)


class FlowServer(LogitechFlowKvmCommand):
    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("leader_device")
        parser.add_argument("follower_devices", nargs="+")
        parser.add_argument("--port", "-p", default=24801, type=int)

    def handle(self) -> None:
        leader_device = get_device_by_id(self.options.leader_device)
        follower_devices = []
        for device in self.options.follower_devices:
            follower_devices.append(get_device_by_id(device))

        console = Console()
        console.print(f"Following [italic]{self.options.leader_device}[/italic]")
        console.print("[bold]Press CTRL+C to exit")

        app = FlowServerAPI(
            __name__,
            host_number=self.options.host_number,
            leader_device=leader_device,
            follower_devices=follower_devices,
        )

        bind_routes(app)

        try:
            app.run(port=self.options.port)
        except KeyboardInterrupt:
            pass
