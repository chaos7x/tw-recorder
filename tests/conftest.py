"""
Gemeinsame Test-Infrastruktur für tw-recorder.py.

tw-recorder.py hat einen Bindestrich im Dateinamen und ist damit kein gültiger
Python-Modulname. Wir laden die Datei deshalb dynamisch über importlib - das
funktioniert unabhängig vom Dateinamen und braucht keine Umbenennung des
Produktionscodes.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "tw-recorder.py"


def _load_tw_recorder_module():
    spec = importlib.util.spec_from_file_location("tw_recorder", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tw_recorder"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def tw_recorder():
    """Lädt tw-recorder.py einmal pro Test-Session und stellt es als Modul-Objekt bereit."""
    return _load_tw_recorder_module()


@pytest.fixture(autouse=True)
def reset_twitch_globals(tw_recorder):
    """
    Setzt die globalen Token-/API-Verfügbarkeits-Variablen vor JEDEM Test zurück.
    Ohne das würde ein gecachter Token oder eine gesetzte api_unavailable_until-
    Sperre aus einem vorherigen Test in den nächsten durchsickern, da das Modul
    nur einmal pro Session geladen wird (scope="session").
    """
    tw_recorder.app_token = None
    tw_recorder.token_expires_at = 0
    tw_recorder.api_unavailable_until = 0.0
    yield
    tw_recorder.app_token = None
    tw_recorder.token_expires_at = 0
    tw_recorder.api_unavailable_until = 0.0
