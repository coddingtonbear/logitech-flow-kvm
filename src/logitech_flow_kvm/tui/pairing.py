from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input
from textual.widgets import Label


class PairingCodeModal(ModalScreen[str | None]):
    """Collects a pairing code from the operator without touching stdin --
    used in place of `rich.prompt.Prompt.ask()` while a Textual UI owns the
    terminal. Dismisses with the typed code on Enter, or `None` on Escape
    (treated the same as a wrong code by the caller)."""

    CSS = """
    PairingCodeModal {
        align: center middle;
    }

    #pairing-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, remote_addr: str) -> None:
        super().__init__()
        self._remote_addr = remote_addr

    def compose(self) -> ComposeResult:
        with Vertical(id="pairing-dialog"):
            yield Label(f"Pairing request from {self._remote_addr}")
            yield Label("Enter the pairing code shown on the client:")
            yield Input(placeholder="Pairing code", id="pairing-code-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)
