# SPDX-License-Identifier: GPL-3.0-or-later
"""Web sources: packages installed from RPM files downloaded from a web site.

zypper cannot know that RustDesk 1.5.3 exists if the RPM came from a folder. A web source says
where to look (for now: the latest release of a GitHub project) and which file to take. Zypp Desk
compares that version with the installed one, downloads the file into a folder (usually the
folder that is already a local zypper repository), checks the SHA-256 that GitHub publishes and
whether rpm can verify a signature, and then the file can be installed through the queue.

The list lives in ~/.config/zypp-desk/web-sources.json.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from . import VERSION
from .backend import ENV_C, vercmp
from .i18n import _

API = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = f"zypp-desk/{VERSION} (+https://github.com/jordirelats/zypp-desk)"


def _path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "zypp-desk" / "web-sources.json"


def load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save(sources):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def new_source(package, repo, pattern, folder):
    return {"package": package, "type": "github", "repo": repo, "pattern": pattern, "folder": folder}


def installed_version(package):
    p = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\n", package], capture_output=True, text=True,
                       env=ENV_C)
    lines = p.stdout.split()
    return lines[-1] if p.returncode == 0 and lines else None


def guess_pattern(package, filename=None):
    """A regular expression for the release file, from the file installed before when we know it."""
    if filename:
        m = re.match(rf"^{re.escape(package)}-[\d.]+(-\d+)?(.*)$", filename)
        if m:
            return rf"^{re.escape(package)}-.*{re.escape(m.group(2))}$"
    return rf"^{re.escape(package)}-.*\.x86_64.*\.rpm$"


def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def check(source):
    """Look for the latest version. Returns a dict with what was found (never downloads)."""
    rel = _get_json(API.format(repo=source["repo"]))
    tag = rel.get("tag_name") or ""
    version = re.sub(r"^[vV]", "", tag)
    rx = re.compile(source["pattern"])
    asset = next((a for a in rel.get("assets", []) if rx.search(a["name"])), None)
    inst = installed_version(source["package"])
    inst_v = inst.split("-")[0] if inst else None
    newer = bool(inst_v and version and vercmp(version, inst_v) > 0)
    digest = (asset or {}).get("digest") or ""
    return {
        "tag": tag, "version": version, "published": (rel.get("published_at") or "")[:10],
        "page": rel.get("html_url", ""), "installed": inst, "newer": newer,
        "asset": asset["name"] if asset else None, "url": asset["browser_download_url"] if asset else None,
        "size": asset.get("size") if asset else None,
        "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
    }


def download(source, found, progress=None):
    """Download the release file into a private folder and verify it.

    Returns dict(path, sha256, sha256_checked, signature). The file stays in ~/.cache/zypp-desk/downloads
    until the user accepts it (see place()); a file that fails a check is deleted.
    """
    folder = download_dir()
    final = folder / found["asset"]
    part = final.with_name(final.name + ".part")
    if not found["url"].startswith("https://"):
        raise ValueError(_("Only https downloads are allowed."))
    req = urllib.request.Request(found["url"], headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:
        total = int(r.headers.get("Content-Length") or found.get("size") or 0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    digest = h.hexdigest()
    if found["sha256"] and digest != found["sha256"]:
        part.unlink(missing_ok=True)
        raise ValueError(_("The SHA-256 does not match: GitHub publishes {expected}, the file has {got}. "
                           "The file was deleted.").format(expected=found["sha256"], got=digest))
    name = subprocess.run(["rpm", "-qp", "--qf", "%{NAME}", str(part)], capture_output=True, text=True,
                          env=ENV_C).stdout.strip()
    if name != source["package"]:
        part.unlink(missing_ok=True)
        raise ValueError(_("The file contains the package “{found}”, not “{expected}”. It was deleted.")
                         .format(found=name, expected=source["package"]))
    part.replace(final)
    return {"path": str(final), "sha256": digest, "sha256_checked": bool(found["sha256"]),
            "signature": signature_of_file(final)}


def download_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "zypp-desk" / "downloads"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def place(path, source):
    """Move an accepted download into the source's folder (often a local zypper repository)."""
    folder = Path(os.path.expanduser(source["folder"]))
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / Path(path).name
    shutil.move(path, final)
    return str(final)


def signature_of_file(path):
    """'signed and verified', 'signed, unknown key (…)', 'not signed' — from rpm -K."""
    p = subprocess.run(["rpm", "-Kv", str(path)], capture_output=True, text=True, env=ENV_C)
    out = p.stdout + p.stderr
    if re.search(r"Signature, key ID \w+: OK", out):
        return "verified"
    m = re.search(r"Signature, key ID (\w+): NOKEY", out)
    if m:
        return "nokey:" + m.group(1)
    if "Signature" in out:
        return "bad"
    return "unsigned"
