# SPDX-License-Identifier: GPL-3.0-or-later
"""Move to another computer: the software of this computer in one file (.zdesk), and that file read on another
computer as changes for the queue.

No Qt here (pages/migrate.py has the two windows).

- collect() reads this computer (it only reads; slow: call it in a thread) and returns everything the file can
  hold; write() saves the parts the user ticked. The file is JSON with a format name and a version number.
- load() reads a file as untrusted input: every field is checked, names are plain text cut to a sane length,
  aliases are made safe, addresses can only be http or https (a local folder is a path under the home folder),
  lists have a maximum size, and whatever does not pass is left out and said. Nothing in it is ever run: it only
  becomes changes for the queue, which the user reviews before applying.
- prepare() compares a loaded file with this computer (slow: in a thread) and returns a plan: what to add, what
  this computer has already, what cannot be done by itself (the list "by hand").

What goes in the file (and what never does):
- the enabled repositories, each with its settings and the fingerprints of the keys that sign it, and the public
  keys themselves (armored, as gpg writes them out again: keys.armored). Repositories of a service come with the
  service (openSUSE's own); an address with a user name, a password or a token is never written: the repository
  goes to the list by hand. A local folder is written as its path under the home folder.
- the packages the user chose (not those zypper installed as dependencies: /var/lib/zypp/AutoInstalled), each
  with the repository it comes from; never the keys, kernels, firmware, microcode, kernel modules, boot loaders,
  products and patterns of this computer (they belong to its hardware and its installation), nor packages from
  local folders (those followed in Programs from the web go with them; the others go to the list by hand);
- the Flatpak apps and the remotes they come from (with the remote's public key when it is not Flathub);
- the programs from the web (where each one is published; its folder only as a path under the home folder);
- the locks (not the temporary ones of the problem window), the requested languages, Zypp Desk's zypp.conf
  settings, the routine and the protected apps;
- a list of what cannot be installed by itself: programs with their own installer, AppImages, web apps of the
  browsers, packages installed from a file.
No passwords, tokens or documents, and nothing of the home folder except folder names.
"""

import configparser
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from . import VERSION, backend, cleandeps, flatpak, keys, websources, zyppconf
from .i18n import _, ngettext, pgettext

FORMAT = "zypp-desk-migration"
FORMAT_VERSION = 1
EXTENSION = ".zdesk"
MAX_FILE = 16 * 1024 * 1024
MAX_KEY_DATA = 256 * 1024
REPOS_D = "/etc/zypp/repos.d"
REQUESTED_LOCALES = "/var/lib/zypp/RequestedLocales"
FLATPAK_SYSTEM_REPO = "/var/lib/flatpak/repo"
FLATHUB_URLS = {  # a remote's address → the .flatpakrepo file that adds it with its key
    "https://dl.flathub.org/repo/": "https://dl.flathub.org/repo/flathub.flatpakrepo",
    "https://dl.flathub.org/beta-repo/": "https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo",
}
# openSUSE's package that adds Flathub to every user (/etc/flatpak/remotes.d)
FLATHUB_PACKAGE = "flatpak-remote-flathub"

# what the file may hold at most: a file with more is refused (a list of 40,000 packages is already bigger than
# any real computer)
LIMITS = {"repositories": 1000, "keys": 300, "packages": 40000, "apps": 40000, "flatpak_remotes": 100,
          "flatpak_apps": 3000, "web_sources": 500, "locks": 3000, "locales": 300, "protected_apps": 3000,
          "todo": 5000}
# the parts the export window offers, and the sections of the file each one writes
PARTS = {
    "repos": ("repositories", "keys"),
    "packages": ("packages", "apps"),
    "flatpak": ("flatpak_remotes", "flatpak_apps"),
    "web": ("web_sources",),
    "settings": ("locks", "locales", "zypp_conf", "routine", "protected_apps"),
    "todo": ("todo",),
}
GPG_MODES = ("default", "allow-unsigned-repo", "allow-unsigned-package", "off")
LOCK_KINDS = ("package", "pattern", "patch", "product", "srcpackage")
TODO_KINDS = ("installer", "appimage", "webapp", "file-package", "repository")

PKG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,199}")
LOCK_RE = re.compile(r"[A-Za-z0-9_*?][A-Za-z0-9._+*?-]{0,199}")
RANGE_RE = re.compile(r"(<|<=|=|>=|>) ?[A-Za-z0-9._+~:-]{1,100}")
LOCALE_RE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?(@[a-z]{1,20})?")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
GITHUB_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
SIGNER_RE = re.compile(r"[0-9A-Fa-f]{8,64}")
SYSTEM_ID_RE = re.compile(r"[a-z0-9._-]{1,40}")
ARCH_RE = re.compile(r"[a-z0-9_]{1,20}")
# packages of this computer's hardware or start-up: never in the file (kernels, products and patterns are found
# by what they provide; see _system_packages)
HARDWARE_RE = re.compile(r"^(gpg-pubkey|kernel-firmware(-.+)?|ucode-.+|.+-kmp-.+|(grub2|shim|systemd-boot|sdbootutil)"
                         r"(-.+)?|zypp-desk-bridge-.+)$")
# characters never shown from a file: control characters and those that turn the direction of the text around
# (a name could look like another one)
_HIDDEN_RE = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
# a query word in an address that carries a secret: such an address is never written to the file
_SECRET_WORDS = ("credentials", "token", "auth", "key", "pass", "secret", "sig", "session", "login", "user")
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+")
_NESTED_QUANT = re.compile(r"\((?:[^()\\]|\\.)*[*+}](?:[^()\\]|\\.)*\)[*+{?]")


class MigrateError(ValueError):
    """The file cannot be used at all (not ours, a newer version, too big)."""


# ------------------------------------------------------------------ small helpers

def text(value, limit=200):
    """A name from a file as plain text: control and direction characters out, cut to `limit` characters ("…").
    None when it is not a string."""
    if not isinstance(value, str):
        return None
    value = _HIDDEN_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def safe_alias(value):
    """An alias zypper and the queue can always use: letters, digits and . _ + - (anything else becomes '-'),
    not starting with '-' or '.', at most 100 characters. '' when nothing is left."""
    if not isinstance(value, str):
        return ""
    a = re.sub(r"[^A-Za-z0-9._+-]+", "-", value).strip("-.")[:100].strip("-.")
    return a


def check_url(value, schemes=("http", "https")):
    """The address when it is one Zypp Desk may hand to zypper or flatpak, else None: one of `schemes`, a host,
    no user name or password in it, no spaces, quotes or control characters, at most 2,000 characters."""
    if not isinstance(value, str) or not value or len(value) > 2000 or not _URL_CHARS.fullmatch(value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        _port = parts.port  # a port that is not a number raises ValueError
    except ValueError:
        return None
    if parts.scheme.lower() not in schemes or not parts.hostname or "@" in parts.netloc:
        return None
    return value


def has_secret(url):
    """True when an address carries a user name, a password or something like a token in its query."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return True
    if parts.username or parts.password or "@" in parts.netloc:
        return True
    for k, _v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if any(w in k.lower() for w in _SECRET_WORDS):
            return True
    return False


def home_relative(path, home=None):
    """'Folder/Sub' for a folder under the home folder, its last name alone for one elsewhere (the other computer
    looks for the same folder name under its home), '' when there is none."""
    if not path:
        return ""
    home = os.path.realpath(str(home or Path.home()))
    p = os.path.realpath(os.path.expanduser(str(path)))
    if p.startswith(home + os.sep):
        return os.path.relpath(p, home)
    return os.path.basename(p.rstrip(os.sep))


def check_relative(value):
    """A path under the home folder from a file ('Folder/Sub'), or None: relative, no '..' or '.', no hidden
    first folder of the configuration (.config, .ssh…), plain names."""
    if not isinstance(value, str) or not value or len(value) > 300 or value.startswith(("/", "~")):
        return None
    parts = value.split("/")
    if any(p in ("", ".", "..") or _HIDDEN_RE.search(p) for p in parts):
        return None
    if parts[0].startswith(".") and parts[0] != ".local":
        return None
    return value


def under_home(rel, home=None):
    """The folder 'rel' under this home folder, when it exists and really is inside it (no link out); else None."""
    home = os.path.realpath(str(home or Path.home()))
    p = os.path.realpath(os.path.join(home, rel))
    if not p.startswith(home + os.sep) or not os.path.isdir(p):
        return None
    return p


def check_pattern(value):
    """A web source's regular expression, when it is short, compiles and has no nested repetition (a pattern like
    (a+)+ can take forever on a file name)."""
    if not isinstance(value, str) or not value or len(value) > 300 or _NESTED_QUANT.search(value):
        return None
    try:
        re.compile(value)
    except (re.error, RecursionError, OverflowError):
        return None
    return value


def norm_url(url):
    """An address as compared between two computers: no scheme, no trailing '/', lower-case host, and openSUSE's
    download hosts as one (cdn.opensuse.org and download.opensuse.org give the same repositories)."""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return url or ""
    host = (p.hostname or "").lower()
    if host in ("cdn.opensuse.org", "download.opensuse.org", "downloadcontent.opensuse.org"):
        host = "download.opensuse.org"
    path = urllib.parse.unquote(p.path).rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def computer_name():
    """This computer's name (its host name), or '' when it has none of its own ('localhost')."""
    try:
        name = socket.gethostname()
    except OSError:
        return ""
    return "" if name.lower().split(".")[0] in ("", "localhost", "localhost6") else name


def default_name(computer=None, day=None):
    """'zypp-desk-<computer>-<YYYY-MM-DD>.zdesk' (the computer's name with only letters, digits and . _ -), or
    'zypp-desk-<YYYY-MM-DD>.zdesk' when it has no name."""
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", computer if computer is not None else computer_name()).strip(".-")
    day = day or time.strftime("%Y-%m-%d")
    return f"zypp-desk-{host[:60]}-{day}{EXTENSION}" if host else f"zypp-desk-{day}{EXTENSION}"


def os_release(path="/etc/os-release"):
    """dict(id, version) of this system ('opensuse-tumbleweed', '20260101')."""
    out = {"id": "", "version": ""}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k in ("ID", "VERSION_ID"):
                    out["id" if k == "ID" else "version"] = v.strip().strip('"\'')
    except OSError:
        pass
    return out


def requested_locales(path=REQUESTED_LOCALES):
    """The languages zypper installs translations for (the file is readable by everyone)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [x.strip() for x in f if x.strip() and not x.lstrip().startswith("#")]
    except OSError:
        return []


def _rpm_names(*args):
    try:
        p = subprocess.run(["rpm", *args], capture_output=True, text=True, env=backend.ENV_C, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {x.strip() for x in p.stdout.splitlines() if x.strip() and " " not in x.strip()}


def installed_names():
    return _rpm_names("-qa", "--qf", "%{NAME}\n")


def _system_packages():
    """Names of the packages that are this computer's own: kernels (multiversion), products and patterns."""
    out = set()
    for cap in ("multiversion(kernel)", "product()", "pattern()"):
        out |= _rpm_names("-q", "--whatprovides", cap, "--qf", "%{NAME}\n")
    return out


def _repo_files():
    """alias → the section of its .repo file (the raw settings: what zypper writes, not what it computes)."""
    out = {}
    try:
        files = sorted(Path(REPOS_D).glob("*.repo"))
    except OSError:
        return out
    for f in files:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for sec in cp.sections():
            out.setdefault(sec, dict(cp[sec]))
    return out


def gpg_mode(raw):
    """The queue's gpg setting of a repository from its .repo section."""
    def flag(key):
        v = (raw.get(key) or "").strip().lower()
        return None if v == "" else v in ("1", "yes", "true", "on")
    if flag("gpgcheck") is False and not flag("repo_gpgcheck") and not flag("pkg_gpgcheck"):
        return "off"
    if flag("repo_gpgcheck") is False:
        return "allow-unsigned-repo"
    if flag("pkg_gpgcheck") is False:
        return "allow-unsigned-package"
    return "default"


def _solvables(enabled_names):
    """(installed name → the name of the repository its version comes from, names available in the enabled
    repositories). One 'zypper search --details' of every package. A package whose version no repository
    has any more takes a repository that has another version of it."""
    root, _code = backend.xml(["search", "--details", "-t", "package"])
    inst, elsewhere, avail = {}, {}, set()
    for s in root.iter("solvable"):
        name, repo = s.get("name") or "", s.get("repository", "")
        if repo in enabled_names:
            avail.add(name)
        if s.get("status") == "installed":
            if name not in inst or (inst[name] not in enabled_names and repo in enabled_names):
                inst[name] = repo
        elif repo in enabled_names:
            elsewhere.setdefault(name, repo)
    for name, repo in inst.items():
        if repo not in enabled_names and name in elsewhere:
            inst[name] = elsewhere[name]
    return inst, avail


def _app_titles(names):
    """{package: the name of its application} for the packages among `names` that put one in the menu."""
    folder = Path("/usr/share/applications")
    files = sorted(folder.glob("*.desktop")) if folder.is_dir() else []
    if not files:
        return {}
    try:
        p = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\n", "--", *map(str, files)], capture_output=True,
                           text=True, env=backend.ENV_C, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return {}
    owners = p.stdout.splitlines()
    if len(owners) != len(files):
        return {}
    out = {}
    for f, owner in zip(files, owners):
        owner = owner.strip()
        if owner not in names or owner in out:
            continue
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        try:
            cp.read(f, encoding="utf-8")
            e = cp["Desktop Entry"]
        except (configparser.Error, OSError, UnicodeDecodeError, KeyError):
            continue
        if e.get("NoDisplay", "").lower() == "true" or e.get("Type", "Application") != "Application":
            continue
        name = text(e.get("Name", ""), 80)
        if name:
            out[owner] = name
    return out


def _package_signers(exported, inst_repo, by_name):
    """{alias: key IDs} of the keys that signed the installed packages of these repositories (one rpm call)."""
    names = {}
    for name, repo in inst_repo.items():
        r = by_name.get(repo)
        if r is not None and r["alias"] in exported and exported[r["alias"]].get("gpg") != "off":
            names.setdefault(name, r["alias"])
    if not names:
        return {}
    try:
        p = subprocess.run(["rpm", "-q", "--qf", "%{NAME}|%{RSAHEADER:pgpsig}|%{DSAHEADER:pgpsig}\n", "--",
                            *sorted(names)], capture_output=True, text=True, env=backend.ENV_C, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in p.stdout.splitlines():
        name, _sep, rest = line.partition("|")
        if name in names:
            out.setdefault(names[name], set()).update(keys._signature_ids(rest))
    return out


def _armored_keys(blob):
    """An armored copy of the public keys in a keyring file (Flatpak's <remote>.trustedkeys.gpg), or ''."""
    if not blob or len(blob) > keys.MAX_KEY:
        return ""
    with keys.gpg_home() as home:
        try:
            p = subprocess.run([*keys.gpg_base(home), "--import"], input=blob, capture_output=True,
                               env=backend.ENV_C, timeout=60)
            if p.returncode != 0:
                return ""
            p = subprocess.run([*keys.gpg_base(home), "--armor", "--export-options", "export-minimal", "--export"],
                               capture_output=True, env=backend.ENV_C, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
    out = p.stdout.decode("ascii", "replace") if p.returncode == 0 else ""
    return out if out.count("-----BEGIN PGP PUBLIC KEY BLOCK-----") == 1 else ""


# ------------------------------------------------------------------ this computer → the file

def collect(settings=None, home=None):
    """Everything this computer would put in the file: a dict with every section of PARTS, a header, and
    'counts' (for the export window; not written). Only reads. Slow: call it in a thread."""
    settings = settings or {}
    home = str(home or Path.home())
    data = {"format": FORMAT, "version": FORMAT_VERSION, "app_version": VERSION,
            "computer": computer_name(), "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "system": dict(os_release(), arch=os.uname().machine)}
    todo, counts = [], {}
    all_repos = backend.repos()
    raw = _repo_files()
    followed = {s.get("package"): s for s in websources.load()}

    # ---- repositories (enabled ones; a service's repositories come with the service)
    repos, local_aliases = [], set()
    for r in all_repos:
        if not r["enabled"] or r.get("service"):
            continue
        url = r.get("url") or ""
        entry = {"alias": r["alias"], "name": r["name"], "priority": r["priority"], "enabled": True,
                 "autorefresh": r["autorefresh"], "keeppackages": r.get("keeppackages", False),
                 "gpg": gpg_mode(raw.get(r["alias"], {})), "keys": []}
        if r.get("type") == "plaindir" or url.startswith(("dir:", "file:")):
            local_aliases.add(r["alias"])
            path = urllib.parse.unquote(url.split(":", 1)[1]) if ":" in url else url
            entry.update(url="", home_folder=home_relative(path, home))
            repos.append(entry)
            continue
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https") or has_secret(url):
            todo.append({"kind": "repository", "name": r["name"],
                         "reason": "secret" if scheme in ("http", "https") else "scheme"})
            continue
        entry["url"] = url
        repos.append(entry)
    data["repositories"] = repos

    # where each installed package comes from (the name of its repository)
    enabled_names = {r["name"] for r in all_repos if r["enabled"]}
    by_name = {}
    for r in sorted(all_repos, key=lambda r: not r["enabled"]):
        by_name.setdefault(r["name"], r)
    try:
        inst_repo, _avail = _solvables(enabled_names)
    except (backend.ZypperError, OSError, subprocess.SubprocessError):
        inst_repo = {}

    # ---- the keys that sign them (openSUSE's own are on every openSUSE: only their fingerprints)
    exported = {r["alias"]: r for r in repos if r.get("url")}
    data["keys"] = []
    try:
        signers = {alias: ids for alias, _n, _e, ids in keys.repo_signers() if alias in exported}
        # a repository whose list of packages is not signed (or not read yet): the keys of the packages
        # installed from it
        for alias, ids in _package_signers(exported, inst_repo, by_name).items():
            if not signers.get(alias):
                signers[alias] = ids
        entries = keys._rpm_keys()
        blob = "\n".join(e[5] for e in entries).encode("utf-8", "replace")
        parsed = keys.parse_colons(keys._gpg(["--with-colons", "--fixed-list-mode", "--show-keys"], blob)[1]) \
            if blob else []
        shipped = keys.shipped_fingerprints()
        for e in entries:
            k = keys._match_rpm_entry(e, parsed)
            if k is None:
                continue
            users = [a for a, ids in signers.items() if any(keys.matches_id(k, i) for i in ids)]
            if not users:
                continue
            for a in users:
                exported[a]["keys"].append(k["fpr"])
            if k["fpr"] in shipped or any(x["fpr"] == k["fpr"] for x in data["keys"]):
                continue
            try:
                armor = keys.armored(e[5].encode("utf-8", "replace"), k["fpr"]).decode("ascii")
            except (ValueError, UnicodeDecodeError, OSError, subprocess.SubprocessError):
                continue
            data["keys"].append({"fpr": k["fpr"], "name": k.get("name") or "", "email": k.get("email") or "",
                                 "data": armor})
    except (backend.ZypperError, OSError, subprocess.SubprocessError):
        pass
    counts["repos"] = len(repos)
    counts["keys"] = len(data["keys"])

    # ---- the packages you chose
    installed = installed_names()
    auto = cleandeps.auto_installed()
    system = _system_packages()
    packages, skipped = [], 0
    for name in sorted(installed - auto):
        if name in system or HARDWARE_RE.match(name):
            skipped += 1
            continue
        r = by_name.get(inst_repo.get(name, ""))
        alias = r["alias"] if r else ""
        if alias in local_aliases or (r is not None and r.get("type") == "plaindir"):
            if name not in followed:
                todo.append({"kind": "file-package", "name": name})
            continue
        if r is None and name in followed:
            continue  # a program from the web whose folder is not a repository: it goes with its web source
        packages.append({"name": name, "repo": alias if alias in exported else ""})
    data["packages"] = packages
    data["apps"] = _app_titles({p["name"] for p in packages})
    counts["packages"] = len(packages)
    counts["packages_skipped"] = skipped

    # ---- Flatpak
    data["flatpak_remotes"], data["flatpak_apps"] = [], []
    if flatpak.available():
        try:
            apps = flatpak.installed()
            eol = flatpak.eol_apps(apps)
            remotes = {(r["name"], r["installation"]): r for r in flatpak.remotes()}
        except (OSError, subprocess.SubprocessError):
            apps, eol, remotes = [], {}, {}
        used = set()
        for a in apps:
            if not flatpak.APP_ID_RE.fullmatch(a.get("application") or ""):
                continue
            inst = a.get("installation") or "system"
            inst = inst if inst in ("system", "user") else "system"
            msg = eol.get(a["application"])
            data["flatpak_apps"].append({"id": a["application"], "name": a.get("name") or a["application"],
                                         "branch": a.get("branch") or "stable", "remote": a.get("origin") or "",
                                         "installation": inst, "eol": (msg[0] or "-") if msg else ""})
            used.add((a.get("origin") or "", inst))
        for (name, inst), r in sorted(remotes.items()):
            if (name, inst) not in used:
                continue
            url = r.get("url") or ""
            if has_secret(url):
                continue
            entry = {"name": name, "title": r.get("title") or name, "url": url, "installation": inst}
            repo_file = FLATHUB_URLS.get(url if url.endswith("/") else url + "/")
            if repo_file:
                entry["flatpakrepo"] = repo_file
            else:
                base = FLATPAK_SYSTEM_REPO if inst == "system" else os.path.join(
                    os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local/share"), "flatpak/repo")
                try:
                    with open(os.path.join(base, f"{name}.trustedkeys.gpg"), "rb") as f:
                        armor = _armored_keys(f.read(keys.MAX_KEY + 1))
                except OSError:
                    armor = ""
                if armor:
                    entry["key"] = armor
            data["flatpak_remotes"].append(entry)
    counts["flatpak"] = len(data["flatpak_apps"])

    # ---- programs from the web (the folder only under the home folder)
    web = []
    for s in websources.load():
        out = {k: s[k] for k in ("package", "type", "repo", "pattern", "url", "page", "note", "signer")
               if isinstance(s.get(k), str) and s.get(k)}
        if s.get("folder"):
            out["home_folder"] = home_relative(s["folder"], home)
        web.append(out)
    data["web_sources"] = web
    counts["web"] = len(web)

    # ---- locks, languages, zypp.conf, the routine, protected apps
    temp = {(t.get("spec"), t.get("type") or "package", t.get("repo") or "") for t in settings.get("temp_locks") or []
            if isinstance(t, dict)}
    try:
        locks = [lk for lk in backend.locks() if (lk["spec"], lk["type"], lk["repo"]) not in temp]
    except (backend.ZypperError, OSError, subprocess.SubprocessError):
        locks = []
    data["locks"] = [{"name": lk["name"], "range": lk["range"], "type": lk["type"], "repo": lk["repo"]}
                     for lk in locks]
    data["locales"] = requested_locales()
    data["zypp_conf"] = {k: zyppconf.normalize(k, v) for k, v in zyppconf.ours().items()
                         if zyppconf.validate(k, zyppconf.normalize(k, v))}
    data["routine"] = settings.get("routine") or ""
    data["protected_apps"] = [p for p in settings.get("protected_apps") or [] if isinstance(p, str)]
    counts["locks"] = len(data["locks"])
    counts["locales"] = len(data["locales"])
    counts["zypp_conf"] = len(data["zypp_conf"])
    counts["protected"] = len(data["protected_apps"])

    # ---- by hand: own installers, AppImages, web apps
    todo += _own_installers(home)
    todo += _appimages(settings, home)
    todo += _webapps()
    data["todo"] = todo
    counts["todo"] = len(todo)
    data["counts"] = counts
    return data


def _short(path, home):
    path = str(path)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _own_installers(home):
    try:
        from . import scripts
        found = scripts.find_programs()
    except Exception:  # a folder that cannot be read, a desktop file that is not one: no list, not a failure
        return []
    out = []
    for p in found:
        if p.get("state") != "ok":
            continue
        out.append({"kind": "installer", "name": p.get("name") or "?",
                    "where": _short(p["folder"], home) if p.get("folder") else ""})
    return out


def _appimages(settings, home):
    """The AppImages in the user's folders (appimages.scan when it is there; else their names in the usual
    folders)."""
    try:
        from . import appimages
        found = appimages.scan(extra=settings.get("appimage_folders") or ())
        out = []
        for a in found:
            if a.get("orphan"):
                continue
            src = a.get("source")
            item = {"kind": "appimage", "name": a.get("name") or os.path.basename(a.get("path") or "?"),
                    "where": _short(a.get("folder") or "", home)}
            if isinstance(src, dict) and src.get("repo"):
                item["github"] = src["repo"]
            out.append(item)
        return out
    except Exception:  # not there yet, or a file that could not be read: the plain way below
        pass
    out, seen = [], set()
    for folder in ("Applications", "AppImages", ".local/bin", "bin", "Downloads", "Desktop"):
        d = os.path.join(home, folder)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names[:5000]:
            p = os.path.join(d, n)
            if n.lower().endswith(".appimage") and os.path.isfile(p) and os.path.realpath(p) not in seen:
                seen.add(os.path.realpath(p))
                out.append({"kind": "appimage", "name": n[:-len(".appimage")], "where": _short(d, home)})
    return out


def _webapps():
    try:
        from . import webapps
        found = webapps.scan()
    except Exception:
        return []
    return [{"kind": "webapp", "name": a.get("name") or "?", "browser": a.get("origin_name") or "",
             "host": a.get("host") or ""} for a in found if not a.get("orphan")]


def file_data(data, parts):
    """What write() saves: the header and the sections of the ticked parts (never 'counts')."""
    out = {k: data[k] for k in ("format", "version", "app_version", "computer", "created", "system") if k in data}
    for part in parts:
        for sec in PARTS.get(part, ()):
            if sec in data:
                out[sec] = data[sec]
    if "repos" not in parts:  # without the repositories, the packages do not name them
        out["packages"] = [dict(p, repo="") for p in out.get("packages", [])]
    return out


def write(path, data, parts):
    """Save the file (the parts ticked), replacing the old one only when the new one is complete."""
    payload = json.dumps(file_data(data, parts), ensure_ascii=False, indent=1).encode("utf-8")
    folder = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".zypp-desk-", suffix=".part", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(payload)


# ------------------------------------------------------------------ a file → checked data

def load(path):
    """Read a .zdesk file and check it (see clean()). Raises MigrateError when it cannot be used at all."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE + 1)
    except OSError as e:
        raise MigrateError(_("The file could not be read: {reason}").format(reason=e.strerror or e)) from None
    if len(raw) > MAX_FILE:
        raise MigrateError(_("The file is bigger than 16 MiB: it is not a file of Zypp Desk."))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MigrateError(_("This is not a file of Zypp Desk's “Move to another computer”.")) from None
    return clean(data)


def _list(data, key, what):
    v = data.get(key, [])
    if v is None:
        return []
    if not isinstance(v, list):
        raise MigrateError(_("The file is damaged ({what}).").format(what=what))
    if len(v) > LIMITS[key]:
        raise MigrateError(_("The file holds too many {what} ({n}; at most {limit}): it was not used.").format(
            what=what, n=len(v), limit=LIMITS[key]))
    return v


def _bool(v, default):
    return v if isinstance(v, bool) else default


def clean(data):
    """The file's data, checked: a new dict with only what passed, and 'refused': [(what, name, why)] for what
    was left out. Raises MigrateError when it is not our format or comes from a newer version."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise MigrateError(_("This is not a file of Zypp Desk's “Move to another computer”."))
    ver = data.get("version")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise MigrateError(_("The file is damaged ({what}).").format(what=_("version")))
    if ver > FORMAT_VERSION:
        raise MigrateError(_("This file was made by a newer Zypp Desk ({version}). Update Zypp Desk on this "
                             "computer first.").format(version=text(data.get("app_version"), 20) or "?"))
    refused = []

    def refuse(what, name, why):
        refused.append((what, text(name, 80) if isinstance(name, str) else "?", why))

    out = {"version": ver, "computer": text(data.get("computer"), 64) or "", "created": text(data.get("created"), 40)
           or "", "app_version": text(data.get("app_version"), 20) or "", "refused": refused}
    sysinfo = data.get("system") if isinstance(data.get("system"), dict) else {}
    out["system"] = {"id": sysinfo.get("id") if isinstance(sysinfo.get("id"), str)
                     and SYSTEM_ID_RE.fullmatch(sysinfo["id"]) else "",
                     "version": text(sysinfo.get("version"), 40) or "",
                     "arch": sysinfo.get("arch") if isinstance(sysinfo.get("arch"), str)
                     and ARCH_RE.fullmatch(sysinfo["arch"]) else ""}
    bad_address = _("its address is not one Zypp Desk can use (http or https, without a password)")

    # ---- repositories
    repos, seen = [], set()
    for r in _list(data, "repositories", _("repositories")):
        if not isinstance(r, dict):
            continue
        alias = safe_alias(r.get("alias"))
        name = text(r.get("name"), 200) or alias
        if not alias or alias in seen:
            refuse(_("Repository"), name or "?", _("it has no usable alias"))
            continue
        entry = {"alias": alias, "name": name}
        if r.get("home_folder"):
            rel = check_relative(r["home_folder"])
            if rel is None:
                refuse(_("Repository"), name, _("its folder is not a folder under the home folder"))
                continue
            entry.update(url="", home_folder=rel)
        else:
            url = check_url(r.get("url"))
            if url is None or has_secret(url):
                refuse(_("Repository"), name, bad_address)
                continue
            entry["url"] = url
        prio = r.get("priority", 99)
        entry["priority"] = prio if isinstance(prio, int) and not isinstance(prio, bool) and 1 <= prio <= 999 else 99
        entry["enabled"] = _bool(r.get("enabled"), True)
        entry["autorefresh"] = _bool(r.get("autorefresh"), True)
        entry["keeppackages"] = _bool(r.get("keeppackages"), False)
        entry["gpg"] = r.get("gpg") if r.get("gpg") in GPG_MODES else "default"
        fprs = r.get("keys") if isinstance(r.get("keys"), list) else []
        entry["keys"] = list(dict.fromkeys(f for f in (keys.normalize_fpr(x) if isinstance(x, str) else ""
                                                       for x in fprs[:20]) if f))
        seen.add(alias)
        repos.append(entry)
    out["repositories"] = repos

    # ---- keys (only read here; prepare() reads each one with gpg)
    key_list, seen = [], set()
    for k in _list(data, "keys", _("keys")):
        if not isinstance(k, dict):
            continue
        fpr = keys.normalize_fpr(k.get("fpr") if isinstance(k.get("fpr"), str) else "")
        name = text(k.get("name"), 200) or ""
        blob = k.get("data")
        if not fpr or fpr in seen:
            refuse(_("Signing key"), name or "?", _("it has no fingerprint"))
            continue
        if (not isinstance(blob, str) or len(blob) > MAX_KEY_DATA or not blob.isascii()
                or blob.count("-----BEGIN PGP") != 1 or "-----BEGIN PGP PUBLIC KEY BLOCK-----" not in blob):
            refuse(_("Signing key"), name or fpr[-8:], _("it is not one public key"))
            continue
        seen.add(fpr)
        key_list.append({"fpr": fpr, "name": name, "email": text(k.get("email"), 200) or "", "data": blob})
    out["keys"] = key_list

    # ---- packages
    pkgs, seen = [], set()
    for p in _list(data, "packages", _("packages")):
        name = p.get("name") if isinstance(p, dict) else p
        if not isinstance(name, str) or not PKG_RE.fullmatch(name):
            refuse(_("Package"), name if isinstance(name, str) else "?", _("it is not a package name"))
            continue
        if name in seen:
            continue
        seen.add(name)
        pkgs.append({"name": name, "repo": safe_alias(p.get("repo")) if isinstance(p, dict) else ""})
    out["packages"] = pkgs
    apps = data.get("apps") if isinstance(data.get("apps"), dict) else {}
    if len(apps) > LIMITS["apps"]:
        raise MigrateError(_("The file holds too many {what} ({n}; at most {limit}): it was not used.").format(
            what=_("applications"), n=len(apps), limit=LIMITS["apps"]))
    out["apps"] = {k: text(v, 80) for k, v in apps.items()
                   if isinstance(k, str) and k in seen and text(v, 80)}

    # ---- Flatpak
    remotes, seen = [], set()
    for r in _list(data, "flatpak_remotes", _("Flatpak remotes")):
        if not isinstance(r, dict):
            continue
        name = r.get("name") if isinstance(r.get("name"), str) and REMOTE_RE.fullmatch(r["name"]) else ""
        inst = r.get("installation") if r.get("installation") in ("system", "user") else "system"
        url = check_url(r.get("url"))
        if not name or url is None or (name, inst) in seen:
            refuse(_("Flatpak remote"), r.get("name") or "?", bad_address)
            continue
        entry = {"name": name, "title": text(r.get("title"), 100) or name, "url": url, "installation": inst}
        repo_file = check_url(r.get("flatpakrepo"), ("https",))
        if repo_file:
            entry["flatpakrepo"] = repo_file
        blob = r.get("key")
        if (isinstance(blob, str) and blob.isascii() and len(blob) <= MAX_KEY_DATA
                and blob.count("-----BEGIN PGP") == 1 and "-----BEGIN PGP PUBLIC KEY BLOCK-----" in blob):
            entry["key"] = blob
        seen.add((name, inst))
        remotes.append(entry)
    out["flatpak_remotes"] = remotes
    fapps, seen = [], set()
    for a in _list(data, "flatpak_apps", _("Flatpak apps")):
        if not isinstance(a, dict):
            continue
        app_id = a.get("id")
        if not isinstance(app_id, str) or len(app_id) > 255 or not flatpak.APP_ID_RE.fullmatch(app_id):
            refuse(_("Flatpak app"), app_id if isinstance(app_id, str) else "?", _("it is not a Flatpak app ID"))
            continue
        if app_id in seen:
            continue
        seen.add(app_id)
        remote = a.get("remote") if isinstance(a.get("remote"), str) and REMOTE_RE.fullmatch(a["remote"]) else ""
        branch = a.get("branch") if isinstance(a.get("branch"), str) and BRANCH_RE.fullmatch(a["branch"]) \
            else "stable"
        fapps.append({"id": app_id, "name": text(a.get("name"), 100) or app_id, "branch": branch,
                      "remote": remote, "installation": a.get("installation")
                      if a.get("installation") in ("system", "user") else "system",
                      "eol": text(a.get("eol"), 300) or ""})
    out["flatpak_apps"] = fapps

    # ---- programs from the web
    web, seen = [], set()
    for s in _list(data, "web_sources", _("programs from the web")):
        if not isinstance(s, dict):
            continue
        pkg = s.get("package")
        if not isinstance(pkg, str) or not PKG_RE.fullmatch(pkg) or pkg in seen:
            continue
        kind = s.get("type") if s.get("type") in websources.TYPES else None
        entry = {"package": pkg, "type": kind}
        ok = kind is not None
        if kind == "github":
            repo = s.get("repo")
            ok = isinstance(repo, str) and bool(GITHUB_RE.fullmatch(repo)) and check_pattern(s.get("pattern"))
            entry.update(repo=repo, pattern=s.get("pattern"))
        elif kind == "url":
            entry["url"] = check_url(s.get("url"))
            ok = entry["url"] is not None
        elif kind == "page":
            entry.update(page=check_url(s.get("page")), pattern=check_pattern(s.get("pattern")))
            ok = bool(entry["page"] and entry["pattern"])
        elif kind == "manual" and s.get("page"):
            page = check_url(s.get("page"))
            if page:
                entry["page"] = page
        if not ok:
            refuse(_("Program from the web"), pkg, _("where it is published cannot be used"))
            continue
        if text(s.get("note"), 300):
            entry["note"] = text(s.get("note"), 300)
        if isinstance(s.get("signer"), str) and SIGNER_RE.fullmatch(s["signer"]):
            entry["signer"] = s["signer"].lower()
        if s.get("home_folder"):
            rel = check_relative(s["home_folder"])
            if rel:
                entry["home_folder"] = rel
        seen.add(pkg)
        web.append(entry)
    out["web_sources"] = web

    # ---- locks, languages, zypp.conf, routine, protected apps
    locks, seen = [], set()
    for lk in _list(data, "locks", _("locks")):
        if not isinstance(lk, dict):
            continue
        name, rng = lk.get("name"), lk.get("range") or ""
        kind = lk.get("type") if lk.get("type") in LOCK_KINDS else None
        if not isinstance(name, str) or not LOCK_RE.fullmatch(name) or kind is None or (
                rng and not (isinstance(rng, str) and RANGE_RE.fullmatch(rng))):
            refuse(pgettext("noun", "Lock"), name if isinstance(name, str) else "?",
                   _("it is not a lock Zypp Desk can add"))
            continue
        entry = {"name": name, "range": rng, "type": kind, "repo": safe_alias(lk.get("repo"))}
        key = (name, rng, kind, entry["repo"])
        if key not in seen:
            seen.add(key)
            locks.append(entry)
    out["locks"] = locks
    out["locales"] = list(dict.fromkeys(c for c in _list(data, "locales", _("languages"))
                                        if isinstance(c, str) and LOCALE_RE.fullmatch(c)))
    zc = data.get("zypp_conf") if isinstance(data.get("zypp_conf"), dict) else {}
    out["zypp_conf"] = {k: v for k, v in zc.items() if zyppconf.validate(k, v)}
    for k in zc:
        if k not in out["zypp_conf"]:
            refuse("zypp.conf", k if isinstance(k, str) else "?", _("Zypp Desk does not write this value"))
    routine = data.get("routine")
    out["routine"] = ""
    if isinstance(routine, str) and routine.strip():
        if len(routine) <= 2000 and not _HIDDEN_RE.search(routine):
            out["routine"] = routine.strip()
        else:
            refuse(_("Routine"), _("Routine"), _("it is not one line of commands"))
    prot = []
    for p in _list(data, "protected_apps", _("protected apps")):
        if not isinstance(p, str):
            continue
        kind, sep, ident = p.partition(":")
        if sep and ((kind == "flatpak" and flatpak.APP_ID_RE.fullmatch(ident)) or (kind == "rpm" and
                                                                                   PKG_RE.fullmatch(ident))):
            prot.append(p)
    out["protected_apps"] = list(dict.fromkeys(prot))

    # ---- by hand
    todo = []
    for t in _list(data, "todo", _("things to do by hand")):
        if not isinstance(t, dict) or t.get("kind") not in TODO_KINDS:
            continue
        name = text(t.get("name"), 200)
        if not name:
            continue
        item = {"kind": t["kind"], "name": name}
        for k, limit in (("where", 300), ("browser", 60), ("host", 200)):
            v = text(t.get(k), limit)
            if v:
                item[k] = v
        if isinstance(t.get("github"), str) and GITHUB_RE.fullmatch(t["github"]):
            item["github"] = t["github"]
        if t.get("reason") in ("secret", "scheme"):
            item["reason"] = t["reason"]
        todo.append(item)
    out["todo"] = todo
    return out


# ------------------------------------------------------------------ the file compared with this computer

def look_here(clean_data, settings=None):
    """What plan() needs to know about this computer. Only reads; slow (zypper, rpm, gpg, flatpak): in a thread."""
    settings = settings or {}
    here = {"home": str(Path.home()), "system": dict(os_release(), arch=os.uname().machine)}
    here["repos"] = backend.repos()
    enabled_names = {r["name"] for r in here["repos"] if r["enabled"]}
    try:
        _inst, here["available"] = _solvables(enabled_names)
    except (backend.ZypperError, OSError, subprocess.SubprocessError):
        here["available"] = set()
    here["installed"] = installed_names()
    try:
        here["locks"] = backend.locks()
    except (backend.ZypperError, OSError, subprocess.SubprocessError):
        here["locks"] = []
    here["locales"] = requested_locales()
    here["flatpak"] = flatpak.available()
    here["flatpak_remotes"], here["flatpak_apps"] = [], set()
    if here["flatpak"]:
        try:
            here["flatpak_remotes"] = flatpak.remotes()
            here["flatpak_apps"] = {a["application"] for a in flatpak.installed()}
        except (OSError, subprocess.SubprocessError):
            pass
    here["web_sources"] = websources.load()
    try:
        snap = zyppconf.Snapshot()
        here["zypp_conf_ours"] = {k: zyppconf.normalize(k, v) for k, v in snap.ours_raw.items()}
        here["zypp_conf_effective"] = {k: v["value"] for k, v in snap.effective().items()}
    except (OSError, ValueError, KeyError, AttributeError):
        here["zypp_conf_ours"], here["zypp_conf_effective"] = {}, {}
    try:
        here["trusted"] = set(keys.installed_keys())
    except (OSError, subprocess.SubprocessError):
        here["trusted"] = set()
    # each key of the file read with gpg: exactly one public key, with the fingerprint the file says
    here["key_check"] = {}
    for k in clean_data.get("keys", []):
        try:
            found = keys.inspect_key(k["data"].encode("ascii"))
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            here["key_check"][k["fpr"]] = str(e) or _("The key could not be read.")
            continue
        if [x["fpr"] for x in found] != [k["fpr"]]:
            here["key_check"][k["fpr"]] = _("The key in the file is not the one with this fingerprint.")
        elif found[0].get("revoked"):
            here["key_check"][k["fpr"]] = _("The key was revoked.")
        elif found[0].get("expired"):
            here["key_check"][k["fpr"]] = _("The key has expired.")
        else:
            here["key_check"][k["fpr"]] = found[0]
    for r in clean_data.get("flatpak_remotes", []):
        if r.get("key"):
            try:
                found = keys.inspect_key(r["key"].encode("ascii"))
            except (ValueError, OSError, subprocess.SubprocessError):
                found = []
            r["key_fprs"] = [x["fpr"] for x in found if not x.get("revoked")]
    here["routine"] = settings.get("routine") or ""
    here["protected_apps"] = list(settings.get("protected_apps") or [])
    return here


def prepare(clean_data, settings=None, queued=None, remote_add=False):
    """look_here() and plan() in one call, for a thread."""
    return plan(clean_data, look_here(clean_data, settings), queued, remote_add=remote_add)


def _fnmatch_any(name, patterns):
    import fnmatch
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def plan(data, here, queued=None, remote_add=False):
    """What importing `data` (from clean()) on this computer (`here`, from look_here()) means, as plain data; the
    import window turns it into queue changes. queued: dict(aliases, urls, packages, flatpaks) of what the queue
    holds already. remote_add: the queue can add a Flatpak remote (a "flatpak-remote-add" change).

    The plan (every list can be empty):
      repos: [{alias, name, url, opts, keys, kind: 'add'}] — opts are the repo-add options of the queue;
      repos_enable: [{alias, name}] — the same repository here, disabled: enable it;
      repos_have: how many are here already; keys: [{fpr, name, data, for_repo, repos}] — keys to trust (repos:
        every new repository it signs; for_repo: the first one);
      packages: [{name, target, title, repo}] — target is 'name', or 'alias:name' for a repository added now;
      packages_have, packages_missing, packages_later, packages_locked: names;
      flatpak_remotes: [{name, url, installation, title, key}] (only with remote_add);
      flatpak: [{id, name, target, remote, installation}]; flatpak_have: count;
      locks: [{target, type, repo}]; locks_have: count; locales: [codes];
      zypp_conf: {settings, changes: [(key, now, new)]} or None; routine: the new one, or '';
      protected: [keys to add]; web_sources: [sources to add]; web_have: count;
      todo: [{kind, title, detail}]; warnings: [text]; refused: as clean() said.
    """
    queued = queued or {}
    home = here.get("home") or str(Path.home())
    out = {"computer": data.get("computer") or "", "warnings": [], "todo": [], "refused": list(data["refused"])}
    sys_file, sys_here = data.get("system") or {}, here.get("system") or {}
    if sys_file.get("id") and sys_here.get("id") and sys_file["id"] != sys_here["id"]:
        out["warnings"].append(_("The file comes from another system ({there}; this one is {here}): its "
                                 "repositories may not fit this one.").format(there=sys_file["id"],
                                                                              here=sys_here["id"]))
    if sys_file.get("arch") and sys_here.get("arch") and sys_file["arch"] != sys_here["arch"]:
        out["warnings"].append(_("The file comes from a computer of another kind ({there}; this one is {here}): "
                                 "some programs may not exist for this one.").format(there=sys_file["arch"],
                                                                                     here=sys_here["arch"]))

    # ---- repositories
    here_repos = here.get("repos") or []
    by_url = {}
    for r in here_repos:
        if r.get("url"):
            by_url.setdefault(norm_url(r["url"]), r)
    used = {r["alias"] for r in here_repos} | set(queued.get("aliases") or ())
    queued_urls = {norm_url(u) for u in queued.get("urls") or ()}
    alias_map = {}  # the file's alias → the alias here (an existing repository, or the new one)
    new_aliases = set()  # aliases of repositories added by this import
    out["repos"], out["repos_enable"], out["repos_have"] = [], [], 0
    for r in data["repositories"]:
        if r.get("home_folder"):
            folder = under_home(r["home_folder"], home)
            url = ("dir:" + urllib.parse.quote(folder)) if folder else ""
            if not folder:
                out["todo"].append({"kind": "folder", "title": _("The folder “{folder}”").format(
                    folder="~/" + r["home_folder"]), "detail": _(
                        "A folder of programs on the other computer. Copy it into your home folder, then add it in "
                        "Programs from the web.")})
                continue
            key = norm_url(url)
            same = next((x for x in here_repos if (x.get("url") or "").startswith(("dir:", "file:"))
                         and os.path.realpath(urllib.parse.unquote(x["url"].split(":", 1)[1])) == folder), None)
        else:
            url, key = r["url"], norm_url(r["url"])
            same = by_url.get(key)
        if same is not None:
            alias_map[r["alias"]] = same["alias"]
            if same["enabled"] or not r["enabled"]:
                out["repos_have"] += 1
            else:
                out["repos_enable"].append({"alias": same["alias"], "name": same["name"]})
            continue
        if key in queued_urls:
            out["repos_have"] += 1
            continue
        alias = r["alias"]
        n = 2
        while alias in used:
            alias = f"{r['alias']}-{n}"
            n += 1
        used.add(alias)
        alias_map[r["alias"]] = alias
        new_aliases.add(alias)
        opts = {"url": url, "name": r["name"], "priority": r["priority"], "autorefresh": r["autorefresh"],
                "gpg": "default" if r.get("home_folder") else r["gpg"]}
        if not r["enabled"]:
            opts["enabled"] = False
        if r["keeppackages"]:
            opts["keeppackages"] = True
        out["repos"].append({"alias": alias, "name": r["name"], "url": url, "opts": opts, "keys": r["keys"],
                             "local": bool(r.get("home_folder")), "file_alias": r["alias"]})

    # ---- keys of the new repositories
    trusted = here.get("trusted") or set()
    file_keys = {k["fpr"]: k for k in data["keys"]}
    checks = here.get("key_check") or {}
    out["keys"], keyed = [], set()  # keyed: new aliases whose packages can be checked on the first run
    for r in out["repos"]:
        if r["local"] or r["opts"]["gpg"] == "off":
            keyed.add(r["alias"])
            continue
        ok = bool(r["keys"])
        for fpr in r["keys"]:
            if fpr in trusted:
                continue
            same = next((k for k in out["keys"] if k["fpr"] == fpr), None)
            if same is not None:  # one key for several repositories (a vendor's): it goes if any of them goes
                same["repos"].append(r["alias"])
                continue
            k = file_keys.get(fpr)
            check = checks.get(fpr)
            if k is None or not isinstance(check, dict):
                ok = False
                why = check if isinstance(check, str) else _("its key is not in the file")
                out["refused"].append((_("Signing key"), (k or {}).get("name") or fpr[-8:], why))
                continue
            out["keys"].append({"fpr": fpr, "name": check.get("name") or k["name"] or check.get("email") or
                                fpr[-8:], "email": check.get("email") or k.get("email") or "", "data": k["data"],
                                "for_repo": r["alias"], "repos": [r["alias"]]})
        if ok:
            keyed.add(r["alias"])
    out["keys_have"] = sum(1 for r in data["repositories"] for f in r["keys"] if f in trusted)

    # ---- packages (a lock here keeps a package out: zypper would refuse the whole install)
    here_locks = here.get("locks") or []
    lock_names = [lk["name"] for lk in here_locks if (lk.get("type") or "package") == "package"
                  and not lk.get("repo")]
    installed = here.get("installed") or set()
    available = here.get("available") or set()
    queued_pkgs = set(queued.get("packages") or ())
    titles = data.get("apps") or {}
    out["packages"], out["packages_missing"], out["packages_later"], out["packages_locked"] = [], [], [], []
    out["packages_have"] = 0
    for p in data["packages"]:
        name = p["name"]
        if name in installed or name in queued_pkgs:
            out["packages_have"] += 1
            continue
        if _fnmatch_any(name, lock_names):
            out["packages_locked"].append(name)
            continue
        alias = alias_map.get(p["repo"], "") if p["repo"] else ""
        if name in available:
            target = name
        elif alias in new_aliases and alias in keyed:
            target = f"{alias}:{name}"
        elif alias:  # a repository added now whose key is not known yet, or one that is enabled now
            out["packages_later"].append(name)
            continue
        else:
            out["packages_missing"].append(name)
            continue
        out["packages"].append({"name": name, "target": target, "title": titles.get(name, ""),
                                "repo": alias if target != name else "", "extra_repo": bool(alias)})

    # ---- locks. The queue adds locks before it installs: a lock of a package installed now would stop the
    # install, so it waits until the file is opened again (the package is there by then)
    have_locks = {(lk["name"], lk.get("range") or "", lk.get("type") or "package", lk.get("repo") or "")
                  for lk in here_locks}
    to_install = [p["name"] for p in out["packages"]]
    out["locks"], out["locks_have"], out["locks_later"] = [], 0, []
    for lk in data["locks"]:
        repo = ""
        if lk["repo"]:
            repo = alias_map.get(lk["repo"], "")
            if not repo:
                out["refused"].append((pgettext("noun", "Lock"), lk["name"],
                                       _("it is only for a repository that is not added")))
                continue
        if (lk["name"], lk["range"], lk["type"], repo) in have_locks:
            out["locks_have"] += 1
            continue
        target = f"{lk['name']} {lk['range']}" if lk["range"] else lk["name"]
        if lk["type"] == "package" and any(_fnmatch_any(n, [lk["name"]]) for n in to_install):
            out["locks_later"].append(target)
            continue
        out["locks"].append({"target": target, "type": lk["type"], "repo": repo})

    # ---- Flatpak
    fp_here = here.get("flatpak_apps") or set()
    remotes_here = here.get("flatpak_remotes") or []
    remotes_file = {(r["name"], r["installation"]): r for r in data["flatpak_remotes"]}
    out["flatpak"], out["flatpak_remotes"], out["flatpak_have"] = [], [], 0
    queued_fp = set(queued.get("flatpaks") or ())
    extra_pkgs = []
    if data["flatpak_apps"] and not here.get("flatpak"):
        extra_pkgs.append("flatpak")
    pending_remotes = {}  # (name, installation) → how it will be there: 'package' or 'queue'
    by_hand_remote = {}
    for a in data["flatpak_apps"]:
        if a["id"] in fp_here or a["id"] in queued_fp:
            out["flatpak_have"] += 1
            continue
        rf = remotes_file.get((a["remote"], a["installation"])) or next(
            (r for (n, _i), r in remotes_file.items() if n == a["remote"]), None)
        if a["eol"]:
            out["todo"].append({"kind": "flatpak-eol", "title": a["name"], "detail": _(
                "It is no longer on {remote}: install it from the copy you saved with “Save a copy…” in "
                "Applications (flatpak install --{installation} <file>).").format(
                    remote=(rf or {}).get("title") or a["remote"] or "?", installation=a["installation"]),
                "id": a["id"], "remote": (rf or {}).get("title") or a["remote"]})
            continue
        want = norm_url(rf["url"]) if rf else ""
        here_r = [r for r in remotes_here if r.get("name") == a["remote"]
                  or (want and norm_url(r.get("url") or "") == want)]
        mine = next((r for r in here_r if r.get("installation") == a["installation"]), None) or (
            here_r[0] if here_r else None)
        inst = a["installation"]
        remote = a["remote"]
        if mine is not None:
            remote, inst = mine["name"], mine.get("installation") or inst
        elif rf is not None and rf.get("flatpakrepo", "").startswith("https://dl.flathub.org/repo/") and (
                FLATHUB_PACKAGE in available or FLATHUB_PACKAGE in extra_pkgs):
            # openSUSE's package adds Flathub for every user before the queue installs the apps
            remote, inst = "flathub", "system"
            if FLATHUB_PACKAGE not in installed and FLATHUB_PACKAGE not in extra_pkgs:
                extra_pkgs.append(FLATHUB_PACKAGE)
            pending_remotes[("flathub", "system")] = "package"
        elif rf is not None and remote_add and (rf.get("flatpakrepo") or rf.get("key_fprs")):
            key = (rf["name"], rf["installation"])
            if key not in pending_remotes:
                pending_remotes[key] = "queue"
                out["flatpak_remotes"].append(dict(rf))
            remote, inst = rf["name"], rf["installation"]
        else:
            by_hand_remote.setdefault(a["remote"] or "?", (rf, []))[1].append(a["name"])
            continue
        ref = a["id"] + (f"//{a['branch']}" if a["branch"] != "stable" else "")
        out["flatpak"].append({"id": a["id"], "name": a["name"], "target": ref, "remote": remote,
                               "installation": inst, "remote_title": (rf or {}).get("title") or remote})
    for name, (rf, names) in by_hand_remote.items():
        where = (rf.get("flatpakrepo") or rf.get("url")) if rf else ""
        out["todo"].append({"kind": "flatpak-remote", "title": _("The Flatpak remote “{name}”").format(name=name)
                            if name != "?" else _("Flatpak apps whose remote the file does not name"),
                            "detail": (_("Add it in Flatpak → Remotes ({url}), then install: {apps}").format(
                                url=where, apps=", ".join(names)) if where else
                                _("It is not in the file. Install by hand: {apps}").format(apps=", ".join(names)))})
    for name in extra_pkgs:  # they go with the Flatpak apps (not in the list of packages to choose from)
        same = next((p for p in out["packages"] if p["name"] == name), None)
        if same is not None:
            same["for_flatpak"] = True
        elif name not in installed and name in available:
            out["packages"].append({"name": name, "target": name, "title": "", "repo": "", "for_flatpak": True})

    # ---- languages, zypp.conf, routine, protected apps
    have_loc = set(here.get("locales") or ())
    out["locales"] = [c for c in data["locales"] if c not in have_loc]
    out["zypp_conf"] = None
    eff, ours = here.get("zypp_conf_effective") or {}, here.get("zypp_conf_ours") or {}
    changes = [(k, eff.get(k, ""), v) for k, v in data["zypp_conf"].items() if eff.get(k) != v]
    if changes:
        settings = {k: v for k, v in ours.items() if zyppconf.validate(k, v)}
        settings.update({k: v for k, _now, v in changes})
        out["zypp_conf"] = {"settings": settings, "changes": changes}
    out["routine"] = data["routine"] if data["routine"] and data["routine"] != (here.get("routine") or "") else ""
    have_prot = set(here.get("protected_apps") or ())
    out["protected"] = [p for p in data["protected_apps"] if p not in have_prot]

    # ---- programs from the web
    have_web = {s.get("package") for s in here.get("web_sources") or []}
    out["web_sources"], out["web_have"] = [], 0
    for s in data["web_sources"]:
        if s["package"] in have_web:
            out["web_have"] += 1
            continue
        src = {k: v for k, v in s.items() if k != "home_folder"}
        src["folder"] = "~/" + s["home_folder"] if s.get("home_folder") else ""
        if src["type"] != "manual" and not src["folder"]:
            src["folder"] = "~/" + _("Programs from the web")
        out["web_sources"].append(src)
        if src["type"] == "manual":
            out["todo"].append({"kind": "web-manual", "title": s["package"], "detail": s.get("note") or _(
                "A program from the web that Zypp Desk does not follow: download it by hand.")})

    # ---- by hand (what the file lists)
    for t in data["todo"]:
        out["todo"].append(_todo_words(t))
    if out["packages_later"]:
        n = len(out["packages_later"])
        out["todo"].append({"kind": "later", "title": ngettext(
            "{n} package from a repository zypper has not read yet", "{n} packages from repositories zypper has "
            "not read yet", n).format(n=n), "detail": _("After you apply the queue, open this file again: then "
                                                         "Zypp Desk finds them.") + " " + ", ".join(
            out["packages_later"][:20]) + (" …" if n > 20 else "")})
    if out["locks_later"]:
        n = len(out["locks_later"])
        out["todo"].append({"kind": "later", "title": ngettext(
            "{n} lock of a package installed now", "{n} locks of packages installed now", n).format(n=n),
            "detail": _("A lock would stop the install. After you apply the queue, open this file again: then Zypp "
                        "Desk adds it.") + " " + ", ".join(out["locks_later"][:20]) + (" …" if n > 20 else "")})
    return out


def _todo_words(t):
    """One item of the file's list by hand, in words (the window's language, not the other computer's)."""
    k, name = t["kind"], t["name"]
    where = t.get("where") or ""
    if k == "installer":
        detail = (_("Installed with its own installer, in {folder}.").format(folder=where) if where else
                  _("Installed with its own installer.")) + " " + _(
            "Download it again from its maker and run its installer (Own installers can run it).")
        return {"kind": k, "title": name, "detail": detail}
    if k == "appimage":
        detail = _("An AppImage in {folder}.").format(folder=where) if where else _("An AppImage.")
        detail += " " + (_("New versions are published on github.com/{repo}.").format(repo=t["github"])
                         if t.get("github") else _("Copy the file, or download it again from its maker."))
        return {"kind": k, "title": name, "detail": detail}
    if k == "webapp":
        browser = t.get("browser") or _("a browser")
        detail = (_("A web app of {browser} ({site}).").format(browser=browser, site=t["host"]) if t.get("host")
                  else _("A web app of {browser}.").format(browser=browser))
        return {"kind": k, "title": name, "detail": detail + " " + _("Install it again from the browser."),
                "browser": t.get("browser") or ""}
    if k == "file-package":
        return {"kind": k, "title": name, "detail": _("A package installed from a file, with nothing that says "
                                                      "where it comes from: download it again from its maker.")}
    why = (_("Its address has a user name, a password or a token, which are never written to the file.")
           if t.get("reason") == "secret" else _("Its kind of address cannot go in the file."))
    return {"kind": k, "title": _("The repository “{name}”").format(name=name),
            "detail": why + " " + _("Add it by hand in Repositories.")}
