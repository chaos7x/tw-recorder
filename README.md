# tw-recorder 📹🔴

`tw-recorder` ist eine leichtgewichtige, Python-basierte Docker-Lösung zur automatischen Überwachung und Aufzeichnung von Livestreams (Twitch) via Streamlink und FFmpeg.

Das Tool prüft regelmäßig konfigurierte Kanäle, zeichnet Live-Streams in Echtzeit auf und remuxt das finale Video automatisch in ein sauber getaggtes MKV-Format mit dynamischer Titelkürzung für Folge-Prozesse (z. B. YouTube Uploads).

---

## ✨ Features

* **Automatische Stream-Erkennung:** Prüft effizient über die Twitch Helix API (oder Streamlink Fallback), ob definierte Kanäle live sind.
* **Automatisches Remuxing:** Konvertiert aufgezeichnete `.ts`-Dateien direkt nach der Übertragung via FFmpeg verlustfrei nach `.mkv`.
* **Metadaten & API-Optimierung:** Schreibt Titel, Kategorie, Artist (Kanal) und Aufnahmedatum direkt in die MKV-Metadaten und kürzt Titel dynamisch auf maximal 95 Zeichen (optimiert für YouTube API Beschränkungen).
* **Titel-/Kategorie-Split:** Erkennt Änderungen am Stream-Titel oder der Kategorie während einer laufenden Aufnahme und beendet/startet die Aufzeichnung automatisch neu (optional, `split_on_title_change`).
* **Hot-Reloading der Konfiguration:** Überwacht die Konfigurationsdatei (`recorder.conf`) und übernimmt Änderungen automatisch im laufenden Betrieb ohne Neustart.
* **Kollisions- & Mehrfachstart-Schutz:** Verhindert mittels File-Locking (`.record.lock`), dass ein Kanal mehrfach parallel aufgenommen wird.
* **Schlankes Docker-Image:** Basiert auf Debian Trixie Slim mit statisch kompiliertem FFmpeg/FFprobe (`mwader/static-ffmpeg`).

---

## 🚀 Schnellstart

### 1. Konfiguration

Die Hauptkonfiguration erfolgt über die `recorder.conf` im Verzeichnis `/etc/tw-recorder/`.

Zusätzlich unterstützt die Anwendung modularisierte Konfigurationsdateien: Alle `.conf`-Dateien im Ordner `/etc/tw-recorder/conf.d/` werden automatisch eingelesen und kombiniert. Änderungen an den Konfigurationsdateien werden zur Laufzeit per Hash-Prüfung erkannt und automatisch neu geladen.

#### Beispiel `recorder.conf`

```ini
[general]
# Zielverzeichnis für die Aufnahmen im Container
storage_dir = /storage

# Prüf-Intervall in Sekunden zwischen den Abfragen
sleep_interval = 15

# Dateinamensmuster für die finalen Aufnahmen (.mkv wird automatisch ergänzt)
# Verfügbare Variablen: {time}, {channel}, {author}, {category}, {title}
filename_pattern = {time:%Y-%m-%d_%H-%M}_{channel}_{title}

[twitch]
# Twitch Helix API Credentials (empfohlen für stabile Abfragen)
# client_id = your_client_id
# client_secret = your_client_secret
# user_token = your_user_token

[streamlink]
# Standard-Qualität für die Aufzeichnungen (z. B. best, 1080p60, 720p, worst)
stream_quality = best

# Log-Level für Streamlink (debug, info, warning, error)
loglevel = info

# Wartezeit/Retry bei Verbindungsfehlern in Sekunden
retry = 30

# Webbrowser-Headless-Modus zur Umgehung von Captchas/Protection
webbrowser = false

# Aufnahme bei Titel-/Kategorieänderung automatisch splitten (nur mit Twitch-API-Zugangsdaten möglich)
split_on_title_change = false

# Prüfintervall in Sekunden für die Titel-/Kategorie-Split-Erkennung
title_check_interval = 90

[channels]
# Format: url_oder_channel = qualität
# Beispiele:
https://twitch.tv/example = best
example = 1080p60
twitch.tv/anotherchannel = best
```

### 2. Starten via Docker CLI
Starte den Container direkt über die Docker CLI:
```bash
docker run -d \
  --name tw-recorder \
  --restart unless-stopped \
  -e TZ=Europe/Berlin \
  -e STREAMLINK_LOGLEVEL=warning \
  -v $(pwd)/config:/etc/tw-recorder:ro \
  -v $(pwd)/videos:/storage:rw \
  ghcr.io/chaos7x/tw-recorder:latest
```

### 3. 📦 Docker Compose Integration
```yaml
services:
  tw-recorder:
    image: ghcr.io/chaos7x/tw-recorder:${IMAGE_VERSION:-latest}
    container_name: tw-recorder
    hostname: tw-recorder
    user: "11107:11108"
    restart: unless-stopped
    environment:
      - TZ=Europe/Berlin
      - STREAMLINK_LOGLEVEL=warning
      - HOME=/tmp
    env_file:
      - .env
    volumes:
      - ./config:/etc/tw-recorder:ro
      - ./videos:/storage:rw
```

---

## 🛠️ Bare-Metal-Installation (ohne Docker)

`tw-recorder` läuft auch direkt auf dem Host. Anders als bei vielen anderen Python-Tools kommt hier **keine** Python-Abhängigkeit über `apt`/`apk`, weil `tw-recorder.py` ausschließlich die Standardbibliothek nutzt (`urllib` statt `requests`) - `pip` installiert nur das eigene Package:

```bash
apt install python3-pip python3-setuptools ffmpeg
pip install --break-system-packages --no-deps .

# streamlink separat installieren (eigenständiges CLI-Tool, kein Python-Import
# von tw-recorder - braucht echte PyPI-Dependency-Auflösung, daher OHNE --no-deps)
pip install --break-system-packages streamlink
```

Danach steht `tw-recorder --version` systemweit zur Verfügung.

### Alternative: Fertiges Debian-Paket (.deb)

Jedes [GitHub Release](https://github.com/chaos7x/tw-recorder/releases) enthält zusätzlich ein `tw-recorder_<version>_all.deb` als Anhang - keine manuelle `pip`-Installation nötig, `apt`/`dpkg` löst die Abhängigkeiten (`streamlink`, `ffmpeg`) automatisch mit auf:

```bash
wget https://github.com/chaos7x/tw-recorder/releases/latest/download/tw-recorder_<version>_all.deb
apt install ./tw-recorder_<version>_all.deb
```

---

## 🐳 Docker-Image-Varianten

Drei Dockerfiles für unterschiedliche Basis-Images - alle bauen dasselbe `tw_recorder`-Package ein, alle bewusst als Single-Stage-Build: `pip`/`setuptools` im Image sind inert (keine laufenden Dienste, keine exponierte Angriffsfläche), und `streamlink` zieht ohnehin schon einen eigenen, nicht kleinen Dependency-Baum mit - der Größenvorteil einer separaten Builder-Stage wäre hier Rauschen, die zusätzliche Komplexität dagegen eine reale Fehlerquelle.

| Dockerfile | Basis | Installationsweg |
|---|---|---|
| `Dockerfile` (Standard) | `debian:trixie-slim` | `apt` für System-Tools, `pip install streamlink` + `pip install --no-deps .` direkt im Image |
| `Dockerfile.alpine` | `alpine:3` | Wie oben, aber `apk` statt `apt` |
| `Dockerfile.pyimg` | `python:3-slim` | Identisches Prinzip, `pip` ist hier ohnehin schon Teil der Basis-Image-Identität |

---

## ⚙️ Umgebungsvariablen (Environment)

* `CLIENT_ID` / `TWITCH_CLIENT_ID`: (optional) Client-ID für die Twitch Helix API Abfrage.
* `CLIENT_SECRET` / `TWITCH_CLIENT_SECRET`: (optional) Client-Secret für Twitch App Access Token.
* `TWITCH_USER_TOKEN`: (optional) OAuth Token zur Umgehung von Streamlink-Limits / Ads.
* `SLEEP_INTERVAL`: Standard `15`. Intervall in Sekunden zwischen den Statusprüfungen.
* `CONFIG_FILE`: Standard `/etc/tw-recorder/recorder.conf`. Pfad zur Konfigurationsdatei im Container.
* `STORAGE_DIR`: Standard `/storage`. Zielpfad für die gespeicherten Videoaufnahmen.
* `DEBUG`: Standard `0`. Auf `true`/`1` setzen für erweiterte Log-Ausgaben.

---

## 🏷️ Versionierung

Reguläre Releases folgen `vX.Y.Z` (SemVer) und entstehen manuell zusammen mit einer echten Code-Änderung.

Zusätzlich prüft ein monatlicher Workflow (`os-patch-release.yml`, 1. jeden Monats), ob das Debian-/Alpine-Basis-Image ungenutzte Security-Patches hat, die `docker-refresh.yml`'s wöchentliches `latest`-Update zwar schon mitnimmt, die aber an den fixen `vX.Y.Z`-Tags vorbeilaufen (die frieren für immer auf ihrem Build-Zeitpunkt ein). Findet der Workflow etwas, hängt er eine **vierte Versionsstelle** an, die ausschließlich für solche reinen OS-Patch-Releases reserviert ist: `v1.2.5` → `v1.2.5.1` → `v1.2.5.2` (jeweils ohne Code-Änderung, nur aktualisierte System-Pakete). Bleibt die vierte Stelle bei Nichts-zu-patchen-Läufen einfach aus, gibt es auch keinen neuen Tag - kein Rauschen in der Release-Historie.

Der nächste echte Code-Release setzt diese vierte Stelle **nicht fort**, sondern lässt sie weg: auf `v1.2.5.2` folgt bei einer echten Änderung `v1.2.6`, nicht `v1.2.6.0` oder `v1.2.5.3`.

---

## 🧑‍💻 Lokaler Build & Entwicklung

Ein neues Docker-Image kann über das mitgelieferte Shell-Skript gebaut werden:

```bash
./build.sh                    # Nutzt Name & Version aus pyproject.toml, Standard-Dockerfile
./build.sh 2.0.0               # Explizite Version, Standard-Dockerfile
./build.sh alpine               # Version aus pyproject.toml, Dockerfile.alpine
./build.sh 2.0.0 pyimg          # Explizite Version, Dockerfile.pyimg
```

Name und Standard-Version werden automatisch aus `pyproject.toml` gelesen - ein explizit übergebenes Versions-Argument überschreibt das.

### Tests & Linting
```bash
pip install -r requirements-test.txt
pytest -v

ruff check src/tw_recorder/
```

---

## 📄 Lizenz

Dieses Projekt steht unter der **GNU General Public License v3.0 (GPLv3)**.
