"""Tests für skip.py: tw-recorder --skip/--unskip und die Stream-ID-Bindung."""

import os

import pytest


@pytest.fixture
def skip_dir(config, tmp_path, monkeypatch):
    d = tmp_path / "skip"
    monkeypatch.setattr(config, "SKIP_DIR", d)
    return d


class TestNormalizeChannel:
    @pytest.mark.parametrize("raw", [
        "SomeStreamer", "https://www.twitch.tv/SomeStreamer", "twitch.tv/somestreamer/", " somestreamer ",
    ])
    def test_accepts_name_and_url_forms(self, skip, raw):
        assert skip.normalize_channel(raw) == "somestreamer"

    @pytest.mark.parametrize("raw", ["", "../etc/passwd", "a/b", "x" * 26, "foo bar", "foo.skip"])
    def test_rejects_invalid_names(self, skip, raw):
        assert skip.normalize_channel(raw) is None


class TestEnsureSkipDir:
    def test_creates_own_dir(self, skip, skip_dir):
        assert skip.ensure_skip_dir() is True
        assert skip_dir.is_dir()
        assert skip_dir.stat().st_uid == os.geteuid()

    def test_symlink_is_rejected(self, skip, skip_dir, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        skip_dir.symlink_to(target)

        assert skip.ensure_skip_dir() is False


class TestShouldSkip:
    def test_without_marker(self, skip, skip_dir):
        skip.ensure_skip_dir()
        assert skip.should_skip("chan", "123") is False

    def test_unbound_marker_gets_bound_to_current_stream(self, skip, skip_dir):
        skip.ensure_skip_dir()
        skip.write_marker("chan")

        assert skip.should_skip("chan", "123") is True
        assert skip.read_marker("chan")["stream_id"] == "123"
        # derselbe Stream bleibt übersprungen
        assert skip.should_skip("chan", "123") is True

    def test_new_stream_lifts_skip(self, skip, skip_dir):
        skip.ensure_skip_dir()
        skip.write_marker("chan", stream_id="123")

        assert skip.should_skip("chan", "456") is False
        assert skip.read_marker("chan") is None

    def test_without_api_marker_stays(self, skip, skip_dir):
        skip.ensure_skip_dir()
        skip.write_marker("chan", stream_id="123")

        assert skip.should_skip("chan", None) is True
        assert skip.read_marker("chan") is not None

    def test_unbound_without_api_skips(self, skip, skip_dir):
        skip.ensure_skip_dir()
        skip.write_marker("chan")

        assert skip.should_skip("chan", None) is True
        assert skip.read_marker("chan")["stream_id"] is None

    def test_corrupt_marker_counts_as_skip(self, skip, skip_dir):
        skip.ensure_skip_dir()
        (skip_dir / "chan.skip").write_text("{kaputt")

        assert skip.should_skip("chan", "1") is True

    def test_symlink_marker_is_ignored(self, skip, skip_dir, tmp_path):
        skip.ensure_skip_dir()
        target = tmp_path / "t.json"
        target.write_text("{}")
        (skip_dir / "chan.skip").symlink_to(target)

        assert skip.is_requested("chan") is False
        assert skip.read_marker("chan") is None


class TestCli:
    def test_skip_requires_running_daemon_dir(self, skip, skip_dir, capsys):
        assert skip.cli_skip("chan", keep=False) == 1
        assert "Läuft der Dämon" in capsys.readouterr().out

    def test_skip_writes_marker(self, skip, skip_dir):
        skip.ensure_skip_dir()

        assert skip.cli_skip("https://twitch.tv/Chan", keep=True) == 0

        marker = skip.read_marker("chan")
        assert marker == {"stream_id": None, "keep": True, "created": marker["created"]}
        assert (skip_dir / "chan.skip").stat().st_mode & 0o777 == 0o644
        assert not list(skip_dir.glob(".*.tmp"))

    def test_invalid_channel(self, skip, skip_dir):
        skip.ensure_skip_dir()
        assert skip.cli_skip("../x", keep=False) == 2
        assert not list(skip_dir.iterdir())

    def test_unskip_and_list(self, skip, skip_dir, capsys):
        skip.ensure_skip_dir()
        skip.write_marker("chan", stream_id="42")

        assert skip.cli_list() == 0
        assert "chan: Stream-ID 42" in capsys.readouterr().out
        assert skip.cli_unskip("chan") == 0
        assert skip.read_marker("chan") is None
        assert skip.cli_unskip("chan") == 0
        assert "kein Skip" in capsys.readouterr().out
