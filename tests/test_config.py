"""
Tests für get_config_files_state, get_config_hash, load_config, parse_streamers
und get_extra_args.
"""


def _write_main_config(tmp_path, content):
    path = tmp_path / "recorder.conf"
    path.write_text(content, encoding="utf-8")
    return path


class TestConfigHashing:
    def test_hash_changes_when_file_content_changes(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, "[general]\nstorage_dir = /storage\n")

        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        hash_before, _ = tw_recorder.get_config_hash()

        main_conf.write_text(main_conf.read_text() + "\nsleep_interval = 30\n")
        hash_after, _ = tw_recorder.get_config_hash()

        assert hash_before != hash_after

    def test_hash_stable_without_changes(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, "[general]\nstorage_dir = /storage\n")

        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        hash1, _ = tw_recorder.get_config_hash()
        hash2, _ = tw_recorder.get_config_hash()

        assert hash1 == hash2

    def test_conf_d_files_included_in_state(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, "[general]\nstorage_dir = /storage\n")
        (conf_d / "extra.conf").write_text("[channels]\nfoo = best\n", encoding="utf-8")

        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        state = tw_recorder.get_config_files_state()
        names = {p.name for p in state.keys()}

        assert names == {"recorder.conf", "extra.conf"}

    def test_missing_config_files_yield_empty_state(self, tw_recorder, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", tmp_path / "does-not-exist.conf")
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", tmp_path / "does-not-exist-dir")

        assert tw_recorder.get_config_files_state() == {}


class TestLoadConfig:
    def test_reads_general_settings(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, (
            "[general]\n"
            "storage_dir = /custom-storage\n"
            "sleep_interval = 42\n"
        ))
        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        cfg = tw_recorder.load_config()

        assert cfg["storage_dir"] == tw_recorder.Path("/custom-storage")
        assert cfg["sleep_interval"] == 42

    def test_falls_back_to_defaults_without_config_file(self, tw_recorder, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", tmp_path / "missing.conf")
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", tmp_path / "missing-dir")
        monkeypatch.delenv("STORAGE_DIR", raising=False)
        monkeypatch.delenv("SLEEP_INTERVAL", raising=False)

        cfg = tw_recorder.load_config()

        assert cfg["storage_dir"] == tw_recorder.Path("/storage")
        assert cfg["sleep_interval"] == 15

    def test_conf_d_overrides_main_config(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, "[general]\nsleep_interval = 15\n")
        (conf_d / "override.conf").write_text("[general]\nsleep_interval = 99\n", encoding="utf-8")

        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        cfg = tw_recorder.load_config()

        assert cfg["sleep_interval"] == 99


class TestParseStreamers:
    def test_strips_common_twitch_prefixes(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, (
            "[channels]\n"
            "plainname = best\n"
            "https://www.twitch.tv/WithHttpsWww/ = 720p\n"
            "twitch.tv/NoScheme = worst\n"
        ))
        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        streamers = tw_recorder.parse_streamers()

        assert streamers == {
            "https://twitch.tv/plainname": "best",
            "https://twitch.tv/WithHttpsWww": "720p",
            "https://twitch.tv/NoScheme": "worst",
        }

    def test_defaults_to_best_quality_when_empty(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, "[channels]\nsomechannel =\n")
        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        streamers = tw_recorder.parse_streamers()

        assert streamers == {"https://twitch.tv/somechannel": "best"}

    def test_no_channels_section_returns_empty_dict(self, tw_recorder, tmp_path, monkeypatch):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        main_conf = _write_main_config(tmp_path, "[general]\nstorage_dir = /storage\n")
        monkeypatch.setattr(tw_recorder, "CONFIG_FILE", main_conf)
        monkeypatch.setattr(tw_recorder, "CONF_D_DIR", conf_d)

        assert tw_recorder.parse_streamers() == {}


class TestGetExtraArgs:
    def test_no_token_no_webbrowser(self, tw_recorder):
        args = tw_recorder.get_extra_args({"user_token": "", "webbrowser": False})
        assert args == ["--webbrowser=False"]

    def test_with_token_and_webbrowser_enabled(self, tw_recorder):
        args = tw_recorder.get_extra_args({"user_token": "abc123", "webbrowser": True})
        assert args == ["--twitch-api-header", "Authorization=OAuth abc123"]

    def test_with_token_and_webbrowser_disabled(self, tw_recorder):
        args = tw_recorder.get_extra_args({"user_token": "abc123", "webbrowser": False})
        assert args == ["--twitch-api-header", "Authorization=OAuth abc123", "--webbrowser=False"]
