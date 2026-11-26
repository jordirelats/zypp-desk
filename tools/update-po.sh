#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Extract the strings to translate into po/zypp-desk.pot and merge them into every po/<lang>.po.
# Run it after changing user-visible strings. Translators only edit po/<lang>.po.
set -eu
cd "$(dirname "$0")/.."
find zypp_desk -name '*.py' | LC_ALL=C sort > po/POTFILES
xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=ngettext:1,2 --keyword=pgettext:1c,2 \
  --add-comments=TRANSLATORS --sort-by-file --package-name="Zypp Desk" \
  --msgid-bugs-address="https://github.com/jordirelats/zypp-desk/issues" \
  --files-from=po/POTFILES --output=po/zypp-desk.pot
for po in po/*.po; do
  [ -e "$po" ] || continue
  msgmerge --quiet --update --backup=none --no-fuzzy-matching "$po" po/zypp-desk.pot
  printf '%s: ' "$po"; msgfmt --statistics --output-file=/dev/null "$po"
done
