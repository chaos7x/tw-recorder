#!/bin/sh
# Wird von dpkg vor dem Entfernen des Pakets ausgefuehrt (fpm --before-remove).
# $1 = "remove" beim tatsaechlichen Entfernen, "upgrade" bei einem Update auf
# eine neue Version - der Dienst soll bei einem Upgrade weiterlaufen.
set -e

if [ "$1" = "remove" ] && [ -d /run/systemd/system ]; then
    systemctl stop tw-recorder.service || true
    systemctl disable tw-recorder.service || true
fi

exit 0
