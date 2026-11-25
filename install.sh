#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install Zypp Desk from this source folder, to try it or to work on it:
#   - the root helper in /usr/local/libexec/zypp-desk/ and its own polkit action (asks for the root password once);
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

if rpm -q zypp-desk >/dev/null 2>&1; then
  echo "The zypp-desk package is installed: use it, or remove it before installing from the source folder." >&2
  exit 1
fi

"$SRC/tools/build-mo.sh"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
sed -e "s|@HELPER@|$HELPER|" -e "s|\"$APP_ID\.run\"|\"$APP_ID.run-local\"|" \
    "$SRC/data/$APP_ID.policy.in" > "$tmp"
echo "Installing the helper and the polkit action (root password needed):"
# The paths go to root as arguments, never inside the command text.
sudo sh -c 'install -D -o root -g root -m 0755 "$1" "$2" && install -D -o root -g root -m 0644 "$3" "$4" &&
            if [ -e "$5" ] && ! rpm -qf "$5" >/dev/null 2>&1; then rm -f "$5"; fi' \
  sh "$SRC/helper/zypp-desk-helper" "$HELPER" "$tmp" "$POLICY" "$OLD_POLICY"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$SRC/data/icons/$APP_ID.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
ln -sf "$SRC/zypp-desk" "$HOME/.local/bin/zypp-desk"
# The icon by its full path: a desktop shell that started before the icon existed does not find it by name.
# Exec is quoted (desktop entry rules) so a folder with spaces works.
sed -e "s|@EXEC@|\"$SRC/zypp-desk\"|" \
    -e "s|^Icon=.*|Icon=$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg|" \
    "$SRC/data/$APP_ID.desktop.in" > "$HOME/.local/share/applications/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
# A stale icon index (icon-theme.cache) hides new icons from the menu and the desktop: rebuild it.
icons="$HOME/.local/share/icons/hicolor"
if [ -e "$icons/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q -f -t "$icons" || true
fi
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Done. Start it from the menu (Zypp Desk) or with: zypp-desk"
