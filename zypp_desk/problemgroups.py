# SPDX-License-Identifier: GPL-3.0-or-later
"""The problems of an upgrade grouped by what causes them, in plain words, with what is usually done.

Jordi (26/11/2026) did not know what to do with an upgrade that shows dozens of unmet dependencies. On
Tumbleweed they nearly always come from a few causes:
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
"""

import fnmatch
import re
import subprocess

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
                solvables.append(sv)
    unlock = []
    for sol in item.get("solutions") or []:
        unlock += re.findall(r"remove lock to allow (?:removal|installation) of (\S+)", sol.get("raw") or "")
    return dict(solvables=solvables, needs=list(dict.fromkeys(needs)),
                unlock=list(dict.fromkeys(sim.split_solvable(u)["name"] for u in unlock)))


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
                 state={}, errors=[])
    for key, fn in (("repos", backend.repos), ("locks", backend.locks)):
        try:
            facts[key] = fn()
        except Exception as e:
            facts["errors"].append(f"{key}: {e}")
    try:
        facts["installed"], facts["providers"] = _rpm_lookup(names, sorted(caps))
    except Exception as e:
        facts["errors"].append(f"rpm: {e}")
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


def _lock_solution(names):
    text = (_("Keep {pkg} as it is").format(pkg=names[0]) if len(names) == 1 else
            _("Keep {list} as they are").format(list=", ".join(names)))
    return dict(number=0, text=text,
                detail=_("The rest goes ahead. They stay at their versions and are locked: they will not change "
                         "until you unlock them."),
                detail_now=_("The rest goes ahead. They stay at their versions and are locked: they will not "
                             "change until you unlock them."),
                raw="", actions=[Action("lock", n) for n in names], possible=True, recommended=False,
                synthesized=True)


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
      Tumbleweed's own packages are not locked (that would hold back the whole upgrade), except for a problem
      of a package you locked yourself: keeping it as it is means keeping what it needs too.
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
    allow_tw = kind == LOCKED
    vendors = {installed[sv["name"]]["vendor"] for sv in svs if sv["name"] in installed}
    if not allow_tw:
        vendors = {v for v in vendors if not _tw_vendor(v)}

    def lockable(name):
        inst = installed.get(name)
        return bool(inst) and not _locked(name, patterns) and (allow_tw or not _tw_vendor(inst["vendor"]))
    names = [sv["name"] for sv in svs if lockable(sv["name"])]
    for cap in parsed["needs"]:
        names += [n for n in facts["providers"].get(cap, []) if lockable(n) and installed[n]["vendor"] in vendors]
    names = list(dict.fromkeys(names))
    for s in sols:
        locks = _targets(s, "lock")
        if locks and len(locks) == len(s["actions"]) and (allow_tw or set(locks) <= set(names)):
            return s
    return _lock_solution(names) if names else None


def _behind_text(alias, repo, st):
    """What a repository that is late usually does, and the advice."""
    name = (repo or {}).get("name") or alias
    url = ((repo or {}).get("url") or "").lower()
    low = (alias + " " + name + " " + url).lower()
    if "videolan" in low:
        what = _("VideoLAN publishes the codecs a few days after each Tumbleweed version. It happens often and "
                 "fixes itself.")
    elif "packman" in low:
        what = _("Packman rebuilds its packages a day or two after each Tumbleweed version. It happens often and "
                 "fixes itself.")
    elif "download.opensuse.org/repositories/" in url:
        what = _("{repo} is a project of the openSUSE Build Service: it rebuilds its packages for each new "
                 "Tumbleweed, usually within a day or two.").format(repo=name)
    else:
        what = _("{repo} has not published anything since the new Tumbleweed came out. Repositories built on top "
                 "of Tumbleweed usually catch up in a day or two.").format(repo=name)
    hours = (st or {}).get("tw_hours")
    if hours is not None and hours >= CATCH_UP_HOURS:
        days = int(hours // 24)
        advice = ngettext("It has had {n} day to catch up. Wait a little longer, or leave them as they are for now.",
                          "It has had {n} days to catch up. Wait a little longer, or leave them as they are for now.",
                          days).format(n=days)
    else:
        advice = _("Recommended: wait a day or two.")
    return what, advice


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
            g["detail"], g["advice"] = _behind_text(g["repo"], repo, st)
        else:
            g["title"] = ngettext("{n} package from {repo} does not fit with this update",
                                  "{n} packages from {repo} do not fit with this update", n).format(n=n, repo=rname)
            g["detail"] = _("{repo} is not part of Tumbleweed: its packages were made for the versions you have now, "
                            "not for the ones this update brings.").format(repo=rname)
            g["advice"] = _("Usually: leave them as they are for now and try again in a few days.")
        g["color"] = ORANGE
        g["repo_name"] = rname
    elif kind == GONE:
        users = facts.get("users") or {}
        used = sorted({u for name in names for u in users.get(name, []) if u not in pk})
        g["title"] = ngettext("{n} package no longer exists in Tumbleweed",
                              "{n} packages no longer exist in Tumbleweed", n).format(n=n)
        if used:
            g["detail"] = ngettext("{names}: no repository has it any more, and {users} still uses it.",
                                   "{names}: no repository has them any more, and {users} still use them.", n).format(
                names=_who(names), users=_who(used))
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
        g["detail"] = ngettext("You locked {names}, so the update cannot change it.",
                               "You locked {names}, so the update cannot change them.", n).format(names=_who(names))
        g["advice"] = _("Usually: if you locked them on purpose, leave these as they are; if not, unlock them in "
                        "Locks.")
        g["color"] = BLUE
    else:
        n = len(g["items"])
        g["title"] = ngettext("{n} other problem", "{n} other problems", n).format(n=n)
        g["detail"] = ngettext("It has a cause of its own.", "They have different causes.", n)
        g["advice"] = _("Look at them one by one.")
        g["color"] = PURPLE
        g["more"] = [items[i].get("text") or "" for i in g["items"]]
    g["count"] = n


def analyse(items, queued=()):
    """group(items, collect(items)): the groups and the solutions that leave each problem as it is."""
    facts = collect(items)
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
            parsed.append(dict(solvables=[], needs=[], unlock=[]))
    facts = dict(parsed=parsed, repos=[], locks=[], installed={}, providers={}, search=None, users={}, state={},
                 errors=["not looked up"])
    groups, keeps = group(items, facts)
    return dict(groups=groups, keeps=[None] * len(items), facts=facts)


# ------------------------------------------------------------------ "Update the rest and leave these as they are"

def keep_text(groups, keeps):
    """What "Update the rest and leave these as they are" does, said honestly, from the solutions of keeps."""
    held, removed, discarded, cannot = [], [], set(), 0
    n_held = n_removed = 0
    for g in groups:
        mine = set(g["packages"])
        locks, rms, n_lock, n_rm = set(), set(), 0, 0
        for i in g["items"]:
            s = keeps[i]
            if s is None:
                cannot += 1
                continue
            acts = s.get("actions") or []
            if acts and all(isinstance(a, dict) for a in acts):  # a queued install leaves the queue
                discarded |= {a["target"] for a in acts}
            elif acts and all(not isinstance(a, dict) and a.kind == "remove" for a in acts):
                rms |= {a.target for a in acts}
                n_rm += 1
            else:
                locks |= {a.target for a in acts if not isinstance(a, dict) and a.kind == "lock"}
                n_lock += 1
        every = len(g["items"])  # the whole group goes one way: the same number as its title
        if n_rm:
            n = g["count"] if n_rm == every and g["kind"] != OTHER else (len(rms & mine) or n_rm)
            removed.append(ngettext("the one that no longer exists", "the {n} that no longer exist", n).format(n=n))
            n_removed += n
        if n_lock:
            if g["kind"] == OTHER:
                n = n_lock
                held.append(ngettext("the other one", "the other {n}", n).format(n=n))
            else:
                n = g["count"] if n_lock == every else (len(locks & mine) or n_lock)
                if g["kind"] in (BEHIND, THIRDPARTY):
                    held.append(ngettext("the one from {repo}", "the {n} from {repo}", n).format(
                        n=n, repo=g.get("repo_name") or g["repo"]))
                elif g["kind"] == LOCKED:
                    held.append(ngettext("the locked one", "the {n} locked ones", n).format(n=n))
                else:
                    held.append(ngettext("the one that no longer exists", "the {n} that no longer exist",
                                         n).format(n=n))
            n_held += n
    parts = []
    if held:
        text = ngettext("{what} stays as it is now and is locked until you unlock it.",
                        "{what} stay as they are now and are locked until you unlock them.", n_held).format(
            what=", ".join(held))
        parts.append(text[:1].upper() + text[1:])
    if removed:
        text = ngettext("{what} is removed.", "{what} are removed.", n_removed).format(what=", ".join(removed))
        parts.append(text[:1].upper() + text[1:])
    if discarded:
        parts.append(ngettext("{names} leaves the queue.", "{names} leave the queue.", len(discarded)).format(
            names=_who(sorted(discarded))))
    if cannot:
        parts.append(ngettext("Zypp Desk cannot leave {n} problem as it is: it will appear again after the "
                              "simulation.",
                              "Zypp Desk cannot leave {n} problems as they are: they will appear again after the "
                              "simulation.", cannot).format(n=cannot))
    return " ".join(parts)


def lock_count(keeps):
    """How many packages "leave these as they are" locks in all."""
    return len({a.target for s in keeps if s for a in s["actions"] if not isinstance(a, dict) and a.kind == "lock"})
