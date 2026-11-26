# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only queries to zypper and rpm, run as the normal user.

Everything here runs zypper with LC_ALL=C so the output can be parsed, and with --xmlout
whenever the command supports it. Nothing here changes the system: changes go through the
privileged helper (see runner.py and helper/zypp-desk-helper).
"""

import configparser
import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from .i18n import _

ENV_C = dict(os.environ, LC_ALL="C", LANG="C", LANGUAGE="C")

# zypper exit codes that are not errors (see "man zypper", EXIT CODES).
OK_CODES = {0, 100, 101, 102, 103, 106, 107}
EXIT_NO_MATCH = 104


class ZypperError(Exception):
    pass


# At most this many zypper processes of the window at once. Each one loads every repository into memory
# (hundreds of MB): unticking 95 rows of Updates one after the other started about 190 of them and froze the
# whole computer. The others wait their turn in their thread.
ZYPPER_SLOTS = threading.BoundedSemaphore(2)


def run_c(args, timeout=600, check=False):
    with ZYPPER_SLOTS:
        p = subprocess.run(["zypper", *args], capture_output=True, text=True, env=ENV_C, timeout=timeout)
    if check and p.returncode not in OK_CODES:
        raise ZypperError((p.stderr or p.stdout).strip() or _("zypper exited with code {code}.").format(code=p.returncode))
    return p


def xml_of(text):
    """Parse zypper --xmlout output. Returns (root element, list of (type, text) messages)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Some commands print plain text before or after the XML stream.
        m = re.search(r"<stream>.*</stream>", text, re.S)
        if not m:
            raise ZypperError(text.strip()[:2000] or _("zypper returned no output."))
        root = ET.fromstring(m.group(0))
    msgs = [(m.get("type", "info"), (m.text or "").strip()) for m in root.iter("message")]
    return root, msgs


def xml(args, allow=(EXIT_NO_MATCH,)):
    p = run_c(["--xmlout", "--non-interactive", "--no-refresh", *args])
    root, msgs = xml_of(p.stdout)
    if p.returncode not in OK_CODES and p.returncode not in allow:
        errs = [t for k, t in msgs if k == "error"] or [p.stderr.strip()]
        raise ZypperError("\n".join(e for e in errs if e) or _("zypper exited with code {code}.").format(code=p.returncode))
    return root, p.returncode


# ------------------------------------------------------------------ versions

def vercmp(a: str, b: str) -> int:
    """Compare two editions (version-release) the way rpm does."""
    def split(ed):
        epoch = "0"
        if ":" in ed:
            epoch, ed = ed.split(":", 1)
        v, _, r = ed.partition("-")
        return epoch, v, r or None
    try:
        import rpm
        return rpm.labelCompare(split(a), split(b))
    except ImportError:
        return _vercmp_fallback(a, b)


def _vercmp_fallback(a, b):
    def seg(s):
        return [int(x) if x.isdigit() else x for x in re.findall(r"\d+|[A-Za-z]+", s)]
    sa, sb = seg(a), seg(b)
    for x, y in zip(sa, sb):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return 1 if x > y else -1
        if isinstance(x, int):
            return 1
        if isinstance(y, int):
            return -1
        return 1 if x > y else -1
    return (len(sa) > len(sb)) - (len(sa) < len(sb))


# ------------------------------------------------------------------ search

KINDS = ("package", "pattern", "patch", "product", "srcpackage")
SPECIAL_LISTS = ("orphaned", "unneeded", "recommended", "suggested")


def search(term="", *, kind="package", status="all", repo=None, descriptions=False, provides=False,
           requires=False, recommends=False, files=False, exact=False, case=False, special=None):
    """Search like 'zypper search'. Returns a list of dicts, one per name, with every edition found."""
    opts = ["-t", kind]
    if status == "installed":
        opts.append("--installed-only")
    elif status == "not-installed":
        opts.append("--not-installed-only")
    if repo:
        opts += ["--repo", repo]
    if descriptions:
        opts.append("--search-descriptions")
    for flag, on in (("--provides", provides), ("--requires", requires), ("--recommends", recommends),
                     ("--file-list", files), ("--match-exact", exact), ("--case-sensitive", case)):
        if on:
            opts.append(flag)
    if special in SPECIAL_LISTS:
        return special_list(special, repo=repo)
    terms = ["--", term] if term else []  # a term starting with "-" is not an option

    root, code = xml(["search", *opts, *terms])
    if code == EXIT_NO_MATCH:
        return []
    found = {}
    for s in root.iter("solvable"):
        key = (s.get("name"), s.get("kind"))
        found[key] = dict(name=key[0], kind=key[1], summary=s.get("summary", ""), status=s.get("status", ""),
                          versions=[], installed=None)
    root, _ = xml(["search", "--details", *opts, *terms])
    for s in root.iter("solvable"):
        key = (s.get("name"), s.get("kind"))
        d = found.setdefault(key, dict(name=key[0], kind=key[1], summary="", status=s.get("status", ""),
                                       versions=[], installed=None))
        v = dict(edition=s.get("edition", ""), arch=s.get("arch", ""), repo=s.get("repository", ""),
                 status=s.get("status", ""))
        d["versions"].append(v)
        if v["status"] == "installed":
            d["installed"] = v
    for d in found.values():
        d["best"] = best_version(d["versions"])
        # A newer edition somewhere is not enough (another vendor, a beta that conflicts…):
        # 'updatable' is set by the caller from 'zypper list-updates', which asks the solver.
        d["updatable"] = False
    return sorted(found.values(), key=lambda d: (d["name"], d["kind"]))


def special_list(which, repo=None):
    """'zypper packages --orphaned/--unneeded/--recommended/--suggested' (plain table, no XML)."""
    p = run_c(["--no-refresh", "packages", "--" + which, *(["--repo", repo] if repo else [])])
    found = {}
    for line in p.stdout.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 5 or cols[0] in ("S", "") and cols[2] in ("Name", ""):
            continue
        if set(cols[0]) <= {"-", "+"}:
            continue
        st, rep, name, ed, arch = cols[:5]
        d = found.setdefault(name, dict(name=name, kind="package", summary="", status="", versions=[],
                                        installed=None))
        v = dict(edition=ed, arch=arch, repo=rep, status="installed" if st.startswith("i") else "not-installed")
        d["versions"].append(v)
        if v["status"] == "installed":
            d["installed"] = v
            d["status"] = "installed"
        elif not d["status"]:
            d["status"] = "not-installed"
    for d in found.values():
        d["best"] = best_version(d["versions"])
        d["updatable"] = False
    return sorted(found.values(), key=lambda d: d["name"])


def best_version(versions):
    best = None
    for v in versions:
        if v["repo"] in ("@System", "(System Packages)"):
            continue
        if best is None or vercmp(v["edition"], best["edition"]) > 0:
            best = v
    return best


# ------------------------------------------------------------------ package information

DEP_SECTIONS = ("Provides", "Requires", "Recommends", "Suggests", "Conflicts", "Obsoletes", "Supplements")


def info(name, kind="package"):
    """Parse 'zypper info' (plain text even with --xmlout)."""
    p = run_c(["--no-refresh", "info", "-t", kind, name])
    return _parse_info(p.stdout)


def info_deps(name, kind="package"):
    p = run_c(["--no-refresh", "info", "-t", kind, "--provides", "--requires", "--recommends", "--suggests",
               "--conflicts", "--obsoletes", "--supplements", name])
    return _parse_info(p.stdout)


def _parse_info(text):
    d, deps, key = {}, {}, None
    started = False
    for line in text.splitlines():
        if line.startswith("Information for"):
            if started:
                break  # only the first block (there may be one per kind)
            started = True
            continue
        if not started or set(line) <= {"-"}:
            continue
        m = re.match(r"^(\S[^:]*?)\s*:\s?(.*)$", line)
        if m and not line.startswith(" "):
            key, val = m.group(1).strip(), m.group(2).strip()
            if key in DEP_SECTIONS:
                deps[key] = []
            else:
                d[key] = val
            continue
        if line.startswith(" ") and key:
            if key in deps:
                deps[key].append(line.strip())
            else:
                d[key] = (d.get(key, "") + "\n" + line.strip()).strip("\n")
    d["deps"] = deps
    return d


def rpm_query(args):
    # a package's text may not be UTF-8 (some packages use Latin-1): never fail on it
    p = subprocess.run(["rpm", *args], capture_output=True, text=True, errors="replace", env=ENV_C)
    return p.stdout if p.returncode == 0 else ""


def needed_by(names, limit=300):
    """{name: [installed packages that need it]} for these installed packages: what requires something one of them
    provides (a library's soname, its name...), leaving out the packages of the list itself (removed together,
    they do not count). "Orphaned" only means that no repository has a package any more: something installed may
    still need it (an older library that a package not updated yet still uses), and 'zypper remove' would take that
    away too, without asking. One rpm query per package; read-only."""
    names = [n for n in names if n and not n.startswith("-")][:limit]
    if not names:
        return {}
    try:
        p = subprocess.run(["rpm", "-q", "--qf", "[%{=NAME}\t%{PROVIDENAME}\n]", "--", *names], capture_output=True,
                           text=True, errors="replace", env=ENV_C, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return {}
    provides = {}
    for line in p.stdout.splitlines():
        name, _sep, cap = line.partition("\t")
        if cap and not cap.startswith("config("):  # config(x) is only required by x itself
            provides.setdefault(name, set()).add(cap)
    together = set(names)
    out = {}
    for name, caps in provides.items():
        try:
            q = subprocess.run(["rpm", "-q", "--whatrequires", "--qf", "%{NAME}\n", "--", *sorted(caps)],
                               capture_output=True, text=True, errors="replace", env=ENV_C, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        # "no package requires X" lines have spaces; package names do not
        who = {ln.strip() for ln in q.stdout.splitlines() if ln.strip() and " " not in ln.strip()}
        who -= together
        if who:
            out[name] = sorted(who)
    return out


def installed_among(names):
    """The names, among these package names, of the packages that are installed (one rpm call)."""
    names = [n for n in names if n and not n.startswith("-")]
    if not names:
        return set()
    try:
        p = subprocess.run(["rpm", "-q", "--qf", "%{NAME}\n", "--", *names], capture_output=True, text=True,
                           errors="replace", env=ENV_C, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set(names)  # it cannot be told: as if they were all there
    # an installed package prints its name; one that is not prints "package X is not installed"
    return {line.strip() for line in p.stdout.splitlines()} & set(names)


def files_of(name):
    return rpm_query(["-ql", name])


def changelog_of(name, max_lines=400):
    return "\n".join(rpm_query(["-q", "--changelog", name]).splitlines()[:max_lines])


def signature_of(name):
    """The signature line rpm stores for an installed package ('(none)' when unsigned)."""
    for line in rpm_query(["-qi", name]).splitlines():
        if line.startswith("Signature"):
            return line.split(":", 1)[1].strip()
    return ""


# ------------------------------------------------------------------ repositories, services, locks, locales

def repos():
    root, _ = xml(["repos"])
    out = []
    for r in root.iter("repo"):
        d = dict(alias=r.get("alias"), name=r.get("name") or r.get("alias"), type=r.get("type", ""),
                 priority=int(r.get("priority", 99)), enabled=r.get("enabled") == "1",
                 autorefresh=r.get("autorefresh") == "1", gpgcheck=r.get("gpgcheck") == "1",
                 repo_gpgcheck=r.get("repo_gpgcheck") == "1", pkg_gpgcheck=r.get("pkg_gpgcheck") == "1",
                 url=r.findtext("url") or "", keeppackages=False, service="")
        out.append(d)
    _add_repo_file_fields(out)
    return out


def _add_repo_file_fields(repo_list):
    """keeppackages and the owning service are only in the .repo files (readable by everyone)."""
    # The file name does not have to match the alias (vendor.repo can hold [VendorApp]): read every file
    # and find each repository by its section name.
    sections = {}
    for f in sorted(Path("/etc/zypp/repos.d").glob("*.repo")):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        for name in cp.sections():
            sections.setdefault(name, cp[name])
    for d in repo_list:
        sec = sections.get(d["alias"])
        if sec is not None:
            d["keeppackages"] = sec.get("keeppackages", "0").strip() == "1"
            d["service"] = sec.get("service", "")


def services():
    root, _ = xml(["services"])
    return [dict(alias=s.get("alias"), name=s.get("name") or s.get("alias"), enabled=s.get("enabled") == "1",
                 autorefresh=s.get("autorefresh") == "1", url=s.get("url", ""), type=s.get("type", ""),
                 repos=[r.get("alias") for r in s.iter("repo")])
            for s in root.iter("service")]


def lock_range(rng):
    """'< 2.0', '= 1.0-1', '>= 1:3' from a <range flag="<" epoch="0" version="2.0" release=""/> of 'zypper locks'
    ('' when the lock has none)."""
    if rng is None or not rng.get("flag") or not rng.get("version"):
        return ""
    edition = rng.get("version")
    if rng.get("release"):
        edition += "-" + rng.get("release")
    if rng.get("epoch") not in (None, "", "0"):
        edition = rng.get("epoch") + ":" + edition
    return rng.get("flag") + " " + edition


def locks():
    """The locks of /etc/zypp/locks. 'name' is the name (or the pattern, like vlc-*) alone; 'spec' is what
    'zypper removelock' needs to find the lock again: the name with its version range ('tool < 2.0'). A lock
    limited to a repository ('repo') or of another kind ('type') is only found with -r / -t as well."""
    root, _ = xml(["locks"])
    out = []
    for lk in root.iter("lock"):
        name = lk.findtext("name") or ""
        rng = lock_range(lk.find("range"))
        out.append(dict(number=lk.get("number", ""), name=name, range=rng, spec=f"{name} {rng}" if rng else name,
                        type=lk.findtext("type") or "package", repo=lk.findtext("repo") or ""))
    return out


def no_lock_removed(output, lang=None):
    """True if zypper's output of 'removelock' says that it removed nothing (it still ends with exit code 0):
    the lock was not written exactly that way (a version range, a repository, another kind)."""
    texts = {"No lock has been removed."}
    if lang:
        import gettext
        code = lang.split(".")[0]
        tr = gettext.translation("zypper", languages=[code, code.split("_")[0]], fallback=True)
        texts.add(tr.gettext("No lock has been removed."))
    return any(t in (output or "") for t in texts)


def locale_requested(text):
    """The third column of 'zypper locales': "Requested" (older zypper: "Yes") for a language that was asked for;
    "Fallback" for one that only comes with it (ca with ca_ES), and nothing or "No" for the others."""
    t = (text or "").strip().lower()
    return t == "requested" or t.startswith("y")


def locales():
    """'zypper locales' has no XML output: parse its table."""
    p = run_c(["--no-refresh", "locales"])
    out = []
    for line in p.stdout.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 3 and cols[0] and cols[0] != "Code" and not set(cols[0]) <= {"-", "+"}:
            out.append(dict(code=cols[0], name=cols[1], requested=locale_requested(cols[2])))
    return out


def products():
    root, _ = xml(["products", "--installed-only"])
    return [dict(name=p.get("name"), version=p.get("version"), summary=p.get("summary", ""),
                 isbase=p.get("isbase") == "true") for p in root.iter("product")]


def patterns(installed_only=False):
    root, _ = xml(["patterns", *(["--installed-only"] if installed_only else [])])
    return [dict(name=p.get("name"), summary=p.get("summary", ""), installed=p.get("installed") == "true",
                 repo=p.get("repo", ""), visible=p.get("uservisible") == "true") for p in root.iter("pattern")]


def list_updates():
    root, _ = xml(["list-updates"])
    return [dict(name=u.get("name"), edition=u.get("edition"), old=u.get("edition-old", ""),
                 arch=u.get("arch"), repo=(u.find("source").get("alias") if u.find("source") is not None else ""))
            for u in root.iter("update")]


# ------------------------------------------------------------------ system status

def system_info():
    base = next((p for p in products() if p["isbase"]), None)
    arch = run_c(["system-architecture"]).stdout.strip()
    nr = run_c(["needs-rebooting"]).returncode
    rs = repos()
    return dict(product=(f"{base['summary']} {base['version']}" if base else ""), arch=arch,
                needs_reboot=(nr == 102), repos_enabled=sum(r["enabled"] for r in rs), repos_total=len(rs),
                locks=len(locks()), zypp_lock=zypp_lock())


def _lock_holders():
    return {"packagekitd": _("PackageKit (Discover)"), "zypper": _("zypper in a terminal"), "myrlyn": "Myrlyn",
            "y2start": "YaST", "y2base": "YaST", "yast2": "YaST", "yast": "YaST"}


def _cmd_names(pid):
    """The program names of a process, from its command line (readable for every process; 'comm' is not
    useful: libzypp names its thread 'Zypp-main' in zypper, PackageKit and Myrlyn alike)."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return []
    names = [os.path.basename(a.decode(errors="replace")) for a in args[:3] if a and not a.startswith(b"-")]
    # an interpreter (python3 script, ruby y2start…): the script tells more than the interpreter
    if len(names) > 1 and re.match(r"(python|ruby|perl|bash|sh)[\d.]*$", names[0]):
        names = names[1:] + names[:1]
    return names


def _cmd_args(pid):
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []


def _parent(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


REFRESH_UNIT = "zypp-desk-refresh.service"


def _in_refresh_timer(pid):
    """True if the process runs inside Zypp Desk's own refresh service (its cgroup, readable by everyone)."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(errors="replace")
    except OSError:
        return False
    return any(line.rstrip().endswith("/" + REFRESH_UNIT) for line in text.splitlines())


def zypp_lock():
    """Which program holds the zypp lock, if any (another zypper, PackageKit…), in words for people."""
    try:
        pid = Path("/run/zypp.pid").read_text().strip()
    except OSError:
        return None
    if not pid.isdigit() or not Path(f"/proc/{pid}").exists():
        return None
    # zypper started by this window (helper → pkexec → us) is not "another program"
    p = int(pid)
    for _i in range(4):
        p = _parent(p)
        if p == os.getpid():
            return _("Zypp Desk itself")
        if p <= 1:
            break
    names = _cmd_names(pid)
    if _in_refresh_timer(pid):  # zypp-desk-refresh.timer: a few minutes every six hours
        if "--download-only" in _cmd_args(pid):
            return _("Zypp Desk's timer, downloading the updates ahead")
        return _("Zypp Desk's timer, refreshing the repositories")
    holders = _lock_holders()
    for n in names:
        if n in holders:
            return holders[n]
    return names[0] if names else pid


ZYPP_RAW_CACHE = "/var/cache/zypp/raw"


def repos_refreshed_at(raw=ZYPP_RAW_CACHE):
    """When the repositories were last refreshed (a Unix time), or None if it cannot be told.

    Every 'zypper refresh' touches each repository's index in the raw cache, even when nothing changed
    (repodata/repomd.xml, or 'cookie' for a plain folder or 'content' for the old SUSE format), and these files
    are readable by everyone. The newest one is the last refresh (one that failed for a repository leaves it
    old). The solv folder is no good for this: it changes only when a repository has something new."""
    newest = None
    try:
        entries = list(os.scandir(raw))
    except OSError:
        return None
    for e in entries:
        if not e.is_dir(follow_symlinks=False):
            continue
        for rel in ("repodata/repomd.xml", "cookie", "content"):
            try:
                t = os.stat(os.path.join(e.path, rel)).st_mtime
            except OSError:
                continue
            newest = t if newest is None or t > newest else newest
    return newest


def processes_using_deleted_files():
    """'zypper ps -s' as the normal user (it can only see this user's processes)."""
    p = run_c(["ps", "-s"])
    procs = []
    for line in p.stdout.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 5 and cols[0].isdigit():
            procs.append(dict(pid=cols[0], user=cols[3], command=cols[4], service=cols[5] if len(cols) > 5 else ""))
    core = "No core libraries or services have been updated" not in p.stdout
    return dict(processes=procs, core_updated=core, text=p.stdout)
