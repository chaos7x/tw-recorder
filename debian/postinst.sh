#!/bin/sh
# Wird von dpkg nach dem Entpacken des .deb ausgefuehrt (fpm --after-install).
set -e

if ! getent passwd tw-recorder >/dev/null 2>&1; then
    adduser --system --group --no-create-home --home /nonexistent \
        --shell /usr/sbin/nologin tw-recorder
fi

# Gemeinsame Gruppe fuer die Uebergabeverzeichnisse der Pipeline
# (tw-recorder -> fetchbridge -> yt-upload). Jedes der drei .deb-Pakete legt
# Gruppe und Verzeichnisse unabhaengig und idempotent an, da die Installationsreihenfolge
# nicht garantiert ist. 2775 + setgid, damit jedes Mitglied neu angelegte
# Dateien des jeweils anderen Dienstes auch wieder verschieben/loeschen kann
# (dafuer reicht Schreibrecht auf das Verzeichnis, unabhaengig vom Datei-Owner).
if ! getent group media-pipeline >/dev/null 2>&1; then
    addgroup --system media-pipeline
fi
adduser tw-recorder media-pipeline

# Rechte/Owner NUR beim allerersten Anlegen setzen, nie bei einem Upgrade
# ueberschreiben - ein Admin, der z.B. chmod 777 auf /srv/media-pipeline/incoming
# gesetzt hat (etwa fuer einen externen Uploader ausserhalb der media-pipeline-
# Gruppe), wuerde sonst bei jedem apt upgrade stillschweigend wieder auf 2775
# zurueckgesetzt. Reihenfolge wichtig: Elternverzeichnis zuerst, sonst wuerde
# ein spaeteres mkdir -p fuer ein Kindverzeichnis das noch fehlende Eltern-
# verzeichnis mit falschen (umask-basierten) Rechten anlegen.
for dir in /srv/media-pipeline /srv/media-pipeline/recordings /srv/media-pipeline/incoming; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        chown root:media-pipeline "$dir"
        chmod 2775 "$dir"
    fi
done

# /var/log gehoert root:root mit 755 - ohne dies koennte der dedizierte
# tw-recorder-User dort nie einen eigenen Unterordner anlegen, und
# logging_setup._resolve_log_file_path()s FHS-Fallback (/var/log/tw-recorder/...)
# wuerde auf jeder frischen Installation stillschweigend nie greifen.
mkdir -p /var/log/tw-recorder
chown tw-recorder:tw-recorder /var/log/tw-recorder

# /run/systemd/system existiert nur, wenn systemd tatsaechlich als Init-System
# laeuft (nicht z.B. in einem Chroot/Container-Build ohne systemd) - ohne
# diese Absicherung wuerde die Paketinstallation dort fehlschlagen. Auf einem
# Nicht-systemd-Host (Devuan, Debian mit sysvinit-core) wird stattdessen das
# mitgelieferte /etc/init.d/tw-recorder per update-rc.d registriert - beide
# Zweige schliessen sich damit gegenseitig aus, es wird nie beides parallel
# verwaltet.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
elif command -v update-rc.d >/dev/null 2>&1; then
    update-rc.d tw-recorder defaults >/dev/null
fi

echo ""
echo "tw-recorder wurde installiert, der Dienst ist aber noch NICHT aktiviert."
echo "Bitte zuerst /etc/tw-recorder/recorder.conf ([channels] etc.) anpassen,"
echo "dann den Dienst manuell aktivieren und starten:"
echo ""
if [ -d /run/systemd/system ]; then
    echo "    systemctl enable --now tw-recorder"
else
    echo "    service tw-recorder start"
fi
echo ""

exit 0
