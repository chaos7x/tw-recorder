"""Tests für write_heartbeat() und run_healthcheck() (healthcheck.py).

HEALTH_FILE/HEALTH_STALE_SECONDS leben in config.py; healthcheck.py liest sie
zur Laufzeit als config.HEALTH_FILE - Patches müssen daher auf dem
config-Modul erfolgen, auch wenn die getesteten Funktionen in healthcheck.py
liegen.
"""

import os
import time


class TestWriteHeartbeatAndHealthcheck:
    def test_missing_heartbeat_file_is_unhealthy(self, healthcheck, config, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HEALTH_FILE", tmp_path / "does-not-exist.health")
        assert healthcheck.run_healthcheck() == 1

    def test_fresh_heartbeat_is_healthy(self, healthcheck, config, tmp_path, monkeypatch):
        health_file = tmp_path / "recorder.health"
        monkeypatch.setattr(config, "HEALTH_FILE", health_file)
        monkeypatch.setattr(config, "HEALTH_STALE_SECONDS", 60)

        healthcheck.write_heartbeat()

        assert health_file.is_file()
        assert healthcheck.run_healthcheck() == 0

    def test_stale_heartbeat_is_unhealthy(self, healthcheck, config, tmp_path, monkeypatch):
        health_file = tmp_path / "recorder.health"
        monkeypatch.setattr(config, "HEALTH_FILE", health_file)
        monkeypatch.setattr(config, "HEALTH_STALE_SECONDS", 60)

        healthcheck.write_heartbeat()
        stale_time = time.time() - 120
        os.utime(health_file, (stale_time, stale_time))

        assert healthcheck.run_healthcheck() == 1

    def test_write_heartbeat_does_not_raise_if_unwritable(self, healthcheck, config, monkeypatch):
        """Ein Healthcheck-Problem darf den Recorder nicht zum Absturz bringen."""

        class UnwritablePath:
            def write_text(self, content):
                raise OSError("Permission denied")

        monkeypatch.setattr(config, "HEALTH_FILE", UnwritablePath())

        healthcheck.write_heartbeat()  # darf nicht raisen
