"""
Tests für die Twitch-Helix-API-Anbindung: get_app_access_token,
check_stream_online, get_stream_info.

urllib.request.urlopen wird komplett gemockt (kein echter Netzwerk-Call).
Die globalen Token-/Verfügbarkeits-Variablen werden durch die autouse-Fixture
reset_twitch_globals in conftest.py vor jedem Test zurückgesetzt.
"""

import json

import urllib.error


class FakeHTTPResponse:
    """Minimales Double für das Objekt, das urllib.request.urlopen liefert."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


TWITCH_CFG = {"client_id": "cid", "client_secret": "csecret"}


class TestGetAppAccessToken:
    def test_successful_fetch_returns_token(self, twitch_api, monkeypatch):
        monkeypatch.setattr(
            twitch_api.urllib.request, "urlopen",
            lambda req, timeout=5: FakeHTTPResponse({"access_token": "TOK1", "expires_in": 3600})
        )

        token = twitch_api.get_app_access_token("cid", "csecret")

        assert token == "TOK1"
        assert twitch_api.token_expires_at > 0

    def test_cached_token_is_reused_without_new_request(self, twitch_api, monkeypatch):
        monkeypatch.setattr(
            twitch_api.urllib.request, "urlopen",
            lambda req, timeout=5: FakeHTTPResponse({"access_token": "TOK1", "expires_in": 3600})
        )
        first = twitch_api.get_app_access_token("cid", "csecret")

        call_count = {"n": 0}

        def counting_urlopen(req, timeout=5):
            call_count["n"] += 1
            return FakeHTTPResponse({"access_token": "SHOULD_NOT_BE_USED", "expires_in": 3600})

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", counting_urlopen)
        second = twitch_api.get_app_access_token("cid", "csecret")

        assert second == first == "TOK1"
        assert call_count["n"] == 0

    def test_force_refresh_bypasses_cache(self, twitch_api, monkeypatch):
        monkeypatch.setattr(
            twitch_api.urllib.request, "urlopen",
            lambda req, timeout=5: FakeHTTPResponse({"access_token": "TOK1", "expires_in": 3600})
        )
        twitch_api.get_app_access_token("cid", "csecret")

        monkeypatch.setattr(
            twitch_api.urllib.request, "urlopen",
            lambda req, timeout=5: FakeHTTPResponse({"access_token": "TOK2", "expires_in": 3600})
        )
        refreshed = twitch_api.get_app_access_token("cid", "csecret", force_refresh=True)

        assert refreshed == "TOK2"

    def test_missing_credentials_returns_none_without_request(self, twitch_api, monkeypatch):
        def fail_if_called(*a, **k):
            raise AssertionError("urlopen sollte bei fehlenden Credentials nicht aufgerufen werden")

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", fail_if_called)

        assert twitch_api.get_app_access_token("", "") is None

    def test_network_error_returns_none(self, twitch_api, monkeypatch):
        def raise_url_error(req, timeout=5):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", raise_url_error)

        assert twitch_api.get_app_access_token("cid", "csecret") is None


class TestCheckStreamOnline:
    def test_returns_true_when_stream_is_live(self, twitch_api, monkeypatch):
        responses = iter([
            FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600}),
            FakeHTTPResponse({"data": [{"type": "live"}]}),
        ])
        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", lambda req, timeout=5: next(responses))

        assert twitch_api.check_stream_online("https://twitch.tv/foo", TWITCH_CFG) is True

    def test_returns_false_when_stream_is_offline(self, twitch_api, monkeypatch):
        responses = iter([
            FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600}),
            FakeHTTPResponse({"data": []}),
        ])
        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", lambda req, timeout=5: next(responses))

        assert twitch_api.check_stream_online("https://twitch.tv/foo", TWITCH_CFG) is False

    def test_401_triggers_token_refresh_and_retries(self, twitch_api, monkeypatch):
        call_log = []

        def urlopen_sequence(req, timeout=5):
            call_log.append(1)
            if len(call_log) == 1:
                return FakeHTTPResponse({"access_token": "TOK-OLD", "expires_in": 3600})
            if len(call_log) == 2:
                raise urllib.error.HTTPError(getattr(req, "full_url", "url"), 401, "unauthorized", {}, None)
            if len(call_log) == 3:
                return FakeHTTPResponse({"access_token": "TOK-NEW", "expires_in": 3600})
            return FakeHTTPResponse({"data": [{"type": "live"}]})

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", urlopen_sequence)

        result = twitch_api.check_stream_online("https://twitch.tv/foo", TWITCH_CFG)

        assert result is True
        assert len(call_log) == 4

    def test_falls_back_to_streamlink_cli_without_credentials(self, twitch_api, monkeypatch):
        cfg_no_creds = {"client_id": "", "client_secret": "", "user_token": "", "webbrowser": False}

        monkeypatch.setattr(
            twitch_api.subprocess, "run",
            lambda *a, **k: type("Result", (), {"returncode": 0})()
        )

        assert twitch_api.check_stream_online("https://twitch.tv/foo", cfg_no_creds) is True

    def test_channel_is_url_encoded_in_query_string(self, twitch_api, monkeypatch):
        """
        channel landet unescaped als f-string direkt in der Query - ohne
        urllib.parse.quote() könnten Sonderzeichen (z.B. aus einer verunglückten
        Config-URL) die Query verfälschen oder zusätzliche Parameter einschleusen.
        """
        captured = {}
        responses = iter([
            FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600}),
            FakeHTTPResponse({"data": []}),
        ])

        def urlopen_capture(req, timeout=5):
            captured["url"] = req.full_url
            return next(responses)

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", urlopen_capture)

        twitch_api.check_stream_online("https://twitch.tv/foo bar&baz", TWITCH_CFG)

        assert "user_login=foo%20bar%26baz" in captured["url"]

    def test_api_unavailable_after_generic_error_sets_cooldown(self, twitch_api, monkeypatch):
        """
        Der Cooldown (api_unavailable_until) wird nur gesetzt, wenn der
        Streams-Request selbst fehlschlägt - nicht, wenn schon der Token-Request
        scheitert (der hat eine eigene, unabhängige Fehlerbehandlung ohne
        Cooldown-Kopplung). Token-Request muss hier also erfolgreich sein.
        """
        cfg = {**TWITCH_CFG, "user_token": "", "webbrowser": False}
        call_log = []

        def urlopen_sequence(req, timeout=5):
            call_log.append(1)
            if len(call_log) == 1:
                return FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600})
            raise urllib.error.URLError("timeout")

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", urlopen_sequence)
        monkeypatch.setattr(
            twitch_api.subprocess, "run",
            lambda *a, **k: type("Result", (), {"returncode": 1})()
        )

        twitch_api.check_stream_online("https://twitch.tv/foo", cfg)

        assert twitch_api.api_unavailable_until > twitch_api.time.monotonic()


class TestGetStreamInfo:
    def test_returns_title_and_category_when_live(self, twitch_api, monkeypatch):
        responses = iter([
            FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600}),
            FakeHTTPResponse({"data": [{"type": "live", "title": "Cool Stream", "game_name": "Just Chatting"}]}),
        ])
        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", lambda req, timeout=5: next(responses))

        info = twitch_api.get_stream_info("https://twitch.tv/foo", TWITCH_CFG)

        assert info == {"online": True, "title": "Cool Stream", "category": "Just Chatting"}

    def test_returns_offline_dict_when_not_live(self, twitch_api, monkeypatch):
        responses = iter([
            FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600}),
            FakeHTTPResponse({"data": []}),
        ])
        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", lambda req, timeout=5: next(responses))

        info = twitch_api.get_stream_info("https://twitch.tv/foo", TWITCH_CFG)

        assert info == {"online": False, "title": "", "category": ""}

    def test_returns_none_without_credentials(self, twitch_api):
        cfg_no_creds = {"client_id": "", "client_secret": ""}
        assert twitch_api.get_stream_info("https://twitch.tv/foo", cfg_no_creds) is None

    def test_returns_none_for_non_twitch_url(self, twitch_api):
        assert twitch_api.get_stream_info("https://youtube.com/foo", TWITCH_CFG) is None

    def test_channel_is_url_encoded_in_query_string(self, twitch_api, monkeypatch):
        captured = {}
        responses = iter([
            FakeHTTPResponse({"access_token": "TOK", "expires_in": 3600}),
            FakeHTTPResponse({"data": []}),
        ])

        def urlopen_capture(req, timeout=5):
            captured["url"] = req.full_url
            return next(responses)

        monkeypatch.setattr(twitch_api.urllib.request, "urlopen", urlopen_capture)

        twitch_api.get_stream_info("https://twitch.tv/foo bar&baz", TWITCH_CFG)

        assert "user_login=foo%20bar%26baz" in captured["url"]
