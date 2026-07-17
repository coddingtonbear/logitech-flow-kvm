import os
import queue
import sqlite3
import uuid
from argparse import ArgumentParser
from functools import partial

import platformdirs
import pyperclip
from flask import Flask
from flask import Response
from flask import abort
from flask import request
from flask_httpauth import HTTPTokenAuth
from rich.console import Console
from rich.progress import Progress
from rich.prompt import Prompt
from rich.table import Table

from .. import constants
from .. import exceptions
from ..hidpp import Notification
from ..hidpp import NotificationListener
from ..hidpp import PairedDevice
from ..hidpp import Receiver
from ..reconciler import Reconciler
from ..sse import EventBroadcaster
from ..sse import format_sse
from ..util import get_certificate_key_path
from ..util import get_devices
from ..util import get_theoretical_max_device_count
from ..util import parse_connection_status
from . import LogitechFlowKvmCommand

# How long an /events subscriber's connection can sit idle before we send a
# keepalive comment -- long enough to be cheap, short enough that a dead TCP
# connection (the client vanished without a clean close) gets noticed and its
# queue cleaned up promptly rather than leaking forever.
KEEPALIVE_INTERVAL = 15.0


class FlowServerAPI(Flask):
    host_number: int

    listeners: list[NotificationListener]
    leader_device: PairedDevice
    follower_devices: list[PairedDevice]
    hostnames: list[str]

    # The one piece of state followers everywhere care about: which host the
    # leader is currently on. Updated only from positive evidence (a connect
    # notification, seen either directly here or reported by a client via
    # `PUT /leader-host`), broadcast to every subscribed client over SSE, and
    # fed to `reconciler` to drive this host's own local followers.
    events: EventBroadcaster
    reconciler: Reconciler

    console: Console = Console()

    db: sqlite3.Connection

    def __init__(
        self,
        *args,
        host_number: int,
        leader_device: PairedDevice,
        follower_devices: list[PairedDevice],
        hostnames: list[str],
        **kwargs,
    ):
        self.host_number = host_number
        self.leader_device = leader_device
        self.follower_devices = follower_devices
        self.hostnames = hostnames

        self.events = EventBroadcaster()
        self.reconciler = Reconciler(
            follower_devices,
            get_desired_host=self._get_desired_host,
            host_number=host_number,
        )

        # Listen to change events for all relevant devices, one listener per
        # distinct receiver (leader and followers may share a receiver).
        self.listeners = []
        seen_receivers: list[Receiver] = []
        for device in (self.leader_device, *self.follower_devices):
            if device.receiver in seen_receivers:
                continue
            seen_receivers.append(device.receiver)
            device.receiver.enable_connection_notifications()
            device.receiver.notify_devices()
            self.listeners.append(
                NotificationListener(
                    device.receiver.path, partial(self.callback, device.receiver)
                )
            )

        for listener in self.listeners:
            listener.start()
        self.reconciler.start()

        user_data_dir = platformdirs.user_data_dir(
            constants.APP_NAME, constants.APP_AUTHOR
        )
        os.makedirs(user_data_dir, exist_ok=True)

        self.db = sqlite3.Connection(
            os.path.join(user_data_dir, "tokens.db"), check_same_thread=False
        )
        self.migrate_db()

        super().__init__(*args, **kwargs)

    def _get_desired_host(self) -> int | None:
        state = self.events.state
        return int(state) if state is not None else None

    def migrate_db(self) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                name string primary key,
                token string
            );
        """
        )
        self.db.commit()
        cursor.close()

    def create_new_auth_token(self, name: str) -> str:
        cursor = self.db.cursor()

        new_token = str(uuid.uuid4())
        cursor.execute(
            """
            INSERT INTO tokens (name, token)
            VALUES (?, ?)
            ON CONFLICT (name) DO UPDATE SET
                token=excluded.token
            ;
        """,
            (name, new_token),
        )

        self.db.commit()
        cursor.close()

        return new_token

    def verify_auth_token(self, token: str) -> bool:
        cursor = self.db.cursor()

        cursor.execute("""SELECT name FROM tokens WHERE token = ?""", (token,))

        results = cursor.fetchall()

        if len(results) > 0:
            # Return the username (probably a host index)
            return results[0][0]

        return False

    def callback(self, receiver: Receiver, notification: Notification) -> None:
        if notification.sub_id != 0x41:
            return

        device = next(
            (
                d
                for d in (self.leader_device, *self.follower_devices)
                if d.receiver is receiver and d.number == notification.devnumber
            ),
            None,
        )
        if device is None:
            return

        result = parse_connection_status(notification.data)
        connected = result["link_status"] == 0

        if device is self.leader_device:
            if connected:
                self.console.print(
                    f":white_heavy_check_mark: [bold]Device {device.id} connected"
                )
                self.report_leader_host(self.host_number)
            else:
                self.console.print(f":x: [bold]Device {device.id} disconnected")
        else:
            self.reconciler.observe(device, connected)
            if connected:
                self.console.print(
                    f":white_heavy_check_mark: [bold]Device {device.id} connected"
                )
            else:
                self.console.print(f":x: [bold]Device {device.id} disconnected")

    def report_leader_host(self, new_host: int) -> None:
        """Record positive evidence that the leader is now on `new_host`."""
        self.events.set_state("leader-host", str(new_host))
        self.reconciler.poke()


def bind_routes(app: FlowServerAPI) -> None:
    auth = HTTPTokenAuth(scheme="Bearer")

    @auth.verify_token
    def verify_token(token: str):
        return app.verify_auth_token(token)

    @app.route("/pairing", methods=["POST"])
    def pair():
        console = Console()

        console.print(
            f"[magenta]Received pairing request from {request.remote_addr}; "
            "a pairing code has been printed to the console running `flow-client` "
            "enter that code below to complete the pairing process."
        )
        typed_pairing_code = Prompt.ask("[bright_magenta]Pairing code")

        request_data = request.json

        if typed_pairing_code.strip().upper() == request_data["pairing_code"].upper():
            console.print("[magenta]Paired successfully")
            cert_path, _ = get_certificate_key_path(
                "server", create=True, hostnames=app.hostnames
            )

            response_data: dict = {
                "token": app.create_new_auth_token(request_data["name"])
            }

            with open(cert_path) as inf:
                response_data["certificate"] = inf.read()

            return response_data

        console.print("[red][bold]Pairing code did not match; pairing failed!")
        abort(401)

    @app.get("/configuration")
    @auth.login_required
    def configuration():
        response: dict = {}

        response["leader"] = app.leader_device.id
        response["followers"] = [device.id for device in app.follower_devices]

        return response

    @app.route("/leader-host", methods=["PUT"])
    @auth.login_required
    def leader_host():
        app.report_leader_host(int(request.data))
        return ""

    @app.route("/events")
    @auth.login_required
    def events():
        connecting_host = auth.current_user()
        subscriber_queue, current = app.events.subscribe()
        app.console.print(f"[cyan]Host {connecting_host} connected")
        app.events.broadcast(
            "host-connected", str(connecting_host), exclude=subscriber_queue
        )

        def stream():
            try:
                if current is not None:
                    yield format_sse("leader-host", current)
                while True:
                    try:
                        yield subscriber_queue.get(timeout=KEEPALIVE_INTERVAL)
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                app.events.unsubscribe(subscriber_queue)

        return Response(stream(), mimetype="text/event-stream")

    @app.route("/clipboard", methods=["PUT", "GET"])
    @auth.login_required
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
        parser.add_argument(
            "--hostname",
            "-H",
            action="append",
            default=[],
            help=(
                "Hostname clients will use to reach this server, in addition to its "
                "IP addresses (which are always included automatically). May be "
                "given more than once. Required if clients connect by hostname "
                "rather than IP, since the certificate must list every name it's "
                "presented as."
            ),
        )

    def handle(self) -> None:
        device_id_map: dict[str, PairedDevice | None] = {
            self.options.leader_device: None,
            **{follower: None for follower in self.options.follower_devices},
        }
        with Progress(transient=True) as progress:
            enumerate_task = progress.add_task(
                "Finding devices...", total=get_theoretical_max_device_count()
            )

            for possible_device in get_devices():
                progress.advance(enumerate_task)
                if possible_device is not None:
                    if possible_device.serial in device_id_map:
                        device_id_map[possible_device.serial] = possible_device

                if None not in device_id_map.values():
                    break

        found_devices: dict[str, PairedDevice] = {}
        for device_id, found_device in device_id_map.items():
            if found_device is None:
                raise exceptions.DeviceNotFound(device_id)
            found_devices[device_id] = found_device

        leader_device = found_devices[self.options.leader_device]
        follower_devices = []
        for device in self.options.follower_devices:
            follower_devices.append(found_devices[device])

        cert_path, key_path = get_certificate_key_path(
            "server", create=True, hostnames=self.options.hostname
        )

        console = Console()

        table = Table()
        table.add_column("Setting Name")
        table.add_column("Setting Value")

        table.add_row("Leader", str(self.options.leader_device))
        table.add_row(
            "Followers",
            "\n".join([str(dev) for dev in self.options.follower_devices]),
        )
        table.add_row("Certificate", cert_path)
        table.add_row("Key", key_path)
        table.add_row("Binding Interface", self.options.binding_interface)
        table.add_row("Port", str(self.options.port))
        if self.options.hostname:
            table.add_row("Hostnames", "\n".join(self.options.hostname))

        console.print(table)

        console.print("Press [red]CTRL+C[/red] to exit")

        app = FlowServerAPI(
            __name__,
            host_number=self.options.host_number,
            leader_device=leader_device,
            follower_devices=follower_devices,
            hostnames=self.options.hostname,
        )

        bind_routes(app)

        try:
            app.run(
                port=self.options.port,
                host=self.options.binding_interface,
                ssl_context=(cert_path, key_path),
                threaded=True,
            )
        except KeyboardInterrupt:
            pass
