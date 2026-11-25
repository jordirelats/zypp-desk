# SPDX-License-Identifier: GPL-3.0-or-later
"""What zypper did, and the snapshots snapper took around it. Only parsing and matching: no widgets.

- zypper's history (/var/log/zypp/history, readable only by root) is one line per event:
      date|command|user@host|'zypper' 'dup'|userdata|
      date|install|name|edition|arch|requested-by|repo|checksum|userdata|
      date|remove |name|edition|arch|requested-by|userdata|
      date|radd   |alias|url|
      date|rremove|alias|
  libzypp pads the action names with spaces and escapes a '|' inside a field with a backslash. A
  'command' line opens a transaction; the package and repository lines after it belong to it.
  An update is logged as an 'install' of the new version only: the old version comes from the
  earlier lines of the same file.
- snapper's zypp plugin takes a 'pre' snapshot when libzypp starts changing packages and a 'post'
  one when it ends. 'snapper --jsonout list' gives them with their date; a transaction is matched to
  the pair whose time window contains it.
"""

import calendar
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SNAPPER = "/usr/bin/snapper"
SLACK = 60          # seconds of tolerance between libzypp's lines and snapper's dates
GAP = 3600          # a pause longer than this between two lines starts a new transaction
REPO_ACTIONS = {"radd", "rremove", "ralias", "rurl", "rprio"}
# Packages that are installed side by side (zypp.conf multiversion = provides:multiversion(kernel)):
# a new one is not an update of the old one.
MULTIVERSION_RE = re.compile(r"^kernel-(default|longterm|rt|kvmsmall|vanilla|preempt|64kb|pae|zfcpdump|devel|"
                             r"source|syms|macros|docs)(-.+)?$")

# zypper's subcommands and their aliases, as one name
CANONICAL = {
    "in": "install", "install": "install", "rm": "remove", "remove": "remove",
    "dup": "dup", "dist-upgrade": "dup", "up": "update", "update": "update", "patch": "patch",
    "ar": "addrepo", "addrepo": "addrepo", "rr": "removerepo", "removerepo": "removerepo",
    "mr": "modifyrepo", "modifyrepo": "modifyrepo", "nr": "renamerepo", "renamerepo": "renamerepo",
    "ref": "refresh", "refresh": "refresh", "ve": "verify", "verify": "verify",
    "inr": "inr", "install-new-recommends": "inr", "purge-kernels": "purge-kernels",
    "si": "source-install", "source-install": "source-install",
    "aloc": "addlocale", "addlocale": "addlocale", "rloc": "removelocale", "removelocale": "removelocale",
    "refs": "services", "refresh-services": "services", "as": "services", "addservice": "services",
    "rs": "services", "removeservice": "services", "ms": "services", "modifyservice": "services",
    "removeptf": "removeptf", "rmptf": "removeptf",
}
# global options of zypper that take a value (the subcommand is the first word that is not one of these)
_GLOBAL_WITH_VALUE = {"-c", "--config", "--userdata", "-s", "--table-style", "-p", "--plus-repo", "--plus-content",
                      "--releasever", "-R", "--root", "--installroot", "-D", "--reposd-dir", "-C", "--cache-dir",
                      "--raw-cache-dir", "--solv-cache-dir", "--pkg-cache-dir", "--servicesd-dir"}
# options of install/remove/… that take a value (so the value is not read as a package name)
_SUB_WITH_VALUE = {"-t", "--type", "-r", "--repo", "--from", "--download", "-n", "--name", "-p", "--priority"}
_DRY_RUN_SHORT = {"install", "remove", "dup", "update", "patch", "verify", "inr", "source-install"}


# ------------------------------------------------------------------ data
@dataclass
class Change:
    kind: str           # install, update, downgrade, reinstall, remove
    name: str
    old: str = ""       # edition before (update, downgrade, reinstall, remove)
    new: str = ""       # edition after (install, update, downgrade, reinstall)
    arch: str = ""
    repo: str = ""
    requested: bool = False  # asked for by the user (not pulled in as a dependency)


@dataclass
class RepoChange:
    kind: str           # radd, rremove, ralias, rurl, rprio
    alias: str
    detail: str = ""    # the URL, the new alias, the priority


@dataclass(eq=False)  # two transactions are the same only if they are the same object
class Transaction:
    start: datetime
    end: datetime
    user: str = ""
    cmdline: str = ""
    argv: list = field(default_factory=list)
    userdata: str = ""
    changes: list = field(default_factory=list)
    repos: list = field(default_factory=list)
    other: int = 0      # lines of other kinds (patch status…)
    pre: int = None     # snapper pre/post numbers (set by match_snapshots)
    post: int = None
    _raw: list = field(default_factory=list, repr=False)  # (action, fields) in order, until classify()
    _kinds: set = field(default_factory=set, repr=False)  # 'pkg', 'repo', 'other': the kinds of lines in _raw
    _sub: str = field(default=None, repr=False)

    # ---- what the command line says
    @property
    def has_command(self):
        return bool(self.argv)

    @property
    def program(self):
        return os.path.basename(self.argv[0]) if self.argv else ""

    @property
    def is_zypper(self):
        return self.program == "zypper"

    @property
    def subcommand(self):
        """zypper's subcommand, under one name (dup, install, addrepo…); '' if not zypper."""
        if self._sub is None:
            i = _subcommand_index(self.argv[1:]) if self.is_zypper else None
            self._sub = CANONICAL.get(self.argv[1 + i], self.argv[1 + i]) if i is not None else ""
        return self._sub

    @property
    def global_options(self):
        i = _subcommand_index(self.argv[1:]) if self.is_zypper else None
        return self.argv[1:1 + i] if i is not None else self.argv[1:]

    @property
    def sub_args(self):
        i = _subcommand_index(self.argv[1:]) if self.is_zypper else None
        return self.argv[2 + i:] if i is not None else []

    @property
    def non_interactive(self):
        for a in self.global_options:
            if a == "--non-interactive" or (a.startswith("-") and not a.startswith("--") and "n" in a[1:]):
                return True
        return False

    @property
    def dry_run(self):
        args = self.sub_args
        return "--dry-run" in args or (self.subcommand in _DRY_RUN_SHORT and "-D" in args)

    @property
    def download_only(self):
        args = self.sub_args
        if "--download-only" in args or "--download=only" in args:
            return True
        if self.subcommand in ("install", "update", "dup", "patch") and "-d" in args:
            return True
        return any(a == "--download" and b == "only" for a, b in zip(args, args[1:]))

    @property
    def via(self):
        """Who ran it: 'zypp-desk', 'terminal' (zypper typed by hand), 'yast', 'packagekit', another program's
        name, or '' when there is no command line.

        Zypp Desk is recognised by its --userdata tag (zypp-desk) or, for older lines, by -n before the
        subcommand: every zypper command Zypp Desk runs as root carries it, and people rarely type it."""
        if not self.argv:
            return ""
        if "zypp-desk" in self.userdata.lower():
            return "zypp-desk"
        prog = self.argv[0].lower()
        if self.is_zypper:
            return "zypp-desk" if self.non_interactive else "terminal"
        if "y2" in prog or "yast" in prog:
            return "yast"
        if "packagekit" in prog:
            return "packagekit"
        return self.program

    @property
    def display_command(self):
        """The command line as it is typed (zypper's own log quotes every word)."""
        if not self.argv:
            return ""
        first = "zypper" if self.is_zypper else self.argv[0]
        return " ".join([first, *(shlex.quote(a) for a in self.argv[1:])])

    @property
    def requested_names(self):
        """What the user asked for: the packages logged as requested, or the names on the command line."""
        req = [c.name for c in self.changes if c.requested]
        if req:
            return list(dict.fromkeys(req))
        names, skip, after_dashes = [], False, False
        for a in self.sub_args:
            if skip:
                skip = False
                continue
            if not after_dashes:
                if a == "--":
                    after_dashes = True
                    continue
                if a.split("=", 1)[0] in _SUB_WITH_VALUE and "=" not in a:
                    skip = True
                    continue
                if a.startswith("-"):
                    continue
            base = os.path.basename(a)
            if base.endswith(".rpm"):
                continue  # a local file: its package name is only known from the changes
            names.append(re.split(r"[<>=]", base, 1)[0])
        return [n for n in dict.fromkeys(names) if n]

    def counts(self):
        out = {}
        for c in self.changes:
            out[c.kind] = out.get(c.kind, 0) + 1
        return out

    @property
    def empty(self):
        return not self.changes and not self.repos

    def search_text(self):
        parts = [self.cmdline, self.userdata, *(c.name for c in self.changes), *(r.alias for r in self.repos),
                 *(r.detail for r in self.repos)]
        return " ".join(parts).lower()


@dataclass
class Snapshot:
    number: int
    type: str = "single"      # single, pre, post
    pre_number: int = None
    date_text: str = ""
    when: float = None         # seconds since the epoch, when the date carried a time zone
    naive: datetime = None     # the date without a time zone (snapper's default: local time)
    user: str = ""
    cleanup: str = ""
    description: str = ""
    userdata: dict = field(default_factory=dict)
    default: bool = False
    active: bool = False


@dataclass
class SnapperInfo:
    snapshots: list = field(default_factory=list)
    pairs: list = field(default_factory=list)   # (pre Snapshot, post Snapshot or None)

    def by_number(self, n):
        return next((s for s in self.snapshots if s.number == n), None)


# ------------------------------------------------------------------ history
def split_fields(line, sep="|"):
    """Split a history line on 'sep', keeping a backslash-escaped separator inside its field."""
    if "\\" not in line:
        return line.split(sep)  # almost every line: the fast way
    out, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] in (sep, "\\"):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == sep:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


_DATE_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)$")


def _parse_date(text):
    """'YYYY-MM-DD HH:MM:SS' (local time); None if the line does not start with one."""
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def _split_cmdline(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.replace("'", "").split()


def _subcommand_index(args):
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in _GLOBAL_WITH_VALUE:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return i
    return None


def _new_group(tx, kind, date):
    """Does a line of this kind ('pkg', 'repo', 'other') start a new transaction after 'tx'?"""
    if tx is None:
        return True
    if (date - tx.end).total_seconds() > GAP:
        return True
    if kind == "repo" and "pkg" in tx._kinds:
        return True  # libzypp never changes repositories in the middle of a package transaction
    if kind == "pkg" and ("repo" in tx._kinds or tx.subcommand in ("addrepo", "removerepo", "modifyrepo",
                                                                    "renamerepo", "refresh", "services")):
        return True
    return False


def parse_history(text):
    """The history as a list of transactions, oldest first, with every change classified."""
    txs, cur, dates = [], None, {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = split_fields(line)
        if len(f) < 3:
            continue
        date = dates.get(f[0])
        if date is None:
            date = dates[f[0]] = _parse_date(f[0])
        if date is None:
            continue
        action = f[1].strip()
        if action == "command":
            cur = Transaction(start=date, end=date, user=f[2].strip(),
                              cmdline=f[3] if len(f) > 3 else "", userdata=f[4] if len(f) > 4 else "")
            cur.argv = _split_cmdline(cur.cmdline)
            txs.append(cur)
            continue
        kind = "pkg" if action in ("install", "remove") else "repo" if action in REPO_ACTIONS else "other"
        if _new_group(cur, kind, date):
            cur = Transaction(start=date, end=date)
            txs.append(cur)
        cur._raw.append((action, f))
        cur._kinds.add(kind)
        cur.end = max(cur.end, date)
    classify(txs)
    return txs


def classify(txs):
    """Turn the raw lines into changes: an install of a package that was there before is an update (or a
    downgrade, or a reinstall), and a remove and an install of the same name in one transaction too."""
    installed = {}  # name -> editions installed, oldest first (several only for kernels)
    for tx in txs:
        removed, added, order = {}, {}, []
        for action, f in tx._raw:
            if action == "install" and len(f) > 3:
                name = f[2]
                added.setdefault(name, []).append(dict(
                    edition=f[3], arch=f[4] if len(f) > 4 else "", requested=bool(len(f) > 5 and f[5].strip()),
                    repo=f[6] if len(f) > 6 else ""))
                order.append(name)
            elif action == "remove" and len(f) > 3:
                name = f[2]
                removed.setdefault(name, []).append(dict(
                    edition=f[3], arch=f[4] if len(f) > 4 else "", requested=bool(len(f) > 5 and f[5].strip())))
                order.append(name)
            elif action in REPO_ACTIONS:
                tx.repos.append(RepoChange(action, f[2] if len(f) > 2 else "", f[3] if len(f) > 3 else ""))
            else:
                tx.other += 1
        for name in dict.fromkeys(order):
            before = installed.get(name, [])
            multi = bool(MULTIVERSION_RE.match(name))
            rm = removed.get(name, [])
            for new in added.get(name, []):
                if rm and not multi:
                    old = rm.pop(0)["edition"]
                elif before and not multi:
                    old = before[-1]
                else:
                    old = ""
                if not old:
                    kind = "install"
                else:
                    c = evr_compare(new["edition"], old)
                    kind = "update" if c > 0 else "downgrade" if c < 0 else "reinstall"
                tx.changes.append(Change(kind, name, old, new["edition"], new["arch"], new["repo"],
                                         new["requested"]))
            for old in rm:
                tx.changes.append(Change("remove", name, old["edition"], "", old["arch"], "", old["requested"]))
            # what is installed after this transaction
            now = [e for e in before if e not in {r["edition"] for r in removed.get(name, [])}]
            news = [a["edition"] for a in added.get(name, [])]
            installed[name] = (now + news) if multi else (news or ([] if name in removed else now))
            if not installed[name]:
                del installed[name]
        tx._raw = []
    return txs


# ------------------------------------------------------------------ versions
_TOKEN_RE = re.compile(r"~|\^|[0-9]+|[A-Za-z]+")


def rpmvercmp(a, b):
    """rpm's version comparison: 1 if a is newer, -1 if older, 0 if equal. Versions are cut into runs of
    digits and runs of letters (anything else only separates them); '~' sorts before everything, even the
    end ('1.0~rc1' < '1.0'), and '^' after the end but before anything else ('1.0' < '1.0^git1' < '1.0.1')."""
    if a == b:
        return 0
    ta, tb = _TOKEN_RE.findall(a), _TOKEN_RE.findall(b)
    i = 0
    while True:
        x = ta[i] if i < len(ta) else None
        y = tb[i] if i < len(tb) else None
        if x == "~" or y == "~":
            if x != "~":
                return 1
            if y != "~":
                return -1
        elif x == "^" or y == "^":
            if x is None:
                return -1
            if y is None:
                return 1
            if x != "^":
                return 1
            if y != "^":
                return -1
        elif x is None or y is None:
            if x is None and y is None:
                return 0
            return -1 if x is None else 1
        else:
            xd, yd = x[0].isdigit(), y[0].isdigit()
            if xd != yd:
                return 1 if xd else -1  # a number is newer than letters
            if xd:
                x, y = x.lstrip("0"), y.lstrip("0")
                if len(x) != len(y):
                    return 1 if len(x) > len(y) else -1
            if x != y:
                return 1 if x > y else -1
        i += 1


def _split_evr(edition):
    epoch, vr = "0", edition
    m = re.match(r"^(\d+):(.*)$", edition)
    if m:
        epoch, vr = m.group(1), m.group(2)
    version, _sep, release = vr.rpartition("-")
    if not version:
        version, release = release, ""
    return int(epoch), version, release


def evr_compare(a, b):
    ea, va, ra = _split_evr(a)
    eb, vb, rb = _split_evr(b)
    if ea != eb:
        return 1 if ea > eb else -1
    c = rpmvercmp(va, vb)
    if c or not ra or not rb:
        return c
    return rpmvercmp(ra, rb)


# ------------------------------------------------------------------ snapper
def _first_json(text):
    """The JSON object in snapper's output (anything printed before it, like a warning, is skipped)."""
    dec = json.JSONDecoder()
    tries = 0
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except ValueError:
                tries += 1
                if tries > 50:
                    break
    raise ValueError("no JSON in snapper's output")


def _int(value):
    try:
        return int(value) if value not in (None, "", False) else None
    except (TypeError, ValueError):
        return None


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "true", "1")
    return bool(value)


def _key(row, *names):
    for n in names:
        if n in row:
            return row[n]
    return None


def parse_snapshot_date(text):
    """(epoch or None, naive datetime or None). With a time zone the moment is exact; without one it is
    kept naive and read later as local time or as UTC (see match_snapshots)."""
    text = (text or "").strip()
    if not text:
        return None, None
    t = text.replace("T", " ", 1)
    if t.endswith(("Z", " UTC", " GMT")):
        t = t[:-1] if t.endswith("Z") else t[:-4]
        try:
            dt = datetime.strptime(t.strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.timestamp(), None
        except ValueError:
            return None, None
    m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)?\s*([+-]\d\d):?(\d\d)$", t)
    if m:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        off = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)) * (1 if m.group(2)[0] == "+" else -1))
        return dt.replace(tzinfo=timezone(off)).timestamp(), None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return None, datetime.strptime(t[:19] if fmt.endswith("%S") else t[:16], fmt)
        except ValueError:
            continue
    return None, None


def parse_snapper_list(text):
    """snapper --jsonout -c root list: every snapshot, and the pre/post pairs."""
    data = _first_json(text)
    rows = []
    if isinstance(data, dict):
        rows = data.get("root")
        if not isinstance(rows, list):
            rows = next((v for v in data.values() if isinstance(v, list)), [])
    elif isinstance(data, list):
        rows = data
    info = SnapperInfo()
    for r in rows:
        if not isinstance(r, dict):
            continue
        num = _int(_key(r, "number", "num", "#"))
        if num is None:
            continue
        when, naive = parse_snapshot_date(str(_key(r, "date", "time") or ""))
        ud = _key(r, "userdata", "user-data", "user_data")
        if isinstance(ud, str):  # some versions print "key=value, key=value"
            ud = dict(p.strip().split("=", 1) for p in ud.split(",") if "=" in p)
        info.snapshots.append(Snapshot(
            number=num, type=str(_key(r, "type") or "single").lower(),
            pre_number=_int(_key(r, "pre-number", "pre_number", "pre-num", "prenum", "pre")),
            date_text=str(_key(r, "date", "time") or ""), when=when, naive=naive,
            user=str(_key(r, "user") or ""), cleanup=str(_key(r, "cleanup", "cleanup-algorithm") or ""),
            description=str(_key(r, "description") or ""), userdata=ud if isinstance(ud, dict) else {},
            default=_bool(_key(r, "default", "is-default")), active=_bool(_key(r, "active", "is-active"))))
    posts = {s.pre_number: s for s in info.snapshots if s.type == "post" and s.pre_number is not None}
    for s in sorted(info.snapshots, key=lambda x: x.number):
        if s.type == "pre" and s.number != 0:
            info.pairs.append((s, posts.get(s.number)))
    return info


def boot_state(info):
    """None when the computer started from the default snapshot. Otherwise ('old', active, default): it was
    started from an older snapshot chosen in the boot menu; or ('restart', active, default): 'snapper
    rollback' already made a writable copy of it the default, and a restart is still to come."""
    if not info:
        return None
    active = next((s for s in info.snapshots if s.active), None)
    default = next((s for s in info.snapshots if s.default), None)
    if not active or not default or active.number == default.number:
        return None
    m = re.search(r"writable copy of #(\d+)", default.description)
    if m and int(m.group(1)) == active.number:
        return ("restart", active, default)
    return ("old", active, default)


def _local_epoch(dt):
    return time.mktime(dt.timetuple())


def _utc_epoch(dt):
    return calendar.timegm(dt.timetuple())


def _snap_epoch(s, as_utc):
    if s.when is not None:
        return s.when
    if s.naive is None:
        return None
    return _utc_epoch(s.naive) if as_utc else _local_epoch(s.naive)


def _match(txs, pairs, as_utc):
    """[(tx index, pair index)]: each transaction with package changes gets the pair that contains it."""
    windows = []
    for k, (pre, post) in enumerate(pairs):
        p = _snap_epoch(pre, as_utc)
        if p is None:
            continue
        q = _snap_epoch(post, as_utc) if post else None
        windows.append((k, p, q if q is not None else float("inf")))
    cands = []
    for ti, tx in enumerate(txs):
        if not tx.changes:
            continue  # zypper only takes snapshots when packages change
        a, b = _local_epoch(tx.start), _local_epoch(tx.end)
        for k, p, q in windows:
            # the pre snapshot is taken when the commit starts: close to the command line, before the first
            # package; the post one after the last package
            if p - SLACK <= b and b <= q + SLACK and p >= a - SLACK:
                cands.append((abs(p - a), ti, k))
    cands.sort()
    used_t, used_p, out = set(), set(), []
    for _score, ti, k in cands:
        if ti in used_t or k in used_p:
            continue
        used_t.add(ti)
        used_p.add(k)
        out.append((ti, k))
    return out


def match_snapshots(txs, info):
    """Set tx.pre / tx.post. snapper usually prints local time without a zone; if it printed UTC, matching
    as UTC finds more pairs, and that reading is kept."""
    for tx in txs:
        tx.pre = tx.post = None
    if not info or not info.pairs:
        return 0
    best = max((_match(txs, info.pairs, False), _match(txs, info.pairs, True)), key=len)
    for ti, k in best:
        pre, post = info.pairs[k]
        txs[ti].pre = pre.number
        txs[ti].post = post.number if post else None
    return len(best)


def oldest_pre_epoch(info):
    """When the oldest snapshot still kept was taken (older transactions lost theirs to the cleanup)."""
    if not info:
        return None
    times = [t for t in (_snap_epoch(pre, False) for pre, _post in info.pairs) if t is not None]
    return min(times) if times else None


def tx_epoch(tx):
    return _local_epoch(tx.start)


# ------------------------------------------------------------------ snapper status
STATUS_RE = re.compile(r"^([-+ctpugxa.]{4,8})\s+(\S.*)$")


def parse_status(text):
    """snapper status PRE..POST: [(flags, path)]. flags[0] is + (created), - (deleted), c (content), t (type)
    or '.'; then p (permissions), u (user), g (group), x (extended attributes), a (ACL)."""
    out = []
    for line in text.splitlines():
        m = STATUS_RE.match(line.rstrip("\n"))
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def load(history_text, snapper_text=None):
    """Everything the History page needs, computed in a thread: (transactions newest first, SnapperInfo or
    None, the error reading snapper's list or '')."""
    txs = parse_history(history_text or "")
    info, err = None, ""
    if snapper_text is not None:
        try:
            info = parse_snapper_list(snapper_text)
        except (ValueError, TypeError, AttributeError) as e:
            info, err = None, str(e) or "invalid output"
        if info is not None:
            match_snapshots(txs, info)
    txs.reverse()
    return txs, info, err


def snapper_installed():
    return os.path.exists(SNAPPER)
