# SPDX-License-Identifier: GPL-3.0-or-later
"""What each change made by Zypp Desk did to the installed packages, and how to undo it.

Before a run that may install, remove or update packages, the window takes the list of installed packages (one
'rpm -qa', about half a second); after it, it takes it again. The difference is kept as an "operation" in
~/.local/share/zypp-desk/operations/<time>.json: what was removed, what was installed, and what changed version.
It is the same whether the run ended well, failed half way or was stopped.

Undoing an operation is the opposite, through the queue (and so through its simulation): put back what it
removed (the version the repositories have now), remove what it installed, and put back the version a package
had before when a repository still has it. What cannot be undone (an older version no repository has any more,
often on Tumbleweed) is said before. On a computer with Btrfs and snapper, the History page can also go back to
the snapshot taken before, which undoes everything. No Qt; nothing here needs root.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import backend

KEEP = 50  # operations kept; the oldest go
# zypper subcommands that install, remove or update packages: a run with one of them is recorded
CHANGING = {"install", "in", "remove", "rm", "update", "up", "dist-upgrade", "dup", "patch", "inr",
            "install-new-recommends", "purge-kernels", "source-install", "si", "removeptf", "rmptf"}


def folder():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "zypp-desk" / "operations"


def changes_packages(steps):
    """True when one of these steps may install, remove or update packages for real (not a dry run)."""
    from .runner import zypper_subcommand
    for st in steps:
        if getattr(st, "program", "") != "zypper" or not getattr(st, "root", False):
            continue
        args = list(st.args)
        if "--dry-run" in args or "-D" in args and zypper_subcommand(args) in ("install", "in", "remove", "rm"):
            continue
        if zypper_subcommand(args) in CHANGING:
            return True
    return False


def snapshot():
    """{"name.arch": [name, "epoch:version-release", arch]} of every installed package (gpg-pubkey left out)."""
    try:
        p = subprocess.run(["rpm", "-qa", "--qf", "%{NAME}\\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\\t%{ARCH}\\n"],
                           capture_output=True, text=True, errors="replace", env=backend.ENV_C, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] == "gpg-pubkey":
            continue
        name, evr, arch = parts
        out[f"{name}.{arch}"] = [name, evr, arch]
    return out


RPMDB_FILES = ("/usr/lib/sysimage/rpm/rpmdb.sqlite", "/usr/lib/sysimage/rpm/Packages.db",
               "/var/lib/rpm/rpmdb.sqlite", "/var/lib/rpm/Packages.db")


def rpmdb_stamp():
    """When the rpm database last changed (its file's time), or None: a snapshot taken after it is still true."""
    for f in RPMDB_FILES:
        try:
            return os.stat(f).st_mtime_ns
        except OSError:
            continue
    return None


def plain_evr(evr):
    """'0:1.2-3.1' → '1.2-3.1' (an epoch of 0 is not shown, as zypper does)."""
    return evr[2:] if evr.startswith("0:") else evr


def diff(before, after):
    """dict(removed=[[name, evr, arch]], installed=[...], changed=[[name, old evr, new evr, arch]])."""
    removed, installed, changed = [], [], []
    for key, (name, evr, arch) in before.items():
        now = after.get(key)
        if now is None:
            removed.append([name, evr, arch])
        elif now[1] != evr:
            changed.append([name, evr, now[1], arch])
    for key, (name, evr, arch) in after.items():
        if key not in before:
            installed.append([name, evr, arch])
    for lst in (removed, installed, changed):
        lst.sort(key=lambda x: x[0])
    return dict(removed=removed, installed=installed, changed=changed)


def empty(d):
    return not (d["removed"] or d["installed"] or d["changed"])


def save(title, commands, outcome, d, when=None):
    """Keep an operation; returns it (a dict with its id). outcome: "done", "stopped" or "failed"."""
    when = when or time.time()
    op = dict(id=str(int(when * 1000)), time=when, title=title, commands=list(commands), outcome=outcome, **d)
    f = folder()
    try:
        f.mkdir(parents=True, exist_ok=True)
        tmp = f / f".{op['id']}.tmp"
        tmp.write_text(json.dumps(op, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f / f"{op['id']}.json")
        for old in sorted(f.glob("*.json"))[:-KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return op


def load_all():
    """Every kept operation, the newest first."""
    out = []
    try:
        files = sorted(folder().glob("*.json"), reverse=True)
    except OSError:
        return out
    for f in files:
        try:
            op = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(op, dict) and "id" in op:
            out.append(op)
    return out


def mark_undone(op_id, undo_id):
    f = folder() / f"{op_id}.json"
    try:
        op = json.loads(f.read_text(encoding="utf-8"))
        op["undone_by"] = undo_id
        f.write_text(json.dumps(op, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass


def available_versions(names):
    """{name: {"epoch:version-release" or "version-release", ...}} the enabled repositories have (one zypper
    search, as the user)."""
    names = sorted(set(names))
    if not names:
        return {}
    root, _code = backend.xml(["search", "--details", "--match-exact", "-t", "package", "--", *names])
    out = {}
    if root is None:
        return out
    for s in root.iter("solvable"):
        if s.get("repository") in ("@System", "(System Packages)"):
            continue
        out.setdefault(s.get("name", ""), set()).add(s.get("edition", ""))
    return out


def _same(evr, editions):
    """True when this "epoch:version-release" is among zypper's editions ("version-release" or "e:v-r")."""
    return evr in editions or plain_evr(evr) in editions


def undo_plan(op):
    """What undoing an operation means now, as dict(reinstall=[names], remove=[names], downgrade=[[name, evr]],
    cannot=[[name, why]], nothing_to_do=[names]) — from the installed packages as they are now, so that what
    was already put back or taken away again is not done twice. Runs zypper and rpm: call it in a thread."""
    now = snapshot() or {}
    have = {v[0]: v[1] for v in now.values()}
    names = [r[0] for r in op.get("removed", [])] + [c[0] for c in op.get("changed", [])]
    avail = available_versions(names)
    plan = dict(reinstall=[], remove=[], downgrade=[], cannot=[], nothing_to_do=[])
    for name, evr, _arch in op.get("removed", []):
        if name in have:
            plan["nothing_to_do"].append(name)
        elif name in avail:
            plan["reinstall"].append(name)
        else:
            plan["cannot"].append([name, "gone"])  # no repository has it: a file it came from may still
    for name, evr, _arch in op.get("installed", []):
        if name in have:
            plan["remove"].append(name)
        else:
            plan["nothing_to_do"].append(name)
    for name, old, new, _arch in op.get("changed", []):
        cur = have.get(name)
        if cur is None:
            plan["cannot"].append([name, "removed since"])
        elif cur == old:
            plan["nothing_to_do"].append(name)
        elif _same(old, avail.get(name, ())):
            plan["downgrade"].append([name, plain_evr(old)])
        else:
            plan["cannot"].append([name, "old version gone"])
    return plan


def short_names(names, n=3):
    """'konsole, dolphin, kate and 33 more' made of the first names; the caller translates "and {n} more"."""
    return list(names[:n]), max(0, len(names) - n)


_SAFE = re.compile(r"^[A-Za-z0-9_.+:-]+$")


def safe_name(name):
    """Package names from a file in the user's folder go to zypper as plain arguments (after '--'): still, only
    names that look like package names are used."""
    return bool(_SAFE.match(name or ""))
