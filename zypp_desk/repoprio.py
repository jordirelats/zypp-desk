# SPDX-License-Identifier: GPL-3.0-or-later
"""Repository priorities explained, a suggested order, and new addresses for repositories that moved.

Nothing here changes anything: it only classifies repositories and reads public web pages.
"""

import re
import urllib.request
from pathlib import Path

from . import VERSION
from .i18n import _
from .style import BLUE, GREEN, ORANGE, PURPLE, RED

GREY = "#7f8c8d"


def tiers():
    """(priority, short name, colour, why) of each kind of repository, lowest number (strongest) first."""
    return [
        (90, _("Codecs"), RED, _("VLC and the codecs must always come from here, even when openSUSE has a version "
                                 "of them.")),
        (99, _("openSUSE"), GREEN, _("The base of the system. 99 is the default number: it stays as it is.")),
        (100, _("Programs from companies"), BLUE, _("Each brings its own program (Chrome, Code, Brave…). They must "
                                                    "not replace anything from openSUSE.")),
        (105, _("OBS projects"), PURPLE, _("openSUSE groups that try newer versions (KDE:Extra, multimedia…).")),
        (110, _("Personal repositories"), ORANGE, _("Made by one person (home:… on OBS, Fedora COPR). Only for what "
                                                    "is nowhere else.")),
        (120, _("Local folders"), GREY, _("Files you downloaded yourself. If openSUSE has a version, openSUSE "
                                          "wins.")),
    ]


def proposal(repo):
    """The suggested priority of a repository (a dict from backend.repos())."""
    alias, url = (repo.get("alias") or "").lower(), (repo.get("url") or "").lower()
    name = (repo.get("name") or "").lower()
    if "videolan" in alias or "videolan" in url or "packman" in url or "packman" in name:
        return 90
    if alias.startswith("opensuse:") or "cdn.opensuse.org" in url or "codecs.opensuse.org" in url \
            or re.search(r"download\.opensuse\.org/(tumbleweed|update|distribution|slowroll|history)/", url):
        return 99
    if url.startswith(("dir:", "file:", "/")) or repo.get("type") == "plaindir":
        return 120
    if "/repositories/home:" in url or "copr" in url:
        return 110
    if "download.opensuse.org/repositories/" in url:
        return 105
    return 100


def tier_of(prio):
    """(short name, colour) for a priority number that matches a tier, else None."""
    for p, name, color, _why in tiers():
        if p == prio:
            return name, color
    return None


# ------------------------------------------------------------------ a new address
OBS_RE = re.compile(r"^(https?)://download\.opensuse\.org/repositories/(?P<project>.+?)/(?P<dist>[^/]+)/?$")
HREF_RE = re.compile(r'href="\./([^"?/]+)/"')  # the folders of a download.opensuse.org listing ("./name/")


def system_dist():
    """The OBS folder name for this system: openSUSE_Tumbleweed, openSUSE_Slowroll, or 16.0 / 15.6 for Leap."""
    info = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            k, _s, v = line.partition("=")
            info[k] = v.strip().strip('"')
    except OSError:
        pass
    ident = info.get("ID", "")
    if "tumbleweed" in ident:
        return "openSUSE_Tumbleweed"
    if "slowroll" in ident:
        return "openSUSE_Slowroll"
    return info.get("VERSION_ID", "")


def obs_parts(url):
    m = OBS_RE.match(url.strip())
    return (m.group("project"), m.group("dist")) if m else None


def _get(url, limit=512 * 1024):
    from .websources import https_open
    req = urllib.request.Request(url, headers={"User-Agent": f"ZyppDesk/{VERSION}"})
    with https_open(req, timeout=20) as r:
        return r.read(limit).decode("utf-8", "replace")


def obs_folders(project):
    """The distributions an OBS project builds for (the folders of its download directory)."""
    text = _get(f"https://download.opensuse.org/repositories/{project}/")
    out = []
    for f in HREF_RE.findall(text):
        if f and f not in out and f != "..":
            out.append(f)
    return out


def candidates(url):
    """Other addresses where this repository may live now: [{url, folder, why, mine}]. OBS projects only:
    the folders the project publishes, this system's first."""
    parts = obs_parts(url)
    if not parts:
        alt = []
        if url.startswith("http://"):
            alt.append({"url": "https://" + url[7:], "folder": "", "why": _("The same address, with https."),
                        "mine": True})
        return alt
    project, dist = parts
    mine = system_dist()
    out = []
    for f in obs_folders(project):
        if f == dist:
            continue
        is_mine = f == mine
        why = (_("It is your system.") if is_mine else
               _("For Slowroll, not for Tumbleweed.") if f == "openSUSE_Slowroll" else
               _("For Leap {version}.").format(version=f) if re.fullmatch(r"\d+\.\d+", f) or "Leap" in f else
               _("For another distribution."))
        out.append({"url": f"https://download.opensuse.org/repositories/{project}/{f}/", "folder": f, "why": why,
                    "mine": is_mine})
    out.sort(key=lambda c: (not c["mine"], c["folder"] != "openSUSE_Slowroll", c["folder"]))
    return out
