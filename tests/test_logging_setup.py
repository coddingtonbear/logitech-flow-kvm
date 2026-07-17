import logging
import logging.handlers

import platformdirs
import pytest

from logitech_flow_kvm import logging_setup


class FakeStream:
    """Minimal stand-in for `sys.stdout` -- just enough surface for
    `isatty()` checks and for a `StreamHandler` to wrap it without error."""

    def __init__(self, is_tty: bool):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


@pytest.fixture
def user_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        platformdirs, "user_log_dir", lambda *args, **kwargs: str(tmp_path)
    )
    return tmp_path


def _stream_handlers(logger: logging.Logger) -> list[logging.StreamHandler]:
    return [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.handlers.RotatingFileHandler)
    ]


class TestGetLogPath:
    def test_creates_and_returns_a_path_under_the_platform_log_dir(self, user_log_dir):
        path = logging_setup.get_log_path()

        assert path == str(user_log_dir / "logitech-flow-kvm.log")
        assert user_log_dir.is_dir()


class TestConfigureLogging:
    def test_sets_the_logger_to_info_level(self, user_log_dir, monkeypatch):
        monkeypatch.setattr(logging_setup.sys, "stdout", FakeStream(is_tty=True))
        logger = logging.Logger("test-configure-logging-level")

        logging_setup.configure_logging(logger)

        assert logger.level == logging.INFO

    def test_always_attaches_a_rotating_file_handler(self, user_log_dir, monkeypatch):
        monkeypatch.setattr(logging_setup.sys, "stdout", FakeStream(is_tty=True))
        logger = logging.Logger("test-configure-logging-file-handler")

        logging_setup.configure_logging(logger)

        file_handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        handler = file_handlers[0]
        assert handler.maxBytes == logging_setup.MAX_LOG_BYTES
        assert handler.backupCount == logging_setup.LOG_BACKUP_COUNT
        assert handler.formatter is not None
        assert handler.formatter._fmt == logging_setup.LOG_FORMAT

    def test_skips_the_plain_stdout_handler_when_interactive(
        self, user_log_dir, monkeypatch
    ):
        monkeypatch.setattr(logging_setup.sys, "stdout", FakeStream(is_tty=True))
        logger = logging.Logger("test-configure-logging-tty")

        logging_setup.configure_logging(logger)

        assert _stream_handlers(logger) == []

    def test_attaches_a_plain_stdout_handler_when_not_interactive(
        self, user_log_dir, monkeypatch
    ):
        monkeypatch.setattr(logging_setup.sys, "stdout", FakeStream(is_tty=False))
        logger = logging.Logger("test-configure-logging-non-tty")

        logging_setup.configure_logging(logger)

        handlers = _stream_handlers(logger)
        assert len(handlers) == 1
        assert handlers[0].formatter is not None
        assert handlers[0].formatter._fmt == logging_setup.LOG_FORMAT

    def test_defaults_to_the_root_logger(self, user_log_dir, monkeypatch):
        monkeypatch.setattr(logging_setup.sys, "stdout", FakeStream(is_tty=True))
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_level = root.level
        try:
            logging_setup.configure_logging()

            assert root.level == logging.INFO
        finally:
            for handler in root.handlers:
                if handler not in original_handlers:
                    handler.close()
            root.handlers = original_handlers
            root.setLevel(original_level)

    def test_pins_the_werkzeug_logger_to_warning(self, user_log_dir, monkeypatch):
        monkeypatch.setattr(logging_setup.sys, "stdout", FakeStream(is_tty=True))
        logger = logging.Logger("test-configure-logging-werkzeug")
        logging.getLogger("werkzeug").setLevel(logging.NOTSET)

        logging_setup.configure_logging(logger)

        assert logging.getLogger("werkzeug").level == logging.WARNING
