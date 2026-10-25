"""
Tests für den experimentellen Prototyp (nicht Teil von CI: pyproject.toml
begrenzt testpaths auf tests/). Manuell ausführen mit:

    pytest -v experimental/

Braucht streamlink >= 8.2 als Library sowie ffmpeg/ffprobe. Statt Twitch
dient ein lokal per ffmpeg erzeugter Live-HLS-Stream (hls://-Plugin) als
Quelle - damit sind API-Pfad, Pipe, Tags und Signal-Verhalten testbar, nur
die Twitch-spezifische Auflösung (GQL/Usher) nicht.
"""

import functools
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("streamlink")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("ffmpeg/ffprobe nicht im PATH", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent))
import streamlink_api_recorder as rec  # noqa: E402

SCRIPT = Path(__file__).parent / "streamlink_api_recorder.py"


@pytest.fixture
def live_hls(tmp_path):
    """Live-HLS (ohne ENDLIST, solange der Encoder läuft) über einen lokalen HTTP-Server."""
    hls_dir = tmp_path / "hls"
    hls_dir.mkdir()
    enc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
         "-hls_flags", "delete_segments+omit_endlist",
         str(hls_dir / "live.m3u8")],
        stdin=subprocess.DEVNULL,
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(hls_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 15
    while not (hls_dir / "live.m3u8").exists() or len(list(hls_dir.glob("*.ts"))) < 3:
        assert time.monotonic() < deadline, "HLS-Encoder liefert keine Segmente"
        time.sleep(0.2)
    yield f"hls://http://127.0.0.1:{httpd.server_port}/live.m3u8"
    httpd.shutdown()
    enc.terminate()
    enc.wait()


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    return json.loads(out.stdout or "{}")


def _wait_for_growth(out_dir: Path, min_bytes: int = 200_000, timeout: float = 30) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = list(out_dir.glob("*.mkv"))
        if files and files[0].stat().st_size >= min_bytes:
            return files[0]
        time.sleep(0.2)
    raise AssertionError("Aufnahme wächst nicht")


def test_twitch_api_header_reaches_plugin_options():
    """Twitch-Token landet ohne Netzwerkzugriff in den Plugin-Optionen (Streamlink-8.x-API)."""
    from streamlink import Streamlink
    from streamlink.options import Options

    session = Streamlink({"webbrowser": False})
    name, plugin_cls, url = session.resolve_url("https://twitch.tv/somechannel")
    assert name == "twitch"
    opts = Options()
    opts.set("api-header", [("Authorization", "OAuth abc")])
    plugin = plugin_cls(session, url, opts)
    assert plugin.get_option("api-header") == [("Authorization", "OAuth abc")]
    assert session.get_option("webbrowser") is False


def test_sigterm_finalizes_mkv_with_tags(live_hls, tmp_path):
    out_dir = tmp_path / "out"
    proc = subprocess.Popen([sys.executable, str(SCRIPT), live_hls, str(out_dir)])
    mkv = _wait_for_growth(out_dir)
    time.sleep(3)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 0

    info = _probe(mkv)
    fmt = info["format"]
    tags = {k.upper(): v for k, v in fmt["tags"].items()}
    assert fmt["format_name"].startswith("matroska")
    assert float(fmt["duration"]) > 2  # Dauer im Header = sauber finalisiert
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert tags["RECORDING_START"].endswith("Z")
    assert tags["ARTIST"] == "live.m3u8"
    assert "TIMECODE" in tags
    # Cues (Seek-Index) vorhanden -> mkvinfo-freier Check per Seek ans Ende
    seek = subprocess.run(
        ["ffprobe", "-v", "error", "-read_intervals", "99999%+1", "-show_entries",
         "packet=pts_time", "-of", "csv", str(mkv)],
        capture_output=True, text=True, check=False,
    )
    assert seek.returncode == 0


def test_sigint_in_terminal_does_not_kill_ffmpeg_first(live_hls, tmp_path):
    """Strg+C trifft die ganze Prozessgruppe; ffmpeg läuft in eigener Session und finalisiert trotzdem."""
    out_dir = tmp_path / "out"
    proc = subprocess.Popen([sys.executable, str(SCRIPT), live_hls, str(out_dir)],
                            start_new_session=True)
    mkv = _wait_for_growth(out_dir)
    time.sleep(2)
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=30) == 0
    assert float(_probe(mkv)["format"]["duration"]) > 2


def test_whole_cgroup_sigterm_like_systemd(live_hls, tmp_path):
    """Wie systemd KillMode=control-group: SIGTERM gleichzeitig an Python UND ffmpeg."""
    out_dir = tmp_path / "out"
    proc = subprocess.Popen([sys.executable, str(SCRIPT), live_hls, str(out_dir)])
    mkv = _wait_for_growth(out_dir)
    time.sleep(2)
    ff_pids = subprocess.run(["pgrep", "-P", str(proc.pid), "ffmpeg"],
                             capture_output=True, text=True, check=False).stdout.split()
    assert ff_pids
    for pid in [proc.pid, *map(int, ff_pids)]:
        os.kill(pid, signal.SIGTERM)
    proc.wait(timeout=30)
    assert float(_probe(mkv)["format"]["duration"]) > 2


def test_sigkill_of_everything_leaves_unfinalized_mkv(live_hls, tmp_path):
    """
    Absturz/OOM/Stromausfall-Simulation: alles per SIGKILL. Dokumentiert den
    Nachteil gegenüber .ts: Datei ist zwar lesbar, aber ohne Dauer im Header.
    """
    out_dir = tmp_path / "out"
    proc = subprocess.Popen([sys.executable, str(SCRIPT), live_hls, str(out_dir)])
    mkv = _wait_for_growth(out_dir)
    time.sleep(2)
    ff_pids = subprocess.run(["pgrep", "-P", str(proc.pid), "ffmpeg"],
                             capture_output=True, text=True, check=False).stdout.split()
    for pid in [*map(int, ff_pids), proc.pid]:
        os.kill(pid, signal.SIGKILL)
    proc.wait()

    fmt = _probe(mkv).get("format", {})
    assert "duration" not in fmt or float(fmt["duration"]) <= 0
    # Reparatur per Remux funktioniert aber:
    fixed = tmp_path / "fixed.mkv"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(mkv), "-c", "copy", str(fixed)], check=True)
    assert float(_probe(fixed)["format"]["duration"]) > 1


def test_stream_end_finishes_cleanly(tmp_path):
    """VOD-HLS mit ENDLIST: read() liefert b"" -> regulärer Abschluss."""
    hls_dir = tmp_path / "vod"
    hls_dir.mkdir()
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
         str(hls_dir / "vod.m3u8")],
        check=True,
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(hls_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = rec.record(f"hls://http://127.0.0.1:{httpd.server_port}/vod.m3u8", tmp_path / "out")
    finally:
        httpd.shutdown()
    assert out and out.exists()
    assert 3 < float(_probe(out)["format"]["duration"]) < 5
    assert oct(out.stat().st_mode & 0o777) == oct(0o664)
