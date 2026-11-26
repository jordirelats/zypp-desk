# SPDX-License-Identifier: GPL-3.0-or-later
"""What only one package needed: a first look at what 'zypper remove --clean-deps' would take away too.

It reads the rpm database as the user (one level: the packages this one requires or recommends that nothing
else installed requires or recommends). --clean-deps only removes packages that were installed as dependencies
(zypper lists them in /var/lib/zypp/AutoInstalled), never the ones you installed yourself, so only those count.
zypper's simulation gives the exact list.

Packages that leave together count as one: a program and its bridge package (bridge.py) do not keep each
other's dependencies.
"""

import os
import subprocess

ENV = dict(os.environ, LC_ALL="C")
AUTO_INSTALLED = "/var/lib/zypp/AutoInstalled"


def _rpm(*args):
    p = subprocess.run(["rpm", *args], capture_output=True, text=True, env=ENV, timeout=120)
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def _caps(*args):
    return [c.split(" ")[0] for c in _rpm(*args) if not c.startswith(("rpmlib(", "/"))]


def size_of(name):
    out = _rpm("-q", "--qf", "%{SIZE}\\n", name)
    return int(out[0]) if out and out[0].isdigit() else 0


def auto_installed(path=AUTO_INSTALLED):
    """The packages that zypper installed as dependencies (not asked for by you). Missing file: none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {x.strip() for x in f if x.strip() and not x.lstrip().startswith("#")}
    except OSError:
        return set()


def only_needed_by(name, auto=None, also=()):
    """[(package, installed size)] of the packages that only `name` (and the packages in `also`, which leave
    with it) requires or recommends and that were installed as dependencies (auto: those names; default: read
    AutoInstalled)."""
    leaving = {name, *also}
    caps = []
    for pkg in sorted(leaving):
        caps += _caps("-q", "--requires", pkg) + _caps("-q", "--recommends", pkg)
    if not caps:
        return []
    auto = auto_installed() if auto is None else auto
    providers = set()
    for line in _rpm("-q", "--whatprovides", "--qf", "%{NAME}\\n", *caps):
        if " " not in line and line in auto:  # "no package provides …"; installed by you: zypper keeps it
            providers.add(line)
    providers -= leaving
    out = []
    for dep in sorted(providers):
        provides = _caps("-q", "--provides", dep)
        if not provides:
            continue
        users = set()
        for flag in ("--whatrequires", "--whatrecommends"):
            users.update(x for x in _rpm("-q", flag, "--qf", "%{NAME}\\n", *provides) if " " not in x)
        users -= leaving | {dep}
        if not users:
            out.append((dep, size_of(dep)))
    return out
