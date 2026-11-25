# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only queries to the flatpak command, run as the user.

Changes (install, uninstall, update) go through the queue and run 'flatpak' as the user too:
for the system installation flatpak asks polkit itself, so the usual password dialog appears.
"""

import os
import shutil
import subprocess

ENV = dict(os.environ)


def available():
    return shutil.which("flatpak") is not None


def _run(args, timeout=120):
    p = subprocess.run(["flatpak", *args], capture_output=True, text=True, env=ENV, timeout=timeout)
    return p


def _table(args, cols, timeout=120):
    p = _run([*args, "--columns=" + ",".join(cols)], timeout=timeout)
    rows = []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        vals = line.split("\t")
        rows.append({c: (vals[i].strip() if i < len(vals) else "") for i, c in enumerate(cols)})
    return rows


def installed(runtimes=False):
    kind = "--runtime" if runtimes else "--app"
    return _table(["list", kind], ["application", "name", "version", "branch", "origin", "installation", "size"])


def updates():
    """Apps and runtimes with an update waiting, in every installation."""
    return _table(["remote-ls", "--updates"], ["application", "version", "branch", "origin"], timeout=180)


def search(term):
    return _table(["search", term], ["application", "name", "version", "branch", "remotes", "description"],
                  timeout=180)


def remotes():
    out = []
    for r in _table(["remotes", "--show-disabled"], ["name", "title", "url", "options"]):
        opts = set(r["options"].split(","))
        r["installation"] = "user" if "user" in opts else "system"
        r["enabled"] = "disabled" not in opts
        out.append(r)
    return out


def info(app, installation=None):
    args = ["info"] + ([f"--{installation}"] if installation in ("system", "user") else []) + [app]
    text = _run(args).stdout.strip()
    perms = _run(["info", "--show-permissions"] + args[1:]).stdout.strip()
    return text, perms


def remote_info(remote, app):
    return _run(["remote-info", remote, app], timeout=180).stdout.strip()
