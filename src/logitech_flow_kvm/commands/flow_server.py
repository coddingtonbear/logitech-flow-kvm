import logging
import os
import queue
import sqlite3
import sys
import threading
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
from rich.progress import Progress
from rich.prompt import Prompt

from .. import constants
from .. import exceptions
from ..hidpp import Notification
from ..hidpp import NotificationListener
from ..hidpp import PairedDevice
from ..hidpp import Receiver
from ..reconciler import Reconciler
from ..sse import EventBroadcaster
from ..sse import format_sse
from ..tui import DeviceStatus
from ..tui import FlowTUIApp
from ..tui import ServerStatus
from ..tui import render_server_status
from ..util import get_certificate_key_path
from ..util import get_devices
from ..util import get_theoretical_max_device_count
from ..util import parse_connection_status
from . import LogitechFlowKvmCommand

logger = logging.getLogger(__name__)

# How long an /events subscriber's connection can sit idle before we send a
# keepalive comment -- long enough to be cheap, short enough that a dead TCP
# connection (the client vanished without a clean close) gets noticed and its
# queue cleaned up promptly rather than leaking forever.
KEEPALIVE_INTERVAL = 15.0


class FlowServerAPI(Flask):
    host_number: int
    binding_interface: str
    port: int
    clipboard_enabled: bool

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

    db: sqlite3.Connection

    # `threaded=True` means concurrent /pairing requests would otherwise run
    # their Prompt.ask() calls on the same console at once, interleaving
    # prompts and input across unrelated pairing attempts. This forces them
    # to queue up one at a time instead.
    pairing_lock: threading.Lock

    # Set once (if ever) a Textual UI is running -- `None` when running
    # non-interactively, in which case status updates are simply skipped.
    tui: FlowTUIApp | None = None

    def __init__(
        self,
        *args,
        host_number: int,
        leader_device: PairedDevice,
        follower_devices: list[PairedDevice],
        hostnames: list[str],
        binding_interface: str,
        port: int,
        clipboard_enabled: bool = True,
        **kwargs,
    ):
        self.host_number = host_number
        self.leader_device = leader_device
        self.follower_devices = follower_devices
        self.hostnames = hostnames
        self.binding_interface = binding_interface
        self.port = port
        self.clipboard_enabled = clipboard_enabled

        self._leader_connected = False

        self.pairing_lock = threading.Lock()

        self.events = EventBroadcaster()
        self.reconciler = Reconciler(
            follower_devices,
            get_desired_host=self._get_desired_host,
            host_number=host_number,
            on_error=self._reconciler_error,
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

        user_data_dir = platformdirs.user_data_dir(
            constants.APP_NAME, constants.APP_AUTHOR
        )
        os.makedirs(user_data_dir, exist_ok=True)

        self.db = sqlite3.Connection(
            os.path.join(user_data_dir, "tokens.db"), check_same_thread=False
        )
        self.migrate_db()

        super().__init__(*args, **kwargs)

    def start_background_threads(self) -> None:
        """Start the reconciler and notification listeners.

        Deliberately not done in `__init__`: `callback()`/`report_leader_host()`
        may call `self.tui.update_status(...)`, which requires the TUI's event
        loop to already be running -- so when interactive, this is called from
        `FlowTUIApp.on_mount` instead of right after construction.
        """
        for listener in self.listeners:
            listener.start()
        self.reconciler.start()

    def _get_desired_host(self) -> int | None:
        state = self.events.state
        return int(state) if state is not None else None

    def _build_status(self) -> ServerStatus:
        def device_status(device: PairedDevice, connected: bool) -> DeviceStatus:
            return DeviceStatus(
                id=device.id, label=device.codename or device.kind, connected=connected
            )

        return ServerStatus(
            host_number=self.host_number,
            binding_interface=self.binding_interface,
            port=self.port,
            hostnames=self.hostnames,
            leader=device_status(self.leader_device, self._leader_connected),
            followers=[
                device_status(device, self.reconciler._connected.get(device, False))
                for device in self.follower_devices
            ],
            desired_host=self._get_desired_host(),
            connected_guests=self.events.subscriber_names,
        )

    def _publish_status(self) -> None:
        if self.tui is not None:
            self.tui.update_status(render_server_status(self._build_status()))

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
            self._leader_connected = connected
            if connected:
                logger.info("Device %s connected", device.id)
                self.report_leader_host(self.host_number)
            else:
                logger.info("Device %s disconnected", device.id)
                self._publish_status()
        else:
            self.reconciler.observe(device, connected)
            if connected:
                logger.info("Device %s connected", device.id)
            else:
                logger.info("Device %s disconnected", device.id)
            self._publish_status()

    def report_leader_host(self, new_host: int) -> None:
        """Record positive evidence that the leader is now on `new_host`."""
        self.events.set_state("leader-host", str(new_host))
        self.reconciler.poke()
        self._publish_status()

    def _reconciler_error(self, device: PairedDevice, error: Exception) -> None:
        logger.warning(
            "Could not switch %s to the desired host yet (%s); will retry",
            device.id,
            error,
        )


def bind_routes(app: FlowServerAPI) -> None:
    auth = HTTPTokenAuth(scheme="Bearer")

    @auth.verify_token
    def verify_token(token: str):
        return app.verify_auth_token(token)

    @app.route("/pairing", methods=["POST"])
    def pair():
        # Serialized: two pairing attempts running at once would interleave
        # either their Prompt.ask() calls on the same stdin/stdout, or their
        # pairing modals on the same TUI.
        with app.pairing_lock:
            logger.info(
                "Received pairing request from %s; a pairing code has been "
                "printed to the console running `flow-client`; enter that "
                "code below to complete the pairing process.",
                request.remote_addr,
            )
            if app.tui is not None:
                typed_pairing_code = app.tui.request_pairing_code(
                    request.remote_addr or "unknown"
                )
            else:
                typed_pairing_code = Prompt.ask("Pairing code")

            request_data = request.json

            if (
                typed_pairing_code is not None
                and typed_pairing_code.strip().upper()
                == request_data["pairing_code"].upper()
            ):
                logger.info("Paired successfully")
                cert_path, _ = get_certificate_key_path(
                    "server", create=True, hostnames=app.hostnames
                )

                response_data: dict = {
                    "token": app.create_new_auth_token(request_data["name"])
                }

                with open(cert_path) as inf:
                    response_data["certificate"] = inf.read()

                return response_data

            logger.warning("Pairing code did not match; pairing failed")
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
        connecting_host = str(auth.current_user())
        subscriber_queue, current = app.events.subscribe(name=connecting_host)
        logger.info("Host %s connected", connecting_host)
        app.events.broadcast(
            "host-connected", connecting_host, exclude=subscriber_queue
        )
        app._publish_status()

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
                app._publish_status()

        return Response(stream(), mimetype="text/event-stream")

    @app.route("/clipboard", methods=["PUT", "GET"])
    @auth.login_required
    def clipboard():
        if not app.clipboard_enabled:
            abort(404)

        if request.method == "GET":
            return pyperclip.paste()
        elif request.method == "PUT":
            pyperclip.copy(request.data.decode("utf-8"))
            logger.info(
                "Clipboard set from client with %d bytes of data", len(request.data)
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
            "--no-clipboard",
            action="store_true",
            help=(
                "Disable clipboard synchronization for this host. This host's "
                "clipboard will neither be read nor written, and the "
                "/clipboard endpoint will be unavailable to clients."
            ),
        )
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

        logger.info("Leader: %s", self.options.leader_device)
        logger.info("Followers: %s", ", ".join(self.options.follower_devices))
        logger.info("Certificate: %s", cert_path)
        logger.info("Key: %s", key_path)
        logger.info("Binding interface: %s", self.options.binding_interface)
        logger.info("Port: %s", self.options.port)
        if self.options.hostname:
            logger.info("Hostnames: %s", ", ".join(self.options.hostname))
        if self.options.no_clipboard:
            logger.info("Clipboard synchronization: disabled")

        app = FlowServerAPI(
            __name__,
            host_number=self.options.host_number,
            leader_device=leader_device,
            follower_devices=follower_devices,
            hostnames=self.options.hostname,
            binding_interface=self.options.binding_interface,
            port=self.options.port,
            clipboard_enabled=not self.options.no_clipboard,
        )

        bind_routes(app)

        def run_flask() -> None:
            app.run(
                port=self.options.port,
                host=self.options.binding_interface,
                ssl_context=(cert_path, key_path),
                threaded=True,
            )

        if sys.stdout.isatty():

            def on_start(tui: FlowTUIApp) -> None:
                app.tui = tui
                app.start_background_threads()
                threading.Thread(target=run_flask, daemon=True).start()

            # Textual owns the main thread's event loop from here; Ctrl+C
            # is handled internally as a quit keybinding, not a raised
            # KeyboardInterrupt.
            FlowTUIApp("flow-server", on_start=on_start).run()
        else:
            logger.info("Press CTRL+C to exit")
            app.start_background_threads()
            try:
                run_flask()
            except KeyboardInterrupt:
                pass
