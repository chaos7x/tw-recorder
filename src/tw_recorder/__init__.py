"""tw-recorder: Multi-Channel Twitch-Stream-Recorder (Streamlink + FFmpeg)."""

from importlib.metadata import PackageNotFoundError, version

__title__ = "Streamlink Recorder CLI"

try:
    # Funktioniert bei jeder Installationsart (echtes `pip install .` oder
    # `pip install --target=...`) - liest die Version aus den von pip
    # erzeugten Metadaten, statt sie hier ein zweites Mal zu pflegen.
    __version__ = version("tw-recorder")
except PackageNotFoundError:
    # Nur relevant, wenn die .py-Dateien ganz ohne pip-Installation direkt
    # kopiert/ausgeführt werden - keiner der gepflegten Installationswege
    # braucht diesen Zweig. Kein hartkodierter Versionsstring, damit er nie
    # veralten kann.
    __version__ = "local-inst"
