"""
Tests für _is_syslog_daemon_running(), _is_dedicated_mount() und
_resolve_log_file_path().

Diese Funktionen greifen auf absolute, systemweite Pfade zu (/proc, /log,
/var/log/<app>). Das Filesystem-Verhalten wird per monkeypatch simuliert,
damit die Tests unabhängig davon laufen, was auf der jeweiligen
Test-Maschine tatsächlich installiert/gemountet ist.
"""

import logging

import pytest


@pytest.fixture
def clean_root_logger():
    """
    setup_logging() manipuliert den echten Root-Logger direkt (nicht nur
    logging.basicConfig()) - ohne Aufräumen blieben nach einem direkten Test
    von setup_logging() zusätzliche Handler (u.a. ein offener
    RotatingFileHandler auf eine tmp_path-Datei) über das Testende hinaus am
    Prozess-weiten Root-Logger hängen.
    """
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    yield
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
        h.close()
    for h in original_handlers:
        root_logger.addHandler(h)
    root_logger.setLevel(original_level)


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


class _FakeStat:
    def __init__(self, st_dev):
        self.st_dev = st_dev


class TestIsDedicatedMount:
    """
    Testet _is_dedicated_mount() - unterscheidet ein echtes Docker-Volume/
    Bind-Mount von einem gewöhnlichen, per `mkdir -p` fest ins Image
    gebackenen Verzeichnis (z.B. /log), das ohne diese Unterscheidung
    fälschlich als "gemountet" durchgehen würde.

    stat() wird bewusst NICHT pauschal für alle Path-Instanzen ersetzt,
    sondern nur für die konkret getesteten Pfade - pytest selbst ruft
    Path.stat() intern für eigene Zwecke auf (z.B. bei der Traceback-
    Formatierung), ein pauschaler Patch ohne Fallback auf die echte
    Methode bricht das mit einem schwer zu diagnostizierenden
    INTERNALERROR statt eines normalen Testfehlers.
    """

    def test_returns_false_if_path_does_not_exist(self, logging_setup, monkeypatch):
        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: False)
        assert logging_setup._is_dedicated_mount(logging_setup.Path("/log")) is False

    def test_returns_false_for_plain_baked_in_directory_same_device(self, logging_setup, monkeypatch):
        """Gleiche st_dev wie das Elternverzeichnis = kein echter Mount, nur ein normaler Ordner."""
        real_stat = logging_setup.Path.stat
        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(
            logging_setup.Path, "stat",
            lambda self: _FakeStat(st_dev=1) if str(self) in ("/log", "/") else real_stat(self)
        )

        assert logging_setup._is_dedicated_mount(logging_setup.Path("/log")) is False

    def test_returns_true_for_real_mount_different_device(self, logging_setup, monkeypatch):
        """Unterschiedliche st_dev zum Elternverzeichnis = tatsächlich eingehängtes Volume/Bind-Mount."""
        real_stat = logging_setup.Path.stat
        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: True)

        def fake_stat(self):
            if str(self) == "/log":
                return _FakeStat(st_dev=2)
            if str(self) == "/":
                return _FakeStat(st_dev=1)
            return real_stat(self)

        monkeypatch.setattr(logging_setup.Path, "stat", fake_stat)

        assert logging_setup._is_dedicated_mount(logging_setup.Path("/log")) is True

    def test_permission_error_on_stat_returns_false(self, logging_setup, monkeypatch):
        real_stat = logging_setup.Path.stat
        monkeypatch.setattr(logging_setup.Path, "is_dir", lambda self: True)

        def raise_or_real_stat(self):
            if str(self) in ("/log", "/"):
                raise OSError("Permission denied")
            return real_stat(self)

        monkeypatch.setattr(logging_setup.Path, "stat", raise_or_real_stat)

        assert logging_setup._is_dedicated_mount(logging_setup.Path("/log")) is False


class TestResolveLogFilePath:
    def test_explicit_path_wins(self, logging_setup):
        result = logging_setup._resolve_log_file_path("myapp", "/custom/path.log")
        assert result == logging_setup.Path("/custom/path.log")

    def test_prefers_docker_log_dir_if_dedicated_mount(self, logging_setup, monkeypatch):
        monkeypatch.setattr(logging_setup, "_is_dedicated_mount", lambda p: str(p) == "/log")

        result = logging_setup._resolve_log_file_path("myapp", "")
        assert result == logging_setup.Path("/log/myapp.log")

    def test_falls_back_to_var_log_if_writable(self, logging_setup, monkeypatch, tmp_path):
        monkeypatch.setattr(logging_setup, "_is_dedicated_mount", lambda p: False)
        # Verhindert echtes Anlegen von /var/log/<app> während des Tests
        monkeypatch.setattr(logging_setup.Path, "mkdir", lambda self, **k: None)
        monkeypatch.setattr(logging_setup.os, "access", lambda *a, **k: True)

        result = logging_setup._resolve_log_file_path("myapp-test", "")
        assert result == logging_setup.Path("/var/log/myapp-test/myapp-test.log")

    def test_final_fallback_when_var_log_unwritable(self, logging_setup, monkeypatch):
        monkeypatch.setattr(logging_setup, "_is_dedicated_mount", lambda p: False)

        def raise_oserror(self, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr(logging_setup.Path, "mkdir", raise_oserror)

        result = logging_setup._resolve_log_file_path("myapp-test", "")
        assert result.name == "myapp-test.log"
        assert "/var/log" not in str(result)


class TestSetupLoggingFileTrigger:
    """
    Deckt den in yt-upload gemeldeten und hier identisch vorhandenen Bug ab:
    ohne echtes /log-Volume (nur das vom Dockerfile fest angelegte
    Verzeichnis) darf setup_logging() KEINEN Datei-Handler mehr aktivieren.
    """

    def test_plain_baked_in_log_dir_without_real_mount_stays_stdout_only(self, logging_setup, monkeypatch, clean_root_logger):
        monkeypatch.setattr(logging_setup, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(logging_setup, "_is_syslog_daemon_running", lambda: False)
        monkeypatch.delenv("LOG_FILE", raising=False)

        logging_setup.setup_logging(cfg={})

        root_handlers = logging.getLogger().handlers
        assert len(root_handlers) == 1
        assert isinstance(root_handlers[0], logging.StreamHandler)

    def test_real_log_volume_mount_adds_rotating_file_handler(self, logging_setup, monkeypatch, tmp_path, clean_root_logger):
        """Docker-Volume: niemand sonst rotiert die Datei -> eigene RotatingFileHandler-Rotation noetig."""
        monkeypatch.setattr(logging_setup, "_is_dedicated_mount", lambda p: True)
        monkeypatch.setattr(logging_setup, "_is_syslog_daemon_running", lambda: False)
        monkeypatch.setattr(
            logging_setup, "_resolve_log_file_path",
            lambda app_name, explicit_path: tmp_path / "test.log"
        )
        monkeypatch.delenv("LOG_FILE", raising=False)

        logging_setup.setup_logging(cfg={})

        handlers = logging.getLogger().handlers
        assert len(handlers) == 2
        assert isinstance(handlers[1], logging_setup.logging.handlers.RotatingFileHandler)

    def test_syslog_daemon_detected_adds_plain_file_handler_without_own_rotation(self, logging_setup, monkeypatch, tmp_path, clean_root_logger):
        """
        Ein laufender Syslog-Daemon impliziert praktisch immer auch logrotate
        (siehe logrotate.d/tw-recorder) - die App soll dort NICHT zusaetzlich
        selbst per RotatingFileHandler rotieren, sonst kommen sich beide
        Mechanismen in die Quere.
        """
        monkeypatch.setattr(logging_setup, "_is_dedicated_mount", lambda p: False)
        monkeypatch.setattr(logging_setup, "_is_syslog_daemon_running", lambda: True)
        monkeypatch.setattr(
            logging_setup, "_resolve_log_file_path",
            lambda app_name, explicit_path: tmp_path / "test.log"
        )
        monkeypatch.delenv("LOG_FILE", raising=False)

        logging_setup.setup_logging(cfg={})

        handlers = logging.getLogger().handlers
        assert len(handlers) == 2
        assert isinstance(handlers[1], logging.FileHandler)
        assert not isinstance(handlers[1], logging_setup.logging.handlers.RotatingFileHandler)
