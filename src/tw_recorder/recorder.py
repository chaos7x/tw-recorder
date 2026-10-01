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
    # os.setxattr() existiert nur auf Linux - auf FreeBSD/OpenBSD/macOS fehlt
    # die Funktion komplett (AttributeError statt OSError), was sonst nach
    # jedem erfolgreichen Remux bis in _remux_recording() durchschlagen würde.
    if not hasattr(os, "setxattr"):
        logger.debug("os.setxattr auf dieser Plattform nicht verfügbar - xattrs werden übersprungen.")
        return
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
    Fix wie in debian/postinst.sh für /srv/media-pipeline selbst).
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
    Werte lokal nicht bekannt sind. Uhrzeit sekundengenau (%H-%M-%S) für
    Start-Timecode und RECORDING_START; _remux_recording() liest auch noch
    ältere .ts-Dateien im minutengenauen Format.
    """
    return str(out_dir / f"{{time:%Y-%m-%d_%H-%M-%S}}_{channel}_[{{category}}]_{{title}}.ts")


def _stop_process(proc: subprocess.Popen, timeout: float) -> None:
    """Beendet proc per terminate(), nach timeout Sekunden notfalls per kill()."""
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def _fetch_reference_info(url: str, cfg: dict) -> tuple[str | None, str | None]:
    """Liefert (title, category) des laufenden Streams als Referenz für die Split-Erkennung."""
    info = get_stream_info(url, cfg)
    if info and info["online"]:
        return info["title"], info["category"]
    return None, None


def _sanitize_path_component(text: str, fallback: str) -> str:
    """Ersetzt Pfad-Sonderzeichen und Whitespace-Folgen durch '_'; leer -> fallback."""
    text = re.sub(r'[/\\:*?"<>|]', '_', text)
    text = re.sub(r'[\s_]+', '_', text).strip('_')
    return text or fallback


def _parse_start_time(date_str: str, time_str: str) -> datetime | None:
    """
    Startzeit aus Datum/Uhrzeit des .ts-Namens: sekundengenau (HH-MM-SS) oder,
    bei älteren Dateien, minutengenau (HH-MM). Ziffern sind lokale Zeit, werden
    aber (wie bisher für filename_pattern) als UTC-markiertes datetime geliefert.
    """
    for fmt in ("%Y-%m-%d_%H-%M-%S", "%Y-%m-%d_%H-%M"):
        try:
            return datetime.strptime(f"{date_str}_{time_str}", fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _build_meta_title(channel: str, category: str, full_title: str, date_str: str) -> str:
    """Baut den Container-TITLE "<Kanal> - <Kategorie>: <Titel> (<Datum>)", gekürzt auf 95 Zeichen."""
    clean_title = "".join(
        c for c in full_title
        if c.isalnum() or c in (" ", "-", "_", "!", "?", ".", "♥")
    ).strip()
    clean_title = re.sub(r'\s+', ' ', clean_title)

    prefix = f"{channel} - {category}: "
    suffix = f" ({date_str})"
    meta_title = f"{prefix}{clean_title}{suffix}"
    if len(meta_title) <= 95:
        return meta_title

    max_title_len = 95 - len(prefix) - len(suffix) - 3
    clean_title = clean_title[:max_title_len].strip() + "..." if max_title_len > 0 else "..."
    return f"{prefix}{clean_title}{suffix}"


def _remux_recording(out_dir, channel: str, cfg: dict) -> None:
    """
    Remuxt die zuletzt geschriebene .ts-Aufnahme von channel verlustfrei nach
    .mkv (Name laut filename_pattern), setzt Container-Tags und xattrs und
    löscht danach die .ts-Datei. Fehler werden nur geloggt.
    """
    try:
        ts_files = sorted(out_dir.glob("*.ts"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not ts_files:
            logger.warning(
                f"Kein .ts-File für {channel} in {out_dir} gefunden - "
                "Remuxing übersprungen (Aufnahme evtl. zu kurz/fehlgeschlagen)."
            )
            return

        latest_file = ts_files[0]
        age_sec = time.time() - latest_file.stat().st_mtime
        if age_sec >= 300:
            logger.warning(
                f"Neueste .ts-Datei für {channel} ({latest_file.name}) ist "
                f"{age_sec:.0f}s alt (Limit: 300s) - vermutlich von einem "
                "früheren Lauf, Remuxing übersprungen."
            )
            return

        date_str, time_str, parsed_channel, category, full_title = (
            _parse_recorded_filename(latest_file.stem, channel)
        )
        category = _sanitize_path_component(category, "NoCategory")

        # Kürzel <3 durch ein echtes Unicode-Herz ersetzen, übrige < und > entfernen
        full_title = full_title.replace("<3", "♥")
        full_title = re.sub(r'[<>]', '', full_title)

        # Bewahrt Unicode/Umlaute, filtert Symbole/Steuerzeichen und Pfad-Sonderzeichen
        safe_title = "".join(
            char for char in full_title
            if unicodedata.category(char) not in {"So", "Sk", "Cf"} or char == "♥"
        )
        safe_title = _sanitize_path_component(safe_title, "Untitled")

        # Datum als datetime-Objekt, damit filename_pattern {time:...}-Formate nutzen kann
        dt_obj = _parse_start_time(date_str, time_str)
        if dt_obj:
            # Streamlinks {time} ist die lokale Zeit des Prozesses (TZ), nicht UTC -
            # für creation_time daher als lokale Zeit interpretieren und nach UTC umrechnen.
            start_utc = dt_obj.replace(tzinfo=None).astimezone(UTC)
        else:
            dt_obj = datetime.now(UTC)
            start_utc = None

        try:
            target_filename = cfg["filename_pattern"].format(
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
        # Aufnahmestart mit Uhrzeit (wird in MKV zu DateUTC; yt-upload liest es als
        # Startzeit), ohne parsebaren Dateinamen bleibt es beim reinen Datum.
        meta_creation_time = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if start_utc else meta_date_compact
        meta_title = _build_meta_title(parsed_channel, category, full_title, date_str)
        meta_comment = full_title

        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "error",
            "-i", str(latest_file),
            "-metadata", f"TITLE={meta_title}",
            "-metadata", f"COMMENT={meta_comment}",
            "-metadata", f"ARTIST={parsed_channel}",
            "-metadata", f"PURL=https://twitch.tv/{parsed_channel}",
            "-metadata", f"DATE={meta_date_compact}",
            "-metadata", f"creation_time={meta_creation_time}",
            # Eigenes Tag nur mit echter Startzeit: yt-upload liest ausschließlich
            # dieses (nicht creation_time, das auch aus fremden Quellen stammen kann).
            *(["-metadata", f"RECORDING_START={meta_creation_time}"] if start_utc else []),
            # Start-Timecode (lokale Zeit wie im Dateinamen, Frames immer 00) - wird
            # z.B. bei "ffmpeg -i x.mkv -c copy x.mov" zur tmcd-Timecode-Spur.
            *(["-timecode", f"{dt_obj:%H:%M:%S}:00"] if start_utc else []),
            "-map", "0:v",
            "-map", "0:a?",
            "-c", "copy",
            str(final_target_path)
        ]

        if cfg["use_ionice"] and shutil.which("ionice"):
            # Idle-I/O-Klasse (-c3): Remux bekommt nur Platten-I/O, wenn gerade
            # nichts anderes (v.a. laufende Aufnahmen anderer Kanäle) es braucht.
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


def _monitor_recording(proc: subprocess.Popen, url: str, channel: str, cfg: dict,
                       stop_event: threading.Event) -> tuple[bool, dict]:
    """
    Überwacht einen laufenden Streamlink-Prozess bis zu dessen Ende, einem
    Stop-Signal oder einer Titel-/Kategorieänderung (split_on_title_change).
    Lädt die Config bei Änderung nach, damit Einstellungen auch während einer
    mehrstündigen Aufnahme greifen. Gibt (title_split_triggered, cfg) zurück.
    """
    sleep_interval = cfg["sleep_interval"]
    last_title, last_category = (None, None)
    if cfg["split_on_title_change"]:
        last_title, last_category = _fetch_reference_info(url, cfg)
    last_title_check = time.monotonic()
    last_cfg_reload = time.monotonic()
    last_cfg_hash, _ = config.get_config_hash()

    while proc.poll() is None:
        if stop_event.is_set():
            _stop_process(proc, timeout=5)
            return False, cfg

        if (time.monotonic() - last_cfg_reload) >= sleep_interval:
            last_cfg_reload = time.monotonic()
            current_cfg_hash, _ = config.get_config_hash()
            if current_cfg_hash != last_cfg_hash:
                last_cfg_hash = current_cfg_hash
                cfg = config.load_config()
                if cfg["split_on_title_change"] and last_title is None:
                    # Feature wurde gerade erst aktiviert -> Referenz jetzt nachträglich ermitteln
                    last_title, last_category = _fetch_reference_info(url, cfg)

        if (
            cfg["split_on_title_change"]
            and last_title is not None
            and (time.monotonic() - last_title_check) >= cfg["title_check_interval"]
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
                _stop_process(proc, timeout=10)
                return True, cfg

        time.sleep(1)

    return False, cfg


def _start_streamlink(url: str, quality: str, channel: str, out_pattern: str, cfg: dict) -> subprocess.Popen:
    """Startet Streamlink und leitet dessen Ausgabe in einem Hintergrund-Thread ans Logging weiter."""
    cmd = [
        "streamlink",
        "--loglevel", cfg["streamlink_loglevel"],
        "--fs-safe-rules", "POSIX",
        *config.get_extra_args(cfg),
        "-o", out_pattern,
        url,
        quality,
    ]
    # Enthält ggf. den OAuth-Token (--twitch-api-header) - daher ausschließlich auf DEBUG-Level.
    logger.debug(f"Streamlink-Kommando für {channel}: {cmd}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    threading.Thread(target=_pump_subprocess_output, args=(proc, channel), daemon=True).start()
    return proc


def record_loop(url: str, quality: str, stop_event: threading.Event):
    channel = url.rstrip("/").split("/")[-1]

    # Config nur bei tatsächlicher Änderung neu laden (Hash-Vergleich), statt bei
    # jedem Poll-Tick blind neu zu parsen - spart bei vielen Kanälen Datei-I/O.
    cfg = config.load_config()
    last_outer_cfg_hash, _ = config.get_config_hash()

    while not stop_event.is_set():
        current_outer_cfg_hash, _ = config.get_config_hash()
        if current_outer_cfg_hash != last_outer_cfg_hash:
            last_outer_cfg_hash = current_outer_cfg_hash
            cfg = config.load_config()

        sleep_interval = cfg["sleep_interval"]
        out_dir = cfg["storage_dir"] / channel
        _ensure_channel_dir(out_dir)
        lock_file = out_dir / ".record.lock"

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
                proc = _start_streamlink(url, quality, channel, _build_out_pattern(out_dir, channel), cfg)
                title_split_triggered, cfg = _monitor_recording(proc, url, channel, cfg, stop_event)

                logger.info(f"⏹️ Aufzeichnung beendet für Kanal: {channel}. Remuxe Datei...")
                time.sleep(2)
                _remux_recording(out_dir, channel, cfg)

            except Exception as e:  # noqa: BLE001 - Top-Level-Boundary für den gesamten Aufnahme-Zyklus (Streamlink-Start, Monitoring, Remux); jeder Fehler hier darf den Überwachungs-Thread für diesen Kanal nicht dauerhaft beenden
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
