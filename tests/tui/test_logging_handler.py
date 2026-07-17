import logging

from logitech_flow_kvm.tui.logging_handler import TextualLogHandler


class TestTextualLogHandler:
    def test_emit_formats_the_record_and_calls_the_sink(self):
        received: list[str] = []
        handler = TextualLogHandler(received.append)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger = logging.Logger("test-textual-log-handler")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logger.info("hello %s", "world")

        assert received == ["[INFO] hello world"]

    def test_a_formatting_error_goes_through_handleError_not_the_sink(self):
        received: list[str] = []
        handler = TextualLogHandler(received.append)
        errors: list[logging.LogRecord] = []
        handler.handleError = errors.append  # type: ignore[assignment]
        logger = logging.Logger("test-textual-log-handler-format-error")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # %d against a string argument raises inside `Formatter.format`.
        logger.info("bad format: %d", "not-a-number")

        assert received == []
        assert len(errors) == 1
