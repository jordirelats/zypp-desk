#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install Zypp Desk from this source folder:
#   - the root helper in /usr/local/libexec/zypp-desk/ and its polkit action (asks for the root password once);
#   - a launcher in ~/.local/bin and a menu entry in ~/.local/share/applications (for your user only);
#   - the compiled translations in ./locale.
# ./uninstall.sh undoes it. Packages for distributions will install to /usr instead.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.jordirelats.ZyppDesk
HELPER=/usr/local/libexec/zypp-desk/zypp-desk-helper
POLICY=/usr/share/polkit-1/actions/$APP_ID.policy

"$SRC/tools/build-mo.sh"
tmp=$(mktemp)
sed "s|@HELPER@|$HELPER|" "$SRC/data/$APP_ID.policy.in" > "$tmp"
echo "Installing the helper and the polkit action (root password needed):"
sudo sh -c "install -D -o root -g root -m 0755 '$SRC/helper/zypp-desk-helper' '$HELPER' && \
            install -D -o root -g root -m 0644 '$tmp' '$POLICY'"
rm -f "$tmp"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$SRC/data/icons/$APP_ID.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
ln -sf "$SRC/zypp-desk" "$HOME/.local/bin/zypp-desk"
# The icon by its full path: a desktop shell that started before the icon existed does not find it by name.
sed -e "s|@EXEC@|$SRC/zypp-desk|" \
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
