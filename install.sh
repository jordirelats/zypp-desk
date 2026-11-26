#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install Zypp Desk from this source folder, to try it or to work on it:
#   - the root helper in /usr/local/libexec/zypp-desk/ and its own polkit action (asks for the root password once);
#   - the system timer that refreshes the repositories as root every few hours, so the tray icon knows what is new
#     (zypp-desk-refresh: it never installs anything), in /etc/systemd/system, turned on;
#   - a launcher in ~/.local/bin and a menu entry in ~/.local/share/applications (for your user only);
#   - the compiled translations in ./locale.
# ./uninstall.sh undoes it. Do not mix it with the zypp-desk package: if the package is installed, this stops.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.jordirelats.ZyppDesk
HELPER=/usr/local/libexec/zypp-desk/zypp-desk-helper
# Its own file and action id, so it never overwrites the polkit action of the package.
POLICY=/usr/share/polkit-1/actions/$APP_ID.local.policy
OLD_POLICY=/usr/share/polkit-1/actions/$APP_ID.policy  # where install.sh wrote it up to 0.3.0
REFRESH=/usr/local/libexec/zypp-desk/zypp-desk-refresh
UNITS=/etc/systemd/system

if rpm -q zypp-desk >/dev/null 2>&1; then
  echo "The zypp-desk package is installed: use it, or remove it before installing from the source folder." >&2
  exit 1
fi

"$SRC/tools/build-mo.sh"
tmp=$(mktemp)
service=$(mktemp)
check=$(mktemp)
trap 'rm -f "$tmp" "$service" "$check"' EXIT
sed -e "s|@HELPER@|$HELPER|" -e "s|\"$APP_ID\.run\"|\"$APP_ID.run-local\"|" \
    "$SRC/data/$APP_ID.policy.in" > "$tmp"
sed -e "s|@REFRESH@|$REFRESH|" "$SRC/data/zypp-desk-refresh.service" > "$service"
# "Look for new updates" in the tray: the same refresh at once; the polkit rule lets a user at the computer start it
sed -e "s|@REFRESH@|$REFRESH|" "$SRC/data/zypp-desk-check.service" > "$check"
echo "Installing the helper, the polkit action and the timer that refreshes the repositories (root password needed):"
# The paths go to root as arguments, never inside the command text.
sudo sh -c 'install -D -o root -g root -m 0755 "$1" "$2" && install -D -o root -g root -m 0644 "$3" "$4" &&
            if [ -e "$5" ] && ! rpm -qf "$5" >/dev/null 2>&1; then rm -f "$5"; fi &&
            install -D -o root -g root -m 0755 "$6" "$7" &&
            install -D -o root -g root -m 0644 "$8" "$9/zypp-desk-refresh.service" &&
            install -D -o root -g root -m 0644 "${10}" "$9/zypp-desk-refresh.timer" &&
            install -D -o root -g root -m 0644 "${11}" "$9/zypp-desk-check.service" &&
            install -D -o root -g root -m 0644 "${12}" "${13}" &&
            systemctl daemon-reload && systemctl enable --now zypp-desk-refresh.timer' \
  sh "$SRC/helper/zypp-desk-helper" "$HELPER" "$tmp" "$POLICY" "$OLD_POLICY" \
     "$SRC/helper/zypp-desk-refresh" "$REFRESH" "$service" "$UNITS" "$SRC/data/zypp-desk-refresh.timer" \
     "$check" "$SRC/data/$APP_ID.rules" "/etc/polkit-1/rules.d/50-$APP_ID.rules"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$SRC/data/icons/$APP_ID.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
ln -sf "$SRC/zypp-desk" "$HOME/.local/bin/zypp-desk"
# The icon by its full path: a desktop shell that started before the icon existed does not find it by name.
# Exec is quoted (desktop entry rules) so a folder with spaces works.
sed -e "s|@EXEC@|\"$SRC/zypp-desk\"|" \
    -e "s|^Icon=.*|Icon=$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg|" \
    "$SRC/data/$APP_ID.desktop.in" > "$HOME/.local/share/applications/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
# The .zdesk file type ("Move to another computer"), so the file opens with Zypp Desk
mkdir -p "$HOME/.local/share/mime/packages"
cp "$SRC/data/$APP_ID.mime.xml" "$HOME/.local/share/mime/packages/$APP_ID.xml"
command -v update-mime-database >/dev/null && update-mime-database "$HOME/.local/share/mime" || true
# A stale icon index (icon-theme.cache) hides new icons from the menu and the desktop: rebuild it.
icons="$HOME/.local/share/icons/hicolor"
if [ -e "$icons/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q -f -t "$icons" || true
fi
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Done. Start it from the menu (Zypp Desk) or with: zypp-desk"
