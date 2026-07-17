"""Server-Sent Events: encoding/decoding the wire format, and server-side fan-out.

`EventBroadcaster` is the server side of the "Better Guarantees" design: it
holds the one piece of state clients care about (the leader's current host)
and fans out changes to every subscribed client, while also handing a brand
new subscriber the current state as its first message -- atomically, so no
update landing between "subscribe" and "read current state" can be missed.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable
from collections.abc import Iterator

Subscription = "queue.Queue[str]"


def format_sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def parse_sse_stream(lines: Iterable[str | None]) -> Iterator[tuple[str, str]]:
    """Parse `event`/`data` pairs out of raw SSE lines (e.g. from `iter_lines()`).

    Comment lines (leading `:`, used for keepalives) are skipped. A `data`
    field with no preceding `event` field defaults to the SSE-standard type
    "message".
    """
    event_type = "message"
    data_lines: list[str] = []
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data_lines:
                yield event_type, "\n".join(data_lines)
            event_type = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_type, "\n".join(data_lines)


class EventBroadcaster:
    """Tracks one piece of broadcast state and fans out events to subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[str]] = []
        self._state: str | None = None

    @property
    def state(self) -> str | None:
        return self._state

    def subscribe(self) -> tuple[queue.Queue[str], str | None]:
        """Register a new subscriber and atomically read the current state.

        Registering before reading is what makes this atomic: a concurrent
        `set_state`/`broadcast` either finishes first (and shows up in the
        `current` value returned here) or starts after (and this subscriber
        is already in `_subscribers`, so it receives it as a normal message).
        Either way, nothing that happens in between can be missed.
        """
        q: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
            current = self._state
        return q, current

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def set_state(self, event: str, data: str) -> None:
        with self._lock:
            self._state = data
        self.broadcast(event, data)

    def broadcast(
        self, event: str, data: str, *, exclude: queue.Queue[str] | None = None
    ) -> None:
        message = format_sse(event, data)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            if subscriber is not exclude:
                subscriber.put(message)
