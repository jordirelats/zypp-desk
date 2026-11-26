# SPDX-License-Identifier: GPL-3.0-or-later
"""Update in parts when the whole upgrade does not fit on the disk at once. No widgets, nothing as root.

Jordi (26/11/2026): "when it downloads in parts we cannot depend on a download failing: it must look at the
dependencies and update the selected packages normally". So each part is a normal, consistent zypper transaction
that downloads everything it needs first and only then installs:

    zypper -n -v update --download-in-advance [-l] [--no-recommends] [--repo X] -- name1 name2 …

- 'zypper update' with names updates those packages, and the solver brings in what they need ('man zypper':
  "To update individual packages, specify one or more package names"). It does not change vendor, and updates
  that need a decision are left out ("The following package updates will NOT be installed"): the full update
  at the end does them.
- With -n, a dependency problem is answered with 'cancel' ("Choose from above solutions by number or cancel"):
  zypper stops and changes nothing, and the parts after it do not run (they are chained with '&&').
- '--download-in-advance' ("First download all packages, then start installing"): if a download fails, nothing
  of that part is installed. zypper itself does not check the free space before installing (libzypp only
  counts it for the summary); rpm checks it package by package while installing. So every part must fit with
  room to spare, and this plan keeps a margin.
- zypper deletes what it downloaded once it is installed, except for repositories that keep their packages
  (keeppackages=1 in /etc/zypp/repos.d, or a .keep_packages file in the package cache): those stay and add up.
- After the parts, a normal 'zypper dist-upgrade' finishes what is left (new packages, removals, vendor
  changes, downgrades); it only downloads that.

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
# the groups of a simulation that a normal update in parts cannot do: the final dup does them
_FINAL_ONLY = ("to-downgrade", "to-reinstall", "to-change-vendor", "to-change-arch")
_DOWNLOADED = ("to-install", "to-upgrade", "to-downgrade", "to-reinstall", "to-change-vendor", "to-change-arch")


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
def update_options(dup_args, licenses):
    """What a part takes from the dup it stands for: dict(licenses, no_recommends, repo). dup_args: the dup's
    arguments from its subcommand on ('dist-upgrade', '--no-recommends', '--from', 'X'…)."""
    a = list(dup_args)
    repo = None
    for i, x in enumerate(a):
        if x == "--from" and i + 1 < len(a):
            repo = a[i + 1]
        elif x.startswith("--from="):
            repo = x.split("=", 1)[1]
    return dict(licenses=bool(licenses), no_recommends="--no-recommends" in a, repo=repo)


def part_args(names, opts):
    """The arguments of one part (after zypper's global options)."""
    args = ["update", "--download-in-advance"]
    if opts.get("licenses"):
        args.append("--auto-agree-with-licenses")
    if opts.get("no_recommends"):
        args.append("--no-recommends")
    if opts.get("repo"):
        args += ["--repo", opts["repo"]]
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
    """(items the parts can update, items only the final dup can do). A part may only update: same vendor, a
    newer version of an installed package."""
    final_names = {it["name"] for it in _package_items(result, _FINAL_ONLY)}
    ok, rest = [], []
    for it in _package_items(result, ("to-upgrade",)):
        same_vendor = not (it.get("vendor") and it.get("old_vendor")) or it["vendor"] == it["old_vendor"]
        (ok if it["name"] not in final_names and same_vendor else rest).append(it)
    return ok, rest


def plan(result, space, info=None, kept=None, snapshots=None):
    """Split the upgrade of a simulation into parts that fit one after another.

    space: diskspace.assess() of the same simulation. info: installed_info() (read when None). kept:
    kept_repos() (read when None). Returns dict(parts=[dict(groups=[[names]], names, download, growth)],
    rest=dict(download, growth, count), feasible, reason ('group', 'rest', 'many', 'none' or None), need (the
    room that was missing), free)."""
    snapshots = space["snapshots"] if snapshots is None else snapshots
    items = _package_items(result, _DOWNLOADED)
    ok, _rest_upgrades = eligible(result)
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
        e = est.setdefault(it["name"], dict(download=0.0, growth=0.0, kept=0.0))
        e["download"] += dl
        e["growth"] += growth
        e["kept"] += dl if kept_pkg else 0.0
    ok_names = {it["name"] for it in ok}
    # groups of related packages (built from the same source package)
    groups = {}
    for name in sorted(ok_names):
        src = info.get(name, (0, name))[1] or name
        groups.setdefault(src, []).append(name)
    glist = []
    for names in groups.values():
        glist.append(dict(names=names, download=sum(est[n]["download"] for n in names),
                          growth=sum(est[n]["growth"] for n in names), kept=sum(est[n]["kept"] for n in names)))
    glist.sort(key=lambda g: (-(g["download"] + g["growth"]), g["names"][0]))
    rest_names = [n for n in est if n not in ok_names]
    rest = dict(download=int(sum(est[n]["download"] for n in rest_names)),
                growth=int(sum(est[n]["growth"] for n in rest_names)), count=len(rest_names))

    root, cache, same = space["root"], space["cache"], space["same"]
    mp, mpc = space["margin"], diskspace.margin(cache["total"])  # the same room to spare as the whole update
    used = dict(root=0.0, cache=0.0)  # what the parts already done keep (installed files, kept downloads)

    def room(dl, growth):
        """How much is missing for a part (0 when it fits)."""
        if same:
            return max(0.0, dl + growth + mp - (root["free"] - used["root"]))
        return max(0.0, growth + mp - (root["free"] - used["root"]), dl + mpc - (cache["free"] - used["cache"]))

    out = dict(parts=[], rest=rest, feasible=True, reason=None, need=0, free=root["free"])
    if not glist:
        out.update(feasible=False, reason="none")
        return out
    todo = list(glist)
    while todo:
        part = dict(groups=[], download=0.0, growth=0.0, kept=0.0)
        for g in list(todo):
            if room(part["download"] + g["download"], part["growth"] + g["growth"]) == 0:
                part["groups"].append(g["names"])
                for k in ("download", "growth", "kept"):
                    part[k] += g[k]
                todo.remove(g)
        if not part["groups"]:
            g = todo[0]
            out.update(feasible=False, reason="group",
                       need=int(g["download"] + g["growth"] + mp))
            return out
        out["parts"].append(part)
        used["root"] += part["growth"] + (part["kept"] if same else 0)
        used["cache"] += part["kept"] if not same else 0
        if len(out["parts"]) > MAX_PARTS:
            out.update(feasible=False, reason="many")
            return out
    missing = room(rest["download"], rest["growth"])
    if missing:
        out.update(feasible=False, reason="rest", need=int(rest["download"] + rest["growth"] + used["root"] + mp))
    for part in out["parts"]:
        part["names"] = [n for g in part["groups"] for n in g]
        part["download"], part["growth"] = int(part["download"]), int(part["growth"])
        del part["kept"]
    return out


def split_first(p):
    """The same plan with its first part cut in two (by groups), when the solver needs more room for it than
    planned. None when the first part is a single group."""
    first = p["parts"][0]
    if len(first["groups"]) < 2:
        return None
    half = (len(first["groups"]) + 1) // 2
    out = dict(p, parts=[])
    for groups in (first["groups"][:half], first["groups"][half:]):
        share = sum(len(g) for g in groups) / max(1, len(first["names"]))
        out["parts"].append(dict(groups=groups, names=[n for g in groups for n in g],
                                 download=int(first["download"] * share), growth=int(first["growth"] * share)))
    out["parts"] += p["parts"][1:]
    return out


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
