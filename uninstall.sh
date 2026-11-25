#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Remove what install.sh installed. Your settings (~/.config/zypp-desk) are kept, and so is the choice to download
# updates ahead (/etc/zypp-desk), which the zypp-desk package also reads.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.jordirelats.ZyppDesk
echo "Removing the helper, the polkit action and the refresh timer (root password needed):"
# The polkit files go only if no package owns them (the zypp-desk package has its own).
# The timer's units in /etc/systemd/system are install.sh's (the package puts its own in /usr/lib/systemd/system).
sudo sh -c 'if [ -e /etc/systemd/system/zypp-desk-refresh.timer ]; then
              systemctl disable --now zypp-desk-refresh.timer 2>/dev/null
              systemctl stop zypp-desk-refresh.service 2>/dev/null
              rm -f /etc/systemd/system/zypp-desk-refresh.timer /etc/systemd/system/zypp-desk-refresh.service
              systemctl daemon-reload
            fi
            rm -f /usr/local/libexec/zypp-desk/zypp-desk-helper /usr/local/libexec/zypp-desk/zypp-desk-refresh
            rmdir --ignore-fail-on-non-empty /usr/local/libexec/zypp-desk 2>/dev/null
            for f in "$1" "$2"; do
              if [ -e "$f" ] && ! rpm -qf "$f" >/dev/null 2>&1; then rm -f "$f"; fi
            done
            true' sh "/usr/share/polkit-1/actions/$APP_ID.local.policy" "/usr/share/polkit-1/actions/$APP_ID.policy"
rm -f "$HOME/.local/bin/zypp-desk" "$HOME/.local/share/applications/$APP_ID.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
# The tray icon at login: an entry that starts it from this folder goes (one that says just "zypp-desk" stays: it
# is skipped while no zypp-desk is installed, and works again with the package).
autostart="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.tray.desktop"
if [ -f "$autostart" ] && grep -qF "$SRC/" "$autostart"; then
  rm -f "$autostart"
fi
# A stale icon index (icon-theme.cache) hides new icons from the menu and the desktop: rebuild it.
icons="$HOME/.local/share/icons/hicolor"
if [ -e "$icons/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q -f -t "$icons" || true
fi
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Done."
