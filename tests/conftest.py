"""
Gemeinsame Test-Infrastruktur für das tw_recorder-Package (src/tw_recorder/).

Seit dem Package-Split ist tw_recorder ein normaler, gültiger Python-
Paketname (kein Bindestrich mehr wie bei der alten tw-recorder.py) - daher
normale `import`-Statements statt des früheren importlib-Loader-Workarounds.

Eine Fixture pro Submodul, statt einer einzigen "tw_recorder"-Fixture: Tests
sollen genau das Modul referenzieren, das die getestete Funktion tatsächlich
enthält, statt über eine künstliche Kompatibilitätsfassade drüberzupatchen.

WICHTIG für monkeypatch.setattr(...)-Aufrufe: Config-Werte (z.B. CONFIG_FILE,
HEALTH_FILE) leben ausschließlich in config.py. Andere Module referenzieren
sie zur Laufzeit als `config.NAME` - Tests, die solche Werte patchen wollen,
müssen daher IMMER die config-Fixture verwenden, auch wenn die eigentlich
getestete Funktion in einem anderen Modul liegt (siehe test_healthcheck.py,
test_config.py::TestParseStreamers).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(scope="session")
def tw_recorder_pkg():
    """Das Top-Level-Package selbst (__title__, __version__)."""
    import tw_recorder
    return tw_recorder


@pytest.fixture(scope="session")
def config():
    from tw_recorder import config
    return config


@pytest.fixture(scope="session")
def logging_setup():
    from tw_recorder import logging_setup
    return logging_setup


@pytest.fixture(scope="session")
def healthcheck():
    from tw_recorder import healthcheck
    return healthcheck


@pytest.fixture(scope="session")
def twitch_api():
    from tw_recorder import twitch_api
    return twitch_api


@pytest.fixture(scope="session")
def recorder():
    from tw_recorder import recorder
    return recorder


@pytest.fixture(scope="session")
def daemon():
    from tw_recorder import daemon
    return daemon


@pytest.fixture(autouse=True)
def reset_twitch_globals(twitch_api):
    """
    Setzt die globalen Token-/API-Verfügbarkeits-Variablen (jetzt in
    twitch_api.py) vor JEDEM Test zurück. Ohne das würde ein gecachter Token
    oder eine gesetzte api_unavailable_until-Sperre aus einem vorherigen Test
    in den nächsten durchsickern, da das Modul nur einmal pro Session
    geladen wird (scope="session").
    """
    twitch_api.app_token = None
    twitch_api.token_expires_at = 0
    twitch_api.api_unavailable_until = 0.0
    yield
    twitch_api.app_token = None
    twitch_api.token_expires_at = 0
    twitch_api.api_unavailable_until = 0.0
