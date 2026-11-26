# SPDX-License-Identifier: GPL-3.0-or-later
"""Programs installed with their own installer (a .sh, a .run, a .tgz with install.pl…): find them, unpack and run
an installer, note what it creates, and the ways to take them away again.

Jordi, 26/11/2026: a section to keep an eye on programs that install with a .sh: install from a folder, and
uninstall. The mockups are seccions-v1-C1 (the page) and seccions-v1-C2 (the question before an installer).
Nothing here knows any program or folder of one computer: it works for any program and any home.

No Qt here: plain Python, meant to run in a thread (tasks.run_async). Every folder it looks at comes from a Places,
so that the tests can point it at a scratch folder instead of the real home.

What counts as such a program:
- a .desktop file of the menu (yours, /usr/local's or /usr/share's) that no package owns, is not a Flatpak, Snap,
  AppImage or browser web app, and starts a program that no package owns either, in your folder, /opt or
  /usr/local. The entries of one program go together by the folder it was installed in (~/SomeApp);
- a folder in /opt with nothing of any package in it;
- what Zypp Desk noted when it ran an installer itself ($XDG_DATA_HOME/zypp-desk/installed.json).
Your own menu entries whose program is not there any more are "left behind": they can be cleaned up.

Nothing here deletes anything. Removing means moving to the trash (gio trash), as the user and only inside the
user's own folder; a program installed for the whole computer gets the commands to run in a terminal instead.
Installers and uninstallers run as the user, in a terminal window (konsole), never through pkexec.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .backend import ENV_C
from .i18n import _, user_lang

# web apps made by a browser ("install this site"): they belong to the browser
WEB_APP_RE = re.compile(r"--app-id=|--app=|--profile-directory=")
FLATPAK_RE = re.compile(r"(^|[\s/])flatpak\s+run\b")

# folders of the home that hold many things: never a program's own folder (the program is one level deeper). The
# XDG user folders, whatever their name in the user's language, are added from user-dirs.dirs (Places.user_dirs).
HOME_CONTAINERS = {
    "bin", ".local", ".local/bin", ".local/share", ".local/lib", ".local/opt", ".local/share/applications",
    ".config", ".cache", "opt", "Applications", "Apps", "apps", "AppImages", "Programs", "programs", "Software",
    "software", "Downloads", "Desktop", "Documents", "Music", "Pictures", "Videos",
    "Public", "Templates", "tmp", "src",
}
# the usual parts of one program's folder: a folder whose next step is one of these is the program's own
LAYOUT_DIRS = {"bin", "bin64", "sbin", "lib", "lib32", "lib64", "libexec", "share", "data", "resources", "res",
               "plugins", "jre", "runtime", "app", "program", "launcher", "x86_64", "x64", "amd64", "linux",
               "linux64", "linux-x64", "usr", "opt", "exec", "files", "assets"}
# /usr/local/<these>/<program>
LOCAL_CONTAINERS = {"bin", "sbin", "lib", "lib64", "libexec", "share", "games", "etc", "include", "src", "opt"}

UNINSTALL_RE = re.compile(r"^(uninstall|uninst|un-install)([-_. ].*)?$|uninstall.*\.(sh|pl|run|bin|py)$", re.I)
NOT_PROGRAMS = {".desktop", ".png", ".svg", ".svgz", ".ico", ".xpm", ".txt", ".md", ".html", ".htm", ".log",
                ".xml", ".json", ".ini", ".cfg", ".conf", ".so", ".dll", ".exe", ".dat", ".qm", ".mo", ".lnk",
                ".pdf", ".rtf", ".jar", ".js", ".css"}
INSTALLER_NAMES = ("install.sh", "install.pl", "setup.sh", "installer.sh", "install", "setup", "install.run",
                   "setup.run", "installer", "install.py", "setup.pl")
ARCHIVE_EXTS = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tbz", ".tar", ".zip")
SCRIPT_EXTS = (".sh", ".run", ".bin", ".pl", ".py")
MAX_UNPACKED = 8 << 30  # an installer archive is a few MB; more than this is not an installer
MAX_MEMBERS = 200_000
SNAP_LIMIT = 100_000  # files under ~/.local/share/icons, at most

# The command a terminal window runs: go to the folder, show the command, run it, write its exit code where Zypp
# Desk looks for it, and wait for Enter so that its last words can be read. Every value is a positional argument:
# nothing from a file name is ever read as shell code.
WRAPPER = ('cd -- "$1" || exit 1; st=$2; ok=$3; bad=$4; bye=$5; shift 5; '
           'printf "%s\\n\\n" "\\$ $*"; "$@"; c=$?; printf "%s\\n" "$c" > "$st"; '
           'if [ "$c" -eq 0 ]; then printf "\\n%s\\n" "$ok"; else printf "\\n%s %s\\n" "$bad" "$c"; fi; '
           'printf "%s" "$bye"; read -r _ 2>/dev/null; exit "$c"')


class UnsafeArchive(Exception):
    """An archive that would write outside its folder, or is far too big to be an installer."""


# ------------------------------------------------------------------ places

def _abs_env(var, default):
    v = os.environ.get(var)
    return Path(v) if v and os.path.isabs(v) else default


def user_dir(config_home, home, key, fallback):
    """A folder of user-dirs.dirs ('XDG_DESKTOP_DIR="$HOME/Desktop"', named in the user's language), the file that
    xdg-user-dir reads; home/fallback (the XDG default) when it says nothing."""
    try:
        text = (Path(config_home) / "user-dirs.dirs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Path(home) / fallback
    m = re.search(rf'^{key}="(.*)"\s*$', text, re.M)
    if not m:
        return Path(home) / fallback
    v = m.group(1).replace("$HOME", str(home))
    return Path(v) if os.path.isabs(v) else Path(home) / v


@dataclass
class Places:
    """Every folder this module looks at or writes to."""
    home: Path
    data_home: Path  # ~/.local/share
    cache_home: Path  # ~/.cache
    desktop_dir: Path = None  # the desktop folder of user-dirs.dirs (copies of menu entries on the desktop)
    download_dir: Path = None  # where the file dialog starts
    user_dirs: tuple = ()  # the XDG user folders (Documents, Downloads…, by language): never a program's folder
    system_app_dirs: tuple = (Path("/usr/local/share/applications"), Path("/usr/share/applications"))
    opt_dirs: tuple = (Path("/opt"),)
    local_root: Path = Path("/usr/local")
    manifest: Path = None  # $XDG_DATA_HOME/zypp-desk/installed.json
    extra: dict = field(default_factory=dict)

    @property
    def app_dir(self):
        return self.data_home / "applications"

    @property
    def icons_dir(self):
        return self.data_home / "icons"

    @property
    def mime_dir(self):
        return self.data_home / "mime" / "packages"

    @property
    def installers_dir(self):
        return self.cache_home / "zypp-desk" / "installers"

    def home_containers(self):
        out = set(HOME_CONTAINERS)
        for d in (self.desktop_dir, self.download_dir, *self.user_dirs):
            if d is not None and _under(d, self.home):
                out.add(Path(d).relative_to(self.home).as_posix())
        return out


def places_for(home, data_home=None, cache_home=None, config_home=None, **kw):
    """The places of a home folder (the real one, or a scratch one in the tests)."""
    home = Path(home)
    config_home = Path(config_home) if config_home else home / ".config"
    data_home = Path(data_home) if data_home else home / ".local/share"
    cache_home = Path(cache_home) if cache_home else home / ".cache"
    keys = {"XDG_DESKTOP_DIR": "Desktop", "XDG_DOWNLOAD_DIR": "Downloads", "XDG_DOCUMENTS_DIR": "Documents",
            "XDG_MUSIC_DIR": "Music", "XDG_PICTURES_DIR": "Pictures", "XDG_VIDEOS_DIR": "Videos",
            "XDG_TEMPLATES_DIR": "Templates", "XDG_PUBLICSHARE_DIR": "Public"}
    dirs = {k: user_dir(config_home, home, k, v) for k, v in keys.items()}
    kw.setdefault("desktop_dir", dirs["XDG_DESKTOP_DIR"])
    kw.setdefault("download_dir", dirs["XDG_DOWNLOAD_DIR"])
    kw.setdefault("user_dirs", tuple(d for d in dirs.values() if d != home))
    kw.setdefault("manifest", data_home / "zypp-desk" / "installed.json")
    return Places(home=home, data_home=data_home, cache_home=cache_home, **kw)


def default_places():
    home = Path.home()
    return places_for(home, data_home=_abs_env("XDG_DATA_HOME", home / ".local/share"),
                      cache_home=_abs_env("XDG_CACHE_HOME", home / ".cache"),
                      config_home=_abs_env("XDG_CONFIG_HOME", home / ".config"))


def _under(path, root):
    """path is root or inside it (no symlinks resolved)."""
    p, r = os.path.normpath(str(path)), os.path.normpath(str(root))
    return p == r or p.startswith(r.rstrip("/") + "/")


under = _under


def home_short(path, home=None):
    home = str(home or Path.home())
    p = str(path)
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def shell_path(path, home=None):
    """A path for a command shown to the user: ~/… when it is in the home, quoted for a shell when needed."""
    home = str(home or Path.home())
    p = str(path)
    if p.startswith(home + "/"):
        rel = p[len(home) + 1:]
        return "~/" + (rel if shlex.quote(rel) == rel else shlex.quote(rel))
    return shlex.quote(p)


# ------------------------------------------------------------------ desktop entries

def lang_keys():
    lang = user_lang().split(".")[0].split("@")[0]
    if lang in ("C", "POSIX", ""):
        return []
    return [lang] + ([lang.split("_")[0]] if "_" in lang else [])


def _desktop_group(path, max_bytes=1 << 20):
    """The keys of the [Desktop Entry] group of a .desktop file ({key: value}), or None. A plain reader: the
    configparser took more than a second for the 400 files of a menu (all their Name[xx] lines)."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    out, inside, found = {}, False, False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if inside:
                break
            inside = line == "[Desktop Entry]"
            found = found or inside
            continue
        if inside and "=" in line:
            k, _sep, v = line.partition("=")
            out.setdefault(k.strip(), v.strip())
    return out if found else None


def read_desktop(path, langs=None):
    """The parts of a .desktop file that matter here, or None when it is not an application."""
    langs = lang_keys() if langs is None else langs
    e = _desktop_group(path)
    if e is None:
        return None
    if e.get("Type", "Application") != "Application" or not e.get("Exec"):
        return None
    if e.get("Hidden", "").lower() == "true":  # "deleted": a user's copy that hides a system entry
        return None

    def loc(key):
        for lg in langs:
            v = e.get(f"{key}[{lg}]")
            if v:
                return v
        return e.get(key, "")

    exec_line = e.get("Exec", "")
    name = Path(path).name
    other = (bool(WEB_APP_RE.search(exec_line)) or bool(FLATPAK_RE.search(exec_line)) or "X-Flatpak" in e
             or "/snap/" in exec_line or "X-SnapInstanceName" in e
             or name.lower().startswith("appimagekit") or ".appimage" in exec_line.lower()
             or any(k.startswith("X-AppImage") for k in e))
    return {"file": str(path), "id": name, "name": loc("Name") or name[:-8], "comment": loc("Comment"),
            "generic": loc("GenericName"), "icon": e.get("Icon", "").strip(), "exec": exec_line,
            "path": e.get("Path", "").strip(), "hidden": e.get("NoDisplay", "").lower() == "true",
            "mime": [m for m in e.get("MimeType", "").split(";") if m.strip()],
            "categories": [c for c in e.get("Categories", "").split(";") if c], "other": other}


def exec_program(exec_line, home=None, which=shutil.which):
    """The program a .desktop line starts: {'written', 'real', 'exists'}; None when it cannot be told (a bare
    name not in PATH, sh -c '…')."""
    try:
        words = shlex.split(exec_line)
    except ValueError:
        return None
    while words and (os.path.basename(words[0]) == "env" or ("=" in words[0] and not words[0].startswith("/"))):
        words.pop(0)
    if not words:
        return None
    w = words[0]
    if w.startswith("~/"):
        w = str(home or Path.home()) + w[1:]
    if "/" in w:
        if not w.startswith("/"):
            return None
        prog = w
    else:
        prog = which(w)
        if not prog:
            return None
    exists = os.path.exists(prog)
    return {"written": prog, "real": os.path.realpath(prog) if exists else prog, "exists": exists}


# ------------------------------------------------------------------ rpm

_NOT_OWNED = re.compile(r"^file (.+) is not owned by any package$")
_MISSING = re.compile(r"^error: file (.+): ")


def rpm_owned(paths, timeout=60):
    """The paths that some package owns (rpm -qf as the user, in batches). Raises OSError when rpm cannot be asked:
    then nothing can be told apart, and saying "no package" would be wrong."""
    paths = sorted({str(p) for p in paths if p and "\n" not in str(p)})
    owned = set()
    for i in range(0, len(paths), 400):
        chunk = paths[i:i + 400]
        try:
            p = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\n", "--", *chunk], capture_output=True, text=True,
                               env=ENV_C, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise OSError(f"rpm -qf: {e}") from e
        not_owned = set()
        for line in (p.stdout + "\n" + p.stderr).splitlines():
            m = _NOT_OWNED.match(line) or _MISSING.match(line)
            if m:
                not_owned.add(m.group(1))
            elif line.startswith("error:"):
                raise OSError(line)
        owned |= {x for x in chunk if x not in not_owned}
    return owned


# ------------------------------------------------------------------ where a program lives

def _root_of(path, places):
    """(root, containers, scope) when the path is in the home, /opt or /usr/local; else None."""
    if _under(path, places.home):
        return places.home, places.home_containers(), "home"
    for o in places.opt_dirs:
        if _under(path, o):
            return Path(o), set(), "system"
    if _under(path, places.local_root):
        return places.local_root, LOCAL_CONTAINERS, "system"
    return None


def _top_folder(dir_path, root, containers):
    """The first folder under root that is not a container, on the way to dir_path (a folder)."""
    rel = Path(os.path.normpath(str(dir_path))).relative_to(root).parts
    for i in range(len(rel)):
        if "/".join(rel[: i + 1]) in containers:
            continue
        return Path(root).joinpath(*rel[: i + 1])
    return None


def _subdirs(folder, limit=50):
    try:
        out = []
        with os.scandir(folder) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    out.append(e.name)
                    if len(out) > limit:
                        break
        return out
    except OSError:
        return []


def program_folder(dir_path, places):
    """(folder, scope) of a program whose files are in dir_path: the top folder under ~, /opt or /usr/local
    (~/SomeApp for ~/SomeApp/Some App 2 or ~/SomeApp/bin). A top folder that holds other things (~/Apps, a synced
    folder, /opt/<vendor>) is shared: then the program's folder is further down, on the way to dir_path. It stops at
    the first folder whose next step is a usual part of a program (bin, lib, share, data…) or its only subfolder.
    (None, scope) when the program sits right in a container (~/bin/foo), (None, None) outside those places."""
    found = _root_of(dir_path, places)
    if not found:
        return None, None
    root, containers, scope = found
    top = _top_folder(dir_path, root, containers)
    if top is None:
        return None, scope
    cur, deep = top, Path(os.path.normpath(str(dir_path)))
    while cur != deep and os.path.isdir(cur):
        nxt = deep.relative_to(cur).parts[0]
        if nxt.lower() in LAYOUT_DIRS or len(_subdirs(cur)) <= 1:
            break
        cur = cur / nxt
    return cur, scope


# ------------------------------------------------------------------ uninstallers and how to run a script

def _rank_uninstaller(name):
    n = name.lower()
    order = ("uninstall.sh", "uninstall.pl", "uninstall", "uninstall.run", "uninstall.py")
    return (order.index(n) if n in order else len(order), n)


def find_uninstaller(folder, max_depth=2, max_entries=6000):
    """The program's own uninstaller in its folder (uninstall*, *uninstall*.sh/.pl/.run, Uninstall), the
    shallowest one; None when there is none. Links are not followed."""
    if not folder or not os.path.isdir(folder):
        return None
    queue, seen = [(str(folder), 0)], 0
    while queue:
        d, depth = queue.pop(0)
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        here = []
        for e in entries:
            seen += 1
            if seen > max_entries:
                return None
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth and not e.name.startswith("."):
                        queue.append((e.path, depth + 1))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if os.path.splitext(e.name)[1].lower() in NOT_PROGRAMS:
                continue
            if UNINSTALL_RE.search(e.name):
                here.append(e)
        if here:
            return sorted(here, key=lambda e: _rank_uninstaller(e.name))[0].path
    return None


def command_from_head(name, head, executable):
    """How to run a script or program called name whose first bytes are head, from its own folder: a list of
    arguments, or None when it cannot be run (a binary without the permission to run)."""
    me = "./" + name
    if head.startswith(b"#!"):
        line = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        parts = line.split(None, 1)  # like the kernel: the interpreter and at most one argument
        if parts:
            interp, arg = parts[0], (parts[1] if len(parts) > 1 else None)
            if os.path.basename(interp) == "env" and arg:
                words = arg.split()
                interp, arg = words[0], (" ".join(words[1:]) or None)
            base = os.path.basename(interp)
            if shutil.which(base):
                interp = base  # "perl ./install.pl" rather than "/usr/bin/perl ./install.pl"
            return [interp, *([arg] if arg else []), me]
    if head.startswith(b"\x7fELF"):
        return [me] if executable else None
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pl":
        return ["perl", me]
    if ext == ".py":
        return ["python3", me]
    if ext in (".sh", ".run", ".bin", ""):
        return ["sh", me]
    return None


def command_for(path):
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    return command_from_head(os.path.basename(path), head, os.access(path, os.X_OK))


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


# ------------------------------------------------------------------ finding the programs

def _fold(text):
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


VERSION_RE = re.compile(r"[-_ ]v?(\d+(?:\.\d+)+)$")


def _version(entries, folder):
    for text in [Path(e["id"]).stem for e in entries] + ([Path(folder).name] if folder else []):
        m = VERSION_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _main_entry(entries, folder):
    """The entry that names the program: a visible one whose name is its folder's ('Some Photo Editor' in a folder
    'Some Photo Editor', accents aside), else the first visible one."""
    dirs = {_fold(Path(folder).name)} if folder else set()
    for e in entries:
        if e.get("path"):
            dirs.add(_fold(Path(e["path"]).name))
        if e.get("prog"):
            dirs.add(_fold(Path(e["prog"]).parent.name))

    def score(e):
        n = _fold(e["name"])
        return (e["hidden"], "uninstall" in n, 0 if n in dirs else 1, e["id"])

    return sorted(entries, key=score)[0] if entries else None


def icon_file(name, places):
    """The biggest picture of an icon name in the user's icon folder (Qt may not look there), or None."""
    if not name:
        return None
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    best, best_size = None, -1
    base = places.icons_dir
    candidates = list(base.glob(f"*/*/apps/{name}.*")) + list(base.glob(f"{name}.*"))
    for f in candidates:
        if f.suffix.lower() not in (".png", ".svg", ".svgz", ".xpm"):
            continue
        size = 10_000 if f.suffix.lower().startswith(".svg") else _icon_px(f)
        if size > best_size:
            best, best_size = str(f), size
    return best


def _icon_px(f):
    m = re.match(r"(\d+)x\d+", f.parent.parent.name)
    return int(m.group(1)) if m else 0


def _icon_files(entries, places):
    """The user's icon files named after the entries' icons and the mime types they open."""
    out = []
    base = places.icons_dir
    if not base.is_dir():
        return out
    for e in entries:
        ic = e.get("icon") or ""
        if ic and not os.path.isabs(ic) and "/" not in ic:
            out += [str(f) for f in base.glob(f"*/*/apps/{ic}.*")] + [str(f) for f in base.glob(f"{ic}.*")]
        for m in e.get("mime", []):
            dash = m.strip().replace("/", "-")
            if dash and "*" not in dash:
                out += [str(f) for f in base.glob(f"*/*/mimetypes/{dash}.*")]
    return sorted(set(out))


def _mime_files(entries, places):
    out = []
    for e in entries:
        for m in e.get("mime", []):
            dash = m.strip().replace("/", "-")
            if dash and "*" not in dash and "/" not in dash:
                f = places.mime_dir / f"{dash}.xml"
                if f.is_file():
                    out.append(str(f))
    return sorted(set(out))


def _has_files(folder, limit=20000):
    n = 0
    for _root, _dirs, files in os.walk(folder):
        if files:
            return True
        n += 1
        if n > limit:
            return True
    return False


def _opt_samples(folder, limit=30):
    """A few paths inside a folder of /opt: the folder, its first entries and its first files further down.
    A package owns some of them when the folder is its (/opt/google/chrome…)."""
    out = [str(folder)]
    try:
        with os.scandir(folder) as it:
            out += sorted(e.path for e in it)[:limit]
    except OSError:
        pass
    files = 0
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for n in sorted(names)[:limit]:
            out.append(os.path.join(root, n))
            files += 1
        if files >= limit:
            break
    return out


def _all_desktops(places, langs):
    """Every application entry of the menu: {file: entry}. The user's folder first; a system file with the same
    name is hidden by it, as in the menu."""
    by_name = {}
    for d in (places.app_dir, *places.system_app_dirs):
        try:
            files = sorted(Path(d).glob("*.desktop"))
        except OSError:
            continue
        for f in files:
            by_name.setdefault(f.name, f)
    out = {}
    for f in by_name.values():
        if "\n" in str(f):
            continue
        e = read_desktop(f, langs)
        if e and not e["other"]:
            out[str(f)] = e
    return out


def find_programs(places=None, owned=rpm_owned, which=shutil.which, langs=None):
    """Every program installed with its own installer, and the entries left behind by programs that are gone.

    A list of dicts:
      id, name, version, summary, categories, icon (Icon= name), icon_file (a picture found in the user's icons),
      folder (str or None), scope ('home' | 'system'), state ('ok' | 'missing': the program is not there, only its
      entries are | 'empty': a folder of /opt with only empty folders in it),
      entries (their .desktop dicts: file, name, hidden, prog…), desktop_copies (the same entries on the desktop),
      icons, mime_files (the user's files named after the entries), uninstaller (path or None),
      recorded (what Zypp Desk noted when it ran the installer, or None), extra (other paths it noted).
    """
    places = places or default_places()
    langs = lang_keys() if langs is None else langs
    desktops = _all_desktops(places, langs)
    owned_desktops = owned(list(desktops))
    cands = {}
    for f, e in desktops.items():
        if f in owned_desktops:
            continue
        prog = exec_program(e["exec"], places.home, which)
        if not prog:
            continue
        e["prog"] = prog["real"]
        e["prog_written"] = prog["written"]
        e["exists"] = prog["exists"]
        cands[f] = e
    owned_progs = owned({p for e in cands.values() if e["exists"] for p in (e["prog"], e["prog_written"])})

    groups = {}
    for f, e in cands.items():
        if e["exists"] and (e["prog"] in owned_progs or e["prog_written"] in owned_progs):
            continue
        mine = _under(f, places.app_dir)
        folder, scope = None, None
        dirs = [os.path.dirname(e["prog"]), os.path.dirname(e["prog_written"])]
        if e["path"] and os.path.isabs(e["path"]):
            dirs.append(e["path"])  # Path=, the folder it starts in: only when the program itself says nothing
        for d in dirs:
            fd, sc = program_folder(d, places)
            scope = scope or sc
            if fd:
                folder, scope = fd, sc
                break
        if not e["exists"]:
            if not mine:
                continue  # a system entry of something gone: not ours to clean
            # what is left is your own menu entry: it can go without the administrator password, wherever the
            # program was
            folder = folder or Path(os.path.dirname(e["prog_written"]))
            scope = "home"
            key = ("missing", str(folder))
        else:
            if scope is None:
                continue  # a program elsewhere (/usr/java…): not one of these
            key = ("ok", str(folder) if folder else e["prog"])
        g = groups.setdefault(key, {"folder": str(folder) if folder else None, "scope": scope,
                                     "state": key[0], "entries": []})
        g["entries"].append(e)

    # left behind, but not in the menu either (NoDisplay): nothing anybody sees
    groups = {k: g for k, g in groups.items() if k[0] != "missing" or not all(e["hidden"] for e in g["entries"])}
    # one folder inside another: the same program (an entry of its bin/, one of its lib/…)
    keys = sorted((k for k in groups if k[0] == "ok" and groups[k]["folder"]), key=lambda k: len(k[1]))
    for i, k in enumerate(keys):
        for outer in keys[:i]:
            if outer in groups and k in groups and _under(k[1], outer[1]) and k != outer:
                groups[outer]["entries"] += groups.pop(k)["entries"]
                break

    # folders of /opt with nothing of any package in them
    known = {g["folder"] for g in groups.values() if g["folder"]}
    opt_folders = []
    for o in places.opt_dirs:
        try:
            with os.scandir(o) as it:
                for d in it:
                    if d.is_dir(follow_symlinks=False) and not d.name.startswith(".") and "\n" not in d.path:
                        opt_folders.append(d.path)
        except OSError:
            continue
    opt_folders = [f for f in sorted(opt_folders) if not any(_under(f, k) or _under(k, f) for k in known)]
    if opt_folders:
        samples = {f: _opt_samples(f) for f in opt_folders}
        owned_opt = owned({s for v in samples.values() for s in v})
        for f in opt_folders:
            if not any(s in owned_opt for s in samples[f]):
                state = "ok" if _has_files(f) else "empty"
                groups[(state, f)] = {"folder": f, "scope": "system", "state": state, "entries": []}

    # entries of the same program on the desktop (the desktop folder of user-dirs.dirs: installers that use
    # xdg-desktop-icon put a copy there too)
    copies = []
    if places.desktop_dir and Path(places.desktop_dir).is_dir():
        for f in sorted(Path(places.desktop_dir).glob("*.desktop")):
            e = read_desktop(f, langs)
            if e:
                prog = exec_program(e["exec"], places.home, which)
                copies.append((str(f), f.name, prog["real"] if prog else ""))

    records = load_manifest(places).get("installed", [])
    used_records = set()
    out = []
    for (_state, _k), g in groups.items():
        out.append(_describe(g, places, copies, records, used_records))
    # what Zypp Desk noted and is still there, but found no other way (a program without menu entries…)
    for i, rec in enumerate(records):
        if i in used_records:
            continue
        p = _from_record(rec, places, langs, copies)
        if p:
            out.append(p)
    out.sort(key=lambda p: (p["state"] != "ok", _fold(p["name"])))
    return out


def _describe(g, places, copies, records, used_records):
    entries = sorted(g["entries"], key=lambda e: (e["hidden"], _fold(e["name"])))
    folder = g["folder"]
    main = _main_entry(entries, folder)
    name = main["name"] if main else (Path(folder).name if folder else "?")
    version = _version(entries, folder)
    if version and version not in name:
        name = f"{name} {version}"
    ids = {e["id"] for e in entries}
    desk = []
    for path, fname, prog in copies:
        if fname in ids or (folder and prog and _under(prog, folder)):
            desk.append(path)
    p = {"id": folder or (entries[0]["file"] if entries else ""), "name": name, "version": version,
         "summary": (main["comment"] or main["generic"]) if main else "",
         "categories": main["categories"] if main else [], "icon": main["icon"] if main else "",
         "icon_file": icon_file(main["icon"], places) if main else None, "folder": folder, "scope": g["scope"],
         "state": g["state"], "entries": entries, "desktop_copies": desk,
         "icons": _icon_files(entries, places),
         "mime_files": _mime_files(entries, places), "recorded": None, "extra": [],
         "uninstaller": find_uninstaller(folder) if g["state"] == "ok" and folder else None,
         "progs": sorted({e["prog_written"] for e in entries})}
    # the installer of this program was run by Zypp Desk: what it noted goes with it
    files = {e["file"] for e in entries}
    for i, rec in enumerate(records):
        created = rec.get("created", {})
        paths = {x for v in created.values() for x in v}
        if (folder and folder in paths) or files & paths:
            used_records.add(i)
            p["recorded"] = rec
            p["extra"] = sorted(x for x in _record_paths(rec, places) if x != folder)
            break
    return p


def _record_paths(rec, places):
    """The paths an install noted that are still there and can go with the program: not the hidden folders of the
    home (settings that something else may have written meanwhile)."""
    created = rec.get("created", {})
    out = []
    for kind, paths in created.items():
        for x in paths:
            if kind == "home" and os.path.basename(x).startswith("."):
                continue
            if os.path.lexists(x):
                out.append(x)
    return out


def _from_record(rec, places, langs, copies):
    created = rec.get("created", {})
    folders = [x for x in created.get("home", []) + created.get("opt", [])
               if os.path.isdir(x) and not os.path.basename(x).startswith(".")]
    apps = [x for x in created.get("apps", []) + created.get("local_apps", []) if os.path.isfile(x)]
    if not folders and not apps:
        return None
    entries = []
    for a in apps:
        e = read_desktop(a, langs)
        if e:
            prog = exec_program(e["exec"], places.home)
            e["prog"] = prog["real"] if prog else ""
            e["prog_written"] = prog["written"] if prog else ""
            e["exists"] = bool(prog and prog["exists"])
            entries.append(e)
    folder = folders[0] if folders else None
    scope = "home" if (folder and _under(folder, places.home)) or (not folder and apps and _under(
        apps[0], places.home)) else "system"
    g = {"folder": folder, "scope": scope, "state": "ok", "entries": entries}
    used = set()
    p = _describe(g, places, copies, [rec], used)
    if not entries:
        p["name"] = rec.get("name") or p["name"]
    return p


# ------------------------------------------------------------------ sizes

def folder_size(folder):
    """Bytes the folder takes on the disk (du), or None."""
    try:
        p = subprocess.run(["du", "-s", "-B1", "-x", "--", str(folder)], capture_output=True, text=True,
                           env=ENV_C, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"(\d+)\s", p.stdout)
    return int(m.group(1)) if m else None


def _size_cache_file(places):
    return places.cache_home / "zypp-desk" / "installer-sizes.json"


def sizes(folders, places=None, max_age=24 * 3600):
    """{folder: bytes} for these folders; a size measured less than a day ago, of a folder that has not changed
    since, is taken from the cache ($XDG_CACHE_HOME/zypp-desk/installer-sizes.json)."""
    places = places or default_places()
    cf = _size_cache_file(places)
    try:
        cache = json.loads(cf.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    now, out, changed = time.time(), {}, False
    for f in folders:
        try:
            mtime = os.stat(f).st_mtime
        except OSError:
            continue
        c = cache.get(f)
        if isinstance(c, dict) and c.get("mtime") == mtime and now - c.get("at", 0) < max_age:
            out[f] = c.get("size")
            continue
        n = folder_size(f)
        if n is not None:
            out[f] = n
            cache[f] = {"mtime": mtime, "size": n, "at": now}
            changed = True
    if changed:
        try:
            _write_json(cf, cache)
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ the record of what an installer created

def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_manifest(places):
    try:
        data = json.loads(Path(places.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "installed": [], "pending": []}
    if not isinstance(data, dict):
        return {"version": 1, "installed": [], "pending": []}
    data.setdefault("version", 1)
    for k in ("installed", "pending"):
        if not isinstance(data.get(k), list):
            data[k] = []
        data[k] = [r for r in data[k] if isinstance(r, dict)]
    return data


def save_manifest(places, data):
    _write_json(places.manifest, data)


def snapshot(places):
    """What is there now where installers put things: {kind: [absolute paths]}."""
    def names(d):
        try:
            return sorted(os.path.join(str(d), n) for n in os.listdir(d))
        except OSError:
            return []

    icons = []
    if places.icons_dir.is_dir():
        for root, dirs, files in os.walk(places.icons_dir):
            dirs.sort()
            icons += [os.path.join(root, f) for f in files]
            if len(icons) > SNAP_LIMIT:
                break
    out = {"home": names(places.home), "apps": names(places.app_dir), "icons": sorted(icons),
           "mime": names(places.mime_dir), "bin": names(places.home / ".local/bin") + names(places.home / "bin"),
           "opt": [x for o in places.opt_dirs for x in names(o)],
           "local_apps": [x for d in places.system_app_dirs if _under(d, places.local_root) for x in names(d)],
           "local_bin": names(places.local_root / "bin")}
    if places.desktop_dir:
        out["desktop"] = names(places.desktop_dir)
    return out


def compare(before, after):
    """What appeared between two snapshots: {kind: [paths]}, only the kinds with something."""
    out = {}
    for k, v in after.items():
        new = sorted(set(v) - set(before.get(k, [])))
        if new:
            out[k] = new
    return out


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", _fold(text).replace(" ", "-")).strip("-")
    return s[:48].strip("-") or "installer"


def make_record(info, before, after, code, started, langs=None):
    """The record of one run of an installer: what it was, how it ended and what it created."""
    created = compare(before, after)
    name = ""
    for a in created.get("apps", []) + created.get("local_apps", []):
        e = read_desktop(a, lang_keys() if langs is None else langs) if a.endswith(".desktop") else None
        if e and not e["hidden"]:
            name = e["name"]
            break
    if not name:
        tops = [x for x in created.get("home", []) + created.get("opt", []) if not os.path.basename(x).startswith(".")]
        name = os.path.basename(tops[0]) if tops else info.get("name", "")
    return {"id": f"{int(started)}-{_slug(info.get('name', ''))}", "name": name, "installer": info.get("path", ""),
            "installer_name": info.get("name", ""), "command": " ".join(info.get("argv") or []),
            "workdir": info.get("workdir", ""), "started": int(started), "finished": int(time.time()),
            "exit_code": code, "created": created}


def add_record(places, rec):
    data = load_manifest(places)
    data["installed"] = [r for r in data["installed"] if r.get("id") != rec["id"]] + [rec]
    save_manifest(places, data)


def add_pending(places, pending):
    data = load_manifest(places)
    data["pending"] = [p for p in data["pending"] if p.get("id") != pending["id"]] + [pending]
    save_manifest(places, data)


def drop_pending(places, pid):
    data = load_manifest(places)
    data["pending"] = [p for p in data["pending"] if p.get("id") != pid]
    save_manifest(places, data)


def prune_manifest(places):
    """Forget the installs of which nothing is left (their program was removed)."""
    data = load_manifest(places)
    keep = [r for r in data["installed"] if _record_paths(r, places)]
    if len(keep) != len(data["installed"]):
        data["installed"] = keep
        save_manifest(places, data)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


# ------------------------------------------------------------------ installers: look inside, unpack

def is_archive(path):
    return str(path).lower().endswith(ARCHIVE_EXTS)


def _strip_archive_ext(name):
    low = name.lower()
    for ext in ARCHIVE_EXTS:
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


def _installer_rank(rel):
    name = os.path.basename(rel).lower()
    if "uninstall" in name or rel.startswith("/") or ".." in rel.split("/"):
        return None
    depth = rel.strip("/").count("/")
    if name in INSTALLER_NAMES:
        r = INSTALLER_NAMES.index(name)
    elif name.endswith((".run", ".bin")):
        r = 20
    elif re.search(r"install|setup", name) and name.endswith((".sh", ".pl", ".py")):
        r = 30
    else:
        return None
    return (depth, r, name)


def _archive_members(path):
    """[(relative path, is_file, size, mode, head)] without unpacking; head only for the possible installers."""
    out = []
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist()[:MAX_MEMBERS + 1]:
                mode = info.external_attr >> 16
                is_file = not info.is_dir() and not stat.S_ISLNK(mode)
                out.append((info.filename, is_file, info.file_size, mode, None))
            heads = {}
            for rel, is_file, _s, _m, _h in out:
                if is_file and _installer_rank(rel):
                    with z.open(rel) as f:
                        heads[rel] = f.read(512)
        return [(r, i, s, m, heads.get(r)) for r, i, s, m, _h in out]
    with tarfile.open(path, "r:*") as t:
        for m in t:
            out.append((m.name, m.isfile(), m.size, m.mode, None))
            if len(out) > MAX_MEMBERS:
                break
        heads = {}
        for m in t.getmembers()[:MAX_MEMBERS + 1]:
            if m.isfile() and _installer_rank(m.name):
                f = t.extractfile(m)
                if f:
                    heads[m.name] = f.read(512)
    return [(r, i, s, md, heads.get(r)) for r, i, s, md, _h in out]


def inspect_installer(path, places=None):
    """What the chosen file is and how it would run, without running or unpacking anything:
    {path, name, folder, size, kind ('archive' | 'script' | 'binary'), installer (its path inside the archive, or
    its name), argv (the command, from its folder), dest (where an archive is unpacked), unpacked (bytes),
    candidates (other possible installers inside)}. Raises OSError / ValueError when it cannot be read."""
    places = places or default_places()
    path = os.path.abspath(path)
    st = os.stat(path)
    info = {"path": path, "name": os.path.basename(path), "folder": os.path.dirname(path), "size": st.st_size,
            "dest": None, "unpacked": None, "candidates": []}
    if is_archive(path):
        try:
            members = _archive_members(path)
        except (tarfile.TarError, zipfile.BadZipFile, EOFError) as e:
            raise ValueError(str(e)) from e
        if len(members) > MAX_MEMBERS:
            raise UnsafeArchive("too many files")
        total = sum(s for _r, f, s, _m, _h in members if f)
        ranked = sorted((r, rel) for rel, is_file, _s, _m, _h in members if is_file
                        for r in [_installer_rank(rel)] if r)
        info.update(kind="archive", unpacked=total, dest=str(places.installers_dir / _slug(
            _strip_archive_ext(info["name"]))), candidates=[rel for _r, rel in ranked[1:6]])
        if not ranked:
            info.update(installer=None, argv=None)
            return info
        rel = ranked[0][1]
        member = next(m for m in members if m[0] == rel)
        info["installer"] = re.sub(r"^(\./)+", "", rel)
        info["argv"] = command_from_head(os.path.basename(rel), member[4] or b"", bool(member[3] & 0o111))
        if info["argv"] is None and (member[4] or b"").startswith(b"\x7fELF"):
            info["argv"] = ["./" + os.path.basename(rel)]  # unpacked here: Zypp Desk can let it run
        return info
    elf = is_elf(path)
    info.update(kind="binary" if elf else "script", installer=info["name"])
    argv = command_for(path)
    if argv is None and elf:
        # a program without the permission to run: a copy in Zypp Desk's own folder gets it
        info["dest"] = str(places.installers_dir / _slug(info["name"]))
        argv = ["./" + info["name"]]
    info["argv"] = argv
    return info


def _own_folder(path, places):
    p = os.path.realpath(path)
    return _under(p, os.path.realpath(places.installers_dir)) and p != os.path.realpath(places.installers_dir)


def _inside(dest, name):
    target = os.path.normpath(os.path.join(str(dest), name))
    if name.startswith("/") or not (target == str(dest) or target.startswith(str(dest).rstrip("/") + "/")):
        raise UnsafeArchive(name)
    return target


def unpack(archive, dest, places):
    """Unpack an installer archive into dest, a folder of Zypp Desk's own (emptied first). Nothing can land
    outside it: no absolute paths, no '..', no links that point out, no devices (tar's "data" filter); a zip's
    links are left out. Raises UnsafeArchive, or OSError / tarfile.TarError / zipfile.BadZipFile."""
    dest = Path(dest)
    if not _own_folder(dest, places):
        raise UnsafeArchive(str(dest))
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, mode=0o700)
    if str(archive).lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise UnsafeArchive("too big")
            for info in infos:
                target = _inside(dest, info.filename)
                mode = info.external_attr >> 16
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if stat.S_ISLNK(mode):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o755 if mode & 0o111 else 0o644)
        return dest
    with tarfile.open(archive, "r:*") as t:
        members, total = [], 0
        for m in t:
            members.append(m)
            total += m.size if m.isfile() else 0
            if len(members) > MAX_MEMBERS or total > MAX_UNPACKED:
                raise UnsafeArchive("too big")
        try:
            t.extractall(dest, members=members, filter="data")
        except tarfile.FilterError as e:
            raise UnsafeArchive(str(e)) from e
    return dest


def prepare_installer(info, places):
    """Unpack (when it is an archive), find the installer and take the 'before' picture. Returns
    {workdir, argv, installer (its full path), before}."""
    if info["kind"] == "archive":
        dest = unpack(info["path"], info["dest"], places)
        installer = os.path.realpath(_inside(dest, info["installer"]))
        if not _under(installer, os.path.realpath(dest)) or not os.path.isfile(installer):
            raise UnsafeArchive(info["installer"])
        argv = command_for(installer)
        if argv is None and is_elf(installer):
            os.chmod(installer, 0o755)
            argv = command_for(installer)
    elif info.get("dest"):  # a binary without the permission to run: a copy of Zypp Desk's own gets it
        dest = Path(info["dest"])
        if not _own_folder(dest, places):
            raise UnsafeArchive(str(dest))
        if dest.exists() and not dest.is_symlink():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, mode=0o700)
        installer = str(dest / info["name"])
        shutil.copyfile(info["path"], installer)
        os.chmod(installer, 0o755)
        argv = command_for(installer)
    else:
        installer = info["path"]
        argv = command_for(installer)
    if not argv:
        raise ValueError(installer)
    return {"workdir": os.path.dirname(installer), "argv": argv, "installer": installer,
            "before": snapshot(places)}


# ------------------------------------------------------------------ running in a terminal window

def wrapped(argv, workdir, status_file, ok, bad, bye):
    """The arguments that run argv in workdir and write its exit code to status_file (see WRAPPER)."""
    return ["sh", "-c", WRAPPER, "zypp-desk", str(workdir), str(status_file), ok, bad, bye, *argv]


def terminal_argv(inner, which=shutil.which):
    """The command that opens a terminal window running inner (a list of arguments), or None when there is no
    terminal program. konsole --separate: its own process, which ends when the window closes."""
    if which("konsole"):
        return ["konsole", "--separate", "-e", *inner]
    if which("xdg-terminal-exec"):
        return ["xdg-terminal-exec", *inner]
    if which("xterm"):
        return ["xterm", "-e", *inner]
    return None


def launch(argv, workdir):
    """Start a terminal window as the user, in a session of its own: closing Zypp Desk does not close it."""
    return subprocess.Popen(argv, cwd=str(workdir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def status_file(places, kind):
    d = places.installers_dir
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = d / f"{kind}-{int(time.time() * 1000)}.status"
    try:
        f.unlink()
    except OSError:
        pass
    return f


def read_status(path):
    """The exit code written by the wrapper, or None when it has not ended (or cannot be read)."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def display(argv):
    return " ".join(shlex.quote(a) for a in argv)


# ------------------------------------------------------------------ removing

def _protected(places):
    """Folders of the home that must never go to the trash themselves."""
    out = {os.path.normpath(str(places.home))}
    for rel in places.home_containers():
        out.add(os.path.normpath(str(places.home / rel)))
    for d in (places.data_home, places.app_dir, places.icons_dir, places.mime_dir, places.cache_home,
              places.desktop_dir, places.data_home / "mime", places.manifest.parent if places.manifest else None):
        if d is not None:
            out.add(os.path.normpath(str(d)))
    return out


def safe_to_trash(path, places):
    """True for a path inside the user's home that is not the home itself nor one of its shared folders."""
    p = os.path.normpath(os.path.abspath(str(path)))
    return _under(p, places.home) and p not in _protected(places) and "\n" not in p


def removal_paths(program, places):
    """What moving the program to the trash takes away, in order: its folder, its menu entries (and their copies on
    the desktop), its icons and mime types, and what Zypp Desk noted when it installed it. Only what is in the
    user's home and still there."""
    want = []
    folder = program.get("folder")
    if folder and program.get("state") != "missing":
        want.append(folder)
    for e in program.get("entries", []):
        want.append(e["file"])
    want += program.get("desktop_copies", []) + program.get("icons", []) + program.get("mime_files", [])
    want += program.get("extra", [])
    if not folder:
        want += [p for p in program.get("progs", []) if os.path.isfile(p)]
    out = []
    for p in want:
        p = os.path.normpath(p)
        if p in out or not os.path.lexists(p) or not safe_to_trash(p, places):
            continue
        if any(_under(p, q) for q in out):
            continue
        out.append(p)
    # a folder listed after a path inside it: keep only the folder
    return [p for p in out if not any(q != p and _under(p, q) for q in out)]


def after_trash(paths, places, which=shutil.which):
    """The commands that bring the menu and the file types up to date after these paths went to the trash."""
    cmds = []
    if any(_under(p, places.mime_dir) for p in paths) and which("update-mime-database"):
        cmds.append(["update-mime-database", str(places.data_home / "mime")])
    if (any(_under(p, places.app_dir) for p in paths) and (places.app_dir / "mimeinfo.cache").exists()
            and which("update-desktop-database")):
        cmds.append(["update-desktop-database", str(places.app_dir)])
    return cmds


def gio_trash(path):
    """Move one path to the trash with gio, as the user. Returns None, or the error."""
    try:
        r = subprocess.run(["gio", "trash", "--", str(path)], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None if r.returncode == 0 else (r.stderr.strip() or f"gio trash: {r.returncode}")


def trash(paths, places, mover=gio_trash):
    """Move the paths to the trash (only safe ones: see safe_to_trash), then update the menu and the file types.
    Returns {'moved': [...], 'failed': [(path, error)]}."""
    moved, failed = [], []
    for p in paths:
        if not safe_to_trash(p, places):
            failed.append((p, _("it is not inside your folder")))
            continue
        err = mover(p)
        if err:
            failed.append((p, err))
        else:
            moved.append(p)
    for cmd in after_trash(moved, places):
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    return {"moved": moved, "failed": failed}


def system_commands(program, places):
    """The commands that remove a program installed for the whole computer, to run in a terminal (with the
    administrator password). Zypp Desk shows them; it never runs them."""
    unin, folder = program.get("uninstaller"), program.get("folder")
    if unin:
        argv = command_for(unin) or ["sh", "./" + os.path.basename(unin)]
        return [f"cd {shlex.quote(os.path.dirname(unin))}", "sudo " + display(argv)]
    cmds = []
    if folder and os.path.lexists(folder):
        cmds.append(f"sudo rm -r -- {shlex.quote(folder)}")
    for e in program.get("entries", []):
        if not _under(e["file"], places.home):
            cmds.append(f"sudo rm -- {shlex.quote(e['file'])}")
    return cmds
