#!/usr/bin/env python3
# Copyright (C) 2026 Chaos7x
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

import argparse
import configparser
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # fcntl ist nur auf Unix verfügbar; auf anderen Plattformen läuft das
    # Skript ohne Datei-Lock-Schutz zwischen mehreren Instanzen weiter.
    fcntl = None
    HAS_FCNTL = False

__title__ = "Streamlink Recorder CLI"
__version__ = "1.2.0"

APP_NAME = "tw-recorder"
logger = logging.getLogger(APP_NAME)

# Ungepufferte Standard-Ausgabe erzwingen
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Standard-Konfigurationspfade
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/etc/tw-recorder/recorder.conf"))
CONF_D_DIR = Path(os.getenv("CONF_D_DIR", "/etc/tw-recorder/conf.d"))

# Heartbeat-Datei für den Healthcheck (z.B. Docker HEALTHCHECK). run_daemon()
# aktualisiert sie bei jedem Schleifendurchlauf; ein separater, sehr
# leichtgewichtiger Aufruf desselben Skripts (--healthcheck) prüft nur, ob
# sie frisch genug ist - ohne Config zu laden, damit der Aufruf schnell ist
# und keine Nebenwirkungen hat.
HEALTH_FILE = Path(os.getenv("HEALTH_FILE", str(Path(tempfile.gettempdir()) / "tw-recorder.health")))
# Wie alt die Heartbeat-Datei maximal sein darf, bevor --healthcheck als
# "unhealthy" gilt. Grosszügig über dem run_daemon()-Zyklus (5s) bemessen.
HEALTH_STALE_SECONDS = int(os.getenv("HEALTH_STALE_SECONDS", "60"))

# Globale Variablen für das Live-Reload-Tracking
LAST_CONFIG_HASH = ""
LAST_FILES_STATE = {}

running_threads = {}

# Globale Variablen für Token-Caching
app_token = None
token_expires_at = 0
token_lock = threading.Lock()

# Verhindert wiederholte API-Anfragen und Warnungen bei einem vorübergehenden
# Ausfall des Twitch-Helix-Endpunkts.
api_unavailable_until = 0.0
api_warning_lock = threading.Lock()


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


def _resolve_log_file_path(app_name: str, explicit_path: str) -> Path:
    """
    Ermittelt den Ziel-Pfad für die rotierende Logdatei nach Priorität:
    1. Explizite Konfiguration (Config-Datei oder ENV-Variable LOG_FILE)
    2. /log/<app_name>.log, falls /log existiert (Docker-Volume-Konvention)
    3. /var/log/<app_name>/<app_name>.log, falls anlegbar/beschreibbar (FHS)
    4. Fallback: Datei im Programmverzeichnis selbst
    """
    if explicit_path:
        return Path(explicit_path)

    docker_log_dir = Path("/log")
    if docker_log_dir.is_dir():
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
    - Zusätzlich ein rotierender Datei-Handler (max. 10 MB, 5 Backups),
      ausgelöst durch: explizite log_file-Konfiguration, ein vorhandenes
      /log-Verzeichnis (Docker-Volume-Konvention), oder einen tatsächlich
      laufenden klassischen Syslog-Daemon. Ohne einen dieser Gründe läuft
      die Ausgabe ohnehin bereits über journald (stdout-Erfassung) - eine
      eigene Logdatei wäre dann nur doppelte Datenhaltung ohne Mehrwert.
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

    docker_log_dir_present = Path("/log").is_dir()
    syslog_running = _is_syslog_daemon_running()

    # Datei-Logging wird ausgelöst durch (a) explizite Konfiguration, (b) die
    # Docker-Volume-Konvention /log (klares Signal, dass Logdateien gewünscht
    # sind - unabhängig davon, ob im Container selbst ein Syslog-Daemon läuft,
    # was in Containern ohnehin unüblich ist), oder (c) einen tatsächlich
    # laufenden klassischen Syslog-Daemon auf Bare-Metal-/systemd-Systemen.
    # Ohne einen dieser drei Gründe läuft die Ausgabe ohnehin schon über
    # journald/docker logs (stdout-Erfassung) - eine eigene Logdatei wäre
    # dann nur doppelte Datenhaltung ohne Mehrwert.
    if explicit_path:
        trigger_reason = "explizite log_file-Konfiguration"
    elif docker_log_dir_present:
        trigger_reason = "/log-Verzeichnis gefunden (Docker-Volume-Konvention)"
    elif syslog_running:
        trigger_reason = "Syslog-Daemon erkannt"
    else:
        trigger_reason = None

    if trigger_reason is not None:
        log_path = _resolve_log_file_path(app_name, explicit_path)

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
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


def get_config_files_state() -> dict:
    """Erstellt ein Mapping von Dateipfad zu MD5-Hash für die Haupt-Config und conf.d."""
    files_state = {}
    config_files = []

    if CONFIG_FILE.is_file():
        config_files.append(CONFIG_FILE)
    if CONF_D_DIR.is_dir():
        config_files.extend(sorted(CONF_D_DIR.glob("*.conf")))

    for f in config_files:
        try:
            files_state[f] = hashlib.md5(f.read_bytes(), usedforsecurity=False).hexdigest()
        except OSError:
            pass

    return files_state


def get_config_hash() -> tuple[str, dict]:
    """Berechnet den Gesamt-Hash und gibt den detaillierten Status aller Dateien zurück."""
    state = get_config_files_state()
    combined = hashlib.md5(usedforsecurity=False)
    for f in sorted(state.keys()):
        combined.update(f.name.encode("utf-8"))
        combined.update(state[f].encode("utf-8"))
    return combined.hexdigest(), state


def load_config():
    """Liest die INI-Konfigurationsdatei und ein conf.d-Verzeichnis ein mit Fallback auf Environment Variables."""
    config = configparser.ConfigParser(
        interpolation=None,
        delimiters=('=',),
        comment_prefixes=('#', ';'),
        allow_no_value=True
    )
    config.optionxform = str

    # 1. Sammle alle gültigen Config-Dateien in der gewünschten Ladereihenfolge
    config_files = []

    # Haupt-ConfigFile zuerst
    if CONFIG_FILE.is_file():
        config_files.append(CONFIG_FILE)

    # conf.d-Verzeichnis scannen (alphabetisch sortiert)
    if CONF_D_DIR.is_dir():
        conf_d_files = sorted(CONF_D_DIR.glob("*.conf"))
        config_files.extend(conf_d_files)

    # 2. Dateien einlesen (spätere Dateien überschreiben frühere Werte)
    if config_files:
        try:
            config.read(config_files, encoding="utf-8")
        except (OSError, configparser.Error, UnicodeDecodeError) as e:
            logger.warning(f"Fehler beim Lesen der Config-Dateien: {e}")

    # General / Output Settings
    storage_dir = Path(config.get("general", "storage_dir", fallback=os.getenv("STORAGE_DIR", "/storage")))
    sleep_interval = int(config.get("general", "sleep_interval", fallback=os.getenv("SLEEP_INTERVAL", "15")))
    filename_pattern = config.get("general", "filename_pattern", fallback=os.getenv("OUTPUT_FILENAME_PATTERN", "{time:%Y-%m-%d_%H-%M}_{channel}_{title}.mkv"))
    log_file = config.get("general", "log_file", fallback=os.getenv("LOG_FILE", "")).strip()

    # Twitch Credentials
    client_id = config.get("twitch", "client_id", fallback=os.getenv("CLIENT_ID", os.getenv("TWITCH_CLIENT_ID", ""))).strip()
    client_secret = config.get("twitch", "client_secret", fallback=os.getenv("CLIENT_SECRET", os.getenv("TWITCH_CLIENT_SECRET", ""))).strip()
    user_token = config.get("twitch", "user_token", fallback=os.getenv("TWITCH_USER_TOKEN", "")).replace("oauth:", "").strip()

    # Streamlink Defaults
    default_quality = config.get("streamlink", "stream_quality", fallback=os.getenv("STREAM_QUALITY", "best"))
    streamlink_loglevel = config.get("streamlink", "loglevel", fallback=os.getenv("STREAMLINK_LOGLEVEL", "info"))
    streamlink_retry = int(config.get("streamlink", "retry", fallback=os.getenv("STREAMLINK_RETRY", "30")))
    webbrowser_enabled = config.getboolean("streamlink", "webbrowser", fallback=os.getenv("STREAMLINK_WEBBROWSER", "false").lower() == "true")

    # Split der Aufnahme bei Titel-/Kategorieänderung (nur mit Twitch API möglich)
    split_on_title_change = config.getboolean(
        "streamlink", "split_on_title_change",
        fallback=os.getenv("SPLIT_ON_TITLE_CHANGE", "false").lower() == "true"
    )
    title_check_interval = int(config.get(
        "streamlink", "title_check_interval",
        fallback=os.getenv("TITLE_CHECK_INTERVAL", "90")
    ))

    # ionice für den FFmpeg-Remux nutzen (Idle-I/O-Klasse), damit der Remux
    # laufende Aufnahmen auf HDDs nicht ausbremst (nur relevant bei mehreren
    # gleichzeitigen Kanälen). Wird automatisch ignoriert, falls ionice nicht
    # installiert ist (z.B. auf Nicht-Linux-Systemen).
    use_ionice = config.getboolean(
        "streamlink", "use_ionice",
        fallback=os.getenv("USE_IONICE", "true").lower() == "true"
    )

    return {
        "storage_dir": storage_dir,
        "sleep_interval": sleep_interval,
        "filename_pattern": filename_pattern,
        "log_file": log_file,
        "client_id": client_id,
        "client_secret": client_secret,
        "user_token": user_token,
        "default_quality": default_quality,
        "streamlink_loglevel": streamlink_loglevel,
        "streamlink_retry": streamlink_retry,
        "webbrowser": webbrowser_enabled,
        "split_on_title_change": split_on_title_change,
        "title_check_interval": title_check_interval,
        "use_ionice": use_ionice,
        "config_obj": config
    }


def get_app_access_token(client_id: str, client_secret: str, force_refresh: bool = False) -> str | None:
    """Holt oder erneuert den Twitch OAuth2 App Access Token (Thread-sicher)."""
    global app_token, token_expires_at

    if not client_id or not client_secret:
        return None

    with token_lock:
        if app_token and time.time() < token_expires_at and not force_refresh:
            return app_token

        url = "https://id.twitch.tv/oauth2/token"
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials"
        }).encode("utf-8")

        req = urllib.request.Request(
            url, 
            data=params, 
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                app_token = data.get("access_token")
                token_expires_at = time.time() + data.get("expires_in", 3600) - 60
                return app_token
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Fehler beim Twitch-Token-Abruf: {e}")
            app_token = None
            return None


def get_extra_args(cfg: dict):
    args = []
    if cfg["user_token"]:
        args.extend(["--twitch-api-header", f"Authorization=OAuth {cfg['user_token']}"])
    if not cfg["webbrowser"]:
        args.append("--webbrowser=False")
    return args


def check_stream_online(url: str, cfg: dict, retry: bool = True) -> bool:
    """Prüft via Twitch Helix API, ob der Kanal live ist."""
    global api_unavailable_until

    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    if "twitch.tv" in url and client_id and client_secret:
        channel = url.rstrip("/").split("/")[-1].lower()
        with api_warning_lock:
            api_available = time.monotonic() >= api_unavailable_until

        token = get_app_access_token(client_id, client_secret) if api_available else None

        if token:
            api_url = f"https://api.twitch.tv/helix/streams?user_login={channel}"
            headers = {
                "Client-ID": client_id,
                "Authorization": f"Bearer {token}"
            }
            req = urllib.request.Request(api_url, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    streams = data.get("data", [])
                    return len(streams) > 0 and streams[0].get("type") == "live"
            except urllib.error.HTTPError as e:
                if e.code == 401 and retry:
                    get_app_access_token(client_id, client_secret, force_refresh=True)
                    return check_stream_online(url, cfg, retry=False)
                logger.warning(f"Twitch API HTTP-Fehler {e.code} für {channel}")
            except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
                with api_warning_lock:
                    api_unavailable_until = time.monotonic() + 60
                    logger.warning(
                        f"Twitch-API vorübergehend nicht erreichbar ({e}). "
                        "Verwende Streamlink-Fallback für 60 Sekunden."
                    )

    # Fallback für Plattformen außer Twitch oder fehlende Keys
    cmd = ["streamlink", "--stream-url"]
    cmd.extend(get_extra_args(cfg))
    cmd.extend([url, "best"])

    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return proc.returncode == 0


def get_stream_info(url: str, cfg: dict, retry: bool = True) -> dict | None:
    """
    Fragt via Twitch Helix API Titel/Kategorie des aktuell laufenden Streams ab.
    Wird für die Split-Erkennung bei Titel-/Kategorieänderung genutzt.

    Gibt bei Erfolg {"online": bool, "title": str, "category": str} zurück.
    Gibt None zurück, wenn keine Twitch-API-Zugangsdaten vorhanden sind, die
    Plattform kein Twitch ist, oder die API gerade nicht erreichbar ist.
    Verursacht nur 1 zusätzlichen Helix-API-Aufruf pro Aufruf (kostet 1 Punkt
    des 800-Punkte/Minute-Limits) - bei üblichen Prüfintervallen (>= 60s)
    vernachlässigbar.
    """
    global api_unavailable_until

    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    if not ("twitch.tv" in url and client_id and client_secret):
        return None

    channel = url.rstrip("/").split("/")[-1].lower()
    with api_warning_lock:
        api_available = time.monotonic() >= api_unavailable_until

    token = get_app_access_token(client_id, client_secret) if api_available else None
    if not token:
        return None

    api_url = f"https://api.twitch.tv/helix/streams?user_login={channel}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}"
    }
    req = urllib.request.Request(api_url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            streams = data.get("data", [])
            if streams and streams[0].get("type") == "live":
                return {
                    "online": True,
                    "title": streams[0].get("title") or "",
                    "category": streams[0].get("game_name") or "",
                }
            return {"online": False, "title": "", "category": ""}
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            get_app_access_token(client_id, client_secret, force_refresh=True)
            return get_stream_info(url, cfg, retry=False)
        logger.warning(f"Twitch API HTTP-Fehler {e.code} für {channel} (Titel-Check)")
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
        with api_warning_lock:
            api_unavailable_until = time.monotonic() + 60
            logger.warning(
                f"Twitch-API vorübergehend nicht erreichbar ({e}). "
                "Titel-Split-Erkennung pausiert für 60 Sekunden."
            )
        return None


def _pump_subprocess_output(proc: subprocess.Popen, channel: str) -> None:
    """
    Liest die kombinierte stdout/stderr-Ausgabe eines Subprozesses zeilenweise
    und reicht sie ans Logging-System weiter, statt sie direkt auf den rohen
    Prozess-stdout/stderr durchzureichen. Ohne das würde Streamlinks Ausgabe
    am konfigurierten Datei-Handler vorbeilaufen (nur im rohen Terminal-Stream
    sichtbar, nie in der Logdatei) - genau die Information, die man bei einer
    fehlgeschlagenen Aufnahme im Nachhinein braucht.
    Läuft in einem eigenen Daemon-Thread und endet automatisch, sobald der
    Subprozess sein stdout schließt (Prozessende).
    """
    try:
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip()
            if line:
                logger.info(f"[streamlink/{channel}] {line}")
    except (OSError, ValueError):
        # Pipe kann geschlossen worden sein (z.B. proc.kill() während des Lesens) - unkritisch.
        pass


def record_loop(url: str, quality: str, stop_event: threading.Event):
    channel = url.rstrip("/").split("/")[-1]

    # Config nur bei tatsächlicher Änderung neu laden (gleicher Hash-Mechanismus
    # wie beim Daemon selbst und bei der Titel-Split-Erkennung weiter unten),
    # statt bei jedem Poll-Tick (alle sleep_interval Sekunden) blind neu zu
    # parsen - besonders bei vielen gleichzeitig überwachten Kanälen spart das
    # unnötige Datei-I/O.
    cfg = load_config()
    last_outer_cfg_hash, _ = get_config_hash()

    while not stop_event.is_set():
        current_outer_cfg_hash, _ = get_config_hash()
        if current_outer_cfg_hash != last_outer_cfg_hash:
            last_outer_cfg_hash = current_outer_cfg_hash
            cfg = load_config()

        storage_dir = cfg["storage_dir"]
        sleep_interval = cfg["sleep_interval"]
        pattern_tmpl = cfg["filename_pattern"]

        out_dir = storage_dir / channel
        out_dir.mkdir(parents=True, exist_ok=True)

        lock_file = out_dir / ".record.lock"

        # Streamlink nimmt rohen Transport-Stream auf (.ts)
        out_pattern = str(out_dir / "{time:%Y-%m-%d_%H-%M}_{author}_{category}_{title}.ts")

        if check_stream_online(url, cfg):
            lock_fd = None
            if HAS_FCNTL:
                try:
                    lock_fd = open(lock_file, "a+")
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (IOError, OSError):
                    if lock_fd is not None and not lock_fd.closed:
                        lock_fd.close()
                    time.sleep(sleep_interval)
                    continue

            title_split_triggered = False
            try:
                logger.info(f"🔴 Starte Aufzeichnung für Kanal: {channel}")

                cmd = [
                    "streamlink",
                    "--loglevel", cfg["streamlink_loglevel"],
                    "--fs-safe-rules", "POSIX",
                ]
                cmd.extend(get_extra_args(cfg))
                cmd.extend([
                    "-o", out_pattern,
                    url,
                    quality
                ])

                # Enthält ggf. den OAuth-Token (--twitch-api-header) - daher
                # ausschließlich auf DEBUG-Level, nie auf INFO oder höher.
                logger.debug(f"Streamlink-Kommando für {channel}: {cmd}")

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                output_thread = threading.Thread(
                    target=_pump_subprocess_output,
                    args=(proc, channel),
                    daemon=True
                )
                output_thread.start()

                split_on_title_change = cfg["split_on_title_change"]
                title_check_interval = cfg["title_check_interval"]

                # Aktuellen Titel/Kategorie als Referenz für die Split-Erkennung merken
                last_title = None
                last_category = None
                if split_on_title_change:
                    initial_info = get_stream_info(url, cfg)
                    if initial_info and initial_info["online"]:
                        last_title = initial_info["title"]
                        last_category = initial_info["category"]
                last_title_check = time.monotonic()
                last_cfg_reload = time.monotonic()
                last_cfg_hash, _ = get_config_hash()

                while proc.poll() is None:
                    if stop_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break

                    # Nutzt denselben Hash-Mechanismus wie der Daemon (sync_processes),
                    # um Config-Dateien nur bei tatsächlicher Änderung neu einzulesen,
                    # statt sie alle paar Sekunden blind neu zu parsen. So greifen
                    # Änderungen an split_on_title_change/title_check_interval sofort,
                    # auch während einer laufenden (u.U. mehrstündigen) Aufnahme.
                    if (time.monotonic() - last_cfg_reload) >= sleep_interval:
                        last_cfg_reload = time.monotonic()
                        current_cfg_hash, _ = get_config_hash()
                        if current_cfg_hash != last_cfg_hash:
                            last_cfg_hash = current_cfg_hash
                            cfg = load_config()
                            split_on_title_change = cfg["split_on_title_change"]
                            title_check_interval = cfg["title_check_interval"]
                            if split_on_title_change and last_title is None:
                                # Feature wurde gerade erst aktiviert -> Referenz-
                                # Titel/Kategorie jetzt nachträglich ermitteln
                                initial_info = get_stream_info(url, cfg)
                                if initial_info and initial_info["online"]:
                                    last_title = initial_info["title"]
                                    last_category = initial_info["category"]

                    if (
                        split_on_title_change
                        and last_title is not None
                        and (time.monotonic() - last_title_check) >= title_check_interval
                    ):
                        last_title_check = time.monotonic()
                        info = get_stream_info(url, cfg)
                        if info and info["online"] and (
                            info["title"] != last_title or info["category"] != last_category
                        ):
                            logger.info(
                                f"✂️ Titel-/Kategorieänderung erkannt für {channel} "
                                f"('{last_title}'/'{last_category}' -> "
                                f"'{info['title']}'/'{info['category']}'). Splitte Aufnahme."
                            )
                            proc.terminate()
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            title_split_triggered = True
                            break

                    time.sleep(1)

                logger.info(f"⏹️ Aufzeichnung beendet für Kanal: {channel}. Remuxe Datei...")
                time.sleep(2)

                try:
                    ts_files = sorted(out_dir.glob("*.ts"), key=lambda f: f.stat().st_mtime, reverse=True)
                    if not ts_files:
                        logger.warning(
                            f"Kein .ts-File für {channel} in {out_dir} gefunden - "
                            "Remuxing übersprungen (Aufnahme evtl. zu kurz/fehlgeschlagen)."
                        )
                    else:
                        latest_file = ts_files[0]
                        age_sec = time.time() - latest_file.stat().st_mtime
                        if age_sec >= 300:
                            logger.warning(
                                f"Neueste .ts-Datei für {channel} ({latest_file.name}) ist "
                                f"{age_sec:.0f}s alt (Limit: 300s) - vermutlich von einem "
                                "früheren Lauf, Remuxing übersprungen."
                            )
                        else:

                            full_stem = latest_file.stem

                            # Zuerst Datum und Uhrzeit abtrennen (enthalten nie
                            # Unterstriche, nur Bindestriche -> sicher via split(_, 2))
                            dt_parts = full_stem.split("_", 2)
                            date_str = dt_parts[0] if len(dt_parts) > 0 and dt_parts[0] else "0000-00-00"
                            time_str = dt_parts[1] if len(dt_parts) > 1 and dt_parts[1] else "00-00"
                            remainder = dt_parts[2] if len(dt_parts) > 2 else ""

                            # Kanalname ist bereits bekannt (aus der URL) und damit
                            # zuverlässiger als ein Re-Parsing des Dateinamens per
                            # Unterstrich-Split (Twitch-Namen dürfen "_" enthalten,
                            # was das alte Split-Verfahren fälschlich abschnitt).
                            parsed_channel = channel
                            channel_prefix = f"{channel}_"
                            if remainder.startswith(channel_prefix):
                                rest_after_channel = remainder[len(channel_prefix):]
                            else:
                                # Unerwartetes Format (z.B. Streamlink hat den Namen
                                # anders geschrieben) -> best effort wie zuvor
                                rest_after_channel = remainder

                            cat_title_parts = rest_after_channel.split("_", 1)
                            raw_category = cat_title_parts[0] if len(cat_title_parts) > 0 else ""
                            category = raw_category.strip() if raw_category.strip() else "NoCategory"
                            category = re.sub(r'[/\\:*?"<>|]', '_', category)
                            category = re.sub(r'[\s_]+', '_', category).strip('_')
                            if not category:
                                category = "NoCategory"


                            raw_title = cat_title_parts[1].strip() if len(cat_title_parts) > 1 else ""
                            full_title = raw_title if raw_title else "Untitled"

                            # 1. Kürzel <3 durch ein echtes Unicode-Herz ersetzen
                            full_title = full_title.replace("<3", "♥")

                            # 2. Übrige einzelne < und > Zeichen ersatzlos entfernen
                            full_title = re.sub(r'[<>]', '', full_title)

                            # Sanitize title: Bewahrt Unicode/Umlaute, filtert nur echte Pfad-Sonderzeichen
                            safe_title = "".join(
                                char for char in full_title
                                if unicodedata.category(char) not in {"So", "Sk", "Cf"} or char == "♥"
                            )

                            safe_title = re.sub(r'[/\\:*?"<>|]', '_', safe_title)
                            safe_title = re.sub(r'[\s_]+', '_', safe_title).strip('_')
                            if not safe_title:
                                safe_title = "Untitled"

                            # Datum als datetime-Objekt parsen für volle Kompatibilität mit {time:...} Formaten
                            try:
                                dt_obj = datetime.strptime(f"{date_str}_{time_str}", "%Y-%m-%d_%H-%M")
                            except ValueError:
                                dt_obj = datetime.now()

                            try:
                                target_filename = pattern_tmpl.format(
                                    time=dt_obj,
                                    channel=parsed_channel,
                                    author=parsed_channel,  # Alias für Streamlink-Kompatibilität
                                    title=safe_title,
                                    category=category
                                )
                            except (KeyError, ValueError) as e:
                                logger.warning(f"Fehler beim Formatieren von filename_pattern ({e}). Nutze Fallback-Name.")
                                target_filename = f"{date_str}_{time_str}_{parsed_channel}_{category}_{safe_title}.mkv"

                            # Falls das Pattern keine Endung hat, erzwinge .mkv
                            final_target_path = out_dir / target_filename
                            if not final_target_path.suffix:
                                final_target_path = final_target_path.with_suffix(".mkv")

                            meta_date_compact = date_str.replace("-", "") if date_str else "00000000"

                            clean_title_meta = "".join(
                                c for c in full_title 
                                if c.isalnum() or c in (" ", "-", "_", "!", "?", ".", "♥")
                            ).strip()

                            clean_title_meta = re.sub(r'\s+', ' ', clean_title_meta)
                            prefix = f"{parsed_channel} - {category}: "
                            suffix = f" ({date_str})"

                            raw_meta_title = f"{prefix}{clean_title_meta}{suffix}"

                            if len(raw_meta_title) > 95:
                                max_title_len = 95 - len(prefix) - len(suffix) - 3
                                if max_title_len > 0:
                                    clean_title_meta = clean_title_meta[:max_title_len].strip() + "..."
                                else:
                                    clean_title_meta = "..."
                                meta_title = f"{prefix}{clean_title_meta}{suffix}"
                            else:
                                meta_title = raw_meta_title

                            meta_comment = full_title

                            ffmpeg_cmd = [
                                "ffmpeg", "-loglevel", "error",
                                "-i", str(latest_file),
                                "-metadata", f"TITLE={meta_title}",
                                "-metadata", f"COMMENT={meta_comment}",
                                "-metadata", f"ARTIST={parsed_channel}",
                                "-metadata", f"PURL=https://twitch.tv/{parsed_channel}",
                                "-metadata", f"DATE={meta_date_compact}",
                                "-metadata", f"creation_time={meta_date_compact}",
                                "-map", "0:v",
                                "-map", "0:a?",
                                "-c", "copy",
                                str(final_target_path)
                            ]

                            if cfg["use_ionice"] and shutil.which("ionice"):
                                # Idle-I/O-Klasse (-c3): Remux bekommt nur Platten-
                                # I/O, wenn gerade nichts anderes (v.a. laufende
                                # Aufnahmen auf anderen Kanälen) es benötigt.
                                ffmpeg_cmd = ["ionice", "-c3"] + ffmpeg_cmd

                            ff_proc = subprocess.run(ffmpeg_cmd)
                            if ff_proc.returncode == 0:
                                if latest_file.exists():
                                    latest_file.unlink()
                                logger.info(f"✅ Erfolgreich remuxed: {final_target_path.name}")
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                    logger.warning(f"Fehler beim FFmpeg-Remuxing für {channel}: {e}")

            except Exception as e:  # noqa: BLE001 - Top-Level-Boundary für den gesamten Aufnahme-Zyklus (Streamlink-Start, Monitoring, Remux); jeder Fehler hier darf den Überwachungs-Thread für diesen Kanal nicht dauerhaft beenden
                # Fängt z.B. Fehler beim Starten von streamlink (Popen) oder
                # sonstige unerwartete Fehler ab, damit der Überwachungs-Thread
                # für diesen Kanal nicht dauerhaft stirbt.
                logger.warning(f"Unerwarteter Fehler bei der Aufnahme für {channel}: {e}")

            finally:
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        lock_fd.close()
                    except OSError:
                        pass

                    lock_file.unlink(missing_ok=True)

            if stop_event.is_set():
                return

            if title_split_triggered:
                # Stream läuft nachweislich weiter (nur Titel/Kategorie geändert) ->
                # sofort neue Aufnahme starten, keine künstliche Pause nötig.
                logger.info(f"▶️ Starte nächsten Aufnahme-Abschnitt für Kanal: {channel}")
                continue

            time.sleep(30)

        time.sleep(sleep_interval)


def parse_streamers():
    """Liest die Kanäle aus der [channels] Sektion der INI-Datei."""
    streamers = {}
    cfg = load_config()
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

    logger.info(f"Starte Streamlink-Manager (Config: {CONFIG_FILE})...")

    # Initialen Zustand beim Start erfassen
    LAST_CONFIG_HASH, LAST_FILES_STATE = get_config_hash()
    sync_processes()

    while True:
        write_heartbeat()
        current_hash, current_state = get_config_hash()

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


def write_heartbeat():
    """
    Aktualisiert die Heartbeat-Datei mit dem aktuellen Zeitstempel. Wird bei
    jedem Durchlauf der run_daemon()-Hauptschleife aufgerufen. Schlägt der
    Schreibvorgang fehl (z.B. kein Schreibzugriff auf /tmp), wird das nur auf
    DEBUG geloggt - ein Healthcheck-Problem soll den Recorder nicht stören.
    """
    try:
        HEALTH_FILE.write_text(str(time.time()))
    except OSError as e:
        logger.debug(f"Konnte Heartbeat-Datei {HEALTH_FILE} nicht schreiben: {e}")


def run_healthcheck() -> int:
    """
    Leichtgewichtige Prüfung für externe Healthchecks (z.B. Docker HEALTHCHECK
    oder Kubernetes livenessProbe): prüft nur, ob die Heartbeat-Datei existiert
    und nicht älter als HEALTH_STALE_SECONDS ist. Lädt bewusst keine Config,
    damit der Aufruf schnell ist und keine Nebenwirkungen hat (wird
    typischerweise alle paar Sekunden aufgerufen).
    Gibt 0 (healthy) oder 1 (unhealthy) zurück, analog zu Exit-Codes.
    """
    if not HEALTH_FILE.is_file():
        print(f"UNHEALTHY: Heartbeat-Datei {HEALTH_FILE} nicht gefunden (Dämon noch nicht gestartet?).")
        return 1

    try:
        age = time.time() - HEALTH_FILE.stat().st_mtime
    except OSError as e:
        print(f"UNHEALTHY: Heartbeat-Datei {HEALTH_FILE} nicht lesbar: {e}")
        return 1

    if age > HEALTH_STALE_SECONDS:
        print(f"UNHEALTHY: Heartbeat ist {age:.0f}s alt (Limit: {HEALTH_STALE_SECONDS}s).")
        return 1

    print(f"HEALTHY: Heartbeat ist {age:.0f}s alt.")
    return 0


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

    cfg = load_config()
    setup_logging(cfg)
    run_daemon()


if __name__ == "__main__":
    main()
