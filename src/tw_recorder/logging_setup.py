"""Logging-Setup: Syslog-Erkennung und Handler-Konfiguration."""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from tw_recorder.config import APP_NAME

logger = logging.getLogger(__name__)


def _is_syslog_daemon_running() -> bool:
    """
    Prüft ohne Zusatzabhängigkeiten (kein subprocess, kein psutil), ob ein
    klassischer Syslog-Daemon (rsyslogd, syslog-ng, syslogd) läuft, indem
    /proc nach Prozessen mit passendem Namen durchsucht wird (Prozessname
    aus /proc/<pid>/comm). Auf Systemen ohne /proc (z.B. nicht-Linux)
    liefert die Prüfung konservativ False.
    """
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return False

    known_daemons = {"rsyslogd", "syslog-ng", "syslogd"}

    try:
        pid_dirs = [p for p in proc_dir.iterdir() if p.name.isdigit()]
    except OSError:
        return False

    for pid_dir in pid_dirs:
        try:
            comm = (pid_dir / "comm").read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            # Prozess kann zwischen listdir() und read_text() beendet worden
            # sein, oder /comm ist nicht lesbar - einfach überspringen.
            continue
        if comm in known_daemons:
            return True

    return False


def _is_dedicated_mount(path: Path) -> bool:
    """
    Prüft, ob path ein eigener Mountpoint ist (Docker-Volume/Bind-Mount) statt
    nur ein gewöhnliches Verzeichnis, das das Dockerfile per `mkdir -p` fest
    ins Image gebacken hat (z.B. /log) - eine reine is_dir()-Prüfung kann
    diese beiden Fälle nicht unterscheiden, da `mkdir -p` das Verzeichnis auch
    ganz ohne jeden Mount anlegt. Vergleicht dazu die Geräte-ID (st_dev) von
    path und seinem Elternverzeichnis: unterschiedliche st_dev bedeutet, dass
    dort tatsächlich ein Volume/Bind-Mount eingehängt ist.
    """
    if not path.is_dir():
        return False
    try:
        return path.stat().st_dev != path.parent.stat().st_dev
    except OSError:
        return False


def _resolve_log_file_path(app_name: str, explicit_path: str) -> Path:
    """
    Ermittelt den Ziel-Pfad für die rotierende Logdatei nach Priorität:
    1. Explizite Konfiguration (Config-Datei oder ENV-Variable LOG_FILE)
    2. /log/<app_name>.log, falls /log als eigenes Docker-Volume gemountet
       ist (nicht nur als vom Dockerfile angelegtes Verzeichnis vorhanden,
       siehe _is_dedicated_mount())
    3. /var/log/<app_name>/<app_name>.log, falls anlegbar/beschreibbar (FHS)
    4. Fallback: Datei im Programmverzeichnis selbst
    """
    if explicit_path:
        return Path(explicit_path)

    docker_log_dir = Path("/log")
    if _is_dedicated_mount(docker_log_dir):
        return docker_log_dir / f"{app_name}.log"

    var_log_dir = Path(f"/var/log/{app_name}")
    try:
        var_log_dir.mkdir(parents=True, exist_ok=True)
        if os.access(var_log_dir, os.W_OK):
            return var_log_dir / f"{app_name}.log"
    except OSError:
        pass

    try:
        script_dir = Path(__file__).resolve().parent
    except OSError:
        script_dir = Path.cwd()
    return script_dir / f"{app_name}.log"


def setup_logging(cfg: dict | None = None, app_name: str = APP_NAME) -> logging.Logger:
    """
    Konfiguriert das Logging der gesamten Anwendung (Root-Logger):
    - Immer ein stdout-Handler, damit journald/systemd/docker logs die
      Ausgabe unabhängig von der Betriebsart automatisch erfassen.
    - Zusätzlich ein Datei-Handler, ausgelöst durch: explizite
      log_file-Konfiguration, ein tatsächlich als Docker-Volume gemountetes
      /log-Verzeichnis (siehe _is_dedicated_mount() - eine reine
      is_dir()-Prüfung reicht nicht, da das Dockerfile /log auch ganz ohne
      Mount fest ins Image anlegt), oder einen tatsächlich laufenden
      klassischen Syslog-Daemon. Ohne einen dieser Gründe läuft die Ausgabe
      ohnehin bereits über journald (stdout-Erfassung) - eine eigene
      Logdatei wäre dann nur doppelte Datenhaltung ohne Mehrwert.
      Nur bei (a)/(b) rotiert die App selbst (RotatingFileHandler, max. 10 MB,
      5 Backups) - dort rotiert sonst niemand. Bei (c) (Syslog-Daemon erkannt)
      dagegen ein einfacher FileHandler ohne eigene Rotation: ein System mit
      laufendem Syslog-Daemon hat so gut wie immer auch logrotate zur Hand
      (Standard-Debian-Konvention, siehe mitgeliefertes
      /etc/logrotate.d/tw-recorder) - zwei unabhängige Rotationsmechanismen
      auf derselben Datei würden sich nur gegenseitig ins Gehege kommen.
    - Log-Level per ENV-Variable DEBUG steuerbar (true/yes/1, case-insensitive).
    - HTTP-Bibliotheks-Logger werden unabhängig vom eigenen Level auf
      WARNING gedrosselt, damit Connection-Pool-Rauschen nicht das
      eigentliche Debug-Logging zumüllt.
    """
    debug_enabled = os.getenv("DEBUG", "").strip().lower() in ("true", "yes", "1")
    level = logging.DEBUG if debug_enabled else logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Bestehende Handler entfernen, damit setup_logging() idempotent
    # aufgerufen werden kann, ohne doppelte Log-Zeilen zu erzeugen.
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    active_handlers_desc = ["stdout"]

    explicit_path = ""
    if cfg is not None:
        explicit_path = str(cfg.get("log_file") or "").strip()
    if not explicit_path:
        explicit_path = os.getenv("LOG_FILE", "").strip()

    docker_log_volume_mounted = _is_dedicated_mount(Path("/log"))
    syslog_running = _is_syslog_daemon_running()

    # Datei-Logging wird ausgelöst durch (a) explizite Konfiguration, (b) ein
    # tatsächlich als Docker-Volume gemountetes /log (klares Signal, dass
    # Logdateien gewünscht sind - unabhängig davon, ob im Container selbst
    # ein Syslog-Daemon läuft, was in Containern ohnehin unüblich ist), oder
    # (c) einen tatsächlich laufenden klassischen Syslog-Daemon auf
    # Bare-Metal-/systemd-Systemen. Ohne einen dieser drei Gründe läuft die
    # Ausgabe ohnehin schon über journald/docker logs (stdout-Erfassung) -
    # eine eigene Logdatei wäre dann nur doppelte Datenhaltung ohne Mehrwert.
    if explicit_path:
        trigger_reason = "explizite log_file-Konfiguration"
    elif docker_log_volume_mounted:
        trigger_reason = "/log als Docker-Volume gemountet"
    elif syslog_running:
        trigger_reason = "Syslog-Daemon erkannt"
    else:
        trigger_reason = None

    if trigger_reason is not None:
        log_path = _resolve_log_file_path(app_name, explicit_path)
        self_rotate = trigger_reason != "Syslog-Daemon erkannt"

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if self_rotate:
                file_handler = logging.handlers.RotatingFileHandler(
                    str(log_path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
                )
            else:
                file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            active_handlers_desc.append(f"Datei ({log_path})")
            reason = f"{trigger_reason}"
        except OSError as e:
            reason = f"{trigger_reason}, aber Datei-Handler konnte nicht eingerichtet werden ({e})"
    else:
        reason = "Kein Syslog-Daemon/-Log-Verzeichnis/-Config erkannt - Logging nur nach stdout (journald erfasst das bereits)"

    # HTTP-Bibliotheks-Rauschen unabhängig vom eigenen Log-Level drosseln
    # (z.B. "Resetting dropped connection" o.ä.), falls entsprechende
    # Abhängigkeiten im Projekt zum Einsatz kommen.
    for noisy_logger_name in ("urllib3", "urllib3.connectionpool", "requests", "httpx"):
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)

    logger.info(
        f"Logging initialisiert - Level={logging.getLevelName(level)}, "
        f"aktive Handler: {', '.join(active_handlers_desc)}. {reason}."
    )

    return logger
