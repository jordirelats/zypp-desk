# SPDX-License-Identifier: GPL-3.0-or-later
"""When each repository last published something new, read from zypper's raw cache (readable by everyone).

Two questions Jordi asked (26/11/2026): is a repository being updated right now, and is a repository behind
Tumbleweed? After a new Tumbleweed version comes out:
- the mirrors take a few hours to copy it, and until then some packages give "File not found" (404);
- repositories built on top of Tumbleweed (VideoLAN, Packman, OBS projects) rebuild a day or two later, and
  until then their packages may not fit, so an upgrade shows many problems that go away by themselves.

Every repository's repodata/repomd.xml has a <revision> (a Unix time, set when it was published) and the
<timestamp> of each part. Nothing here runs zypper or needs root.
"""

import os
import re
import time

RAW = "/var/cache/zypp/raw"
HOUR = 3600
# after a new Tumbleweed version, the mirrors may still be copying it for about this long
MIRROR_SETTLE_HOURS = 6
# Tumbleweed's own repositories: the ones that say which version of Tumbleweed is out
_TW_URL = re.compile(r"download\.opensuse\.org/(tumbleweed/repo/(oss|non-oss)|update/tumbleweed)|"
                     r"/tumbleweed/repo/oss|openSUSE[:_-]?Tumbleweed/?$", re.I)
_TW_ALIAS = re.compile(r"^(openSUSE:)?repo-(oss|non-oss)$|^repo-oss$|tumbleweed-oss$", re.I)


def published_at(alias, raw=RAW):
    """When the repository with this alias last published new metadata (Unix time), or None."""
    path = os.path.join(raw, alias, "repodata", "repomd.xml")
    try:
        with open(path, "rb") as f:
            data = f.read(256 * 1024).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r"<revision>\s*(\d{9,11})\s*</revision>", data)
    if m:
        return int(m.group(1))
    stamps = [int(x) for x in re.findall(r"<timestamp>\s*(\d{9,11})\s*</timestamp>", data)]
    return max(stamps) if stamps else None


def is_tumbleweed(repo):
    """repo: a dict from backend.repos() (alias, url…). True for Tumbleweed's main repository (oss)."""
    url = repo.get("url") or ""
    alias = repo.get("alias") or ""
    if "non-oss" in url or "non-oss" in alias:
        return False
    return bool(re.search(r"tumbleweed/repo/oss", url, re.I) or _TW_ALIAS.match(alias))


def tumbleweed_family(repo):
    """True for any repository of Tumbleweed itself (oss, non-oss, update, openh264…): they are never 'behind'."""
    url = repo.get("url") or ""
    return bool(re.search(r"download\.opensuse\.org/(tumbleweed|update/tumbleweed)|codecs\.opensuse\.org", url, re.I)
                or (repo.get("alias") or "").startswith("openSUSE:") or is_tumbleweed(repo))


def tumbleweed_published(repos=None, raw=RAW):
    """When the Tumbleweed version you get now was published (Unix time), or None. repos: backend.repos(), or
    None to look at the cache folder names only."""
    aliases = [r["alias"] for r in (repos or []) if r.get("enabled", True) and is_tumbleweed(r)]
    if not aliases:
        try:
            aliases = [e.name for e in os.scandir(raw) if _TW_ALIAS.match(e.name)]
        except OSError:
            aliases = []
    times = [t for t in (published_at(a, raw) for a in aliases) if t]
    return max(times) if times else None


def hours_since(t, now=None):
    if not t:
        return None
    return max(0.0, ((now or time.time()) - t) / HOUR)


def mirrors_settling(repos=None, raw=RAW, now=None, hours=MIRROR_SETTLE_HOURS):
    """True when a new Tumbleweed came out less than `hours` ago: the mirrors may still be copying it."""
    h = hours_since(tumbleweed_published(repos, raw), now)
    return h is not None and h < hours


def behind_tumbleweed(alias, repos=None, raw=RAW):
    """True when this repository last published before the current Tumbleweed did: if it is built on top of
    Tumbleweed (VideoLAN, Packman, an OBS project), it has not been rebuilt for the new version yet.
    None when it cannot be told."""
    tw = tumbleweed_published(repos, raw)
    mine = published_at(alias, raw)
    if not tw or not mine:
        return None
    return mine < tw


def state(repos=None, raw=RAW, now=None):
    """dict(tw_published, tw_hours, settling, behind=[aliases of enabled repositories published before
    Tumbleweed]) for the windows that explain problems and the 'is it worth waiting?' question."""
    tw = tumbleweed_published(repos, raw)
    out = dict(tw_published=tw, tw_hours=hours_since(tw, now), settling=False, behind=[])
    if tw is None:
        return out
    out["settling"] = out["tw_hours"] < MIRROR_SETTLE_HOURS
    for r in repos or []:
        if not r.get("enabled", True) or tumbleweed_family(r):
            continue
        t = published_at(r["alias"], raw)
        if t and t < tw:
            out["behind"].append(r["alias"])
    return out
