#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Remove what install.sh installed. Your settings (~/.config/zypp-desk) are kept, and so is the choice to download
# updates ahead (/etc/zypp-desk), which the zypp-desk package also reads. At the end it says what else stays.
# If the zypp-desk package is installed too, its own helper, polkit action and timer stay and keep working.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.jordirelats.ZyppDesk
if rpm -q zypp-desk >/dev/null 2>&1; then PACKAGE=1; else PACKAGE=0; fi
echo "Removing the helper, the polkit action and the refresh timer (root password needed):"
# The polkit files go only if no package owns them (the zypp-desk package has its own).
# The timer's units in /etc/systemd/system are install.sh's (the package puts its own in /usr/lib/systemd/system).
# Units in /etc hide the package's units with the same name, and its "enabled" link is the same one: with the
# package installed, the package's timer is turned on again the way the package does it (its preset) and started.
sudo sh -c 'if [ -e /etc/systemd/system/zypp-desk-refresh.timer ]; then
              if [ "$3" = 1 ]; then
                # the "enabled" link points to the unit in /etc: it goes first, the preset makes a new one
                systemctl disable zypp-desk-refresh.timer 2>/dev/null
                rm -f /etc/systemd/system/zypp-desk-refresh.timer /etc/systemd/system/zypp-desk-refresh.service
                systemctl daemon-reload
                systemctl preset zypp-desk-refresh.timer 2>/dev/null
                if systemctl -q is-enabled zypp-desk-refresh.timer 2>/dev/null; then
                  systemctl restart zypp-desk-refresh.timer 2>/dev/null
                fi
              else
                systemctl disable --now zypp-desk-refresh.timer 2>/dev/null
                systemctl stop zypp-desk-refresh.service 2>/dev/null
                rm -f /etc/systemd/system/zypp-desk-refresh.timer /etc/systemd/system/zypp-desk-refresh.service
                systemctl daemon-reload
              fi
            fi
            # "Look for new updates": the unit and the polkit rule of install.sh (the package keeps its own)
            rm -f /etc/systemd/system/zypp-desk-check.service
            rm -f /etc/polkit-1/rules.d/50-io.github.jordirelats.ZyppDesk.rules
            systemctl daemon-reload
            rm -f /usr/local/libexec/zypp-desk/zypp-desk-helper /usr/local/libexec/zypp-desk/zypp-desk-refresh
            rmdir --ignore-fail-on-non-empty /usr/local/libexec/zypp-desk 2>/dev/null
            for f in "$1" "$2"; do
              if [ -e "$f" ] && ! rpm -qf "$f" >/dev/null 2>&1; then rm -f "$f"; fi
            done
            true' sh "/usr/share/polkit-1/actions/$APP_ID.local.policy" "/usr/share/polkit-1/actions/$APP_ID.policy" \
  "$PACKAGE"
rm -f "$HOME/.local/bin/zypp-desk" "$HOME/.local/share/applications/$APP_ID.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "$HOME/.local/share/mime/packages/$APP_ID.xml"
if [ -d "$HOME/.local/share/mime" ] && command -v update-mime-database >/dev/null; then
  update-mime-database "$HOME/.local/share/mime" || true
fi
# The tray icon at login: an entry that starts it from this folder goes (one that says just "zypp-desk" stays: it
# is skipped while no zypp-desk is installed, and works again with the package).
autostart="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.tray.desktop"
if [ -f "$autostart" ] && grep -qF "$SRC/" "$autostart"; then
  rm -f "$autostart"
fi
# The tray icon that is running now (started from this folder) would go on until you log out: ask it to quit,
# through its socket (see zypp_desk/app.py, socket_path). With the package installed it stays.
if [ "$PACKAGE" = 0 ] && command -v python3 >/dev/null; then
  python3 -I - <<'EOF' || true
import os, socket, tempfile
name = f"zypp-desk-tray-{os.getuid()}.sock"
path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), name)
if len(path.encode()) >= 100:
    path = os.path.join(tempfile.gettempdir(), name)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(1)
try:
    s.connect(path)
    s.sendall(b"quit\n")
    print("The tray icon was asked to quit.")
except OSError:
    pass
finally:
    s.close()
EOF
fi
# A stale icon index (icon-theme.cache) hides new icons from the menu and the desktop: rebuild it.
icons="$HOME/.local/share/icons/hicolor"
if [ -e "$icons/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q -f -t "$icons" || true
fi
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Done."
# What stays, and how to remove it if you want to.
cache="${XDG_CACHE_HOME:-$HOME/.cache}/zypp-desk"
config="${XDG_CONFIG_HOME:-$HOME/.config}/zypp-desk"
dropin=/etc/zypp/zypp.conf.d/50-zypp-desk.conf
if [ "$PACKAGE" = 1 ]; then
  echo "The zypp-desk package is installed: it keeps working with its own helper and timer."
else
  echo
  echo "These stay on the computer:"
  if [ -e "$dropin" ]; then
    echo "  - $dropin: the zypp.conf settings you chose in Zypp Desk. zypper still uses them."
    echo "    To go back to openSUSE's settings: sudo rm $dropin"
  fi
  if [ -e /etc/zypp-desk/download-ahead ]; then
    echo "  - /etc/zypp-desk/download-ahead: nothing reads it without Zypp Desk. To remove it: sudo rm -r /etc/zypp-desk"
  fi
  if [ -e /var/cache/zypp-desk-refresh ]; then
    echo "  - /var/cache/zypp-desk-refresh: the timer's cache. To remove it: sudo rm -r /var/cache/zypp-desk-refresh"
  fi
  if [ -e "$cache" ]; then
    echo "  - $cache: downloaded files and keys that were not installed. To remove it: rm -r '$cache'"
  fi
  if [ -e "$config" ]; then
    echo "  - $config: your settings, routines and programs from the web."
  fi
fi
