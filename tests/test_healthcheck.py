"""Tests für write_heartbeat() und run_healthcheck()."""

import os
import time


class TestWriteHeartbeatAndHealthcheck:
    def test_missing_heartbeat_file_is_unhealthy(self, tw_recorder, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_recorder, "HEALTH_FILE", tmp_path / "does-not-exist.health")
        assert tw_recorder.run_healthcheck() == 1

    def test_fresh_heartbeat_is_healthy(self, tw_recorder, tmp_path, monkeypatch):
        health_file = tmp_path / "recorder.health"
        monkeypatch.setattr(tw_recorder, "HEALTH_FILE", health_file)
        monkeypatch.setattr(tw_recorder, "HEALTH_STALE_SECONDS", 60)

        tw_recorder.write_heartbeat()

        assert health_file.is_file()
        assert tw_recorder.run_healthcheck() == 0

    def test_stale_heartbeat_is_unhealthy(self, tw_recorder, tmp_path, monkeypatch):
        health_file = tmp_path / "recorder.health"
        monkeypatch.setattr(tw_recorder, "HEALTH_FILE", health_file)
        monkeypatch.setattr(tw_recorder, "HEALTH_STALE_SECONDS", 60)

        tw_recorder.write_heartbeat()
        stale_time = time.time() - 120
        os.utime(health_file, (stale_time, stale_time))

        assert tw_recorder.run_healthcheck() == 1

    def test_write_heartbeat_does_not_raise_if_unwritable(self, tw_recorder, monkeypatch):
        """Ein Healthcheck-Problem darf den Recorder nicht zum Absturz bringen."""

        class UnwritablePath:
            def write_text(self, content):
                raise OSError("Permission denied")

        monkeypatch.setattr(tw_recorder, "HEALTH_FILE", UnwritablePath())

        tw_recorder.write_heartbeat()  # darf nicht raisen
