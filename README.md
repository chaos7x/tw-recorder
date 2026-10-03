# tw-recorder 📹🔴

`tw-recorder` ist eine leichtgewichtige, Python-basierte Docker-Lösung zur automatischen Überwachung und Aufzeichnung von Livestreams (Twitch) via Streamlink und FFmpeg.

Das Tool prüft regelmäßig konfigurierte Kanäle, zeichnet Live-Streams in Echtzeit auf und remuxt das finale Video automatisch in ein sauber getaggtes MKV-Format mit dynamischer Titelkürzung für Folge-Prozesse (z. B. YouTube Uploads).

Eine Übersicht der internen Architektur (Module, Datenfluss, Diagramm) findet sich in [ARCHITECTURE.md](ARCHITECTURE.md).

> **⚠️ Streamlink-Version:** Für Twitch wird mindestens **Streamlink >= 8.2.0** benötigt - ältere Versionen scheitern an Twitch-seitigen API-Änderungen und liefern keine Streams mehr. Das mitgelieferte Docker-Image zieht bei jedem Build automatisch die aktuelle PyPI-Version, betrifft also nur eine manuelle Bare-Metal-Installation mit bereits vorhandenem, veraltetem Streamlink.

---

## ✨ Features

* **Automatische Stream-Erkennung:** Prüft effizient über die Twitch Helix API (oder Streamlink Fallback), ob definierte Kanäle live sind.
* **Automatisches Remuxing:** Konvertiert aufgezeichnete `.ts`-Dateien direkt nach der Übertragung via FFmpeg verlustfrei nach `.mkv`.
* **Metadaten & API-Optimierung:** Schreibt Titel, Kategorie, Artist (Kanal), Aufnahmedatum und die sekundengenaue Startzeit der Aufnahme (`creation_time`, in der MKV als `DateUTC`, korrekt nach UTC umgerechnet aus der lokalen `TZ`; zusätzlich als eigenes Tag `RECORDING_START`, das `yt-upload` als Aufnahmedatum und Beschreibungszeile übernimmt) sowie einen Start-Timecode (`TIMECODE`, lokale Zeit `HH:MM:SS:00`, der bei `ffmpeg -i aufnahme.mkv -c copy aufnahme.mov` oder mit `.mov` als Endung in `filename_pattern` direkt zur Timecode-Spur wird) direkt in die MKV-Metadaten und kürzt Titel dynamisch auf maximal 95 Zeichen (optimiert für YouTube API Beschränkungen). Zusätzlich werden dieselben Extended Attributes (`user.dublincore.*`, `user.xdg.referrer.url`) gesetzt, die auch `yt-dlp --xattrs` schreibt - Dateimanager & Tools behandeln Aufnahmen und yt-dlp-Downloads damit einheitlich (nur auf Dateisystemen mit xattr-Unterstützung, z.B. ext4/xfs/btrfs; auf FAT/exFAT o.ä. wird das übersprungen, kein Fehler).
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
storage_dir = /srv/media-pipeline/recordings

# Prüf-Intervall in Sekunden zwischen den Abfragen
sleep_interval = 15

# Dateinamensmuster für die finalen Aufnahmen (.mkv wird automatisch ergänzt)
# Verfügbare Variablen: {time}, {channel}, {author}, {category}, {title}
# Die Endung bestimmt den Container: .mkv (Standard) oder .mov/.mp4 - bei .mov
# wird der Start-Timecode direkt zur Timecode-Spur (tmcd), alle Tags inkl.
# RECORDING_START für yt-upload bleiben erhalten.
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
  -v $(pwd)/recordings:/srv/media-pipeline/recordings:rw \
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
      # Ganzer gemeinsamer /srv/media-pipeline-Baum wie bei yt-upload und
      # fetchbridge; tw-recorder schreibt davon nur nach recordings/
      # (STORAGE_DIR-Default).
      - /srv/media-pipeline:/srv/media-pipeline:rw
```

#### 🔗 Interop mit Bare-Metal/anderen Containern (gemeinsamer Host-Pfad)

`user: "11107:11108"` oben ist nur ein Platzhalter. Der Mount oben nutzt bereits denselben Host-Pfad `/srv/media-pipeline`, den auch die Bare-Metal-/`.deb`-Installationen von `fetchbridge`/`yt-upload` nutzen - Container und Bare-Metal teilen sich den Baum also ohne weitere Anpassung.

`/srv/media-pipeline` und seine Unterordner gehören dort `root:media-pipeline` mit Modus `2775` (setgid, bewusst **ohne** Sticky-Bit) - Schreib-/Löschrecht hängt also rein an der **Gruppe**, nicht an der UID oder dem Datei-Owner. Die GID im `user:`-Feld muss deshalb mit der echten Host-Gruppe übereinstimmen, sonst gibt's `Permission denied`:

```bash
getent group media-pipeline   # z.B. media-pipeline:x:998:
```

Die zweite Zahl in `user: "<uid>:<gid>"` durch diese echte GID ersetzen (z.B. `user: "11107:998"`) - die UID (erste Zahl) ist frei wählbar, da sie für die Zugriffsrechte auf dieses Verzeichnis keine Rolle spielt.

---

## 🛠️ Bare-Metal-Installation (ohne Docker)

`tw-recorder` läuft auch direkt auf dem Host. Anders als bei vielen anderen Python-Tools kommt hier **keine** Python-Abhängigkeit über `apt`/`apk`, weil `tw-recorder.py` ausschließlich die Standardbibliothek nutzt (`urllib` statt `requests`) - `pip` installiert nur das eigene Package:

```bash
apt install python3-pip python3-setuptools ffmpeg
pip install --break-system-packages --no-deps .

# streamlink separat installieren (eigenständiges CLI-Tool, kein Python-Import
# von tw-recorder - braucht echte PyPI-Dependency-Auflösung, daher OHNE --no-deps).
# Version >= 8.2.0 zwingend, sonst funktioniert Twitch nicht (siehe Hinweis oben) -
# apt/apk-Pakete sind dafür i.d.R. zu alt, deshalb bewusst über pip statt System-Paket.
pip install --break-system-packages "streamlink>=8.2.0"
```

Danach steht `tw-recorder --version` systemweit zur Verfügung.

Die Logdatei landet je nach Umgebung automatisch am sinnvollsten Ort (`/var/log/tw-recorder/`, sofern beschreibbar und ein klassischer Syslog-Daemon läuft, sonst nur auf `stdout`/journald) - siehe `LOG_FILE`-Umgebungsvariable, falls ein fester Pfad gewünscht ist. Läuft ein Syslog-Daemon, rotiert die App die Datei bewusst **nicht** selbst (kein `RotatingFileHandler`) - das übernimmt das mitgelieferte `/etc/logrotate.d/tw-recorder` (nur im `.deb`-Paket enthalten; bei einer reinen `pip`-Installation ohne `.deb` selbst einrichten, falls gewünscht). Nur bei explizit gesetztem `LOG_FILE` oder einem gemounteten Docker-`/log`-Volume rotiert die App eigenständig, da dort sonst niemand rotieren würde.

### Alternative: Fertiges Debian-Paket (.deb)

Jedes [GitHub Release](https://github.com/chaos7x/tw-recorder/releases) enthält zusätzlich ein `tw-recorder_<version>_all.deb` als Anhang - keine manuelle `pip`-Installation nötig, `apt`/`dpkg` löst die Abhängigkeiten (`streamlink`, `ffmpeg`) automatisch mit auf. **Achtung:** Das `streamlink`-Paket aus den Debian/Ubuntu-Repos ist oft älter als die oben geforderte Version 8.2.0 - im Zweifel danach `pip install --break-system-packages --upgrade "streamlink>=8.2.0"` ausführen, um die apt-Version zu überschreiben:

```bash
wget https://github.com/chaos7x/tw-recorder/releases/latest/download/tw-recorder_<version>_all.deb
apt install -t trixie-backports ./tw-recorder_<version>_all.deb
```

Das Paket legt einen dedizierten Systemuser (`tw-recorder`) an und startet den Dienst bewusst nicht automatisch - erst `/etc/tw-recorder/recorder.conf` (bzw. `conf.d/`) anpassen, dann:

```bash
systemctl enable --now tw-recorder
```

**Devuan / Debian ohne systemd (`sysvinit-core`, OpenRC):** Das Paket bringt zusätzlich ein klassisches `/etc/init.d/tw-recorder`-Skript mit, das `postinst` automatisch anstelle des systemd-Service registriert, wenn kein systemd läuft - ebenfalls deaktiviert (`update-rc.d … defaults-disabled`, kein Start beim Booten). Die mitgelieferte `.service`-Datei unter `/usr/lib/systemd/system/` bleibt dort einfach ungenutzt liegen, wie bei Debian-Paketen üblich. Aktivieren und starten:

```bash
update-rc.d tw-recorder enable
service tw-recorder start
```

### 🔒 Optional: Twitch-Credentials mit systemd-creds verschlüsseln (nur Dämon/.deb-Paket)

`client_secret`/`user_token` landen sonst im Klartext in `recorder.conf`/`conf.d/*.conf`. Das .deb- bzw. FreeBSD-Paket legt `recorder.conf` deshalb als `root:tw-recorder` mit `0640` an (nur root und der Dienst können sie lesen, der Dienst braucht kein Schreibrecht), das Verzeichnis `conf.d/` als `root:tw-recorder` mit `0750`; eigene `conf.d/*.conf` mit Secrets sollten ebenfalls `0640` bekommen. `check_secrets_permissions()` warnt zwar, wenn eine solche Datei für Andere oder eine fremde Gruppe lesbar ist, verhindert aber nicht, dass die Secrets überhaupt im Klartext auf der Platte liegen. Mit `LoadCredentialEncrypted=` (systemd >= 250) lässt sich das vermeiden.

**Ohne systemd (Devuan, sysvinit/OpenRC)** gibt es kein `systemd-creds` und damit auch keine TPM-gebundene Verschlüsselung. Dort bleiben die Secrets in `recorder.conf` bzw. `conf.d/*.conf`, geschützt durch die oben genannten Rechte (`0640`/`0750`) - für andere lokale User nicht lesbar, für root aber im Klartext.

Analog zu yt-uploads `CREDENTIALS_FILE`: `client_id`/`client_secret`/`user_token` lassen sich optional aus einer KEY=VALUE-Datei nachladen, deren Pfad in `TWITCH_CREDENTIALS_FILE` steht (`config._load_twitch_credentials_file()`) - ein einzelner, frei konfigurierbarer Pfad statt einer `$CREDENTIALS_DIRECTORY`-spezifischen Sonderlogik. `tw-recorder` weiß dabei nichts von systemd-Credentials, es liest einfach "die Datei, deren Pfad in `TWITCH_CREDENTIALS_FILE` steht".

**Wichtig:** Um diesen Pfad bei systemd-creds auf die entschlüsselte Kopie zeigen zu lassen, `Environment=TWITCH_CREDENTIALS_FILE=%d/twitch-secrets` verwenden (ein einzelner Wert mit `%d`-Specifier) - **nicht** `EnvironmentFile=%d/twitch-secrets` (eine ganze, bulk-eingelesene Datei). Letzteres funktioniert nicht zuverlässig: der `%d`-Specifier wird darin nicht wie erwartet aufgelöst und führt zu `Failed to load environment files`/einer Restart-Crashloop des gesamten Diensts (real reproduziert). `Environment=` mit einem einzelnen Wert ist dagegen zuverlässig - genau das Muster, das yt-uploads `CREDENTIALS_FILE` bereits nutzt:

```bash
# 1. Klartext-Secrets als KEY=VALUE-Datei anlegen (einmalig, als root) -
#    CLIENT_ID ist bei Twitch kein echtes Geheimnis (vergleichbar mit einer
#    öffentlichen OAuth-Client-ID), lässt sich aber genauso gut mit ins
#    verschlüsselte Bundle packen
cat > /etc/tw-recorder/twitch-secrets << 'EOF'
CLIENT_ID="deine_client_id"
CLIENT_SECRET="dein_client_secret"
TWITCH_USER_TOKEN="dein_user_token"
EOF
chmod 600 /etc/tw-recorder/twitch-secrets

# 2. Verschlüsseln (Standard ist --with-key=auto: bindet automatisch an
#    einen vorhandenen TPM2-Chip zusätzlich zum maschinen-eigenen Schlüssel
#    unter /var/lib/systemd/credential.secret - ganz ohne manuelle Angabe.
#    --with-key=tpm2 erzwingt AUSSCHLIESSLICH TPM2, ohne den Host-Schlüssel).
#    --name MUSS exakt dem
#    Namen vor dem Doppelpunkt in LoadCredentialEncrypted= unten entsprechen
#    - sonst schlägt die Entschlüsselung beim Dienststart fehl.
systemd-creds encrypt \
  --name=twitch-secrets \
  /etc/tw-recorder/twitch-secrets \
  /etc/tw-recorder/twitch-secrets.cred

# 3. Override-Datei anlegen statt die Unit direkt zu editieren
systemctl edit tw-recorder
```

Im Editor, der sich dabei öffnet, folgendes Override-Snippet einfügen - `Environment=`, **kein** `EnvironmentFile=`:

```ini
[Service]
LoadCredentialEncrypted=twitch-secrets:/etc/tw-recorder/twitch-secrets.cred
Environment=TWITCH_CREDENTIALS_FILE=%d/twitch-secrets
```

```bash
# 4. client_secret/user_token aus recorder.conf/conf.d entfernen bzw.
#    auskommentieren, sonst gewinnt der Datei-Wert weiterhin gegen den aus
#    TWITCH_CREDENTIALS_FILE gelesenen Wert: config.get(..., fallback=...)
#    greift nur, wenn der Key in der Datei komplett fehlt.

# 5. Erst NACH erfolgreichem Test (systemctl restart tw-recorder, Logs
#    prüfen) das Klartext-Original entfernen
systemctl restart tw-recorder
shred -u /etc/tw-recorder/twitch-secrets
```

Die `.cred`-Datei ist Base64-kodierter Text (kein rohes Binärformat, `cat` ist also unproblematisch), ohne den Maschinen-Schlüssel unter `/var/lib/systemd/credential.secret` (root-only) aber nicht entschlüsselbar.

`TWITCH_CREDENTIALS_FILE` selbst landet zwar als echte Prozess-Umgebungsvariable (über `/proc/<pid>/environ` einsehbar) - ihr Wert ist aber nur ein **Dateipfad** (z.B. `/run/credentials/tw-recorder.service/twitch-secrets`), kein Secret. Das eigentliche `client_secret`/`user_token` liest `tw-recorder` direkt aus der Datei, es landet nie selbst in einer Umgebungsvariable - genau das Zugriffsmuster, das systemd selbst für Anwendungscode empfiehlt (siehe [systemd/systemd docs/CREDENTIALS.md](https://github.com/systemd/systemd/blob/main/docs/CREDENTIALS.md)). Nach Schritt 4 entfällt außerdem die Warnung aus `check_secrets_permissions()` von selbst, da `recorder.conf` dann kein Secret mehr enthält.

`TWITCH_CREDENTIALS_FILE` funktioniert dabei unabhängig von systemd-creds - der Pfad kann genauso auf eine gewöhnliche Datei irgendwo auf der Platte zeigen, falls eine einzelne KEY=VALUE-Datei statt der `[twitch]`-Sektion in `recorder.conf` bevorzugt wird.

### Alternative: FreeBSD-Paket (.pkg)

Jedes Release enthält neben den `.deb`-Dateien auch FreeBSD-Pakete, je eines für FreeBSD 14 und 15 (`…-freebsd14.pkg` / `…-freebsd15.pkg`, gebaut von `freebsd/build-pkg.py`). Sie hängen fest an Python 3.11 (`python311`), damit Interpreter und Python-Abhängigkeiten zusammenpassen:

```sh
pkg install python311 ffmpeg
pkg add tw-recorder-<version>-freebsd14.pkg
```

Das Paket legt `/etc/tw-recorder/recorder.conf` aus der Vorlage an (falls noch keine existiert), dazu den Dienstuser, die Gruppe `media-pipeline` und die Verzeichnisse unter `/srv/media-pipeline`, genau wie das Debian-Paket. streamlink ≥ 8.2.0 kommt weiterhin separat per `pip install "streamlink>=8.2.0"`. Der Dienst wird dabei bewusst **nicht** aktiviert, die nötigen `sysrc`/`service`-Befehle zeigt `pkg` nach der Installation an. Noch nicht auf einem echten FreeBSD-System getestet.

### Alternative: FreeBSD manuell (rc.d)

Für FreeBSD (ab 14.5, wie bei yt-upload und fetchbridge) liegt unter `freebsd/rc.d/tw-recorder` ein rc.d-Skript bei, das Pendant zum systemd-Service aus `debian/`. Ohne das fertige Paket geht die Einrichtung auch von Hand und aktiviert den Dienst ebenfalls nicht von selbst. Bisher nur gegen die Doku geschrieben, noch nicht auf einem echten FreeBSD-System getestet.

```sh
# Abhängigkeiten (py311 an die installierte Python-Version anpassen)
pkg install python3 py311-pip ffmpeg
cd /pfad/zu/tw-recorder
pip install --no-deps .
pip install "streamlink>=8.2.0"

# Dienstuser und gemeinsame Pipeline-Gruppe (wie debian/postinst)
pw groupshow media-pipeline >/dev/null 2>&1 || pw groupadd media-pipeline
pw usershow tw-recorder >/dev/null 2>&1 || pw useradd tw-recorder -d /nonexistent -s /usr/sbin/nologin -G media-pipeline
install -d -o root -g media-pipeline -m 2775 /srv/media-pipeline /srv/media-pipeline/recordings
install -d /etc/tw-recorder

# rc.d-Skript installieren und aktivieren
install -m 755 freebsd/rc.d/tw-recorder /usr/local/etc/rc.d/tw-recorder
sysrc tw_recorder_enable=YES
service tw-recorder start
```

Vor dem `service tw-recorder start` wie unter Linux `/etc/tw-recorder/recorder.conf` anlegen (Vorlage: `recorder.conf.example`).

Das Skript startet `tw-recorder -D` über `daemon(8)` (Neustart bei Absturz, Userwechsel inkl. `media-pipeline`-Zusatzgruppe) und leitet die Ausgabe an syslog weiter (Tag `tw-recorder`, landet standardmäßig in `/var/log/messages`). Weitere Einstellungen (`tw_recorder_runas`, `tw_recorder_args`, `tw_recorder_env`) stehen im Kopf des Skripts.

### Alternative: Standalone .pyz (kein pip nötig)

`./build-pyz.sh` baut aus `src/` ein einziges, selbst-enthaltenes `tw-recorder.pyz` - läuft auf jedem System mit einem nackten `python3`, ganz ohne vorherige `pip install`. `streamlink` bleibt aber eine echte externe Abhängigkeit (eigenständiges CLI-Tool, kein Python-Import) und muss weiterhin separat installiert sein (Version >= 8.2.0, siehe Hinweis oben):

```bash
./build-pyz.sh
./tw-recorder.pyz --version
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
* `STORAGE_DIR`: Standard `/srv/media-pipeline/recordings`, einheitlich für Docker und Bare-Metal - dasselbe Verzeichnis, aus dem `fetchbridge` liest (`SOURCE_DIR`). Das `.deb`-Postinst legt es mit einer gemeinsamen Gruppe (`media-pipeline`) an, damit beide Systemuser darauf zugreifen können.
* `LOG_LEVEL`: `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (case-insensitive). Standard `INFO`, ein ungültiger Wert fällt sicher darauf zurück. Hat immer Vorrang vor `DEBUG`.
* `DEBUG`: Standard `0`. Auf `true`/`1` setzen für erweiterte Log-Ausgaben - abwärtskompatible Kurzform für `LOG_LEVEL=DEBUG`, nur wirksam falls `LOG_LEVEL` nicht gesetzt ist.

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
