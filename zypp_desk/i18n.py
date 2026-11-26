# SPDX-License-Identifier: GPL-3.0-or-later
"""Translations (gettext). Source strings are English; po/ holds the translations."""

import gettext
import os
from pathlib import Path

DOMAIN = "zypp-desk"
_LOCALE_DIRS = (
    Path(__file__).resolve().parent.parent / "locale",  # running from the source tree
    Path("/usr/local/share/locale"),
    Path("/usr/share/locale"),
)


def _load():
    for d in _LOCALE_DIRS:
        if gettext.find(DOMAIN, str(d)):
            return gettext.translation(DOMAIN, str(d))
    return gettext.NullTranslations()


_t = _load()
_ = _t.gettext
ngettext = _t.ngettext
# a context for the same English words used in two roles ("Remove {name}" as a button and as a change in a list:
# Catalan says "Treu" for the first and "Treure" for the second)
pgettext = _t.pgettext


def user_lang() -> str:
    """The user's message locale, passed to zypper so the terminal speaks their language."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v:
            return v
    return "C"
