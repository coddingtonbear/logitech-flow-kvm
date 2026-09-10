import asyncio
import logging

from textual.widgets import RichLog

from logitech_flow_kvm.tui.app import FlowTUIApp
from logitech_flow_kvm.tui.widgets import StatusPanel


def run(coro):
    return asyncio.run(coro)


class TestFlowTUIApp:
    def test_on_start_runs_once_the_app_is_mounted(self):
        started: list[FlowTUIApp] = []

        async def body():
            app = FlowTUIApp("flow-server", on_start=started.append)
            async with app.run_test():
                assert started == [app]

        run(body())

    def test_update_status_is_thread_safe_and_updates_the_panel(self):
        async def body():
            app = FlowTUIApp("flow-server", on_start=lambda a: None)
            async with app.run_test() as pilot:
                await asyncio.to_thread(app.update_status, "new status")
                await pilot.pause()

                panel = app.query_one(StatusPanel)
                assert "new status" in str(panel.render())

        run(body())

    def test_log_records_from_a_background_thread_reach_the_log_panel(self):
        # Needs `getLogger`, not a bare `Logger(...)` -- only loggers created
        # through the manager get a `.parent` chain, and it's propagation up
        # that chain that reaches the root handler `on_mount` installs.
        logger = logging.getLogger("test-flow-tui-app-log-bridge")
        logger.setLevel(logging.INFO)

        async def body():
            app = FlowTUIApp("flow-server", on_start=lambda a: None)
            async with app.run_test() as pilot:
                # Intercept at the widget boundary rather than asserting on
                # rendered/wrapped output -- word-wrap can legitimately split
                # a line across multiple `.lines` entries, which isn't what
                # this test cares about.
                written: list[object] = []
                log_widget = app.query_one(RichLog)
                log_widget.write = written.append  # type: ignore[assignment]

                def log_from_background_thread():
                    logger.info("hello from a background thread")

                await asyncio.to_thread(log_from_background_thread)
                await pilot.pause()

                assert any(
                    "hello from a background thread" in str(item) for item in written
                )

        run(body())

    def test_the_log_handler_is_detached_on_unmount(self):
        async def body():
            app = FlowTUIApp("flow-server", on_start=lambda a: None)
            root = logging.getLogger()
            handlers_before = set(root.handlers)

            async with app.run_test():
                added = set(root.handlers) - handlers_before
                assert len(added) == 1

            assert set(root.handlers) == handlers_before

        run(body())

    def test_the_ui_inherits_the_terminals_colours_and_keeps_the_log_plain(self):
        async def body():
            app = FlowTUIApp("flow-server", on_start=lambda a: None)
            async with app.run_test():
                # No painted theme background -- the terminal's own colours.
                assert app.current_theme.ansi is True

                # The log is just a log: word-wrapped to the widget's width,
                # with no border or scrollbars around it.
                log_widget = app.query_one(RichLog)
                assert log_widget.wrap is True
                assert log_widget.min_width == 0
                assert log_widget.styles.scrollbar_size_vertical == 0
                assert log_widget.styles.scrollbar_size_horizontal == 0
                assert all(
                    border_type == "" for border_type, _ in log_widget.styles.border
                )

        run(body())
