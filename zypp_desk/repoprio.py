# SPDX-License-Identifier: GPL-3.0-or-later
"""Repository priorities explained, a suggested order, and new addresses for repositories that moved.

Nothing here changes the system: it classifies repositories, reads public web pages and checks addresses. The
only thing it writes is the .repo file of a repository at its new address, in Zypp Desk's own cache folder, for
the queue (new_address_file).
"""

import configparser
import os
import re
import urllib.parse
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
        leap = re.search(r"\d+\.\d+", f) if re.fullmatch(r"\d+\.\d+", f) or "Leap" in f else None
        why = (_("It is your system.") if is_mine else
               _("For Slowroll, not for Tumbleweed.") if f == "openSUSE_Slowroll" else
               _("For Leap {version}.").format(version=leap.group(0)) if leap else  # "15.6", not the folder name
               _("For another distribution."))
        out.append({"url": f"https://download.opensuse.org/repositories/{project}/{f}/", "folder": f, "why": why,
                    "mine": is_mine})
    out.sort(key=lambda c: (not c["mine"], c["folder"] != "openSUSE_Slowroll", c["folder"]))
    return out


# ------------------------------------------------------------------ checking an address, and the new .repo file
URL_SCHEMES = ("https", "http", "ftp", "tftp", "dir", "file", "hd", "iso", "nfs", "nfs4", "smb", "cifs", "cd", "dvd")
_NET_SCHEMES = ("https", "http", "ftp", "tftp", "nfs", "nfs4", "smb", "cifs")
REPOS_D = Path("/etc/zypp/repos.d")
ADDRESS_KEYS = ("baseurl", "mirrorlist", "metalink", "service")  # replaced by the new address, or not carried over


def url_problem(url):
    """Why zypper could not use this as the address of a repository, in plain words ('' when it looks right).
    Add a repository and Change the address check it before anything goes to the queue."""
    u = url or ""
    if not u.strip():
        return _("Write the address.")
    if any(c.isspace() for c in u):
        return _("An address has no spaces. Where the address really has one, write %20 instead.")
    if any(ord(c) < 32 or ord(c) == 127 for c in u):
        return _("The address has characters that cannot be part of it.")
    if u.startswith("/"):  # zypper takes a full path as a local folder
        return ""
    try:
        parts = urllib.parse.urlsplit(u)
        parts.port  # noqa: B018 (a port that is not a number raises ValueError)
    except ValueError:
        return _("This is not an address.")
    scheme = parts.scheme.lower()
    if not scheme or "://" not in u and scheme not in ("dir", "file", "hd", "iso", "cd", "dvd"):
        return _("An address starts with its kind, for example https:// (or dir:/ for a folder).")
    if scheme not in URL_SCHEMES:
        return _("zypper does not know addresses of the kind “{scheme}”.").format(scheme=scheme)
    if scheme in _NET_SCHEMES and not parts.hostname:
        return _("The address names no server.")
    if scheme in ("dir", "file") and not parts.path.startswith("/"):
        return _("The folder must be a full path, starting with /.")
    return ""


def repo_section(alias, folder=REPOS_D):
    """{key: value} of a repository as its .repo file writes it (the file name need not match the alias), or None."""
    for f in sorted(Path(folder).glob("*.repo")):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
        except (configparser.Error, OSError, ValueError):
            continue
        if cp.has_section(alias):
            return {k: v for k, v in cp.items(alias)}
    return None


def _cache_dir(path):
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def new_alias(alias):
    """The alias the repository is added under, before the old one goes and it takes the old alias."""
    from .txqueue import REPO_URL_TEMP_SUFFIX  # the queue's steps use the same name (renamerepo)
    return alias + REPO_URL_TEMP_SUFFIX


def new_address_path(alias):
    """Where new_address_file writes the .repo file of this repository (nothing is created here). Always an
    absolute path: the helper runs zypper with it as root."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or "")
    if not base.is_absolute():  # unset, or relative (which the XDG rules say to ignore)
        base = Path.home() / ".cache"
    return base / "zypp-desk" / "repos" / (re.sub(r"[^A-Za-z0-9._:+@-]", "_", new_alias(alias)) + ".repo")


def _flag(value):
    return "1" if value else "0"


def _one_line(value):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()


def new_address_file(repo, url, settings=None):
    """Write the .repo file the queue adds for a repository at a new address, and return its path.

    zypper cannot change an address, so the queue adds this file (the repository under the alias <alias>-zdnew),
    removes the old repository and renames the new one to <alias>. The file keeps EVERY setting of the old one, as
    its own .repo file has them (name, enabled, autorefresh, priority, keeppackages, type, path, gpgcheck,
    repo_gpgcheck, pkg_gpgcheck, gpgkey…; what the old file leaves to zypp.conf stays that way), with
    baseurl=<url>, and on top the changes that wait in the queue for it (settings: name, enabled, autorefresh,
    priority, keeppackages). A gpgkey inside the old address moves to the new one.

    It goes to $XDG_CACHE_HOME/zypp-desk/repos/<alias>-zdnew.repo (folder 0700, file 0600). Raises ValueError when
    the address or the alias cannot be written, OSError when the file cannot.
    """
    alias = repo.get("alias") or ""
    problem = url_problem(url)
    if problem:
        raise ValueError(problem)
    if not alias or re.search(r"[\x00-\x1f\x7f\[\]/]", alias) or alias.startswith((".", "-")):
        raise ValueError(_("The alias of this repository cannot be written in a .repo file."))
    settings = settings or {}
    old = repo_section(alias)
    if old is None:  # its .repo file cannot be read: what zypper lists about it
        old = {"name": repo.get("name") or alias, "enabled": _flag(repo.get("enabled")),
               "autorefresh": _flag(repo.get("autorefresh")), "keeppackages": _flag(repo.get("keeppackages"))}
        if repo.get("type"):
            old["type"] = repo["type"]
        if repo.get("priority") not in (None, 99):
            old["priority"] = str(repo["priority"])
        if not repo.get("gpgcheck"):
            old["gpgcheck"] = "0"
        elif not repo.get("repo_gpgcheck") and repo.get("pkg_gpgcheck"):
            old["repo_gpgcheck"], old["pkg_gpgcheck"] = "0", "1"
    out = {"name": old.get("name") or repo.get("name") or alias}
    for key, value in old.items():
        if key not in ADDRESS_KEYS:
            out.setdefault(key, value)
    for key in ("enabled", "autorefresh", "keeppackages"):
        if key in settings:
            out[key] = _flag(settings[key])
    if "priority" in settings:
        out["priority"] = str(int(settings["priority"]))
    if settings.get("name"):
        out["name"] = settings["name"]
    old_urls = (repo.get("url") or old.get("baseurl") or "").split()
    old_url = old_urls[0] if old_urls else ""
    if old_url and out.get("gpgkey"):  # a key published inside the old address: look for it at the new one
        base, new = old_url.rstrip("/") + "/", url.rstrip("/") + "/"
        out["gpgkey"] = "\n".join(new + k[len(base):] if k.startswith(base) else k for k in out["gpgkey"].split("\n"))
    lines = ["# Written by Zypp Desk: the repository {a} at its new address. The queue adds it as {n}, removes {a}"
             .format(a=_one_line(alias), n=_one_line(new_alias(alias))),
             "# and renames {n} to {a}.".format(a=_one_line(alias), n=_one_line(new_alias(alias))),
             f"[{new_alias(alias)}]"]
    first = ("name", "enabled", "autorefresh", "baseurl")
    out["baseurl"] = url
    for key in first + tuple(k for k in out if k not in first):
        if key in out:
            # a value of several lines (gpgkey, baseurl) goes on with indented lines, as zypper writes them
            value = "\n    ".join(_one_line(v) for v in str(out[key]).split("\n") if v.strip())
            lines.append(f"{key}={value}")
    path = new_address_path(alias)
    _cache_dir(path)
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return str(path)
