# SPDX-License-Identifier: GPL-3.0-or-later
"""Room on the disk for an upgrade: how much there is, how much the upgrade needs, and what can be freed.

Jordi (26/11/2026): when there is little disk space, split a general update into parts. On his laptop / has
6.0 GiB free of 39 GiB. No widgets and nothing as root here: statvfs, the package cache (readable by
everyone) and rpm as the user.

Where the space goes during 'zypper dup':
- By default zypper downloads every package first (to /var/cache/zypp/packages) and only then installs them:
  the whole download must fit at once, on the filesystem of /var/cache/zypp.
- Installing writes the new files and removes the old ones: the installed system grows by the "space usage
  difference" of the simulation (it can be negative).
- With snapper (Btrfs), a snapshot of / is taken before the change and keeps every old file: nothing that the
  upgrade replaces or removes is freed, so the new files take their whole size. The simulation does not say
  it, so it is worked out from the installed size of what is replaced (rpm).
- When it does not fit, heaps.py splits the update into parts that do, each one a normal transaction.

What can be freed:
- the packages kept in /var/cache/zypp/packages ('zypper clean' removes only these; '-m', '-M' and '-a' would
  also throw away the repository metadata);
- old kernels ('zypper purge-kernels', as zypp.conf multiversion.kernels says). With snapper the old kernels
  stay in the older snapshots, so removing them frees nothing until those snapshots are deleted: they are not
  offered then.
"""

import functools
import os
import re
import subprocess

from . import snapper, zyppconf

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

ROOT = "/"
ZYPP_CACHE = "/var/cache/zypp"
PKG_CACHE = "/var/cache/zypp/packages"
SNAPPER_SYSCONFIG = "/etc/sysconfig/snapper"
SNAPPER_ROOT_CONFIG = "/etc/snapper/configs/root"
ZYPP_COMMIT_PLUGINS = "/usr/lib/zypp/plugins/commit"

# Below this, the package cache is not worth a 'zypper clean'.
CACHE_WORTH = 20 * MiB
# The routine has no simulation: below this much free space before it, it asks. A big Tumbleweed update (a
# rebuild of everything) downloads 3 to 5 GiB; with snapshots the old files also stay.
ROUTINE_MIN_FREE = 4 * GiB
ROUTINE_MIN_FREE_SNAPSHOTS = 6 * GiB

# the groups of a simulation whose old packages stop being used (their files stay in a snapshot)
_REPLACED = ("to-upgrade", "to-downgrade", "to-reinstall", "to-change-vendor", "to-change-arch", "to-remove")
# the groups whose new packages are downloaded
_DOWNLOADED = ("to-install", "to-upgrade", "to-downgrade", "to-reinstall", "to-change-vendor", "to-change-arch")


def margin(total):
    """The room to keep free on a filesystem of this size: 5 %, at least 1 GiB and at most 3 GiB."""
    return int(min(3 * GiB, max(1 * GiB, total * 0.05)))


# ------------------------------------------------------------------ filesystems
def _mounts(text=None):
    """[(source, mount point, type)] from /proc/self/mounts (or a text with its format)."""
    if text is None:
        try:
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return []
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 3:
            out.append((f[0], f[1].replace("\\040", " "), f[2]))
    return out


def mount_of(path, mounts=None):
    """(source, mount point, type) of the filesystem that holds path: the longest mount point above it."""
    mounts = _mounts() if mounts is None else mounts
    real = os.path.realpath(path)
    best = None
    for src, mp, typ in mounts:
        if real == mp or mp == "/" or real.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) >= len(best[1]):
                best = (src, mp, typ)
    return best or ("", "/", "")


def fs_info(path, mounts=None):
    """dict(path, mount, source, fstype, total, free, used) in bytes, or None. free is what a normal user may
    use (statvfs f_bavail); used is what 'df' shows as used."""
    p = path
    while p and not os.path.exists(p):  # /var/cache/zypp may not exist yet: its parent's filesystem
        p = os.path.dirname(p.rstrip("/")) or "/"
    try:
        st = os.statvfs(p or "/")
    except OSError:
        return None
    src, mp, typ = mount_of(p, mounts)
    size = st.f_frsize or st.f_bsize
    return dict(path=path, mount=mp, source=src, fstype=typ, total=st.f_blocks * size, free=st.f_bavail * size,
                used=(st.f_blocks - st.f_bfree) * size)


def same_fs(a, b):
    """True when both paths take their space from the same place. Btrfs subvolumes (/ and /var on openSUSE) are
    mounted from the same device and share it, even though stat() gives each its own device number."""
    if not a or not b:
        return True
    if a["source"] and a["source"] == b["source"] and a["fstype"] == b["fstype"]:
        return True
    return a["total"] == b["total"] and abs(a["free"] - b["free"]) < 64 * MiB and a["fstype"] == b["fstype"]


def snapshots_in_use(root_fs=None):
    """True when snapper takes a snapshot of / before zypper changes packages: / is Btrfs, snapper is
    installed and has a configuration for /, and its zypp plugin is there."""
    if not snapper.snapper_installed():
        return False
    fs = root_fs or fs_info(ROOT)
    if not fs or fs["fstype"] != "btrfs":
        return False
    configured = os.path.exists(SNAPPER_ROOT_CONFIG)
    if not configured:
        try:
            with open(SNAPPER_SYSCONFIG, encoding="utf-8", errors="replace") as f:
                m = re.search(r'^\s*SNAPPER_CONFIGS\s*=\s*"?([^"\n]*)"?', f.read(), re.M)
            configured = bool(m) and "root" in m.group(1).split()
        except OSError:
            configured = False
    try:
        plugin = any("snapper" in n for n in os.listdir(ZYPP_COMMIT_PLUGINS))
    except OSError:
        plugin = False
    return configured and plugin


# ------------------------------------------------------------------ what can be freed
def package_file(item):
    """The file name zypper gives a package in its cache: name-version-release.arch.rpm (no epoch)."""
    edition = (item.get("edition") or "").split(":", 1)[-1]
    return f"{item.get('name', '')}-{edition}.{item.get('arch', '')}.rpm"


def planned_files(result):
    """The cache file names of the packages a simulation would download."""
    if not result:
        return set()
    return {package_file(it) for key in _DOWNLOADED for it in result.get("groups", {}).get(key, [])
            if it.get("kind", "package") == "package"}


def package_cache(planned=(), root=PKG_CACHE):
    """dict(bytes, files, planned_bytes, readable): what /var/cache/zypp/packages holds; planned_bytes is the
    part that the planned upgrade would use (cleaning throws it away too, and it is downloaded again)."""
    out = dict(bytes=0, files=0, planned_bytes=0, readable=True)
    planned = set(planned)

    def err(_e):
        out["readable"] = False
    for folder, _dirs, files in os.walk(root, onerror=err):
        for name in files:
            try:
                st = os.lstat(os.path.join(folder, name))
            except OSError:
                out["readable"] = False
                continue
            out["bytes"] += st.st_size
            out["files"] += 1
            if name in planned:
                out["planned_bytes"] += st.st_size
    return out


def _rpm(args, timeout=60):
    try:
        p = subprocess.run(["rpm", *args], capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL="C"), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout


def installed_sizes(names, chunk=400):
    """{name: installed size in bytes} for the installed packages among names (rpm as the user)."""
    names = sorted({n for n in names if n and not n.startswith("-")})
    out = {}
    for i in range(0, len(names), chunk):
        for line in _rpm(["-q", "--qf", "%{NAME}\t%{SIZE}\n", "--", *names[i:i + chunk]], timeout=120).splitlines():
            f = line.split("\t")
            if len(f) == 2 and f[1].isdigit():
                out[f[0]] = out.get(f[0], 0) + int(f[1])
    return out


def _vr_base(version, release):
    """'7.3.5', '1.1' → '7.3.5-1': how the kernel's version appears in 'uname -r' (7.3.5-1-default)."""
    rel = release.rsplit(".", 1)[0] if "." in release else release
    return f"{version}-{rel}"


def old_kernels(spec=None, running=None, lines=None):
    """dict(count, bytes, items): the kernel packages 'zypper purge-kernels' would remove, worked out as
    libzypp does from zypp.conf multiversion.kernels (latest, latest-N, oldest, oldest+N, running, a version),
    for each package name. count only counts the kernels themselves (not -devel, -source…). An estimate: zypper
    decides. lines: rpm's output, for tests."""
    if spec is None:
        try:
            spec = zyppconf.read_effective()["multiversion.kernels"]["value"]
        except Exception:
            spec = ""
    empty = dict(count=0, bytes=0, items=[])
    if not spec.strip():
        return empty  # libzypp's own default: keep every kernel
    if running is None:
        running = os.uname().release
    running_base = running.rsplit("-", 1)[0] if "-" in running else running
    if lines is None:
        lines = _rpm(["-q", "--whatprovides", "multiversion(kernel)", "--qf",
                      "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{SIZE}\n"]).splitlines()
    by_name = {}
    for line in lines:
        f = line.split("\t")
        if len(f) != 4 or not f[0].startswith("kernel-") or not f[3].isdigit():
            continue
        by_name.setdefault(f[0], []).append((f[1], f[2], int(f[3])))
    items, total, count = [], 0, 0
    for name, pkgs in sorted(by_name.items()):
        pkgs.sort(key=functools.cmp_to_key(lambda a, b: snapper.evr_compare(f"{a[0]}-{a[1]}", f"{b[0]}-{b[1]}")))
        keep = set()
        for token in (t.strip() for t in spec.split(",")):
            m = re.fullmatch(r"(latest|oldest)(?:([-+])(\d+))?", token)
            if m:
                n = int(m.group(3) or 0)
                idx = (len(pkgs) - 1 - n) if m.group(1) == "latest" else n
                if 0 <= idx < len(pkgs):
                    keep.add(idx)
            elif token == "running":
                keep.update(i for i, p in enumerate(pkgs) if _vr_base(p[0], p[1]) == running_base)
            elif token:
                keep.update(i for i, p in enumerate(pkgs) if token in (p[0], f"{p[0]}-{p[1]}"))
        for i, (ver, rel, size) in enumerate(pkgs):
            if i in keep:
                continue
            items.append(f"{name}-{ver}-{rel}")
            total += size
            if not re.search(r"-(devel|source|syms|macros|docs)(-|$)", name):
                count += 1
    return dict(count=count, bytes=total, items=items)


def freeable(planned=(), snapshots=False):
    """dict(cache, kernels, bytes, steps): what a cleaning could free now. steps: 'clean' and/or 'purge-kernels'
    (the zypper subcommands worth running). With snapshots, old kernels are not counted (see above)."""
    cache = package_cache(planned)
    kernels = dict(count=0, bytes=0, items=[]) if snapshots else old_kernels()
    steps = []
    if cache["bytes"] >= CACHE_WORTH:
        steps.append("clean")
    if kernels["items"]:
        steps.append("purge-kernels")
    total = (cache["bytes"] if "clean" in steps else 0) + kernels["bytes"]
    return dict(cache=cache, kernels=kernels, bytes=total, steps=steps)


# ------------------------------------------------------------------ an upgrade
def as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def assess(result, snapshots=None, root=None, cache=None, sizes=None, free_info=None):
    """How an upgrade (a parsed simulation, simulation.parse_dry_run) fits on the disk.

    Returns dict(root, cache, same, snapshots, download, diff, growth, margin, need_all, fits, free (see
    freeable()), fits_after_free, short). Sizes in bytes.
    - growth: what the installed system grows (with snapshots, the whole size of the new packages);
    - need_all: the room the usual way needs (everything downloaded first) on /, when the cache is on the same
      filesystem (else the download needs its own filesystem, and it counts there);
    - fits: with the margin kept free (each part of an update in parts keeps the same margin); short: how
      much is missing.
    root, cache, sizes, free_info: for tests (fs_info dicts, {name: installed size}, a freeable() dict)."""
    root = root or fs_info(ROOT)
    if not root:
        raise OSError("cannot read the free space of /")
    cache = cache or fs_info(ZYPP_CACHE) or root
    same = same_fs(root, cache)
    if snapshots is None:
        snapshots = snapshots_in_use(root)
    groups = (result or {}).get("groups", {})
    download = max(0, as_int((result or {}).get("download")))
    diff = as_int((result or {}).get("disk"))
    if snapshots:
        names = [it["name"] for key in _REPLACED for it in groups.get(key, [])
                 if it.get("kind", "package") == "package"]
        if sizes is None:
            sizes = installed_sizes(names)
        old = sum(sizes.get(n, 0) for n in set(names))
        growth = max(0, old + diff)
    else:
        growth = max(0, diff)
    m = margin(root["total"])
    if same:
        need_all = download + growth
        fits = root["free"] >= need_all + m
        short = max(0, need_all + m - root["free"])
    else:
        mc = margin(cache["total"])
        need_all = growth
        fits = root["free"] >= growth + m and cache["free"] >= download + mc
        short = max(0, growth + m - root["free"], download + mc - cache["free"])
    if free_info is None:
        free_info = freeable(planned_files(result), snapshots) if not fits else dict(
            cache=dict(bytes=0, files=0, planned_bytes=0, readable=True), kernels=dict(count=0, bytes=0, items=[]),
            bytes=0, steps=[])
    # after the cleaning: the cache is on the download's filesystem, the kernels on /
    gain_cache = free_info["cache"]["bytes"] if "clean" in free_info["steps"] else 0
    gain_root = free_info["kernels"]["bytes"]
    redownload = free_info["cache"]["planned_bytes"] if gain_cache else 0
    if same:
        fits_after = root["free"] + gain_cache + gain_root >= need_all + redownload + m
    else:
        fits_after = (root["free"] + gain_root >= growth + m
                      and cache["free"] + gain_cache >= download + redownload + margin(cache["total"]))
    return dict(root=root, cache=cache, same=same, snapshots=snapshots, download=download, diff=diff,
                growth=growth, margin=m, need_all=need_all, fits=fits,
                free=free_info, fits_after_free=fits_after, short=short)


# ------------------------------------------------------------------ the routine (no simulation)
def routine_threshold(snapshots):
    return ROUTINE_MIN_FREE_SNAPSHOTS if snapshots else ROUTINE_MIN_FREE


def routine_low(root=None, cache=None, snapshots=None):
    """Quick (statvfs only, no rpm): (low, free, threshold). low when the free space where the routine downloads
    and installs is below routine_threshold(): clearly too little for a big update downloaded at once."""
    root = root or fs_info(ROOT)
    cache = cache or fs_info(ZYPP_CACHE) or root
    if not root:
        return False, None, None
    if snapshots is None:
        snapshots = snapshots_in_use(root)
    free = min(root["free"], cache["free"]) if cache else root["free"]
    limit = routine_threshold(snapshots)
    return free < limit, free, limit


def routine_space():
    """Everything the routine's question shows: dict(root, cache, same, snapshots, free_now, threshold, low,
    free (see freeable()))."""
    root = fs_info(ROOT)
    if not root:
        raise OSError("cannot read the free space of /")
    cache = fs_info(ZYPP_CACHE) or root
    snaps = snapshots_in_use(root)
    low, free_now, limit = routine_low(root, cache, snaps)
    return dict(root=root, cache=cache, same=same_fs(root, cache), snapshots=snaps, free_now=free_now,
                threshold=limit, low=low, free=freeable((), snaps))
