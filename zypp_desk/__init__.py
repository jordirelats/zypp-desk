# SPDX-License-Identifier: GPL-3.0-or-later
"""Zypp Desk: a graphical front end that exposes every zypper feature."""

APP_ID = "io.github.jordirelats.ZyppDesk"
APP_NAME = "Zypp Desk"
VERSION = "0.5.5"

# Where the privileged helper may be installed (packaged first, then a local install).
HELPER_PATHS = (
    "/usr/libexec/zypp-desk/zypp-desk-helper",
    "/usr/local/libexec/zypp-desk/zypp-desk-helper",
)
