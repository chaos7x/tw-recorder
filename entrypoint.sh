#!/bin/sh

# Prüfen, ob ein gültiger Systembefehl oder absoluter Pfad als erstes Argument übergeben wurde
if [ "$#" -gt 0 ] && ( [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 ); then
    exec "$@"
fi

# Docker startet den Recorder standardmäßig im Daemon-Modus, wenn keine Argumente übergeben werden
if [ "$#" -eq 0 ]; then
    set -- -D
fi

# Wenn ein externes Skript gemountet wurde, nutzen wir das
if [ -f /app/tw-recorder ]; then
    echo ">>> Using external tw-recorder from volume..."
    chmod +x /app/tw-recorder 2>/dev/null || true
    exec /app/tw-recorder "$@"
else
    # Andernfalls greifen wir auf das im Image eingebaute Skript zurück
    echo ">>> Using internal /usr/local/bin/tw-recorder from image..."
    exec /usr/local/bin/tw-recorder "$@"
fi
