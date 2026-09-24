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


# STORAGE_DIR zeigt einheitlich (Docker wie Bare-Metal) auf das gemeinsame
# Uebergabeverzeichnis der Pipeline statt auf einen rein privaten,
# Package-relativen Pfad oder den inzwischen abgeloesten /storage-Mountpunkt:
# tw-recorder ist hier nur der Schreiber, fetchbridge liest von genau
# demselben Pfad (dessen SOURCE_DIR) weiter - identisch zum Docker-Compose-
# Setup, wo beide Container denselben Host-Pfad mounten. Das .deb-Postinst
# legt dieses Verzeichnis mit einer gemeinsamen Gruppe an, damit beide
# Systemuser (tw-recorder, fetchbridge) tatsaechlich zugreifen koennen.
STORAGE_DIR = Path("/srv/media-pipeline/recordings")

# Heartbeat-Datei für den Healthcheck (z.B. Docker HEALTHCHECK). run_daemon()
# aktualisiert sie bei jedem Schleifendurchlauf; ein separater, sehr
# leichtgewichtiger Aufruf desselben Skripts (--healthcheck) prüft nur, ob
# sie frisch genug ist - ohne Config zu laden, damit der Aufruf schnell ist
# und keine Nebenwirkungen hat.
HEALTH_FILE = Path(os.getenv("HEALTH_FILE", str(Path(tempfile.gettempdir()) / "tw-recorder.health")))
# Wie alt die Heartbeat-Datei maximal sein darf, bevor --healthcheck als
# "unhealthy" gilt. Grosszügig über dem run_daemon()-Zyklus (5s) bemessen.
HEALTH_STALE_SECONDS = int(os.getenv("HEALTH_STALE_SECONDS", "60"))


def _load_credentials_directory_env(credential_name: str = "twitch-secrets") -> dict:
    """
    Liest optional eine per systemd LoadCredentialEncrypted= entschlüsselte
    KEY=VALUE-Datei direkt aus $CREDENTIALS_DIRECTORY (siehe README,
    "Twitch-Credentials mit systemd-creds verschlüsseln") ein.

    Bewusst NICHT über EnvironmentFile=%d/... gelöst: das hat sich real als
    unzuverlässig erwiesen (der %d-Specifier wurde dort nicht wie erwartet
    aufgelöst, Symptom war "Failed to load environment files"/eine
    Restart-Crashloop des gesamten Diensts). Direktes Einlesen der Datei
    entspricht stattdessen dem von systemd selbst empfohlenen Zugriffsmuster
    (siehe systemd/systemd docs/CREDENTIALS.md: Anwendungscode liest
    $CREDENTIALS_DIRECTORY/<name> selbst) und vermeidet zusätzlich den Umweg
    über echte Prozess-Umgebungsvariablen, die über /proc/<pid>/environ für
    andere hinreichend privilegierte Prozesse einsehbar wären.

    Gibt bei fehlendem $CREDENTIALS_DIRECTORY oder fehlender/nicht lesbarer
    Datei ein leeres dict zurück - kein Fehlerfall, das Feature ist rein
    optional und muss bare-metal-Installationen ohne systemd-creds nicht
    betreffen.
    """
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credentials_dir:
        return {}

    try:
        lines = (Path(credentials_dir) / credential_name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    result = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


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

    # 2. Dateien einzeln einlesen (spätere Dateien überschreiben frühere Werte).
    # Bewusst NICHT config.read(config_files, ...) mit der ganzen Liste auf
    # einmal: ConfigParser.read() bricht beim ersten Parse-Fehler in der Liste
    # komplett ab, wodurch jede DANACH folgende Datei (auch eine gültige!)
    # stillschweigend gar nicht mehr gelesen wird - ein Tippfehler in einer
    # frühen conf.d-Datei würde sonst alle alphabetisch späteren unsichtbar
    # deaktivieren, ohne dass das aus der Warnung ersichtlich wäre.
    # Und bewusst config.read_file() auf einem selbst geöffneten Handle statt
    # config.read(f, ...): Letzteres öffnet die Datei intern und FÄNGT einen
    # dabei auftretenden OSError (z.B. Permission denied, falls eine
    # conf.d-Datei versehentlich dem falschen User/einer falschen Gruppe
    # gehört) STILL AB, ohne ihn je an aufrufenden Code durchzureichen - das
    # except unten würde so einen Fall nie sehen, die Datei würde ohne jede
    # Warnung einfach ignoriert. Mit dem eigenen open() landet ein
    # Berechtigungsfehler dagegen in unserem eigenen except.
    for f in config_files:
        try:
            with open(f, encoding="utf-8") as fp:
                config.read_file(fp, source=str(f))
        except (OSError, configparser.Error, UnicodeDecodeError) as e:
            logger.warning(f"Fehler beim Lesen von {f}: {e}")

    # General / Output Settings
    storage_dir = Path(config.get("general", "storage_dir", fallback=os.getenv("STORAGE_DIR", str(STORAGE_DIR))))
    sleep_interval = int(config.get("general", "sleep_interval", fallback=os.getenv("SLEEP_INTERVAL", "15")))
    filename_pattern = config.get("general", "filename_pattern", fallback=os.getenv("OUTPUT_FILENAME_PATTERN", "{time:%Y-%m-%d_%H-%M}_{channel}_{title}.mkv"))
    log_file = config.get("general", "log_file", fallback=os.getenv("LOG_FILE", "")).strip()

    # Twitch Credentials - Fallback-Reihenfolge: Config-Datei -> systemd-creds
    # ($CREDENTIALS_DIRECTORY/twitch-secrets, siehe _load_credentials_directory_env())
    # -> Env-Var direkt.
    credential_env = _load_credentials_directory_env()
    client_id = config.get(
        "twitch", "client_id",
        fallback=credential_env.get("CLIENT_ID") or os.getenv("CLIENT_ID", os.getenv("TWITCH_CLIENT_ID", ""))
    ).strip()
    client_secret = config.get(
        "twitch", "client_secret",
        fallback=credential_env.get("CLIENT_SECRET") or os.getenv("CLIENT_SECRET", os.getenv("TWITCH_CLIENT_SECRET", ""))
    ).strip()
    user_token = config.get(
        "twitch", "user_token",
        fallback=credential_env.get("TWITCH_USER_TOKEN") or os.getenv("TWITCH_USER_TOKEN", "")
    ).replace("oauth:", "").strip()

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

    Prüft dazu bewusst über config_obj.has_option(), ob client_secret/
    user_token TATSÄCHLICH in einer Config-DATEI stehen - nicht nur, ob der
    aufgelöste Wert (cfg["client_secret"]/cfg["user_token"]) nicht leer ist,
    denn der kann auch über den Env-Fallback kommen (CLIENT_SECRET/
    TWITCH_CLIENT_SECRET/TWITCH_USER_TOKEN, z.B. via systemd-creds/
    LoadCredentialEncrypted= + EnvironmentFile=, siehe README). Ein reiner
    Wert-Check würde recorder.confs eigene Dateirechte fälschlich bemängeln,
    obwohl die Datei nach einer solchen Migration gar kein Secret mehr enthält
    und ihre Rechte damit irrelevant sind (derselbe Fehlalarm-Bug wie kürzlich
    in yt-uploads get_access_token(), nur mit anderer Ursache: dort kannte der
    Check keine ACLs, hier würde er die falsche Datei prüfen).

    Anders als bei yt-uploads selbst geschriebenen OAuth-Credentials wird hier
    nur gewarnt statt aktiv korrigiert: die Datei wird per (meist :ro-)
    Bind-Mount vom Host verwaltet, ein chmod von innen würde bei einem
    Read-Only-Mount ohnehin fehlschlagen und wäre außerdem host-seitig wieder
    verloren. Soll von Aufrufern nur nach einer tatsächlichen Config-Änderung
    aufgerufen werden (z.B. run_daemon()s Hash-Vergleich), nicht bei jedem
    Poll-Tick, um Log-Spam zu vermeiden.
    """
    cfg = load_config()
    config_obj = cfg["config_obj"]
    if not (config_obj.has_option("twitch", "client_secret") or config_obj.has_option("twitch", "user_token")):
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
