#!/usr/bin/env bash
#
# Remove home-broadband. Keeps the database unless you pass --purge.
set -euo pipefail

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1
[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

CONF_DIR=/etc/broadband-monitor
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# install.sh records where it put things; fall back to the default.
APP_DIR_INSTALLED=/opt/broadband-monitor
# shellcheck source=/dev/null
[[ -f "$CONF_DIR/install.env" ]] && . "$CONF_DIR/install.env"

systemctl disable --now broadband-latency.timer broadband-speed.timer \
                         broadband-publish.timer broadband-prune.timer 2>/dev/null || true
rm -f /etc/systemd/system/broadband-*.service /etc/systemd/system/broadband-*.timer
rm -f /etc/sysctl.d/60-broadband-monitor.conf
systemctl daemon-reload

if [[ "$APP_DIR_INSTALLED" == "$SRC_DIR" ]]; then
  # An --in-place install: this is your working clone, so never delete it.
  echo "Left your checkout at $APP_DIR_INSTALLED alone (in-place install)."
  echo "Remove the virtualenv yourself if you want to: rm -rf $SRC_DIR/.venv"
else
  rm -rf "$APP_DIR_INSTALLED"
  echo "Removed $APP_DIR_INSTALLED"
fi

if [[ $PURGE -eq 1 ]]; then
  rm -rf /var/lib/broadband-monitor "$CONF_DIR"
  userdel broadband 2>/dev/null || true
  echo "Removed everything, including the measurement database."
else
  echo "Kept /var/lib/broadband-monitor (database + deploy key) and $CONF_DIR."
  echo "Re-run with --purge to delete those too."
fi
