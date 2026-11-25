#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Remove what install.sh installed. Your settings (~/.config/zypp-desk) are kept.
set -eu
APP_ID=io.github.jordirelats.ZyppDesk
echo "Removing the helper and the polkit action (root password needed):"
sudo sh -c "rm -f /usr/local/libexec/zypp-desk/zypp-desk-helper /usr/share/polkit-1/actions/$APP_ID.policy && \
            rmdir --ignore-fail-on-non-empty /usr/local/libexec/zypp-desk 2>/dev/null || true"
rm -f "$HOME/.local/bin/zypp-desk" "$HOME/.local/share/applications/$APP_ID.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
# A stale icon index (icon-theme.cache) hides new icons from the menu and the desktop: rebuild it.
icons="$HOME/.local/share/icons/hicolor"
if [ -e "$icons/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q -f -t "$icons" || true
fi
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Done."
