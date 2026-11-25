# SPDX-License-Identifier: GPL-3.0-or-later
"""What only one package needed: a first look at what 'zypper remove --clean-deps' would take away too.

It reads the rpm database as the user (one level: the packages this one requires or recommends that nothing
else installed requires or recommends). zypper's simulation gives the exact list.
"""

import os
import subprocess

ENV = dict(os.environ, LC_ALL="C")


def _rpm(*args):
    p = subprocess.run(["rpm", *args], capture_output=True, text=True, env=ENV, timeout=120)
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def _caps(*args):
    return [c.split(" ")[0] for c in _rpm(*args) if not c.startswith(("rpmlib(", "/"))]


def size_of(name):
    out = _rpm("-q", "--qf", "%{SIZE}\\n", name)
    return int(out[0]) if out and out[0].isdigit() else 0


def only_needed_by(name):
    """[(package, installed size)] of the packages that only `name` requires or recommends."""
    caps = _caps("-q", "--requires", name) + _caps("-q", "--recommends", name)
    if not caps:
        return []
    providers = set()
    for line in _rpm("-q", "--whatprovides", "--qf", "%{NAME}\\n", *caps):
        if " " not in line:  # "no package provides …"
            providers.add(line)
    providers.discard(name)
    out = []
    for dep in sorted(providers):
        provides = _caps("-q", "--provides", dep)
        if not provides:
            continue
        users = set()
        for flag in ("--whatrequires", "--whatrecommends"):
            users.update(x for x in _rpm("-q", flag, "--qf", "%{NAME}\\n", *provides) if " " not in x)
        users -= {name, dep}
        if not users:
            out.append((dep, size_of(dep)))
    return out
