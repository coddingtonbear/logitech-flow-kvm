import asyncio
import threading

from textual.app import App

from logitech_flow_kvm.tui.pairing import PairingCodeModal


def run(coro):
    return asyncio.run(coro)


class HarnessApp(App):
    """A minimal app to push `PairingCodeModal` onto, mirroring how
    `FlowTUIApp` uses it without pulling in the rest of the app shell."""

    pass


class TestPairingCodeModal:
    def test_submitting_the_input_dismisses_with_the_typed_code(self):
        async def body():
            app = HarnessApp()
            async with app.run_test() as pilot:
                results: list[str | None] = []
                app.push_screen(PairingCodeModal("10.0.0.5"), callback=results.append)
                await pilot.pause()

                await pilot.click("#pairing-code-input")
                for char in "123456":
                    await pilot.press(char)
                await pilot.press("enter")
                await pilot.pause()

                assert results == ["123456"]

        run(body())

    def test_escape_dismisses_with_none(self):
        async def body():
            app = HarnessApp()
            async with app.run_test() as pilot:
                results: list[str | None] = []
                app.push_screen(PairingCodeModal("10.0.0.5"), callback=results.append)
                await pilot.pause()

                await pilot.press("escape")
                await pilot.pause()

                assert results == [None]

        run(body())

    def test_shows_the_requesting_address(self):
        async def body():
            app = HarnessApp()
            async with app.run_test() as pilot:
                app.push_screen(PairingCodeModal("10.0.0.5"))
                await pilot.pause()

                from textual.widgets import Label

                # `app.query` only searches the base screen -- the labels
                # live on the pushed modal, which is `app.screen` once active.
                labels = [str(label.render()) for label in app.screen.query(Label)]
                assert any("10.0.0.5" in label for label in labels)

        run(body())


class TestFlowTUIAppRequestPairingCode:
    def test_returns_the_typed_code_from_a_background_thread(self):
        from logitech_flow_kvm.tui.app import FlowTUIApp

        async def body():
            app = FlowTUIApp("flow-server", on_start=lambda a: None)
            async with app.run_test() as pilot:
                results: dict[str, object] = {}

                def call_from_bg_thread():
                    results["value"] = app.request_pairing_code("10.0.0.5")

                t = threading.Thread(target=call_from_bg_thread)
                t.start()
                await pilot.pause()
                await pilot.pause()

                await pilot.click("#pairing-code-input")
                for char in "654321":
                    await pilot.press(char)
                await pilot.press("enter")

                await asyncio.to_thread(t.join)

                assert results["value"] == "654321"

        run(body())

    def test_cancel_returns_none_from_a_background_thread(self):
        from logitech_flow_kvm.tui.app import FlowTUIApp

        async def body():
            app = FlowTUIApp("flow-server", on_start=lambda a: None)
            async with app.run_test() as pilot:
                results: dict[str, object] = {}

                def call_from_bg_thread():
                    results["value"] = app.request_pairing_code("10.0.0.5")

                t = threading.Thread(target=call_from_bg_thread)
                t.start()
                await pilot.pause()
                await pilot.pause()

                await pilot.press("escape")

                await asyncio.to_thread(t.join)

                assert results["value"] is None

        run(body())
