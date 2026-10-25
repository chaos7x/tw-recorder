# Experimentell: Streamlink als Library statt CLI

**Nicht eingebaut, nicht mergen.** `streamlink_api_recorder.py` ist ein
eigenständiger Prototyp; `record_loop()` (streamlink-CLI → `.ts` → Remux)
bleibt unverändert.

## Ablauf

```
Streamlink()-Session ─ resolve_url() ─ Twitch-Plugin(Options{api-header}) ─ streams()["best"].open()
        │  fd.read(64 KiB)   (MPEG-TS-Bytes, genau wie sie Twitch liefert)
        ▼
ffmpeg -f mpegts -i pipe:0 -metadata … -timecode … -c copy -f matroska <ziel>.mkv
```

Streamlink remuxt nicht; ffmpeg bleibt als Muxer drin, nur das Zwischen-`.ts`
und der zweite Remux-Lauf fallen weg.

## Testen

```
pip install "streamlink>=8.2" pytest
pytest -v experimental/          # nicht in CI (testpaths = tests/)
python3 experimental/streamlink_api_recorder.py https://twitch.tv/<kanal> /tmp/rec
```

Die Tests nutzen einen lokal mit ffmpeg erzeugten Live-HLS-Stream (`hls://`),
kein echtes Twitch.

## Befunde (Streamlink 8.6.2, ffmpeg 6.1)

| Punkt | Ergebnis |
|---|---|
| Twitch-Token | über `Options().set("api-header", …)` an den Plugin-Konstruktor; bleibt im Speicher, keine `--config`-Temp-Datei nötig |
| Metadaten | `plugin.get_author/get_category/get_title()` direkt nach `streams()`, kein Parsen des Dateinamens mehr |
| Tags | `RECORDING_START`, `creation_time`, `TIMECODE` usw. vorab gesetzt, landen korrekt in der MKV |
| SIGTERM / Strg+C | sauber: Dauer + Cues vorhanden (ffmpeg in eigener Session, Beenden per EOF auf stdin) |
| SIGTERM an alle (systemd `KillMode=control-group`) | ebenfalls sauber, ffmpeg finalisiert selbst |
| SIGKILL an alles (OOM, Absturz, Stromausfall) | MKV lesbar, aber **ohne Dauer/Cues**; per `ffmpeg -i x.mkv -c copy y.mkv` reparierbar. `.ts` hat dieses Problem nicht |
| Stream-Ende | `read()` liefert `b""`, regulärer Abschluss |

## Was für einen echten Einbau fehlen würde

- Reconnect/Retry und Titel-/Kategorie-Split (heute Sache von `record_loop()`/`_monitor_recording()`)
- `filename_pattern`, `--fs-safe-rules POSIX`-Äquivalent, xattrs, Logging-Integration
- Recovery-Pfad für unfertige MKVs nach Absturz (Remux beim nächsten Start)
- streamlink als echte Python-Dependency (heute nur CLI im PATH); Debian/FreeBSD-Pakete
  müssten die Library mitbringen, Bookworm hat zu alte Versionen
- API-Bruch-Risiko: Plugin-Optionen liefen früher über `session.set_plugin_option()`
  (in Streamlink 6 entfernt); die CLI-Argumente sind seit Jahren stabil
