# SPDX-License-Identifier: GPL-3.0-or-later
"""Update in parts when the whole upgrade does not fit on the disk at once. No widgets, nothing as root.

Jordi (26/11/2026): "when it downloads in parts we cannot depend on a download failing: it must look at the
dependencies and update the selected packages normally". So each part is a normal, consistent zypper transaction
that downloads everything it needs first and only then installs:

    zypper -n -v update --download-in-advance [-l] [--no-recommends] [--repo X] -- name1 name2 …

- 'zypper update' with names updates those packages, and the solver brings in what they need ('man zypper':
  "To update individual packages, specify one or more package names"). It keeps the vendor: an update that
  changes vendor is not done ("There is an update candidate for 'E' from vendor 'Packman'…"), and the full
  update at the end does it.
- One named package whose update needs a decision (it conflicts with an installed package that a dup would drop,
  it needs a library that a dup would take back to an older version…) makes the whole part fail: with -n zypper
  answers 'cancel' ("Choose from above solutions by number or cancel"), stops with exit code 4 and changes
  nothing, not even the other packages of the part. ("The following package updates will NOT be installed" is
  only for 'zypper update' without names.) So, before anything runs, EVERY part is simulated (review()): the
  packages a problem names go, with their group, to the full update at the end, which takes those decisions;
  a part that changes something the full update would not is cut in two until the package that does it is
  found. The plan is then made again with the sizes the simulations gave, and a part that still does not fit
  is cut in two. Each part is simulated on the system as it is now: a problem that only shows once the parts
  before it are installed cannot be seen. Then that part stops without changing anything, and the chain stops
  there (the parts are joined with '&&', and so is the full update at the end).
- '--download-in-advance' ("First download all packages, then start installing"): if a download fails, nothing
  of that part is installed. What it downloaded stays in /var/cache/zypp/packages (zypper keeps it after an
  abort): the next try does not download it again, and 'zypper clean' throws it away. zypper itself does not
  check the free space before installing (libzypp only counts it for the summary); rpm checks it package by
  package while installing. So every part must fit with room to spare, and this plan keeps a margin.
- zypper deletes what it downloaded once it is installed, except for repositories that keep their packages
  (keeppackages=1 in /etc/zypp/repos.d, or a .keep_packages file in the package cache): those stay and add up.
- 'dup --from X' upgrades what X has, and every enabled repository is seen for what that needs. 'update --repo
  X' would load X only ("nothing provides 'B >= 2'"), so the parts get no repository option: their names are
  the ones the '--from' simulation upgrades, and review() checks that each part takes them to the same version.
  'dup --repo X' sees only X, and so do its parts ('update --repo X').
- After the parts, a normal 'zypper dist-upgrade' finishes what is left (new packages, removals, vendor
  changes, downgrades, the packages that need a decision); it only downloads that.

The plan: the packages of the simulation that are only updated go into parts. Related packages stay together:
they are grouped by the source package they were built from (SOURCERPM of the installed package). The size of
each package is not in the simulation: it is worked out from the installed size, scaled so that the whole adds
up to the simulation's download size (and its space difference). Groups are packed, biggest first, into parts
that fit one after another (what each part installs stays, so the next part has less room). A group bigger
than one part gets a part of its own; if it does not fit even alone, it cannot be done in parts.
"""

import os
import re
import subprocess

from . import diskspace

REPOS_D = "/etc/zypp/repos.d"
MAX_PARTS = 40
NAMES_SHOWN = 8
# review(): how many times the parts are simulated again before giving up (cutting a part of 1,000 names in
# two until one group is left takes about 10)
MAX_ROUNDS = 10
# the groups of a simulation that a normal update in parts cannot do: the final dup does them
_FINAL_ONLY = ("to-downgrade", "to-reinstall", "to-change-vendor", "to-change-arch")
_DOWNLOADED = ("to-install", "to-upgrade", "to-downgrade", "to-reinstall", "to-change-vendor", "to-change-arch")
# a package in zypper's problem texts: name-version-release.arch
_SOLVABLE = re.compile(r"^(.+)-[^-]+-[^-]+\.(?:noarch|x86_64(?:_v[234])?|i[3-6]86|athlon|pentium4|aarch64(?:_ilp32)?|"
                       r"armv\w+|ppc64(?:le|p7)?|ppc|s390x?|riscv64|loongarch64|ia64|sparc64|mips64)$")


# ------------------------------------------------------------------ what the system says
def installed_info(names):
    """{name: (installed size, source package name)} for the installed packages among names: one rpm call, as
    the user. A package installed in several versions (kernels) adds up its sizes."""
    names = sorted({n for n in names if n and not n.startswith("-")})
    if not names:
        return {}
    try:
        p = subprocess.run(["rpm", "-q", "--qf", "%{NAME}\t%{SIZE}\t%{SOURCERPM}\n", "--", *names],
                           capture_output=True, text=True, timeout=300, env=dict(os.environ, LC_ALL="C"),
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) != 3 or not f[1].isdigit():
            continue
        src = f[2]
        m = re.match(r"^(.+)-[^-]+-[^-]+\.(?:no)?src\.rpm$", src)
        src = m.group(1) if m else (f[0] if src in ("", "(none)") else src)
        size = out.get(f[0], (0, src))[0] + int(f[1])
        out[f[0]] = (size, src)
    return out


def kept_repos(repos_d=REPOS_D, cache=diskspace.PKG_CACHE):
    """(every repository keeps its packages, {aliases and names of the ones that do})."""
    if os.path.exists(os.path.join(cache, ".keep_packages")):
        return True, set()
    kept = set()
    try:
        files = [os.path.join(repos_d, f) for f in os.listdir(repos_d) if f.endswith(".repo")]
    except OSError:
        files = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for block in re.split(r"(?m)^\s*\[", text)[1:]:
            alias, _sep, body = block.partition("]")
            if re.search(r"(?mi)^\s*keeppackages\s*=\s*1\s*$", body):
                kept.add(alias.strip())
                m = re.search(r"(?m)^\s*name\s*=\s*(.+?)\s*$", body)
                if m:
                    kept.add(m.group(1))
    return False, kept


# ------------------------------------------------------------------ the options of a part
def _option_values(a, long, short=None):
    """The values of an option that can be given several times ('--from X', '--from=X', '-r X')."""
    out = []
    for i, x in enumerate(a):
        if (x == long or (short and x == short)) and i + 1 < len(a):
            out.append(a[i + 1])
        elif x.startswith(long + "="):
            out.append(x.split("=", 1)[1])
    return out


def update_options(dup_args, licenses):
    """What a part takes from the dup it stands for: dict(licenses, no_recommends, repos, from_repos, globals).
    dup_args: the dup's arguments, global options first ('--no-refresh', 'dist-upgrade', '--from', 'X'…).
    repos: the dup's --repo (the parts see only those too); from_repos: its --from, only to say it (the parts
    get no repository option, see above); globals: the global options before the subcommand."""
    a = list(dup_args)
    sub = next((i for i, x in enumerate(a) if x in ("dist-upgrade", "dup")), 0)
    rest = a[sub + 1:] if a[sub:sub + 1] in (["dist-upgrade"], ["dup"]) else a
    return dict(licenses=bool(licenses), no_recommends="--no-recommends" in rest,
                repos=_option_values(rest, "--repo", "-r"), from_repos=_option_values(rest, "--from"),
                globals=list(a[:sub]) if a[sub:sub + 1] in (["dist-upgrade"], ["dup"]) else [])


def part_args(names, opts):
    """The arguments of one part (after zypper's global options)."""
    args = ["update", "--download-in-advance"]
    if opts.get("licenses"):
        args.append("--auto-agree-with-licenses")
    if opts.get("no_recommends"):
        args.append("--no-recommends")
    for repo in opts.get("repos") or ():
        args += ["--repo", repo]
    return args + ["--", *names]


def shown(words, names, more_text):
    """A command with a long list of names, shortened for the screen: the first NAMES_SHOWN names and
    more_text(n) ('… and 120 more'). The real command has them all."""
    head = " ".join(words)
    if len(names) <= NAMES_SHOWN + 2:
        return " ".join([head, *names])
    return " ".join([head, *names[:NAMES_SHOWN], more_text(len(names) - NAMES_SHOWN)])


# ------------------------------------------------------------------ the plan
def _package_items(result, keys):
    return [it for key in keys for it in (result or {}).get("groups", {}).get(key, [])
            if it.get("kind", "package") == "package"]


def eligible(result):
    """(items the parts can update, items only the final dup can do). A part may only update an installed
    package to a newer version from the same vendor: zypper's XML has no vendor, but a package that changes
    vendor is also listed in <to-change-vendor>, which only the final dup does."""
    final_names = {it["name"] for it in _package_items(result, _FINAL_ONLY)}
    ok, rest = [], []
    for it in _package_items(result, ("to-upgrade",)):
        (ok if it["name"] not in final_names else rest).append(it)
    return ok, rest


def estimates(result, info=None, kept=None, snapshots=False):
    """{name: dict(download, growth, kept, src)} for every package the simulation downloads: what each one takes
    (worked out from the installed size, see above; a new package: the average), how much of its download stays
    in a cache that keeps packages, and its source package (for the groups)."""
    items = _package_items(result, _DOWNLOADED)
    if info is None:
        info = installed_info({it["name"] for it in items})
    all_kept, kept_set = kept_repos() if kept is None else kept
    known = [info[it["name"]][0] for it in items if it["name"] in info and info[it["name"]][0] > 0]
    avg = (sum(known) / len(known)) if known else 4 * diskspace.MiB
    # a package's weight: its installed size (a new package: the average)
    weights = [info[it["name"]][0] if info.get(it["name"], (0,))[0] > 0 else avg for it in items]
    total_w = sum(weights) or 1.0
    download = max(0, diskspace.as_int((result or {}).get("download")))
    diff = diskspace.as_int((result or {}).get("disk"))
    r_dl = download / total_w
    est = {}
    for it, w in zip(items, weights):
        dl = w * r_dl
        growth = max(0.0, w + diff * w / total_w) if snapshots else max(0, diff) * w / total_w
        kept_pkg = all_kept or it.get("repo") in kept_set
        e = est.setdefault(it["name"], dict(download=0.0, growth=0.0, kept=0.0,
                                            src=info.get(it["name"], (0, it["name"]))[1] or it["name"]))
        e["download"] += dl
        e["growth"] += growth
        e["kept"] += dl if kept_pkg else 0.0
    return est


class _Room:
    """The free space while the parts run one after another: what each part needs, and what it keeps."""

    def __init__(self, space):
        self.root, self.cache, self.same = space["root"], space["cache"], space["same"]
        self.mp, self.mpc = space["margin"], diskspace.margin(self.cache["total"])
        self.used_root = self.used_cache = 0.0  # what the parts already done keep (installed files, downloads)

    def missing(self, dl, growth):
        """How much is missing for a part (0 when it fits)."""
        if self.same:
            return max(0.0, dl + growth + self.mp - (self.root["free"] - self.used_root))
        return max(0.0, growth + self.mp - (self.root["free"] - self.used_root),
                   dl + self.mpc - (self.cache["free"] - self.used_cache))

    def keep(self, growth, kept):
        self.used_root += growth + (kept if self.same else 0)
        self.used_cache += kept if not self.same else 0


def _group(names, est):
    return dict(names=list(names), download=sum(est[n]["download"] for n in names),
                growth=sum(est[n]["growth"] for n in names), kept=sum(est[n]["kept"] for n in names))


def _rest(est, in_parts):
    names = [n for n in est if n not in in_parts]
    return dict(download=int(sum(est[n]["download"] for n in names)),
                growth=int(sum(est[n]["growth"] for n in names)), count=len(names))


def _finish(out):
    for part in out["parts"]:
        part["names"] = [n for g in part["groups"] for n in g]
        part["download"], part["growth"] = int(part["download"]), int(part["growth"])
        part.pop("kept", None)
    return out


def plan(result, space, info=None, kept=None, snapshots=None, est=None, exclude=()):
    """Split the upgrade of a simulation into parts that fit one after another.

    space: diskspace.assess() of the same simulation. info: installed_info() (read when None). kept:
    kept_repos() (read when None). est: estimates() (made when None; review() passes them with the sizes the
    simulations of the parts gave). exclude: names left to the final dup (whole groups). Returns dict(parts=
    [dict(groups=[[names]], names, download, growth)], rest=dict(download, growth, count), feasible, reason
    ('group', 'rest', 'many', 'none' or None), need (the free space it needs to work), short (how much of that is
    missing), free, est, exclude, moved (names review() took out of the parts), rounds)."""
    snapshots = space["snapshots"] if snapshots is None else snapshots
    if est is None:
        est = estimates(result, info, kept, snapshots)
    ok, _rest_upgrades = eligible(result)
    exclude = set(exclude)
    ok_names = {it["name"] for it in ok if it["name"] in est} - exclude
    # groups of related packages (built from the same source package)
    groups = {}
    for name in sorted(ok_names):
        groups.setdefault(est[name]["src"], []).append(name)
    glist = [_group(names, est) for names in groups.values()]
    glist.sort(key=lambda g: (-(g["download"] + g["growth"]), g["names"][0]))
    rest = _rest(est, ok_names)

    room = _Room(space)
    out = dict(parts=[], rest=rest, feasible=True, reason=None, need=0, short=0, free=space["root"]["free"],
               est=est, exclude=exclude, moved=[], rounds=0)
    if not glist:
        out.update(feasible=False, reason="none")
        return _finish(out)
    todo = list(glist)
    while todo:
        part = dict(groups=[], download=0.0, growth=0.0, kept=0.0)
        for g in list(todo):
            if room.missing(part["download"] + g["download"], part["growth"] + g["growth"]) == 0:
                part["groups"].append(g["names"])
                for k in ("download", "growth", "kept"):
                    part[k] += g[k]
                todo.remove(g)
        if not part["groups"]:
            # even alone, after what the parts before it keep (code review 0.5, #5: they count too)
            short = room.missing(todo[0]["download"], todo[0]["growth"])
            out.update(feasible=False, reason="group", short=int(short), need=int(out["free"] + short))
            return _finish(out)
        out["parts"].append(part)
        room.keep(part["growth"], part["kept"])
        if len(out["parts"]) > MAX_PARTS:
            out.update(feasible=False, reason="many")
            return _finish(out)
    missing = room.missing(rest["download"], rest["growth"])
    if missing:
        out.update(feasible=False, reason="rest", short=int(missing), need=int(out["free"] + missing))
    return _finish(out)


def part_need(sim_result, snapshots, sizes=None):
    """(download, growth) that a part's own simulation says it needs (with snapshots, the whole size of what it
    writes: sizes = installed sizes of what it replaces, read when None)."""
    download = max(0, diskspace.as_int((sim_result or {}).get("download")))
    diff = diskspace.as_int((sim_result or {}).get("disk"))
    if not snapshots:
        return download, max(0, diff)
    names = [it["name"] for it in _package_items(sim_result, ("to-upgrade", "to-downgrade", "to-reinstall",
                                                              "to-change-vendor", "to-change-arch", "to-remove"))]
    if sizes is None:
        sizes = diskspace.installed_sizes(names)
    return download, max(0, sum(sizes.get(n, 0) for n in set(names)) + diff)


# ------------------------------------------------------------------ before anything runs: every part simulated
def part_key(names):
    return tuple(sorted(names))


def problem_names(res, names):
    """The names among `names` that the solver problems of a part's simulation speak of: the packages whose
    update needs a decision."""
    names = set(names)
    texts = [p.get("raw") or "" for p in res.get("problem_items") or []]
    texts += [t for _kind, t in res.get("messages") or [] if re.match(r"^\s*(Problem|\d+:)", t)]
    found = set()
    for text in texts:
        for cap in re.findall(r"'([^']+)'", text):  # 'R', 'B >= 2', 'vlc-noX = 3-1'
            n = re.split(r"\s*[<>=!]", cap.strip(), maxsplit=1)[0]
            if n in names:
                found.add(n)
        for tok in re.findall(r"[^\s'\"(),]+", text):  # S-2-1.noarch, R-1-1.noarch[repo]
            m = _SOLVABLE.match(re.sub(r"\[[^\]]*\]$", "", tok).rstrip(".:;"))
            if m and m.group(1) in names:
                found.add(m.group(1))
    return found


def unplanned(res, result, names):
    """What a part's simulation would do that the full update (result) would not: (own, other). own: names of the
    part it would take to another version than planned; other: other packages it would install, change or
    remove that the full update does not."""
    planned = {(it["name"], it["edition"]) for it in _package_items(result, _DOWNLOADED)}
    removed = {it["name"] for it in _package_items(result, ("to-remove",))}
    own, other = set(), set()
    for it in _package_items(res, _DOWNLOADED):
        if (it["name"], it["edition"]) not in planned:
            (own if it["name"] in names else other).add(it["name"])
    for it in _package_items(res, ("to-remove",)):
        if it["name"] not in removed:
            other.add(it["name"])
    return own, other


def _listed(res):
    return {it["name"] for it in _package_items(res, _DOWNLOADED)}


def _dl(est, name, avg):
    e = est.get(name)
    return e["download"] if e else avg


def _effective(sim, done, est, avg):
    """(download, growth, kept) of a part when it runs: its simulation's sizes without what the parts before it
    install (the simulation was made on the system as it is now, so it counts their packages it needs too)."""
    listed = _listed(sim["res"])
    w = {n: _dl(est, n, avg) for n in listed}
    total = sum(w.values())
    share = (sum(v for n, v in w.items() if n not in done) / total) if total else 1.0
    new_dl = sum(v for n, v in w.items() if n not in done)
    kept = sum(est[n]["kept"] for n in listed if n in est and n not in done)
    kept_share = (kept / new_dl) if new_dl else 0.0
    d, g = sim["download"] * share, sim["growth"] * share
    return d, g, d * kept_share


def _measured_est(p, sims):
    """The estimates with the sizes the simulations gave: in each part that was simulated without a problem,
    its names share what its simulation needs (as much of it as is theirs, not what they bring in)."""
    est = {n: dict(e) for n, e in p["est"].items()}
    avg = _avg(est)
    for part in p["parts"]:
        sim = sims.get(part_key(part["names"]))
        if not sim or sim["res"].get("problems"):
            continue
        listed = _listed(sim["res"]) | set(part["names"])
        w = {n: _dl(est, n, avg) for n in listed}
        total = sum(w.values())
        own = [n for n in part["names"] if n in est]
        own_w = sum(w[n] for n in own)
        if not total or not own_w:
            continue
        d_own, g_own = sim["download"] * own_w / total, sim["growth"] * own_w / total
        for n in own:
            old_dl = est[n]["download"]
            est[n]["download"] = d_own * w[n] / own_w
            est[n]["growth"] = g_own * w[n] / own_w
            est[n]["kept"] = est[n]["kept"] * (est[n]["download"] / old_dl) if old_dl else 0.0
    return est


def _avg(est):
    return (sum(e["download"] for e in est.values()) / len(est)) if est else 4 * diskspace.MiB


def _trim(p, exclude, split):
    """The plan without the excluded groups, with the parts in `split` cut in two (by groups)."""
    est = p["est"]
    parts = []
    for i, part in enumerate(p["parts"]):
        groups = [g for g in part["groups"] if not set(g) & exclude]
        if not groups:
            continue
        chunks = [groups]
        if i in split and len(groups) > 1:
            half = (len(groups) + 1) // 2
            chunks = [groups[:half], groups[half:]]
        for ch in chunks:
            names = [n for g in ch for n in g]
            g = _group(names, est)
            parts.append(dict(groups=ch, names=names, download=int(g["download"]), growth=int(g["growth"])))
    in_parts = {n for part in parts for n in part["names"]}
    out = dict(p, parts=parts, rest=_rest(est, in_parts), exclude=set(exclude))
    if not parts:
        out.update(feasible=False, reason="none")
    return out


def review(p, sims, result, space):
    """One look at the simulations of the parts of plan p, made before anything runs.

    sims: {part_key(names): dict(res (the parsed simulation of the part), download, growth (part_need()))} for
    every part of p; result: the simulation of the full update. Returns dict(action, plan, moved):
    - 'run': every part keeps to the plan of the full update and fits after the parts before it, with the sizes
      its simulation gave (and the full update at the end fits too);
    - 'again': the plan changed (packages moved to the full update at the end, a part cut in two, or the plan
      made again with the real sizes, once): simulate the parts that are new;
    - 'stop': it cannot be done in parts; plan is not feasible and says why (reason 'group', 'rest', 'none',
      'rounds', or 'error' with error = zypper's message).
    moved: the names taken out of the parts this time; plan['moved'] has them all."""
    est = p["est"]
    exclude = set(p.get("exclude") or ())
    moved, split = [], set()
    for i, part in enumerate(p["parts"]):
        res = sims[part_key(part["names"])]["res"]
        if res.get("problems") and not res.get("problem_items"):
            # not a decision but an error (a repository, zypper itself): it would stop the update as well
            return dict(action="stop", moved=[], plan=dict(p, feasible=False, reason="error",
                                                            error=res["problems"][0]))
        names = set(part["names"])
        own, other = unplanned(res, result, names)
        bad = own | (problem_names(res, names) if res.get("problem_items") else set())
        if bad:
            for g in part["groups"]:
                if set(g) & bad:
                    exclude |= set(g)
                    moved += g
        elif res.get("problem_items") or other:
            if len(part["groups"]) > 1:
                split.add(i)  # the package that does it is not known: cut the part until it is found
            else:
                exclude |= set(part["groups"][0])
                moved += part["groups"][0]
    rounds = p.get("rounds", 0) + 1

    def again(new):
        new.update(moved=list(p.get("moved") or []) + moved, rounds=rounds)
        if not new["feasible"]:
            return dict(action="stop", plan=new, moved=moved)
        if rounds >= MAX_ROUNDS:
            new.update(feasible=False, reason="rounds")
            return dict(action="stop", plan=new, moved=moved)
        return dict(action="again", plan=new, moved=moved)

    if moved or split:
        if moved and not p.get("replanned"):
            new = plan(result, space, est=_measured_est(p, sims), exclude=exclude)
            new["replanned"] = True
        else:
            new = _trim(p, exclude, split)
        return again(new)

    # every part keeps to the plan: does each one fit after the ones before it, with its real sizes?
    room, done, avg = _Room(space), set(), _avg(est)
    for i, part in enumerate(p["parts"]):
        sim = sims[part_key(part["names"])]
        d, g, k = _effective(sim, done, est, avg)
        short = room.missing(d, g)
        if short:
            if not p.get("replanned"):
                new = plan(result, space, est=_measured_est(p, sims), exclude=exclude)
                new["replanned"] = True
                return again(new)
            if len(part["groups"]) > 1:
                return again(_trim(p, exclude, {i}))
            return dict(action="stop", moved=[], plan=dict(p, feasible=False, reason="group", short=int(short),
                                                            need=int(space["root"]["free"] + short)))
        room.keep(g, k)
        done |= _listed(sim["res"]) | set(part["names"])
    left = [n for n in est if n not in done]
    short = room.missing(sum(est[n]["download"] for n in left), sum(est[n]["growth"] for n in left))
    if short:
        return dict(action="stop", moved=[], plan=dict(p, feasible=False, reason="rest", short=int(short),
                                                        need=int(space["root"]["free"] + short)))
    return dict(action="run", plan=p, moved=[])
