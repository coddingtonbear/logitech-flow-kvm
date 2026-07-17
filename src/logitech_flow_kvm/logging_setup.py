import logging
import logging.handlers
import os
import sys

import platformdirs

from . import constants

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def get_log_path() -> str:
    log_dir = platformdirs.user_log_dir(constants.APP_NAME, constants.APP_AUTHOR)
    os.makedirs(log_dir, exist_ok=True)

    return os.path.join(log_dir, "logitech-flow-kvm.log")


def configure_logging(logger: logging.Logger | None = None) -> None:
    """Attach a rotating file handler (always) and a plain stdout handler
    (only when stdout isn't a TTY -- an interactive command builds its own
    Textual log handler instead, once it knows it's actually running one)."""
    if logger is None:
        logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        get_log_path(), maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if not sys.stdout.isatty():
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
