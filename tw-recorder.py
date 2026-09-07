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
import fcntl
import hashlib
import json
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
import glob
import re
import unicodedata

__title__ = "Streamlink Recorder CLI"
__version__ = "1.2.1"

# Ungepufferte Standard-Ausgabe erzwingen
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Standard-Konfigurationspfade
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/etc/tw-recorder/recorder.conf"))
CONF_D_DIR = Path(os.getenv("CONF_D_DIR", "/etc/tw-recorder/conf.d"))

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
            files_state[f] = hashlib.md5(f.read_bytes()).hexdigest()
        except Exception:
            pass

    return files_state


def get_config_hash() -> tuple[str, dict]:
    """Berechnet den Gesamt-Hash und gibt den detaillierten Status aller Dateien zurück."""
    state = get_config_files_state()
    combined = hashlib.md5()
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
        except Exception as e:
            print(f"[WARN] Fehler beim Lesen der Config-Dateien: {e}", flush=True)

    # General / Output Settings
    storage_dir = Path(config.get("general", "storage_dir", fallback=os.getenv("STORAGE_DIR", "/storage")))
    sleep_interval = int(config.get("general", "sleep_interval", fallback=os.getenv("SLEEP_INTERVAL", "15")))
    filename_pattern = config.get("general", "filename_pattern", fallback=os.getenv("OUTPUT_FILENAME_PATTERN", "{time:%Y-%m-%d_%H-%M}_{channel}_{title}.mkv"))

    # Twitch Credentials
    client_id = config.get("twitch", "client_id", fallback=os.getenv("CLIENT_ID", os.getenv("TWITCH_CLIENT_ID", ""))).strip()
    client_secret = config.get("twitch", "client_secret", fallback=os.getenv("CLIENT_SECRET", os.getenv("TWITCH_CLIENT_SECRET", ""))).strip()
    user_token = config.get("twitch", "user_token", fallback=os.getenv("TWITCH_USER_TOKEN", "")).replace("oauth:", "").strip()

    # Streamlink Defaults
    default_quality = config.get("streamlink", "stream_quality", fallback=os.getenv("STREAM_QUALITY", "best"))
    streamlink_loglevel = config.get("streamlink", "loglevel", fallback=os.getenv("STREAMLINK_LOGLEVEL", "info"))
    streamlink_retry = int(config.get("streamlink", "retry", fallback=os.getenv("STREAMLINK_RETRY", "30")))
    webbrowser = config.getboolean("streamlink", "webbrowser", fallback=os.getenv("STREAMLINK_WEBBROWSER", "false").lower() == "true")

    return {
        "storage_dir": storage_dir,
        "sleep_interval": sleep_interval,
        "filename_pattern": filename_pattern,
        "client_id": client_id,
        "client_secret": client_secret,
        "user_token": user_token,
        "default_quality": default_quality,
        "streamlink_loglevel": streamlink_loglevel,
        "streamlink_retry": streamlink_retry,
        "webbrowser": webbrowser,
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
        except Exception as e:
            print(f"[WARN] Fehler beim Twitch-Token-Abruf: {e}", flush=True)
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
                print(f"[WARN] Twitch API HTTP-Fehler {e.code} für {channel}", flush=True)
            except Exception as e:
                with api_warning_lock:
                    api_unavailable_until = time.monotonic() + 60
                    print(
                        f"[WARN] Twitch-API vorübergehend nicht erreichbar ({e}). "
                        "Verwende Streamlink-Fallback für 60 Sekunden.",
                        flush=True
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


def record_loop(url: str, quality: str, stop_event: threading.Event):
    channel = url.rstrip("/").split("/")[-1]

    while not stop_event.is_set():
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
            try:
                lock_fd = open(lock_file, "a+")
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                if 'lock_fd' in locals() and not lock_fd.closed:
                    lock_fd.close()
                time.sleep(sleep_interval)
                continue

            try:
                print(f"[INFO] 🔴 Starte Aufzeichnung für Kanal: {channel}", flush=True)

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

                proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)

                while proc.poll() is None:
                    if stop_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break
                    time.sleep(1)

                print(f"[INFO] ⏹️ Aufzeichnung beendet für Kanal: {channel}. Remuxe Datei...", flush=True)
                time.sleep(2)

                try:
                    ts_files = sorted(out_dir.glob("*.ts"), key=lambda f: f.stat().st_mtime, reverse=True)
                    if ts_files:
                        latest_file = ts_files[0]
                        if (time.time() - latest_file.stat().st_mtime) < 300:

                            full_stem = latest_file.stem
                            # Max. 4 Trennungen, damit Unterstriche im Titel intakt bleiben
                            parts = full_stem.split("_", 4)

                            date_str = parts[0] if len(parts) > 0 and parts[0] else "0000-00-00"
                            time_str = parts[1] if len(parts) > 1 and parts[1] else "00-00"
                            parsed_channel = parts[2] if len(parts) > 2 and parts[2] else channel

                            raw_category = parts[3] if len(parts) > 3 else ""
                            category = raw_category.strip() if raw_category.strip() else "NoCategory"
                            category = re.sub(r'[/\\:*?"<>|]', '_', category)
                            category = re.sub(r'[\s_]+', '_', category).strip('_')
                            if not category:
                                category = "NoCategory"

                            raw_title = parts[4].strip() if len(parts) > 4 else ""
                            full_title = raw_title if raw_title else "Untitled"

                            # Sanitize title: Bewahrt Unicode/Umlaute, filtert nur echte Pfad-Sonderzeichen
                            safe_title = "".join(
                                char for char in full_title
                                if unicodedata.category(char) not in {"So", "Sk", "Cf"}
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
                                print(f"[WARN] Fehler beim Formatieren von filename_pattern ({e}). Nutze Fallback-Name.", flush=True)
                                target_filename = f"{date_str}_{time_str}_{parsed_channel}_{category}_{safe_title}.mkv"

                            # Falls das Pattern keine Endung hat, erzwinge .mkv
                            final_target_path = out_dir / target_filename
                            if not final_target_path.suffix:
                                final_target_path = final_target_path.with_suffix(".mkv")

                            meta_date_compact = date_str.replace("-", "") if date_str else "00000000"

                            clean_title_meta = "".join(c for c in full_title if c.isalnum() or c in (" ", "-", "_", "!", "?", ".")).strip()
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
                                "-metadata", f"DATE={meta_date_compact}",
                                "-metadata", f"creation_time={meta_date_compact}",
                                "-map", "0:v",
                                "-map", "0:a",
                                "-c", "copy",
                                str(final_target_path)
                            ]

                            ff_proc = subprocess.run(ffmpeg_cmd)
                            if ff_proc.returncode == 0:
                                if latest_file.exists():
                                    latest_file.unlink()
                                print(f"[INFO] ✅ Erfolgreich remuxed: {final_target_path.name}", flush=True)
                except Exception as e:
                    print(f"[WARN] Fehler beim FFmpeg-Remuxing für {channel}: {e}", flush=True)

            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass

                lock_file.unlink(missing_ok=True)

            if stop_event.is_set():
                return

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
    print(f"[INFO] {len(current_streamers)} Streamer in Config gefunden.", flush=True)

    # 1. Entfernte Streamer stoppen
    for url in list(running_threads.keys()):
        if url not in current_streamers:
            channel = url.rstrip("/").split("/")[-1]
            print(f"[INFO] Stoppe Überwachung für Kanal: {channel}", flush=True)
            thread, stop_event = running_threads.pop(url)
            stop_event.set()

    # 2. Neue Streamer starten (laufende Threads nicht verdoppeln)
    for url, quality in current_streamers.items():
        if url in running_threads:
            thread, stop_event = running_threads[url]
            if thread.is_alive():
                continue

        channel = url.rstrip("/").split("/")[-1]
        print(f"[INFO] Starte Überwachung für Kanal: {channel}", flush=True)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=record_loop,
            args=(url, quality, stop_event),
            daemon=True
        )
        thread.start()
        running_threads[url] = (thread, stop_event)


def shutdown_handler(signum, frame):
    print("\n[INFO] Signal empfangen, beende alle Aufnahmen...", flush=True)
    threads = []
    for url, (thread, stop_event) in running_threads.items():
        stop_event.set()
        threads.append(thread)

    for thread in threads:
        thread.join(timeout=10)

    sys.exit(0)


def run_daemon():
    global LAST_CONFIG_HASH, LAST_FILES_STATE

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    print(f"[INFO] Starte Streamlink-Manager (Config: {CONFIG_FILE})...", flush=True)

    # Initialen Zustand beim Start erfassen
    LAST_CONFIG_HASH, LAST_FILES_STATE = get_config_hash()
    sync_processes()

    while True:
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
            print(f"[INFO] 🔄 Konfigurationsänderung erkannt ({changes_str}). Synchronisiere...", flush=True)

            LAST_CONFIG_HASH = current_hash
            LAST_FILES_STATE = current_state

            sync_processes()

        time.sleep(5)


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
    args = parser.parse_args()

    if not args.daemon:
        parser.print_usage()
        print("Hinweis: Zum Starten bitte -D oder --daemon verwenden.")
        return

    run_daemon()


if __name__ == "__main__":
    main()
