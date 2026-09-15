"""Twitch-Helix-API-Anbindung: App-Access-Token, Online-Check, Stream-Info."""

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from tw_recorder.config import get_extra_args

logger = logging.getLogger(__name__)

# Globale Variablen für Token-Caching
app_token = None
token_expires_at = 0
token_lock = threading.Lock()

# Verhindert wiederholte API-Anfragen und Warnungen bei einem vorübergehenden
# Ausfall des Twitch-Helix-Endpunkts.
api_unavailable_until = 0.0
api_warning_lock = threading.Lock()


def get_app_access_token(client_id: str, client_secret: str, force_refresh: bool = False) -> str | None:
    """Holt oder erneuert den Twitch OAuth2 App Access Token (Thread-sicher)."""
    global app_token, token_expires_at

    if not client_id or not client_secret:
        return None

    with token_lock:
        if app_token and time.time() < token_expires_at and not force_refresh:
            return app_token

        url = "https://id.twitch.tv/oauth2/token"
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials"
        }).encode("utf-8")

        req = urllib.request.Request(
            url, 
            data=params, 
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                app_token = data.get("access_token")
                token_expires_at = time.time() + data.get("expires_in", 3600) - 60
                return app_token
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Fehler beim Twitch-Token-Abruf: {e}")
            app_token = None
            return None


def check_stream_online(url: str, cfg: dict, retry: bool = True) -> bool:
    """Prüft via Twitch Helix API, ob der Kanal live ist."""
    global api_unavailable_until

    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    if "twitch.tv" in url and client_id and client_secret:
        channel = url.rstrip("/").split("/")[-1].lower()
        with api_warning_lock:
            api_available = time.monotonic() >= api_unavailable_until

        token = get_app_access_token(client_id, client_secret) if api_available else None

        if token:
            api_url = f"https://api.twitch.tv/helix/streams?user_login={channel}"
            headers = {
                "Client-ID": client_id,
                "Authorization": f"Bearer {token}"
            }
            req = urllib.request.Request(api_url, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    streams = data.get("data", [])
                    return len(streams) > 0 and streams[0].get("type") == "live"
            except urllib.error.HTTPError as e:
                if e.code == 401 and retry:
                    get_app_access_token(client_id, client_secret, force_refresh=True)
                    return check_stream_online(url, cfg, retry=False)
                logger.warning(f"Twitch API HTTP-Fehler {e.code} für {channel}")
            except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
                with api_warning_lock:
                    api_unavailable_until = time.monotonic() + 60
                    logger.warning(
                        f"Twitch-API vorübergehend nicht erreichbar ({e}). "
                        "Verwende Streamlink-Fallback für 60 Sekunden."
                    )

    # Fallback für Plattformen außer Twitch oder fehlende Keys
    cmd = ["streamlink", "--stream-url"]
    cmd.extend(get_extra_args(cfg))
    cmd.extend([url, "best"])

    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return proc.returncode == 0


def get_stream_info(url: str, cfg: dict, retry: bool = True) -> dict | None:
    """
    Fragt via Twitch Helix API Titel/Kategorie des aktuell laufenden Streams ab.
    Wird für die Split-Erkennung bei Titel-/Kategorieänderung genutzt.

    Gibt bei Erfolg {"online": bool, "title": str, "category": str} zurück.
    Gibt None zurück, wenn keine Twitch-API-Zugangsdaten vorhanden sind, die
    Plattform kein Twitch ist, oder die API gerade nicht erreichbar ist.
    Verursacht nur 1 zusätzlichen Helix-API-Aufruf pro Aufruf (kostet 1 Punkt
    des 800-Punkte/Minute-Limits) - bei üblichen Prüfintervallen (>= 60s)
    vernachlässigbar.
    """
    global api_unavailable_until

    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    if not ("twitch.tv" in url and client_id and client_secret):
        return None

    channel = url.rstrip("/").split("/")[-1].lower()
    with api_warning_lock:
        api_available = time.monotonic() >= api_unavailable_until

    token = get_app_access_token(client_id, client_secret) if api_available else None
    if not token:
        return None

    api_url = f"https://api.twitch.tv/helix/streams?user_login={channel}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}"
    }
    req = urllib.request.Request(api_url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            streams = data.get("data", [])
            if streams and streams[0].get("type") == "live":
                return {
                    "online": True,
                    "title": streams[0].get("title") or "",
                    "category": streams[0].get("game_name") or "",
                }
            return {"online": False, "title": "", "category": ""}
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            get_app_access_token(client_id, client_secret, force_refresh=True)
            return get_stream_info(url, cfg, retry=False)
        logger.warning(f"Twitch API HTTP-Fehler {e.code} für {channel} (Titel-Check)")
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
        with api_warning_lock:
            api_unavailable_until = time.monotonic() + 60
            logger.warning(
                f"Twitch-API vorübergehend nicht erreichbar ({e}). "
                "Titel-Split-Erkennung pausiert für 60 Sekunden."
            )
        return None
