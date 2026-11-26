# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge packages: programs made for Fedora that ask for things openSUSE has under other names.

A program packaged for Fedora asks for "fontpackages-filesystem", "gtk3" or "v4l2loopback"; openSUSE has the
same things, called "filesystem", "libgtk-3-0" or "kmod(v4l2loopback.ko)". zypper only compares names, so it
says the requirement cannot be met. A bridge package is an empty package that tells zypper "this Fedora name
is here: it is that openSUSE package": it Provides the Fedora name and Requires the openSUSE one.

analyse(info) looks at what a downloaded .rpm asks for (read as the normal user, without changing anything):
what this computer has, what the enabled repositories have (zypper search, read-only) and, for the rest, the
openSUSE equivalent, found in this order:

1. a file path: the same file somewhere else (/usr/sbin is /usr/bin on Tumbleweed) and who owns it;
2. a "…-filesystem" package (it only owns folders): the packages that own, on this computer, the folders
   where the program puts its files;
3. a table of common Fedora names (FEDORA_NAMES), each one checked on Tumbleweed;
4. a name with the processor written in it, "foo(x86-64)": the plain name "foo".

Every equivalent is checked here: the openSUSE package is installed, or an enabled repository has it.

build_bridge(plan, folder) writes the package (rpmwrite, no rpmbuild): zypp-desk-bridge-<program>, version 1,
release N. Each Fedora name is provided with the version of the openSUSE package it stands for ("fontpackages-
filesystem = 84.87"), never without one: an unversioned provide would also meet "fontpackages-filesystem >= 99" of
a later program, and zypper would never pull the real package or say that what is here is too old. Its description says which program it is for and which names it translates, so nothing else has to
be stored: installed_bridges() and check_bridges() read it back from rpm.
"""

import os
import re
import subprocess

from . import rpmwrite
from .backend import ENV_C
from .i18n import _, ngettext
from .localrpm import SENSE_EQUAL, SENSE_GREATER, SENSE_LESS, SENSE_RPMLIB, op_text, satisfies

PREFIX = "zypp-desk-bridge-"
VENDOR = "Zypp Desk"
URL = "https://github.com/jordirelats/zypp-desk"
TIMEOUT = 180

# Fedora (and Red Hat) names → openSUSE Tumbleweed. Each openSUSE name was checked with
# 'zypper search --match-exact' on Tumbleweed (26/11/2026); every one is checked again on the computer before
# it is used. Names openSUSE already provides itself (gtk3: libgtk-3-0 provides it) never reach this table.
FEDORA_NAMES = {
    "alsa-lib": "libasound2",
    "cairo": "libcairo2",
    "cups-libs": "libcups2",
    "dbus-libs": "libdbus-1-3",
    "expat": "libexpat1",
    "fontpackages-filesystem": "filesystem",
    "fuse-libs": "libfuse2",
    "glib2": "libglib-2_0-0",
    "gtk3": "libgtk-3-0",
    "libX11": "libX11-6",
    "libXScrnSaver": "libXss1",
    "libXcomposite": "libXcomposite1",
    "libXdamage": "libXdamage1",
    "libXext": "libXext6",
    "libXfixes": "libXfixes3",
    "libXrandr": "libXrandr2",
    "libappindicator-gtk3": "libappindicator3-1",
    "libdrm": "libdrm2",
    "libnotify": "libnotify4",
    "libsecret": "libsecret-1-0",
    "libstdc++": "libstdc++6",
    "libuuid": "libuuid1",
    "libxcb": "libxcb1",
    "libxkbcommon": "libxkbcommon0",
    "mesa-libgbm": "libgbm1",
    "nss": "mozilla-nss",
    "openssl-libs": "libopenssl3",
    "pango": "libpango-1_0-0",
    "redhat-lsb-core": "lsb-release",
    "v4l2loopback": "kmod(v4l2loopback.ko)",
    "zlib": "libz1",
}

# how Fedora writes the processor in a name: "libX11(x86-64)"
ARCH_QUALIFIER = {"x86_64": "x86-64", "i686": "x86-32", "i586": "x86-32", "aarch64": "aarch-64",
                  "ppc64le": "ppc-64", "s390x": "s390-64", "riscv64": "riscv-64"}
_QUALIFIED_RE = re.compile(r"^(.+)\(([a-z0-9]+-(?:32|64))\)$")
_CAP_RE = re.compile(r"^(\S+)(?:\s+(<=|>=|=|<|>)\s+(\S+))?$")
_OPS = {"=": SENSE_EQUAL, ">=": SENSE_GREATER | SENSE_EQUAL, "<=": SENSE_LESS | SENSE_EQUAL, ">": SENSE_GREATER,
        "<": SENSE_LESS}
_MAP_LINE_RE = re.compile(r"^\s+(\S+) -> (.+)$")
_PROGRAM_LINE_RE = re.compile(r"^Program: (\S+)\s*$")


# ------------------------------------------------------------------ small helpers

def _rpm(args, timeout=TIMEOUT):
    return subprocess.run(["rpm", *args], capture_output=True, text=True, env=ENV_C, errors="replace",
                          timeout=timeout)


def req_text(name, flags, ver):
    """'foo >= 1.2' as rpm writes a requirement."""
    op = op_text(flags)
    return f"{name} {op} {ver}" if op and ver else name


def _without_release(ver):
    epoch, sep, rest = ver.rpartition(":")
    return epoch + sep + rest.split("-")[0]


def bridge_name(program):
    return PREFIX + program


def system_of(info):
    """What the package was made for, from its release (.fc44, .el9) and its distribution or vendor:
    ('fedora', 'Fedora 44'), ('rhel', 'Enterprise Linux 9'), or ('', '') when it does not say."""
    rel = info.get("release") or ""
    text = " ".join(str(info.get(k) or "") for k in ("distribution", "vendor"))
    m = re.search(r"\.fc(\d+)", rel)
    if m:
        return "fedora", f"Fedora {m.group(1)}"
    if "fedora" in text.lower():
        return "fedora", "Fedora"
    m = re.search(r"\.el(\d+)", rel)
    if m:
        return "rhel", f"Enterprise Linux {m.group(1)}"
    if re.search(r"red hat|centos|alma|rocky", text, re.I):
        return "rhel", "Red Hat Enterprise Linux"
    return "", ""


def file_list(path):
    """Every path the .rpm file installs (folders too)."""
    p = subprocess.run(["rpm", "-qp", "--nosignature", "--nodigest", "--qf", "[%{FILENAMES}\n]", "--", path],
                       capture_output=True, env=ENV_C, timeout=TIMEOUT)
    if p.returncode != 0:
        return []
    return [ln for ln in p.stdout.decode("utf-8", "replace").splitlines() if ln.startswith("/")]


# ------------------------------------------------------------------ who provides what

def rpm_providers(names):
    """{capability: [(installed package, provide flags, provide version)]}: one rpm call for every name (no
    file paths: see path_owners)."""
    names = sorted({n for n in names if n and not n.startswith("/")})
    if not names:
        return {}
    p = _rpm(["-q", "--whatprovides", "--qf", "[%{=NAME}\t%{PROVIDENAME}\t%{PROVIDEFLAGS}\t%{PROVIDEVERSION}\n]",
              "--", *names])
    want, out = set(names), {}
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 4 and f[1] in want:
            try:
                flags = int(f[2])
            except ValueError:
                flags = 0
            if (f[0], flags, f[3]) not in out.get(f[1], []):
                out.setdefault(f[1], []).append((f[0], flags, f[3]))
    return out


def path_owners(path):
    """The installed packages that own (or provide) a file path."""
    p = _rpm(["-q", "--whatprovides", "--qf", "%{NAME}\n", "--", path])
    if p.returncode != 0:
        return []
    return sorted({ln.strip() for ln in p.stdout.splitlines() if ln.strip() and " " not in ln.strip()})


def zypper_providers(caps):
    """{capability: [dict(name, status, edition, repo, installed, op, version)]} of the packages that provide
    each capability, installed or in an enabled repository: 'zypper search --provides --match-exact --verbose'
    (read-only, as the normal user, one call). File paths need a call of their own: a path among the terms
    turns the whole search into a search of file lists."""
    caps = sorted(set(c for c in caps if c))
    if not caps:
        return {}
    try:
        p = subprocess.run(["zypper", "-n", "--no-refresh", "search", "--provides", "--match-exact", "--verbose",
                            "--", *caps], capture_output=True, text=True, env=ENV_C, errors="replace",
                           timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    want, out, row = set(caps), {}, None
    for line in p.stdout.splitlines():
        m = re.match(r"^\s+(\w+):\s+(.*)$", line)  # what matched, under its row: "    provides: gtk3 = 3.24"
        if not m:
            cols = [c.strip() for c in line.split("|")]
            row = None
            if len(cols) >= 6 and cols[1] and cols[1] != "Name" and cols[2] == "package":
                row = {"name": cols[1], "status": cols[0], "edition": cols[3], "arch": cols[4], "repo": cols[5],
                       "installed": cols[0].startswith("i")}
            continue
        if not row or m.group(1) not in ("provides", "filelist"):
            continue
        cm = _CAP_RE.match(m.group(2).strip())
        if not cm or cm.group(1) not in want:
            continue
        entry = dict(row, op=cm.group(2) or "", version=cm.group(3) or "")
        if entry not in out.setdefault(cm.group(1), []):
            out[cm.group(1)].append(entry)
    return out


def _real(pkg):
    return not pkg.startswith(PREFIX)


def _zypper_meets(entry, flags, ver):
    return satisfies(_OPS.get(entry["op"], 0), entry["version"], flags, ver)


# ------------------------------------------------------------------ installed bridges

_QF_BRIDGE = ("@@\t%{NAME}\t%{VERSION}\t%{RELEASE}\t%{DESCRIPTION:json}\n"
              "[P\t%{PROVIDENAME}\t%{PROVIDEFLAGS}\t%{PROVIDEVERSION}\n]"
              "[R\t%{REQUIRENAME}\t%{REQUIREFLAGS}\t%{REQUIREVERSION}\n]")


def _parse_description(desc):
    """(program, {Fedora name: [openSUSE names]}) from the description a bridge was written with."""
    program, maps = "", {}
    for line in (desc or "").splitlines():
        m = _PROGRAM_LINE_RE.match(line)
        if m:
            program = m.group(1)
            continue
        m = _MAP_LINE_RE.match(line)
        if m:
            maps[m.group(1)] = [t.strip() for t in m.group(2).split(",") if t.strip()]
    return program, maps


def installed_bridges():
    """The bridge packages installed now: [dict(name, version, release, evr, program, provides [(name, flags,
    version)], requires [(name, flags, version)], maps {Fedora name: [openSUSE names]})]. Read from rpm."""
    import json
    p = _rpm(["-qa", "--qf", _QF_BRIDGE, PREFIX + "*"])
    out, cur = [], None
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if f[0] == "@@" and len(f) >= 5:
            try:
                desc = json.loads("\t".join(f[4:]))
            except ValueError:
                desc = ""
            program, maps = _parse_description(desc)
            cur = {"name": f[1], "version": f[2], "release": f[3], "evr": f"{f[2]}-{f[3]}",
                   "program": program or f[1][len(PREFIX):], "provides": [], "requires": [], "maps": maps,
                   "description": desc}
            out.append(cur)
        elif cur is not None and f[0] in ("P", "R") and len(f) == 4:
            try:
                flags = int(f[2])
            except ValueError:
                flags = 0
            if f[0] == "P":
                if f[1] != cur["name"]:
                    cur["provides"].append((f[1], flags, f[3]))
            elif not f[1].startswith("rpmlib(") and not flags & SENSE_RPMLIB:
                cur["requires"].append((f[1], flags, f[3]))
    for b in out:
        for name, _f, _v in b["provides"]:
            b["maps"].setdefault(name, [r[0] for r in b["requires"]])
    return out


def bridges_of(program):
    """The installed bridges made for this program."""
    return [b for b in installed_bridges() if b["program"] == program]


def _releases_in(folder, name):
    """The releases of this bridge whose files are in a folder (so that a new one never takes an old name)."""
    out = []
    try:
        entries = os.listdir(os.path.expanduser(folder)) if folder else []
    except OSError:
        return out
    rx = re.compile(rf"^{re.escape(name)}-1-(\d+)\.noarch\.rpm$")
    for e in entries:
        m = rx.match(e)
        if m:
            out.append(int(m.group(1)))
    return out


# ------------------------------------------------------------------ the analysis of a file

def _anchor_dirs(files):
    """The folders where the package puts its files and that it does not make itself."""
    own = set(files)
    out = set()
    for f in files:
        d = os.path.dirname(f)
        while d in own:
            d = os.path.dirname(d)
        if d and d != "/":
            out.add(d)
    return sorted(out)


def _official(pkg):
    """Whether an installed package was built by openSUSE (not a program from the web)."""
    p = _rpm(["-q", "--qf", "%{DISTURL}\n", "--", pkg])
    return p.returncode == 0 and p.stdout.startswith("obs://build.opensuse.org/openSUSE:")


def _dir_owners(files):
    """{owner: [folders]}: who owns, on this computer, the folders where the files go. A folder of many
    packages counts as filesystem's when filesystem is one of them, else a '…-filesystem' package's, else the
    one owner built by openSUSE; anything else (a folder of many programs, one owned by a program from the web)
    tells nothing."""
    owners = {}
    for d in _anchor_dirs(files):
        if not os.path.isdir(d):
            continue
        who = path_owners(d)
        if not who:
            continue
        if "filesystem" in who:
            pick = "filesystem"
        else:
            fs = [w for w in who if w.endswith("-filesystem") and _real(w)]
            if fs:
                pick = fs[0]
            elif len(who) == 1 and _real(who[0]) and _official(who[0]):
                pick = who[0]
            else:
                continue
        owners.setdefault(pick, []).append(d)
    return dict(sorted(owners.items(), key=lambda kv: (kv[0] != "filesystem", kv[0])))


def _same_place(path):
    """Where a file path really is on this computer, when it is not that path: /usr/sbin/x is /usr/bin/x when
    /usr/sbin is a link to /usr/bin. [] when it is the same."""
    out = []
    try:
        real_dir = os.path.realpath(os.path.dirname(path))
        cand = os.path.join(real_dir, os.path.basename(path))
        if cand != path:
            out.append(cand)
        if os.path.lexists(path):
            real = os.path.realpath(path)
            if real != path and real not in out:
                out.append(real)
    except OSError:
        pass
    return out


def _provide_version(flags, ver, target_ver):
    """The version the bridge gives a Fedora name: always the version of the openSUSE package it stands for
    (without its release), also when the program asks for no version. An unversioned provide would meet any
    versioned requirement of a later program ("nss >= 99"), and zypper would never pull the real package or
    say it is too old (zypper review 0.5, #6). None when that version does not meet what the program asks for,
    or nobody knows it."""
    if not target_ver:
        return None
    tv = _without_release(target_ver)
    if not ver or not flags & (SENSE_LESS | SENSE_GREATER | SENSE_EQUAL):
        return tv
    if ":" in ver and ":" not in tv:  # Fedora writes an epoch openSUSE may not have
        tv = ver.split(":", 1)[0] + ":" + tv
    return tv if satisfies(SENSE_EQUAL, tv, flags, ver) else None


def installed_evr(names):
    """{package: 'epoch:version-release' of its newest installed edition}, one rpm call (packages that are not
    installed are left out)."""
    from .backend import vercmp
    names = sorted({n for n in names if n and not n.startswith("/")})
    if not names:
        return {}
    p = _rpm(["-q", "--qf", "%{NAME}\t%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\n", "--", *names])
    out = {}
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 2 and f[0] in names and f[1]:  # "package x is not installed" has no tab
            if f[0] not in out or vercmp(f[1], out[f[0]]) > 0:
                out[f[0]] = f[1]
    return out


def capability_version(cap):
    """The version an installed package gives a capability ('3.24.43-1.1' for libgtk-3-0): the version written
    with the provide, else the version of the package that provides it. '' when nothing installed provides it."""
    who = [(pkg, pv) for pkg, _pf, pv in rpm_providers([cap]).get(cap, []) if _real(pkg)]
    if not who:
        return ""
    pkg, pv = who[0]
    return pv or installed_evr([pkg]).get(pkg, "")


def requirements_of(info):
    """What the file asks for, without rpm's own features, what it brings itself and rich dependencies
    ('(a or b)', which zypper solves itself)."""
    own = set(info.get("provides_list") or [])
    out, seen = [], set()
    for name, flags, ver in info.get("requires") or []:
        if (not name or name.startswith("rpmlib(") or flags & SENSE_RPMLIB or name in own
                or name.startswith("(")):
            continue
        key = (name, flags & 0x0E, ver)
        if key not in seen:
            seen.add(key)
            out.append((name, flags, ver))
    return out


def analyse(info, folder=""):
    """What a downloaded .rpm (read by localrpm.read_rpm) asks for that openSUSE has under another name.

    Read-only: rpm and zypper as the normal user, without the network. folder: the folder of programs from
    the web (the bridge's next release never takes the name of a file already there).

    Returns dict:
      program, evr, system (('fedora', 'Fedora 44') …), bridge_name, bridge (the installed bridge of this
      program, or None), release (the release a new bridge gets),
      available: [requirement texts] that the enabled repositories have (zypper installs them),
      kept: [mapping] met today by the program's bridge, still asked for,
      mapped: [mapping] that need a bridge: dict(name, flags, version, text, targets [dict(name, installed,
              repo, version)], why, provide_version),
      unmapped: [(requirement text, reason)] that openSUSE has under no name Zypp Desk knows,
      needs_bridge: True when a new bridge (or a new release of it) makes every requirement met,
      file: name, version, release, summary and path of the file (for the question).
    """
    program = info.get("name") or ""
    plan = {"program": program, "evr": info.get("evr", ""), "system": system_of(info),
            "bridge_name": bridge_name(program), "bridge": None, "release": 1, "available": [], "kept": [],
            "mapped": [], "unmapped": [], "needs_bridge": False,
            "file": {k: info.get(k) or "" for k in ("name", "version", "release", "summary", "path")}}
    reqs = requirements_of(info)
    if not reqs or not program:
        return plan
    plain = [r for r in reqs if not r[0].startswith("/")]
    paths = [r for r in reqs if r[0].startswith("/")]
    arch = os.uname().machine
    qual = ARCH_QUALIFIER.get(arch)

    def candidates(name):
        """The openSUSE names to try for a Fedora name, with how they were found."""
        out = []
        if name.endswith("-filesystem"):
            out.append(("dirs", None))
        m = _QUALIFIED_RE.match(name)
        bare = m.group(1) if m and m.group(2) == qual else None
        if name in FEDORA_NAMES:
            out.append(("table", FEDORA_NAMES[name]))
        if bare:
            out.append(("arch", bare))
            if bare in FEDORA_NAMES:
                out.append(("table", FEDORA_NAMES[bare]))
        return out

    cand_of = {r[0]: candidates(r[0]) for r in plain}
    cand_names = {c for cs in cand_of.values() for kind, c in cs if c}
    inst = rpm_providers([r[0] for r in plain] + list(cand_names))

    # 1. what this computer has (a requirement met only by a bridge counts apart: it is kept)
    todo = []
    bridges = None
    for name, flags, ver in plain:
        who = [pkg for pkg, pf, pv in inst.get(name, []) if satisfies(pf, pv, flags, ver)]
        if any(_real(w) for w in who):
            continue
        if who:
            if bridges is None:
                bridges = {b["name"]: b for b in installed_bridges()}
            b = bridges.get(next(w for w in who if not _real(w)))
            if b is not None:
                targets = b["maps"].get(name) or [r[0] for r in b["requires"]]
                pv = next((v for n, _f, v in b["provides"] if n == name), "")
                if not pv and targets:
                    # a bridge made before 0.5.1 provides it without a version: its next release gives it the
                    # version of the openSUSE package (#6); it meets this requirement, which asks for none
                    pv = _provide_version(0, "", capability_version(targets[0])) or ""
                plan["kept"].append({"name": name, "flags": flags, "version": ver,
                                     "text": req_text(name, flags, ver),
                                     "targets": [{"name": t, "installed": True, "repo": "", "version": ""}
                                                 for t in targets],
                                     "why": _("Your bridge {bridge} already says so.").format(bridge=b["name"]),
                                     "provide_version": pv, "bridge": b["name"]})
                continue
        todo.append((name, flags, ver))
    path_todo, path_cands = [], {}
    for name, flags, ver in paths:
        owners = path_owners(name)
        if any(_real(o) for o in owners):
            continue
        path_todo.append((name, flags, ver))
        path_cands[name] = _same_place(name)

    # 2. what the enabled repositories have (and the candidates found by name)
    if todo:
        repo = zypper_providers([r[0] for r in todo] + [c for r in todo for kind, c in cand_of[r[0]] if c])
    else:
        repo = {}
    repo_paths = zypper_providers([r[0] for r in path_todo] + [c for cs in path_cands.values() for c in cs]) \
        if path_todo else {}

    evr_of = {}  # installed package -> its version, asked once

    def version_of(pkg):
        if pkg not in evr_of:
            evr_of.update(installed_evr([pkg]))
            evr_of.setdefault(pkg, "")
        return evr_of[pkg]

    def best_target(cap):
        """The openSUSE package that gives cap on this computer: installed first, else from a repository. Its
        version: the one written with the provide, else the package's own (kmod(…) provides carry none)."""
        for pkg, _pf, pv in inst.get(cap, []):
            if _real(pkg):
                return {"name": cap, "installed": True, "repo": "", "version": pv or version_of(pkg), "package": pkg}
        for e in repo.get(cap, []):
            if _real(e["name"]):
                return {"name": cap, "installed": e["installed"], "repo": e["repo"],
                        "version": e["version"] or e["edition"], "package": e["name"]}
        return None

    files = None
    for name, flags, ver in todo:
        text = req_text(name, flags, ver)
        if any(_real(e["name"]) and _zypper_meets(e, flags, ver) for e in repo.get(name, [])):
            plan["available"].append(text)
            continue
        found, why = None, ""
        for kind, cand in cand_of[name]:
            if kind == "dirs":
                if files is None:
                    where = info.get("private") or info.get("path")  # the copy that was checked, when there is one
                    files = file_list(where) if where else []
                owners = _dir_owners(files)
                if owners:
                    found = [{"name": o, "installed": True, "repo": "", "version": version_of(o), "package": o}
                             for o in owners]
                    parts = [ngettext("{dirs} belongs to {package}", "{dirs} belong to {package}", len(ds))
                             .format(dirs=", ".join(ds), package=o) for o, ds in owners.items()]
                    why = _("The folders where it puts its files are already there: {list}.").format(
                        list="; ".join(parts))
                    break
                continue
            t = best_target(cand)
            if t is None:
                continue
            found = [t]
            if kind == "arch":
                why = _("{name} is {bare} for this computer's processor; openSUSE does not write the processor "
                        "in the name.").format(name=name, bare=cand)
            else:
                why = _("The same thing: {fedora} in Fedora is {opensuse} in openSUSE.").format(
                    fedora=re.sub(r"\(.*\)$", "", name), opensuse=cand)
            break
        if not found:
            plan["unmapped"].append((text, _("openSUSE has nothing under this name, and Zypp Desk knows no other "
                                             "name for it.")))
            continue
        pv = _provide_version(flags, ver, found[0].get("version"))
        if pv is None:
            if found[0].get("version"):
                why = _("openSUSE has {package} {version}, which is not what it asks for.").format(
                    package=found[0]["name"], version=found[0]["version"])
            else:  # the bridge must say a version (#6): without one it would meet any later requirement
                why = _("Zypp Desk cannot tell which version of {package} openSUSE has.").format(
                    package=found[0]["name"])
            plan["unmapped"].append((text, why))
            continue
        plan["mapped"].append({"name": name, "flags": flags, "version": ver, "text": text, "targets": found,
                               "why": why, "provide_version": pv})

    for name, flags, ver in path_todo:
        text = req_text(name, flags, ver)
        if any(_real(e["name"]) for e in repo_paths.get(name, [])):
            plan["available"].append(text)
            continue
        found = None
        for cand in path_cands.get(name, []):
            owners = [o for o in path_owners(cand) if _real(o)]
            if owners:
                found = ({"name": owners[0], "installed": True, "repo": "", "version": "", "package": owners[0]},
                         _("{path} is the same file as {real}, which belongs to {package}.").format(
                             path=name, real=cand, package=owners[0]))
                break
            avail = [e for e in repo_paths.get(cand, []) if _real(e["name"])]
            if avail:
                e = avail[0]
                found = ({"name": e["name"], "installed": e["installed"], "repo": e["repo"], "version": "",
                          "package": e["name"]},
                         _("{path} is the same place as {real}, which {package} installs.").format(
                             path=name, real=cand, package=e["name"]))
                break
        if not found:
            plan["unmapped"].append((text, _("No package of openSUSE has this file.")))
            continue
        plan["mapped"].append({"name": name, "flags": flags, "version": ver, "text": text, "targets": [found[0]],
                               "why": found[1], "provide_version": ""})

    # the bridge this program has now, and the release a new one gets
    if bridges is None:
        bridges = {b["name"]: b for b in installed_bridges()}
    mine = bridges.get(plan["bridge_name"]) or next(
        (b for b in bridges.values() if b["program"] == program), None)
    plan["bridge"] = mine
    releases = _releases_in(folder, plan["bridge_name"])
    if mine:
        try:
            releases.append(int(mine["release"].split(".")[0]))
        except ValueError:
            pass
    plan["release"] = max(releases, default=0) + 1
    plan["needs_bridge"] = bool(plan["mapped"]) and not plan["unmapped"] and bool(
        rpmwrite.NAME_RE.fullmatch(plan["bridge_name"]))
    return plan


def analyse_file(path, folder=""):
    """analyse() for a file that was not read yet (a download of Programs from the web)."""
    from .localrpm import read_rpm
    return analyse(read_rpm(path), folder)


# ------------------------------------------------------------------ writing the bridge

def _system_names(plan):
    """('Fedora 44', 'Fedora'): what the package was made for, and how its names are called."""
    kind, full = plan["system"]
    short = {"fedora": "Fedora", "rhel": "Red Hat"}.get(kind, "")
    return full or "another system", short or "its own"


def description(plan, mappings):
    """The description of the bridge package: what it is for, in words, and lines that Zypp Desk reads back
    ('  fedora-name -> opensuse-name, …' and 'Program: name'). In English, like any package's metadata."""
    import textwrap
    system, short = _system_names(plan)
    prog = plan["program"]
    head = textwrap.fill(f"Zypp Desk made this empty package on this computer so that {prog}, made for {system}, "
                         f"can be installed on openSUSE. {prog} asks for these things by their {short} names; "
                         "openSUSE has them under other names, and this package tells zypper so:", 76,
                         break_on_hyphens=False, break_long_words=False)
    tail = textwrap.fill("It holds no files. Zypp Desk lists it in Programs from the web, under Bridge packages, "
                         "and says when it is no longer needed.", 76, break_on_hyphens=False,
                         break_long_words=False)
    lines = [head, ""]
    for m in mappings:
        lines.append(f"  {m['name']} -> {', '.join(t['name'] for t in m['targets'])}")
    lines += ["", tail, "", f"Program: {prog}"]
    return "\n".join(lines)


def bridge_contents(plan):
    """(provides, requires, mappings) of the bridge a plan asks for: the kept mappings and the new ones."""
    mappings = list(plan["kept"]) + list(plan["mapped"])
    provides, requires = [], []
    for m in mappings:
        pv = m.get("provide_version") or ""
        dep = (m["name"], SENSE_EQUAL if pv else 0, pv)
        if dep not in provides:
            provides.append(dep)
        for t in m["targets"]:
            r = (t["name"], 0, "")
            if r not in requires:
                requires.append(r)
    return provides, requires, mappings


def planned_file(plan, folder):
    return os.path.join(folder, rpmwrite.file_name(plan["bridge_name"], "1", str(plan["release"])))


def build_bridge(plan, folder, web_folder=""):
    """Write the bridge package of a plan into folder (as the normal user, nothing is installed). Returns
    dict(path, name, evr, sha256). web_folder: where Apply will move it; a release whose file is already there
    is never used again (the plan may have been made before the folder was known)."""
    from .websources import sha256_of
    taken = _releases_in(web_folder, plan["bridge_name"])
    if taken and plan["release"] <= max(taken):
        plan["release"] = max(taken) + 1
    provides, requires, mappings = bridge_contents(plan)
    short = _system_names(plan)[1]
    path = rpmwrite.write(
        folder, name=plan["bridge_name"], version="1", release=str(plan["release"]),
        summary=f"Bridge for {plan['program']}: {short} names that openSUSE has under other names",
        description=description(plan, mappings), provides=provides, requires=requires, vendor=VENDOR, url=URL)
    return {"path": path, "name": plan["bridge_name"], "evr": f"1-{plan['release']}", "sha256": sha256_of(path)}


# ------------------------------------------------------------------ is each bridge still needed?

def check_bridges(bridges=None):
    """Whether each installed bridge is still needed. Returns the bridges (installed_bridges()) with:
      program_installed, asked {Fedora name: (flags, version) the program asks for now},
      real {Fedora name: [dict(package, installed, repo)]} (providers other than bridges, meeting the version),
      install: [packages to install when the bridge goes] (real providers that are only in a repository),
      state: 'orphan' (the program is not installed), 'unused' (it no longer asks for any of the names),
             'replaced' (openSUSE now provides every name it asks for, installed), 'replaceable' (… some only
             in a repository: they are installed when the bridge goes), 'needed'.
    Read-only: rpm and one zypper search."""
    bridges = installed_bridges() if bridges is None else bridges
    if not bridges:
        return []
    programs = sorted({b["program"] for b in bridges})
    p = _rpm(["-q", "--qf", "@@\t%{NAME}\n[%{=NAME}\t%{REQUIRENAME}\t%{REQUIREFLAGS}\t%{REQUIREVERSION}\n]", "--",
              *programs])
    asks, installed = {}, set()
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 2 and f[0] == "@@":
            installed.add(f[1])
        elif len(f) == 4:
            try:
                flags = int(f[2])
            except ValueError:
                flags = 0
            asks.setdefault(f[0], {})[f[1]] = (flags, f[3])
    names = sorted({n for b in bridges for n, _f, _v in b["provides"]})
    inst = rpm_providers(names)
    inst_paths = {n: [o for o in path_owners(n) if _real(o)] for n in names if n.startswith("/")}
    need_repo = [n for n in names if not n.startswith("/") and not any(_real(pk) for pk, _f, _v in inst.get(n, []))]
    repo = zypper_providers(need_repo)
    repo_paths = zypper_providers([n for n in names if n.startswith("/") and not inst_paths.get(n)])
    for b in bridges:
        prog = b["program"]
        b["program_installed"] = prog in installed
        mine = asks.get(prog, {})
        b["asked"] = {n: mine[n] for n, _f, _v in b["provides"] if n in mine}
        b["real"], b["install"] = {}, []
        for n, (flags, ver) in b["asked"].items():
            found = []
            if n.startswith("/"):
                found += [{"package": o, "installed": True, "repo": ""} for o in inst_paths.get(n, [])]
                found += [{"package": e["name"], "installed": e["installed"], "repo": e["repo"]}
                          for e in repo_paths.get(n, []) if _real(e["name"])]
            else:
                found += [{"package": pk, "installed": True, "repo": ""} for pk, pf, pv in inst.get(n, [])
                          if _real(pk) and satisfies(pf, pv, flags, ver)]
                found += [{"package": e["name"], "installed": e["installed"], "repo": e["repo"]}
                          for e in repo.get(n, []) if _real(e["name"]) and _zypper_meets(e, flags, ver)]
            if found:
                b["real"][n] = found
        if not b["program_installed"]:
            b["state"] = "orphan"
        elif not b["asked"]:
            b["state"] = "unused"
        elif all(n in b["real"] for n in b["asked"]):
            if all(any(x["installed"] for x in b["real"][n]) for n in b["asked"]):
                b["state"] = "replaced"
            else:
                b["state"] = "replaceable"
                for n in b["asked"]:
                    if not any(x["installed"] for x in b["real"][n]):
                        pkg = b["real"][n][0]["package"]
                        if pkg not in b["install"]:
                            b["install"].append(pkg)
        else:
            b["state"] = "needed"
    return bridges


def users_of(b):
    """The installed packages, other than its program, that need what a bridge provides: removing the bridge
    would remove them too."""
    names = [n for n, _f, _v in b["provides"]]
    if not names:
        return []
    p = _rpm(["-q", "--whatrequires", "--qf", "%{NAME}\n", "--", *names])
    return sorted({ln.strip() for ln in p.stdout.splitlines()
                   if ln.strip() and " " not in ln.strip() and ln.strip() not in (b["program"], b["name"])})
