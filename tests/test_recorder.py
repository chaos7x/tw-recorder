"""
Tests für _parse_recorded_filename(), _write_xattrs() und
_ensure_channel_dir() (recorder.py).

Der alte naive "_"-Split zwischen Kategorie und Titel (cat_title_parts =
rest_after_channel.split("_", 1)) hat mehrteilige Kategorienamen falsch
zugeordnet, sobald die Kategorie selbst einen "_" enthielt (z.B. durch
Leerzeichen-Ersetzung) - der erste Wortteil landete als "Kategorie", der Rest
fälschlich im Titel. Die Kategorie wird jetzt in eckigen Klammern kodiert
(siehe out_pattern in record_loop()), was die Trennung eindeutig macht.
"""

import os
import stat
from pathlib import Path

import pytest


class TestParseRecordedFilename:
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
