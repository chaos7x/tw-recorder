"""CLI-Einstiegspunkt: Argument-Parsing und Moduswahl."""

import argparse
import sys

from tw_recorder import __title__, __version__, config
from tw_recorder.daemon import run_daemon
from tw_recorder.healthcheck import run_healthcheck
from tw_recorder.logging_setup import setup_logging


def main():
    parser = argparse.ArgumentParser(
        prog="tw-recorder",
        description=f"{__title__} v{__version__}"
    )
    parser.add_argument(
        "-D", "--daemon",
        action="store_true",
        help="Dämon-Modus: Dauerhafte Streamüberwachung"
    )
    parser.add_argument(
        "-v", "-V", "--version",
        action="version",
        version=f"{__title__} v{__version__}"
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Prüft nur den Heartbeat des laufenden Dämons und beendet sich sofort (für Docker HEALTHCHECK)"
    )
    args = parser.parse_args()

    if args.healthcheck:
        sys.exit(run_healthcheck())

    if not args.daemon:
        parser.print_usage()
        print("Hinweis: Zum Starten bitte -D oder --daemon verwenden.")
        return

    cfg = config.load_config()
    setup_logging(cfg)
    run_daemon()



if __name__ == "__main__":
    main()
