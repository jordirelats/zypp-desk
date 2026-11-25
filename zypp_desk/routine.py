# SPDX-License-Identifier: GPL-3.0-or-later
"""Routines: a chain of commands written the way you type them in a terminal.

    zypper -v ref ; zypper -v dup -d -l && zypper -v dup ; flatpak update -y

';' runs the next command anyway, '&&' only if the previous one worked. Only zypper and
flatpak commands are accepted. A leading 'sudo' is dropped: the app asks for the password itself.
zypper gets -n (no questions) and flatpak -y when they are missing: nobody can answer in the middle.
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
    # A license accepted with -l in one step (zypper dup -d -l) is asked again by the next one (zypper dup);
    # in a terminal you would say yes, but with -n nobody can: carry the acceptance over.
    agreed = False
    for st in steps:
        if st.program != "zypper":
            continue
        if {"-l", "--auto-agree-with-licenses"} & set(st.args):
            agreed = True
        elif agreed and _subcommand(st.args) in LICENSE_SUBCOMMANDS:
            st.args.append("--auto-agree-with-licenses")
    return steps


LICENSE_SUBCOMMANDS = {"dup", "dist-upgrade", "up", "update", "in", "install", "patch"}


def _subcommand(args):
    return next((a for a in args if not a.startswith("-")), "")


FLATPAK_CHANGES = {"install", "update", "upgrade", "uninstall", "remove"}


def _make_step(words, cond):
    if words[0] == "sudo":
        words = words[1:]
    if not words or words[0] not in ALLOWED_PROGRAMS:
        raise RoutineError(_("Only zypper and flatpak commands are allowed (found '{cmd}').")
                           .format(cmd=" ".join(words)))
    args = list(words[1:])
    # Nobody can answer a question in the middle of a routine (zypper would stop with "cannot read
    # input"). zypper -n takes the default answers ("yes, continue") and stops without changing
    # anything when you would have to choose (a conflict, an unknown key). The added option is part
    # of the command shown on screen.
    if words[0] == "zypper" and not _has_non_interactive(args):
        args.insert(0, "-n")
    if words[0] == "flatpak" and FLATPAK_CHANGES & set(args) and not {"-y", "--assumeyes", "--noninteractive"} & set(args):
        args.append("-y")
    return Step(words[0], args, cond=cond, strict=True)  # '&&' as in a shell: only exit code 0 goes on


def _has_non_interactive(args):
    for a in args:
        if not a.startswith("-"):
            return False  # the subcommand: global options come before it
        if a == "--non-interactive" or (not a.startswith("--") and "n" in a[1:]):
            return True
    return False


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
