#!/usr/bin/env python3
"""
EXPERIMENTELL - Prototyp, NICHT in den Daemon eingebaut.

Nimmt einen Stream über die Streamlink-*Python-API* (statt des
streamlink-CLI-Subprozesses) auf und reicht die MPEG-TS-Bytes per Pipe an
ffmpeg weiter, das direkt eine getaggte .mkv schreibt - ohne Zwischen-.ts und
ohne anschließenden Remux-Schritt.

Der bestehende Aufnahmepfad (recorder.record_loop() mit streamlink-CLI ->
.ts -> _remux_recording()) bleibt davon komplett unberührt.

Aufruf (eine Aufnahme, endet mit Stream-Ende, SIGTERM oder Strg+C):

    python3 experimental/streamlink_api_recorder.py https://twitch.tv/<kanal> /pfad/zum/ausgabe-ordner
    TW_USER_TOKEN=... python3 experimental/streamlink_api_recorder.py ...   # Token für werbefreie Streams

Voraussetzung: streamlink >= 8.2 als importierbare Python-Library (nicht nur
das CLI) und ffmpeg im PATH.
"""

import argparse
import contextlib
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from streamlink import Streamlink
from streamlink.exceptions import NoPluginError, PluginError, StreamError
from streamlink.options import Options

logger = logging.getLogger("tw_recorder.experimental")

READ_CHUNK = 64 * 1024


def _sanitize(text: str | None, fallback: str) -> str:
    """Entspricht recorder._sanitize_path_component() (bewusst dupliziert, Prototyp)."""
    text = re.sub(r'[/\\:*?"<>|]', "_", text or "")
    text = re.sub(r"[\s_]+", "_", text).strip("_")
    return text or fallback


def open_stream(url: str, quality: str, user_token: str = "", webbrowser: bool = False):
    """
    Löst url über Streamlinks Plugin-System auf und öffnet den gewünschten
    Stream. Gibt (stream_fd, metadata) zurück, metadata = dict mit
    author/category/title (je None, falls das Plugin sie nicht liefert).

    Plugin-Optionen (hier: Twitchs api-header) gehen seit Streamlink 6 über
    ein eigenes Options-Objekt an den Plugin-Konstruktor - das frühere
    session.set_plugin_option("twitch", ...) gibt es nicht mehr. Genau diese
    Art Bruch ist das Haupt-Risiko der Library-Variante.
    """
    session = Streamlink({"webbrowser": webbrowser})

    plugin_options = Options()
    if user_token:
        # Der Token bleibt im Prozess-Speicher: keine Kommandozeile, keine
        # temporäre --config-Datei (vgl. config.streamlink_extra_args()).
        plugin_options.set("api-header", [("Authorization", f"OAuth {user_token}")])

    _name, plugin_cls, resolved_url = session.resolve_url(url)
    plugin = plugin_cls(session, resolved_url, plugin_options)
    streams = plugin.streams()
    if not streams:
        raise PluginError("Keine Streams gefunden (offline?)")
    if quality not in streams:
        raise PluginError(f"Qualität '{quality}' nicht verfügbar: {', '.join(streams)}")

    metadata = {
        "author": plugin.get_author(),
        "category": plugin.get_category(),
        "title": plugin.get_title(),
    }
    return streams[quality].open(), metadata


def build_ffmpeg_cmd(out_path: Path, channel: str, metadata: dict, start_utc: datetime) -> list[str]:
    """
    ffmpeg liest MPEG-TS von stdin und schreibt per Stream-Copy direkt MKV.
    Die Tags werden - anders als im Remux-Pfad - VOR der Aufnahme gesetzt:
    Start ist ja bekannt, Titel/Kategorie stammen aus dem Plugin.
    """
    start_local = start_utc.astimezone()
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    category = metadata.get("category") or "NoCategory"
    title = metadata.get("title") or "Untitled"
    tags = {
        "TITLE": f"{channel} - {category}: {title} ({start_local:%Y-%m-%d})"[:95],
        "COMMENT": title,
        "ARTIST": channel,
        "PURL": f"https://twitch.tv/{channel}",
        "DATE": f"{start_local:%Y%m%d}",
        "creation_time": start_iso,
        "RECORDING_START": start_iso,
    }
    meta_args = [a for k, v in tags.items() for a in ("-metadata", f"{k}={v}")]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "mpegts", "-i", "pipe:0",
        *meta_args,
        "-timecode", f"{start_local:%H:%M:%S}:00",
        "-map", "0:v", "-map", "0:a?",
        "-c", "copy",
        "-f", "matroska",
        str(out_path),
    ]


def record(url: str, out_dir: Path, quality: str = "best", user_token: str = "",
           stop_event: threading.Event | None = None) -> Path | None:
    """
    Eine Aufnahme von Start bis Stream-Ende bzw. stop_event. Gibt den Pfad der
    fertigen .mkv zurück (oder None, wenn der Stream nicht geöffnet werden
    konnte). Kein Reconnect/Retry - das wäre wie bisher Sache von record_loop().
    """
    stop_event = stop_event or threading.Event()
    channel = url.rstrip("/").split("/")[-1]

    try:
        fd, metadata = open_stream(url, quality, user_token)
    except (NoPluginError, PluginError, StreamError) as e:
        logger.warning(f"Stream {url} nicht geöffnet: {e}")
        return None

    start_utc = datetime.now(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (
        f"{start_utc.astimezone():%Y-%m-%d_%H-%M-%S}_{channel}_"
        f"{_sanitize(metadata.get('category'), 'NoCategory')}_"
        f"{_sanitize(metadata.get('title'), 'Untitled')}.mkv"
    )
    cmd = build_ffmpeg_cmd(out_path, channel, metadata, start_utc)
    logger.info(f"🔴 Aufnahme {channel} -> {out_path.name}")

    # Eigene Session: ein Strg+C im Terminal (SIGINT an die ganze
    # Prozessgruppe) soll ffmpeg nicht parallel abschießen - ffmpeg wird
    # stattdessen über EOF auf stdin sauber beendet und schreibt dann
    # Cues/Dauer. (Unter systemd mit Default KillMode=control-group bekommt
    # ffmpeg das SIGTERM trotzdem direkt; das ist ok, ffmpeg finalisiert auch
    # dann selbst - unten wird der resultierende BrokenPipe abgefangen.)
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True)
    written = 0
    try:
        while not stop_event.is_set():
            chunk = fd.read(READ_CHUNK)
            if not chunk:
                logger.info(f"Stream-Ende für {channel}.")
                break
            ff.stdin.write(chunk)
            written += len(chunk)
    except BrokenPipeError:
        logger.warning("ffmpeg hat die Pipe geschlossen (Abbruch/Fehler).")
    except OSError as e:
        # Streamlink meldet Lese-Timeouts/Netzwerkfehler als OSError
        logger.warning(f"Lesefehler im Stream {channel}: {e}")
    finally:
        fd.close()
        with contextlib.suppress(BrokenPipeError):
            ff.stdin.close()
        try:
            rc = ff.wait(timeout=60)
        except subprocess.TimeoutExpired:
            ff.kill()
            rc = ff.wait()
    logger.info(f"⏹️ {channel}: {written / 1e6:.1f} MB gelesen, ffmpeg rc={rc}")
    if rc == 0:
        with contextlib.suppress(OSError):
            out_path.chmod(0o664)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="EXPERIMENTELL: Streamlink-API -> ffmpeg -> MKV")
    parser.add_argument("url")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--quality", default="best")
    parser.add_argument("--loglevel", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    stop = threading.Event()

    def _on_signal(signum, _frame):
        logger.info(f"Signal {signal.Signals(signum).name} - beende Aufnahme sauber...")
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    out = record(args.url, args.out_dir, args.quality, os.environ.get("TW_USER_TOKEN", ""), stop)
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
