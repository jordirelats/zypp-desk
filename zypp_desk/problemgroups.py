# SPDX-License-Identifier: GPL-3.0-or-later
"""The problems of an upgrade grouped by what causes them, in plain words, with what is usually done.

An upgrade that shows dozens of unmet dependencies leaves most people not knowing what to do. On Tumbleweed
they nearly always come from a few causes:
- right after a new Tumbleweed version, the repositories built on top of it (VideoLAN, Packman, OBS projects)
  are a day or two late: their packages do not fit until they are rebuilt, and the problems go away by
  themselves (repostate tells which repositories published before the current Tumbleweed);
- packages that no longer exist in any repository, whose dependencies the upgrade replaces;
- packages that you locked;
- the rest.

collect() looks the packages up, as the normal user and in one go: one 'zypper search' for all the names, the
rpm database (vendor, what provides and what needs each package), the repositories and the locks. It takes a
few seconds: run it in a thread. group() makes the groups and, for each problem, the solution that leaves it as
it is ("Update the rest and leave these as they are"). analyse() does both.

The problems come from simulation.problem_items(). With -n, zypper only gives the solutions of the first
problem; the others are a line each. So the solution that leaves a problem as it is, is made here from the
packages the problem names (a lock, a removal or, in the queue, leaving the queue), and zypper's own solution
is used when it has one that does the same.

Keeping a package of another repository as it is can mean keeping some of Tumbleweed's own packages as they are
too: VideoLAN's vlc-codecs needs 'vlc-noX = 3.0.21', exactly the version you have, and the update replaces
vlc-noX. Locking only vlc-codecs then fails again at the next simulation. So the whole
set that needs each other at an exact version is locked in one go, and the text says which Tumbleweed packages
are held back; a set that is too big is not kept at all. The locks made this way are remembered in the settings
("temp_locks": temp_lock_records, remember_temp_locks), so that the problem window and Locks can say when the
repository has published again and they can go.
"""

import fnmatch
import re
import subprocess
import time

from . import repostate
from .i18n import _, ngettext
from .style import BLUE, ORANGE, PURPLE
from .txqueue import Action

BEHIND, THIRDPARTY, LOCKED, GONE, OTHER = "behind", "thirdparty", "locked", "gone", "other"
ORDER = (BEHIND, THIRDPARTY, LOCKED, GONE, OTHER)
GREY = "#7f8c8d"
# from how many problems the window starts with the groups instead of the problems one by one
MANY = 4
# Tumbleweed came out this long ago and a repository built on top of it has still not caught up: waiting a day
# or two is no longer the obvious advice
CATCH_UP_HOURS = 96
NAMES_IN_TEXT = 3
# a repository that has published nothing for this many days is not catching up: waiting will not fix it
QUIET_DAYS = 7
# keeping a package as it is may hold back at most this many packages that need each other at an exact version
MAX_HOLD = 15
_SYSTEM = ("(System Packages)", "@System", "")
# files that make a package a program somebody uses, not only a library: it is never removed on its own
_PROGRAM_FILES = ("/usr/bin/", "/usr/sbin/", "/usr/games/", "/usr/share/applications/", "/opt/")


def _sim():
    from . import simulation  # simulation imports pages.problems, which imports this module: import it late
    return simulation


# ------------------------------------------------------------------ what a problem is about

def _same_edition(a, b):
    """'0:1.2-3' and '1.2-3' are the same edition."""
    def norm(e):
        e = (e or "").strip()
        return e[2:] if e.startswith("0:") else e
    return bool(a) and bool(b) and norm(a) == norm(b)


def _cap_name(cap):
    """The name of a capability as rpm stores it: 'vlc = 3.0.21' → 'vlc'; 'libfoo.so.1()(64bit)' stays."""
    c = cap.strip().strip("'")
    m = re.match(r"^(\S+?)\s*(<=|>=|=|<|>)\s*\S+$", c)
    return m[1] if m else c


def _scan(line):
    """The solvables a line of zypper's problem text names (each with 'state': 'installed', 'to be installed'
    or ''), and the capabilities it says are needed ('requires X', 'nothing provides X')."""
    sim = _sim()
    needs = []
    for rx in (r"\brequires '([^']+)', but\b", r"\bnothing provides '([^']+)' needed by\b"):
        needs += re.findall(rx, line)
    if not needs:  # libsolv's own wording has no quotes
        for rx in (r"\brequires (.+?), but\b", r"\bnothing provides (.+?) needed by\b"):
            needs += re.findall(rx, line)
    rest = re.sub(r"'[^']*'", " ", line)
    for n in needs:
        rest = rest.replace(n, " ")
    rest = re.sub(r"\b(conflicts with|obsoletes) (\S+(?: [<>=]+ \S+)?) provided by\b", r"\1 provided by", rest)
    words = rest.split()
    out, state = [], ""
    for i, w in enumerate(words):
        if w == "installed" and i and words[i - 1] in ("the", "be"):
            state = "to be installed" if words[i - 1] == "be" else "installed"
            continue
        sv = sim.split_solvable(w.rstrip(",;:"))
        if sv["kind"] == "package" and sv["edition"] and (sv["arch"] or sv["repo"]):
            sv["state"] = state
            out.append(sv)
        state = ""
    return out, needs


def parse_problem(item):
    """dict(solvables, needs, unlock): the packages a problem names, in the order zypper names them (the one the
    problem is about first, then the ones its details list as 'deleted providers'), what they need, and the
    packages a solution offers to unlock (they are locked)."""
    sim = _sim()
    blocks = sim._problem_blocks(item.get("raw") or "")
    head, details = (blocks[0]["head"], blocks[0]["details"]) if blocks else ((item.get("raw") or "").strip(), [])
    lines = [head]
    if re.match(r"^Detailed information:?\s*$", head):  # zypper -v: every rule of the problem, one per line
        lines = [d[2:] for d in details if d.startswith("- ")] or [head]
        details = [d for d in details if not d.startswith("- ")]
    solvables, needs = [], []
    for line in lines:
        s, n = _scan(line)
        solvables += s
        needs += n
    key = None
    for d in details:
        m = re.match(r"^(deleted|not installable|uninstallable) providers:\s*(.*)$", d)
        if m:
            key, words = m[1], m[2].split()
        elif key and re.match(r"^\S+$", d):
            words = [d]
        else:
            key = None
            continue
        for w in words:
            sv = sim.split_solvable(w)
            if sv["kind"] == "package" and sv["edition"]:
                sv["state"] = "installed" if key == "deleted" else "to be installed"
                sv["provider"] = key  # 'deleted': installed, and this update replaces or removes it
                solvables.append(sv)
    unlock, locks = [], []
    for sol in item.get("solutions") or []:
        unlock += re.findall(r"remove lock to allow (?:removal|installation) of (\S+)", sol.get("raw") or "")
        locks += [a.target for a in sol.get("actions") or [] if not isinstance(a, dict) and a.kind == "lock"]
    return dict(solvables=solvables, needs=list(dict.fromkeys(needs)),
                unlock=list(dict.fromkeys(sim.split_solvable(u)["name"] for u in unlock)),
                locks=list(dict.fromkeys(locks)))


# ------------------------------------------------------------------ looking the packages up (slow: in a thread)

def _hdr_edition(h):
    epoch = h["epoch"]
    return (f"{epoch}:" if epoch not in (None, 0, "0", "") else "") + f"{h['version']}-{h['release']}"


def _rpm_lookup(names, caps):
    """installed {name: dict(edition, vendor, provides, files_program)}, providers {cap: [names]}: the rpm
    database, read as the normal user (python3-rpm, which the package requires; rpm itself if it is missing)."""
    installed, providers = {}, {}

    def add(h):
        n = h["name"]
        if n in installed:
            return
        files = h["filenames"] or []
        installed[n] = dict(edition=_hdr_edition(h), vendor=h["vendor"] or "", provides=list(h["providename"] or []),
                            program=any(f.startswith(_PROGRAM_FILES) for f in files))
    try:
        import rpm
        ts = rpm.TransactionSet()
        for n in names:
            for h in ts.dbMatch("name", n):
                add(h)
        for c in caps:
            cname = _cap_name(c)
            if not cname or cname.startswith("("):  # a rich dependency ('(a or b)'): not one package
                continue
            found = []
            for h in ts.dbMatch("providename", cname):
                add(h)
                found.append(h["name"])
            providers[c] = sorted(set(found))
        return installed, providers
    except ImportError:
        pass
    if names:  # no python3-rpm: name, edition and vendor only
        p = subprocess.run(["rpm", "-q", "--qf", "%{NAME}\\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\\t%{VENDOR}\\n",
                            "--", *names], capture_output=True, text=True, errors="replace", timeout=60)
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] not in installed:
                installed[parts[0]] = dict(edition=parts[1], vendor="" if parts[2] == "(none)" else parts[2],
                                           provides=[], program=True)  # program: unknown, so it is never removed
    return installed, providers


def _rpm_users(names):
    """{name: [installed packages that need something it provides]} (python3-rpm; {} without it)."""
    out = {}
    try:
        import rpm
    except ImportError:
        return out
    ts = rpm.TransactionSet()
    for n in names:
        users = set()
        for h in ts.dbMatch("name", n):
            for prov in h["providename"] or []:
                users.update(x["name"] for x in ts.dbMatch("requirename", prov))
        users.discard(n)
        out[n] = sorted(users)
    return out


def _exact(flags, rpm):
    """True for an rpm dependency with '=' alone (not <=, >=): an exact version."""
    sense = rpm.RPMSENSE_LESS | rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL
    return (flags & sense) == rpm.RPMSENSE_EQUAL


def _exact_family(seeds, limit=MAX_HOLD):
    """({seed: [installed packages tied to it by exact-version dependencies ('vlc-noX = 3.0.21'), both ways and
    one after the other, the seed first]}, {name: dict(edition, vendor, provides, program)} of those packages).
    A family that grows past `limit` stops there (it has limit + 1 names: too big to keep). Without python3-rpm,
    each seed alone."""
    out, info = {}, {}
    try:
        import rpm
    except ImportError:
        return {n: [n] for n in seeds}, info
    ts = rpm.TransactionSet()

    def note(h):
        n = h["name"]
        if n not in info:
            files = h["filenames"] or []
            info[n] = dict(edition=_hdr_edition(h), vendor=h["vendor"] or "", provides=list(h["providename"] or []),
                           program=any(f.startswith(_PROGRAM_FILES) for f in files))
        return n
    for seed in seeds:
        seen, todo = [seed], [seed]
        while todo and len(seen) <= limit:
            n = todo.pop(0)
            for h in ts.dbMatch("name", n):
                note(h)
                found = []
                # what it needs at an exact version (not config(…), files or rpmlib(…))
                for rn, fl, rv in zip(h[rpm.RPMTAG_REQUIRENAME], h[rpm.RPMTAG_REQUIREFLAGS],
                                      h[rpm.RPMTAG_REQUIREVERSION]):
                    if rv and _exact(fl, rpm) and not rn.startswith(("rpmlib(", "config(", "/")):
                        found += [note(p) for p in ts.dbMatch("providename", rn)]
                # what needs it at an exact version
                for prov in dict.fromkeys(h["providename"] or []):
                    if prov.startswith("config("):
                        continue
                    for u in ts.dbMatch("requirename", prov):
                        if u["name"] not in seen and any(
                                rn == prov and _exact(fl, rpm)
                                for rn, fl in zip(u[rpm.RPMTAG_REQUIRENAME], u[rpm.RPMTAG_REQUIREFLAGS])):
                            found.append(note(u))
                for f in found:
                    if f not in seen:
                        seen.append(f)
                        todo.append(f)
        out[seed] = seen
    return out, info


def _exact_cap(cap):
    """'vlc-noX = 3.0.21' → ('vlc-noX', '3.0.21'); None for any other capability (>=, a library…)."""
    m = re.match(r"^(\S+?)\s*=\s*(\S+)$", cap.strip().strip("'"))
    return (m[1], m[2]) if m and not m[1].endswith(("<", ">")) else None


def _edition_has(edition, ver):
    """True when an installed edition ('3.0.21-1.1', '1:2.0-3') is the version a capability asks for ('3.0.21' or
    '3.0.21-1.1')."""
    def bare(e):
        return e.split(":", 1)[1] if re.match(r"^\d+:", e or "") else (e or "")
    e, v = bare(edition), bare(ver)
    return bool(v) and (e == v or e.startswith(v + "-"))


def _seeds(parsed, installed):
    """Tumbleweed's own installed packages that keeping a package of another repository may hold back: the ones
    a problem needs at an exact version, the ones the update replaces ('deleted providers') and the ones a
    zypper solution locks ('keep obsolete X')."""
    seeds = []
    for p in parsed:
        cands = [sv["name"] for sv in p["solvables"] if sv.get("provider") == "deleted"]
        for cap in p["needs"]:
            ex = _exact_cap(cap)
            if ex:
                cands.append(ex[0])
        cands += p.get("locks") or []
        seeds += [n for n in cands if n in installed and _tw_vendor(installed[n]["vendor"])]
    return list(dict.fromkeys(seeds))[:20]


def _search(names, repos):
    """{name: [dict(edition, repo alias ('' for the installed system), status)]} from one 'zypper search' as the
    normal user, or None when zypper could not answer."""
    from . import backend
    names = [n for n in names if re.fullmatch(r"[A-Za-z0-9_.+-]+", n)]
    if not names:
        return {}
    # one regular expression for all the names: zypper matches each separate term against the whole pool, so
    # 16 terms take 5 s and one expression with the 16 names 2 s (most of it is loading the repositories)
    rx = "/^(" + "|".join(re.sub(r"([.+])", r"\\\1", n) for n in names) + ")$/"
    try:
        p = backend.run_c(["--xmlout", "--non-interactive", "--no-refresh", "search", "-s", "--case-sensitive",
                           "-t", "package", "--", rx], timeout=120)
        if p.returncode not in backend.OK_CODES and p.returncode != backend.EXIT_NO_MATCH:
            return None
        root, _msgs = backend.xml_of(p.stdout)
    except Exception:
        return None
    alias = {r.get("name"): r["alias"] for r in repos if r.get("name")}
    alias.update({r["alias"]: r["alias"] for r in repos})
    out = {n: [] for n in names}
    for s in root.iter("solvable"):
        repo = s.get("repository", "")
        out.setdefault(s.get("name", ""), []).append(dict(
            edition=s.get("edition", ""), repo="" if repo in _SYSTEM else alias.get(repo, repo),
            status=s.get("status", "")))
    return out


def collect(items):
    """Everything group() needs. Read-only, as the normal user; a few seconds (one zypper search)."""
    from . import backend
    parsed = [parse_problem(it) for it in items]
    names = {sv["name"] for p in parsed for sv in p["solvables"]} | {u for p in parsed for u in p["unlock"]}
    caps = {c for p in parsed for c in p["needs"]}
    # a capability that is a package name ('vlc = 3.0.21') is looked up as a package too
    names |= {_cap_name(c) for c in caps if re.match(r"^[A-Za-z0-9][\w.+-]*$", _cap_name(c))
              and ".so" not in _cap_name(c)}
    names = sorted(n for n in names if n)[:400]
    facts = dict(parsed=parsed, repos=[], locks=[], installed={}, providers={}, search=None, users={},
                 state={}, family={}, temp_locks=[], errors=[])
    for key, fn in (("repos", backend.repos), ("locks", backend.locks)):
        try:
            facts[key] = fn()
        except Exception as e:
            facts["errors"].append(f"{key}: {e}")
    try:
        facts["installed"], facts["providers"] = _rpm_lookup(names, sorted(caps))
    except Exception as e:
        facts["errors"].append(f"rpm: {e}")
    try:  # Tumbleweed packages that keeping something as it is would hold back, with what needs them exactly
        facts["family"], more = _exact_family(_seeds(parsed, facts["installed"]))
        for n, inf in more.items():
            facts["installed"].setdefault(n, inf)
    except Exception as e:
        facts["errors"].append(f"family: {e}")
    facts["search"] = _search(names, facts["repos"])
    try:
        facts["state"] = repostate.state(facts["repos"])
    except Exception as e:
        facts["errors"].append(f"repostate: {e}")
    # what still uses the installed packages that no repository has any more
    gone = [n for n in names if n in facts["installed"] and facts["search"] is not None
            and not any(e["repo"] for e in facts["search"].get(n, []))]
    try:
        facts["users"] = _rpm_users(gone)
    except Exception as e:
        facts["errors"].append(f"users: {e}")
    return facts


# ------------------------------------------------------------------ the groups

def _tw_vendor(vendor):
    return (vendor or "").lower().startswith(("opensuse", "suse"))


def _repo_by_vendor(vendor, repos):
    """The repository an installed package came from, by its vendor (when zypper does not know the version any
    more): 'VideoLAN Project …' → the VideoLAN repository, 'obs://build.opensuse.org/home:x' → that project."""
    v = (vendor or "").strip()
    low = v.lower()
    want = None
    if low.startswith("obs://build.opensuse.org/"):
        project = v.split("/", 3)[3].strip("/")
        if project:
            want = "/repositories/" + project.replace(":", ":/")
    elif "videolan" in low:
        want = "videolan"
    elif "packman" in low:
        want = "packman"
    if not want:
        return None
    for r in repos:
        url = (r.get("url") or "").lower()
        if not r.get("enabled", True) or not url:
            continue
        if want.startswith("/repositories/"):
            i = url.find(want.lower())
            if i >= 0 and url[i + len(want):i + len(want) + 1] in ("/", ":"):
                return r["alias"]
        elif want in url or want in r["alias"].lower():
            return r["alias"]
    return None


def _where(sv, facts):
    """Where a package of a problem comes from: a repository alias, 'tw' (Tumbleweed itself), 'gone' (installed,
    and no repository has it any more) or '' (unknown)."""
    repos = {r["alias"]: r for r in facts["repos"]}
    by_name = {r.get("name"): r["alias"] for r in facts["repos"] if r.get("name")}

    def family(alias):
        r = repos.get(alias) or {"alias": alias}
        return "tw" if repostate.tumbleweed_family(r) else alias
    if sv.get("repo"):
        a = sv["repo"] if sv["repo"] in repos else by_name.get(sv["repo"], sv["repo"])
        return family(a)
    entries = (facts["search"] or {}).get(sv["name"]) if facts["search"] is not None else None
    if entries:
        same = [e["repo"] for e in entries if e["repo"] and _same_edition(e["edition"], sv["edition"])]
        if same:
            return family(same[0])
    inst = facts["installed"].get(sv["name"])
    if inst and (not sv["edition"] or _same_edition(inst["edition"], sv["edition"]) or sv["state"] == "installed"):
        if entries is not None and not any(e["repo"] for e in entries):
            return "gone"
        a = _repo_by_vendor(inst["vendor"], facts["repos"])
        if a:
            return family(a)
        if _tw_vendor(inst["vendor"]):
            return "tw"
    return ""


def _locked(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _who(names, limit=NAMES_IN_TEXT):
    names = list(names)
    text = ", ".join(names[:limit])
    return text + ("…" if len(names) > limit else "")


def _lock_solution(names, holds=()):
    """Lock `names` (the packages kept as they are, and the Tumbleweed packages in `holds` that they need exactly
    as they are now)."""
    text = (_("Keep {pkg} as it is").format(pkg=names[0]) if len(names) == 1 else
            _("Keep {list} as they are").format(list=", ".join(names)))
    detail = _("The rest goes ahead. They stay at their versions and are locked: they will not change until you "
               "unlock them.")
    return dict(number=0, text=text, detail=detail, detail_now=detail, raw="",
                actions=[Action("lock", n) for n in names], possible=True, recommended=False, synthesized=True,
                holds_tw=[n for n in names if n in set(holds)])


def _remove_solution(names):
    return dict(number=0, text=_("Remove {list}").format(list=", ".join(names)),
                detail=_("They are uninstalled: whatever uses them may lose some functions. The next simulation "
                         "shows if anything else would go too."),
                detail_now=_("They are uninstalled now, but only if nothing else needs them: otherwise zypper "
                             "refuses and nothing changes. Whatever uses them may lose some functions."),
                raw="", actions=[Action("remove", n) for n in names], possible=True, recommended=False,
                synthesized=True)


def _discard_solution(names):
    return dict(number=0, text=_("Do not install {name}").format(name=", ".join(names)),
                detail=_("{name} leaves the queue; the rest goes ahead.").format(name=", ".join(names)),
                detail_now=_("{name} leaves the queue; the rest goes ahead.").format(name=", ".join(names)),
                raw="", actions=[dict(kind="discard", target=n, of="install") for n in names], possible=True,
                recommended=False, synthesized=True)


def _targets(sol, kind):
    return [a.target for a in sol.get("actions") or [] if not isinstance(a, dict) and a.kind == kind]


def keep_solution(item, parsed, kind, facts, queued=()):
    """The solution that leaves this problem as it is, or None when there is none that is safe:
    - a package that no longer exists and that nothing uses (and is not a program) is removed;
    - in the queue, a queued install that causes it leaves the queue;
    - otherwise its packages are locked at the version you have, with what they need from the same vendor.
      Tumbleweed's own packages are locked only when what is kept needs them exactly as they are now (an exact
      version, or one the update replaces), or zypper's own solution for a package you locked keeps them; then
      the whole set that needs each other at an exact version is locked in one go (facts["family"]), and the
      solution says which ones in 'holds_tw'. A set bigger than MAX_HOLD is not kept.
    zypper's own solution is used when it has one that does the same."""
    sols = [s for s in item.get("solutions") or [] if s.get("possible") and s.get("actions")]
    svs = parsed["solvables"]
    installed = facts["installed"]
    if kind == GONE:
        gone = [sv["name"] for sv in svs if sv.get("where") == "gone"]
        gone = list(dict.fromkeys(gone))
        users = facts.get("users") or {}
        removable = [n for n in gone if n in users and not (set(users[n]) - set(gone))
                     and not installed.get(n, {}).get("program", True)]
        if gone and removable == gone:
            for s in sols:
                rm = _targets(s, "remove")
                if rm and set(rm) <= set(removable) and len(rm) == len(s["actions"]):
                    return s
            return _remove_solution(gone)
    queued = set(queued or ())
    leave = list(dict.fromkeys(sv["name"] for sv in svs if sv["state"] == "to be installed" and sv["name"] in queued))
    if leave:
        for s in sols:
            ds = [a for a in s["actions"] if isinstance(a, dict) and a.get("kind") == "discard"]
            if ds and len(ds) == len(s["actions"]) and {a["target"] for a in ds} <= set(leave):
                return s
        return _discard_solution(leave)
    patterns = [lk["name"] for lk in facts["locks"] if lk.get("type", "package") == "package"]

    def tw(name):
        return _tw_vendor(installed.get(name, {}).get("vendor"))

    def lockable(name):
        return name in installed and not _locked(name, patterns)
    vendors = {installed[sv["name"]]["vendor"] for sv in svs if sv["name"] in installed and not tw(sv["name"])}
    own = [sv["name"] for sv in svs if lockable(sv["name"]) and not tw(sv["name"])]
    for cap in parsed["needs"]:
        own += [n for n in facts["providers"].get(cap, []) if lockable(n) and not tw(n)
                and installed[n]["vendor"] in vendors]
    # Tumbleweed packages that what is kept needs as they are now
    deleted = {sv["name"] for sv in svs if sv.get("provider") == "deleted"}
    needed = []
    for cap in parsed["needs"]:
        ex = _exact_cap(cap)
        for n in facts["providers"].get(cap, []):
            if not tw(n) or not (ex or n in deleted):
                continue
            if ex and ex[0] == n and not _edition_has(installed[n]["edition"], ex[1]):
                continue  # the version you have is not the one it needs: holding it back would not help
            needed.append(n)
    zyp = None
    if kind == LOCKED:  # zypper's "keep obsolete X" for a package you locked: keeping it means keeping X too
        for s in sols:
            locks = _targets(s, "lock")
            if locks and len(locks) == len(s["actions"]):
                zyp = s
                break
        for n in _targets(zyp, "lock") if zyp else []:
            (needed if tw(n) else own).append(n)
    holds = []
    for n in dict.fromkeys(needed):
        family = (facts.get("family") or {}).get(n) or [n]
        if len(family) > MAX_HOLD:
            return None  # too much of Tumbleweed would stay behind: it cannot be left as it is
        holds += [f for f in family if f not in holds]
    if len(holds) > MAX_HOLD:
        return None
    own = list(dict.fromkeys(own + [f for f in holds if not tw(f)]))
    held_tw = [f for f in holds if tw(f) and lockable(f) and f not in own]
    names = [n for n in own if lockable(n)] + held_tw
    if not names:
        return None
    if not held_tw:
        for s in sols:
            locks = _targets(s, "lock")
            if locks and len(locks) == len(s["actions"]) and set(locks) <= set(names):
                return s
    return _lock_solution(names, held_tw)


def _behind_text(alias, repo, st, n):
    """What a repository that is late usually does, and the advice (n: how many of its packages)."""
    name = (repo or {}).get("name") or alias
    url = ((repo or {}).get("url") or "").lower()
    low = (alias + " " + name + " " + url).lower()
    if "videolan" in low:
        what = _("VideoLAN publishes the codecs a few days after each Tumbleweed version. It happens often and "
                 "fixes itself.")
    elif "packman" in low:
        what = _("Packman rebuilds its packages a day or two after each Tumbleweed version. It happens often and "
                 "fixes itself.")
    elif "/repositories/" in url:
        what = _("{repo} is a project of the openSUSE Build Service: it rebuilds its packages for each new "
                 "Tumbleweed, usually within a day or two.").format(repo=name)
    else:
        what = _("{repo} has not published anything since the new Tumbleweed came out. Repositories built on top "
                 "of Tumbleweed usually catch up in a day or two.").format(repo=name)
    hours = (st or {}).get("tw_hours")
    if hours is not None and hours >= CATCH_UP_HOURS:
        days = int(hours // 24)
        advice = (ngettext("It has had {n} day to catch up.", "It has had {n} days to catch up.", days).format(n=days)
                  + " " + ngettext("Wait a little longer, or leave it as it is for now.",
                                   "Wait a little longer, or leave them as they are for now.", n))
    else:
        advice = _("Recommended: wait a day or two.")
    return what, advice


def _thirdparty_text(alias, repo, st, n, now=None):
    """What a repository that is not catching up means, and the advice: honest about waiting."""
    name = (repo or {}).get("name") or alias
    st = st or {}
    published = (st.get("published") or {}).get(alias)
    tw = st.get("tw_published")
    rebuilt = repostate.rebuilt_for_tumbleweed(repo or {"alias": alias})
    days = int(((now or time.time()) - published) // 86400) if published else None
    if days is not None and days >= QUIET_DAYS and (not rebuilt or not tw or published < tw):
        what = ngettext("{repo} has not published anything for {n} day, so waiting will probably not fix it.",
                        "{repo} has not published anything for {n} days, so waiting will probably not fix it.",
                        days).format(repo=name, n=days)
    elif rebuilt:
        what = _("{repo} is built for Tumbleweed, but these packages do not fit with this update yet.").format(
            repo=name)
        return what, ngettext("Usually: leave it as it is for now and try again in a few days.",
                              "Usually: leave them as they are for now and try again in a few days.", n)
    else:
        what = _("{repo} is not part of Tumbleweed and is not rebuilt for each new version: its packages were made "
                 "for the versions you have now.").format(repo=name)
    return what, ngettext("Usually: leave it as it is and update the rest. If it keeps happening, look for a newer "
                          "version or remove it.",
                          "Usually: leave them as they are and update the rest. If it keeps happening, look for "
                          "newer versions or remove them.", n)


def _temp_locks(facts):
    """{spec: record} of the locks "leave these as they are" made that still exist, each record with
    'caught_up' (its repository published after the lock was made)."""
    names = {lk["name"] for lk in facts.get("locks") or []}
    published = (facts.get("state") or {}).get("published") or {}
    out = {}
    for r in facts.get("temp_locks") or []:
        if not isinstance(r, dict) or r.get("spec") not in names:
            continue
        r = dict(r)
        try:
            r["caught_up"] = bool(r.get("repo")) and published.get(r["repo"], 0) > int(r.get("added") or 0)
        except (TypeError, ValueError):
            r["caught_up"] = False
        out[r["spec"]] = r
    return out


def group(items, facts, queued=()):
    """(groups, keeps). groups: a list of dicts, in the order of ORDER, each with kind, key, repo (alias or ''),
    items (indices into items), packages ({name: edition}), title, detail, advice, color and more (the lines
    under 'Show them'). keeps: one solution (or None) per problem, see keep_solution()."""
    repos = {r["alias"]: r for r in facts["repos"]}
    patterns = [lk["name"] for lk in facts["locks"] if lk.get("type", "package") == "package"]
    st = facts.get("state") or {}
    behind = set(st.get("behind") or [])
    groups, keeps = {}, []
    for i, (item, p) in enumerate(zip(items, facts["parsed"])):
        svs = p["solvables"]
        for sv in svs:
            sv["where"] = _where(sv, facts)
        locked = [n for n in p["unlock"]] + [sv["name"] for sv in svs if _locked(sv["name"], patterns)]
        cause = next((sv for sv in svs if sv["where"] not in ("", "tw", "gone")), None)
        gone = [sv for sv in svs if sv["where"] == "gone"]
        if locked:
            kind, key, pkgs = LOCKED, LOCKED, [(n, "") for n in locked]
        elif cause is not None:
            alias = cause["where"]
            kind = BEHIND if alias in behind else THIRDPARTY
            key = kind + ":" + alias
            pkgs = [(sv["name"], sv["edition"]) for sv in svs if sv["where"] == alias]
        elif gone:
            kind, key, pkgs = GONE, GONE, [(sv["name"], sv["edition"]) for sv in gone]
        else:
            kind, key, pkgs = OTHER, OTHER, []
        g = groups.setdefault(key, dict(kind=kind, key=key, repo=cause["where"] if kind in (BEHIND, THIRDPARTY)
                                        else "", items=[], packages={}))
        g["items"].append(i)
        for n, ed in pkgs:
            if n not in g["packages"] or not g["packages"][n]:
                inst = facts["installed"].get(n)
                g["packages"][n] = (inst["edition"] if inst else "") or ed
        keeps.append(keep_solution(item, p, kind, facts, queued))
    out = sorted(groups.values(), key=lambda g: (ORDER.index(g["kind"]), -len(g["items"]), g["key"]))
    for g in out:
        _describe(g, items, facts, repos, st)
    return out, keeps


def _date(t):
    from PySide6.QtCore import QDateTime, QLocale
    return QLocale().toString(QDateTime.fromSecsSinceEpoch(int(t)).date(), QLocale.FormatType.ShortFormat)


def _describe(g, items, facts, repos, st):
    kind, pk = g["kind"], g["packages"]
    n = len(pk) or len(g["items"])
    names = sorted(pk)
    g["more"] = [f"{name}  {pk[name]}".rstrip() for name in names]
    if kind in (BEHIND, THIRDPARTY):
        repo = repos.get(g["repo"]) or {"alias": g["repo"]}
        rname = repo.get("name") or g["repo"]
        if kind == BEHIND:
            g["title"] = ngettext("{n} package from {repo} is not up to date yet",
                                  "{n} packages from {repo} are not up to date yet", n).format(n=n, repo=rname)
            g["detail"], g["advice"] = _behind_text(g["repo"], repo, st, n)
        else:
            g["title"] = ngettext("{n} package from {repo} does not fit with this update",
                                  "{n} packages from {repo} do not fit with this update", n).format(n=n, repo=rname)
            g["detail"], g["advice"] = _thirdparty_text(g["repo"], repo, st, n)
        g["color"] = ORANGE
        g["repo_name"] = rname
    elif kind == GONE:
        users = facts.get("users") or {}
        used = sorted({u for name in names for u in users.get(name, []) if u not in pk})
        g["title"] = ngettext("{n} package no longer exists in Tumbleweed",
                              "{n} packages no longer exist in Tumbleweed", n).format(n=n)
        if used:
            # the verb goes with {names}, never with {users}
            g["detail"] = ngettext("{names}: no repository has it any more, but it is still used by {users}.",
                                   "{names}: no repository has them any more, but they are still used by {users}.",
                                   n).format(names=_who(names), users=_who(used))
            g["advice"] = ngettext("Usually it is kept until what uses it is updated or removed.",
                                   "Usually they are kept until what uses them is updated or removed.", n)
        elif all(name in users for name in names):
            g["detail"] = ngettext("{names}: nothing you have uses it.", "{names}: nothing you have uses them.",
                                   n).format(names=_who(names))
            programs = [name for name in names if facts["installed"].get(name, {}).get("program", True)]
            if programs:  # a program somebody may use is never removed on its own
                g["advice"] = ngettext("{names} is a program: it is kept while you use it.",
                                       "{names} are programs: they are kept while you use them.",
                                       len(programs)).format(names=_who(programs))
            else:
                g["advice"] = ngettext("It can be removed.", "They can be removed.", n)
        else:
            g["detail"] = ngettext("{names}: no repository has it any more.",
                                   "{names}: no repository has them any more.", n).format(names=_who(names))
            g["advice"] = ""
        g["color"] = GREY
    elif kind == LOCKED:
        g["title"] = ngettext("{n} locked package is in the way", "{n} locked packages are in the way", n).format(n=n)
        temp = _temp_locks(facts)
        mine = [name for name in names if name in temp]
        if mine and len(mine) == len(names):
            # locks that "leave these as they are" made earlier: say so, and whether they can go now
            recs = [temp[name] for name in mine]
            g["detail"] = ngettext("Zypp Desk locked {names} on {date} to leave an earlier problem as it was, so the "
                                   "update cannot change it.",
                                   "Zypp Desk locked {names} on {date} to leave an earlier problem as it was, so the "
                                   "update cannot change them.", n).format(
                names=_who(names), date=_date(min(int(r.get("added") or 0) for r in recs)))
            rnames = list(dict.fromkeys((repos.get(r.get("repo")) or {}).get("name") or r.get("repo")
                                        for r in recs if r.get("repo")))
            if rnames and all(r.get("caught_up") for r in recs if r.get("repo")):
                g["advice"] = ngettext("{repo} has published since then: unlock it in Locks and simulate again.",
                                       "{repo} has published since then: unlock them in Locks and simulate again.",
                                       n).format(repo=", ".join(rnames))
            elif rnames:
                g["advice"] = ngettext("Unlock it in Locks once {repo} has caught up.",
                                       "Unlock them in Locks once {repo} has caught up.", n).format(
                    repo=", ".join(rnames))
            else:
                g["advice"] = ngettext("Unlock it in Locks when you no longer need it.",
                                       "Unlock them in Locks when you no longer need them.", n)
        else:
            g["detail"] = ngettext("You locked {names}, so the update cannot change it.",
                                   "You locked {names}, so the update cannot change them.", n).format(
                names=_who(names))
            g["advice"] = ngettext("Usually: if you locked it on purpose, leave it as it is; if not, unlock it in "
                                   "Locks.",
                                   "Usually: if you locked them on purpose, leave them as they are; if not, unlock "
                                   "them in Locks.", n)
        g["color"] = BLUE
    else:
        n = len(g["items"])
        g["title"] = ngettext("{n} other problem", "{n} other problems", n).format(n=n)
        g["detail"] = ngettext("It has a cause of its own.", "They have different causes.", n)
        g["advice"] = _("Look at them one by one.")
        g["color"] = PURPLE
        g["more"] = [items[i].get("text") or "" for i in g["items"]]
    g["count"] = n


def analyse(items, queued=(), temp_locks=()):
    """group(items, collect(items)): the groups and the solutions that leave each problem as it is. temp_locks:
    settings["temp_locks"] (the locks an earlier "leave these as they are" made)."""
    facts = collect(items)
    facts["temp_locks"] = list(temp_locks or [])
    groups, keeps = group(items, facts, queued)
    return dict(groups=groups, keeps=keeps, facts=facts)


def fallback(items):
    """What analyse() gives when the packages could not be looked up: every problem is 'other', and none can be
    left as it is from here."""
    parsed = []
    for it in items:
        try:
            parsed.append(parse_problem(it))
        except Exception:
            parsed.append(dict(solvables=[], needs=[], unlock=[], locks=[]))
    facts = dict(parsed=parsed, repos=[], locks=[], installed={}, providers={}, search=None, users={}, state={},
                 family={}, temp_locks=[], errors=["not looked up"])
    groups, _keeps = group(items, facts)
    return dict(groups=groups, keeps=[None] * len(items), facts=facts)


# ------------------------------------------------------------------ "Update the rest and leave these as they are"

def _join(parts):
    """'a', or 'a, b and c' (translatable)."""
    parts = list(parts)
    if len(parts) <= 1:
        return "".join(parts)
    return _("{list} and {last}").format(list=", ".join(parts[:-1]), last=parts[-1])


def _cap(text):
    return text[:1].upper() + text[1:]


def keep_summary(groups, keeps):
    """What "Update the rest and leave these as they are" does, said honestly:
    dict(title (what really happens: "Update the rest: lock 12 packages and remove 2"), lines (one short line per
    cause), warning ('' or which problems will come back), locks, removes, discards (how many packages),
    holds (Tumbleweed packages held back with them))."""
    lines, back = [], []
    cannot = 0
    all_locks, all_rms, all_disc, holds = set(), set(), set(), []
    for g in groups:
        locks, rms, n_lock, n_rm, n_no = set(), set(), 0, 0, 0
        for i in g["items"]:
            s = keeps[i]
            if s is None:
                n_no += 1
                continue
            acts = s.get("actions") or []
            if acts and all(isinstance(a, dict) for a in acts):  # a queued install leaves the queue
                all_disc |= {a["target"] for a in acts}
            elif acts and all(not isinstance(a, dict) and a.kind == "remove" for a in acts):
                rms |= {a.target for a in acts}
                n_rm += 1
            else:
                locks |= {a.target for a in acts if not isinstance(a, dict) and a.kind == "lock"}
                n_lock += 1
                holds += [h for h in s.get("holds_tw") or [] if h not in holds]
        all_locks |= locks
        all_rms |= rms
        every = len(g["items"])  # the whole group goes one way: the same number as its title
        kind = g["kind"]
        # the lines count packages, as the title does ("lock 12 packages"): what is locked for a cause is its
        # packages and what they need from the same place; Tumbleweed's own get a line of their own
        if n_rm:
            n = len(rms)
            lines.append(ngettext("{n} package that no longer exists: removed.",
                                  "{n} packages that no longer exist: removed.", n).format(n=n))
        n = len(locks - set(holds))
        if n_lock and n:
            if kind in (BEHIND, THIRDPARTY):
                lines.append(ngettext("{n} package from {repo}: locked at the version you have.",
                                      "{n} packages from {repo}: locked at the versions you have.", n).format(
                    n=n, repo=g.get("repo_name") or g["repo"]))
            elif kind == LOCKED:
                lines.append(ngettext("{n} package that the locked ones need: locked at the version you have.",
                                      "{n} packages that the locked ones need: locked at the versions you have.",
                                      n).format(n=n))
            elif kind == GONE:
                lines.append(ngettext("{n} package that no longer exists: locked as it is.",
                                      "{n} packages that no longer exist: locked as they are.", n).format(n=n))
            else:
                lines.append(ngettext("Other problems: {n} package locked at the version you have.",
                                      "Other problems: {n} packages locked at the versions you have.", n).format(n=n))
        if n_no:
            cannot += n_no
            n = g["count"] if n_no == every and kind != OTHER else n_no
            if kind in (BEHIND, THIRDPARTY):
                back.append(ngettext("the one from {repo}", "the {n} from {repo}", n).format(
                    n=n, repo=g.get("repo_name") or g["repo"]))
            elif kind == LOCKED:
                back.append(ngettext("the locked one", "the {n} locked ones", n).format(n=n))
            elif kind == GONE:
                back.append(ngettext("the one that no longer exists", "the {n} that no longer exist", n).format(n=n))
            else:
                back.append(ngettext("the other one", "the other {n}", n).format(n=n))
    if all_disc:
        lines.append(ngettext("{names}: leaves the queue.", "{names}: leave the queue.", len(all_disc)).format(
            names=_who(sorted(all_disc))))
    if holds:  # never silently: Tumbleweed's own packages stay behind with them
        lines.append(ngettext("{names} (from Tumbleweed): also locked at the version you have, because what is kept "
                              "needs exactly that one.",
                              "{n} packages from Tumbleweed ({names}): also locked at the versions you have, because "
                              "what is kept needs exactly those.", len(holds)).format(n=len(holds), names=_who(holds)))
    parts = []
    if all_locks:
        parts.append(ngettext("lock {n} package", "lock {n} packages", len(all_locks)).format(n=len(all_locks)))
    if all_rms:
        parts.append(ngettext("remove {n} package", "remove {n} packages", len(all_rms)).format(n=len(all_rms)))
    if all_disc:
        parts.append(ngettext("take {n} package out of the queue", "take {n} packages out of the queue",
                              len(all_disc)).format(n=len(all_disc)))
    title = _("Update the rest: {what}").format(what=_join(parts)) if parts else ""
    warning = ""
    if cannot:
        warning = _cap(ngettext("{what} cannot be left as it is: it will come back in the next simulation.",
                                "{what} cannot be left as they are: they will come back in the next simulation.",
                                cannot).format(what=_join(back)))
    return dict(title=title, lines=lines, warning=warning, locks=len(all_locks), removes=len(all_rms),
                discards=len(all_disc), holds=holds)


def keep_text(groups, keeps):
    """keep_summary() in one paragraph: its lines, then its warning."""
    k = keep_summary(groups, keeps)
    return " ".join(k["lines"] + ([k["warning"]] if k["warning"] else []))


def lock_count(keeps):
    """How many packages "leave these as they are" locks in all."""
    return len({a.target for s in keeps if s for a in s["actions"] if not isinstance(a, dict) and a.kind == "lock"})


# ------------------------------------------------------------------ the locks "leave these as they are" made

def temp_lock_records(groups, keeps, now=None):
    """The locks that "Update the rest and leave these as they are" adds, to remember in settings["temp_locks"]:
    [dict(spec, type, repo (the alias of the repository whose packages were kept, '' when none), added (Unix
    time), tw (a Tumbleweed package held back with them))]."""
    now = int(now or time.time())
    out = {}
    for g in groups:
        for i in g["items"]:
            s = keeps[i] if i < len(keeps) else None
            if not s:
                continue
            tw = set(s.get("holds_tw") or [])
            for a in s.get("actions") or []:
                if not isinstance(a, dict) and a.kind == "lock":
                    out.setdefault((a.target, a.opts.get("type", "package")), dict(
                        spec=a.target, type=a.opts.get("type", "package"), repo=g.get("repo") or "", added=now,
                        tw=a.target in tw))
    return list(out.values())


def remember_temp_locks(settings, records):
    """Add the records to settings["temp_locks"] (a record of the same lock is replaced) and save."""
    if not records:
        return
    have = {(r["spec"], r.get("type", "package")) for r in records}
    old = [r for r in settings.get("temp_locks") or []
           if isinstance(r, dict) and (r.get("spec"), r.get("type", "package")) not in have]
    settings["temp_locks"] = old + list(records)
    settings.save()


def forget_temp_locks(settings, specs):
    """Take these locks out of settings["temp_locks"] (after they are unlocked) and save."""
    specs = set(specs)
    old = [r for r in settings.get("temp_locks") or [] if isinstance(r, dict)]
    keep = [r for r in old if r.get("spec") not in specs]
    if len(keep) != len(old):
        settings["temp_locks"] = keep
        settings.save()


def temp_locks(settings, locks, repos=None, published=None):
    """The locks an earlier "leave these as they are" made that still exist: a list of the records of
    settings["temp_locks"], each with 'caught_up' (True when its repository has published since the lock was
    made: it can probably go now) and 'repo_name'. locks: backend.locks(); repos: backend.repos() (for the
    names); published: {alias: Unix time}, or None to read it from zypper's cache (repostate.published_at)."""
    names = {lk["name"] for lk in locks or []}
    by_alias = {r["alias"]: r for r in repos or []}
    out = []
    for r in settings.get("temp_locks") or []:
        if not isinstance(r, dict) or r.get("spec") not in names:
            continue
        r = dict(r)
        repo = r.get("repo") or ""
        t = (published or {}).get(repo) if published is not None else (repostate.published_at(repo) if repo else None)
        try:
            r["caught_up"] = bool(repo and t and t > int(r.get("added") or 0))
        except (TypeError, ValueError):
            r["caught_up"] = False
        r["repo_name"] = (by_alias.get(repo) or {}).get("name") or repo
        out.append(r)
    return out


def temp_locks_note(records):
    """One sentence about the locks an earlier "leave these as they are" made (temp_locks()), or ''."""
    if not records:
        return ""
    names = [r["spec"] for r in records]
    n = len(names)
    date = _date(min(int(r.get("added") or 0) for r in records))
    text = _("Zypp Desk locked {names} on {date} to leave a problem as it was.").format(names=_who(names), date=date)
    caught = list(dict.fromkeys(r["repo_name"] for r in records if r.get("caught_up") and r.get("repo_name")))
    if caught:
        text += " " + ngettext("{repo} has published since then: you can probably unlock it in Locks.",
                               "{repo} has published since then: you can probably unlock them in Locks.", n).format(
            repo=_join(caught))
    else:
        text += " " + ngettext("You can unlock it in Locks.", "You can unlock them in Locks.", n)
    return text
