#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Compile po/<lang>.po into locale/<lang>/LC_MESSAGES/zypp-desk.mo (what the program reads).
set -eu
cd "$(dirname "$0")/.."
for po in po/*.po; do
  [ -e "$po" ] || continue
  lang=$(basename "$po" .po)
  mkdir -p "locale/$lang/LC_MESSAGES"
  msgfmt --check --output-file="locale/$lang/LC_MESSAGES/zypp-desk.mo" "$po"
done
