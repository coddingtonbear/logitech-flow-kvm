import json
import logging
import os
import random
import string
import sys
import threading
import time
from argparse import ArgumentParser
from functools import partial
from typing import Literal

import pyperclip
import requests
import urllib3
from rich.progress import Progress
from urllib3.exceptions import InsecureRequestWarning

from .. import constants
from .. import exceptions
from ..hidpp import Notification
from ..hidpp import NotificationListener
from ..hidpp import PairedDevice
from ..hidpp import Receiver
from ..hidpp import find_receivers
from ..reconciler import Reconciler
from ..sse import parse_sse_stream
from ..tui import ClientStatus
from ..tui import DeviceStatus
from ..tui import FlowTUIApp
from ..tui import render_client_status
from ..util import get_host_certificate_path_and_token
from ..util import get_theoretical_max_device_count
from ..util import parse_connection_status
from ..util import set_host_certificate_and_token
from . import LogitechFlowKvmCommand

logger = logging.getLogger(__name__)

# Backoff for reconnecting the /events stream after it drops.
EVENTS_MIN_BACKOFF = 1.0
EVENTS_MAX_BACKOFF = 30.0


class FlowClient(LogitechFlowKvmCommand):
    leader_id: str
    follower_ids: list[str]
    cert: str | None = None
    token: str | None = None

    follower_devices: list[PairedDevice]
    local_receivers: list[Receiver]
    reconciler: Reconciler
    # The leader's last-known host, as reported over the server's /events
    # stream. `None` until the first event arrives (or the stream's initial,
    # atomic snapshot -- see `sse.EventBroadcaster.subscribe`).
    leader_host: int | None = None

    # Set once (if ever) a Textual UI is running -- `None` when running
    # non-interactively, in which case status updates are simply skipped.
    tui: FlowTUIApp | None = None
    _connected_to_server: bool = False

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("server")
        parser.add_argument("--port", "-p", default=constants.DEFAULT_PORT, type=int)

    def callback(self, receiver: Receiver, notification: Notification) -> None:
        if notification.sub_id != 0x41:
            return

        device = receiver.get_device(notification.devnumber)
        if device is None:
            return

        result = parse_connection_status(notification.data)
        connected = result["link_status"] == 0
        is_leader = device.id == self.leader_id

        if connected:
            logger.info("Device %s connected", device.id)
        else:
            logger.info("Device %s disconnected", device.id)

        if not is_leader:
            self.reconciler.observe(device, connected)

        if connected:
            if is_leader:
                # Positive evidence: the leader is here. Report it so every
                # client (including this one) learns to converge followers
                # toward this host.
                response = self.request(
                    "PUT",
                    self.build_url("leader-host"),
                    data=str(self.options.host_number),
                )
                response.raise_for_status()

            clipboard_response = self.request("GET", self.build_url("clipboard"))
            if clipboard_response.ok:
                pyperclip.copy(clipboard_response.text)
        elif is_leader:
            clipboard_data = pyperclip.paste()
            clipboard_response = self.request(
                "PUT",
                self.build_url("clipboard"),
                data=clipboard_data.encode("utf-8"),
            )
            if clipboard_response.ok:
                logger.info(
                    "Clipboard contents set on server with %d bytes of data",
                    len(clipboard_data),
                )

        self._publish_status()

    def _reconciler_error(self, device: PairedDevice, error: Exception) -> None:
        logger.warning(
            "Could not switch %s to the desired host yet (%s); will retry",
            device.id,
            error,
        )

    def _handle_event(self, event_type: str, data: str) -> None:
        if event_type == "leader-host":
            self.leader_host = int(data)
            self.reconciler.poke()
            self._publish_status()
        elif event_type == "host-connected":
            logger.info("Host %s connected", data)

    def _build_status(self) -> ClientStatus:
        return ClientStatus(
            host_number=self.options.host_number,
            server=self.options.server,
            connected_to_server=self._connected_to_server,
            leader_host=self.leader_host,
            followers=[
                DeviceStatus(
                    id=device.id,
                    label=device.codename or device.kind,
                    connected=self.reconciler._connected.get(device, False),
                )
                for device in self.follower_devices
            ],
        )

    def _publish_status(self) -> None:
        if self.tui is not None:
            self.tui.update_status(render_client_status(self._build_status()))

    def _consume_events(self) -> None:
        backoff = EVENTS_MIN_BACKOFF
        while not self._stop.is_set():
            try:
                response = self.request(
                    "GET", self.build_url("events"), stream=True, timeout=(10, None)
                )
                response.raise_for_status()
                # Re-announce our own devices' current status as a side
                # effect of (re)establishing this connection, so the server
                # recovers cross-client state (e.g. after a restart) at the
                # same moment we're asking it for its current state.
                for receiver in self.local_receivers:
                    receiver.notify_devices()
                backoff = EVENTS_MIN_BACKOFF
                self._connected_to_server = True
                self._publish_status()
                for event_type, data in parse_sse_stream(
                    response.iter_lines(decode_unicode=True)
                ):
                    self._handle_event(event_type, data)
            except requests.exceptions.RequestException:
                pass
            if self._connected_to_server:
                self._connected_to_server = False
                self._publish_status()
            if self._stop.is_set():
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, EVENTS_MAX_BACKOFF)

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

    def pair(self) -> str:
        urllib3.disable_warnings(InsecureRequestWarning)

        logger.info("Pairing with new server %s...", self.options.server)
        response = self.request("OPTIONS", self.build_url("pairing"), verify=False)
        if not response.ok:
            raise exceptions.ServerNotAvailable(self.options.server)

        pairing_code = "".join(random.choices(string.digits, k=6))
        logger.info("Pairing code: %s", pairing_code)
        logger.info(
            "To complete the pairing process, enter the above code into the "
            "server console running `flow-server` when requested."
        )

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
            except requests.exceptions.RequestException as e:
                raise exceptions.ServerNotAvailable() from e
        else:
            token = self.pair()

        return cert_path, token

    def handle(self):
        self.cert, self.token = self.get_certificate_path_and_token()

        logger.info("Connecting to server at %s...", self.build_url())
        result = self.request("GET", self.build_url("configuration"))
        result.raise_for_status()

        response = result.json()
        self.leader_id = response["leader"]
        self.follower_ids = response["followers"]

        device_id_map: dict[str, PairedDevice | None] = {
            follower: None for follower in self.follower_ids
        }
        self.local_receivers = []

        with Progress(transient=True) as progress:
            enumerate_task = progress.add_task(
                "Finding devices...", total=get_theoretical_max_device_count()
            )
            for info in find_receivers():
                receiver = Receiver(info)
                self.local_receivers.append(receiver)
                for device in receiver.enumerate_devices():
                    if device.serial in device_id_map:
                        device_id_map[device.serial] = device
                progress.advance(enumerate_task, receiver.max_devices)

        self.follower_devices = []
        for follower_id, found_device in device_id_map.items():
            if found_device is None:
                raise exceptions.DeviceNotFound(follower_id)
            self.follower_devices.append(found_device)

        self.reconciler = Reconciler(
            self.follower_devices,
            get_desired_host=lambda: self.leader_host,
            host_number=self.options.host_number,
            on_error=self._reconciler_error,
        )

        self._stop = threading.Event()

        logger.info("Server URL: %s", self.build_url())
        logger.info("Certificate: %s", self.cert)
        logger.info("Leader serial: %s", self.leader_id)
        logger.info("Follower serials: %s", ", ".join(self.follower_ids))

        if sys.stdout.isatty():

            def on_start(tui: FlowTUIApp) -> None:
                self.tui = tui
                self.start_background_threads()

            # Textual owns the main thread's event loop from here; Ctrl+C
            # is handled internally as a quit keybinding, not a raised
            # KeyboardInterrupt.
            FlowTUIApp("flow-client", on_start=on_start).run()
            self._stop.set()
            self.reconciler.stop()
        else:
            self.start_background_threads()
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self._stop.set()
                self.reconciler.stop()

    def start_background_threads(self) -> None:
        """Start the reconciler, notification listeners, and the /events
        consumer.

        Deliberately not done inline in `handle()`: `callback()`/
        `_handle_event()`/`_consume_events()` may call
        `self.tui.update_status(...)`, which requires the TUI's event loop
        to already be running -- so when interactive, this is called from
        `FlowTUIApp.on_mount` instead.
        """
        self.reconciler.start()

        for receiver in self.local_receivers:
            receiver.enable_connection_notifications()
            listener = NotificationListener(
                receiver.path, partial(self.callback, receiver)
            )
            listener.start()

        events_thread = threading.Thread(target=self._consume_events, daemon=True)
        events_thread.start()
