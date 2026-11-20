"""
Laufende Aufnahme gezielt abbrechen und denselben Stream danach überspringen.

`tw-recorder --skip KANAL` legt eine kleine Marker-Datei in SKIP_DIR an
(unter /var/run/tw-recorder, d.h. tmpfs: ein Reboot löscht sie von selbst).
Der Dämon sieht den Marker, beendet eine laufende Aufnahme per SIGTERM an
streamlink und bindet den Marker an die Twitch-Stream-ID des aktuellen
Streams. Solange genau dieser Stream läuft, wird nicht neu aufgenommen -
auch nicht nach einem Dienst-Neustart (RuntimeDirectoryPreserve=yes in der
Unit). Startet der Streamer einen neuen Stream (neue ID), löscht der Dämon
den Marker und nimmt wieder ganz normal auf.

Ohne Twitch-API-Zugangsdaten gibt es keine Stream-ID; der Skip gilt dann bis
zum Reboot bzw. bis `--unskip`.

Rechte (least privilege): SKIP_DIR legt der Dämon selbst an, es gehört dem
Dienstuser (0755). Marker anlegen/löschen können damit nur root und der
Dienstuser - genau wie bei der Heartbeat-Datei kann ein anderer lokaler User
nichts unterschieben.
"""

import json
import logging
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from tw_recorder import config

logger = logging.getLogger(__name__)

# Twitch-Logins: 1-25 Zeichen aus Buchstaben, Ziffern und "_" - verhindert
# zugleich jeden Pfad-Trick ("../", "/") im Marker-Dateinamen.
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")

_TWITCH_PREFIXES = (
    "https://www.twitch.tv/", "http://www.twitch.tv/", "www.twitch.tv/",
    "https://twitch.tv/", "http://twitch.tv/", "twitch.tv/",
)

# Gemeinsame Kennung für "Marker existiert, ist aber noch an keinen Stream gebunden"
UNBOUND = None


def normalize_channel(raw: str) -> str | None:
    """Kanalname oder Twitch-URL -> kleingeschriebener Login, None wenn ungültig."""
    text = (raw or "").strip()
    for prefix in _TWITCH_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip("/")
    if not _CHANNEL_RE.match(text):
        return None
    return text.lower()


def _marker_path(channel: str) -> Path:
    return config.SKIP_DIR / f"{channel}.skip"


def ensure_skip_dir() -> bool:
    """
    Legt SKIP_DIR beim Dämonstart an (gehört damit dem Dienstuser) und prüft,
    dass es ein echtes, eigenes Verzeichnis ist (kein Symlink, kein fremdes
    Verzeichnis im gemeinsamen /tmp). Gibt False zurück, wenn die
    Skip-Funktion deshalb deaktiviert bleiben muss.
    """
    try:
        config.SKIP_DIR.mkdir(mode=0o755, exist_ok=True)
        st = config.SKIP_DIR.lstat()
    except OSError as e:
        logger.warning(f"Skip-Verzeichnis {config.SKIP_DIR} nicht verfügbar, --skip wirkungslos: {e}")
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        logger.warning(
            f"Skip-Verzeichnis {config.SKIP_DIR} ist kein eigenes Verzeichnis dieses Dienstusers "
            "- --skip bleibt aus Sicherheitsgründen wirkungslos."
        )
        return False
    return True


def read_marker(channel: str) -> dict | None:
    """Liest den Marker eines Kanals; None, wenn keiner (oder kein gültiger) existiert."""
    path = _marker_path(channel)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug(f"Skip-Marker {path} nicht lesbar: {e}")
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        # Halb geschriebene/kaputte Datei: trotzdem als Skip werten (der
        # Wunsch "nicht aufnehmen" ist eindeutig), nur ohne Zusatzinfos.
        logger.debug(f"Skip-Marker {path} unlesbar ({e}), werte ihn als ungebundenen Skip.")
        data = {}
    if not isinstance(data, dict):
        data = {}
    stream_id = data.get("stream_id")
    return {
        "stream_id": str(stream_id) if stream_id else UNBOUND,
        "keep": bool(data.get("keep", False)),
        "created": data.get("created"),
    }


def write_marker(channel: str, stream_id: str | None = UNBOUND, keep: bool = False) -> Path:
    """
    Schreibt den Marker atomar (temporäre Datei + os.replace im selben
    Verzeichnis), damit der Dämon nie eine halbe Datei liest. Wirft OSError,
    wenn SKIP_DIR fehlt (Dämon läuft nicht) oder nicht beschreibbar ist.
    """
    path = _marker_path(channel)
    payload = json.dumps({"stream_id": stream_id, "keep": keep, "created": int(time.time())})
    fd, tmp_name = tempfile.mkstemp(dir=config.SKIP_DIR, prefix=f".{channel}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def remove_marker(channel: str) -> bool:
    try:
        _marker_path(channel).unlink()
        return True
    except FileNotFoundError:
        return False


def is_requested(channel: str) -> bool:
    """Billiger Existenz-Check (ein lstat) für die Sekunden-Schleife während einer Aufnahme."""
    try:
        return stat.S_ISREG(_marker_path(channel).lstat().st_mode)
    except OSError:
        return False


def bind(channel: str, marker: dict, stream_id: str | None) -> None:
    """Bindet einen noch ungebundenen Marker an die Stream-ID des laufenden Streams."""
    if not stream_id or marker["stream_id"] is not UNBOUND:
        return
    try:
        write_marker(channel, stream_id=stream_id, keep=marker["keep"])
        marker["stream_id"] = stream_id
        logger.info(f"⏭️ Skip für {channel} gilt jetzt für Stream-ID {stream_id}.")
    except OSError as e:
        logger.warning(f"Konnte Skip-Marker für {channel} nicht an Stream {stream_id} binden: {e}")


def should_skip(channel: str, stream_id: str | None) -> bool:
    """
    Entscheidet für einen gerade live gesehenen Kanal, ob nicht aufgenommen
    werden soll. stream_id ist die aktuelle Twitch-Stream-ID (None ohne API).
    Ein ungebundener Marker wird dabei an diese ID gebunden; ein Marker mit
    anderer ID (= neuer Stream) wird gelöscht.
    """
    marker = read_marker(channel)
    if marker is None:
        return False
    if marker["stream_id"] is UNBOUND:
        bind(channel, marker, stream_id)
        return True
    if stream_id and stream_id != marker["stream_id"]:
        remove_marker(channel)
        logger.info(f"▶️ Neuer Stream von {channel} (ID {stream_id}) - Skip aufgehoben, nehme wieder auf.")
        return False
    return True


def list_markers() -> list[tuple[str, dict]]:
    try:
        names = sorted(p.name for p in config.SKIP_DIR.glob("*.skip"))
    except OSError:
        return []
    result = []
    for name in names:
        channel = name[:-len(".skip")]
        marker = read_marker(channel)
        if marker is not None:
            result.append((channel, marker))
    return result


# --- CLI-Seite (tw-recorder --skip/--unskip/--list-skips) -------------------

def _cli_dir_error() -> str:
    return (
        f"Skip-Verzeichnis {config.SKIP_DIR} fehlt oder ist nicht beschreibbar. "
        "Läuft der Dämon? Befehl als root oder als Dienstuser tw-recorder ausführen."
    )


def cli_skip(raw_channel: str, keep: bool) -> int:
    channel = normalize_channel(raw_channel)
    if channel is None:
        print(f"Ungültiger Kanalname: {raw_channel!r}")
        return 2
    if not config.SKIP_DIR.is_dir():
        print(_cli_dir_error())
        return 1
    try:
        write_marker(channel, keep=keep)
    except OSError as e:
        print(f"{_cli_dir_error()} ({e})")
        return 1
    what = "wird gemuxt und behalten" if keep else "wird verworfen"
    print(
        f"Skip für {channel} gesetzt. Eine laufende Aufnahme wird in wenigen Sekunden beendet "
        f"(bisheriger Teil {what}). Der aktuelle Stream wird nicht erneut aufgenommen, "
        "der nächste Stream wieder normal."
    )
    return 0


def cli_unskip(raw_channel: str) -> int:
    channel = normalize_channel(raw_channel)
    if channel is None:
        print(f"Ungültiger Kanalname: {raw_channel!r}")
        return 2
    try:
        removed = remove_marker(channel)
    except OSError as e:
        print(f"{_cli_dir_error()} ({e})")
        return 1
    print(f"Skip für {channel} aufgehoben." if removed else f"Für {channel} war kein Skip gesetzt.")
    return 0


def cli_list() -> int:
    markers = list_markers()
    if not markers:
        print("Keine Skips gesetzt.")
        return 0
    for channel, marker in markers:
        sid = marker["stream_id"] or "noch nicht gebunden"
        keep = "behalten" if marker["keep"] else "verwerfen"
        print(f"{channel}: Stream-ID {sid}, angefangener Teil: {keep}")
    return 0
