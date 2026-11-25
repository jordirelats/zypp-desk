# SPDX-License-Identifier: GPL-3.0-or-later
"""Routines: a chain of commands written the way you type them in a terminal.

    zypper -v ref ; zypper -v dup -d -l && zypper -v dup ; flatpak update -y

';' runs the next command anyway, '&&' only if the previous one worked. Only zypper and
flatpak commands are accepted. A leading 'sudo' is dropped: the app asks for the password itself.
"""

import shlex

from .i18n import _
from .runner import Step

ALLOWED_PROGRAMS = ("zypper", "flatpak")
DEFAULT_ROUTINE = "zypper -v ref ; zypper -v dup -d -l && zypper -v dup"


class RoutineError(ValueError):
    pass


def parse(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&")
    lex.whitespace_split = True
    tokens = list(lex)
    steps, current, cond = [], [], "always"
    for tok in tokens + [";"]:
        if tok in (";", "&&"):
            if current:
                steps.append(_make_step(current, cond))
            elif tok == "&&":
                raise RoutineError(_("Empty command before '{op}'.").format(op=tok))
            current = []
            cond = "success" if tok == "&&" else "always"
        elif set(tok) <= {";", "&"}:
            raise RoutineError(_("Only ';' and '&&' can join commands (found '{op}').").format(op=tok))
        else:
            current.append(tok)
    if not steps:
        raise RoutineError(_("The routine is empty."))
    steps[0].cond = "always"
    return steps


def _make_step(words, cond):
    if words[0] == "sudo":
        words = words[1:]
    if not words or words[0] not in ALLOWED_PROGRAMS:
        raise RoutineError(_("Only zypper and flatpak commands are allowed (found '{cmd}').")
                           .format(cmd=" ".join(words)))
    return Step(words[0], words[1:], cond=cond)


def describe(step):
    """A short human title for a routine step."""
    a = step.args
    if step.program == "flatpak":
        if "update" in a:
            return _("Update Flatpak apps")
        return _("Flatpak")
    sub = next((x for x in a if not x.startswith("-")), "")
    if sub in ("ref", "refresh"):
        return _("Refresh the repositories")
    if sub in ("dup", "dist-upgrade"):
        if "-d" in a or "--download-only" in a:
            if "-l" in a or "--auto-agree-with-licenses" in a:
                return _("Download everything and accept the licenses")
            return _("Download everything")
        return _("Upgrade the system")
    if sub in ("up", "update"):
        return _("Update packages")
    if sub == "patch":
        return _("Install patches")
    if sub == "ps":
        return _("Check what must be restarted")
    return "zypper " + sub
