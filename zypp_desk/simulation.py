# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the XML of a '--dry-run' and show what would change, without changing anything."""

import re
import shlex
import xml.etree.ElementTree as ET

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .i18n import _, ngettext
from .pages.problems import ProblemDialog
from .txqueue import Action

PROMPT_CONTINUE, PROMPT_DEP_RESOLVE, PROMPT_LICENSE = 0, 1, 10

CATEGORIES = [
    ("to-install", _("To install")),
    ("to-upgrade", _("To upgrade")),
    ("to-downgrade", _("To downgrade")),
    ("to-reinstall", _("To reinstall")),
    ("to-change-vendor", _("To change vendor")),
    ("to-change-arch", _("To change architecture")),
    ("to-remove", _("To remove")),
]


def human_size(n):
    """A size in the user's own format (QLocale: '4,4 MiB' in Catalan)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    sign = "-" if n < 0 else ""
    return sign + QLocale().formattedDataSize(int(abs(n)))


def disk_text(value):
    """What a transaction does to the disk, in words: frees space or uses more."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return _("Frees {size} on disk").format(size=human_size(-n))
    return _("Uses {size} more on disk").format(size=human_size(n))


def cancelled(steps):
    """True when the password was refused or cancelled (pkexec 126/127): there is nothing to show."""
    return any(getattr(s, "code", None) in (126, 127) for s in steps)


# Lines of zypper's dependency solver (the dry run runs in English) said in plain words.
_SOLVER = [
    (r"nothing provides '?([^'\s]+)'? needed by the to be installed (\S+)",
     lambda m: _("{pkg} needs {what}, and no repository provides it").format(pkg=m[2], what=m[1])),
    (r"nothing provides '?([^'\s]+)'? needed by the installed (\S+)",
     lambda m: _("the installed {pkg} needs {what}, and no repository provides it").format(pkg=m[2], what=m[1])),
    (r"the to be installed (\S+) conflicts with '?([^'\s]+)'? provided by the installed (\S+)",
     lambda m: _("{new} cannot be installed together with the installed {old}").format(new=m[1], old=m[3])),
    (r"the installed (\S+) conflicts with '?([^'\s]+)'? provided by the to be installed (\S+)",
     lambda m: _("{new} cannot be installed together with the installed {old}").format(new=m[3], old=m[1])),
    (r"(\S+) requires (.+), but this requirement cannot be provided",
     lambda m: _("{pkg} needs {what}, which cannot be installed").format(pkg=m[1], what=m[2])),
    (r"break (\S+) by ignoring some of its dependencies",
     lambda m: _("install {pkg} anyway, ignoring some of its dependencies (it may not work)").format(pkg=m[1])),
    (r"deinstallation of (\S+)", lambda m: _("remove {pkg}").format(pkg=m[1])),
    (r"do not keep (\S+) installed", lambda m: _("remove {pkg}").format(pkg=m[1])),
    (r"do not install (\S+)", lambda m: _("do not install {pkg}").format(pkg=m[1])),
    (r"keep obsolete (\S+)", lambda m: _("keep the old {pkg}").format(pkg=m[1])),
    (r"downgrade of (\S+) to (\S+)", lambda m: _("go back from {old} to {new}").format(old=m[1], new=m[2])),
]
_PROMPT_LINE = re.compile(r"Choose from above solutions|\[\d+(/\d+)*/[a-z?/]+\]\s*\(\w\)", re.I)
_QUESTION = re.compile(_PROMPT_LINE.pattern + r"|Choose the above solution", re.I)


def plain_problem(text):
    """A solver problem with zypper's question removed and its lines said in plain words."""
    out = []
    for line in text.splitlines():
        if _PROMPT_LINE.search(line):
            continue  # zypper's own question (which solution?) cannot be answered here
        s = line.strip()
        for rx, fn in _SOLVER:
            s = re.sub(rx, fn, s)
        m = re.match(r"^Problem:\s*(\d+):\s*(.*)$", s)
        if m:
            s = _("Problem {n}: {text}").format(n=m[1], text=m[2])
        elif (m := re.match(r"^Problem:\s*(.*)$", s)):
            s = _("Problem: {text}").format(text=m[1])
        m = re.match(r"^Solution\s+(\d+):\s*(.*)$", s)
        if m:
            s = "  " + _("Possible solution {n}: {text}").format(n=m[1], text=m[2])
        elif s and line[:1].isspace() and out:
            s = "    " + s
        if s:
            out.append(s)
    return "\n".join(out)


# ------------------------------------------------------------------ solver problems, one by one
# A dry run runs in English (LC_ALL=C), so these are libzypp's own sentences (zypp/solver/detail/SATResolver.cc
# and ProblemSolution*.cc; the msgids are in /usr/share/locale/*/LC_MESSAGES/zypp.mo), plus a few of libsolv's.
# zypper lists them as:
#   Problem: 1: the installed vlc-codecs-3.0.21-1699.10.pm.x86_64 requires 'vlc = 3.0.21', but this …
#   deleted providers: vlc-3.0.21-2.1.x86_64
#    Solution 1: deinstallation of vlc-codecs-3.0.21-1699.10.pm.x86_64
#    Solution 2: keep obsolete vlc-3.0.21-2.1.x86_64
# With -n zypper answers 'cancel' to the first problem, so only that one comes with its solutions; when there are
# several, the others are only listed (one line each) before it.

ARCHES = frozenset((
    "noarch", "x86_64", "x86_64_v2", "x86_64_v3", "x86_64_v4", "i386", "i486", "i586", "i686", "athlon", "pentium4",
    "aarch64", "aarch64_ilp32", "armv5tel", "armv6hl", "armv6l", "armv7hl", "armv7l", "armv8l", "ppc", "ppc64",
    "ppc64le", "ppc64p7", "s390", "s390x", "riscv64", "loongarch64", "ia64", "sparc64", "mips64", "src", "nosrc"))
_KINDS = ("pattern", "patch", "product", "srcpackage", "application")
_PROBLEM_HEAD = re.compile(r"^Problem(?::\s*(\d+)\s*:|\s+(\d+)\s*:|:)\s*(.*)$")
_SOLUTION_HEAD = re.compile(r"^Solution\s+(\d+)\s*:\s*(.*)$")
_DETAIL = re.compile(r"^(deleted providers:|not installable providers:|uninstallable providers:|"
                     r"Have you enabled all the required repositories\?|- )")
_CAP = re.compile(r"^(\S+?)\s*(<=|>=|=|<|>)\s*(\S+)$")
_LIB = re.compile(r"^([^\s()]+\.so[^\s()]*)(?:\(([^)]*)\))?(?:\(64bit\))?$")
_STATE = r"(installed|to be installed)"


def split_solvable(text):
    """'vlc-codecs-3.0.21-1699.10.pm.x86_64' → dict(name='vlc-codecs', edition='3.0.21-1699.10.pm',
    version='3.0.21', arch='x86_64', kind='package').

    Names contain dashes; the edition is the last two dash-separated parts (version-release, the version maybe
    with an epoch 'E:'), and the arch follows the last dot. A bare name ('vlc', as in 'keep vlc') has no edition.
    'pattern:kde-…' gives kind 'pattern' and name 'kde'; a repository alias at the end ('…x86_64[packman]', as
    libzypp writes packages that are not installed) goes to 'repo'."""
    s = text.strip().strip("'\"")
    repo = ""
    m = re.match(r"^(.+)\[([^\[\]]*)\]$", s)
    if m:
        s, repo = m[1], m[2]
    kind = "package"
    head, colon, rest = s.partition(":")
    if colon and head in _KINDS and rest:
        kind, s = head, rest
    arch = ""
    base, dot, tail = s.rpartition(".")
    if dot and base and tail in ARCHES:
        s, arch = base, tail
    name, edition = s, ""
    parts = s.rsplit("-", 2)
    if len(parts) == 3 and all(parts) and (
            arch or (re.match(r"^(\d+:)?\d", parts[1]) and re.search(r"\d", parts[2]))):
        name, edition = parts[0], f"{parts[1]}-{parts[2]}"
    version = edition.split("-")[0].split(":", 1)[-1] if edition else ""
    return dict(name=name, edition=edition, version=version, arch=arch, kind=kind, repo=repo)


def _who(text):
    """A package said in plain words: 'vlc-codecs 3.0.21'."""
    sv = split_solvable(text)
    name = sv["name"] if sv["kind"] == "package" else f"{sv['kind']}:{sv['name']}"
    who = f"{name} {sv['version']}" if sv["version"] else name
    return _("{pkg} from {repo}").format(pkg=who, repo=sv["repo"]) if sv["repo"] else who


def _whos(items):
    return ", ".join(_who(x) for x in items if x.strip())


def plain_dep(dep):
    """What a package needs, in plain words: 'vlc = 3.0.21' → 'vlc 3.0.21'; a library → 'the library libfoo.so.1'."""
    d = dep.strip().strip("'")
    m = _CAP.match(d)
    if m:
        forms = {"=": _("{name} {version}"), ">=": _("{name} {version} or newer"),
                 "<=": _("{name} {version} or older"), ">": _("{name} newer than {version}"),
                 "<": _("{name} older than {version}")}
        return forms[m[2]].format(name=m[1], version=m[3])
    m = _LIB.match(d)
    if m:
        lib = m[1] + (f" ({m[2]})" if m[2] else "")
        return _("the library {lib}").format(lib=lib)
    if d.startswith("/"):
        return _("the file {path}").format(path=d)
    return d


def _dep_name(dep):
    """The package name a capability asks for ('vlc = 3.0.21' → 'vlc')."""
    d = dep.strip().strip("'")
    m = _CAP.match(d)
    return m[1] if m else d


def _target(sv):
    """The name the queue (and zypper install/remove) uses for a solvable."""
    return sv["name"] if sv["kind"] == "package" else f"{sv['kind']}:{sv['name']}"


def _explain_problem(head, details):
    """A solver problem (its first line and its detail lines) in plain words."""
    h = re.sub(r"\s+", " ", head.strip())
    providers = {"deleted": [], "uninstallable": []}
    extra = []
    key = None
    details = [d.strip() for d in details if d.strip()]
    for d in details:
        m = re.match(r"^(deleted|not installable|uninstallable) providers:\s*(.*)$", d)
        if m:
            key = "deleted" if m[1] == "deleted" else "uninstallable"
            providers[key] += m[2].split()
        elif key and re.match(r"^\S+$", d):
            providers[key].append(d)  # zypper puts each further provider on its own, indented line
        elif d.startswith("Have you enabled all the required repositories"):
            extra.append(_("Maybe a repository that it needs is missing or disabled."))
            key = None
        else:
            key = None
    out = None
    m = re.match(rf"^the {_STATE} (\S+) requires '?(.+?)'?, but this requirement cannot be provided$", h) or \
        re.match(r"^(?:package )?()(\S+) requires (.+?), but (?:this requirement cannot be provided|"
                 r"none of the providers can be installed)$", h)
    if m:
        pkg, dep = _who(m[2]), plain_dep(m[3])
        if providers["deleted"] or providers["uninstallable"]:  # the detail lines below say why
            out = {"to be installed": _("{pkg}, which would be installed, needs {dep}."),
                   "installed": _("The installed {pkg} needs {dep}.")}.get(m[1], _("{pkg} needs {dep}."))
        else:
            out = {"to be installed": _("{pkg}, which would be installed, needs {dep}, and nothing that can be "
                                        "installed with it provides it."),
                   "installed": _("The installed {pkg} needs {dep}, and after this change nothing would provide "
                                  "it.")}.get(m[1], _("{pkg} needs {dep}, and nothing that can be installed "
                                                     "provides it."))
        out = out.format(pkg=pkg, dep=dep)
        if providers["deleted"]:
            out += " " + _("Today it comes from {list}, which this change replaces or removes, so the two cannot "
                           "go together.").format(list=_whos(providers["deleted"]))
        if providers["uninstallable"]:
            out += " " + _("{list} would provide it, but cannot be installed together with the rest of the "
                           "changes.").format(
                list=_whos(providers["uninstallable"]))
    elif (m := re.match(rf"^nothing provides '?(.+?)'? needed by the {_STATE} (\S+)$", h) or
          re.match(r"^nothing provides (.+?) needed by (?:package )?()(\S+)$", h)):
        pkg, dep = _who(m[3]), plain_dep(m[1])
        if m[2] == "installed":
            out = _("The installed {pkg} needs {dep}, and no enabled repository has it any more.").format(
                pkg=pkg, dep=dep)
        else:
            out = _("{pkg} needs {dep}, and no enabled repository has it. Maybe a repository is missing, or the "
                    "package is broken.").format(pkg=pkg, dep=dep)
    elif (m := re.match(r"^nothing provides the requested '(.+)'$", h)):
        out = _("You asked for {dep}, but no enabled repository has it.").format(dep=plain_dep(m[1]))
    elif (m := re.match(r"^the requested package (\S+) does not exist$", h)):
        out = _("The package {pkg} that you asked for is not in any enabled repository.").format(pkg=m[1])
    elif (m := re.match(rf"^the {_STATE} (\S+) conflicts with '(.+)' provided by itself$", h)):
        out = _("{pkg} conflicts with itself ({dep}): the package is broken.").format(pkg=_who(m[2]),
                                                                                      dep=plain_dep(m[3]))
    elif (m := re.match(rf"^the {_STATE} (\S+) (conflicts with|obsoletes) '(.+)' provided by the {_STATE} (\S+)$",
                        h)):
        state = {"installed": _("installed"), "to be installed": _("would be installed")}
        out = _("These two cannot be on the system together: {a} ({a_state}) and {b} ({b_state}).").format(
            a=_who(m[2]), a_state=state[m[1]], b=_who(m[6]), b_state=state[m[5]])
        if m[3] == "conflicts with":
            out += " " + _("{a} conflicts with {dep}, which {b} provides.").format(
                a=split_solvable(m[2])["name"], dep=plain_dep(m[4]), b=split_solvable(m[6])["name"])
        else:
            out += " " + _("{a} replaces {dep}, which {b} provides.").format(
                a=split_solvable(m[2])["name"], dep=plain_dep(m[4]), b=split_solvable(m[6])["name"])
    elif (m := re.match(r"^(?:package )?(\S+) (conflicts with|obsoletes) (.+) provided by (\S+)$", h)):
        out = _("These two cannot be on the system together: {a} and {b}.").format(a=_who(m[1]), b=_who(m[4]))
    elif (m := re.match(r"^cannot install both (\S+) and (\S+)$", h)):
        out = _("{a} and {b} cannot be installed at the same time.").format(a=_who(m[1]), b=_who(m[2]))
    elif (m := re.match(rf"^problem with the {_STATE} (\S+)$", h)):
        out = (_("The installed {pkg} can neither stay as it is nor be updated.") if m[1] == "installed"
               else _("There is a problem with {pkg}, which would be installed.")).format(pkg=_who(m[2]))
    elif (m := re.match(r"^the installed (\S+) does not belong to a distupgrade repository and must be replaced$",
                        h)):
        out = _("The installed {pkg} is not in any of your repositories any more, so the upgrade has to replace "
                "it.").format(pkg=_who(m[1]))
    elif (m := re.match(r"^(?:the to be installed |package )(\S+) does not belong to a distupgrade repository$", h)):
        out = _("{pkg} comes from a repository that this upgrade does not use.").format(pkg=_who(m[1]))
    elif (m := re.match(r"^(?:the (?:installed |to be installed )|package )(\S+) has inferior architecture$", h)):
        out = _("{pkg} is built for an older type of processor than the other versions available.").format(
            pkg=_who(m[1]))
    elif (m := re.match(r"^(?:the (?:installed |to be installed )|package )?(\S+) is not installable$", h)):
        out = _("{pkg} cannot be installed on this computer.").format(pkg=_who(m[1]))
    elif (m := re.match(r"^'(.+)' is provided by the system and cannot be erased$", h)):
        out = _("{dep} is part of the system and cannot be removed.").format(dep=plain_dep(m[1]))
    elif h == "conflicting requests":
        out = _("The changes asked for contradict each other.")
    elif h == "some dependency problem":
        out = _("There is a dependency problem.")
    elif h == "unsupported request":
        out = _("zypper cannot do what was asked.")
    elif h == "This request will break your system!":
        out = _("This change would break your system.")
    if out is None:  # a sentence we do not know: zypper's own words are better than a guess
        out = "\n".join([head.strip()] + details)
        extra = []
    return " ".join([out] + extra)


def _action_command(actions):
    """The zypper commands that the actions add to the queue, for display: ['zypper addlock vlc']."""
    groups = {}
    for a in actions:
        if isinstance(a, dict):
            continue
        q = shlex.quote
        if a.kind in ("lock", "unlock"):
            typ = a.opts.get("type", "package")
            key = ("zypper addlock" if a.kind == "lock" else "zypper removelock") + (
                f" -t {typ}" if typ != "package" else "")
        elif a.kind == "remove":
            key = "zypper remove"
        elif a.kind == "install":
            key = "zypper install" + (" --oldpackage" if a.opts.get("oldpackage") else "")
        else:
            key = f"{a.kind}"
            q = str
        groups.setdefault(key, []).append(q(a.target))
    return [f"{k} {' '.join(v)}" for k, v in groups.items()]


def _part(desc):
    """One action of a solution, from zypper's words: dict(text, detail, actions, possible)."""
    d = re.sub(r"\s+", " ", desc.strip())

    def ok(text, detail, *actions, who=""):
        return dict(text=text, detail=detail, actions=list(actions), possible=True, raw=desc.strip(), who=who)

    def no(text, detail=None):
        return dict(text=text, detail=detail or _("This cannot be done safely from here."), actions=[],
                    possible=False, raw=desc.strip(), who="")

    if (m := re.match(r"^(?:deinstallation of|do not keep) (\S+?)(?: installed)?$", d)):
        sv = split_solvable(m[1])
        return ok(_("Remove {name}").format(name=_target(sv)),
                  _("{pkg} is uninstalled. Whatever uses it may lose some functions until you install it again.")
                  .format(pkg=_who(m[1])), Action("remove", _target(sv)), who=_target(sv))
    if (m := re.match(r"^(keep obsolete|do not install most recent version of|do not ask to update) (\S+)$", d)) \
            or (m := re.match(r"^(keep) (\S+) (despite the inferior architecture)$", d)):
        sv = split_solvable(m[2])
        lock = Action("lock", sv["name"], **({} if sv["kind"] == "package" else {"type": sv["kind"]}))
        if m[1].startswith("keep"):
            text = _("Keep {pkg} as it is").format(pkg=_who(m[2]))
        else:
            text = _("Do not update {name}").format(name=_target(sv))
        detail = (_("The rest goes ahead. {name} stays at version {version} and is locked: it will not change "
                    "until you unlock it.").format(name=_target(sv), version=sv["version"]) if sv["version"] else
                  _("The rest goes ahead. {name} stays as it is and is locked: it will not change until you unlock "
                    "it.").format(name=_target(sv)))
        return ok(text, detail, lock, who=_who(m[2]))
    if (m := re.match(r"^do not install (\S+)$", d)) or \
            (m := re.match(r"^do not ask to install a solvable providing (.+)$", d)):
        name = _target(split_solvable(m[1])) if "providing" not in d else _dep_name(m[1])
        return ok(_("Do not install {name}").format(name=name),
                  _("{name} leaves the queue; the rest goes ahead.").format(name=name),
                  dict(kind="discard", target=name, of="install"))
    if (m := re.match(r"^keep (\S+)$", d)) or \
            (m := re.match(r"^do not ask to delete all solvables providing (.+)$", d)):
        name = _target(split_solvable(m[1])) if "providing" not in d else _dep_name(m[1])
        return ok(_("Do not remove {name}").format(name=name),
                  _("{name} stays installed and its removal leaves the queue; the rest goes ahead.").format(name=name),
                  dict(kind="discard", target=name, of="remove"))
    if (m := re.match(r"^remove lock to allow (removal|installation) of (\S+)$", d)):
        sv = split_solvable(m[2])
        unlock = Action("unlock", sv["name"], **({} if sv["kind"] == "package" else {"type": sv["kind"]}))
        detail = (_("{name} is locked. Without the lock, zypper can remove it.") if m[1] == "removal" else
                  _("{name} is locked. Without the lock, zypper can install it.")).format(name=_target(sv))
        return ok(_("Unlock {name}").format(name=_target(sv)), detail, unlock)
    if (m := re.match(r"^downgrade of (\S+) to (\S+)$", d)):
        old, new = split_solvable(m[1]), split_solvable(m[2])
        if new["edition"]:
            return ok(_("Go back to {pkg}").format(pkg=_who(m[2])),
                      _("{name} goes back from version {old} to the older {new}, so that everything fits.").format(
                          name=_target(new), old=old["version"] or "?", new=new["version"]),
                      Action("install", f"{_target(new)}={new['edition']}", oldpackage=True))
    if (m := re.match(r"^install (\S+) \(with vendor change\) (.+?) --> (.+)$", d)) or \
            (m := re.match(r"^install (\S+) from vendor (.+?) replacing (\S+) from vendor (.+)$", d)):
        sv = split_solvable(m[1])
        if len(m.groups()) == 3:
            old_vendor, new_vendor = m[2].strip(), m[3].strip()
        else:
            new_vendor, old_vendor = m[2].strip(), m[4].strip()
        if sv["edition"]:
            return ok(_("Switch {name} to the package from {vendor}").format(name=_target(sv), vendor=new_vendor),
                      _("{pkg} from {vendor} replaces the one from {old_vendor}. This changes the vendor: from now "
                        "on {name} is updated from {vendor}.").format(pkg=_who(m[1]), vendor=new_vendor,
                                                                     old_vendor=old_vendor, name=_target(sv)),
                      Action("install", f"{_target(sv)}={sv['edition']}"))
    if (m := re.match(r"^replacement of (\S+) with (\S+)$", d)):
        new = split_solvable(m[2])
        if new["edition"]:
            return ok(_("Replace {old} with {new}").format(old=_who(m[1]), new=_who(m[2])),
                      _("{new} is installed in place of {old}.").format(new=_who(m[2]), old=_who(m[1])),
                      Action("install", f"{_target(new)}={new['edition']}"))
    if (m := re.match(r"^install (\S+) from excluded repository$", d)):
        sv = split_solvable(m[1])
        if sv["edition"]:
            return ok(_("Install {pkg} anyway").format(pkg=_who(m[1])),
                      _("It comes from a repository that this upgrade leaves out, but it is taken from there "
                        "anyway."), Action("install", f"{_target(sv)}={sv['edition']}", oldpackage=True))
    # the ones that cannot be chosen here: they need options that Zypp Desk does not use on purpose
    if (m := re.match(r"^break (\S+) by ignoring some of its dependencies$", d)):
        return no(_("Install it anyway, ignoring some of the dependencies of {name}").format(
            name=split_solvable(m[1])["name"]),
            _("This cannot be done safely from here: {name} might not work.").format(
                name=split_solvable(m[1])["name"]))
    if (m := re.match(r"^architecture change of (\S+) to (\S+)$", d)):
        return no(_("Change {name} to another architecture").format(name=split_solvable(m[1])["name"]))
    if (m := re.match(r"^install (\S+) despite the inferior architecture$", d)):
        return no(_("Install {pkg}, built for an older type of processor").format(pkg=_who(m[1])))
    if (m := re.match(r"^install (\S+) although it has been retracted$", d)):
        return no(_("Install {pkg}, although its makers withdrew it").format(pkg=_who(m[1])))
    if (m := re.match(r"^install (\S+) although it is blacklisted$", d)):
        return no(_("Install {pkg}, although it is blocked").format(pkg=_who(m[1])))
    if (m := re.match(r"^allow installing the PTF (\S+)$", d)):
        return no(_("Install the special fix (PTF) {pkg}").format(pkg=_who(m[1])))
    if d.startswith("ignore the warning of a broken system"):
        return no(_("Ignore the warning that the system would break"))
    if d == "generally ignore of some dependencies":
        return no(_("Ignore some dependencies"))
    return no(desc.strip(), _("Zypp Desk does not know this solution, so it cannot be chosen here."))


def _solution(number, head, more, raw):
    """A solution: one action, or several after 'Following actions will be done:'."""
    if head.startswith("Following actions will be done"):
        parts = []
        for line in more:
            s = line.strip()
            if not s:
                continue
            if parts and (s.startswith("replacing ") or "-->" in s):
                parts[-1] += "\n" + s  # the second line of a vendor change
            else:
                parts.append(s)
    else:
        parts = ["\n".join([head] + [x.strip() for x in more if x.strip()])]
    subs = [_part(p) for p in parts] or [_part(head)]
    actions = {}  # one action per change: a downgrade that also changes vendor is a single install
    for sub in subs:
        for a in sub["actions"]:
            key = ("discard", a["target"], a["of"]) if isinstance(a, dict) else (a.kind, a.target)
            if key not in actions:
                actions[key] = a
            elif not isinstance(a, dict):
                actions[key].opts.update({k: v for k, v in a.opts.items() if v})
    actions = list(actions.values())
    possible = all(s["possible"] for s in subs)
    if len(subs) == 1:
        text, detail = subs[0]["text"], subs[0]["detail"]
    elif possible and len(actions) == 1:  # several lines for one change (a downgrade that also changes vendor)
        text, detail = subs[0]["text"], " ".join(s["detail"] for s in subs)
    else:
        kinds = {(a["kind"] if isinstance(a, dict) else a.kind) for a in actions}
        if possible and kinds == {"lock"}:
            text = _("Keep {list} as they are").format(list=", ".join(s["who"] for s in subs))
            detail = _("The rest goes ahead. They stay at their versions and are locked: they will not change "
                       "until you unlock them.")
        elif possible and kinds == {"remove"}:
            text = _("Remove {list}").format(list=", ".join(s["who"] for s in subs))
            detail = _("They are uninstalled. Whatever uses them may lose some functions until you install them "
                       "again.")
        else:
            text = "; ".join(s["text"] for s in subs)
            detail = " ".join(s["detail"] for s in subs if s["possible"] == possible)
    return dict(number=number, text=text, detail=detail, raw="\n".join(raw), actions=actions if possible else [],
                commands=_action_command(actions) if possible else [], possible=possible, recommended=False)


def _problem_blocks(text, strict=False):
    """zypper's problem text split into problems: dict(number, head, details, solutions, raw).

    strict: the text is a series of messages, so after a problem only its known detail lines count."""
    out, cur, sol, in_detail = [], None, None, False
    for line in text.splitlines():
        if _QUESTION.search(line):
            cur, sol = None, None  # zypper's question ends the problem
            continue
        s = line.strip()
        m = _PROBLEM_HEAD.match(s)
        if m:
            cur = dict(number=int(m[1] or m[2] or 0) or None, head=m[3].strip(), details=[], solutions=[],
                       raw=[line.rstrip()])
            out.append(cur)
            sol, in_detail = None, False
            continue
        if cur is None:
            continue
        m = _SOLUTION_HEAD.match(s)
        if m:
            sol = dict(number=int(m[1]), head=m[2].strip(), more=[], raw=[line.rstrip()])
            cur["solutions"].append(sol)
            cur["raw"].append(line.rstrip())
            continue
        if not s:
            cur["raw"].append("")
            continue
        if sol is not None:
            sol["more"].append(line)
            sol["raw"].append(line.rstrip())
            cur["raw"].append(line.rstrip())
            continue
        if strict and not (_DETAIL.match(s) or (in_detail and line[:1].isspace() and " " not in s)):
            cur, in_detail = None, False  # another message: the problem is over
            continue
        cur["details"].append(s)
        cur["raw"].append(line.rstrip())
        in_detail = True
    for p in out:
        while p["raw"] and not p["raw"][-1]:
            p["raw"].pop()
    return out


def problem_items(texts, message_text="", source=""):
    """The solver problems of a dry run with the solutions zypper offers, in plain words.

    texts: problem blocks with their solutions (the <description> of zypper's prompt); message_text: zypper's
    messages, where it lists every problem (one line each) when there are several."""
    found = {}
    order = []
    for strict, block in [(True, message_text)] + [(False, t) for t in texts]:
        for p in _problem_blocks(block or "", strict=strict):
            key = p["number"] or re.sub(r"\s+", " ", p["head"])
            if key in found and not p["solutions"] and found[key]["solutions"]:
                continue
            if key not in found:
                order.append(key)
            found[key] = p
    items = []
    for key in order:
        p = found[key]
        head, details = p["head"], list(p["details"])
        if re.match(r"^Detailed information:?\s*$", head):  # zypper -v: every rule of the problem, one per line
            infos = [d[2:] for d in details if d.startswith("- ")]
            details = [d for d in details if not d.startswith("- ")]
            head = infos[0] if infos else head
        sols = [_solution(s["number"], s["head"], s["more"], s["raw"]) for s in p["solutions"]]
        good = [s for s in sols if s["possible"]]
        best = next((s for s in good if all(
            isinstance(a, dict) or a.kind == "lock" for a in s["actions"])), good[0] if good else None)
        if best is not None:
            best["recommended"] = True
        items.append(dict(number=p["number"], text=_explain_problem(head, details), raw="\n".join(p["raw"]),
                          solutions=sols, source=source))
    return items


def parse_dry_run(text, source=""):
    """Return dict(groups, download, disk, messages, problems, problem_items, ok).

    problem_items: the solver problems, each with the solutions zypper offers in plain words and the queue
    actions that carry them out (see problem_items()). source: what was simulated ('zypper dup'), for the
    window that shows them."""
    m = re.search(r"<stream>.*</stream>", text, re.S)
    res = dict(groups={}, download=None, disk=None, messages=[], problems=[], problem_items=[], ok=False, raw=text)
    if not m:
        res["problems"].append(text.strip()[-3000:] or _("zypper returned no output."))
        return res
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError as e:
        res["problems"].append(str(e))
        return res
    for msg in root.iter("message"):
        kind, t = msg.get("type", "info"), (msg.text or "").strip()
        if not t:
            continue
        res["messages"].append((kind, t))
        if kind == "error":
            res["problems"].append(t)
    # Prompt ids come from 'zypper --promptids'. 0 is the ordinary "Continue?" before committing,
    # which a dry run always shows: it is not a problem.
    blocks = []  # zypper's problem texts with their solutions
    for p in root.iter("prompt"):
        try:
            pid = int(p.get("id", "-1"))
        except ValueError:
            pid = -1
        if pid == PROMPT_CONTINUE:
            continue
        # zypper puts the problem and its numbered solutions in <description>; <text> is only the question
        question = " ".join((x.text or "").strip() for x in p.iter("text")) or (p.text or "").strip()
        description = (p.findtext("description") or "").strip()
        desc = "\n".join(x for x in (description, question) if x)
        opts = [o.get("desc") or o.get("value") or "" for o in p.iter("option")]
        if pid == PROMPT_DEP_RESOLVE:
            blocks.append(description)
            detail = [t for k, t in res["messages"] if re.match(r"^\s*(Problem|Solution|\d+:)", t)]
            res["problems"].append(plain_problem("\n".join(detail + [description]).strip() or desc))
        elif pid == PROMPT_LICENSE:
            res["problems"].append(_("A license must be accepted. Turn on “Accept licenses without asking” in "
                                     "Settings, or install it from a terminal.") + "\n" + desc)
        elif desc:
            res["problems"].append(desc + (("\n  " + "\n  ".join(o for o in opts if o)) if opts else ""))
    for s in root.iter("solverproblem") if hasattr(root, "iter") else []:
        res["problems"].append(ET.tostring(s, encoding="unicode", method="text").strip())
        blocks.append(res["problems"][-1])
    res["problem_items"] = problem_items(blocks, "\n".join(t for k, t in res["messages"]), source)
    summ = next(root.iter("install-summary"), None)
    if summ is not None:
        res["download"] = summ.get("download-size")
        res["disk"] = summ.get("space-usage-diff")
        for child in summ:
            items = []
            for s in child.iter("solvable"):
                items.append(dict(name=s.get("name", ""), edition=s.get("edition", ""),
                                  old=s.get("edition-old", ""), arch=s.get("arch", ""),
                                  repo=s.get("repository", ""), kind=s.get("type", "package"),
                                  vendor=s.get("vendor", ""), old_vendor=s.get("vendor-old", "")))
            if items:
                res["groups"].setdefault(child.tag, []).extend(items)
        res["ok"] = not res["problems"]
    elif not res["problems"]:
        res["ok"] = True  # nothing to do
    return res


def merge_results(results):
    """One result for several dry runs of the same queue (removals and installs run as separate steps)."""
    if len(results) == 1:
        return results[0]
    out = dict(groups={}, download=None, disk=None, messages=[], problems=[], problem_items=[], ok=True,
               raw="\n".join(r["raw"] for r in results))

    def add(a, b):
        try:
            return str(int(a or 0) + int(b or 0))
        except ValueError:
            return a or b
    for r in results:
        for k, items in r["groups"].items():
            out["groups"].setdefault(k, []).extend(items)
        out["messages"] += r["messages"]
        out["problems"] += r["problems"]
        out["problem_items"] += r.get("problem_items", [])
        out["ok"] = out["ok"] and r["ok"]
        if r["download"] is not None:
            out["download"] = add(out["download"], r["download"])
        if r["disk"] is not None:
            out["disk"] = add(out["disk"], r["disk"])
    return out


def self_order(extra_steps, command):
    return command.replace(" && ", "\n")


def summary_lines(result):
    """A plain-text summary of a dry run, for the terminal panel."""
    lines = []
    for key, label in CATEGORIES:
        items = result["groups"].get(key, [])
        if items:
            names = ", ".join(f"{i['name']} {i['edition']}" for i in items[:40])
            more = "" if len(items) <= 40 else " …"
            lines.append(f"{label} ({len(items)}): {names}{more}")
    if result["download"]:
        lines.append(_("Download: {size}").format(size=human_size(result["download"])))
    if result["disk"]:
        lines.append(disk_text(result["disk"]))
    for p in result["problems"]:
        lines.append(_("Problem: {text}").format(text=p))
    if not lines:
        lines.append(_("Nothing to do."))
    return lines


class SimulationDialog(QDialog):
    """What a dry-run says would happen. 'Apply' runs the real command.

    When zypper found a problem and offers solutions, "Choose a solution…" opens ProblemDialog; if a solution is
    chosen, the dialog closes with RESOLVE and self.chosen holds the chosen solutions (see problem_items()).
    problem_label and problem_note are passed to ProblemDialog as apply_label and note."""

    RESOLVE = 2

    def __init__(self, result, command, *, can_apply=True, extra_steps=(), parent=None, problem_label=None,
                 problem_note=None):
        super().__init__(parent)
        self.result = result
        self.chosen = []
        self.problem_label, self.problem_note = problem_label, problem_note
        solvable = [it for it in result.get("problem_items", []) if any(s["possible"] for s in it["solutions"])]
        self.setWindowTitle(_("Simulation"))
        self.resize(900, 640)
        lay = QVBoxLayout(self)

        banner = QFrame(self)
        banner.setObjectName("banner")
        ok = result["ok"]
        color = "39,174,96" if ok else "246,116,0"
        banner.setStyleSheet(f"#banner {{ border: 1px solid rgba({color},0.55); border-radius: 5px;"
                             f" background: rgba({color},0.09); }}")
        bl = QVBoxLayout(banner)
        title = QLabel(_("It can be done. Nothing has been changed.") if ok
                       else _("zypper found a problem. Nothing has been changed."), banner)
        f = title.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        title.setFont(f)
        bl.addWidget(title)
        counts = []
        for key, label in CATEGORIES:
            n = len(result["groups"].get(key, []))
            if n:
                counts.append(f"{label}: {n}")
        if result["download"]:
            counts.append(_("Download: {size}").format(size=human_size(result["download"])))
        if result["disk"]:
            counts.append(disk_text(result["disk"]))
        if not counts and ok:
            counts.append(_("Nothing to do."))
        bl.addWidget(QLabel("  ·  ".join(counts), banner))
        cmd = QLabel(command, banner)
        cmd.setTextFormat(Qt.TextFormat.PlainText)  # package and repository names come from outside
        cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        cmd.setFont(mono)
        cmd.setWordWrap(True)
        bl.addWidget(cmd)
        lay.addWidget(banner)

        if result["problems"]:
            lay.addWidget(QLabel("<b>" + _("Problems") + "</b>", self))
            box = QPlainTextEdit("\n\n".join(result["problems"]), self)
            box.setReadOnly(True)
            box.setMaximumHeight(200)
            lay.addWidget(box)
            hint = QLabel(_("Choose one of the solutions that zypper offers, or change the queue yourself (for "
                            "example, lock or remove a package) and simulate again.") if solvable else
                          _("Change the queue (for example, lock or remove a package) and simulate again."), self)
            hint.setWordWrap(True)
            lay.addWidget(hint)

        tree = QTreeWidget(self)
        tree.setHeaderLabels([_("Package"), _("Version"), _("Repository")])
        tree.setRootIsDecorated(True)
        for key, label in CATEGORIES + [(k, k) for k in result["groups"] if k not in dict(CATEGORIES)]:
            items = result["groups"].get(key)
            if not items:
                continue
            top = QTreeWidgetItem(tree, [f"{label} ({len(items)})"])
            ft = top.font(0)
            ft.setBold(True)
            top.setFont(0, ft)
            for it in items:
                ver = f"{it['old']} → {it['edition']}" if it["old"] else it["edition"]
                if it.get("old_vendor") and it.get("vendor"):
                    ver += f"  ({it['old_vendor']} → {it['vendor']})"
                repo = _("installed") if it["repo"] == "@System" else it["repo"]
                QTreeWidgetItem(top, [it["name"], ver, repo])
            top.setExpanded(True)
        tree.resizeColumnToContents(0)
        tree.resizeColumnToContents(1)
        lay.addWidget(tree, 1)

        if extra_steps:
            lbl = QLabel(_("These commands are also in the queue and cannot be simulated. Everything runs in "
                           "this order:") + "\n" + self_order(extra_steps, command), self)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setWordWrap(True)
            lay.addWidget(lbl)

        buttons = QDialogButtonBox(self)
        self.apply_btn = buttons.addButton(_("Apply"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_btn.setEnabled(can_apply and ok)
        buttons.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        self.resolve_btn = None
        if solvable:
            self.resolve_btn = buttons.addButton(_("Choose a solution…"), QDialogButtonBox.ButtonRole.ActionRole)
            self.resolve_btn.setProperty("primary", True)
            self.resolve_btn.style().unpolish(self.resolve_btn)  # the button box has already styled it
            self.resolve_btn.style().polish(self.resolve_btn)
            self.resolve_btn.setDefault(True)
            self.resolve_btn.clicked.connect(self.choose_solution)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def choose_solution(self):
        dlg = ProblemDialog(self.result.get("problem_items", []), parent=self, apply_label=self.problem_label,
                            note=self.problem_note)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.chosen = dlg.chosen()
            self.done(self.RESOLVE)
