# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only queries to the flatpak command, run as the user.

Changes (install, uninstall, update) go through the queue and run 'flatpak' as the user too:
for the system installation flatpak asks polkit itself, so the usual password dialog appears.
"""

import os
import re
import shutil
import subprocess

# Names and descriptions in the user's language, but numbers with a dot, so sizes can be read.
ENV = dict(os.environ, LC_NUMERIC="C")
# 'flatpak info' with English field names, which are read by name; the app shows its own labels.
ENV_C = dict(os.environ, LC_ALL="C", LANG="C", LANGUAGE="C")
APP_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}")


def available():
    return shutil.which("flatpak") is not None


def _run(args, timeout=120, env=None):
    return subprocess.run(["flatpak", *args], capture_output=True, text=True, env=env or ENV, timeout=timeout)


def _table(args, cols, timeout=120, names=()):
    """A flatpak table. names go after '--', so a name can never be read as an option."""
    p = _run([*args, "--columns=" + ",".join(cols), *(["--", *names] if names else [])], timeout=timeout)
    rows = []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        vals = line.split("\t")
        rows.append({c: (vals[i].strip() if i < len(vals) else "") for i, c in enumerate(cols)})
    return rows


def parse_size(text):
    """'142.1 MB' (flatpak counts in powers of 1000) → bytes, or None."""
    m = re.search(r"([\d.,]+)\s*([kKMGT]?)i?B\b", text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    power = {"": 0, "k": 1, "K": 1, "M": 2, "G": 3, "T": 4}[m.group(2)]
    return int(value * 1000 ** power)


def installed(runtimes=False):
    kind = "--runtime" if runtimes else "--app"
    rows = _table(["list", kind], ["application", "name", "version", "branch", "origin", "installation", "size"])
    for r in rows:
        r["bytes"] = parse_size(r["size"])
    return rows


def updates():
    """Apps and runtimes with an update waiting, in every installation."""
    return _table(["remote-ls", "--updates"], ["application", "version", "branch", "origin"], timeout=180)


def search(term):
    rows = _table(["search"], ["application", "name", "version", "branch", "remotes", "description"],
                  timeout=180, names=[term])
    # "No matches found" is printed on stdout with exit code 0: keep only real app IDs (reverse DNS)
    return [r for r in rows if APP_ID_RE.fullmatch(r["application"])]


def remotes():
    out = []
    for r in _table(["remotes", "--show-disabled"], ["name", "title", "url", "options"]):
        opts = set(r["options"].split(","))
        r["installation"] = "user" if "user" in opts else "system"
        r["enabled"] = "disabled" not in opts
        out.append(r)
    return out


def _parse_info(text):
    """'flatpak info' / 'remote-info' (in English): the title line and the 'Key: value' fields."""
    title, fields = "", {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][A-Za-z -]*?):\s(.*)$", line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
        elif line.strip() and not fields and not title:
            title = line.strip()
    return {"title": title, "fields": fields}


def eol_of(fields):
    """(message, replacement app) when the app is no longer maintained, else None."""
    if "End-of-life" in fields or "End-of-life-rebase" in fields:
        return fields.get("End-of-life", ""), fields.get("End-of-life-rebase", "")
    return None


def details(app, installation=None):
    """An installed app: dict(title, fields, eol, perms)."""
    inst = [f"--{installation}"] if installation in ("system", "user") else []
    info = _parse_info(_run(["info", *inst, "--", app], env=ENV_C).stdout)
    info["eol"] = eol_of(info["fields"])
    info["perms"] = _run(["info", "--show-permissions", *inst, "--", app]).stdout.strip()
    return info


def remote_details(remote, app):
    """An app in a remote, not installed: dict(title, fields, eol, perms='')."""
    info = _parse_info(_run(["remote-info", "--", remote, app], timeout=180, env=ENV_C).stdout)
    info["eol"] = eol_of(info["fields"])
    info["perms"] = ""
    return info


def eol_apps(apps):
    """{app ID: (message, replacement)} for the installed apps that are no longer maintained.
    apps: rows from installed(). One 'flatpak info' per app: call it in a thread."""
    out = {}
    for r in apps:
        if not APP_ID_RE.fullmatch(r.get("application", "")):
            continue
        p = _run(["info", f"--{r.get('installation') or 'system'}", "--", r["application"]], env=ENV_C, timeout=30)
        eol = eol_of(_parse_info(p.stdout)["fields"])
        if eol:
            out[r["application"]] = eol
    return out
