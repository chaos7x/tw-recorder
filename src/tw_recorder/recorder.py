"""Kern der Aufnahme-Schleife: Streamlink-Subprocess, Monitoring, FFmpeg-Remux."""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from datetime import UTC, datetime

from tw_recorder import config

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # fcntl ist nur auf Unix verfügbar; auf anderen Plattformen läuft das
    # Skript ohne Datei-Lock-Schutz zwischen mehreren Instanzen weiter.
    fcntl = None
    HAS_FCNTL = False

from tw_recorder.twitch_api import check_stream_online, get_stream_info

logger = logging.getLogger(__name__)


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


def _parse_recorded_filename(full_stem: str, channel: str) -> tuple[str, str, str, str, str]:
    """
    Rekonstruiert Datum, Uhrzeit, Kanal, Kategorie und Titel aus dem von
    Streamlink vergebenen .ts-Dateinamen (Pattern siehe out_pattern in
    record_loop()). Gibt (date_str, time_str, parsed_channel, category,
    full_title) zurück - category/full_title sind noch unsanitized (roh),
    Aufrufer wendet die Pfad-Sanitisierung selbst an.
    """
    # Zuerst Datum und Uhrzeit abtrennen (enthalten nie Unterstriche, nur
    # Bindestriche -> sicher via split(_, 2))
    dt_parts = full_stem.split("_", 2)
    date_str = dt_parts[0] if len(dt_parts) > 0 and dt_parts[0] else "0000-00-00"
    time_str = dt_parts[1] if len(dt_parts) > 1 and dt_parts[1] else "00-00"
    remainder = dt_parts[2] if len(dt_parts) > 2 else ""

    # Kanalname ist bereits bekannt (aus der URL) und damit zuverlässiger
    # als ein Re-Parsing des Dateinamens per Unterstrich-Split (Twitch-Namen
    # dürfen "_" enthalten, was das alte Split-Verfahren fälschlich abschnitt).
    parsed_channel = channel
    channel_prefix = f"{channel}_"

    # Kategorie steht in eckigen Klammern (siehe out_pattern) - dadurch
    # bleibt die Trennung zum Titel eindeutig, selbst wenn die Kategorie
    # selbst einen "_" enthält (z.B. durch Leerzeichen-Ersetzung). Bewusst
    # re.search() auf dem GESAMTEN remainder statt re.match() auf dem um
    # den Kanal-Präfix gekürzten Rest: out_pattern setzt den Kanalnamen seit
    # dem {author}-Fix zwar direkt (nicht mehr über Streamlinks eigenen,
    # von Twitch-Metadaten abhängigen {author}-Platzhalter) ein, ältere,
    # bereits vor diesem Fix geschriebene .ts-Dateien können aber noch mit
    # leerem Kanal-Präfix in out_dir herumliegen (z.B.
    # "2026-09-23_20-00__[]_.ts") - ein Prefix-Matching würde die Klammer
    # dort nie finden und in den naiven Fallback fallen, der die
    # Klammer-Zeichen selbst fälschlich als Teil des Titels behandelt,
    # statt sauber auf "Untitled" zurückzufallen.
    bracket_match = re.search(r'\[(.*?)\]_?(.*)$', remainder)
    if bracket_match:
        raw_category = bracket_match.group(1)
        raw_title = bracket_match.group(2).strip()
    else:
        # Altes Format ohne Klammern (Dateien von vor diesem Fix, die
        # zufällig noch in out_dir herumliegen) - bestmöglicher naiver
        # Fallback per Unterstrich-Split.
        if remainder.startswith(channel_prefix):
            rest_after_channel = remainder[len(channel_prefix):]
        else:
            rest_after_channel = remainder
        cat_title_parts = rest_after_channel.split("_", 1)
        raw_category = cat_title_parts[0] if len(cat_title_parts) > 0 else ""
        raw_title = cat_title_parts[1].strip() if len(cat_title_parts) > 1 else ""

    category = raw_category.strip() or "NoCategory"
    full_title = raw_title or "Untitled"

    return date_str, time_str, parsed_channel, category, full_title


def _write_xattrs(path, attrs: dict[str, str]) -> None:
    """
    Setzt dieselben Extended Attributes (Namensschema: dublincore/xdg), die
    auch yt-dlp per --xattrs schreibt - so behandeln Dateimanager/Tools
    Twitch-Aufnahmen und yt-dlp-Downloads einheitlich. Rein zusätzlich zu den
    ffmpeg-Container-Tags (nicht deren Ersatz): xattrs bleiben unabhängig vom
    Container lesbar (z.B. auch bei einer kaputten/unvollständigen Datei) und
    lassen sich nachträglich ändern, ohne die - bei mehrstündigen Aufnahmen
    ggf. sehr große - Datei per ffmpeg neu schreiben zu müssen.
    Nicht jedes Dateisystem unterstützt xattrs (z.B. FAT/exFAT, manche
    Netzwerk-Mounts) - ein OSError dabei ist keine echte Fehlfunktion, wird
    daher nur als Warnung geloggt statt die Aufnahme fehlschlagen zu lassen.
    """
    for name, value in attrs.items():
        try:
            os.setxattr(path, name, value.encode("utf-8"))
        except OSError as e:
            logger.warning(
                f"xattr '{name}' konnte nicht auf {path.name} gesetzt werden "
                f"(Dateisystem unterstützt evtl. keine Extended Attributes): {e}"
            )
            return


def _ensure_channel_dir(out_dir) -> None:
    """
    Legt das Kanal-Unterverzeichnis unter STORAGE_DIR an und erzwingt beim
    tatsächlichen Neuanlegen explizit 2775 statt sich auf mkdir()s
    Standard-Mode zu verlassen: mkdir() allein würde das Gruppen-Schreibrecht
    durch das Prozess-Umask verlieren (Standard-Mode 0o777 wird umask-
    maskiert, z.B. auf 0o755 bei umask 022) - das Setgid-Bit selbst wird zwar
    vom Elternverzeichnis geerbt, das für die media-pipeline-Gruppe eigentlich
    nötige g+w aber nicht. Ohne das könnte fetchbridge (Mitglied der
    media-pipeline-Gruppe, aber nicht Owner) Dateien in diesem
    Kanal-Unterordner nicht mehr verschieben/löschen - exakt das Szenario,
    für das die 2775-Rechte auf /srv/media-pipeline überhaupt eingeführt
    wurden.
    Nur beim tatsächlichen Neuanlegen gesetzt, sonst würde eine bewusste
    Admin-Anpassung bei jedem Recording-Start wieder überschrieben (derselbe
    Fix wie in postinst.sh für /srv/media-pipeline selbst).
    """
    newly_created = not out_dir.is_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    if newly_created:
        try:
            out_dir.chmod(0o2775)
        except OSError as e:
            logger.warning(f"Konnte Rechte von {out_dir} nicht auf 2775 setzen: {e}")


def _build_out_pattern(out_dir, channel: str) -> str:
    """
    Baut das Streamlink-Ausgabepattern für die rohe .ts-Aufnahme. Kanalname
    wird direkt eingesetzt statt Streamlinks eigenen {author}-Platzhalter zu
    nutzen: der Kanal ist bereits aus der URL bekannt (record_loop()), ein
    {author}-Platzhalter müsste Streamlink dagegen erst live aus den
    Twitch-Metadaten auflösen - die können direkt nach Stream-Start noch
    nicht verfügbar sein und {author} bleibt dann leer (real beobachtet:
    "2026-09-23_20-00__[]_.ts", siehe _parse_recorded_filename()).
    Category/Titel bleiben bewusst Streamlink-Platzhalter (in eckigen
    Klammern um die Kategorie, siehe _parse_recorded_filename()), da diese
    Werte lokal nicht bekannt sind.
    """
    return str(out_dir / f"{{time:%Y-%m-%d_%H-%M}}_{channel}_[{{category}}]_{{title}}.ts")


def record_loop(url: str, quality: str, stop_event: threading.Event):
    channel = url.rstrip("/").split("/")[-1]

    # Config nur bei tatsächlicher Änderung neu laden (gleicher Hash-Mechanismus
    # wie beim Daemon selbst und bei der Titel-Split-Erkennung weiter unten),
    # statt bei jedem Poll-Tick (alle sleep_interval Sekunden) blind neu zu
    # parsen - besonders bei vielen gleichzeitig überwachten Kanälen spart das
    # unnötige Datei-I/O.
    cfg = config.load_config()
    last_outer_cfg_hash, _ = config.get_config_hash()

    while not stop_event.is_set():
        current_outer_cfg_hash, _ = config.get_config_hash()
        if current_outer_cfg_hash != last_outer_cfg_hash:
            last_outer_cfg_hash = current_outer_cfg_hash
            cfg = config.load_config()

        storage_dir = cfg["storage_dir"]
        sleep_interval = cfg["sleep_interval"]
        pattern_tmpl = cfg["filename_pattern"]

        out_dir = storage_dir / channel
        _ensure_channel_dir(out_dir)

        lock_file = out_dir / ".record.lock"

        out_pattern = _build_out_pattern(out_dir, channel)

        if check_stream_online(url, cfg):
            lock_fd = None
            if HAS_FCNTL:
                try:
                    lock_fd = open(lock_file, "a+")  # noqa: SIM115
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
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
                cmd.extend(config.get_extra_args(cfg))
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
                last_cfg_hash, _ = config.get_config_hash()

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
                        current_cfg_hash, _ = config.get_config_hash()
                        if current_cfg_hash != last_cfg_hash:
                            last_cfg_hash = current_cfg_hash
                            cfg = config.load_config()
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

                            date_str, time_str, parsed_channel, category, full_title = (
                                _parse_recorded_filename(full_stem, channel)
                            )
                            category = re.sub(r'[/\\:*?"<>|]', '_', category)
                            category = re.sub(r'[\s_]+', '_', category).strip('_')
                            if not category:
                                category = "NoCategory"

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
                            # For DTZ007 (strptime):
                            try:
                                dt_obj = datetime.strptime(f"{date_str}_{time_str}", "%Y-%m-%d_%H-%M").replace(tzinfo=UTC)
                            except ValueError:
                                # For DTZ005 (datetime.now):
                                dt_obj = datetime.now(UTC)

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

                            ff_proc = subprocess.run(ffmpeg_cmd, check=False)
                            if ff_proc.returncode == 0:
                                if latest_file.exists():
                                    latest_file.unlink()
                                logger.info(f"✅ Erfolgreich remuxed: {final_target_path.name}")
                                _write_xattrs(final_target_path, {
                                    "user.dublincore.title": meta_title,
                                    "user.dublincore.contributor": parsed_channel,
                                    "user.dublincore.date": date_str,
                                    "user.dublincore.description": meta_comment,
                                    "user.xdg.referrer.url": f"https://twitch.tv/{parsed_channel}",
                                })
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
