# 🏗️ Architektur

Dieses Dokument beschreibt den Aufbau von `tw-recorder` auf Modulebene: welche Komponente was tut und wie Konfiguration, Streamlink/FFmpeg und die Twitch-Helix-API zusammenspielen. Für Betriebsanleitungen (Docker, `recorder.conf`) siehe [README.md](README.md).

## Überblick

```mermaid
flowchart TD

subgraph group_entry_ops["Entry &amp; Operations"]
  node_cli["CLI Entry<br/>[main.py]"]
  node_daemon["Stream Daemon<br/>[daemon.py]"]
  node_healthcheck["Healthcheck<br/>[healthcheck.py]"]
  node_logging["Logging Setup<br/>[logging_setup.py]"]
  node_heartbeat["Heartbeat File"]
end

subgraph group_configuration["Configuration"]
  node_config_loader["Config Loader<br/>[config.py]"]
  node_config_files["Config Files"]
end

subgraph group_discovery["Stream Discovery"]
  node_stream_status["Stream Status<br/>[twitch_api.py]"]
end

subgraph group_recording["Recording Pipeline"]
  node_stream_recorder["Record Loop<br/>[recorder.py]"]
  node_lock_guard["File Lock Guard<br/>[recorder.py]"]
  node_recording_store["Recording Store"]
  node_output_metadata["MKV Metadata<br/>[recorder.py]"]
end

subgraph group_external["External Integrations"]
  node_twitch_helix["Twitch Helix"]
  node_streamlink["Streamlink CLI"]
  node_ffmpeg["FFmpeg Remuxer"]
end

node_operator(("Operator"))

node_operator -->|"invokes"| node_cli
node_cli -->|"loads config"| node_config_loader
node_cli -->|"configures logs"| node_logging
node_cli -->|"starts daemon"| node_daemon
node_cli -->|"runs check"| node_healthcheck
node_daemon -->|"loads channels"| node_config_loader
node_config_loader -->|"reads files"| node_config_files
node_daemon -->|"hashes config"| node_config_loader
node_daemon -->|"dispatches channels"| node_stream_recorder
node_daemon -->|"writes heartbeat"| node_heartbeat
node_healthcheck -->|"reads heartbeat"| node_heartbeat
node_stream_recorder -->|"acquires lock"| node_lock_guard
node_stream_recorder -->|"gets options"| node_config_loader
node_stream_recorder -->|"checks status"| node_stream_status
node_stream_status -->|"queries API"| node_twitch_helix
node_stream_status -.->|"falls back"| node_streamlink
node_stream_recorder -->|"records stream"| node_streamlink
node_stream_recorder -.->|"checks metadata"| node_stream_status
node_stream_recorder -->|"writes transport"| node_recording_store
node_stream_recorder -->|"builds metadata"| node_output_metadata
node_stream_recorder -->|"remuxes recording"| node_ffmpeg
node_ffmpeg -->|"writes MKV"| node_recording_store

click node_cli "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/main.py"
click node_daemon "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/daemon.py"
click node_healthcheck "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/healthcheck.py"
click node_logging "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/logging_setup.py"
click node_config_loader "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/config.py"
click node_stream_recorder "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/recorder.py"
click node_lock_guard "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/recorder.py"
click node_stream_status "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/twitch_api.py"
click node_output_metadata "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/recorder.py"

classDef toneNeutral fill:#f8fafc,stroke:#334155,stroke-width:1.5px,color:#0f172a
classDef toneBlue fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#172554
classDef toneAmber fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
classDef toneMint fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
classDef toneRose fill:#ffe4e6,stroke:#e11d48,stroke-width:1.5px,color:#881337
classDef toneIndigo fill:#e0e7ff,stroke:#4f46e5,stroke-width:1.5px,color:#312e81
classDef toneTeal fill:#ccfbf1,stroke:#0f766e,stroke-width:1.5px,color:#134e4a
class node_cli,node_daemon,node_healthcheck,node_logging,node_heartbeat toneBlue
class node_config_loader,node_config_files toneAmber
class node_stream_status toneMint
class node_stream_recorder,node_lock_guard,node_recording_store,node_output_metadata toneRose
class node_twitch_helix,node_streamlink,node_ffmpeg,node_operator toneIndigo
```

## Komponenten

### Laufzeitsteuerung

- **`main.py`** — argparse-CLI mit genau einem sinnvollen Dauerbetriebs-Modus: `-D`/`--daemon` startet den Kanal-Daemon; `--healthcheck` prüft nur den Heartbeat und beendet sich sofort (für Docker `HEALTHCHECK`, keine Config wird dafür geladen); ohne Flag druckt es nur die Usage. Lädt vor `run_daemon()` einmalig die Konfiguration und initialisiert darüber das Logging.
- **`logging_setup.py`** — richtet das Logging (Konsole/Datei) anhand der geladenen Konfiguration ein.
- **`daemon.py`** — hält pro überwachtem Kanal einen eigenen Thread (`running_threads`, Dict von URL → `(Thread, threading.Event)`). `sync_processes()` vergleicht die aktuell in der Config eingetragenen Kanäle (`parse_streamers()`, Sektion `[channels]`) mit den laufenden Threads: neue Kanäle bekommen einen Thread (`record_loop`), entfernte werden per `stop_event` sauber beendet. `run_daemon()` läuft alle 5s eine Hauptschleife, die per Hash-Vergleich (`config.get_config_hash()`) erkennt, ob sich Config-Dateien geändert haben, und nur dann neu synchronisiert — sonst reines Heartbeat-Schreiben. `SIGINT`/`SIGTERM` stoppen alle Kanal-Threads mit einem gemeinsamen 10s-Zeitbudget (nicht 10s pro Thread), um den Shutdown bei vielen Kanälen nicht unnötig zu verzögern.
- **`healthcheck.py`** — `write_heartbeat()` wird bei jedem Durchlauf der `run_daemon()`-Hauptschleife aufgerufen; `run_healthcheck()` prüft nur Existenz/Alter der Heartbeat-Datei, ohne Config zu laden, damit externe Health-Probes den Prozess nicht belasten.

### Konfiguration

- **`config.py`** — liest `recorder.conf` und alphabetisch sortiert alle `conf.d/*.conf` ein (spätere Dateien überschreiben frühere Werte), mit Fallback auf Environment-Variablen pro Einstellung. `get_config_hash()`/`get_config_files_state()` bilden einen MD5-Hash über alle Config-Dateien — die Grundlage für das Hot-Reload sowohl im Daemon als auch pro Kanal-Thread in `record_loop()` (Config wird dort nur bei tatsächlicher Änderung neu geparst, nicht bei jedem Poll-Tick). `check_secrets_permissions()` warnt (korrigiert aber nicht aktiv, da die Datei meist read-only vom Host gemountet ist), falls `client_secret`/`user_token` im Klartext in einer für Gruppe/Andere lesbaren Datei liegen.

### Stream-Erkennung

- **`twitch_api.py`** — Anbindung an die Twitch-Helix-API: `get_app_access_token()` holt/cached einen App-Access-Token thread-sicher; `check_stream_online()` prüft den Live-Status eines Kanals; `get_stream_info()` liefert zusätzlich Titel/Kategorie für die Split-Erkennung. Ist die Helix-API vorübergehend nicht erreichbar, wird für 60 Sekunden auf einen Streamlink-Fallback (`streamlink --stream-url`) umgeschaltet, statt bei jedem Poll erneut zu scheitern. Fehlen `client_id`/`client_secret`, greift für Nicht-Twitch-URLs bzw. generell derselbe Streamlink-Fallback.

### Aufnahmeausgabe

- **`recorder.py`** — `record_loop()` ist die pro Kanal in einem eigenen Thread laufende Kernschleife: prüft per `twitch_api` den Live-Status, startet bei Bedarf einen `streamlink`-Subprozess (schreibt eine `.ts`-Datei mit Kanal/Kategorie/Titel im Dateinamen), pumpt dessen Ausgabe ins Logging (`_pump_subprocess_output()`, sonst würde sie am Datei-Log vorbeilaufen) und überwacht optional (`split_on_title_change`) per periodischem Helix-Abruf, ob sich Titel oder Kategorie geändert haben — bei einer Änderung wird die laufende Aufnahme sauber beendet und sofort eine neue gestartet, statt bis zum Stream-Ende in einer Datei zu bleiben. Nach Ende eines Aufnahme-Abschnitts remuxt ein `ffmpeg`-Aufruf (`-c copy`, verlustfrei) die `.ts` in eine `.mkv` mit gesetzten Metadaten (Titel, Kommentar, Artist, Datum) und optional gedrosselter I/O-Priorität (`ionice -c3`), bevor die `.ts`-Quelldatei gelöscht wird. Ein `fcntl`-Filelock (`.record.lock` pro Kanalverzeichnis) verhindert parallele Aufnahmen desselben Kanals.

### Dateien & Zustand

- **TS-Aufnahmen** — von Streamlink direkt geschriebene Rohaufnahmen je Kanal-Unterverzeichnis unter `storage_dir`; Dateiname kodiert Zeit, Kanal, Kategorie (in `[...]`) und Titel für das spätere Parsing durch `_parse_recorded_filename()`.
- **MKV-Speicher** — das nach dem Remux verbleibende Endergebnis, benannt nach dem konfigurierbaren `filename_pattern`.
- **Aufnahme-Lock** — `.record.lock`-Datei pro Kanalverzeichnis, verhindert doppelte gleichzeitige Aufnahmen desselben Kanals (z. B. bei zwei Daemon-Instanzen).
- **Heartbeat-Datei** — von `healthcheck.py` geschrieben/gelesen, Grundlage für `--healthcheck` und Docker/Kubernetes-Liveness-Probes.
