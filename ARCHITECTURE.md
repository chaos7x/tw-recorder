# 🏗️ Architektur

Dieses Dokument beschreibt den Aufbau von `tw-recorder` auf Modulebene: welche Komponente was tut und wie Konfiguration, Streamlink/FFmpeg und die Twitch-Helix-API zusammenspielen. Für Betriebsanleitungen (Docker, `recorder.conf`) siehe [README.md](README.md).

## Überblick

```mermaid
flowchart TD

subgraph group_cli_runtime["CLI und Laufzeit"]
  node_main_cli["CLI Einstieg<br/>[main.py]"]
  node_daemon_manager["Daemon Manager<br/>[daemon.py]"]
end

subgraph group_configuration["Konfiguration"]
  node_config_loader["Konfigurationslader<br/>[config.py]"]
  node_config_reload["Hot Reload<br/>[config.py]"]
  node_secret_check["Secret-Prüfung<br/>[config.py]"]
end

subgraph group_acquisition["Stream-Erfassung"]
  node_recording_loop["Aufnahme-Schleife<br/>[recorder.py]"]
  node_twitch_api["Twitch API<br/>[twitch_api.py]"]
  node_record_lock["Aufnahme-Sperre<br/>[recorder.py]"]
end

subgraph group_media_output["Medienausgabe"]
  node_ts_files["TS-Aufnahmen"]
  node_mkv_files["MKV-Aufnahmen"]
end

subgraph group_observability["Betrieb und Status"]
  node_heartbeat_writer["Heartbeat-Schreiber<br/>[healthcheck.py]"]
  node_healthcheck["Healthcheck<br/>[healthcheck.py]"]
  node_heartbeat_file["Heartbeat-Datei"]
  node_logging_setup["Logging Setup<br/>[logging_setup.py]"]
  node_log_output["Log-Ausgabe"]
end

node_operator(("Operator"))
node_twitch_service["Twitch Helix"]
node_streamlink_cli["Streamlink CLI"]
node_ffmpeg_cli["FFmpeg CLI"]

node_operator -->|"startet CLI"| node_main_cli
node_main_cli -->|"startet Prüfung"| node_healthcheck
node_main_cli -->|"lädt Konfiguration"| node_config_loader
node_main_cli -->|"richtet Logging ein"| node_logging_setup
node_main_cli -->|"startet Daemon"| node_daemon_manager
node_daemon_manager -->|"liest Kanäle"| node_config_loader
node_daemon_manager -->|"prüft Änderungen"| node_config_reload
node_daemon_manager -->|"prüft Secrets"| node_secret_check
node_daemon_manager -->|"startet Threads"| node_recording_loop
node_recording_loop -->|"prüft Status"| node_twitch_api
node_twitch_api -->|"fragt Helix ab"| node_twitch_service
node_twitch_api -.->|"nutzt Fallback"| node_streamlink_cli
node_recording_loop -->|"akquiriert Lock"| node_record_lock
node_recording_loop -->|"startet Aufnahme"| node_streamlink_cli
node_streamlink_cli -->|"schreibt TS"| node_ts_files
node_recording_loop -->|"prüft Metadaten"| node_twitch_api
node_recording_loop -->|"startet Remux"| node_ffmpeg_cli
node_ffmpeg_cli -->|"schreibt MKV"| node_mkv_files
node_daemon_manager -->|"schreibt Heartbeat"| node_heartbeat_writer
node_heartbeat_writer -->|"aktualisiert Datei"| node_heartbeat_file
node_healthcheck -->|"liest Datei"| node_heartbeat_file
node_logging_setup -->|"schreibt Logs"| node_log_output

click node_main_cli "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/main.py"
click node_daemon_manager "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/daemon.py"
click node_recording_loop "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/recorder.py"
click node_config_loader "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/config.py"
click node_config_reload "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/config.py"
click node_secret_check "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/config.py"
click node_twitch_api "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/twitch_api.py"
click node_record_lock "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/recorder.py"
click node_heartbeat_writer "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/healthcheck.py"
click node_healthcheck "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/healthcheck.py"
click node_logging_setup "https://github.com/chaos7x/tw-recorder/blob/main/src/tw_recorder/logging_setup.py"

classDef toneNeutral fill:#f8fafc,stroke:#334155,stroke-width:1.5px,color:#0f172a
classDef toneBlue fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#172554
classDef toneAmber fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
classDef toneMint fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
classDef toneRose fill:#ffe4e6,stroke:#e11d48,stroke-width:1.5px,color:#881337
classDef toneIndigo fill:#e0e7ff,stroke:#4f46e5,stroke-width:1.5px,color:#312e81
classDef toneTeal fill:#ccfbf1,stroke:#0f766e,stroke-width:1.5px,color:#134e4a
class node_main_cli,node_daemon_manager toneBlue
class node_config_loader,node_config_reload,node_secret_check toneAmber
class node_recording_loop,node_twitch_api,node_record_lock toneMint
class node_ts_files,node_mkv_files toneRose
class node_heartbeat_writer,node_healthcheck,node_heartbeat_file,node_logging_setup,node_log_output,node_operator,node_twitch_service,node_streamlink_cli,node_ffmpeg_cli toneIndigo
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
