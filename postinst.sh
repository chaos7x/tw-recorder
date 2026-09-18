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

exit 0
