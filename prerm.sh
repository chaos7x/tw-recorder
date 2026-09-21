#!/bin/sh
# Wird von dpkg vor dem Entfernen des Pakets ausgefuehrt (fpm --before-remove).
# $1 = "remove" beim tatsaechlichen Entfernen, "upgrade" bei einem Update auf
# eine neue Version - der Dienst soll bei einem Upgrade weiterlaufen.
set -e

if [ "$1" = "remove" ]; then
    if [ -d /run/systemd/system ]; then
        systemctl stop tw-recorder.service || true
        systemctl disable tw-recorder.service || true
    elif [ -x /etc/init.d/tw-recorder ]; then
        /etc/init.d/tw-recorder stop || true
        command -v update-rc.d >/dev/null 2>&1 && update-rc.d tw-recorder remove >/dev/null || true
    fi
fi

exit 0
