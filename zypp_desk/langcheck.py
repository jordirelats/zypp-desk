# SPDX-License-Identifier: GPL-3.0-or-later
"""Is the language of this computer among zypper's requested languages?

zypper installs the translations of the programs (the -lang packages, dictionaries) only for the languages it was
asked for (/var/lib/zypp/RequestedLocales). A computer set up in a language that zypper was never asked for
shows parts of its programs in English, and nothing says why. Zypp Desk proposes to add it (Home), for whatever
language the computer uses. No Qt; files only.
"""

import os
import re

REQUESTED = "/var/lib/zypp/RequestedLocales"
LOCALE_CONF = "/etc/locale.conf"


def _code(value):
    """'ca_ES.UTF-8@euro' → 'ca_ES'; None for C, POSIX or nothing."""
    v = (value or "").strip().strip('"').split(".")[0].split("@")[0]
    if not v or v in ("C", "POSIX") or not re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", v):
        return None
    return v


def system_language(conf=LOCALE_CONF):
    """The language of the computer: LANG of /etc/locale.conf, else of this session."""
    try:
        with open(conf, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("LANG="):
                    code = _code(line.split("=", 1)[1])
                    if code:
                        return code
    except OSError:
        pass
    return _code(os.environ.get("LANG"))


def requested(path=REQUESTED):
    """The languages zypper was asked for, or None if the file cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except OSError:
        return None


def missing_system_language(conf=LOCALE_CONF, path=REQUESTED):
    """The code of the computer's language when zypper was not asked for it (so its translations are not
    installed), else None. English needs nothing: the programs are written in it."""
    code = system_language(conf)
    if not code or code.split("_")[0] == "en":
        return None
    req = requested(path)
    if req is None:
        return None
    if code in req or code.split("_")[0] in req:
        return None
    return code
