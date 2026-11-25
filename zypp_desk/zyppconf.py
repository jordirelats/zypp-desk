# SPDX-License-Identifier: GPL-3.0-or-later
r"""How libzypp reads zypp.conf, and the few settings Zypp Desk lets you change (no widgets here).

libzypp (17.36 and later) merges its configuration as the UAPI.6 Configuration Files Specification says
(/etc/zypp/zypp.conf.README and the FILES section of 'man 5 zypp.conf'):

  vendor files:  /usr/etc/zypp/zypp.conf  and  /usr/etc/zypp/zypp.conf.d/*.conf
  system files:  /etc/zypp/zypp.conf      and  /etc/zypp/zypp.conf.d/*.conf

- A system file replaces the vendor file with the same name, even when it is empty or a link to /dev/null:
  /etc/zypp/zypp.conf replaces /usr/etc/zypp/zypp.conf, and /etc/zypp/zypp.conf.d/x.conf replaces
  /usr/etc/zypp/zypp.conf.d/x.conf.
- The main file is read first, then the drop-ins of both folders together, in lexical order of their file
  names. A later value wins (also inside one file).

Each line is read as libzypp 17.38 reads it (checked with ZYPP_CONF and ZYPP_LOGFILE): the line is trimmed;
'#' or ';' at the start makes it a comment; '[name]' opens a section (the text up to the first ']', exactly,
case included); 'key = value' is split at the first '=', and both sides are trimmed. There are no comments
after a value: 'true # yes' is the value 'true # yes'. A line without '=' (or a key holding one of :/?|,\)
is "garbage": zypper stops with an error, so it is reported as a problem. Only the [main] section counts.

Zypp Desk writes only its own drop-in, /etc/zypp/zypp.conf.d/50-zypp-desk.conf, through the root helper
(verb 'zypp-conf'), and only with the keys and values validate() accepts. openSUSE's file is never touched.
"""

import os
import re
from dataclasses import dataclass

ETC = "/etc/zypp"
USR = "/usr/etc/zypp"
STEM = "zypp.conf"
DROPIN_NAME = "50-zypp-desk.conf"
DROPIN = ETC + "/" + STEM + ".d/" + DROPIN_NAME

_WS = " \t\r\n\v\f"
_BAD_KEY_CHARS = set(":/?|,\\")


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str  # "bool", "choice" or "int" (an int is a choice among its allowed numbers)
    default: str  # libzypp's built-in value ('man 5 zypp.conf')
    choices: tuple = ()  # the values Zypp Desk may write (bool: "true", "false")
    expert: bool = False  # the manual marks it "[Expert Only!]"


SETTINGS = [
    Spec("solver.onlyRequires", "bool", "false", ("true", "false")),
    # built-in: empty, so no kernel is removed; openSUSE's vendor file sets latest,latest-1,running
    Spec("multiversion.kernels", "choice", "",
         ("latest,running", "latest,latest-1,running", "latest,latest-1,latest-2,running", "oldest,latest,running")),
    Spec("download.max_concurrent_connections", "int", "5", tuple(str(n) for n in range(1, 11))),
    Spec("download.max_download_speed", "choice", "0",
         ("0", "1000000", "2000000", "5000000", "10000000", "20000000", "50000000")),
    Spec("solver.dupAllowVendorChange", "bool", "false", ("true", "false"), expert=True),
    Spec("solver.cleandepsOnRemove", "bool", "false", ("true", "false"), expert=True),
]
SPECS = {s.key: s for s in SETTINGS}

# What the root helper accepts for 'zypp-conf key=value' (helper/zypp-desk-helper, ZYPP_CONF). The helper runs
# as root and imports nothing from here, so it keeps its own copy of these rules: keep both the same.
_ASCII_NUM = re.compile(r"[0-9]+")


def validate(key, value):
    """True if Zypp Desk may write 'key = value' (the same rules as the root helper)."""
    if not isinstance(key, str) or not isinstance(value, str) or key not in SPECS:
        return False
    if key == "download.max_concurrent_connections":
        return bool(_ASCII_NUM.fullmatch(value)) and 1 <= int(value) <= 10
    if key == "download.max_download_speed":  # any speed, though the card offers only a few
        return bool(_ASCII_NUM.fullmatch(value)) and int(value) <= 1_000_000_000
    return value in SPECS[key].choices


# ------------------------------------------------------------------ how libzypp understands a value
def _strtoll_nonzero(text):
    """C's strtoll(text, NULL, 10) != 0: leading spaces, a sign, then digits (the rest is ignored)."""
    m = re.match(r"[ \t\n\v\f\r]*[+-]?(\d+)", text)
    return bool(m) and int(m.group(1)) != 0


def to_bool(raw):
    """libzypp's str::strToBool(value, false), used for the solver.* switches (their default is false):
    true only for 1, yes, true, always, on, + (any case) or a number that is not 0."""
    return raw.lower() in ("1", "yes", "true", "always", "on", "+") or _strtoll_nonzero(raw)


def normalize(key, raw):
    """The value as Zypp Desk shows and compares it: "true"/"false" for switches, plain numbers for numbers."""
    spec = SPECS.get(key)
    if spec is None:
        return raw
    if spec.kind == "bool":
        return "true" if to_bool(raw) else "false"
    if spec.kind == "int" or spec.key == "download.max_download_speed":
        if re.fullmatch(r"[+-]?\d+", raw):
            return str(int(raw))
    return raw


# ------------------------------------------------------------------ one file
def parse(text):
    """The [main] section of one file: (dict key -> raw value, problems [(line number, line)]).
    Garbage lines (zypper refuses the whole file) are reported, not raised."""
    main, problems = {}, []
    section = None
    for n, line in enumerate(text.split("\n"), 1):
        t = line.strip(_WS)
        if not t or t[0] in "#;":
            continue
        if t[0] == "[":
            end = t.find("]")
            if end == -1:
                problems.append((n, t))
            else:
                section = t[1:end]
            continue
        eq = t.find("=")
        key = t[:eq].rstrip(_WS) if eq != -1 else ""
        if eq == -1 or _BAD_KEY_CHARS & set(key):
            problems.append((n, t))
            continue
        if section == "main":
            main[key] = t[eq + 1:].lstrip(_WS)
    return main, problems


def _present(path):
    """The name exists (a file, an empty file, a link to /dev/null, even a broken link): it hides the vendor one."""
    return os.path.lexists(path) and not os.path.isdir(path)


def _read(path):
    """(dict, problems) for one file; a file that cannot be read counts as empty and is a problem."""
    try:
        with open(path, "rb") as f:
            text = f.read(1 << 20).decode("utf-8", errors="replace")
    except OSError as exc:
        return {}, [(None, exc.strerror or str(exc))]
    return parse(text)


# ------------------------------------------------------------------ every file, merged
ON_DISK = object()  # 'ours' argument: our drop-in as it is now


class Snapshot:
    """Every configuration file read once; the merge can then be computed with our drop-in as it is,
    missing, or holding other values (what the queue would write) without reading the disk again."""

    def __init__(self, etc=ETC, usr=USR):
        self.etc, self.usr = etc, usr
        self.dropin = os.path.join(etc, STEM + ".d", DROPIN_NAME)
        self.parsed = {}  # path -> (dict, problems)
        self.main = None  # the main file libzypp reads, or None
        for path in (os.path.join(etc, STEM), os.path.join(usr, STEM)):
            if _present(path):
                self.main = path
                break
        self.dropins = {}  # file name -> path (the /etc one replaces the /usr/etc one)
        self.vendor_dropins = {}  # file name -> path in /usr/etc (for "our drop-in is missing")
        for base in (usr, etc):
            folder = os.path.join(base, STEM + ".d")
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                path = os.path.join(folder, name)
                if name.endswith(".conf") and _present(path):
                    self.dropins[name] = path
                    if base == usr:
                        self.vendor_dropins[name] = path
        for path in [self.main, *self.dropins.values()]:
            if path:
                self.parsed[path] = _read(path)
        self.ours_raw = dict(self.parsed[self.dropin][0]) if self.dropins.get(DROPIN_NAME) == self.dropin else {}
        self.ours_exists = self.dropins.get(DROPIN_NAME) == self.dropin

    # ---------------------------------------------------------- the files libzypp would read, in order
    def files(self, ours=ON_DISK):
        """[(path, dict)] in reading order. ours: ON_DISK, None (our drop-in missing) or a dict (our drop-in
        holding exactly these values; an empty dict means it is deleted, as the helper does)."""
        dropins = dict(self.dropins)
        virtual = None
        if ours is not ON_DISK:
            if ours:
                dropins[DROPIN_NAME] = self.dropin
                virtual = dict(ours)
            elif DROPIN_NAME in self.vendor_dropins:
                dropins[DROPIN_NAME] = self.vendor_dropins[DROPIN_NAME]
            else:
                dropins.pop(DROPIN_NAME, None)
        out = []
        if self.main:
            out.append((self.main, self.parsed[self.main][0]))
        for name in sorted(dropins):
            path = dropins[name]
            if virtual is not None and path == self.dropin:
                out.append((path, virtual))
            else:
                out.append((path, self.parsed[path][0]))
        return out

    def problems(self):
        """[(path, line number or None, text)] for the files libzypp reads now: lines it refuses (zypper
        stops) and files that could not be read."""
        out = []
        for path, _values in self.files():
            for n, text in self.parsed[path][1]:
                out.append((path, n, text))
        return out

    def source(self, path):
        if path is None:
            return "default"
        if path == self.dropin:
            return "yours"
        if path.startswith(self.usr.rstrip("/") + "/"):
            return "openSUSE"
        return "system"

    def effective(self, ours=ON_DISK):
        """key -> {"value", "raw", "source", "file", "later"} for every key in SETTINGS.
        value: normalized ("true"/"false" for switches); raw: the text in the file (None for the default);
        source: "default", "openSUSE" (/usr/etc), "yours" (Zypp Desk's drop-in) or "system" (another /etc
        file); later: the file is read after Zypp Desk's drop-in, so Zypp Desk cannot change this value."""
        out = {s.key: {"value": s.default, "raw": None, "source": "default", "file": None, "later": False}
               for s in SETTINGS}
        for path, values in self.files(ours):
            for key, raw in values.items():
                if key in out:
                    out[key] = {"value": normalize(key, raw), "raw": raw, "source": self.source(path),
                                "file": path, "later": self._after_ours(path)}
        return out

    def _after_ours(self, path):
        if path == self.main:
            return False
        name = os.path.basename(path)
        return name > DROPIN_NAME

    # ---------------------------------------------------------- what to write
    def desired(self, wanted):
        """The content of our drop-in that gives the wanted values: only the keys that differ from what
        applies without it (so going back to a default removes the key; {} means delete the drop-in).
        Values set in a file read after ours cannot be changed and are left out."""
        for key, value in wanted.items():
            if key not in SPECS:
                raise ValueError("unknown zypp.conf setting: " + str(key))
        base = self.effective(ours=None)
        out = {k: v for k, v in wanted.items() if v != base[k]["value"] and not base[k]["later"]}
        # A vendor drop-in with our file name is hidden while ours exists: repeat what it set, if wanted.
        for _round in range(3):
            eff = self.effective(ours=out)
            missing = {k: v for k, v in wanted.items() if eff[k]["value"] != v and not eff[k]["later"]}
            if not missing:
                break
            out.update(missing)
        return {s.key: out[s.key] for s in SETTINGS if s.key in out}  # in the order of SETTINGS


# ------------------------------------------------------------------ the API used by the card
def read_effective(etc=ETC, usr=USR):
    """key -> {"value", "source", "file", …}: what libzypp uses now (see Snapshot.effective)."""
    return Snapshot(etc, usr).effective()


def ours(etc=ETC):
    """What Zypp Desk's drop-in sets now: [main] key -> raw value ({} if it does not exist)."""
    path = os.path.join(etc, STEM + ".d", DROPIN_NAME)
    if not _present(path):
        return {}
    return dict(_read(path)[0])


def without_ours(etc=ETC, usr=USR):
    """The effective values if Zypp Desk's drop-in did not exist."""
    return Snapshot(etc, usr).effective(ours=None)


def desired_dropin(wanted, etc=ETC, usr=USR):
    """What to write in Zypp Desk's drop-in to get the wanted values (key -> str); {} = delete it."""
    return Snapshot(etc, usr).desired(wanted)
