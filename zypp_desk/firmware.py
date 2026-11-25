# SPDX-License-Identifier: GPL-3.0-or-later
"""Firmware updates through fwupd: the BIOS, a dock, an SSD, a mouse… (Discover did this before).

Everything here only reads, as the user (`fwupdmgr … --json`). Installing goes through the queue
(`fwupdmgr update DEVICE`), and fwupd itself asks for the password through polkit. The list of what is new
comes from LVFS (fwupd.org); the system timer zypp-desk-refresh keeps it fresh.
"""

import html
import json
import os
import re
import shutil
import subprocess

FWUPDMGR = "fwupdmgr"


def available():
    return shutil.which(FWUPDMGR) is not None


def _json(args, timeout=90):
    env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")
    p = subprocess.run([FWUPDMGR, *args, "--json", "--no-unreported-check"], capture_output=True, text=True,
                       timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    text = p.stdout.strip()
    if not text:
        if p.returncode not in (0, 2):  # 2: nothing to do
            raise RuntimeError((p.stderr or "").strip()[:300] or f"fwupdmgr exit code {p.returncode}")
        return {}
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(text[:300])


def plain(markup):
    """fwupd's release notes are a little HTML (<p>, <ul><li>): plain text with bullets."""
    if not markup:
        return ""
    t = re.sub(r"<\s*li\s*>", "\n• ", markup, flags=re.I)
    t = re.sub(r"<\s*/\s*p\s*>|<\s*br\s*/?\s*>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in t.splitlines())).strip()


def _device(d):
    flags = set(d.get("Flags") or [])
    icons = d.get("Icons") or []
    return dict(id=d.get("DeviceId") or "", name=d.get("Name") or "?", vendor=d.get("Vendor") or "",
                version=d.get("Version") or "", plugin=d.get("Plugin") or "", icons=list(icons),
                updatable="updatable" in flags, needs_reboot="needs-reboot" in flags,
                needs_shutdown="needs-shutdown" in flags, internal="internal" in flags,
                summary=d.get("Summary") or "")


def devices():
    """Every device fwupd knows, updatable or not."""
    return [_device(d) for d in _json(["get-devices"]).get("Devices") or []]


def updates():
    """Devices with a newer firmware on LVFS: one dict each, the newest release."""
    out = []
    for d in _json(["get-updates"]).get("Devices") or []:
        releases = d.get("Releases") or []
        upgrades = [r for r in releases if "is-upgrade" in (r.get("Flags") or [])] or releases
        if not upgrades:
            continue
        r = upgrades[0]
        dev = _device(d)
        rflags = set(r.get("Flags") or [])
        dev.update(new_version=r.get("Version") or "", release_summary=r.get("Summary") or "",
                   description=plain(r.get("Description") or ""), size=int(r.get("Size") or 0),
                   urgency=r.get("Urgency") or "", remote=r.get("RemoteId") or "",
                   release_vendor=r.get("Vendor") or dev["vendor"],
                   needs_reboot=dev["needs_reboot"] or "needs-reboot" in rflags)
        out.append(dev)
    return out


def last_refresh():
    """When the list from LVFS was last downloaded (epoch), or None."""
    try:
        remotes = _json(["get-remotes"]).get("Remotes") or []
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    times = [int(r["Mtime"]) for r in remotes if r.get("Enabled") and str(r.get("Mtime") or "").isdigit()
             and int(r["Mtime"]) > 0 and r.get("Kind") != "directory"]
    return max(times) if times else None
