import json
import logging
import os
import queue
import random
import string
import sys
import threading
import time
from argparse import ArgumentParser
from collections.abc import Callable
from typing import Literal

import pyperclip
import requests
import urllib3
from rich.progress import Progress
from urllib3.exceptions import InsecureRequestWarning

from .. import constants
from .. import exceptions
from ..hidpp import Notification
from ..hidpp import PairedDevice
from ..hidpp import Receiver
from ..hidpp import ReceiverManager
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
from ..util import resolve_devices
from ..util import set_host_certificate_and_token
from . import LogitechFlowKvmCommand

logger = logging.getLogger(__name__)

# Backoff for reconnecting the /events stream after it drops. The maximum is
# deliberately short: while the stream is down this host stops acting on the
# leader's whereabouts (see `_get_desired_host`), and the reconnect is what
# ends that. A failed connect to a host that isn't there costs almost
# nothing, so there's no reason to make the user wait out a long backoff
# once the server does come back.
EVENTS_MIN_BACKOFF = 1.0
EVENTS_MAX_BACKOFF = 5.0

# How long after the /events stream drops we keep acting on the last-known
# leader host. Short outages (a wifi blip, a server restart) are common and
# self-healing, and freezing instantly would stall a switch the user just
# asked for; a host that's genuinely gone stays gone for far longer than
# this.
SERVER_GRACE_PERIOD = 10.0

# Default (connect, read) timeout for ordinary request/response calls.
# Without one, `requests` waits forever -- so a network change that
# black-holes an established connection would hang whichever thread made
# the call (often the notification listener) until process restart.
DEFAULT_HTTP_TIMEOUT = (5.0, 10.0)

# The /events stream idles between events, but the server emits a keepalive
# comment every 15s (`flow_server.KEEPALIVE_INTERVAL`), so a healthy
# connection never goes this long without bytes. A read that does is a dead
# connection; timing out hands control back to `_consume_events`'s
# backoff/reconnect loop instead of blocking until TCP gives up.
EVENTS_READ_TIMEOUT = 45.0

# POST /pairing blocks until a human types the pairing code into the server
# console; give them plenty of time.
PAIRING_READ_TIMEOUT = 600.0


class FlowClient(LogitechFlowKvmCommand):
    leader_id: str
    follower_ids: list[str]
    cert: str | None = None
    token: str | None = None
    clipboard_enabled: bool = True

    follower_devices: list[PairedDevice]
    local_receivers: list[Receiver]
    manager: ReceiverManager
    reconciler: Reconciler
    # The leader's last-known host, as reported over the server's /events
    # stream. `None` until the first event arrives (or the stream's initial,
    # atomic snapshot -- see `sse.EventBroadcaster.subscribe`).
    leader_host: int | None = None

    # Set once (if ever) a Textual UI is running -- `None` when running
    # non-interactively, in which case status updates are simply skipped.
    tui: FlowTUIApp | None = None
    _connected_to_server: bool = False
    # When the /events stream last dropped, so `_get_desired_host` can tell a
    # momentary blip from a host that's actually gone. `None` until the first
    # drop (and, at startup, until the first successful connection).
    _disconnected_since: float | None = None
    # Whether the leader is connected to *this* host right now, learned from
    # our own notifications. Direct evidence, and therefore better than
    # anything the server can tell us about where the leader is.
    _leader_here: bool = False
    # Whether we're currently declining to act on `leader_host`; tracked only
    # so the transitions get logged once rather than every reconcile tick.
    _holding: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Server requests triggered by device notifications are queued here
        # and executed by a dedicated worker thread (`_run_http_tasks`), so
        # a slow or unreachable server can never stall the notification
        # listener -- the sole source of connect/disconnect events. A single
        # worker (rather than a thread per request) preserves ordering, e.g.
        # a clipboard push on leader-disconnect completes before the pull
        # triggered by the next connect.
        self._http_tasks: queue.Queue[Callable[[], None]] = queue.Queue()
        # Set by `resync()` to cut short the /events reconnect backoff.
        self._resync = threading.Event()
        # Populated by `handle()`; empty until then so notification handling
        # is well-defined even before devices have been resolved.
        self.follower_devices = []

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("host_number", type=int)
        parser.add_argument("server")
        parser.add_argument("--port", "-p", default=constants.DEFAULT_PORT, type=int)
        parser.add_argument(
            "--no-clipboard",
            action="store_true",
            help=(
                "Disable clipboard synchronization for this host. This host's "
                "clipboard will neither be read nor written."
            ),
        )

    def _find_follower(self, receiver: Receiver, devnumber: int) -> PairedDevice | None:
        """Match an incoming notification to an already-resolved follower."""
        for device in self.follower_devices:
            if device.receiver is receiver and device.number == devnumber:
                return device
        return None

    def callback(self, receiver: Receiver, notification: Notification) -> None:
        if notification.sub_id != 0x41:
            return

        # Followers are matched against the devices resolved at startup rather
        # than rebuilt with `receiver.get_device()`, which re-reads the pairing
        # registers live. Those reads can fail outright -- dropping the
        # notification -- or come back with a differing codename/serial, and
        # since `PairedDevice` is a frozen dataclass compared by value, the
        # result is then not the key `Reconciler` stores connection state
        # under, so `observe()` silently discards the observation. A single
        # disconnect lost that way leaves the reconciler driving a device that
        # has already left, forever. The live lookup remains for the leader,
        # which (unlike on the server) the client never resolves up front.
        device = self._find_follower(receiver, notification.devnumber)
        if device is None:
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

        if is_leader:
            # Direct evidence of the leader's whereabouts, which outranks
            # whatever the server last told us -- `_get_desired_host` uses it
            # to guarantee we never drive followers away from a host that is
            # demonstrably holding the leader.
            self._leader_here = connected
            self.reconciler.poke()
        else:
            self.reconciler.observe(device, connected)

        if connected:
            if is_leader:
                self._http_tasks.put(self._report_leader_host_here)
            if self.clipboard_enabled:
                self._http_tasks.put(self._pull_clipboard)
        elif is_leader and self.clipboard_enabled:
            self._http_tasks.put(self._push_clipboard)

        self._publish_status()

    def _server_evidence_is_fresh(self) -> bool:
        """Can `leader_host` still be trusted? It's only ever as good as our
        connection to the server that told us -- and that connection is also
        the only thing that would ever correct it."""
        if self._connected_to_server:
            return True
        if self._disconnected_since is None:
            return False
        return (time.monotonic() - self._disconnected_since) < SERVER_GRACE_PERIOD

    def _is_holding(self) -> bool:
        """Are we declining to act on a desired host because we can no longer
        confirm the host is there?"""
        if self._leader_here or self._server_evidence_is_fresh():
            return False
        return (
            self.leader_host is not None
            and self.leader_host != self.options.host_number
        )

    def _get_desired_host(self) -> int | None:
        """Where the reconciler should be driving followers, right now.

        `leader_host` on its own isn't enough: it's a belief formed from a
        single connect notification, and nothing about it expires. If the
        host it names is unplugged, acting on it means shoving the user's
        mouse onto a dead machine every couple of seconds, forever -- and
        since a receiver can only push a device away and never pull one
        back, nobody can undo that but the user, by hand, repeatedly.

        So the belief is only actionable while its source -- our connection
        to the server -- is alive. Without that, we return `None` and the
        reconciler simply holds: the devices stay on whichever host they're
        on, which is by definition one that works. Recovery needs no
        intervention; reconnecting re-announces our devices, which
        re-establishes where the leader really is.
        """
        holding = self._is_holding()
        if holding != self._holding:
            self._holding = holding
            if holding:
                logger.warning(
                    "Cannot reach the server, so it's no longer safe to assume "
                    "the leader is still on host %s; holding devices here until "
                    "the server is reachable again",
                    self.leader_host,
                )
            else:
                logger.info(
                    "The leader's whereabouts are known again; resuming host switching"
                )
            self._publish_status()

        if self._leader_here:
            # The leader is right here, so here is where the followers
            # belong. (`Reconciler` treats our own host number as "nothing
            # to do", which is exactly right.)
            return self.options.host_number
        return None if holding else self.leader_host

    def _report_leader_host_here(self) -> None:
        """Positive evidence: the leader is here. Report it so every client
        (including this one) learns to converge followers toward this host.
        """
        response = self.request(
            "PUT",
            self.build_url("leader-host"),
            data=str(self.options.host_number),
        )
        response.raise_for_status()

    def _pull_clipboard(self) -> None:
        response = self.request("GET", self.build_url("clipboard"))
        if response.ok:
            pyperclip.copy(response.text)

    def _push_clipboard(self) -> None:
        clipboard_data = pyperclip.paste()
        response = self.request(
            "PUT",
            self.build_url("clipboard"),
            data=clipboard_data.encode("utf-8"),
        )
        if response.ok:
            logger.info(
                "Clipboard contents set on server with %d bytes of data",
                len(clipboard_data),
            )

    def _run_http_tasks(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._http_tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                task()
            except Exception:
                # A failed server request (network blip, server restart)
                # must not kill this worker; the next notification or SSE
                # reconnect re-announcement will get things back in sync.
                logger.exception("Server request failed")

    def _reconciler_error(self, device: PairedDevice, error: Exception) -> None:
        logger.warning(
            "Could not switch %s to the desired host yet (%s); will retry",
            device.id,
            error,
        )

    def _reconciler_observation(self, device: PairedDevice, connected: bool) -> None:
        """The reconciler worked out a device's whereabouts by itself, rather
        than being told by a notification -- keep the log and UI honest."""
        if connected:
            logger.info(
                "Device %s answered here after all; resuming switching", device.id
            )
        else:
            logger.info(
                "Device %s is no longer on this host; will resume switching it "
                "if it returns",
                device.id,
            )
        self._publish_status()

    def _handle_event(self, event_type: str, data: str) -> None:
        if event_type == "leader-host":
            self.leader_host = int(data)
            self.reconciler.poke()
            self._publish_status()
        elif event_type == "leader-host-unknown":
            # The server no longer knows where the leader is: the client for
            # the host it believed held the leader just disconnected, taking
            # the only source of evidence with it.
            logger.info(
                "Host %s disconnected; the leader can no longer be assumed to be there",
                data,
            )
            self.leader_host = None
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
            holding=self._is_holding(),
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
                    "GET",
                    self.build_url("events"),
                    stream=True,
                    timeout=(10, EVENTS_READ_TIMEOUT),
                )
                response.raise_for_status()
                self._renotify_receivers()
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
                self._disconnected_since = time.monotonic()
                self._publish_status()
            if self._stop.is_set():
                return
            self._sleep_between_retries(backoff)
            backoff = min(backoff * 2, EVENTS_MAX_BACKOFF)

    def _renotify_receivers(self) -> None:
        """Ask every local receiver to resend a connection notification per
        device.

        This is the recovery mechanism the whole design leans on: replaying
        those notifications re-derives what this host actually knows -- which
        devices are here, and whether the leader is among them -- and reports
        it onward. Done on every (re)connection of the /events stream, so a
        server that went away and came back relearns the truth rather than
        carrying on with whatever it believed beforehand.
        """
        for receiver in self.local_receivers:
            try:
                receiver.notify_devices()
            except OSError:
                # This receiver is gone (unplugged); the ReceiverManager
                # will rebuild it and re-announce.
                pass

    def _sleep_between_retries(self, seconds: float) -> None:
        """Wait out the reconnect backoff, unless `resync()` says otherwise."""
        self._resync.wait(seconds)
        self._resync.clear()

    def resync(self) -> None:
        """Re-establish everything by hand, without waiting.

        Recovery is automatic, but its timing is bounded by the reconnect
        backoff -- so this exists for the case where someone is looking at a
        held display and would rather not wait for it.
        """
        logger.info("Resynchronising with the server")
        self._renotify_receivers()
        self._resync.set()

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
        kwargs.setdefault("timeout", DEFAULT_HTTP_TIMEOUT)

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
            timeout=(DEFAULT_HTTP_TIMEOUT[0], PAIRING_READ_TIMEOUT),
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
        self.clipboard_enabled = not self.options.no_clipboard

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
            get_desired_host=self._get_desired_host,
            host_number=self.options.host_number,
            on_error=self._reconciler_error,
            on_observation=self._reconciler_observation,
        )
        self.manager = ReceiverManager(
            self.local_receivers, rebind=self._bind_receivers, callback=self.callback
        )

        self._stop = threading.Event()

        logger.info("Server URL: %s", self.build_url())
        logger.info("Certificate: %s", self.cert)
        logger.info("Leader serial: %s", self.leader_id)
        logger.info("Follower serials: %s", ", ".join(self.follower_ids))
        if not self.clipboard_enabled:
            logger.info("Clipboard synchronization: disabled")

        if sys.stdout.isatty():

            def on_start(tui: FlowTUIApp) -> None:
                self.tui = tui
                self.start_background_threads()

            # Textual owns the main thread's event loop from here; Ctrl+C
            # is handled internally as a quit keybinding, not a raised
            # KeyboardInterrupt.
            logger.info("Press 'r' to resynchronise with the server at any time")
            FlowTUIApp("flow-client", on_start=on_start, on_resync=self.resync).run()
            self._stop.set()
            self.reconciler.stop()
            self.manager.stop()
        else:
            self.start_background_threads()
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self._stop.set()
                self.reconciler.stop()
                self.manager.stop()

    def _bind_receivers(self, receivers: list[Receiver]) -> None:
        """Re-resolve the follower devices against freshly opened receivers;
        called by the `ReceiverManager` after a receiver was rediscovered
        post-replug. Raises `DeviceNotFound` (making the manager retry)
        while a follower's receiver is still missing.
        """
        devices = resolve_devices(receivers, self.follower_ids)
        self.follower_devices = [devices[i] for i in self.follower_ids]
        self.local_receivers = receivers
        self.reconciler.set_devices(self.follower_devices)
        self._publish_status()

    def start_background_threads(self) -> None:
        """Start the reconciler, the receiver manager (which in turn starts
        the notification listeners), and the /events consumer.

        Deliberately not done inline in `handle()`: `callback()`/
        `_handle_event()`/`_consume_events()` may call
        `self.tui.update_status(...)`, which requires the TUI's event loop
        to already be running -- so when interactive, this is called from
        `FlowTUIApp.on_mount` instead.
        """
        self.reconciler.start()
        self.manager.start()

        http_thread = threading.Thread(target=self._run_http_tasks, daemon=True)
        http_thread.start()

        events_thread = threading.Thread(target=self._consume_events, daemon=True)
        events_thread.start()
