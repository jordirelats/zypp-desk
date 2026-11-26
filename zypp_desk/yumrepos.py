# SPDX-License-Identifier: GPL-3.0-or-later
"""Repositories left for Fedora: the .repo files in /etc/yum.repos.d, which zypper never reads.

A program made for Fedora can install its own repository as /etc/yum.repos.d/<name>.repo (ChatGPT/Codex does:
chatgpt.repo). dnf reads that folder; zypper only reads /etc/zypp/repos.d, so the program would never update. This
module finds those repositories, tells which ones zypper does not have yet (compared by address), which package
left each one, and which of their signing keys rpm does not have yet.

Only some can be used on openSUSE: an address on the web (http or https) whose only variables are $basearch and
$arch. An address with $releasever is made for one version of Fedora, and a metalink or mirrorlist is a list of
mirrors that zypper cannot read; those are listed as "cannot be used", with the reason. So is an address that
carries settings for zypper in its query (?ssl_verify=no turns off the https check), and an http address whose
packages are not signed either: anybody on the network could change what root installs (security review 0.5, #2).
An http address is tried with https first (scan), and the https one is used when it answers.

The signature checks follow the file: gpgcheck (the packages) and repo_gpgcheck (the list of packages) become the
queue's 'default' (both), 'allow-unsigned-repo' (only the packages), 'allow-unsigned-package' (only the list:
zypper addrepo --gpgcheck-allow-unsigned-package keeps the list checked, zypper review 0.5, #7) or 'off'.

Nothing here changes the system: it reads the files, asks rpm as the normal user, and reads key files with gpg in a
throwaway folder (keys.py). Adding a repository and importing its key go through the queue.
"""

import configparser
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .backend import ENV_C
from .i18n import _, user_lang

YUM_DIR = Path("/etc/yum.repos.d")  # tests point it at a scratch folder
DESKTOP_DIR = "/usr/share/applications/"

# dnf's $basearch for each processor (uname -m)
_BASEARCH = {"i386": "i386", "i486": "i386", "i586": "i386", "i686": "i386", "athlon": "i386",
             "x86_64": "x86_64", "aarch64": "aarch64", "armv6l": "armhfp", "armv7l": "armhfp", "armv7hl": "armhfp",
             "armv8l": "armhfp", "ppc64le": "ppc64le", "ppc64": "ppc64", "s390x": "s390x", "riscv64": "riscv64"}
_VAR_RE = re.compile(r"\$(\{)?([A-Za-z_][A-Za-z0-9_]*)(?(1)\})")
_ALIAS_BAD = re.compile(r"[^A-Za-z0-9._:+@-]+")
_TRUE = ("1", "yes", "true", "on")
_FALSE = ("0", "no", "false", "off")
# options zypper reads from the query of an address (libzypp's media options): they change how it checks the server
# (ssl_verify=no) or where it downloads through (proxy=…). A vendor's .repo file has no business setting them.
_URL_OPTION_RE = re.compile(r"ssl_.*|proxy.*|mediahandler|credentials", re.I)
HTTPS_TIMEOUT = 8
HTTPS_TTL = 3600  # seconds: whether an address answers over https is asked again after an hour
_https_memo = {}


def machine():
    from .localrpm import machine_arch
    return machine_arch()


def basearch(mach=None):
    m = mach or machine()
    return _BASEARCH.get(m, m)


def expand(text, mach=None):
    """$basearch, ${basearch}, $arch and ${arch} replaced, as dnf does. Returns (text, [other variables left])."""
    m = mach or machine()
    values = {"basearch": basearch(m), "arch": m}
    left = []

    def sub(mt):
        name = mt.group(2)
        if name in values:
            return values[name]
        left.append(name)
        return mt.group(0)

    return _VAR_RE.sub(sub, text or ""), left


def norm_url(url, mach=None):
    """An address in the form used to compare two of them: variables replaced, http and https alike, the host in
    lower case, no default port, no query, no repeated or trailing slashes."""
    u, _left = expand((url or "").strip(), mach)
    u = urllib.parse.unquote(u)
    try:
        parts = urllib.parse.urlsplit(u)
        port = parts.port
    except ValueError:
        return u.rstrip("/").lower()
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        if port in (80, 443):
            port = None
        scheme = "web"
    host = (parts.hostname or "").lower()
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    return f"{scheme}://{host}{f':{port}' if port else ''}{path}"


def _flag(value, default):
    v = (value or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


def _split_list(value):
    """A yum list (baseurl, gpgkey): separated by spaces, commas or new lines."""
    return [x for x in re.split(r"[\s,]+", value or "") if x]


def _safe_file(path):
    """Whether a file is only writable by root or by you (a file anybody can change must not decide what root
    installs)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_uid in (0, os.getuid()) and not st.st_mode & 0o022


def alias_for(section, used=()):
    """A zypper alias from the section name: letters, digits and . _ : + @ - only, not starting with . or -, and
    not one already in use (then -2, -3…)."""
    base = _ALIAS_BAD.sub("-", section or "").strip(".-") or "fedora-repo"
    alias, n = base[:60], 2
    used = set(used)
    while alias in used:
        alias, n = f"{base[:56]}-{n}", n + 1
    return alias


def _key_source(raw, mach):
    """One gpgkey= entry: {url, kind ('file', 'https' or 'other'), path}."""
    url, left = expand(raw, mach)
    low = url.lower()
    if left:
        return {"url": url, "kind": "other", "path": ""}
    if low.startswith("file:"):
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        return {"url": url, "kind": "file" if path.startswith("/") else "other", "path": path}
    if low.startswith("https://"):
        return {"url": url, "kind": "https", "path": ""}
    return {"url": url, "kind": "other", "path": ""}


def _section_entry(path, section, sec, mach):
    name, _left = expand(sec.get("name") or "", mach)
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name).strip()[:120]  # one line: it becomes the name in zypper
    e = {"file": str(path), "section": section, "id": f"{path}#{section}", "name": name or section,
         "url": "", "raw_url": "", "usable": True, "why": "",
         "gpgcheck": _flag(sec.get("gpgcheck"), True), "repo_gpgcheck": _flag(sec.get("repo_gpgcheck"), False),
         "gpgkeys": [_key_source(k, mach) for k in _split_list(sec.get("gpgkey"))],
         "in_zypper": None, "package": "", "program": "", "icon": ""}
    e["gpg"] = (("default" if e["repo_gpgcheck"] else "allow-unsigned-repo") if e["gpgcheck"] else
                ("allow-unsigned-package" if e["repo_gpgcheck"] else "off"))
    e["plain_http"] = False  # scan: True when only http answers
    e["https_from"] = ""  # scan: the http address of the file, when the same one with https is used instead
    urls = _split_list(sec.get("baseurl"))
    web = [u for u in urls if u.lower().startswith(("https://", "http://"))]
    if web:
        e["raw_url"] = web[0]
        url, left = expand(web[0], mach)
        e["url"] = url
        if "releasever" in left or any(v.startswith("releasever") for v in left):
            e["usable"], e["why"] = False, _("Its address is made for one version of Fedora ($releasever).")
        elif left:
            e["usable"], e["why"] = False, _("Its address uses {variable}, which only dnf knows.").format(
                variable="$" + left[0])
        else:
            from .repoprio import url_problem
            problem = url_problem(url) or _url_options_problem(url)
            if problem:
                e["usable"], e["why"] = False, problem
    elif urls:
        e["raw_url"] = urls[0]
        e["usable"], e["why"] = False, _("Its address is not on the web (http or https).")
    elif sec.get("metalink") or sec.get("mirrorlist"):
        e["usable"], e["why"] = False, _("It only has a list of mirrors (metalink or mirrorlist), which zypper "
                                          "cannot read.")
    else:
        e["usable"], e["why"] = False, _("It has no address.")
    if e["usable"] and not _safe_file(path):
        e["usable"], e["why"] = False, _("Other users can change this file, so it is not used.")
    return e


def _url_options_problem(url):
    """Why an address cannot be used because of the options in its query ('' when it has none of them)."""
    try:
        query = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return _("This is not an address.")
    found = sorted({k for k, _v in query if _URL_OPTION_RE.fullmatch(k)})
    if not found:
        return ""
    return _("Its address carries settings for zypper ({options}) that change how it checks the server or where it "
             "downloads from.").format(options=", ".join(found))


def https_answers(url):
    """Whether the same address with https serves a repository (repodata/repomd.xml answers, without leaving https).
    Network; remembered for an hour."""
    https = "https://" + url[len("http://"):] if url.lower().startswith("http://") else url
    memo = _https_memo.get(https)
    if memo and time.monotonic() - memo[0] < HTTPS_TTL:
        return memo[1]
    from .repocheck import _join
    from .websources import USER_AGENT, https_open
    target = _join(https, "repodata/repomd.xml")
    ok = False
    for method in ("HEAD", "GET"):
        headers = {"User-Agent": USER_AGENT}
        if method == "GET":
            headers["Range"] = "bytes=0-1023"
        try:
            with https_open(urllib.request.Request(target, method=method, headers=headers),
                            timeout=HTTPS_TIMEOUT) as r:
                ok = 200 <= r.status < 300 and r.geturl().startswith("https://")
                if method == "GET":
                    r.read(1024)
            break
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (400, 403, 405, 406, 501):
                continue  # some servers only refuse HEAD
            break
        except (urllib.error.URLError, OSError, ValueError):
            break
    _https_memo[https] = (time.monotonic(), ok)
    return ok


def secure_transport(entry):
    """For a usable repository whose file says http: use the same address with https when it answers; otherwise it
    stays http, and one whose packages are not signed either cannot be used (security review 0.5, #2). Network."""
    if not entry["usable"] or not entry["url"].lower().startswith("http://"):
        return entry
    if https_answers(entry["url"]):
        entry["https_from"] = entry["url"]
        entry["url"] = "https://" + entry["url"][len("http://"):]
        return entry
    entry["plain_http"] = True
    if entry["gpg"] == "off":
        entry["usable"], entry["why"] = False, _(
            "Its address is not encrypted (http, and https does not answer there), and its packages are not "
            "signed: anybody on the network could change what zypper installs as root.")
    return entry


def read_folder(folder=None, mach=None):
    """Every enabled repository of the .repo files in the folder (default YUM_DIR), usable or not."""
    folder = Path(folder or YUM_DIR)
    mach = mach or machine()
    try:
        files = sorted(f for f in folder.glob("*.repo") if f.is_file())
    except OSError:
        return []
    out = []
    for f in files:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for section in cp.sections():
            if section == "main":
                continue  # dnf's own settings, not a repository
            sec = cp[section]
            if not _flag(sec.get("enabled"), True):
                continue
            out.append(_section_entry(f, section, sec, mach))
    return out


def _owners(files):
    """{file: package that owns it} (rpm -qf as the normal user; files of no package are left out)."""
    files = sorted(set(files))
    if not files:
        return {}
    try:
        p = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\n", "--", *files], capture_output=True, text=True,
                           env=ENV_C, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    lines = p.stdout.splitlines()
    if len(lines) != len(files):  # one line a file; otherwise they cannot be matched safely
        return {}
    return {f: n.strip() for f, n in zip(files, lines) if n.strip() and " " not in n.strip()}


def _lang_keys():
    lang = user_lang().split(".")[0].split("@")[0]
    if lang in ("C", "POSIX", ""):
        return []
    return [lang] + ([lang.split("_")[0]] if "_" in lang else [])


def program_of(package):
    """(name, icon) of the program a package installs, from its .desktop file (ChatGPT for chatgpt), or
    (package, '') when it has none."""
    try:
        p = subprocess.run(["rpm", "-q", "--qf", "[%{FILENAMES}\n]", "--", package], capture_output=True, text=True,
                           env=ENV_C, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return package, ""
    langs = _lang_keys()
    for path in p.stdout.splitlines():
        if not (path.startswith(DESKTOP_DIR) and path.endswith(".desktop")):
            continue
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        try:
            cp.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        if not cp.has_section("Desktop Entry"):
            continue
        d = cp["Desktop Entry"]
        if d.get("NoDisplay", "").lower() == "true":
            continue
        name = next((d.get(f"Name[{lg}]") for lg in langs if d.get(f"Name[{lg}]")), None) or d.get("Name", "")
        name = re.sub(r"[\x00-\x1f\x7f]+", " ", name).strip()
        if name:
            return name[:80], d.get("Icon", "").strip()
    return package, ""


def scan(zypper_repos, folder=None, mach=None):
    """The repositories for Fedora on this computer, each a dict:

    file, section, id (file#section, for "Do not add it"), name, url (variables replaced; https when the file says
    http and https answers), raw_url, usable, why (when it cannot be used), gpgcheck, repo_gpgcheck, gpg (the queue's
    signature setting: 'default', 'allow-unsigned-repo', 'allow-unsigned-package' or 'off'), plain_http (only http
    answers), https_from (the http address of the file, when https is used instead), gpgkeys [{url, kind, path}],
    in_zypper (the alias of the zypper repository at the same address, or None), package (that left the file, ''
    when none), program and icon (what to call it).

    zypper_repos: backend.repos(). Runs rpm, and asks the network about http addresses: call it in a thread."""
    mach = mach or machine()
    entries = read_folder(folder, mach)
    if not entries:
        return []
    have = {}
    for r in zypper_repos or []:
        have.setdefault(norm_url(r.get("url") or "", mach), r.get("alias"))
    for e in entries:
        if e["url"]:
            e["in_zypper"] = have.get(norm_url(e["url"], mach))
        if not e["in_zypper"]:  # one zypper already has needs no question about http
            secure_transport(e)
    owners = _owners(e["file"] for e in entries)
    programs = {}
    for e in entries:
        pkg = owners.get(e["file"], "")
        e["package"] = pkg
        if pkg and pkg not in programs:
            programs[pkg] = program_of(pkg)
        e["program"], e["icon"] = programs.get(pkg, ("", ""))
    return entries


def missing(entries, dismissed=(), queued_urls=(), mach=None):
    """The usable ones that zypper does not have, that are not waiting in the queue, and that were not put aside
    with "Do not add it"."""
    queued = {norm_url(u, mach) for u in queued_urls}
    dismissed = set(dismissed)
    return [e for e in entries if e["usable"] and not e["in_zypper"] and e["id"] not in dismissed
            and norm_url(e["url"], mach) not in queued]


# ------------------------------------------------------------------ the signing keys

def prepare_keys(entry, queued=()):
    """In a thread: the signing keys of a repository that rpm does not have yet, read and checked.

    Returns {"import": [key], "have": [fingerprint], "problems": [(source, text)]}. Each key to import is the dict
    of keys.inspect_key plus source (the address it came from), path (the local file, '' when downloaded), owner
    (the package that installed the file) and data (the key as gpg writes it out again: what the queue keeps for
    the helper). A key file the package installed is read from disk; an https one is downloaded. queued:
    fingerprints already waiting in the queue (they count as there)."""
    from . import keys as keyring
    out = {"import": [], "have": [], "problems": []}
    if not entry["gpgkeys"]:
        return out
    installed = keyring.installed_keys()
    seen = set()
    owners = _owners(k["path"] for k in entry["gpgkeys"] if k["kind"] == "file")
    for src in entry["gpgkeys"]:
        try:
            if src["kind"] == "file":
                if not _safe_file(src["path"]):
                    raise ValueError(_("Other users can change this file, or it does not exist."))
                with open(src["path"], "rb") as f:
                    data = f.read(keyring.MAX_KEY + 1)
            elif src["kind"] == "https":
                data = keyring.fetch_url(src["url"])
            else:
                raise ValueError(_("Zypp Desk only takes keys from a file on this computer or from an https "
                                   "address."))
            found = keyring.inspect_key(data)
            if not found:
                raise ValueError(_("There is no signing key in it."))
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            out["problems"].append((src["url"], str(e)))
            continue
        useful = False
        for k in found:
            fpr = k["fpr"]
            if fpr in seen:
                useful = True
                continue
            seen.add(fpr)
            if fpr in installed or fpr in queued:
                out["have"].append(fpr)
                useful = True
                continue
            if k["revoked"] or k["expired"]:
                continue  # a vendor's file can still hold its old key: it signs nothing new
            useful = True
            try:
                k = dict(k, data=keyring.armored(data, fpr))
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                out["problems"].append((src["url"], str(e)))
                continue
            k.update(source=src["url"], path=src["path"], owner=owners.get(src["path"], ""))
            out["import"].append(k)
        if not useful:
            out["problems"].append((src["url"], _("Its key has expired or was revoked by its owner.")))
    return out
