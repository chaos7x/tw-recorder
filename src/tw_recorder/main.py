"""CLI-Einstiegspunkt: Argument-Parsing und Moduswahl."""

import argparse
import sys

from tw_recorder import __title__, __version__, config, skip
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
    parser.add_argument(
        "--skip",
        metavar="KANAL",
        help="Laufende Aufnahme von KANAL beenden und diesen Stream nicht erneut aufnehmen "
             "(gilt bis zum nächsten Stream des Kanals oder bis zum Reboot)"
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Mit --skip: bisher aufgenommenen Teil behalten (remuxen) statt ihn zu verwerfen"
    )
    parser.add_argument(
        "--unskip",
        metavar="KANAL",
        help="Skip für KANAL aufheben, damit der laufende Stream wieder aufgenommen wird"
    )
    parser.add_argument(
        "--list-skips",
        action="store_true",
        help="Gesetzte Skips anzeigen"
    )
    args = parser.parse_args()

    if args.keep and not args.skip:
        parser.error("--keep ist nur zusammen mit --skip sinnvoll")
    if sum(bool(x) for x in (args.skip, args.unskip, args.list_skips, args.healthcheck, args.daemon)) > 1:
        parser.error("-D, --healthcheck, --skip, --unskip und --list-skips schließen sich gegenseitig aus")

    if args.healthcheck:
        sys.exit(run_healthcheck())
    if args.skip:
        sys.exit(skip.cli_skip(args.skip, keep=args.keep))
    if args.unskip:
        sys.exit(skip.cli_unskip(args.unskip))
    if args.list_skips:
        sys.exit(skip.cli_list())

    if not args.daemon:
        parser.print_usage()
        print("Hinweis: Zum Starten bitte -D oder --daemon verwenden.")
        return

    cfg = config.load_config()
    setup_logging(cfg)
    run_daemon()


if __name__ == "__main__":
    main()
