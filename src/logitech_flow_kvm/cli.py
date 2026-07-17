import sys

from safdie import SafdieRunner

from .logging_setup import configure_logging


def main(args=sys.argv):
    configure_logging()

    SafdieRunner("logitech_flow_kvm.commands").run()
