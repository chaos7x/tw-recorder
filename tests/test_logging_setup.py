"""
Tests für _is_syslog_daemon_running() und _resolve_log_file_path().

Beide Funktionen greifen auf absolute, systemweite Pfade zu (/proc, /log,
/var/log/<app>). Das Filesystem-Verhalten wird per monkeypatch simuliert,
damit die Tests unabhängig davon laufen, was auf der jeweiligen
Test-Maschine tatsächlich installiert/gemountet ist.
"""


class FakePidDir:
    """Simuliert ein Path-Objekt für /proc/<pid> mit steuerbarem comm-Inhalt."""

    def __init__(self, name, comm_content=None, raise_on_read=False):
        self.name = name
        self._comm_content = comm_content
        self._raise_on_read = raise_on_read

    def __truediv__(self, other):
        return FakeCommFile(self._comm_content, self._raise_on_read)


class FakeCommFile:
    def __init__(self, content, raise_on_read):
        self._content = content
        self._raise_on_read = raise_on_read

    def read_text(self, encoding="utf-8", errors="ignore"):
        if self._raise_on_read:
            raise OSError("Permission denied")
        return self._content


class TestIsSyslogDaemonRunning:
    def test_returns_false_without_proc_directory(self, logging_setup, monkeypatch):
        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: False)
        assert logging_setup._is_syslog_daemon_running() is False

    def test_detects_running_rsyslogd(self, logging_setup, monkeypatch):
        fake_pids = [FakePidDir("111", "bash"), FakePidDir("222", "rsyslogd")]

        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(logging_setup.Path, "iterdir", lambda self: iter(fake_pids))

        assert logging_setup._is_syslog_daemon_running() is True

    def test_returns_false_when_no_matching_process(self, logging_setup, monkeypatch):
        fake_pids = [FakePidDir("111", "bash"), FakePidDir("222", "python3")]

        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(logging_setup.Path, "iterdir", lambda self: iter(fake_pids))

        assert logging_setup._is_syslog_daemon_running() is False

    def test_unreadable_comm_file_is_skipped(self, logging_setup, monkeypatch):
        fake_pids = [
            FakePidDir("111", raise_on_read=True),
            FakePidDir("222", "syslog-ng"),
        ]

        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(logging_setup.Path, "iterdir", lambda self: iter(fake_pids))

        assert logging_setup._is_syslog_daemon_running() is True


class TestResolveLogFilePath:
    def test_explicit_path_wins(self, logging_setup):
        result = logging_setup._resolve_log_file_path("myapp", "/custom/path.log")
        assert result == logging_setup.Path("/custom/path.log")

    def test_prefers_docker_log_dir_if_present(self, logging_setup, monkeypatch):
        real_is_dir = logging_setup.Path.is_dir
        monkeypatch.setattr(
            logging_setup.Path, "is_dir",
            lambda self: True if str(self) == "/log" else real_is_dir(self)
        )

        result = logging_setup._resolve_log_file_path("myapp", "")
        assert result == logging_setup.Path("/log/myapp.log")

    def test_falls_back_to_var_log_if_writable(self, logging_setup, monkeypatch, tmp_path):
        real_is_dir = logging_setup.Path.is_dir
        monkeypatch.setattr(
            logging_setup.Path, "is_dir",
            lambda self: False if str(self) == "/log" else real_is_dir(self)
        )
        # Verhindert echtes Anlegen von /var/log/<app> während des Tests
        monkeypatch.setattr(logging_setup.Path, "mkdir", lambda self, **k: None)
        monkeypatch.setattr(logging_setup.os, "access", lambda *a, **k: True)

        result = logging_setup._resolve_log_file_path("myapp-test", "")
        assert result == logging_setup.Path("/var/log/myapp-test/myapp-test.log")

    def test_final_fallback_when_var_log_unwritable(self, logging_setup, monkeypatch):
        real_is_dir = logging_setup.Path.is_dir
        monkeypatch.setattr(
            logging_setup.Path, "is_dir",
            lambda self: False if str(self) == "/log" else real_is_dir(self)
        )

        def raise_oserror(self, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr(logging_setup.Path, "mkdir", raise_oserror)

        result = logging_setup._resolve_log_file_path("myapp-test", "")
        assert result.name == "myapp-test.log"
        assert "/var/log" not in str(result)
