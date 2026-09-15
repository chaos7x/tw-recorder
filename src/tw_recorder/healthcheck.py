"""Heartbeat-Datei und externer Healthcheck (z.B. Docker HEALTHCHECK)."""

import logging
import time

from tw_recorder import config

logger = logging.getLogger(__name__)


def write_heartbeat():
    """
    Aktualisiert die Heartbeat-Datei mit dem aktuellen Zeitstempel. Wird bei
    jedem Durchlauf der run_daemon()-Hauptschleife aufgerufen. Schlägt der
    Schreibvorgang fehl (z.B. kein Schreibzugriff auf /tmp), wird das nur auf
    DEBUG geloggt - ein Healthcheck-Problem soll den Recorder nicht stören.
    """
    try:
        config.HEALTH_FILE.write_text(str(time.time()))
    except OSError as e:
        logger.debug(f"Konnte Heartbeat-Datei {config.HEALTH_FILE} nicht schreiben: {e}")


def run_healthcheck() -> int:
    """
    Leichtgewichtige Prüfung für externe Healthchecks (z.B. Docker HEALTHCHECK
    oder Kubernetes livenessProbe): prüft nur, ob die Heartbeat-Datei existiert
    und nicht älter als config.HEALTH_STALE_SECONDS ist. Lädt bewusst keine Config,
    damit der Aufruf schnell ist und keine Nebenwirkungen hat (wird
    typischerweise alle paar Sekunden aufgerufen).
    Gibt 0 (healthy) oder 1 (unhealthy) zurück, analog zu Exit-Codes.
    """
    if not config.HEALTH_FILE.is_file():
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} nicht gefunden (Dämon noch nicht gestartet?).")
        return 1

    try:
        age = time.time() - config.HEALTH_FILE.stat().st_mtime
    except OSError as e:
        print(f"UNHEALTHY: Heartbeat-Datei {config.HEALTH_FILE} nicht lesbar: {e}")
        return 1

    if age > config.HEALTH_STALE_SECONDS:
        print(f"UNHEALTHY: Heartbeat ist {age:.0f}s alt (Limit: {config.HEALTH_STALE_SECONDS}s).")
        return 1

    print(f"HEALTHY: Heartbeat ist {age:.0f}s alt.")
    return 0
