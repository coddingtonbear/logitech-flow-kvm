from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from rich.console import RenderableType
from textual.app import App
from textual.app import ComposeResult
from textual.widgets import RichLog

from ..logging_setup import LOG_FORMAT
from .logging_handler import TextualLogHandler
from .widgets import StatusPanel


class FlowTUIApp(App):
    """Shared shell for flow-server/flow-client: a status panel on top, a
    scrolling log panel below.

    Textual's event loop must be live before any foreign thread can call
    `call_from_thread` on this app -- so background work (Flask, the
    reconciler, notification listeners, ...) is started from `on_start`,
    which is invoked once `on_mount` confirms the app is actually running,
    rather than before `App.run()` is called.
    """

    CSS_PATH = Path(__file__).parent / "app.tcss"

    def __init__(self, title: str, on_start: Callable[[FlowTUIApp], None]):
        super().__init__()
        self.title = title
        self._on_start = on_start
        self._log_handler: logging.Handler | None = None

    def compose(self) -> ComposeResult:
        yield StatusPanel(id="status-panel")
        yield RichLog(id="log-panel", markup=True, wrap=True)

    def on_mount(self) -> None:
        log_panel = self.query_one(RichLog)

        def sink(line: str) -> None:
            self.call_from_thread(log_panel.write, line)

        handler = TextualLogHandler(sink)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

        self._on_start(self)

    def on_unmount(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

    def update_status(self, renderable: RenderableType) -> None:
        """Thread-safe: call from any background thread to refresh the
        status panel."""
        self.call_from_thread(self.query_one(StatusPanel).update, renderable)
