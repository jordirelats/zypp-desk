# SPDX-License-Identifier: GPL-3.0-or-later
"""Every zypper command with its options and arguments, for the Expert page (no Qt here).

What a command is and which options it has comes from zypper itself: 'LC_ALL=C zypper help' (the list of commands,
their aliases and the global options) and 'LC_ALL=C zypper help <command>' (its usage, description and options).
It is read once per zypper version and kept in ~/.cache/zypp-desk/zypper-help-<version>.json.

What is Zypp Desk's own is the policy on top of it:
- the group each command goes in on the page (GROUPS), and the ones shown before "All commands" (COMMON);
- the commands that only read (RUN_AS_USER): they run as the user, without the administrator password;
- the commands that change the system: they go through the root helper (helper/zypp-desk-helper), so only those
  it allows (HELPER_SUBCOMMANDS) can run from here, and only with the global options it allows;
- the options that are never accepted (FORBIDDEN): another root, configuration, cache or repository folder, extra
  repositories, keys trusted on their own. The helper refuses them too; here they are refused with a clear message
  before anything runs.
The sets that mirror the helper must be kept equal to it (a test compares them).
"""

import json
import os
import re
import shlex
import subprocess
import tempfile

from .i18n import _

ZYPPER = "/usr/bin/zypper"
FORMAT = 1  # of the cached JSON: a new parser writes a new number, and the old cache is read again from zypper

# ------------------------------------------------------------------ the helper's rules (helper/zypp-desk-helper)
HELPER_SUBCOMMANDS = {
    "refresh", "ref", "dist-upgrade", "dup", "update", "up", "patch", "list-patches", "lp",
    "list-updates", "lu", "patch-check", "pchk", "install", "in", "remove", "rm", "verify", "ve",
    "install-new-recommends", "inr", "source-install", "si", "removeptf", "rmptf",
    "addrepo", "ar", "removerepo", "rr", "renamerepo", "nr", "modifyrepo", "mr", "clean", "cc",
    "addservice", "as", "modifyservice", "ms", "removeservice", "rs", "refresh-services", "refs",
    "addlock", "al", "removelock", "rl", "cleanlocks", "cl", "addlocale", "aloc", "removelocale", "rloc",
    "purge-kernels", "ps", "needs-rebooting", "download", "source-download", "licenses",
}
HELPER_GLOBAL_FLAGS = {
    "-v", "--verbose", "-q", "--quiet", "-n", "--non-interactive", "-x", "--xmlout", "-t", "--terse",
    "-A", "--no-abbrev", "--color", "--no-color", "--no-refresh", "-i", "--ignore-unknown", "--no-cd",
    "--no-remote", "--non-interactive-include-reboot-patches",
}
HELPER_GLOBAL_WITH_VALUE = {"--userdata"}
SHORT_COMBINABLE = set("vqnxtAi")
FORBIDDEN = {
    "-R", "--root", "--installroot", "-c", "--config", "-D", "--reposd-dir", "--servicesd-dir",
    "-C", "--cache-dir", "--raw-cache-dir", "--solv-cache-dir", "--pkg-cache-dir",
    "--disable-system-resolvables", "--plus-repo", "--plus-content", "--gpg-auto-import-keys", "--releasever",
}
# Short global options that are forbidden before the command (after it, the same letters mean other things:
# 'install -D' is --dry-run, 'addrepo -c' is --check). -p is --plus-repo.
FORBIDDEN_SHORT_GLOBAL = {"-R", "-c", "-D", "-C", "-p"}
# Global options that only print something about zypper itself: not offered in the form, accepted on the line.
META_GLOBALS = {"-h", "--help", "-V", "--version", "--promptids"}

# ------------------------------------------------------------------ Zypp Desk's policy
# The commands that only read (or, for download, only write into the user's own cache, ~/.cache/zypp/packages):
# they run as the user, without the administrator password. Canonical names; their aliases follow them.
RUN_AS_USER = {
    "help", "repos", "services", "search", "info", "patch-info", "pattern-info", "product-info", "patches",
    "packages", "patterns", "products", "what-provides", "list-updates", "list-patches", "patch-check", "locks",
    "locales", "versioncmp", "targetos", "licenses", "needs-rebooting", "ps", "system-architecture", "subcommand",
    "lifecycle", "download",
}
# Commands that wait for someone to type: nobody can type into a command here.
INTERACTIVE = {"shell"}

# (key, commands in the order they are listed). Commands zypper adds later go to "other".
GROUPS = [
    ("packages", ["install", "remove", "verify", "source-install", "install-new-recommends", "removeptf", "download",
                  "source-download"]),
    ("updates", ["refresh", "list-updates", "update", "dist-upgrade", "patch", "list-patches", "patch-check"]),
    ("search", ["search", "info", "what-provides", "packages", "patterns", "products", "patches", "patch-info",
                "pattern-info", "product-info"]),
    ("repositories", ["repos", "addrepo", "removerepo", "renamerepo", "modifyrepo"]),
    ("services", ["services", "addservice", "modifyservice", "removeservice", "refresh-services"]),
    ("locks", ["locks", "addlock", "removelock", "cleanlocks", "locales", "addlocale", "removelocale"]),
    ("maintenance", ["clean", "purge-kernels", "ps", "needs-rebooting", "licenses"]),
    ("other", ["versioncmp", "targetos", "system-architecture", "help", "shell", "subcommand"]),
]
# Shown before "All commands" is opened (the mockup seccions-v1-D-expert).
COMMON = {"install", "remove", "verify", "source-install", "refresh", "list-updates", "update", "dist-upgrade",
          "search", "info", "repos", "addrepo", "modifyrepo", "addlock", "addlocale", "clean", "purge-kernels"}

# What a command that changes the system changes (the question says it in words).
EFFECTS = {
    "packages": {"install", "remove", "verify", "source-install", "install-new-recommends", "removeptf", "update",
                 "patch", "dist-upgrade", "purge-kernels"},
    "sources": {"addrepo", "removerepo", "renamerepo", "modifyrepo", "addservice", "modifyservice",
                "removeservice"},
    "refresh": {"refresh", "refresh-services"},
    "cache": {"clean"},
    "locks": {"addlock", "removelock", "cleanlocks"},
    "languages": {"addlocale", "removelocale"},
    "download": {"download", "source-download"},
}

# The queue's kind of change for each command it can carry (txqueue.Action); the page maps the options.
QUEUE_KINDS = {"install": "install", "remove": "remove", "addlock": "lock", "removelock": "unlock",
               "addlocale": "locale-add", "removelocale": "locale-remove", "addrepo": "repo-add",
               "removerepo": "repo-remove", "modifyrepo": "repo-modify"}

# Arguments zypper's usage line does not show.
ARGS_OVERRIDE = {"help": "[COMMAND]", "subcommand": ""}


# ------------------------------------------------------------------ reading zypper's help

def _zypper(args, timeout=30):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_") and k not in ("LANG", "LANGUAGE")}
    env.update(LC_ALL="C", LANG="C")
    p = subprocess.run([ZYPPER, *args], capture_output=True, text=True, errors="replace", timeout=timeout,
                       env=env, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout


def zypper_version():
    """'1.14.102', or '' when zypper cannot be run."""
    try:
        _code, out = _zypper(["--version"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"(\d+(?:\.\d+)+\S*)", out)
    return m.group(1) if m else ""


_OPT_RE = re.compile(r"^(?P<flags>--?[A-Za-z0-9?][\w-]*(?:,\s*--?[A-Za-z0-9?][\w-]*)*)"
                     r"(?:\s+(?P<value><[^>]*>))?(?:\s+(?P<help>\S.*))?$")
_CONT_RE = re.compile(r"^ {12,}(\S.*)$")
_OPT_START_RE = re.compile(r"^ {0,6}-")
_SECTION_RE = re.compile(r"^  ([A-Z][\w /-]*?):?\s*$")
_CMD_RE = re.compile(r"^ {6}([\w?][\w?-]*(?:, [\w?][\w?-]*)*)(?:\s{2,}(\S.*))?$")


def _choices(value, text):
    """The values an option takes, when its help lists them."""
    if not value:
        return []
    m = re.search(r"Available modes: ([\w-]+(?:, [\w-]+)+)", text)
    if m:
        return m.group(1).split(", ")
    quoted = re.findall(r"'([\w-]+)' \(", text)
    if len(quoted) >= 2:
        return quoted
    m = re.search(r"\(((?:[\w-]+, )+[\w-]+)\)", text)
    if m and value in ("<TYPE>",):
        return m.group(1).split(", ")
    return []


def _option_block(lines, section):
    """Options from the lines of one section: [dict(flags, value, help, section, choices)]."""
    out, cur = [], None
    for line in lines:
        if not line.strip():
            cur = None
            continue
        m = _CONT_RE.match(line)
        if m and cur is not None:
            cur["help"] = (cur["help"] + " " + m.group(1)).strip()
            continue
        if _OPT_START_RE.match(line):
            m = _OPT_RE.match(line.strip())
            if m:
                flags = [f.strip() for f in m.group("flags").split(",")]
                flags.sort(key=lambda f: f.startswith("--"))  # -x before --long, as in 'zypper help <command>'
                cur = dict(flags=flags, value=m.group("value") or "", help=m.group("help") or "", section=section)
                out.append(cur)
                continue
        cur = None
    # '--color' then '--no-color  Whether to use colors…': the first one has its help on the second line
    for i, o in enumerate(out[:-1]):
        if not o["help"] and out[i + 1]["section"] == o["section"]:
            o["help"] = out[i + 1]["help"]
    for o in out:
        if not o["value"]:
            o["help"] = re.sub(r"\s*Default: false\.?$", "", o["help"])
        o["choices"] = _choices(o["value"], o["help"])
    return out


def parse_main_help(text):
    """(global options, commands) from 'zypper help'. A command: dict(name, aliases, summary, zypper_group,
    external)."""
    lines = text.splitlines()
    state, section, glob_lines, cmds = None, "", {}, []
    order = []
    for line in lines:
        if line.startswith("Global Options"):
            state, section = "globals", "Global Options"
            order.append(section)
            glob_lines[section] = []
            continue
        if line.startswith("Commands"):
            state, section = "commands", ""
            continue
        if line.startswith("Type 'zypper help"):
            state = None
            continue
        if state == "globals":
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1).strip()
                order.append(section)
                glob_lines[section] = []
            else:
                glob_lines[section].append(line)
        elif state == "commands":
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1).strip()
                continue
            m = _CMD_RE.match(line)
            if m:
                names = m.group(1).split(", ")
                cmds.append(dict(name=names[0], aliases=names[1:], summary=m.group(2) or "", zypper_group=section,
                                 external=section == "Subcommands" and names[0] != "subcommand"))
                continue
            m = _CONT_RE.match(line)
            if m and cmds:
                cmds[-1]["summary"] = (cmds[-1]["summary"] + " " + m.group(1)).strip()
    globs = []
    for sec in order:
        globs += _option_block(glob_lines[sec], sec)
    for c in cmds:
        if c["summary"].startswith("<No manual entry"):
            c["summary"] = ""
    return globs, cmds


def parse_command_help(text, name=""):
    """dict(usage, args, description, args_help, examples, options) from 'zypper help <command>'."""
    lines = text.splitlines()
    i = 0
    usage = []
    while i < len(lines) and lines[i].strip():
        usage.append(lines[i].strip())
        i += 1
    args = ""
    if usage:
        m = re.match(r"^(\S+)(?:\s+\(([^)]*)\))?\s*(.*)$", usage[0])
        if m and (not name or m.group(1) == name):
            args = re.sub(r"\[OPTIONS\]|<OPTIONS>|\[--command-options\]", "", m.group(3), flags=re.I)
            args = re.sub(r"\s+", " ", args).strip()
    paragraphs, para = [], []
    sections, order, section = {}, [], None
    for line in lines[i:]:
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            order.append(section)
            sections[section] = []
            continue
        if section is None:
            if line.strip():
                para.append(line.strip())
            elif para:
                paragraphs.append(" ".join(para))
                para = []
        else:
            sections[section].append(line)
    if para:
        paragraphs.append(" ".join(para))
    options, args_help, examples = [], "", ""
    for sec in order:
        if sec.lower().startswith("argument"):
            args_help = _text_of(sections[sec])
        elif sec.lower().startswith("example"):
            examples = "\n".join(x.rstrip() for x in sections[sec]).strip()
        else:
            options += _option_block(sections[sec], sec)
    if name in ARGS_OVERRIDE:
        args = ARGS_OVERRIDE[name]
    return dict(usage=usage, args=args, description="\n\n".join(paragraphs), args_help=args_help,
                examples=examples, options=options)


def _text_of(lines):
    paras, cur = [], []
    for line in lines:
        if line.strip():
            cur.append(line.strip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras)


def read_help(version=None):
    """Everything zypper's help says, as the dict that is cached (zypper runs about 55 times, ~1 s)."""
    code, main = _zypper(["help"])
    if code != 0 or "Commands:" not in main:
        raise RuntimeError(_("zypper's help could not be read (exit code {code}).").format(code=code))
    globs, cmds = parse_main_help(main)
    for c in cmds:
        if c["external"]:  # a program of its own: its help is a manual page, not zypper's format
            c.update(usage=[], args="[ARGUMENTS] ...", description="", args_help="", examples="", options=[],
                     help_ok=False)
            continue
        try:
            code, out = _zypper(["help", c["name"]])
        except (OSError, subprocess.SubprocessError):
            code, out = -1, ""
        c.update(parse_command_help(out, c["name"]) if code == 0 else
                 dict(usage=[], args=ARGS_OVERRIDE.get(c["name"], ""), description="", args_help="", examples="",
                      options=[]))
        c["help_ok"] = code == 0
    return dict(format=FORMAT, zypper=version or zypper_version(), globals=globs, commands=cmds)


def cache_path(version):
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    safe = re.sub(r"[^\w.+-]", "_", version or "unknown")
    return os.path.join(base, "zypp-desk", f"zypper-help-{safe}.json")


def load(refresh=False):
    """The Catalog of zypper's commands: from the cache when it was made for this zypper, else from zypper's help
    (and then cached). Raises RuntimeError when zypper cannot be read."""
    version = zypper_version()
    if not version:
        raise RuntimeError(_("zypper could not be run."))
    path = cache_path(version)
    if not refresh:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("format") == FORMAT and data.get("zypper") == version and data.get("commands"):
                return Catalog(data)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    data = read_help(version)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".zypper-help-", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass  # only a cache: next time zypper's help is read again
    return Catalog(data)


# ------------------------------------------------------------------ the model

class Option:
    def __init__(self, d):
        self.flags = list(d["flags"])
        self.value = d.get("value") or ""
        self.help = d.get("help") or ""
        self.section = d.get("section") or ""
        self.choices = list(d.get("choices") or [])
        self.short = next((f for f in self.flags if not f.startswith("--")), "")
        self.long = next((f for f in self.flags if f.startswith("--")), "")

    @property
    def takes_value(self):
        # --releasever takes one, though zypper's help shows none (it is forbidden anyway)
        return bool(self.value) or self.long == "--releasever"

    @property
    def cli(self):
        """The form used when Zypp Desk writes the command: the long one, which says what it does."""
        return self.long or self.short

    @property
    def label(self):
        return ", ".join(self.flags)

    @property
    def forbidden(self):
        """For a global option: never accepted (its long name is in FORBIDDEN). Not for a command's own options:
        their short letters mean other things ('addrepo -c' is --check)."""
        return (self.long or self.short) in FORBIDDEN

    @property
    def meta(self):
        return any(f in META_GLOBALS for f in self.flags)

    @property
    def helper_allows(self):
        """For a global option: whether the root helper accepts it."""
        return any(f in HELPER_GLOBAL_FLAGS or f in HELPER_GLOBAL_WITH_VALUE for f in self.flags)

    def __repr__(self):
        return f"Option({self.label}{' ' + self.value if self.value else ''})"


class Command:
    def __init__(self, d):
        self.name = d["name"]
        self.aliases = list(d.get("aliases") or [])
        self.summary = d.get("summary") or ""
        self.zypper_group = d.get("zypper_group") or ""
        self.external = bool(d.get("external"))
        self.usage = list(d.get("usage") or [])
        self.args = d.get("args") or ""
        self.description = d.get("description") or ""
        self.args_help = d.get("args_help") or ""
        self.examples = d.get("examples") or ""
        self.help_ok = d.get("help_ok", True)
        self.options = [Option(o) for o in d.get("options") or []]
        self.group = next((g for g, names in GROUPS if self.name in names), "other")
        self.common = self.name in COMMON
        if self.name in INTERACTIVE:
            self.mode, self.reason = "none", "interactive"
        elif self.name in RUN_AS_USER:
            self.mode, self.reason = "user", ""
        elif self.name in HELPER_SUBCOMMANDS:
            self.mode, self.reason = "root", ""
        else:
            self.mode, self.reason = "none", "helper"
        self.effect = next((e for e, names in EFFECTS.items() if self.name in names), "")
        self.queue_kind = QUEUE_KINDS.get(self.name, "")

    @property
    def names(self):
        return [self.name, *self.aliases]

    @property
    def title(self):
        """'install (in)'."""
        return f"{self.name} ({', '.join(self.aliases)})" if self.aliases else self.name

    @property
    def has_dry_run(self):
        return self.option("--dry-run") is not None

    @property
    def args_required(self):
        """zypper's usage names an argument that is not in [brackets]."""
        return bool(re.search(r"(^|\s)<", re.sub(r"\[[^\]]*\]", "", self.args)))

    @property
    def args_kind(self):
        """What the arguments are, to name the field: packages, search, locks, unlock, languages, address,
        rename, repositories, services, versions, command, patches, patterns, products or arguments."""
        a = self.args.upper()
        if not a:
            return ""
        if "QUERYSTRING" in a:
            return "search"
        if "LOCKSPEC" in a:
            return "locks"
        if "LOCK-NUMBER" in a:
            return "unlock"
        if "LOCALE" in a:
            return "languages"
        if "<URI> <ALIAS>" in a:
            return "address"
        if "NEW-ALIAS" in a:
            return "rename"
        if "VERSION1" in a:
            return "versions"
        if "COMMAND" in a:
            return "command"
        if "PATCHNAME" in a:
            return "patches"
        if "PATTERN" in a:
            return "patterns"
        if "PRODUCT" in a:
            return "products"
        if "SERVICE" in a or (self.name in ("removeservice", "modifyservice")):
            return "services"
        if "ALIAS" in a or "REPO" in a:
            return "repositories"
        if any(w in a for w in ("CAPABILITY", "PACKAGE", "NAME", "PTF")):
            return "packages"
        return "arguments"

    def option(self, flag):
        return next((o for o in self.options if flag in o.flags), None)

    def repo_option(self):
        """The option the "From the repository" choice sets: --from (packages from that repository) when the
        command has it, else -r/--repo when it takes a repository."""
        o = self.option("--from")
        if o is not None:
            return o
        o = self.option("--repo")
        if o is not None and "ALIAS" in o.value.upper():
            return o
        return None

    def __repr__(self):
        return f"Command({self.title}, {self.mode}, {len(self.options)} options)"


class Catalog:
    def __init__(self, data):
        self.version = data.get("zypper") or ""
        self.globals = [Option(o) for o in data.get("globals") or []]
        self.commands = [Command(c) for c in data.get("commands") or []]
        self._by = {}
        for c in self.commands:
            for n in c.names:
                self._by.setdefault(n, c)
        self._glob = {}
        for o in self.globals:
            for f in o.flags:
                self._glob.setdefault(f, o)

    def find(self, word):
        return self._by.get(word)

    def global_option(self, flag):
        return self._glob.get(flag)

    def grouped(self):
        """[(group key, [Command])] in the order of GROUPS; commands zypper added since go to "other"."""
        out = []
        known = set()
        for key, names in GROUPS:
            cmds = [self._by[n] for n in names if n in self._by and self._by[n].name == n]
            known.update(c.name for c in cmds)
            out.append((key, cmds))
        rest = [c for c in self.commands if c.name not in known]
        out[-1] = (out[-1][0], out[-1][1] + rest)
        return [(k, cmds) for k, cmds in out if cmds]


# ------------------------------------------------------------------ a command line, checked

class LineError(ValueError):
    """What is wrong with a command line, in words for the person who typed it."""


class Parsed:
    def __init__(self, args, command, root, dry_run, notes, at=None):
        self.args = args          # what zypper gets (after 'zypper'), -n included
        self.command = command    # the Command, or None for 'zypper --version'
        self.root = root          # True: through the root helper, after a question
        self.dry_run = dry_run    # it only simulates (--dry-run): nothing changes
        self.notes = notes        # what was added or left out, in words
        self.at = at              # where the command is in args (after the global options)

    @property
    def rest(self):
        """What comes after the command."""
        return self.args[self.at + 1:] if self.at is not None else []

    def display(self):
        return " ".join(["zypper", *(shlex.quote(a) for a in self.args)])


def _forbidden_error(flag):
    return LineError(_("“{option}” is not accepted here. Options that point zypper at another root, configuration, "
                       "cache or repository folder, or that add repositories or trust keys on their own, are refused, "
                       "as the helper that runs zypper as administrator refuses them.").format(option=flag))


def unquoted_operator(text):
    """The first shell operator (; & | < >) outside quotes, or None. Nothing runs through a shell here, so a line
    with one would not do what it says."""
    quote, escaped = None, False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch in ";&|<>":
            m = re.match(r"[;&|<>]+", text[i:])
            return m.group(0)  # '&&', not '&'
    return None


def _global_token(tokens, i, catalog):
    """The global option(s) at tokens[i]: ([Option], tokens it takes)."""
    tok = tokens[i]
    if tok.startswith("--"):
        name, eq, _val = tok.partition("=")
        if name in FORBIDDEN:
            raise _forbidden_error(name)
        o = catalog.global_option(name)
        if o is None:
            raise LineError(_("zypper has no global option “{option}”.").format(option=name))
        if o.forbidden:
            raise _forbidden_error(name)
        if o.takes_value and not eq:
            if i + 1 >= len(tokens):
                raise LineError(_("“{option}” needs a value after it.").format(option=name))
            return [o], 2
        return [o], 1
    opts = []
    letters = tok[1:]
    for j, ch in enumerate(letters):
        flag = "-" + ch
        if flag in FORBIDDEN_SHORT_GLOBAL or flag in FORBIDDEN:
            raise _forbidden_error(flag)
        o = catalog.global_option(flag)
        if o is None:
            raise LineError(_("zypper has no global option “{option}”.").format(option=flag))
        if o.forbidden:
            raise _forbidden_error(flag)
        opts.append(o)
        if o.takes_value:
            if j == len(letters) - 1:
                if i + 1 >= len(tokens):
                    raise LineError(_("“{option}” needs a value after it.").format(option=flag))
                return opts, 2
            return opts, 1  # '-s3': the rest is the value
    return opts, 1


def _helper_accepts_global(group):
    """The helper's own test for one global option (and its value): see check_zypper_args."""
    a = group[0]
    name = a.split("=", 1)[0]
    if name in HELPER_GLOBAL_WITH_VALUE:
        return True
    return name in HELPER_GLOBAL_FLAGS or (a.startswith("-") and not a.startswith("--") and len(a) > 1
                                           and set(a[1:]) <= SHORT_COMBINABLE)


def split_rest(cmd, rest):
    """What comes after the command: ([(Option, value or True)], [plain arguments]). zypper reads options also
    after the arguments (until '--'); an option this command does not have is left out of both lists."""
    opts, plain = [], []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--":
            plain += rest[i + 1:]
            break
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            o = cmd.option(name) if cmd is not None else None
            if o is not None and o.takes_value and not eq:
                opts.append((o, rest[i + 1] if i + 1 < len(rest) else ""))
                i += 2
                continue
            if o is not None:
                opts.append((o, val if eq else True))
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            letters, skip = t[1:], 1
            for j, ch in enumerate(letters):
                o = cmd.option("-" + ch) if cmd is not None else None
                if o is None:
                    break
                if o.takes_value:
                    if j == len(letters) - 1:
                        opts.append((o, rest[i + 1] if i + 1 < len(rest) else ""))
                        skip = 2
                    else:
                        opts.append((o, letters[j + 1:]))  # '-p90'
                    break
                opts.append((o, True))
            i += skip
            continue
        plain.append(t)
        i += 1
    return opts, plain


def is_dry_run(cmd, rest):
    """True when the options after the command ask for --dry-run (also '-D' or '-yD' where -D is --dry-run for this
    command). Unsure (an abbreviation, an option this command does not have) means False: then the question is
    asked."""
    if cmd is None or not cmd.has_dry_run:
        return False
    return any(o.long == "--dry-run" for o, _v in split_rest(cmd, rest)[0])


def reason_text(cmd):
    """Why a command cannot run from here, in words."""
    if cmd.reason == "interactive":
        return _("“zypper {command}” waits for you to type, and nobody can type into a command here. Write each "
                 "command on the line above instead.").format(command=cmd.name)
    return _("“zypper {command}” is not one of the commands the helper runs as administrator, so it cannot run "
             "from here. Use a terminal for it.").format(command=cmd.name)


def note_n_added():
    return _("-n was added: zypper cannot ask you anything here, so it takes its default answers.")


def analyse(tokens, catalog, *, add_n=True):
    """Check zypper's arguments (without 'zypper') as the helper would, and add -n. Returns a Parsed; raises
    LineError with the reason in words."""
    tokens = list(tokens)
    for t in tokens:
        if "\0" in t or "\n" in t or "\r" in t:
            raise LineError(_("A line break or a null character cannot go into a command."))
    i, groups, opts = 0, [], []
    while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
        found, n = _global_token(tokens, i, catalog)
        groups.append(tokens[i:i + n])
        opts += found
        i += n
    flat = [t for g in groups for t in g]
    if i >= len(tokens):
        if opts and all(o.meta for o in opts):  # 'zypper --version', 'zypper -h'
            return Parsed(flat, None, False, False, [])
        raise LineError(_("Which zypper command? It comes after the global options, for example: zypper -v se vlc"))
    sub, rest = tokens[i], tokens[i + 1:]
    cmd = catalog.find(sub)
    if cmd is None:
        raise LineError(_("zypper has no command “{command}”. The list on the left has them all.").format(
            command=sub))
    for t in rest:  # as the helper: the long forbidden options anywhere on the line
        name = t.split("=", 1)[0]
        if name.startswith("--") and name in FORBIDDEN:
            raise _forbidden_error(name)
    if cmd.mode == "none":
        raise LineError(reason_text(cmd))
    if cmd.external and groups:
        raise LineError(_("“{command}” is a program of its own (a zypper subcommand): global options cannot go "
                          "before it.").format(command=sub))
    if cmd.mode == "root":
        for g in groups:
            if not _helper_accepts_global(g):
                raise LineError(_("The global option “{option}” is not accepted with a command that changes the "
                                  "system: the helper that runs zypper as administrator refuses it.").format(
                    option=g[0].split("=", 1)[0]))
    notes = []
    has_n = any(o.long == "--non-interactive" for o in opts)
    if add_n and not has_n and not cmd.external:
        flat = ["-n", *flat]
        notes.append(note_n_added())
    return Parsed([*flat, sub, *rest], cmd, cmd.mode == "root", is_dry_run(cmd, rest), notes, at=len(flat))


def split_line(text, catalog):
    """A typed line → (zypper's arguments, notes). 'zypper' (and 'sudo') at the start may be left out. Raises
    LineError."""
    text = (text or "").strip()
    if not text:
        raise LineError(_("Type a zypper command, for example: zypper se vlc"))
    if "\0" in text or "\n" in text or "\r" in text:
        raise LineError(_("A line break or a null character cannot go into a command."))
    op = unquoted_operator(text)
    if op:
        raise LineError(_("Only one zypper command at a time: “{op}” is not accepted here (no ;, &&, |, > or <). "
                          "Nothing runs through a shell.").format(op=op))
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise LineError(_("The line cannot be read: {error}.").format(error=str(e).lower())) from None
    notes = []
    if tokens and tokens[0] == "sudo":
        tokens = tokens[1:]
        notes.append(_("sudo is not needed: Zypp Desk asks for the administrator password itself when the command "
                       "changes the system."))
    if tokens and tokens[0] in ("zypper", ZYPPER):
        tokens = tokens[1:]
    elif tokens and not tokens[0].startswith("-") and catalog.find(tokens[0]) is None:
        raise LineError(_("Only zypper runs here: “{word}” is not a zypper command.").format(word=tokens[0]))
    if not tokens:
        raise LineError(_("Type a zypper command, for example: zypper se vlc"))
    # '~/file.repo' as a terminal would read it
    tokens = [os.path.expanduser(t) if t.startswith("~/") else t for t in tokens]
    return tokens, notes


def parse_line(text, catalog):
    """A typed line, checked: a Parsed with -n added. Raises LineError."""
    tokens, notes = split_line(text, catalog)
    parsed = analyse(tokens, catalog)
    parsed.notes = notes + parsed.notes
    return parsed


def compose(cmd, globals_on=(), options_on=(), arguments=(), repo=None, dry_run=False):
    """zypper's arguments for a command filled in on the form. globals_on and options_on: (Option, True or the
    value). The arguments go after '--' (never read as options); -n always goes first. dry_run: add --dry-run
    (Simulate) when the command has it and it is not there yet."""
    if cmd.external:  # a program of its own: no global options, and it may not know '--'
        return [cmd.name, *arguments]
    args = ["-n"]
    for o, v in globals_on:  # short ones, as people type them: zypper -n -v …
        if o.long == "--non-interactive":
            continue
        flag = o.short or o.long
        args += [flag] if v is True else [flag, v]
    args.append(cmd.name)
    dry = cmd.option("--dry-run")
    for o, v in options_on:
        args += [o.cli] if v is True else [o.cli, v]
    if dry_run and dry is not None and not any(o is dry for o, _v in options_on):
        args.append(dry.cli)
    ro = cmd.repo_option()
    if repo and ro is not None:
        args += [ro.cli, repo]
    if arguments:
        args += ["--", *arguments]
    return args
