# SPDX-License-Identifier: GPL-3.0-or-later
"""Routines: a chain of commands written the way you type them in a terminal.

    zypper -v ref ; zypper -v dup -d -l && zypper -v dup ; flatpak update -y

';' runs the next command anyway, '&&' only if the previous one worked. Only zypper, flatpak and
fwupdmgr commands are accepted. A leading 'sudo' is dropped: the app asks for the password itself.
zypper gets -n (no questions), flatpak -y and 'fwupdmgr update' --assume-yes --no-reboot-check
--no-unreported-check when they are missing: nobody can answer in the middle.

The routine editor works on "items", one per command (to_items / from_items):

    {"kind": "download", "opts": {"verbose", "licenses"}, "cond": "always", "raw": "zypper -v dup -d -l"}

'kind' is one of KINDS, 'opts' the ticked options of that kind (KIND_OPTIONS), 'cond' how the step joins
the previous one ('always' = ';', 'success' = '&&'; ignored for the first) and 'raw' the command as it was
written. A command that is not one of the known kinds is kept as it is, as kind "custom".
"""

import shlex

from .i18n import _
from .runner import Step

ALLOWED_PROGRAMS = ("zypper", "flatpak", "fwupdmgr")
DEFAULT_ROUTINE = "zypper -v ref ; zypper -v dup -d -l && zypper -v dup"


class RoutineError(ValueError):
    pass


def _split(text):
    """[(words, cond)]: the commands of a routine, and how each one joins the previous one."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:  # shlex: "No closing quotation" or "No escaped character"
        if "quotation" in str(e):
            raise RoutineError(_("A quotation mark is not closed.")) from None
        raise RoutineError(_("The line ends with a backslash.")) from None
    commands, current, cond = [], [], "always"
    for tok in tokens + [";"]:
        if tok in (";", "&&"):
            if current:
                commands.append((current, cond))
            elif tok == "&&":
                raise RoutineError(_("Empty command before '{op}'.").format(op=tok))
            current = []
            cond = "success" if tok == "&&" else "always"
        elif set(tok) <= {";", "&"}:
            raise RoutineError(_("Only ';' and '&&' can join commands (found '{op}').").format(op=tok))
        else:
            current.append(tok)
    return commands


def parse(text):
    steps = [_make_step(words, cond) for words, cond in _split(text)]
    if not steps:
        raise RoutineError(_("The routine is empty."))
    steps[0].cond = "always"
    # A license accepted with -l in one step (zypper dup -d -l) is asked again by the next one (zypper dup);
    # in a terminal you would say yes, but with -n nobody can: carry the acceptance over.
    agreed = False
    for st in steps:
        if st.program != "zypper":
            continue
        sub, rest = _sub_args(st)
        if {"-l", "--auto-agree-with-licenses"} & set(st.args) or (sub in LICENSE_SUBCOMMANDS and any(
                x.startswith("-") and not x.startswith("--") and "l" in x[1:] for x in rest)):  # also -dl
            agreed = True
        elif agreed and _subcommand(st.args) in LICENSE_SUBCOMMANDS:
            st.args.append("--auto-agree-with-licenses")
    return steps


LICENSE_SUBCOMMANDS = {"dup", "dist-upgrade", "up", "update", "in", "install", "patch"}


def _subcommand(args):
    return next((a for a in args if not a.startswith("-")), "")


FLATPAK_CHANGES = {"install", "update", "upgrade", "uninstall", "remove"}
FWUPD_SUBCOMMANDS = ("refresh", "update")


def _make_step(words, cond):
    if words[0] == "sudo":
        words = words[1:]
    if not words or words[0] not in ALLOWED_PROGRAMS:
        raise RoutineError(_("Only zypper, flatpak and fwupdmgr commands are allowed (found '{cmd}').")
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
    if words[0] == "fwupdmgr":
        # fwupdmgr runs as the user; fwupd asks for the password itself (polkit). In a routine it may only
        # fetch the firmware lists and install new firmware, without questions and without restarting.
        sub = _subcommand(args)
        if sub not in FWUPD_SUBCOMMANDS:
            raise RoutineError(_("In a routine, fwupdmgr can only 'refresh' or 'update' (found '{cmd}').")
                               .format(cmd=" ".join(words)))
        if sub == "update":
            if not any(a == "--assume-yes" or (a.startswith("-") and not a.startswith("--") and "y" in a[1:])
                       for a in args):
                args.append("--assume-yes")
            if "--no-reboot-check" not in args:
                args.append("--no-reboot-check")
            if "--no-unreported-check" not in args:  # else --assume-yes says yes to sending update reports
                args.append("--no-unreported-check")
    return Step(words[0], args, cond=cond, strict=True)  # '&&' as in a shell: only exit code 0 goes on


def _has_non_interactive(args):
    for a in args:
        if not a.startswith("-"):
            return False  # the subcommand: global options come before it
        if a == "--non-interactive" or (not a.startswith("--") and "n" in a[1:]):
            return True
    return False


def kind_title(kind):
    """The name of a kind of step: the same in the routine editor and in the routine band of Updates."""
    return {
        "refresh": _("Refresh the repositories"),
        "download": _("Download the updates without installing"),
        "dup": _("Update the system (dup)"),
        "update": _("Update the packages (update)"),
        "flatpak": _("Update the Flatpak apps"),
        "firmware": _("Update the firmware"),
        "restart-check": _("Check what must be restarted"),
        "purge-kernels": _("Remove old kernels"),
        "custom": _("Own command"),
    }[kind]


def describe(step):
    """A short human title for a routine step (the names of the routine editor; downloading, shorter)."""
    a = step.args
    if step.program == "flatpak":
        if "update" in a:
            return kind_title("flatpak")
        return _("Flatpak")
    if step.program == "fwupdmgr":
        if "update" in a:
            return kind_title("firmware")
        if "refresh" in a:
            return _("Look for new firmware")
        return "fwupdmgr"
    sub = next((x for x in a if not x.startswith("-")), "")
    if sub in ("ref", "refresh"):
        return kind_title("refresh")
    if sub in ("dup", "dist-upgrade"):
        if is_download_only(step):
            return _("Download the updates")
        return kind_title("dup")
    if sub in ("up", "update"):
        return kind_title("update")
    if sub == "patch":
        return _("Install patches")
    if sub == "ps":
        return kind_title("restart-check")
    if sub == "purge-kernels":
        return kind_title("purge-kernels")
    return "zypper " + sub


# ------------------------------------------------------------------ this time in parts (little disk space)
_DOWNLOADING = {"dup", "dist-upgrade", "up", "update", "patch"}


def _sub_args(step):
    """(subcommand, the arguments after it) of a zypper step, or ("", [])."""
    if step.program != "zypper":
        return "", []
    a = step.args
    for i, x in enumerate(a):
        if not x.startswith("-"):
            return x, a[i + 1:]
    return "", []


def is_download_only(step):
    """True for a step like 'zypper dup -d' (also -dl, --download-only, --download only): it downloads the
    update and installs nothing."""
    sub, rest = _sub_args(step)
    if sub not in _DOWNLOADING:
        return False
    for i, x in enumerate(rest):
        if x in ("--download-only", "--download=only") or (x == "--download" and rest[i + 1:i + 2] == ["only"]):
            return True
        if x.startswith("-") and not x.startswith("--") and "d" in x[1:]:
            return True
    return False


def split_for_parts(steps):
    """The routine cut around its system update, for a run in parts (little disk space): dict(pre, dup, post,
    join). pre: the steps before the first one that downloads or updates (the refresh); dup: its last 'zypper
    dup' that installs; post: the steps after that one; join: how the update joined pre ('always' for ';',
    'success' for '&&'). The download-only steps and other updates between them are
    left out: the update in parts does their work. None when the routine has no 'zypper dup' that installs.
    The saved routine does not change."""
    first = next((i for i, st in enumerate(steps) if _sub_args(st)[0] in _DOWNLOADING), None)
    last = next((i for i in range(len(steps) - 1, -1, -1)
                 if _sub_args(steps[i])[0] in ("dup", "dist-upgrade") and not is_download_only(steps[i])), None)
    if first is None or last is None:
        return None
    return dict(pre=list(steps[:first]), dup=steps[last], post=list(steps[last + 1:]), join=steps[first].cond)


def dup_arguments(step):
    """(the dup's simulation arguments, licenses): a routine's dup step as 'dist-upgrade' with its own options,
    without -n, -v, -l, -y and the download options (the simulation adds --xmlout -n … --dry-run)."""
    glob, sub_rest, seen_sub = [], [], False
    licenses = False
    skip = False
    for x in step.args:
        if skip:
            skip = False
            continue
        if not seen_sub:
            if not x.startswith("-"):
                seen_sub = True
                continue
            if x in ("-n", "--non-interactive", "-v", "--verbose", "-q", "--quiet") or (
                    not x.startswith("--") and set(x[1:]) <= set("nvq")):
                continue
            glob.append(x)
            continue
        if x in ("-l", "--auto-agree-with-licenses"):
            licenses = True
        elif x in ("-y", "--no-confirm", "--download-only", "--download-in-advance") or x.startswith("--download="):
            continue
        elif x == "--download":
            skip = True
        elif x.startswith("-") and not x.startswith("--") and set(x[1:]) <= set("dly"):
            licenses = licenses or "l" in x
        else:
            sub_rest.append(x)
    return [*glob, "dist-upgrade", *sub_rest], licenses


def steps_text(steps):
    """Steps written back as one line, the way a routine is written ('a ; b && c')."""
    parts = []
    for i, st in enumerate(steps):
        if i:
            parts.append("&&" if st.cond == "success" else ";")
        parts.append(st.display())
    return " ".join(parts)


# ------------------------------------------------------------------ items (the graphical editor)

KINDS = ("refresh", "download", "dup", "update", "flatpak", "firmware", "restart-check", "purge-kernels", "custom")

# option id → (how it is written, True if it is a global zypper option: it goes before the subcommand)
OPTIONS = {
    "verbose": ("-v", True),
    "force": ("-f", False),
    "licenses": ("-l", False),
    "no-recommends": ("--no-recommends", False),
    "vendor-change": ("--allow-vendor-change", False),
    "no-refresh": ("--no-refresh", True),
}

# the options of each kind, in the order they are shown
KIND_OPTIONS = {
    "refresh": ("verbose", "force"),
    "download": ("verbose", "licenses", "no-recommends", "vendor-change"),
    "dup": ("verbose", "licenses", "no-recommends", "vendor-change", "no-refresh"),
    "update": ("verbose", "licenses", "no-recommends"),
    "flatpak": (),
    "firmware": (),
    "restart-check": (),
    "purge-kernels": (),
    "custom": (),
}

# what a new step of each kind starts with (the usual routine explains everything)
DEFAULT_OPTIONS = {
    "refresh": ("verbose",),
    "download": ("verbose", "licenses"),
    "dup": ("verbose",),
    "update": ("verbose",),
}

_SUBCOMMANDS = {"ref": "refresh", "refresh": "refresh", "dup": "dup", "dist-upgrade": "dup", "up": "update",
                "update": "update", "ps": "ps", "purge-kernels": "purge-kernels"}

# Spellings → option id. None: -n and its aliases, which every zypper step gets anyway. Each entry is
# (long or single spellings, letters that can be joined like -dl).
_GLOBAL_FLAGS = ({"-v": "verbose", "--verbose": "verbose", "-n": None, "--non-interactive": None,
                  "--no-refresh": "no-refresh"},
                 {"v": "verbose", "n": None})
_LICENSES = {"-l": "licenses", "--auto-agree-with-licenses": "licenses"}
_NO_CONFIRM = {"-y": None, "--no-confirm": None}
_COMMAND_FLAGS = {
    "refresh": ({"-f": "force", "--force": "force"}, {"f": "force"}),
    "dup": ({"-d": "download-only", "--download-only": "download-only", "--download=only": "download-only",
             "--no-recommends": "no-recommends", "--allow-vendor-change": "vendor-change",
             **_LICENSES, **_NO_CONFIRM},
            {"d": "download-only", "l": "licenses", "y": None}),
    "update": ({"--no-recommends": "no-recommends", **_LICENSES, **_NO_CONFIRM}, {"l": "licenses", "y": None}),
    "ps": ({"-s": "short", "--short": "short"}, {"s": "short"}),
    "purge-kernels": ({}, {}),
}
_GLOBAL_OPTIONS = {"refresh": {"verbose"}, "download": {"verbose"}, "dup": {"verbose", "no-refresh"},
                   "update": {"verbose"}, "ps": set(), "purge-kernels": set()}


def _flag_ids(arg, flags):
    """The option ids an argument stands for ([] for none), or None if it is not one we know."""
    spellings, letters = flags
    if arg in spellings:
        return [spellings[arg]]
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and all(c in letters for c in arg[1:]):
        return [letters[c] for c in arg[1:]]
    return None


def _zypper_kind(args):
    """(kind, opts) for a zypper command the editor knows, or None."""
    i, glob = 0, []
    while i < len(args) and args[i].startswith("-"):
        ids = _flag_ids(args[i], _GLOBAL_FLAGS)
        if ids is None:
            return None
        glob += ids
        i += 1
    if i == len(args) or args[i] not in _SUBCOMMANDS:
        return None
    sub = _SUBCOMMANDS[args[i]]
    rest, cmd = args[i + 1:], []
    j = 0
    while j < len(rest):
        a = rest[j]
        if sub == "dup" and a == "--download" and j + 1 < len(rest) and rest[j + 1] == "only":
            cmd.append("download-only")
            j += 2
            continue
        ids = _flag_ids(a, _COMMAND_FLAGS[sub])
        if ids is None:
            return None  # an option we do not show, or a package or repository name
        cmd += ids
        j += 1
    glob = [x for x in glob if x]
    cmd = [x for x in cmd if x]
    if len(set(glob + cmd)) != len(glob + cmd):
        return None  # written twice (-vv means "even more"): keep it as it is
    opts = set(glob) | set(cmd)
    if sub == "ps":
        return ("restart-check", set()) if opts == {"short"} else None
    if sub == "purge-kernels":
        return ("purge-kernels", set()) if not opts else None
    kind = sub
    if sub == "dup" and "download-only" in opts:
        kind = "download"
        opts.discard("download-only")
    if not set(glob) <= _GLOBAL_OPTIONS[kind] or not opts <= set(KIND_OPTIONS[kind]):
        return None
    return kind, opts


def _known_kind(words):
    prog, args = words[0], words[1:]
    if prog == "zypper":
        return _zypper_kind(args)
    if prog in ("flatpak", "fwupdmgr"):
        allowed = ({"-y", "--assumeyes"} if prog == "flatpak" else
                   {"-y", "--assume-yes", "--no-reboot-check", "--no-unreported-check"})
        flags = [a for a in args if a.startswith("-")]
        if ([a for a in args if not a.startswith("-")] == ["update"] and set(flags) <= allowed
                and len(set(flags)) == len(flags)):
            return ("flatpak" if prog == "flatpak" else "firmware"), set()
    return None


def _item(words, cond):
    raw = shlex.join(words)
    cmd = words[1:] if words[0] == "sudo" else words
    found = _known_kind(cmd) if cmd else None
    if found is None:
        return {"kind": "custom", "opts": set(), "cond": cond, "raw": raw}
    return {"kind": found[0], "opts": found[1], "cond": cond, "raw": raw}


def to_items(text):
    """The steps of a routine, for the editor. Raises RoutineError only when the text cannot be split into
    commands (an open quotation mark, a single '&'…); a command that is not allowed becomes a "custom" item,
    and item_error() says what is wrong with it. An empty text gives []."""
    items = [_item(words, cond) for words, cond in _split(text)]
    if items:
        items[0]["cond"] = "always"
    return items


def new_item(kind, cond="success"):
    item = {"kind": kind, "opts": set(DEFAULT_OPTIONS.get(kind, ())), "cond": cond, "raw": ""}
    if kind != "custom":
        item["raw"] = item_command(item)
    return item


def item_command(item):
    """One step as it is written in a terminal (without the -n that every zypper step gets)."""
    kind = item.get("kind")
    if kind not in KINDS or kind == "custom":
        return (item.get("raw") or "").strip()
    if kind == "flatpak":
        return "flatpak update -y"
    if kind == "firmware":
        return "fwupdmgr update"
    if kind == "restart-check":
        return "zypper ps -s"
    if kind == "purge-kernels":
        return "zypper purge-kernels"
    opts = [o for o in KIND_OPTIONS[kind] if o in (item.get("opts") or ())]
    words = ["zypper"] + [OPTIONS[o][0] for o in opts if OPTIONS[o][1]]
    words.append({"refresh": "ref", "download": "dup", "dup": "dup", "update": "up"}[kind])
    if kind == "download":
        words.append("-d")
    words += [OPTIONS[o][0] for o in opts if not OPTIONS[o][1]]
    return " ".join(words)


def from_items(items):
    """The routine as one line. Steps with an empty command are left out."""
    parts = []
    for it in items:
        cmd = item_command(it)
        if not cmd:
            continue
        if parts:
            parts.append("&&" if it.get("cond") == "success" else ";")
        parts.append(cmd)
    return " ".join(parts)


def item_error(item):
    """What is wrong with one step (a message for the user), or None."""
    cmd = item_command(item)
    if not cmd:
        return _("Write the command.")
    try:
        commands = _split(cmd)
        if not commands:
            return _("Write the command.")
        if len(commands) != 1:
            return _("Write only one command here. Add another step for the next one.")
        _make_step(*commands[0])
    except RoutineError as e:
        return str(e)
    return None
