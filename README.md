# tw-recorder 📹🔴

`tw-recorder` ist eine leichtgewichtige, Python-basierte Docker-Lösung zur automatischen Überwachung und Aufzeichnung von Livestreams (Twitch) via Streamlink und FFmpeg[cite: 11, 14].

Das Tool prüft regelmäßig konfigurierte Kanäle, zeichnet Live-Streams in Echtzeit auf und remuxt das finale Video automatisch in ein sauber getaggtes MKV-Format mit dynamischer Titelkürzung für Folge-Prozesse (z. B. YouTube Uploads)[cite: 14].

---

## 🚀 Schnellstart

### 1. Vorbereitung (Ordner & Konfiguration)
## Konfiguration

Die Hauptkonfiguration erfolgt über die `recorder.conf` im Verzeichnis `/etc/tw-recorder/`. 

Zusätzlich unterstützt die Anwendung modularisierte Konfigurationsdateien: Alle `.conf`-Dateien im Ordner `/etc/tw-recorder/conf.d/` werden automatisch eingelesen und kombiniert. Änderungen an den Konfigurationsdateien werden zur Laufzeit per Hash-Prüfung erkannt und automatisch neu geladen.

### Beispiel `recorder.conf`

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

[channels]
# Format: url_oder_channel = qualität
# Beispiele:
[https://twitch.tv/example](https://twitch.tv/example) = best
example = 1080p60
twitch.tv/anotherchannel = best

```

### 2. Starten via Docker CLI
Starte den Container direkt über die Docker CLI[cite: 10]:
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
---

## 📦 Docker Compose Integration

Alternativ kannst du den Service ganz einfach in deine `docker-compose.yml` einbinden[cite: 10]:

```yaml
services:
  Twitch-recorder:
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


## ✨ Features

* **Automatische Stream-Erkennung:** Prüft effizient über die Twitch Helix API (oder Streamlink Fallback), ob definierte Kanäle live sind[cite: 14].
* **Automatisches Remuxing:** Konvertiert aufgezeichnete `.ts`-Dateien direkt nach der Übertragung via FFmpeg verlustfrei nach `.mkv`[cite: 14].
* **Metadaten & API-Optimierung:** Schreibt Titel, Kategorie, Artist (Kanal) und Aufnahmedatum direkt in die MKV-Metadaten und kürzt Titel dynamisch auf maximal 95 Zeichen (optimiert für YouTube API Beschränkungen)[cite: 14].
* **Hot-Reloading der Konfiguration:** Überwacht die Konfigurationsdatei (`recorder.conf`) und übernimmt Änderungen automatisch im laufenden Betrieb ohne Neustart[cite: 14].
* **Kollisions- & Mehrfachstart-Schutz:** Verhindert mittels File-Locking (`.record.lock`), dass ein Kanal mehrfach parallel aufgenommen wird[cite: 14].
* **Schlankes Docker-Image:** Basiert auf Debian Trixie Slim mit statisch kompiliertem FFmpeg/FFprobe (`mwader/static-ffmpeg`)[cite: 11].

---

## ⚙️ Umgebungsvariablen (Environment)

* `CLIENT_ID` / `TWITCH_CLIENT_ID`: (optional) Client-ID für die Twitch Helix API Abfrage[cite: 14].
* `CLIENT_SECRET` / `TWITCH_CLIENT_SECRET`: (optional) Client-Secret für Twitch App Access Token[cite: 14].
* `TWITCH_USER_TOKEN`: (optional) OAuth Token zur Umgehung von Streamlink-Limits / Ads[cite: 14].
* `SLEEP_INTERVAL`: Standard `15`. Intervall in Sekunden zwischen den Statusprüfungen[cite: 14].
* `CONFIG_FILE`: Standard `/etc/tw-recorder/recorder.conf`. Pfad zur Konfigurationsdatei im Container[cite: 14].
* `STORAGE_DIR`: Standard `/storage`. Zielpfad für die gespeicherten Videoaufnahmen[cite: 11, 14].

---

## 🛠️ Lokaler Build & Entwicklung

Ein lokales Image kann einfach über das Build-Skript kompiliert werden[cite: 9]:
```bash
./build.sh v1.0.0
```
Falls du Änderungen am Python-Skript testen möchtest, kannst du das `app`-Volume mounten[cite: 10, 12]. Das `entrypoint.sh` führt bevorzugt das externe Skript aus `/app/tw-recorder` aus, wenn vorhanden[cite: 12].

---

## 📄 Lizenz

Dieses Projekt steht unter der **GNU General Public License v3.0 (GPLv3)**[cite: 13, 14].
