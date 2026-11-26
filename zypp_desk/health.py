# SPDX-License-Identifier: GPL-3.0-or-later
"""Health check: what is worth a look on this computer, from the most to the least important (Maintenance).

No widgets here, and nothing changes: every check only reads (rpm as the user, sysfs, the .repo files, flatpak,
zypper through backend.run_c) and is meant to run in a thread. The page turns each finding into a row with a
button; a button opens the right place, or puts changes in the queue, which shows the exact commands first.

A finding is a dict:
- severity: "high", "medium", "low" or "ok" (the list goes in this order);
- title and text: one line in bold and one plain sentence;
- action: what its button does ("firmware", "show-app", "vendor", "repos", "keys", "orphans", "verify",
  "duplicates") or None, and button: its text;
- details: lines shown under "Details" (names as plain text);
- data: what the action needs; weight: within one severity, the bigger first (bytes, a count).

Where the facts come from:
- Firmware: each kernel-firmware-* package says which devices it is for (Supplements: modalias(…), the same data
  zypper uses to put firmware back with 'zypper inr'), and the devices are the modalias files under /sys/devices.
  The firmware a driver module lists (modinfo) is not used as proof: a generic driver lists the firmware of every
  vendor (btusb asks for Realtek and MediaTek files on a computer with an Intel adapter). See firmware_plan().
- Vendors and orphaned packages: the packages of the enabled repositories, read from zypper's own cache with
  libsolv (python3-solv, in a short process of its own) when it is installed, else from one 'zypper search
  --details' (no vendors there: the vendor of a repository is then worked out from the installed packages that came
  from it). Only the installed names are kept.
- Installed packages: one 'rpm -qa' (name, version, vendor, size).
- Signing keys: keys.py (rpm's keys read with gpg, the key that signed each repository's metadata).
- Flatpak: flatpak.py (the "End-of-life" field of 'flatpak info').
- Room: rpm's sizes, 'flatpak list', the package cache and the old kernels (diskspace.py).
"""

import bisect
import configparser
import datetime
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import backend, diskspace, flatpak, keys
from .i18n import _, ngettext

HIGH, MEDIUM, LOW, OK = "high", "medium", "low", "ok"
RANK = {HIGH: 0, MEDIUM: 1, LOW: 2, OK: 3}

SYS = "/sys"
REPOS_D = "/etc/zypp/repos.d"
SOLV_CACHE = "/var/cache/zypp/solv"
RAW_CACHE = "/var/cache/zypp/raw"
LOCKS_FILE = "/etc/zypp/locks"
VENDORS_DIRS = ("/usr/etc/zypp/vendors.d", "/etc/zypp/vendors.d")
FIRMWARE_DIRS = ("/usr/lib/firmware", "/lib/firmware")
FIRMWARE_ALL = "kernel-firmware-all"
FW_PREFIX = "kernel-firmware-"
KEY_SOON_DAYS = 60
SYSTEM_REPOS = ("@System", "(System Packages)")
BRIDGE_VENDOR = "Zypp Desk"  # bridge.VENDOR: packages made here, in no repository on purpose
X86 = ("x86_64", "i386", "i486", "i586", "i686")

_cache_lock = threading.Lock()
_cache = {}


def finding(severity, title, text, action=None, button="", details=(), data=None, weight=0, check=""):
    return dict(severity=severity, title=title, text=text, action=action, button=button, details=list(details),
                data=data if data is not None else {}, weight=weight, check=check)


def ranked(findings):
    """Most important first: the severity, then the weight (bytes, a count), then the title."""
    return sorted(findings, key=lambda f: (RANK.get(f["severity"], 9), -(f.get("weight") or 0), f["title"]))


def forget():
    """Throw away what is kept for the session (after a change, or "Check again")."""
    with _cache_lock:
        _cache.clear()


def _cached(key, fn, refresh=False):
    """fn() once per session (until forget() or refresh); every thread that asks meanwhile waits for it."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None or refresh:
            entry = _cache[key] = {"lock": threading.Lock(), "done": False, "value": None}
    with entry["lock"]:
        if not entry["done"]:
            entry["value"] = fn()  # an exception leaves it undone: the next call tries again
            entry["done"] = True
        return entry["value"]


def _run(args, timeout=120):
    """A read-only command as the user, in English; '' when it cannot run."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace", env=backend.ENV_C,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout


def nice_size(n):
    """'1.2 GB', '819 MB': sizes in the sentences of this module, unless the window gave its own way (Qt's, in
    the user's language) with set_size_format."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            if unit in ("B", "KB", "MB"):
                return f"{n:.0f} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


_size_format = [None]


def set_size_format(fn):
    """fn(bytes) → text: how the sentences write sizes (the window passes Qt's, which follows the language)."""
    _size_format[0] = fn


def size_text(n):
    fn = _size_format[0]
    if fn is not None:
        try:
            return fn(n)
        except Exception:
            pass
    return nice_size(n)


def _join(names, limit=4):
    """'a, b, c and 2 more' (names as they are)."""
    names = list(names)
    if len(names) <= limit:
        return ", ".join(names)
    rest = len(names) - limit
    return ngettext("{list} and {n} more", "{list} and {n} more", rest).format(list=", ".join(names[:limit]), n=rest)


# ====================================================================== installed packages and repositories

def installed_packages():
    """[dict(name, evr, arch, vendor, size)] of every installed package but the keys (one 'rpm -qa')."""
    fmt = "%{NAME}\t%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\t%{ARCH}\t%{VENDOR}\t%{LONGSIZE}\n"
    out = []
    for line in _run(["rpm", "-qa", "--qf", fmt], timeout=180).splitlines():
        f = line.split("\t")
        if len(f) != 5 or f[0] == "gpg-pubkey":
            continue
        out.append(dict(name=f[0], evr=f[1], arch=f[2], vendor=_norm_vendor(f[3]),
                        size=int(f[4]) if f[4].isdigit() else 0))
    if not out:  # an empty list would read as "nothing orphaned, nothing big"
        raise RuntimeError(_("rpm could not list the installed packages."))
    return out


def _norm_vendor(v):
    v = (v or "").strip()
    return "" if v in ("(none)", "<NULL>", "None") else v


def repo_index(repos, wanted=None):
    """What the enabled repositories offer: dict(source, by_name, installed_in, solvables).

    by_name: {name: [(alias, edition, arch, vendor)]}, only for the names in wanted (the checks only ask about
    installed packages, and the repositories have tens of thousands of names): a set, a function that returns one
    (called when the names are needed, so that zypper can read the repositories meanwhile), or None (every name);
    vendor is None when it is not known (read through zypper). installed_in: {name: alias} of the installed
    packages whose installed version is in a repository (only through zypper: it is how the vendor of a repository
    is worked out then). solvables: how many packages were read, wanted or not (0: none could be read). Source
    packages, patterns and products are left out."""
    enabled = [r for r in repos if r.get("enabled")]
    try:
        return _index_solv(enabled, wanted)
    except Exception:  # no python3-solv, a cache that cannot be read, another libsolv version…
        return _index_zypper(enabled, wanted)


# libsolv keeps a whole repository in memory (openSUSE's main one: about 60 MB, whatever is asked of it) and never
# frees a pool on its own. The repositories are read by a short Python process of their own, one at a time, which
# prints only the wanted names: that memory is never the window's, and all of it goes back when the process ends.
# One such process at a time in the whole window.
_solv_lock = threading.Lock()

SOLV_READER = r"""
import json, sys
try:
    import solv
except ImportError:
    sys.exit(3)
job = json.load(sys.stdin)
wanted = None if job["wanted"] is None else set(job["wanted"])
count = 0
for alias, path in job["repos"]:
    pool = solv.Pool()
    try:
        f = solv.xfopen(path)
        if not f:
            sys.exit(4)
        repo = pool.add_repo(alias)
        ok = repo.add_solv(f)
        f.close()
        if not ok:
            sys.exit(5)
        for s in repo.solvables:
            name, arch = s.name, s.arch
            if ":" in name or arch in ("src", "nosrc"):
                continue
            count += 1
            if wanted is None or name in wanted:
                sys.stdout.write(json.dumps([alias, name, s.evr, arch, s.vendor]) + "\n")
    finally:
        pool.free()
sys.stdout.write(json.dumps({"solvables": count}) + "\n")
"""


def _index_solv(enabled, wanted=None):
    """repo_index() from zypper's own cache of each repository (python3-solv), read by SOLV_READER."""
    if not _has_solv():
        raise ImportError("solv")
    if callable(wanted):
        wanted = wanted()
    repos = []
    for r in enabled:
        alias = r["alias"]
        if not alias or "/" in alias or alias in (".", ".."):
            raise ValueError(alias)
        repos.append([alias, os.path.join(SOLV_CACHE, alias, "solv")])
    job = json.dumps(dict(repos=repos, wanted=None if wanted is None else sorted(wanted)))
    with _solv_lock:
        p = subprocess.run([sys.executable or "python3", "-I", "-c", SOLV_READER], input=job, capture_output=True,
                           text=True, errors="replace", timeout=300)
    if p.returncode != 0:  # no python3-solv (3), a cache zypper would build again (4), one that cannot be read (5)
        raise RuntimeError(f"libsolv: {p.returncode} {p.stderr.strip()[-300:]}")
    by_name, count = {}, None
    for line in p.stdout.splitlines():
        row = json.loads(line)
        if isinstance(row, dict):
            count = row["solvables"]
            continue
        alias, name, evr, arch, vendor = row
        by_name.setdefault(name, []).append((alias, evr, arch, _norm_vendor(vendor)))
    if count is None:
        raise RuntimeError("libsolv: no answer")
    return dict(source="solv", by_name=by_name, installed_in={}, solvables=count)


FEW_NAMES = 50  # up to this many wanted names, zypper is asked about those names only


def _index_zypper(enabled, wanted=None):
    args = ["search", "--details", "-t", "package"]
    if wanted is not None and not callable(wanted) and len(wanted) <= FEW_NAMES:
        args += ["--match-exact", "--", *sorted(wanted)]
    try:
        return _index_rows(enabled, wanted, _zypper_solvables(args))
    except ET.ParseError:  # not only XML (backend.xml finds the XML inside the text)
        root, _code = backend.xml(args)
        return _index_rows(enabled, wanted, (s.attrib for s in root.iter("solvable")))


def _index_rows(enabled, wanted, solvables):
    """repo_index() from the attributes of zypper's <solvable> elements."""
    alias_of = {}
    for r in enabled:
        alias_of.setdefault(r["name"], r["alias"])
        alias_of.setdefault(r["alias"], r["alias"])
    by_name, installed_in = {}, {}
    count = 0
    for s in solvables:
        repo = s.get("repository", "")
        if repo in SYSTEM_REPOS or repo not in alias_of:
            continue
        name, arch = s.get("name", ""), s.get("arch", "")
        if not name or arch in ("src", "nosrc"):
            continue
        count += 1
        if callable(wanted):  # zypper has read the repositories: by now the installed packages are listed too
            wanted = wanted()
        if wanted is not None and name not in wanted:
            continue
        alias = alias_of[repo]
        by_name.setdefault(name, []).append((alias, s.get("edition", ""), arch, None))
        if s.get("status") == "installed":
            installed_in.setdefault(name, alias)
    return dict(source="zypper", by_name=by_name, installed_in=installed_in, solvables=count)


def _zypper_solvables(args, timeout=600):
    """The attributes of each <solvable> of 'zypper --xmlout … <args>' (as backend.xml runs it), one by one as zypper
    prints them: every package of every repository is about 9 MB of XML, and tens of MB as a tree, which glibc then
    keeps. Raises what backend.xml raises when zypper fails; ET.ParseError when the output is not only XML."""
    import tempfile
    with backend.ZYPPER_SLOTS, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["zypper", "--xmlout", "--non-interactive", "--no-refresh", *args],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, env=backend.ENV_C)
        killed = []
        timer = threading.Timer(timeout, lambda: (killed.append(True), p.kill()))
        timer.start()
        msgs, parents = [], []
        try:
            for event, el in ET.iterparse(p.stdout, events=("start", "end")):
                if event == "start":
                    parents.append(el)
                    continue
                parents.pop()
                if el.tag == "solvable":
                    yield el.attrib
                    if parents:
                        parents[-1].remove(el)  # nothing keeps the packages already read
                elif el.tag == "message":
                    msgs.append((el.get("type", "info"), (el.text or "").strip()))
        finally:
            timer.cancel()
            p.stdout.close()
            code = p.wait()
        err.seek(0)
        stderr = err.read().decode("utf-8", "replace")
    if killed:
        raise subprocess.TimeoutExpired(p.args, timeout)
    if code not in backend.OK_CODES and code != backend.EXIT_NO_MATCH:
        errs = [t for k, t in msgs if k == "error"] or [stderr.strip()]
        raise backend.ZypperError("\n".join(e for e in errs if e) or
                                  _("zypper exited with code {code}.").format(code=code))


def cached_repos(refresh=False):
    return _cached("repos", backend.repos, refresh)


def _installed_names():
    """The names the checks ask the index about (the installed packages, and kernel-firmware-all for the firmware
    lock), or None when rpm cannot list them (then the index keeps every name, as the checks will fail anyway)."""
    try:
        names = {p["name"] for p in cached_installed()}
    except Exception:
        return None
    names.add(FIRMWARE_ALL)
    return names


def cached_index(refresh=False):
    return _cached("index", lambda: repo_index(cached_repos(), _installed_names), refresh)


_solv_found = []


def _has_solv():
    """Whether python3-solv is there (without loading it)."""
    if not _solv_found:
        import importlib.util
        try:
            _solv_found.append(importlib.util.find_spec("solv") is not None)
        except Exception:  # (a broken module)
            _solv_found.append(False)
    return _solv_found[0]


def index_of(names):
    """An index with these names: the session's index when it has been read, or is being read through zypper (a
    second zypper would cost more than waiting), else a small one of these names only, not kept (the firmware plan
    only asks about kernel-firmware-all: no need to wait for a whole 'rpm -qa')."""
    with _cache_lock:
        entry = _cache.get("index")
    if entry is not None and (entry["done"] or not _has_solv()):
        return cached_index()
    return repo_index(cached_repos(), set(names))


def cached_installed(refresh=False):
    return _cached("installed", installed_packages, refresh)


# ====================================================================== vendors

def vendor_groups(dirs=VENDORS_DIRS):
    """The vendors zypper treats as one (vendors.d), lower case; openSUSE and SUSE always go together."""
    groups = [["suse", "opensuse"]]
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*"))):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
            except (configparser.Error, OSError, UnicodeDecodeError):
                continue
            if cp.has_option("main", "vendors"):
                g = [v.strip().lower() for v in cp.get("main", "vendors").split(",") if v.strip()]
                if g:
                    groups.append(g)
    return groups


def _obs_root(v):
    """'obs://build.opensuse.org/multimedia:proaudio' → 'build.opensuse.org/multimedia'."""
    host, _sep, project = v[len("obs://"):].partition("/")
    return host + "/" + project.split(":")[0]


def same_vendor(a, b, groups):
    """Whether zypper may update a package of vendor a with one of vendor b. When in doubt, yes: a wrong "no"
    would propose changes nobody needs."""
    a, b = _norm_vendor(a).lower(), _norm_vendor(b).lower()
    if a == b:
        return True
    if not a or not b:
        return False
    for g in groups:
        if any(a.startswith(e) for e in g) and any(b.startswith(e) for e in g):
            return True
    if a.startswith("obs://") and b.startswith("obs://"):
        return _obs_root(a) == _obs_root(b)
    return False


def _arch_ok(inst, cand):
    return inst == cand or "noarch" in (inst, cand)


def stuck_packages(installed, index, repos, groups):
    """Installed packages that zypper never updates because no enabled repository has them from their vendor,
    while some repository has them from another one (leftovers after switching codec repositories, say).

    Each one: dict(name, arch, edition, vendor, alias, repo, cand_edition, cand_vendor, older, downgrade).
    alias/repo: where it would come from (the best priority, then the newest); older: that version is older than
    the installed one (switching needs --oldpackage); downgrade: even its version (not only the release) is older,
    so switching is not proposed. A package whose candidates' vendor is not known is left out."""
    prio = {r["alias"]: r.get("priority", 99) for r in repos}
    names = {r["alias"]: r.get("name") or r["alias"] for r in repos}
    by_name = index["by_name"]
    inferred = {}
    if index.get("installed_in"):
        # through zypper: the vendors of the installed packages that came from each repository
        vend = {p["name"]: p["vendor"] for p in installed}
        counts = {}
        for name, alias in index["installed_in"].items():
            if name in vend:
                counts.setdefault(alias, Counter())[vend[name].lower()] += 1
        inferred = {alias: set(c) for alias, c in counts.items()}
    out, seen = [], set()
    for p in installed:
        if p["name"] in seen or p["vendor"] == BRIDGE_VENDOR:
            continue
        cands = [c for c in by_name.get(p["name"], ()) if _arch_ok(p["arch"], c[2])]
        if not cands:
            continue
        good, unknown = False, False
        for alias, _ed, _arch, vendor in cands:
            vendors = [vendor] if vendor is not None else list(inferred.get(alias, ()))
            if not vendors:
                unknown = True
            elif any(same_vendor(p["vendor"], v, groups) for v in vendors):
                good = True
                break
        if good or unknown:
            continue
        best = cands[0]
        for c in cands[1:]:
            pc, pb = prio.get(c[0], 99), prio.get(best[0], 99)
            if pc < pb or (pc == pb and backend.vercmp(c[1], best[1]) > 0):
                best = c
        alias, edition, _arch, vendor = best
        if vendor is None:
            vendor = ", ".join(sorted(inferred.get(alias, ()))) or ""
        seen.add(p["name"])
        older = backend.vercmp(edition, p["evr"]) < 0
        out.append(dict(name=p["name"], arch=p["arch"], edition=p["evr"], vendor=p["vendor"], alias=alias,
                        repo=names.get(alias, alias), cand_edition=edition, cand_vendor=vendor, older=older,
                        downgrade=older and backend.vercmp(_version(edition), _version(p["evr"])) < 0))
    return sorted(out, key=lambda d: d["name"])


def _version(evr):
    """'1:7.1-1699.pm' → '1:7.1': the version without the release (a rebuild by another vendor often has a lower
    release number than Packman's, for the same program version)."""
    return evr.rsplit("-", 1)[0] if "-" in evr else evr


def check_vendor(facts):
    repos = facts.repos()
    stuck = stuck_packages(facts.installed(), facts.index(), repos, vendor_groups())
    if not stuck:
        return []
    n = len(stuck)
    olds = Counter(d["vendor"] or _("no vendor") for d in stuck)
    repos_to = Counter(d["repo"] for d in stuck if not d["downgrade"])
    old_vendor = olds.most_common(1)[0][0]
    if repos_to:
        text = _("They come from {vendor}, and none of your repositories has them from there any more: zypper does "
                 "not update them on its own. Switched to the version of “{repo}”, they are updated again.").format(
            vendor=old_vendor, repo=repos_to.most_common(1)[0][0])
    else:
        text = _("They come from {vendor}, and none of your repositories has them from there any more: zypper does "
                 "not update them on its own. Your repositories only have older versions, so it is better to leave "
                 "them as they are for now.").format(vendor=old_vendor)
    details = [_("{name}: {vendor} → “{repo}” {version}").format(
        name=d["name"], vendor=d["vendor"] or _("no vendor"), repo=d["repo"], version=d["cand_edition"])
        + ("  " + _("(an older version)") if d["downgrade"] else "") for d in stuck]
    return [finding(LOW, ngettext("{n} package with a vendor different from its repository's",
                                  "{n} packages with a vendor different from their repository's", n).format(n=n),
                    text, "vendor", ngettext("Look at the package…", "Look at the {n} packages…", n).format(n=n),
                    details, dict(packages=stuck), weight=n, check="vendor")]


# ====================================================================== orphaned packages and dependencies

def orphaned(installed, index):
    """Installed packages that no enabled repository has under their name (as 'zypper packages --orphaned';
    Zypp Desk's own bridge packages are left out: they are made here on purpose)."""
    by_name = index["by_name"]
    out = {}
    for p in installed:
        if p["name"] not in by_name and p["vendor"] != BRIDGE_VENDOR:
            out.setdefault(p["name"], p)
    return [out[k] for k in sorted(out)]


def check_orphans(facts):
    index = facts.index()
    if not index["by_name"] and not index.get("solvables"):  # every package would look orphaned
        raise RuntimeError(_("No repository is enabled, or none could be read."))
    orphans = orphaned(facts.installed(), index)
    n = len(orphans)
    if not n:
        return [finding(OK, _("No orphaned packages"),
                        _("Every installed package comes from one of your repositories. Broken dependencies are "
                          "looked for by “Check dependencies”, which asks for the administrator password."),
                        "verify", _("Check the dependencies…"), check="orphans")]
    size = sum(p["size"] for p in orphans)
    return [finding(LOW, ngettext("{n} package that no repository has", "{n} packages that no repository has",
                                  n).format(n=n),
                    _("{names}. They get no updates any more; most are leftovers of programs that something else "
                      "replaced. Look before removing them.").format(names=_join(p["name"] for p in orphans)),
                    "orphans", ngettext("Show the package", "Show the {n} packages", n).format(n=n),
                    [f"{p['name']} {p['evr']} ({size_text(p['size'])})" for p in orphans],
                    dict(packages=[p["name"] for p in orphans]), weight=size, check="orphans")]


# ====================================================================== repositories

def _flag(sec, key):
    v = sec.get(key) if sec is not None else None
    if v is None:
        return None
    return v.strip().lower() in ("1", "yes", "true", "on")


def gpg_checks(sec, defaults=None):
    """(metadata checked, packages checked) as libzypp reads a .repo section: gpgcheck on by default;
    repo_gpgcheck follows gpgcheck; pkg_gpgcheck is on when the metadata is not checked."""
    d = defaults or {}
    gpg = _flag(sec, "gpgcheck")
    if gpg is None:
        gpg = d.get("gpgcheck", True)
    repo = _flag(sec, "repo_gpgcheck")
    if repo is None:
        repo = d["repo_gpgcheck"] if d.get("repo_gpgcheck") is not None else gpg
    pkg = _flag(sec, "pkg_gpgcheck")
    if pkg is None:
        pkg = d["pkg_gpgcheck"] if d.get("pkg_gpgcheck") is not None else (gpg and not repo)
    return repo, pkg


def _repo_sections(folder=REPOS_D):
    out = {}
    for f in sorted(glob.glob(os.path.join(folder, "*.repo"))):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for s in cp.sections():
            out.setdefault(s, cp[s])
    return out


def _zypp_gpg_defaults():
    try:
        from . import zyppconf
        eff = zyppconf.read_effective()
    except Exception:
        return {}
    out = {}
    for k in ("gpgcheck", "repo_gpgcheck", "pkg_gpgcheck"):
        v = (eff.get(k) or {}).get("value")
        if v is not None:
            out[k] = str(v).strip().lower() in ("1", "yes", "true", "on")
    return out


def weak_repos(repos, sections, defaults=None):
    """Enabled repositories whose signature checks are weak: dict(alias, name, url, severity, why, local, http).
    - nothing checked, from the network: medium (anyone on the way could change what you install);
    - nothing checked, a folder on this computer: low;
    - the list of packages not checked (the packages are) over plain http or ftp: low (someone on the network
      could hide updates). Plain http with signed lists (openSUSE's own cdn.opensuse.org) is fine: not shown."""
    out = []
    for r in repos:
        if not r.get("enabled"):
            continue
        url = (r.get("url") or "").strip()
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        local = (r.get("type") == "plaindir" or scheme in ("dir", "file", "iso", "cd", "dvd", "hd")
                 or url.startswith("/"))
        plain = scheme in ("http", "ftp")
        meta, pkg = gpg_checks(sections.get(r["alias"]), defaults)
        if not meta and not pkg:
            sev = LOW if local else MEDIUM
            why = (_("nothing from this folder is checked against a signature") if local else
                   _("nothing from it is checked against a signature"))
        elif not meta and plain:
            sev, why = LOW, _("its list of packages is not signed and comes over plain {scheme}; the packages "
                              "themselves are checked").format(scheme=scheme)
        else:
            continue
        out.append(dict(alias=r["alias"], name=r.get("name") or r["alias"], url=url, severity=sev, why=why,
                        local=local, http=plain))
    return out


def check_repos(facts):
    weak = weak_repos(facts.repos(), _repo_sections(), _zypp_gpg_defaults())
    if not weak:
        return []
    n = len(weak)
    sev = MEDIUM if any(w["severity"] == MEDIUM for w in weak) else LOW

    def short(w):
        tags = []
        if w["http"]:
            tags.append("http")
        if w["local"]:
            tags.append(_("a folder"))
        return f"{w['name']} ({', '.join(tags)})" if tags else w["name"]
    text = _("{names}. Signatures prove that what you install comes from who it says.").format(
        names=_join(short(w) for w in weak))
    details = [f"{w['name']}: {w['why']}." for w in weak]
    return [finding(sev, ngettext("{n} repository with weak signature checks",
                                  "{n} repositories with weak signature checks", n).format(n=n),
                    text, "repos", ngettext("Look at the repository", "Look at the {n} repositories", n).format(n=n),
                    details, dict(repos=[w["alias"] for w in weak]),
                    weight=n, check="repos")]


# ====================================================================== signing keys

def rpm_keys():
    """The keys rpm trusts, one per fingerprint (the copy that lasts longest when rpm has it twice)."""
    entries = keys._rpm_keys()
    data = "\n".join(e[5] for e in entries).encode("utf-8", "replace")
    if not data:
        return []
    parsed = keys.parse_colons(keys._gpg(["--with-colons", "--fixed-list-mode", "--show-keys"], data)[1])
    best = {}
    for k in parsed:
        old = best.get(k["fpr"])
        if old is None or _lasts(k) > _lasts(old):
            best[k["fpr"]] = k
    return list(best.values())


def _lasts(k):
    if k.get("revoked"):
        return -1
    return k.get("expires") or float("inf")


def repo_key_ids(repos):
    """{alias: key IDs that signed its metadata} for the enabled repositories (zypper keeps the signature)."""
    out = {}
    for r in repos:
        alias = r["alias"]
        if not r.get("enabled") or not alias or "/" in alias or alias in (".", ".."):
            continue
        ids = set()
        for rel in ("repodata/repomd.xml.asc", "content.asc"):
            p = keys.RAW_CACHE / alias / rel
            if p.is_file():
                ids |= keys._signature_of_metadata(p)
        out[alias] = ids
    return out


def key_problems(all_keys, repos, repo_ids, shipped, now=None, soon_days=KEY_SOON_DAYS):
    """dict(expired_used, soon_used, expired_unused): [(key, [repository names])]. Keys openSUSE ships with the
    system are left out of expired_unused (they stay, and harm nothing)."""
    now = now or time.time()
    soon = now + soon_days * 86400
    names = {r["alias"]: r.get("name") or r["alias"] for r in repos}
    out = dict(expired_used=[], soon_used=[], expired_unused=[])
    for k in all_keys:
        dead = k.get("revoked") or k.get("expired") or (k.get("expires") and k["expires"] < now)
        ending = k.get("expires") and now <= k["expires"] < soon
        if not dead and not ending:
            continue
        used = sorted(names.get(a, a) for a, ids in repo_ids.items() if any(keys.matches_id(k, i) for i in ids))
        if dead and used:
            out["expired_used"].append((k, used))
        elif ending and used:
            out["soon_used"].append((k, used))
        elif dead and k.get("fpr") not in shipped:
            out["expired_unused"].append((k, []))
    return out


def _date(epoch):
    return datetime.date.fromtimestamp(epoch).isoformat() if epoch else "?"


def check_keys(facts):
    all_keys = rpm_keys()
    now = time.time()
    flagged = [k for k in all_keys if k.get("revoked") or k.get("expired")
               or (k.get("expires") and k["expires"] < now + KEY_SOON_DAYS * 86400)]
    if not flagged:
        return []
    probs = key_problems(flagged, facts.repos(), repo_key_ids(facts.repos()), keys.shipped_fingerprints(), now)
    out = []
    if probs["expired_used"]:
        items = probs["expired_used"]
        n = len(items)
        title = (_("An expired signing key: {name}").format(name=items[0][0]["name"]) if n == 1 else
                 ngettext("{n} expired signing key", "{n} expired signing keys", n).format(n=n))
        repos_ = sorted({r for _k, used in items for r in used})
        out.append(finding(MEDIUM, title, _(
            "{repos} still sign with it. Usually the project renews it and zypper takes the new date when it "
            "refreshes; if refreshing shows a key error, import the new key in Signing keys.").format(
            repos=_join(repos_, 3)), "keys", _("Open Signing keys"),
            [_("{name} ({key}): expired on {date}; used by {repos}").format(
                name=k["name"], key=k["fpr"][-8:], date=_date(k.get("expires")), repos=", ".join(used))
             for k, used in items], dict(fingerprints=[k["fpr"] for k, _u in items]), weight=n, check="keys"))
    if probs["soon_used"]:
        items = probs["soon_used"]
        n = len(items)
        first = min(k["expires"] for k, _u in items)
        title = (_("A signing key expires soon: {name}").format(name=items[0][0]["name"]) if n == 1 else
                 ngettext("{n} signing key expires soon", "{n} signing keys expire soon", n).format(n=n))
        out.append(finding(LOW, title, _("On {date}. Projects usually renew their key in time: nothing to do "
                                         "unless refreshing shows a key error then.").format(date=_date(first)),
                           "keys", _("Open Signing keys"),
                           [_("{name} ({key}): until {date}; used by {repos}").format(
                               name=k["name"], key=k["fpr"][-8:], date=_date(k.get("expires")),
                               repos=", ".join(used)) for k, used in items],
                           dict(fingerprints=[k["fpr"] for k, _u in items]), weight=n, check="keys"))
    if probs["expired_unused"]:
        items = probs["expired_unused"]
        n = len(items)
        out.append(finding(LOW, ngettext("{n} expired key that no repository uses",
                                         "{n} expired keys that no repository uses", n).format(n=n),
                           _("Keys of repositories you no longer have. Signing keys shows whether installed "
                             "packages still use them, and can remove the ones nothing uses."),
                           "keys", _("Open Signing keys"),
                           [_("{name} ({key}): expired on {date}").format(
                               name=k["name"], key=k["fpr"][-8:], date=_date(k.get("expires"))) for k, _u in items],
                           dict(fingerprints=[k["fpr"] for k, _u in items]), weight=n, check="keys"))
    return out


# ====================================================================== Flatpak apps no longer maintained

def documents_dir(home=None):
    """The user's Documents folder (xdg-user-dirs), else ~/Documents."""
    home = home or os.path.expanduser("~")
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "user-dirs.dirs")
    try:
        with open(cfg, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*XDG_DOCUMENTS_DIR\s*=\s*"(.*)"', line)
                if m:
                    return m.group(1).replace("$HOME", home)
    except OSError:
        pass
    return os.path.join(home, "Documents")


def saved_copy(app_id, folders):
    """A file saved by "Save a copy" (<app id>-<version>.flatpak) in one of these folders, or ''."""
    for d in folders:
        if not d or not os.path.isdir(d):
            continue
        for f in sorted(glob.glob(os.path.join(glob.escape(d), glob.escape(app_id) + "*.flatpak"))):
            return f
    return ""


def eol_findings(apps, eol, settings):
    """apps: rows of flatpak.installed(); eol: {app ID: (message, replacement)}."""
    from . import protect
    folders = [(settings or {}).get("bundle_folder") or "", documents_dir()]
    out = []
    for a in apps:
        app_id = a.get("application", "")
        if app_id not in eol:
            continue
        message, rebase = eol[app_id]
        name = a.get("name") or app_id
        prot = protect.is_protected(settings or {}, "flatpak", app_id)
        copy = saved_copy(app_id, folders)
        parts = [_("Its developers replaced it with {app}.").format(app=rebase) if rebase else
                 _("It gets no more updates, and Flathub may stop offering it.")]
        if prot and copy:
            parts.append(_("It is protected and you have a copy."))
        elif prot:
            parts.append(_("It is protected. Save a copy too (in Applications), to install it again if it "
                           "disappears."))
        elif copy:
            parts.append(_("You have a copy. Protect it too (in Applications), so it is never removed by "
                           "mistake."))
        else:
            parts.append(_("Protect it and save a copy in Applications, so you do not lose it."))
        out.append(finding(LOW if prot and copy else MEDIUM,
                           _("An app that is no longer maintained: {name}").format(name=name), " ".join(parts),
                           "show-app", _("Show {name} in Applications").format(name=name) if len(name) <= 18
                           else _("Show the app in Applications"), [message] if message else [],
                           dict(app=app_id, name=name, protected=prot, copy=copy), weight=1, check="flatpak-eol"))
    return out


def check_flatpak_eol(facts):
    apps = facts.flatpak_apps()
    if not apps:
        return []
    return eol_findings(apps, flatpak.eol_apps(apps), facts.settings)


# ====================================================================== the same app twice

def desktop_apps():
    """The visible RPM applications: [dict(desktop, name, names, package)] (the .desktop files of the menu and
    the package that owns each one)."""
    from . import apps
    langs = apps.lang_keys()
    by_file = {}
    for d in apps.DESKTOP_DIRS:
        if d.is_dir():
            for f in sorted(d.glob("*.desktop")):
                by_file.setdefault(f.name, f)
    entries = {}
    for f in by_file.values():
        e = apps._desktop_entry(f, langs)
        if e and not apps.WEB_APP_RE.search(e["exec"]):
            plain = apps._desktop_entry(f, []) or e
            e["names"] = sorted({e["name"], plain["name"]} - {""})
            entries[str(f)] = e
    owners = apps._owners(list(entries))
    return [dict(desktop=e["desktop"], name=e["name"], names=e["names"], package=owners[p][0])
            for p, e in entries.items() if p in owners]


def _name_key(text):
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def duplicated_apps(rpm_apps, fp_apps):
    """[(rpm app, flatpak row)]: the same program as a package and as a Flatpak app (the same desktop ID, or the
    same name)."""
    by_id = {a["application"].lower(): a for a in fp_apps if a.get("application")}
    by_name = {}
    for a in fp_apps:
        k = _name_key(a.get("name"))
        if len(k) >= 3:
            by_name.setdefault(k, a)
    out, seen = [], set()
    for e in rpm_apps:
        stem = e["desktop"][:-len(".desktop")] if e["desktop"].endswith(".desktop") else e["desktop"]
        fp = by_id.get(stem.lower())
        if fp is None:
            fp = next((by_name[k] for k in map(_name_key, e["names"]) if k in by_name), None)
        if fp is not None and (e["package"], fp["application"]) not in seen:
            seen.add((e["package"], fp["application"]))
            out.append((e, fp))
    return out


def check_duplicates(facts):
    pairs = duplicated_apps(facts.desktop_apps(), facts.flatpak_apps())
    if not pairs:
        return []
    n = len(pairs)
    names = [e["name"] for e, _fp in pairs]
    return [finding(LOW, ngettext("{n} app installed twice", "{n} apps installed twice", n).format(n=n),
                    _("{names}: as a package and as a Flatpak app. Two copies take room and show twice in the "
                      "menu; keep the one you use.").format(names=_join(names)),
                    "duplicates", _("Show {name} in Applications").format(name=names[0]) if len(names[0]) <= 18
                    else _("Show the app in Applications"),
                    [_("{name}: package {package}, Flatpak {app}").format(name=e["name"], package=e["package"],
                                                                         app=fp["application"]) for e, fp in pairs],
                    dict(names=names, pairs=[(e["package"], fp["application"]) for e, fp in pairs]),
                    weight=n, check="duplicates")]


# ====================================================================== firmware

GRAB_BAG = ("kernel-firmware-platform", "kernel-firmware-media", "kernel-firmware-network",
            "kernel-firmware-usb-network", "kernel-firmware-serial")
# what docks and USB adapters often need (kept when the computer has USB-C or Thunderbolt)
DOCK_PACKAGES = ("kernel-firmware-usb-network", "kernel-firmware-realtek", "kernel-firmware-mediatek",
                 "kernel-firmware-media", "kernel-firmware-platform")

PCI_VENDORS = {"8086": "Intel", "1002": "AMD", "1022": "AMD", "10DE": "NVIDIA", "10EC": "Realtek",
               "14E4": "Broadcom", "168C": "Qualcomm Atheros", "17CB": "Qualcomm", "14C3": "MediaTek",
               "11AB": "Marvell", "1B4B": "Marvell", "15B3": "Mellanox", "1077": "QLogic", "1425": "Chelsio",
               "19EE": "Netronome", "177D": "Cavium", "104C": "Texas Instruments", "1957": "NXP",
               "1969": "Qualcomm Atheros", "1217": "O2 Micro", "1912": "Renesas", "1B21": "ASMedia",
               "144D": "Samsung", "15B7": "Western Digital", "1E0F": "KIOXIA", "1D6B": "Linux"}
USB_VENDORS = {"8087": "Intel", "0BDA": "Realtek", "0E8D": "MediaTek", "0CF3": "Qualcomm Atheros",
               "0A5C": "Broadcom", "13D3": "IMC Networks", "04CA": "Lite-On", "0489": "Foxconn",
               "0451": "Texas Instruments",
               "1286": "Marvell", "148F": "Ralink", "2357": "TP-Link", "0846": "Netgear", "7392": "Edimax",
               "03F0": "HP", "17EF": "Lenovo", "413C": "Dell", "046D": "Logitech", "0A12": "Cambridge Silicon Radio"}
_PCI_RE = re.compile(r"^pci:v0000([0-9A-F]{4})d0000([0-9A-F]{4})sv[0-9A-F*]+sd[0-9A-F*]+bc([0-9A-F]{2})sc([0-9A-F]{2})")
_USB_RE = re.compile(r"^usb:v([0-9A-F]{4})p([0-9A-F]{4})d[0-9A-F]+dc([0-9A-F]{2})dsc([0-9A-F]{2})dp[0-9A-F]{2}"
                     r"ic([0-9A-F]{2})isc([0-9A-F]{2})")


def firmware_for():
    """What each firmware package is for, in words (the packages Zypp Desk may propose to remove)."""
    return {
        "kernel-firmware-amdgpu": _("AMD graphics"),
        "kernel-firmware-radeon": _("older AMD and ATI graphics"),
        "kernel-firmware-nvidia": _("NVIDIA graphics and chips"),
        "kernel-firmware-i915": _("Intel graphics"),
        "kernel-firmware-intel": _("Intel chips: sound processors, cameras, sensors, accelerators"),
        "kernel-firmware-iwlwifi": _("Intel Wi-Fi"),
        "kernel-firmware-bluetooth": _("Bluetooth adapters"),
        "kernel-firmware-sound": _("sound chips"),
        "kernel-firmware-qcom": _("Qualcomm chips: Arm laptops and boards, 5G modems"),
        "kernel-firmware-ath10k": _("Qualcomm Atheros Wi-Fi"),
        "kernel-firmware-ath11k": _("Qualcomm Atheros Wi-Fi"),
        "kernel-firmware-ath12k": _("Qualcomm Wi-Fi 7"),
        "kernel-firmware-atheros": _("older Atheros Wi-Fi and Bluetooth"),
        "kernel-firmware-mediatek": _("MediaTek Wi-Fi and Bluetooth"),
        "kernel-firmware-realtek": _("Realtek network cards, Wi-Fi and Bluetooth"),
        "kernel-firmware-brcm": _("Broadcom and Cypress Wi-Fi"),
        "kernel-firmware-bnx2": _("Broadcom server network cards"),
        "kernel-firmware-marvell": _("Marvell Wi-Fi"),
        "kernel-firmware-mwifiex": _("Marvell and NXP Wi-Fi"),
        "kernel-firmware-prestera": _("Marvell network switches"),
        "kernel-firmware-mellanox": _("Mellanox server network cards"),
        "kernel-firmware-qlogic": _("QLogic server network and storage cards"),
        "kernel-firmware-chelsio": _("Chelsio server network cards"),
        "kernel-firmware-nfp": _("Netronome server network cards"),
        "kernel-firmware-liquidio": _("Cavium server network cards"),
        "kernel-firmware-dpaa2": _("NXP network processors (Arm boards)"),
        "kernel-firmware-ti": _("Texas Instruments Wi-Fi and Bluetooth (boards)"),
        "kernel-firmware-ueagle": _("old ADSL USB modems"),
    }


def device_aliases(root=None):
    """The modalias of every device the kernel sees (what zypper matches firmware packages against)."""
    root = root or os.path.join(SYS, "devices")
    out = set()
    for base, _dirs, files in os.walk(root, onerror=lambda e: None):
        if "modalias" in files:
            try:
                with open(os.path.join(base, "modalias"), encoding="ascii", errors="replace") as f:
                    a = f.read().strip()
            except OSError:
                continue
            if a:
                out.add(a)
    return out


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def hardware(aliases, sysroot=None):
    """dict(arch, usb_c, bluetooth, sound): what the firmware rules need besides the devices' aliases."""
    sysroot = sysroot or SYS
    usb = [m.groups() for m in map(_USB_RE.match, aliases) if m]
    pci = [m.groups() for m in map(_PCI_RE.match, aliases) if m]
    acpi = {n.split(":")[0].upper() for n in _listdir(os.path.join(sysroot, "bus/acpi/devices"))}
    usb_c = bool(_listdir(os.path.join(sysroot, "class/typec"))
                 or _listdir(os.path.join(sysroot, "bus/thunderbolt/devices"))
                 or acpi & {"USBC000", "PNP0CA0"}  # the USB-C connector manager (UCSI)
                 or any(a.startswith("pci:") and "bc0Csc03i40" in a for a in aliases))  # a USB4 host
    bluetooth = bool(_listdir(os.path.join(sysroot, "class/bluetooth"))) or any(
        ic == "E0" and isc == "01" for _v, _p, _dc, _dsc, ic, isc in usb)
    sound = any(n.startswith("card") for n in _listdir(os.path.join(sysroot, "class/sound"))) or any(
        c == "04" and s in ("01", "03") for _v, _d, c, s in pci)
    return dict(arch=os.uname().machine, usb_c=usb_c, bluetooth=bluetooth, sound=sound)


def _unescape(p):
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), p)


def _literal_prefix(p):
    return re.match(r"[^*?\[\\]*", p).group(0)


def match_aliases(patterns, aliases_sorted):
    """The aliases (sorted) that match any of these glob patterns, as libzypp matches modalias() supplements.
    Only the aliases that start like a pattern are compared with it: thousands of patterns stay quick."""
    hits = []
    for p in patterns:
        pre = _literal_prefix(p)
        i = bisect.bisect_left(aliases_sorted, pre)
        while i < len(aliases_sorted) and aliases_sorted[i].startswith(pre):
            a = aliases_sorted[i]
            if fnmatch.fnmatchcase(a, p):
                hits.append(a)
            i += 1
    return hits


def describe_device(alias):
    """A device in words: 'graphics card Intel (8086:3EA0)'."""
    m = _PCI_RE.match(alias)
    if m:
        v, d, c, s = m.groups()
        kind = {"03": _("graphics card"), "02": _("Wi-Fi") if s == "80" else _("network card"),
                "04": _("sound card") if s in ("01", "03") else _("video device"), "0D": _("wireless adapter"),
                "0C": _("USB or Thunderbolt controller"), "01": _("disk controller")}.get(c, _("device"))
        return f"{kind} {PCI_VENDORS.get(v, v)} ({v}:{d})"
    m = _USB_RE.match(alias)
    if m:
        v, p, _dc, _dsc, ic, isc = m.groups()
        kind = {"E0": _("Bluetooth adapter"), "02": _("USB network adapter"), "0A": _("USB network adapter"),
                "01": _("USB sound device"), "0E": _("camera")}.get(ic, _("USB device"))
        return f"{kind} {USB_VENDORS.get(v, v)} ({v}:{p})"
    bus = alias.split(":", 1)[0]
    if bus == "hdaudio":
        return _("sound chip")
    if bus in ("acpi", "platform", "of", "i2c", "spi"):
        return _("a chip on the board ({id})").format(id=alias.split(":", 1)[-1].strip(":")[:24])
    return _("device ({id})").format(id=alias[:40])


def _class_reason(name, pci, hw):
    """Why a package stays because of the kind of hardware this computer has, or None."""
    vendors = {v for v, _d, _c, _s in pci}
    if name == "kernel-firmware-intel" and "8086" in vendors:
        return _("This computer has Intel chips, and this package has firmware for several kinds of them.")
    if name == "kernel-firmware-i915" and any(v == "8086" and c == "03" for v, _d, c, _s in pci):
        return _("This computer has Intel graphics.")
    if name in ("kernel-firmware-amdgpu", "kernel-firmware-radeon") and any(
            v == "1002" and c == "03" for v, _d, c, _s in pci):
        return _("This computer has AMD graphics.")
    if name == "kernel-firmware-nvidia" and "10DE" in vendors:
        return _("This computer has an NVIDIA chip.")
    if name == "kernel-firmware-iwlwifi" and any(v == "8086" and c == "02" and s == "80" for v, _d, c, s in pci):
        return _("This computer has Intel Wi-Fi.")
    if name == "kernel-firmware-bluetooth" and hw.get("bluetooth"):
        return _("This computer has Bluetooth.")
    if name == "kernel-firmware-sound" and hw.get("sound"):
        return _("This computer has a sound card.")
    return None


def decide(packages, aliases, hw):
    """keep, remove, unsure: [(package, reason)] for the installed firmware packages.

    packages: {name: dict(size, patterns)} (not kernel-firmware-all). Conservative: a package goes only when it is
    known what it is for, no device of this computer matches it, and the computer is a PC (on Arm boards the
    firmware is often for chips Zypp Desk cannot see)."""
    devices = sorted(aliases)
    pci = [m.groups() for m in map(_PCI_RE.match, devices) if m]
    what = firmware_for()
    keep, remove, unsure = [], [], []
    for name in sorted(packages):
        hits = match_aliases(packages[name].get("patterns", ()), devices)
        if hits:
            found = list(dict.fromkeys(describe_device(a) for a in hits))
            keep.append((name, _("Needed by this computer's {devices}.").format(devices=_join(found, 3))))
            continue
        why = _class_reason(name, pci, hw)
        if why:
            keep.append((name, why))
        elif hw.get("arch") not in X86:
            unsure.append((name, _("On this kind of computer firmware is often for chips on the board that Zypp "
                                   "Desk cannot see.")))
        elif hw.get("usb_c") and name in DOCK_PACKAGES:
            unsure.append((name, _("Docks and USB adapters often use it, and this computer has USB-C or "
                                   "Thunderbolt.")))
        elif name in GRAB_BAG:
            unsure.append((name, _("It has firmware for many different devices; Zypp Desk cannot tell for sure "
                                   "that none of them is here.")))
        elif name not in what:
            unsure.append((name, _("Zypp Desk does not know which devices it is for.")))
        else:
            remove.append((name, _("No device of this computer needs it: it is for {what}.").format(what=what[name])))
    remove.sort(key=lambda x: (-packages[x[0]].get("size", 0), x[0]))
    return keep, remove, unsure


def firmware_packages(dirs=FIRMWARE_DIRS):
    """The installed kernel-firmware-* packages: the owners of the firmware folder (one quick rpm query)."""
    folders = []
    for d in dirs:
        real = os.path.realpath(d)
        if os.path.isdir(real) and real not in folders:
            folders.append(real)
    if not folders:
        return []
    names = set(_run(["rpm", "-qf", "--qf", "%{NAME}\n", "--", *folders]).split())
    return sorted(n for n in names if n.startswith(FW_PREFIX) and n != FIRMWARE_ALL)


def firmware_info(names):
    """{name: dict(size, patterns)} of these packages that are installed (one rpm query). patterns: the glob
    patterns of their modalias() supplements, unescaped."""
    if not names:
        return {}
    fmt = "%{NAME}\tSIZE\t%{LONGSIZE}\n[%{=NAME}\tSUP\t%{SUPPLEMENTS}\n]"
    out = {}
    for line in _run(["rpm", "-q", "--qf", fmt, "--", *names]).splitlines():
        f = line.split("\t", 2)
        if len(f) != 3 or f[0] not in names:
            continue  # "package … is not installed"
        p = out.setdefault(f[0], dict(size=0, patterns=[]))
        if f[1] == "SIZE":
            p["size"] = int(f[2]) if f[2].isdigit() else 0
        else:
            for m in re.finditer(r"modalias\(([^()]*)\)", f[2]):
                pat = _unescape(m.group(1))
                if re.match(r"^kernel[^:]*:", pat):  # the old form: modalias(kernel-default:pci:…)
                    pat = pat.split(":", 1)[1]
                p["patterns"].append(pat)
    return out


def locked_names(path=LOCKS_FILE):
    """The names locked in /etc/zypp/locks (readable by everyone)."""
    out = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*solvable_name\s*:\s*(\S+)", line)
                if m:
                    out.add(m.group(1))
    except OSError:
        pass
    return out


def repos_with(name, index, repos):
    """The aliases of the enabled repositories that have this package, the preferred first."""
    prio = {r["alias"]: r.get("priority", 99) for r in repos}
    aliases = {c[0] for c in index["by_name"].get(name, ())}
    return sorted(aliases, key=lambda a: (prio.get(a, 99), a))


def _firmware_plan():
    names = firmware_packages()
    info = firmware_info(names + [FIRMWARE_ALL])
    meta = info.pop(FIRMWARE_ALL, None)
    aliases = device_aliases()
    hw = hardware(aliases)
    keep, remove, unsure = decide(info, aliases, hw)
    sizes = {n: p["size"] for n, p in info.items()}
    locked = FIRMWARE_ALL in locked_names()
    lock, lock_repo = [], ""
    if remove and not locked:
        lock = [FIRMWARE_ALL]
        try:
            found = repos_with(FIRMWARE_ALL, index_of([FIRMWARE_ALL]), cached_repos())
            lock_repo = found[0] if found else ""
        except Exception:  # without it the lock is for every repository (see firmware_changes)
            lock_repo = ""
    if meta is not None and remove:
        remove.insert(0, (FIRMWARE_ALL, _("Only installs all the others; it holds no firmware itself.")))
        sizes[FIRMWARE_ALL] = meta["size"]
    return dict(keep=keep, remove=remove, unsure=unsure,
                size_freed=sum(sizes.get(n, 0) for n, _w in remove), metapackage_installed=meta is not None,
                metapackage=FIRMWARE_ALL, total_size=sum(p["size"] for p in info.values()), count=len(info),
                sizes=sizes, lock=lock, lock_repo=lock_repo, locked=locked, usb_c=hw["usb_c"], arch=hw["arch"],
                devices=len(aliases))


def firmware_plan(refresh=False):
    """Which firmware packages this computer needs. Read-only, quick (two rpm queries and sysfs), kept for the
    session: refresh=True looks again (after a change).

    dict(keep, remove, unsure: [(package, reason)], size_freed (bytes), metapackage_installed (bool), and
    metapackage, total_size, count, sizes {package: bytes}, lock, lock_repo, locked, usb_c, arch, devices).

    - keep: a device of this computer matches the package's modalias() supplements, or the computer has the kind
      of hardware it is for (Intel chips, Intel or AMD graphics, NVIDIA, Intel Wi-Fi, Bluetooth, sound);
    - unsure (kept too, never proposed): packages with firmware for many unrelated devices, packages docks and
      USB adapters use when the computer has USB-C or Thunderbolt, packages Zypp Desk does not know, and
      everything on computers that are not PCs;
    - remove: the rest, the biggest first; kernel-firmware-all first when it is installed;
    - lock: ["kernel-firmware-all"] when something would be removed and it is not locked yet. kernel-default
      recommends 'kernel-firmware', which only kernel-firmware-all provides: the next kernel would bring it, and
      all the firmware with it, back. lock_repo: the repository to lock it in (see firmware_changes)."""
    return _cached("firmware", _firmware_plan, refresh)


def firmware_changes(plan):
    """The queue changes for a plan, as (kind, target, opts) (txqueue.Action(kind, target, **opts)): remove each
    package of plan["remove"], and lock kernel-firmware-all (the kernel recommends "kernel-firmware", which only
    that package provides: every new kernel would bring it back). The queue runs a lock on a package it removes
    after the removal (TxQueue.steps), so the lock covers every repository."""
    out = [("remove", name, {}) for name, _why in plan["remove"]]
    for name in plan.get("lock") or ():
        out.append(("lock", name, {"comment": _("Zypp Desk: only the firmware this computer uses")}))
    return out


def check_firmware(facts):
    plan = firmware_plan()
    if not plan["remove"]:
        return []
    freed = plan["size_freed"]
    kept = [n[len(FW_PREFIX):] for n, _w in plan["keep"]]
    kept_text = _join(kept, 8) if kept else _("none")
    if plan["metapackage_installed"]:
        total = plan["total_size"] + plan["sizes"].get(FIRMWARE_ALL, 0)
        title = _("The firmware of every possible device: {size}").format(size=size_text(total))
        text = ngettext("kernel-firmware-all installs {n} firmware package; this computer uses {used}. Removing "
                        "the rest frees {freed}.",
                        "kernel-firmware-all installs {n} firmware packages; this computer uses {used}. Removing "
                        "the rest frees {freed}.", plan["count"]).format(
            n=plan["count"], used=kept_text, freed=size_text(freed))
    else:
        if freed < 50 * 1000 * 1000:
            return []  # little to gain, and someone already chose what to keep
        title = _("Firmware that this computer does not use: {size}").format(size=size_text(freed))
        text = _("This computer uses {used}. The others are for devices it does not have.").format(used=kept_text)
    sev = MEDIUM if freed >= 200 * 1000 * 1000 else LOW
    details = ([_("Keep {name}: {why}").format(name=n, why=w) for n, w in plan["keep"]]
               + [_("Keep {name}: {why}").format(name=n, why=w) for n, w in plan["unsure"]]
               + [_("Remove {name} ({size}): {why}").format(name=n, size=size_text(plan["sizes"].get(n, 0)), why=w)
                  for n, w in plan["remove"]])
    return [finding(sev, title, text, "firmware", _("Show the firmware in Drivers"), details, dict(plan=plan),
                    weight=freed, check="firmware")]


# ====================================================================== what takes most room

def room(facts):
    """What takes most room: dict(packages=[(package, label, bytes)] the biggest first, items=[dict(key, label,
    bytes, action, data)]). label: the app's name when the package has one in the menu."""
    sizes = Counter()
    for p in facts.installed():
        sizes[p["name"]] += p["size"]
    labels = {}
    try:
        for a in facts.desktop_apps():
            labels.setdefault(a["package"], a["name"])
    except Exception:
        pass
    packages = [(name, labels.get(name, name), size) for name, size in sizes.most_common(20)]
    items = []
    fp_apps = facts.flatpak_apps()
    if fp_apps:
        total = sum(a.get("bytes") or 0 for a in fp_apps)
        items.append(dict(key="flatpak-apps", label=_("Flatpak apps"), bytes=total, action="flatpak", data={}))
    runtimes = facts.flatpak_runtimes()
    if runtimes:
        total = sum(a.get("bytes") or 0 for a in runtimes)
        items.append(dict(key="flatpak-runtimes", label=_("Flatpak runtimes"), bytes=total, action="flatpak",
                          data={}))
    cache = diskspace.package_cache()
    if cache["bytes"] >= diskspace.CACHE_WORTH:
        items.append(dict(key="cache", label=_("downloaded packages"), bytes=cache["bytes"], action="clean",
                          data=dict(files=cache["files"])))
    kernels = diskspace.old_kernels()
    if kernels["count"]:
        items.append(dict(key="kernels", label=ngettext("{n} old kernel", "{n} old kernels", kernels["count"]).format(
            n=kernels["count"]), bytes=kernels["bytes"], action="purge",
            data=dict(items=kernels["items"], snapshots=diskspace.snapshots_in_use())))
    items.sort(key=lambda i: -i["bytes"])
    return dict(packages=packages, items=items)


# ====================================================================== all together

class Facts:
    """What several checks read, read once per run (each thread that needs a fact waits for the first one)."""

    def __init__(self, settings=None):
        self.settings = settings if settings is not None else {}

    def repos(self):
        return cached_repos()

    def index(self):
        return cached_index()

    def installed(self):
        return cached_installed()

    def flatpak_apps(self):
        return _cached("flatpak-apps", lambda: flatpak.installed() if flatpak.available() else [])

    def flatpak_runtimes(self):
        return _cached("flatpak-runtimes", lambda: flatpak.installed(runtimes=True) if flatpak.available() else [])

    def desktop_apps(self):
        return _cached("desktop-apps", desktop_apps)


CHECKS = (("firmware", check_firmware), ("flatpak-eol", check_flatpak_eol), ("vendor", check_vendor),
          ("repos", check_repos), ("keys", check_keys), ("orphans", check_orphans),
          ("duplicates", check_duplicates))


def check_names():
    """Each check in a few words (the page says which ones found nothing, and which could not run)."""
    return {"firmware": _("firmware"), "flatpak-eol": _("Flatpak apps"), "vendor": _("vendors"),
            "repos": _("repositories"), "keys": _("signing keys"), "orphans": _("orphaned packages"),
            "duplicates": _("apps installed twice"), "room": _("room")}


def run_all(settings=None, refresh=False, checks=CHECKS, workers=4):
    """Every check, in threads: dict(findings (ranked), room, clean (checks that found nothing), errors
    {check: text}, timings {check: seconds}, seconds). refresh=True reads everything again (after a change)."""
    if refresh:
        forget()
    facts = Facts(settings)
    start = time.monotonic()
    results, errors, timings = {}, {}, {}

    def one(key, fn):
        t = time.monotonic()
        try:
            results[key] = fn(facts)
        except Exception as e:  # one check that cannot run must not hide the others
            errors[key] = str(e) or type(e).__name__
        timings[key] = round(time.monotonic() - t, 2)

    all_checks = list(checks) + [("room", room)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, fn in all_checks:
            pool.submit(one, key, fn)
    found = []
    for key, _fn in checks:
        found += results.get(key) or []
    clean = [key for key, _fn in checks if key in results and not results[key]]
    return dict(findings=ranked(found), room=results.get("room"), clean=clean, errors=errors, timings=timings,
                seconds=round(time.monotonic() - start, 2))
