#!/bin/sh
# Wird von dpkg nach dem Entpacken des .deb ausgefuehrt (fpm --after-install).
set -e

if ! getent passwd tw-recorder >/dev/null 2>&1; then
    adduser --system --group --no-create-home --home /nonexistent \
        --shell /usr/sbin/nologin tw-recorder
fi

# recorder.conf kann Twitch-Zugangsdaten (client_secret/user_token) im
# Klartext enthalten, dpkg legt sie aber als root:root 0644 an (fuer jeden
# lesbar). Der Dienst muss sie nur lesen, nie schreiben: root:tw-recorder 0640.
# Nur angepasst, solange die Datei noch genau im Paket-Standard steht - eigene
# Rechte des Admins bleiben bei einem Upgrade unangetastet.
conf=/etc/tw-recorder/recorder.conf
if [ -f "$conf" ] && [ "$(stat -c '%U:%G %a' "$conf")" = "root:root 644" ]; then
    chown root:tw-recorder "$conf"
    chmod 0640 "$conf"
fi
# Dasselbe fuer conf.d/: dort angelegte *.conf koennen ebenfalls Secrets
# enthalten. root:tw-recorder 0750 sperrt alle anderen schon am Verzeichnis
# aus, auch wenn eine einzelne Datei darin versehentlich 0644 hat.
confd=/etc/tw-recorder/conf.d
if [ -d "$confd" ] && [ "$(stat -c '%U:%G %a' "$confd")" = "root:root 755" ]; then
    chown root:tw-recorder "$confd"
    chmod 0750 "$confd"
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
# defaults-disabled statt defaults: "defaults" legt S-Links in rc2-5 an, der
# Dienst wuerde also beim naechsten Boot automatisch starten - genau wie ein
# `systemctl enable`, das der systemd-Zweig bewusst NICHT macht. Mit
# defaults-disabled entstehen nur K-Links, aktiviert wird erst manuell per
# `update-rc.d tw-recorder enable`. Existieren bereits Links (Upgrade, oder vom
# Admin aktiviert), aendert update-rc.d nichts daran.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
elif command -v update-rc.d >/dev/null 2>&1; then
    update-rc.d tw-recorder defaults-disabled >/dev/null
fi

echo ""
echo "tw-recorder wurde installiert, der Dienst ist aber noch NICHT aktiviert."
echo "Bitte zuerst /etc/tw-recorder/recorder.conf ([channels] etc.) anpassen,"
echo "dann den Dienst manuell aktivieren und starten:"
echo ""
if [ -d /run/systemd/system ]; then
    echo "    systemctl enable --now tw-recorder"
else
    echo "    update-rc.d tw-recorder enable"
    echo "    service tw-recorder start"
fi
echo ""

exit 0
