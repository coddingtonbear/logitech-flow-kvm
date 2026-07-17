"""Test doubles for the hidpp.protocol.Transport protocol."""

import dataclasses
from collections.abc import Callable


@dataclasses.dataclass
class ScriptedReply:
    """Reply with `reply` (payload after the echoed header) when `matcher` matches."""

    matcher: Callable[[int, bytes], bool]
    reply: bytes


def register_matcher(devnumber: int, params: bytes) -> Callable[[int, bytes], bool]:
    """Match a request to `devnumber` whose params equal `params`."""

    def matcher(actual_devnumber: int, payload: bytes) -> bool:
        return actual_devnumber == devnumber and payload[2:] == params

    return matcher


Responder = Callable[[int, bytes, bool], "bytes | None"]


class ScriptedTransport:
    """A fake Transport that replies based on scripted (devnumber, params) matchers.

    Request IDs carry a randomized SoftwareId, so replies are matched by
    devnumber + params rather than by predicting the exact on-wire header.

    For the common case (a successful register/feature read), pass `replies`:
    the fake echoes back whatever header it actually received, followed by
    the scripted reply bytes. For full control over the reply -- e.g. to
    simulate an error reply, which doesn't follow that echoing convention --
    pass `respond` instead, returning the exact reply payload to deliver.
    """

    def __init__(
        self,
        replies: list[ScriptedReply] | None = None,
        respond: Responder | None = None,
    ):
        self._replies = replies or []
        self._respond = respond
        self._pending: list[tuple[int, int, bytes]] = []
        self.writes: list[tuple[int, bytes, bool]] = []

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None:
        self.writes.append((devnumber, payload, long_message))
        report_id = 0x11 if long_message else 0x10

        if self._respond is not None:
            reply_data = self._respond(devnumber, payload, long_message)
            if reply_data is not None:
                self._pending.append((report_id, devnumber, reply_data))
            return

        for scripted in self._replies:
            if scripted.matcher(devnumber, payload):
                self._pending.append(
                    (report_id, devnumber, payload[:2] + scripted.reply)
                )
                return

    def read(self, timeout: float) -> tuple[int, int, bytes] | None:
        if self._pending:
            return self._pending.pop(0)
        return None

    def drain(self) -> None:
        self._pending.clear()
