import logging
from collections.abc import Callable


class TextualLogHandler(logging.Handler):
    """Bridges stdlib logging records into a Textual widget.

    Deliberately decoupled from Textual's runtime: `sink` is a plain
    callable, so this is unit-testable without a real `App`/event loop. The
    caller is responsible for making `sink` thread-safe -- `FlowTUIApp` wraps
    a `RichLog` widget's `write` in `App.call_from_thread` for this reason,
    since log records can arrive from any of the background threads flow-
    server/flow-client run.
    """

    def __init__(self, sink: Callable[[str], None]):
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return

        self._sink(message)
