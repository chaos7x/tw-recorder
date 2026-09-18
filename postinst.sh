#!/bin/sh
# Wird von dpkg nach dem Entpacken des .deb ausgefuehrt (fpm --after-install).
set -e

if ! getent passwd tw-recorder >/dev/null 2>&1; then
    adduser --system --group --no-create-home --home /nonexistent \
        --shell /usr/sbin/nologin tw-recorder
fi

# /run/systemd/system existiert nur, wenn systemd tatsaechlich als Init-System
# laeuft (nicht z.B. in einem Chroot/Container-Build ohne systemd) - ohne
# diese Absicherung wuerde die Paketinstallation dort fehlschlagen.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi

echo ""
echo "tw-recorder wurde installiert, der systemd-Service ist aber noch NICHT aktiviert."
echo "Bitte zuerst /etc/tw-recorder/recorder.conf ([channels] etc.) anpassen,"
echo "dann den Dienst manuell aktivieren und starten:"
echo ""
echo "    systemctl enable --now tw-recorder"
echo ""

exit 0
