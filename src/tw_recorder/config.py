"""
Konfiguration & Standard-Pfade.

Enthält alle Laufzeit-Einstellungen als Modul-Globals sowie load_config()/
get_config_hash() für das Hot-Reload-Verhalten. Andere Module referenzieren
diese Werte als `config.NAME`, damit sie nach einer Änderung den aktuellen
Wert sehen statt einer beim Import eingefrorenen Kopie.
"""

import configparser
import contextlib
import hashlib
import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "tw-recorder"

# Standard-Konfigurationspfade
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/etc/tw-recorder/recorder.conf"))
CONF_D_DIR = Path(os.getenv("CONF_D_DIR", "/etc/tw-recorder/conf.d"))


def _is_dedicated_mount(path: Path) -> bool:
    """
    Prüft, ob path ein eigener Mountpoint ist (Docker-Volume/Bind-Mount) statt
    nur ein gewöhnliches Verzeichnis, das das Dockerfile per `mkdir -p` fest
    ins Image gebacken hat (z.B. /storage) - eine reine is_dir()-Prüfung kann
    diese beiden Fälle nicht unterscheiden, da `mkdir -p` das Verzeichnis auch
    ganz ohne jeden Mount anlegt (derselbe Bug wie bei yt-upload: Aufnahmen
    liefen unbemerkt gegen den flüchtigen Container-Layer statt auf ein
    tatsächlich persistentes Volume). Vergleicht dazu die Geräte-ID (st_dev)
    von path und seinem Elternverzeichnis: unterschiedliche st_dev bedeutet,
    dass dort tatsächlich ein Volume/Bind-Mount eingehängt ist.
    """
    if not path.is_dir():
        return False
    try:
        return path.stat().st_dev != path.parent.stat().st_dev
    except OSError:
        return False


# Dynamic Path Detection: Docker-Volume (/storage) vs. Bare-Metal-Host.
# _is_dedicated_mount() statt blosser Existenzpruefung, da das Dockerfile
# /storage unconditional per `mkdir -p` anlegt - ohne echtes Volume wuerde
# der Recorder sonst faelschlich "Container-Modus" annehmen und Aufnahmen in
# den fluechtigen Container-Layer statt auf einen Bare-Metal-Pfad schreiben.
#
# Der Bare-Metal-Fallback zeigt bewusst auf das gemeinsame Uebergabeverzeichnis
# der Pipeline (/srv/media-pipeline/recordings) statt auf einen rein privaten,
# Package-relativen Pfad: tw-recorder ist hier nur der Schreiber, fetchbridge
# liest von genau demselben Pfad (dessen SOURCE_DIR) weiter - identisch zum
# Docker-Compose-Setup, wo beide Container denselben Host-Pfad mounten. Das
# .deb-Postinst legt dieses Verzeichnis mit einer gemeinsamen Gruppe an, damit
# beide Systemuser (tw-recorder, fetchbridge) tatsaechlich zugreifen koennen.
STORAGE_DIR = Path("/storage") if _is_dedicated_mount(Path("/storage")) else Path("/srv/media-pipeline/recordings")

# Heartbeat-Datei für den Healthcheck (z.B. Docker HEALTHCHECK). run_daemon()
# aktualisiert sie bei jedem Schleifendurchlauf; ein separater, sehr
# leichtgewichtiger Aufruf desselben Skripts (--healthcheck) prüft nur, ob
# sie frisch genug ist - ohne Config zu laden, damit der Aufruf schnell ist
# und keine Nebenwirkungen hat.
HEALTH_FILE = Path(os.getenv("HEALTH_FILE", str(Path(tempfile.gettempdir()) / "tw-recorder.health")))
# Wie alt die Heartbeat-Datei maximal sein darf, bevor --healthcheck als
# "unhealthy" gilt. Grosszügig über dem run_daemon()-Zyklus (5s) bemessen.
HEALTH_STALE_SECONDS = int(os.getenv("HEALTH_STALE_SECONDS", "60"))


def get_config_files_state() -> dict:
    """Erstellt ein Mapping von Dateipfad zu MD5-Hash für die Haupt-Config und conf.d."""
    files_state = {}
    config_files = []

    if CONFIG_FILE.is_file():
        config_files.append(CONFIG_FILE)
    if CONF_D_DIR.is_dir():
        config_files.extend(sorted(CONF_D_DIR.glob("*.conf")))

    for f in config_files:
        with contextlib.suppress(OSError):
            files_state[f] = hashlib.md5(f.read_bytes(), usedforsecurity=False).hexdigest()

    return files_state


def get_config_hash() -> tuple[str, dict]:
    """Berechnet den Gesamt-Hash und gibt den detaillierten Status aller Dateien zurück."""
    state = get_config_files_state()
    combined = hashlib.md5(usedforsecurity=False)
    for f in sorted(state.keys()):
        combined.update(f.name.encode())
        combined.update(state[f].encode())
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
    storage_dir = Path(config.get("general", "storage_dir", fallback=os.getenv("STORAGE_DIR", str(STORAGE_DIR))))
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


def check_secrets_permissions():
    """
    Warnt, falls recorder.conf/conf.d Twitch-Credentials (client_secret/
    user_token) im Klartext enthält, aber für Gruppe/Andere lesbar ist.

    Anders als bei yt-uploads selbst geschriebenen OAuth-Credentials wird hier
    nur gewarnt statt aktiv korrigiert: die Datei wird per (meist :ro-)
    Bind-Mount vom Host verwaltet, ein chmod von innen würde bei einem
    Read-Only-Mount ohnehin fehlschlagen und wäre außerdem host-seitig wieder
    verloren. Soll von Aufrufern nur nach einer tatsächlichen Config-Änderung
    aufgerufen werden (z.B. run_daemon()s Hash-Vergleich), nicht bei jedem
    Poll-Tick, um Log-Spam zu vermeiden.
    """
    cfg = load_config()
    if not (cfg["client_secret"] or cfg["user_token"]):
        return

    config_files = []
    if CONFIG_FILE.is_file():
        config_files.append(CONFIG_FILE)
    if CONF_D_DIR.is_dir():
        config_files.extend(sorted(CONF_D_DIR.glob("*.conf")))

    for f in config_files:
        try:
            mode = f.stat().st_mode
        except OSError:
            continue
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(
                f"⚠️ {f} enthält Twitch-Zugangsdaten (client_secret/user_token) im Klartext "
                f"und ist für Gruppe/Andere lesbar (Modus {oct(stat.S_IMODE(mode))}). "
                "Empfehlung: auf dem Host `chmod 600` setzen, sofern der Mount das zulässt."
            )


def get_extra_args(cfg: dict):
    args = []
    if cfg["user_token"]:
        args.extend(["--twitch-api-header", f"Authorization=OAuth {cfg['user_token']}"])
    if not cfg["webbrowser"]:
        args.append("--webbrowser=False")
    return args
