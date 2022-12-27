import sys
import logging.config

from safdie import SafdieRunner


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": True,
    "formatters": {
        "standard": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
    },
    "handlers": {
        "default": {
            "level": "INFO",
            "formatter": "standard",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "": {
            "handlers": ["default"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}


def main(args=sys.argv):
    logging.config.dictConfig(LOGGING_CONFIG)

    SafdieRunner("logitech_flow_kvm.commands").run()
