"""
Tests für die Hilfsfunktionen von recorder.py (_parse_recorded_filename(),
_write_xattrs(), _ensure_channel_dir(), _remux_recording(),
_monitor_recording() u.a.).

Der alte naive "_"-Split zwischen Kategorie und Titel (cat_title_parts =
rest_after_channel.split("_", 1)) hat mehrteilige Kategorienamen falsch
zugeordnet, sobald die Kategorie selbst einen "_" enthielt (z.B. durch
Leerzeichen-Ersetzung) - der erste Wortteil landete als "Kategorie", der Rest
fälschlich im Titel. Die Kategorie wird jetzt in eckigen Klammern kodiert
(siehe out_pattern in record_loop()), was die Trennung eindeutig macht.
"""

import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest


class TestParseStartTime:
    def test_seconds_format(self, recorder):
        assert recorder._parse_start_time("2026-09-23", "20-15-42") == datetime(2026, 9, 23, 20, 15, 42, tzinfo=UTC)

    def test_old_minute_format(self, recorder):
        assert recorder._parse_start_time("2026-09-23", "20-15") == datetime(2026, 9, 23, 20, 15, tzinfo=UTC)

    def test_invalid_returns_none(self, recorder):
        assert recorder._parse_start_time("0000-00-00", "00-00") is None


class TestParseRecordedFilename:
    def test_seconds_in_time_are_kept(self, recorder):
        date_str, time_str, *_ = recorder._parse_recorded_filename(
            "2026-09-17_14-30-05_somechannel_[Gaming]_Title", "somechannel"
        )
        assert (date_str, time_str) == ("2026-09-17", "14-30-05")

    def test_multi_word_category_with_underscore_is_not_split(self, recorder):
        full_stem = "2026-09-17_14-30_somechannel_[Just_Chatting]_Some_Cool_Title"

        date_str, time_str, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "somechannel"
        )

        assert date_str == "2026-09-17"
        assert time_str == "14-30"
        assert channel == "somechannel"
        assert category == "Just_Chatting"
        assert title == "Some_Cool_Title"

    def test_category_without_underscore(self, recorder):
        full_stem = "2026-09-17_14-30_somechannel_[Minecraft]_Building_a_house"

        _, _, _, category, title = recorder._parse_recorded_filename(full_stem, "somechannel")

        assert category == "Minecraft"
        assert title == "Building_a_house"

    def test_channel_name_itself_containing_underscore(self, recorder):
        full_stem = "2026-09-17_14-30_some_channel_[Just_Chatting]_Title_here"

        _, _, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "some_channel"
        )

        assert channel == "some_channel"
        assert category == "Just_Chatting"
        assert title == "Title_here"

    def test_missing_category_brackets_falls_back_to_naive_split(self, recorder):
        """Legacy-Format (vor diesem Fix) ohne eckige Klammern - best effort wie zuvor."""
        full_stem = "2026-09-17_14-30_somechannel_Minecraft_Building_a_house"

        _, _, _, category, title = recorder._parse_recorded_filename(full_stem, "somechannel")

        assert category == "Minecraft"
        assert title == "Building_a_house"

    def test_empty_category_and_title_fall_back_to_defaults(self, recorder):
        full_stem = "2026-09-17_14-30_somechannel_[]_"

        _, _, _, category, title = recorder._parse_recorded_filename(full_stem, "somechannel")

        assert category == "NoCategory"
        assert title == "Untitled"

    def test_unexpected_format_missing_channel_prefix(self, recorder):
        """Streamlink hat den Namen unerwartet geschrieben - best effort, kein Crash."""
        full_stem = "2026-09-17_14-30_totally-different-format"

        date_str, time_str, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "somechannel"
        )

        assert date_str == "2026-09-17"
        assert time_str == "14-30"
        assert channel == "somechannel"
        assert category  # kein Crash, irgendein Fallback-Wert
        assert title

    def test_empty_author_from_streamlink_falls_back_cleanly(self, recorder):
        """
        Regression: real auf einem Host beobachtet. Streamlinks {author}-
        Platzhalter im Dateinamen kann leer bleiben, wenn die Twitch-
        Metadaten beim Aufnahmestart noch nicht verfügbar waren (Stream
        gerade erst live) - der Dateiname beginnt dann gar nicht mit dem
        Kanalnamen. Vorher landete "[]_" als Datenmüll im Titel, statt
        sauber auf "Untitled" zurückzufallen.
        """
        full_stem = "2026-09-23_20-00__[]_"

        date_str, time_str, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "somechannel"
        )

        assert date_str == "2026-09-23"
        assert time_str == "20-00"
        assert channel == "somechannel"
        assert category == "NoCategory"
        assert title == "Untitled"


class TestWriteXattrs:
    """
    Dieselben Extended Attributes (dublincore/xdg-Namensschema), die auch
    yt-dlp per --xattrs schreibt - zusätzlich zu den ffmpeg-Container-Tags,
    nicht als deren Ersatz (siehe _write_xattrs()-Docstring).
    """

    def test_sets_all_attributes_on_supported_filesystem(self, recorder, tmp_path):
        target = tmp_path / "recording.mkv"
        target.write_bytes(b"dummy")
        attrs = {
            "user.dublincore.title": "somechannel - Just Chatting: Cool Title (2026-09-17)",
            "user.dublincore.contributor": "somechannel",
            "user.dublincore.date": "2026-09-17",
            "user.dublincore.description": "Cool Title",
            "user.xdg.referrer.url": "https://twitch.tv/somechannel",
        }

        try:
            recorder._write_xattrs(target, attrs)
        except OSError as e:
            pytest.skip(f"Dateisystem unterstützt keine Extended Attributes: {e}")

        for name, value in attrs.items():
            assert os.getxattr(target, name) == value.encode("utf-8")

    def test_unsupported_filesystem_logs_warning_without_raising(self, recorder, tmp_path, monkeypatch, caplog):
        target = tmp_path / "recording.mkv"
        target.write_bytes(b"dummy")

        def raise_not_supported(*args, **kwargs):
            raise OSError("Operation not supported")

        monkeypatch.setattr(recorder.os, "setxattr", raise_not_supported)

        with caplog.at_level("WARNING"):
            recorder._write_xattrs(target, {"user.dublincore.title": "Cool Title"})

        assert "konnte nicht" in caplog.text

    def test_stops_after_first_failure_instead_of_retrying_each_attribute(self, recorder, tmp_path, monkeypatch):
        target = tmp_path / "recording.mkv"
        target.write_bytes(b"dummy")
        call_count = 0

        def raise_always(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise OSError("Operation not supported")

        monkeypatch.setattr(recorder.os, "setxattr", raise_always)

        recorder._write_xattrs(target, {"a": "1", "b": "2", "c": "3"})

        assert call_count == 1

    def test_skips_silently_on_platforms_without_setxattr(self, recorder, tmp_path, monkeypatch):
        # FreeBSD/OpenBSD/macOS: os.setxattr existiert gar nicht.
        target = tmp_path / "recording.mkv"
        target.write_bytes(b"dummy")
        monkeypatch.delattr(recorder.os, "setxattr", raising=False)

        recorder._write_xattrs(target, {"user.dublincore.title": "Cool Title"})


class TestBuildOutPattern:
    """
    Regression: out_pattern nutzte bislang Streamlinks eigenen {author}-
    Platzhalter für den Kanalnamen im Dateinamen, obwohl der Kanal bereits
    aus der URL bekannt ist (record_loop()). {author} muss Streamlink erst
    live aus den Twitch-Metadaten auflösen - direkt nach Stream-Start können
    die noch nicht verfügbar sein, wodurch {author} leer blieb (real
    beobachtet: "2026-09-23_20-00__[]_.ts"). Der Kanalname wird jetzt direkt
    eingesetzt statt über einen unzuverlässigen Streamlink-Platzhalter.
    """

    def test_channel_name_is_inserted_directly_not_via_author_placeholder(self, recorder, tmp_path):
        out_pattern = recorder._build_out_pattern(tmp_path, "somechannel")

        assert "{author}" not in out_pattern
        assert "_somechannel_" in out_pattern

    def test_category_and_title_placeholders_remain_streamlink_templates(self, recorder, tmp_path):
        """Kategorie/Titel sind lokal nicht bekannt - bleiben bewusst Streamlink-Platzhalter."""
        out_pattern = recorder._build_out_pattern(tmp_path, "somechannel")

        assert "[{category}]" in out_pattern
        assert "{title}" in out_pattern
        assert "{time:%Y-%m-%d_%H-%M-%S}" in out_pattern

    def test_channel_name_containing_underscore_is_preserved(self, recorder, tmp_path):
        out_pattern = recorder._build_out_pattern(tmp_path, "some_channel")

        assert "_some_channel_" in out_pattern


class TestEnsureChannelDir:
    """
    Regression: out_dir.mkdir(parents=True, exist_ok=True) ohne explizites
    mode= verliert das Gruppen-Schreibrecht durchs Prozess-Umask (Standard-
    Mode 0o777 wird umask-maskiert, z.B. auf 0o755 bei umask 022) - das
    Setgid-Bit selbst wird zwar vom Elternverzeichnis geerbt, das fuer die
    media-pipeline-Gruppe eigentlich noetige g+w aber nicht. Real auf einem
    Host reproduziert: alle von tw-recorder angelegten Kanal-Unterordner
    unter /srv/media-pipeline/recordings/ standen auf 2755 statt 2775,
    wodurch fetchbridge (Gruppenmitglied, aber nicht Owner) dort keine
    Dateien mehr verschieben/loeschen konnte.
    """

    def test_newly_created_dir_gets_2775_regardless_of_umask(self, recorder, tmp_path):
        old_umask = os.umask(0o022)
        try:
            out_dir = tmp_path / "somechannel"
            recorder._ensure_channel_dir(out_dir)
            assert stat.S_IMODE(out_dir.stat().st_mode) == 0o2775
        finally:
            os.umask(old_umask)

    def test_existing_dir_permissions_are_not_overwritten(self, recorder, tmp_path):
        """Eine bewusste Admin-Anpassung (z.B. chmod 777) darf nicht bei jedem Recording-Start zurueckgesetzt werden."""
        out_dir = tmp_path / "somechannel"
        out_dir.mkdir()
        out_dir.chmod(0o777)

        recorder._ensure_channel_dir(out_dir)

        assert stat.S_IMODE(out_dir.stat().st_mode) == 0o777

    def test_chmod_failure_logs_warning_without_raising(self, recorder, tmp_path, monkeypatch, caplog):
        out_dir = tmp_path / "somechannel"

        def raise_oserror(self, mode):
            raise OSError("Operation not permitted")

        monkeypatch.setattr(Path, "chmod", raise_oserror)

        with caplog.at_level("WARNING"):
            recorder._ensure_channel_dir(out_dir)  # darf nicht raisen

        assert out_dir.is_dir()
        assert "2775" in caplog.text


class TestSanitizePathComponent:
    def test_replaces_path_special_chars_and_collapses_whitespace(self, recorder):
        assert recorder._sanitize_path_component('Just  Chatting/IRL: "x"', "Fallback") == "Just_Chatting_IRL_x"

    def test_empty_result_uses_fallback(self, recorder):
        assert recorder._sanitize_path_component("  / ", "NoCategory") == "NoCategory"


class TestBuildMetaTitle:
    def test_short_title_is_kept(self, recorder):
        assert (
            recorder._build_meta_title("chan", "Gaming", "Hello World!", "2026-09-23")
            == "chan - Gaming: Hello World! (2026-09-23)"
        )

    def test_long_title_is_truncated_to_95_chars(self, recorder):
        meta_title = recorder._build_meta_title("chan", "Gaming", "x" * 200, "2026-09-23")
        assert len(meta_title) <= 95
        assert meta_title.startswith("chan - Gaming: ")
        assert meta_title.endswith("... (2026-09-23)")


class TestRemuxRecording:
    def _cfg(self):
        return {"filename_pattern": "{time:%Y-%m-%d}_{channel}_{category}_{title}.mkv", "use_ionice": False}

    def test_remuxes_latest_ts_and_deletes_it(self, recorder, tmp_path, monkeypatch):
        ts_file = tmp_path / "2026-09-23_20-00_chan_[Just Chatting]_Hello <3.ts"
        ts_file.write_bytes(b"data")
        calls = []

        class FakeCompleted:
            returncode = 0

        monkeypatch.setattr(recorder.subprocess, "run", lambda cmd, check: calls.append(cmd) or FakeCompleted())
        monkeypatch.setattr(recorder, "_write_xattrs", lambda path, attrs: None)

        recorder._remux_recording(tmp_path, "chan", self._cfg())

        assert len(calls) == 1
        assert calls[0][-1] == str(tmp_path / "2026-09-23_chan_Just_Chatting_Hello_♥.mkv")
        assert "TITLE=chan - Just_Chatting: Hello ♥ (2026-09-23)" in calls[0]
        assert not ts_file.exists()

    def test_creation_time_is_local_start_time_in_utc(self, recorder, tmp_path, monkeypatch):
        """Streamlinks {time} ist lokale Zeit - creation_time muss die echte UTC-Startzeit sein."""
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        try:
            ts_file = tmp_path / "2026-09-23_20-00_chan_[Gaming]_Title.ts"
            ts_file.write_bytes(b"data")
            calls = []

            class FakeCompleted:
                returncode = 0

            monkeypatch.setattr(recorder.subprocess, "run", lambda cmd, check: calls.append(cmd) or FakeCompleted())
            monkeypatch.setattr(recorder, "_write_xattrs", lambda path, attrs: None)

            recorder._remux_recording(tmp_path, "chan", self._cfg())

            assert "creation_time=2026-09-23T18:00:00Z" in calls[0]
            assert "RECORDING_START=2026-09-23T18:00:00Z" in calls[0]
            # Timecode bleibt lokale Zeit (wie Dateiname), nicht UTC
            tc_idx = calls[0].index("-timecode")
            assert calls[0][tc_idx + 1] == "20:00:00:00"
            assert "DATE=20260923" in calls[0]
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_seconds_precise_start_time(self, recorder, tmp_path, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        try:
            ts_file = tmp_path / "2026-09-23_20-00-37_chan_[Gaming]_Title.ts"
            ts_file.write_bytes(b"data")
            calls = []

            class FakeCompleted:
                returncode = 0

            monkeypatch.setattr(recorder.subprocess, "run", lambda cmd, check: calls.append(cmd) or FakeCompleted())
            monkeypatch.setattr(recorder, "_write_xattrs", lambda path, attrs: None)

            recorder._remux_recording(tmp_path, "chan", self._cfg())

            assert "RECORDING_START=2026-09-23T18:00:37Z" in calls[0]
            assert calls[0][calls[0].index("-timecode") + 1] == "20:00:37:00"
        finally:
            monkeypatch.undo()
            time.tzset()

    @pytest.mark.parametrize(("pattern", "expect_flag"), [("{title}.mov", True), ("{title}.MP4", True), ("{title}.mkv", False)])
    def test_movflags_only_for_mov_targets(self, recorder, tmp_path, monkeypatch, pattern, expect_flag):
        (tmp_path / "2026-09-23_20-00-00_chan_[Gaming]_Title.ts").write_bytes(b"data")
        calls = []

        class FakeCompleted:
            returncode = 0

        monkeypatch.setattr(recorder.subprocess, "run", lambda cmd, check: calls.append(cmd) or FakeCompleted())
        monkeypatch.setattr(recorder, "_write_xattrs", lambda path, attrs: None)

        recorder._remux_recording(tmp_path, "chan", {"filename_pattern": pattern, "use_ionice": False})

        assert ("use_metadata_tags" in calls[0]) is expect_flag

    def test_failed_ffmpeg_keeps_ts_file(self, recorder, tmp_path, monkeypatch):
        ts_file = tmp_path / "2026-09-23_20-00_chan_[Gaming]_Title.ts"
        ts_file.write_bytes(b"data")

        class FakeCompleted:
            returncode = 1

        monkeypatch.setattr(recorder.subprocess, "run", lambda cmd, check: FakeCompleted())

        recorder._remux_recording(tmp_path, "chan", self._cfg())

        assert ts_file.exists()

    def test_stale_ts_file_is_skipped(self, recorder, tmp_path, monkeypatch):
        ts_file = tmp_path / "2026-09-23_20-00_chan_[Gaming]_Title.ts"
        ts_file.write_bytes(b"data")
        old = ts_file.stat().st_mtime - 3600
        os.utime(ts_file, (old, old))
        monkeypatch.setattr(recorder.subprocess, "run", lambda *a, **k: pytest.fail("ffmpeg darf nicht laufen"))

        recorder._remux_recording(tmp_path, "chan", self._cfg())

        assert ts_file.exists()


class TestMonitorRecording:
    class FakeProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

        def kill(self):
            pass

    def test_stop_event_terminates_process(self, recorder, monkeypatch):
        import threading

        stop_event = threading.Event()
        stop_event.set()
        proc = self.FakeProc()
        cfg = {"sleep_interval": 60, "split_on_title_change": False, "title_check_interval": 60}

        split, _ = recorder._monitor_recording(proc, "https://twitch.tv/chan", "chan", cfg, stop_event)

        assert split is False
        assert proc.terminated

    def test_title_change_triggers_split(self, recorder, monkeypatch):
        import threading

        infos = iter([
            {"online": True, "title": "A", "category": "Gaming"},
            {"online": True, "title": "B", "category": "Gaming"},
        ])
        monkeypatch.setattr(recorder, "get_stream_info", lambda url, cfg: next(infos))
        monkeypatch.setattr(recorder.time, "sleep", lambda s: None)
        proc = self.FakeProc()
        cfg = {"sleep_interval": 60, "split_on_title_change": True, "title_check_interval": 0}

        split, _ = recorder._monitor_recording(proc, "https://twitch.tv/chan", "chan", cfg, threading.Event())

        assert split is True
        assert proc.terminated
