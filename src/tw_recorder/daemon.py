"""Dämon-Modus: Thread-Verwaltung pro Kanal, Config-Hot-Reload, Shutdown."""

import logging
import signal
import sys
import threading
import time

from tw_recorder import config
from tw_recorder.healthcheck import write_heartbeat
from tw_recorder.recorder import record_loop

logger = logging.getLogger(__name__)

running_threads = {}

# Globale Variablen für das Live-Reload-Tracking
LAST_CONFIG_HASH = ""
LAST_FILES_STATE = {}


def parse_streamers():
    """Liest die Kanäle aus der [channels] Sektion der INI-Datei."""
    streamers = {}
    cfg = config.load_config()
    config_obj = cfg["config_obj"]

    if config_obj.has_section("channels"):
        for raw_url, quality in config_obj.items("channels"):
            raw_url = raw_url.strip()
            quality = quality.strip() if quality.strip() else "best"

            # Bereinige sämtliche gängigen Twitch-Präfixe (inkl. www)
            clean_input = raw_url
            prefixes = [
                "https://www.twitch.tv/", "http://www.twitch.tv/", "www.twitch.tv/",
                "https://twitch.tv/", "http://twitch.tv/", "twitch.tv/"
            ]

            for prefix in prefixes:
                if clean_input.lower().startswith(prefix):
                    clean_input = clean_input[len(prefix):]
                    break

            # Entferne eventuelle Slash-Endungen (z.B. bei /sandyjackson/)
            clean_input = clean_input.rstrip("/")

            if clean_input:
                # Einheitliche URL generieren, Originalschreibweise (z.B. Reved) bleibt im Namen erhalten
                full_url = f"https://twitch.tv/{clean_input}"
                streamers[full_url] = quality

    return streamers


def sync_processes():
    current_streamers = parse_streamers()
    logger.info(f"{len(current_streamers)} Streamer in Config gefunden.")

    # 1. Entfernte Streamer stoppen
    for url in list(running_threads.keys()):
        if url not in current_streamers:
            channel = url.rstrip("/").split("/")[-1]
            logger.info(f"Stoppe Überwachung für Kanal: {channel}")
            thread, stop_event = running_threads.pop(url)
            stop_event.set()

    # 2. Neue Streamer starten (laufende Threads nicht verdoppeln)
    for url, quality in current_streamers.items():
        if url in running_threads:
            thread, stop_event = running_threads[url]
            if thread.is_alive():
                continue

        channel = url.rstrip("/").split("/")[-1]
        logger.info(f"Starte Überwachung für Kanal: {channel}")
        stop_event = threading.Event()
        thread = threading.Thread(
            target=record_loop,
            args=(url, quality, stop_event),
            daemon=True
        )
        thread.start()
        running_threads[url] = (thread, stop_event)


def shutdown_handler(signum, frame):
    logger.info("Signal empfangen, beende alle Aufnahmen...")
    threads = []
    for url, (thread, stop_event) in running_threads.items():
        stop_event.set()
        threads.append(thread)

    # Gemeinsames Zeitbudget für ALLE Threads statt 10s pro Thread einzeln -
    # bei z.B. 5 aktiven Kanälen sonst im Worst Case 50s Shutdown-Verzögerung.
    shutdown_deadline = time.monotonic() + 10
    for thread in threads:
        remaining = shutdown_deadline - time.monotonic()
        if remaining > 0:
            thread.join(timeout=remaining)

    sys.exit(0)


def run_daemon():
    global LAST_CONFIG_HASH, LAST_FILES_STATE

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info(f"Starte Streamlink-Manager (Config: {config.CONFIG_FILE})...")

    # Initialen Zustand beim Start erfassen
    LAST_CONFIG_HASH, LAST_FILES_STATE = config.get_config_hash()
    sync_processes()

    while True:
        write_heartbeat()
        current_hash, current_state = config.get_config_hash()

        if current_hash != LAST_CONFIG_HASH:
            changed_files = []

            # Neue oder geänderte Dateien ermitteln
            for path, file_hash in current_state.items():
                if path not in LAST_FILES_STATE:
                    changed_files.append(f"neu: {path.name}")
                elif LAST_FILES_STATE[path] != file_hash:
                    changed_files.append(f"geändert: {path.name}")

            # Gelöschte Dateien ermitteln
            for path in LAST_FILES_STATE:
                if path not in current_state:
                    changed_files.append(f"gelöscht: {path.name}")

            changes_str = ", ".join(changed_files) if changed_files else "Konfiguration"
            logger.info(f"🔄 Konfigurationsänderung erkannt ({changes_str}). Synchronisiere...")

            LAST_CONFIG_HASH = current_hash
            LAST_FILES_STATE = current_state

            sync_processes()

        time.sleep(5)
