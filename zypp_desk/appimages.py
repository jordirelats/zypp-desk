# SPDX-License-Identifier: GPL-3.0-or-later
"""AppImages: programs that are one file, outside zypper and Flatpak.

An AppImage is a small ELF program with a file system (squashfs) appended to it. Zypp Desk:

- finds them in the folders where people keep them (FOLDERS, the Downloads folder only at its first level, the
  setting "appimage_folders") and through the menu entries that run one (AppImageLauncher, Gear Lever or the
  AppImage itself write them). Only the names and the first bytes of files are read: no walk of the whole home.
- reads the name, version and icon from the .desktop file inside WITHOUT running the AppImage: the squashfs offset
  is computed from the ELF headers and `unsquashfs -lls` / `-cat` read that file system as data. Nothing is
  extracted to disk and nothing inside is executed. Without unsquashfs (or for the old type 1 and other formats)
  the menu entry or the file name is shown instead. What was read is kept in ~/.cache/zypp-desk/appimage/<key>.
- reads where new versions are published from the ELF section ".upd_info" (pure Python), and asks GitHub for the
  latest release only when the user asks (websources: its ETag cache and the 60 questions an hour).
- puts one in the menu, downloads a new version next to it as <file>.new (SHA-256 checked when GitHub gives it),
  replaces it keeping the old one as <file>.old until the new one has opened, and lists what goes to the trash
  when one is removed.

Nothing here runs an AppImage (the page opens one, as the user, when asked) and nothing needs root. No Qt.
"""

import configparser
import difflib
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from . import websources
from .backend import vercmp
from .i18n import _, user_lang
from .webapps import _exec_words

# where people keep AppImages: (folder, how many levels of sub-folders to look into). The Downloads folder (XDG)
# is added at its first level only, and the setting "appimage_folders" adds more.
FOLDERS = (
    ("~/Applications", 0),  # AppImageLauncher's folder, and where "Add an AppImage…" puts them
    ("~/AppImages", 0),  # Gear Lever's
    ("~/.local/bin", 0),
    ("~/bin", 0),
    ("/opt", 1),  # /opt/<vendor>/<program>.AppImage
)
HOME_FOLDER = "~/Applications"  # "Add an AppImage…" moves or copies the file here (setting "appimage_home")
SETTING_FOLDERS = "appimage_folders"
SETTING_HOME = "appimage_home"
SETTING_SOURCES = "appimage_sources"  # {path: "owner/repo"}: where the user said new versions are published

MAX_FILES_PER_FOLDER = 5000  # a Downloads folder with years of files: look at names only, and not forever
MAX_DESKTOP = 64 * 1024
MAX_ICON = 4 * 1024 * 1024
MAX_LISTING = 4 * 1024 * 1024
TOOL_TIMEOUT = 30
CACHE_DAYS = 30
ENV_C = dict(os.environ, LC_ALL="C")

_SHT_NOBITS = 8
_GH_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_VERSION_RE = re.compile(r"(?<![0-9A-Za-z])[vV]?(\d+(?:\.\d+)+(?:[-~.]?(?:rc|beta|alpha|pre)\.?\d*)?)",
                         re.I)
_ARCH_WORDS = {
    "x86_64": ("x86_64", "x86-64", "amd64", "x64"),
    "aarch64": ("aarch64", "arm64"),
    "armv7l": ("armhf", "armv7l", "armv7"),
    "i686": ("i386", "i686", "x86"),
}
_LIST_RE = re.compile(r"^([-dlcbps][-rwxsStT]{9})\s+\S+\s+(\d+)\s+\d{4}-\d\d-\d\d\s+\d\d:\d\d\s(.*)$")


# ------------------------------------------------------------------ folders

def data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def applications_dir():
    return data_home() / "applications"


def icons_dir():
    return data_home() / "icons"


def cache_dir():
    """~/.cache/zypp-desk/appimage: what was read from inside each AppImage (only you can read it)."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "zypp-desk" / "appimage"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def xdg_download_dir():
    """The user's Downloads folder, from ~/.config/user-dirs.dirs (the page passes Qt's answer, the same one)."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        m = re.search(r'^XDG_DOWNLOAD_DIR="([^"]*)"', cfg.read_text(encoding="utf-8"), re.M)
    except (OSError, UnicodeDecodeError):
        m = None
    if m:
        value = m.group(1).replace("$HOME", str(Path.home()))
        if value.startswith("/") and value.rstrip("/") != str(Path.home()):
            return value
    return str(Path.home() / "Downloads")


def home_folder(settings=None):
    """Where "Add an AppImage…" puts the file."""
    value = (settings or {}).get(SETTING_HOME) or HOME_FOLDER
    return os.path.expanduser(value)


def folders(extra=(), download=None):
    """[(folder, levels)]: every folder looked at, each once, those that exist."""
    out, seen = [], set()
    items = list(FOLDERS) + [(download or xdg_download_dir(), 0)] + [(f, 0) for f in (extra or ()) if f]
    for folder, depth in items:
        p = os.path.realpath(os.path.expanduser(str(folder)))
        if p in seen or not os.path.isdir(p):
            continue
        seen.add(p)
        out.append((os.path.expanduser(str(folder)), depth))
    return out


def display_path(path):
    """'/home/x/AppImages/a.AppImage' → '~/AppImages/a.AppImage'."""
    home = str(Path.home())
    path = str(path)
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


# ------------------------------------------------------------------ what a file is

def appimage_type(path):
    """2 or 1 when the file is an AppImage of that type, 0 when it is not (or cannot be read).

    Type 2: an ELF with the magic bytes 'AI' 0x02 at offset 8. Type 1: an ELF with 'AI' 0x01 there, or an ELF that is
    also an ISO 9660 image (the oldest ones carry no magic)."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):  # a FIFO named .AppImage would block the reading
            return 0
        with open(path, "rb") as f:
            head = f.read(16)
            if len(head) < 16 or head[:4] != b"\x7fELF":
                return 0
            if head[8:11] == b"AI\x02":
                return 2
            if head[8:11] == b"AI\x01":
                return 1
            f.seek(32769)
            return 1 if f.read(5) == b"CD001" else 0
    except OSError:
        return 0


def _elf(f, size):
    """The sections of an ELF file ({name: (offset, size, type)}) and where the ELF part ends, or None."""
    f.seek(0)
    h = f.read(64)
    if len(h) < 52 or h[:4] != b"\x7fELF" or h[4] not in (1, 2) or h[5] not in (1, 2):
        return None
    e = "<" if h[5] == 1 else ">"
    if h[4] == 2:
        if len(h) < 64:
            return None
        shoff, = struct.unpack_from(e + "Q", h, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", h, 0x3A)
        fmt, need = e + "IIQQQQ", 64
    else:
        shoff, = struct.unpack_from(e + "I", h, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", h, 0x2E)
        fmt, need = e + "IIIIII", 40
    if not (0 < shnum <= 4096 and shentsize >= need and shoff > 0 and shoff + shentsize * shnum <= size
            and shstrndx < shnum):
        return None
    f.seek(shoff)
    raw = f.read(shentsize * shnum)
    if len(raw) < shentsize * shnum:
        return None
    heads = [struct.unpack_from(fmt, raw, i * shentsize) for i in range(shnum)]
    str_off, str_size = heads[shstrndx][4], heads[shstrndx][5]
    names = b""
    if str_off + str_size <= size and str_size <= 1 << 20:
        f.seek(str_off)
        names = f.read(str_size)
    sections = {}
    end = shoff + shentsize * shnum
    for name_i, typ, _flags, _addr, off, sz in heads:
        if typ != _SHT_NOBITS and off + sz <= size:
            end = max(end, off + sz)
        if name_i < len(names):
            stop = names.find(b"\0", name_i)
            name = names[name_i:stop if stop >= 0 else len(names)]
            if name:
                sections[name.decode("latin-1")] = (off, sz, typ)
    return {"sections": sections, "end": end}


def _squashfs_ok(sb, pos, size):
    """A squashfs 4 superblock that fits in the file."""
    if len(sb) < 48 or sb[:4] != b"hsqs":
        return False
    block_size, = struct.unpack_from("<I", sb, 12)
    major, minor = struct.unpack_from("<HH", sb, 28)
    used, = struct.unpack_from("<Q", sb, 40)
    return major == 4 and minor == 0 and 4096 <= block_size <= 1 << 20 and \
        block_size & (block_size - 1) == 0 and 0 < used <= size - pos


def squashfs_offset(path):
    """Where the AppImage's file system starts (what `--appimage-offset` would say, without running it), or None.
    From the ELF headers; else the first squashfs superblock in the first 16 MiB."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            info = _elf(f, size)
            if info:
                f.seek(info["end"])
                if _squashfs_ok(f.read(96), info["end"], size):
                    return info["end"]
            f.seek(0)
            data = f.read(16 << 20)
    except OSError:
        return None
    pos = data.find(b"hsqs")
    while pos >= 0:
        if _squashfs_ok(data[pos:pos + 96], pos, size):
            return pos
        pos = data.find(b"hsqs", pos + 1)
    return None


def payload_kind(path):
    """'squashfs', 'dwarfs' (some new runtimes), 'iso' (type 1) or '' — what follows the runtime."""
    if appimage_type(path) == 1:
        return "iso"
    if squashfs_offset(path) is not None:
        return "squashfs"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            info = _elf(f, size)
            if info:
                f.seek(info["end"])
                if f.read(6) == b"DWARFS":
                    return "dwarfs"
    except OSError:
        pass
    return ""


def update_info(path):
    """The update information the AppImage carries ('gh-releases-zsync|owner|repo|latest|name*.zsync', 'zsync|URL'…),
    or "". Type 2: the ELF section .upd_info; type 1: 512 bytes at offset 33651 of the ISO image."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if appimage_type(path) == 1:
                f.seek(33651)
                raw = f.read(512)
            else:
                info = _elf(f, size)
                sec = (info or {}).get("sections", {}).get(".upd_info")
                if not sec or sec[1] > 8192 or sec[2] == _SHT_NOBITS:
                    return ""
                f.seek(sec[0])
                raw = f.read(sec[1])
    except OSError:
        return ""
    text = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    return text if text.isprintable() else ""


def parse_update_info(text):
    """What the update information says, or None:
    {"kind": "github", "repo": "owner/repo", "tag": "latest", "pattern": "App-*-x86_64.AppImage"},
    {"kind": "zsync", "url": "https://…/App.AppImage.zsync"}, {"kind": "page", "url": …} (pling, the AppImageHub
    store) or {"kind": "unknown"}. "text" is always the text as it was written."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split("|")
    kind = parts[0].strip()
    if kind.startswith("gh-releases") and len(parts) >= 5:
        owner, repo, tag, name = (p.strip() for p in parts[1:5])
        if _GH_NAME.fullmatch(owner) and _GH_NAME.fullmatch(repo) and owner not in (".", "..") and \
                repo not in (".", ".."):
            pattern = name[:-len(".zsync")] if name.endswith(".zsync") else name
            return {"kind": "github", "repo": f"{owner}/{repo}", "tag": tag or "latest", "pattern": pattern,
                    "text": text}
    if kind == "zsync" and len(parts) >= 2 and parts[1].strip().startswith("https://"):
        return {"kind": "zsync", "url": parts[1].strip(), "text": text}
    if kind == "pling-v1-zsync" and len(parts) >= 2 and parts[1].strip().isdigit():
        return {"kind": "page", "url": f"https://www.pling.com/p/{parts[1].strip()}/", "text": text}
    return {"kind": "unknown", "text": text}


def github_source(text):
    """What the user typed as the place of new versions ('https://github.com/owner/repo', 'owner/repo') →
    the source dict, or None."""
    text = (text or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", text) or \
        re.match(r"^([^/\s]+)/([^/\s#?]+)$", text)
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    if not (_GH_NAME.fullmatch(owner) and _GH_NAME.fullmatch(repo)) or owner in (".", "..") or repo in (".", ".."):
        return None
    return {"kind": "github", "repo": f"{owner}/{repo}", "tag": "latest", "pattern": "", "user": True,
            "text": f"github.com/{owner}/{repo}"}


def source_text(source):
    """Where new versions come from, in a few words."""
    if not source:
        return ""
    if source["kind"] == "github":
        return "github.com/" + source["repo"]
    if source["kind"] in ("zsync", "page"):
        return urllib.parse.urlsplit(source["url"]).netloc
    return source.get("text", "")


# ------------------------------------------------------------------ reading inside (never running it)

def _run_capped(cmd, cap):
    """stdout of a command, at most cap bytes (None when it is longer, fails or takes too long)."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             env=ENV_C)
    except OSError:
        return None
    timer = threading.Timer(TOOL_TIMEOUT, p.kill)
    timer.start()
    try:
        data = p.stdout.read(cap + 1)
        if len(data) > cap:
            p.kill()
            return None
        p.stdout.read()  # nothing should be left; let the process end
        rc = p.wait()
    finally:
        timer.cancel()
        p.stdout.close()
        if p.poll() is None:
            p.kill()
            p.wait()
    return data if rc == 0 else None


def _listing(tool, path, offset):
    """{name: (type char, size, link target)} of the root folder of the file system."""
    out = _run_capped([tool, "-o", str(offset), "-lls", "-max-depth", "1", "-d", "", str(path)], MAX_LISTING)
    if out is None:
        return None
    entries = {}
    for line in out.decode("utf-8", errors="replace").splitlines():
        m = _LIST_RE.match(line)
        if not m:
            continue
        name, target = m.group(3), ""
        if m.group(1)[0] == "l" and " -> " in name:
            name, target = name.split(" -> ", 1)
        name = name.lstrip("/")
        if name and "/" not in name:
            entries[name] = (m.group(1)[0], int(m.group(2)), target)
    return entries


def _cat(tool, path, offset, inner, cap):
    """The bytes of one file inside (a symbolic link is followed by unsquashfs), at most cap."""
    return _run_capped([tool, "-o", str(offset), "-cat", str(path), "/" + inner.lstrip("/")], cap)


def _parse_desktop(text):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read_string(text)
    except (configparser.Error, ValueError):
        return None
    return cp["Desktop Entry"] if cp.has_section("Desktop Entry") else None


def _localized(entry, key):
    """{"": value, "ca": value, "de_DE": value…} of a key of a .desktop entry."""
    out = {}
    for k, v in entry.items():
        if k == key:
            out[""] = v.strip()
        elif k.startswith(key + "[") and k.endswith("]"):
            out[k[len(key) + 1:-1]] = v.strip()
    return {k: v[:200] for k, v in out.items() if v}


def _image_kind(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    head = data[:2048].lstrip()
    if (head.startswith(b"<?xml") or head.startswith(b"<svg") or head.startswith(b"<!--")) and b"<svg" in data[:8192]:
        return "svg"
    return ""


def _cache_key(path, st):
    raw = f"{os.path.realpath(path)}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ino}"
    return hashlib.sha256(raw.encode("utf-8", errors="surrogateescape")).hexdigest()[:32]


def read_embedded(path, st=None):
    """What the AppImage says about itself: {"names", "comments" ({lang: text}), "version", "icon_file",
    "terminal", "categories", "wmclass", "why" (why nothing was read: "notool", "iso", "dwarfs", "unknown"…)}.

    Read from its .desktop file and icon with unsquashfs, as data: the AppImage is never run. Kept in the cache
    folder until the file changes."""
    try:
        st = st or os.stat(path)
    except OSError:
        return {"why": "gone"}
    key = _cache_key(path, st)
    d = cache_dir() / key
    meta_file = d / "meta.json"
    tool = shutil.which("unsquashfs")
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if not (meta.get("why") == "notool" and tool):  # unsquashfs installed since: read it now
            if meta.get("icon_file") and not os.path.isfile(meta["icon_file"]):
                meta["icon_file"] = ""
            os.utime(d)  # still used: the cache cleaning keeps it
            return meta
    except (OSError, ValueError):
        pass
    meta = _extract(path, tool, d)
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(meta_file)
    except OSError:
        pass
    return meta


def _extract(path, tool, d):
    kind = payload_kind(path)
    if kind != "squashfs":
        return {"why": kind or "unknown"}
    if not tool:
        return {"why": "notool"}
    offset = squashfs_offset(path)
    entries = _listing(tool, path, offset)
    if not entries:
        return {"why": "unreadable"}
    desktops = sorted(n for n, (t, size, _l) in entries.items() if n.endswith(".desktop") and t in "-l"
                      and size <= MAX_DESKTOP)
    entry = None
    for name in desktops:
        data = _cat(tool, path, offset, name, MAX_DESKTOP)
        if data:
            entry = _parse_desktop(data.decode("utf-8", errors="replace"))
            if entry is not None:
                break
    if entry is None:
        return {"why": "nodesktop"}
    meta = {"names": _localized(entry, "Name"), "comments": _localized(entry, "Comment"),
            "version": (entry.get("X-AppImage-Version") or "").strip()[:60],
            "terminal": (entry.get("Terminal") or "").strip().lower() == "true",
            "categories": [c for c in (entry.get("Categories") or "").split(";") if re.fullmatch(r"[A-Za-z0-9-]+", c)],
            "wmclass": re.sub(r"[\r\n]", " ", entry.get("StartupWMClass") or "")[:100],
            "icon_file": "", "why": ""}
    icon = (entry.get("Icon") or "").strip()
    candidates = []
    if icon and "/" not in icon:
        candidates += [f"{icon}.svg", f"{icon}.png"]
    candidates.append(".DirIcon")
    for name in candidates:
        if name not in entries:
            continue
        data = _cat(tool, path, offset, name, MAX_ICON)
        kind = _image_kind(data or b"")
        if not kind:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = d / f"icon.{kind}"
            target.write_bytes(data)
            meta["icon_file"] = str(target)
        except OSError:
            pass
        break
    return meta


def _pick(values, langs):
    if not values:
        return ""
    for lang in langs:
        if lang in values:
            return values[lang]
    return values.get("", "")


def _langs():
    """The user's languages for Name[xx]: 'ca_ES.UTF-8' → ['ca_ES', 'ca']."""
    out = []
    for value in (os.environ.get("LANGUAGE") or "").split(":") + [user_lang()]:
        value = value.split(".")[0].split("@")[0]
        if value and value not in ("C", "POSIX"):
            for v in (value, value.split("_")[0]):
                if v not in out:
                    out.append(v)
    return out


def pretty_name(path):
    """A name from the file name when nothing better is known: 'Kaku-2.0.2-x86_64.AppImage' → 'Kaku'."""
    stem = Path(path).name
    stem = _without_arch(re.sub(r"\.appimage$", "", stem, flags=re.I))
    stem = _VERSION_RE.sub("", stem)
    stem = re.sub(r"[-_.]+", " ", stem).strip()
    return (stem[:1].upper() + stem[1:]) if stem else Path(path).name


def _without_arch(text):
    for words in _ARCH_WORDS.values():
        for w in words:
            text = re.sub(rf"[-_.]?{re.escape(w)}(?![0-9A-Za-z])", "", text, flags=re.I)
    return text


def version_from_name(path):
    """'Kaku-2.0.2-x86_64.AppImage' → '2.0.2'; "" when the name has no version."""
    m = _VERSION_RE.search(_without_arch(Path(path).name))
    return m.group(1) if m else ""


# ------------------------------------------------------------------ menu entries

def _desktop_string(value):
    """A value of type string in a .desktop file: one line, backslashes escaped."""
    return re.sub(r"[\r\n\t]+", " ", str(value)).replace("\\", "\\\\")


def _exec_quote(path):
    """A path as an argument of Exec: quoted, with the reserved characters escaped (desktop entry specification:
    first the quoting, then the string escaping that doubles each backslash), and % as %%."""
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", path) + '"'
    return quoted.replace("%", "%%").replace("\\", "\\\\")


def _targets(entry):
    """The files a menu entry runs: the absolute paths in its Exec and TryExec."""
    words = _exec_words(entry.get("Exec", "")) + ([entry.get("TryExec", "")] if entry.get("TryExec") else [])
    out = []
    for w in words:
        w = os.path.expanduser(w)
        if w.startswith("/") and w not in out:
            out.append(w)
    return out


def _runs_appimage(word):
    if word.lower().endswith(".appimage"):
        return True
    if word.startswith(("/usr/", "/bin/", "/sbin/", "/lib", "/etc/", "/proc/", "/sys/", "/dev/")):
        return False
    return os.path.isfile(word) and appimage_type(word) > 0


def menu_entries(folder=None):
    """[dict(desktop, target, name, comment, icon, version, ours)] — the menu entries of the user that run an
    AppImage (the file may be gone: then the entry is an orphan)."""
    folder = Path(folder) if folder else applications_dir()
    out = []
    try:
        files = sorted(folder.glob("*.desktop"))
    except OSError:
        return out
    for f in files:
        try:
            if f.stat().st_size > MAX_DESKTOP:
                continue
            entry = _parse_desktop(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if entry is None or (entry.get("Hidden") or "").strip().lower() == "true":
            continue
        target = next((w for w in _targets(entry) if _runs_appimage(w)), None)
        if not target:
            continue
        out.append(dict(desktop=str(f), target=target, name=_pick(_localized(entry, "Name"), _langs()),
                        comment=_pick(_localized(entry, "Comment"), _langs()),
                        icon=(entry.get("Icon") or "").strip(), version=(entry.get("X-AppImage-Version") or "").strip(),
                        ours=(entry.get("X-Zypp-Desk-AppImage") or "").strip() == "true"))
    return out


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "app"


def _png_size(path):
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None


def install_icon(icon_file, slug):
    """Copy the AppImage's icon into the user's icon folder; its path, or "" (the menu entry then names a generic
    icon). The entry names the icon by its full path: the icon theme cache of a running desktop may not know a new
    name yet."""
    kind = _image_kind(Path(icon_file).read_bytes()[:8192]) if icon_file and os.path.isfile(icon_file) else ""
    if not kind:
        return ""
    if kind == "svg":
        folder = icons_dir() / "hicolor" / "scalable" / "apps"
    else:
        size = (_png_size(icon_file) or (256, 256))[0]
        std = [16, 22, 24, 32, 48, 64, 96, 128, 192, 256, 512]
        bucket = next((s for s in std if s >= min(size, 512)), 512)
        folder = icons_dir() / "hicolor" / f"{bucket}x{bucket}" / "apps"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"appimage-{slug}.{kind}"
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(icon_file, tmp)
    os.chmod(tmp, 0o644)
    tmp.replace(target)
    return str(target)


def write_menu_entry(app):
    """Put an AppImage in the menu: a .desktop file in the user's applications folder (with the marker
    X-Zypp-Desk-AppImage=true) and its icon in the user's icon folder. Returns the .desktop path. An entry Zypp
    Desk wrote before for the same file is written again (after a new version, say)."""
    path = app["path"]
    folder = applications_dir()
    folder.mkdir(parents=True, exist_ok=True)
    mine = next((e["desktop"] for e in menu_entries() if e["ours"] and
                 os.path.realpath(e["target"]) == os.path.realpath(path)), None)
    if mine:
        target = Path(mine)
        slug = target.stem[len("appimage-"):] if target.stem.startswith("appimage-") else _slug(pretty_name(path))
    else:
        base = _slug(pretty_name(path))  # from the file: the same name in every language
        slug, n = base, 2
        while (folder / f"appimage-{slug}.desktop").exists():
            slug, n = f"{base}-{n}", n + 1
        target = folder / f"appimage-{slug}.desktop"
    icon = install_icon(app.get("icon_file") or "", slug) or "application-x-executable"
    lines = ["[Desktop Entry]", "Type=Application", f"Name={_desktop_string(app['name'])}"]
    if app.get("summary"):
        lines.append(f"Comment={_desktop_string(app['summary'])}")
    lines += [f"Exec={_exec_quote(path)} %U", f"TryExec={_desktop_string(path)}", f"Icon={_desktop_string(icon)}",
              f"Terminal={'true' if app.get('terminal') else 'false'}"]
    if app.get("categories"):
        lines.append("Categories=" + ";".join(app["categories"]) + ";")
    if app.get("wmclass"):
        lines.append(f"StartupWMClass={_desktop_string(app['wmclass'])}")
    if app.get("version"):
        lines.append(f"X-AppImage-Version={_desktop_string(app['version'])}")
    lines.append("X-Zypp-Desk-AppImage=true")
    tmp = target.with_name("." + target.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)
    return str(target)


def _icon_files(icon, app_folder, keep):
    """The icon files of a menu entry that are the user's own (never one of the system): a full path inside the
    user's icon folder or the '.icons' folder beside the AppImages, or a name found in the user's icon folder.
    Icons named by another entry that stays (keep) are left."""
    if not icon or icon in keep:
        return []
    base = icons_dir()
    own_roots = [os.path.realpath(base), os.path.realpath(os.path.join(app_folder, ".icons"))]
    out = []
    if icon.startswith("/"):
        real = os.path.realpath(icon)
        if os.path.isfile(icon) and any(real.startswith(r + "/") for r in own_roots):
            out.append(icon)
    elif "/" not in icon and base.is_dir():
        for p in base.glob(f"*/*/apps/{glob_escape(icon)}.*"):
            if p.suffix in (".png", ".svg", ".svgz", ".xpm") and p.is_file():
                out.append(str(p))
    return out


def glob_escape(text):
    return re.sub(r"([*?\[])", r"[\1]", text)


def menu_files(app):
    """What "Remove it from the menu" moves to the trash: its menu entries and their icons that are the user's own
    and that no other entry uses."""
    desktops = list(app.get("menu") or [])
    keep = set()
    folder = applications_dir()
    try:
        others = [f for f in folder.glob("*.desktop") if str(f) not in desktops]
    except OSError:
        others = []
    for f in others:
        try:
            entry = _parse_desktop(f.read_text(encoding="utf-8", errors="replace")) if \
                f.stat().st_size <= MAX_DESKTOP else None
        except OSError:
            entry = None
        if entry is not None and entry.get("Icon"):
            keep.add(entry.get("Icon").strip())
    files = list(desktops)
    for d in desktops:
        try:
            entry = _parse_desktop(Path(d).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            entry = None
        if entry is None:
            continue
        for f in _icon_files((entry.get("Icon") or "").strip(), app.get("folder") or "", keep):
            if f not in files:
                files.append(f)
    return files


def remove_files(app):
    """What "Remove it…" moves to the trash: the file, the old version kept after an update, its menu entries and
    their icons."""
    files = [app["path"]] if app.get("path") and os.path.lexists(app["path"]) and not app.get("orphan") else []
    if app.get("backup"):
        files.append(app["backup"])
    for f in menu_files(app):
        if f not in files:
            files.append(f)
    return files


# ------------------------------------------------------------------ the scan

def _files_in(folder, depth):
    out = []
    try:
        with os.scandir(folder) as it:
            for n, e in enumerate(it):
                if n >= MAX_FILES_PER_FOLDER:
                    break
                try:
                    if e.name.lower().endswith(".appimage") and e.is_file():
                        out.append(e.path)
                    elif depth > 0 and e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        out += _files_in(e.path, depth - 1)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def backup_of(path):
    """The old version kept after an update (<file>.old), or ""."""
    b = path + ".old"
    return b if os.path.isfile(b) and appimage_type(b) else ""


def describe(path, entries=(), langs=None):
    """One AppImage as an app of the Applications page (see scan)."""
    st = os.stat(path)
    langs = langs if langs is not None else _langs()
    meta = read_embedded(path, st)
    e0 = entries[0] if entries else {}
    source = parse_update_info(update_info(path))
    name = _pick(meta.get("names"), langs) or e0.get("name") or pretty_name(path)
    version = meta.get("version") or e0.get("version") or version_from_name(path)
    folder = os.path.dirname(path)
    return dict(
        id="appimage:" + path, kind="appimage", origin="appimage", origin_name="AppImage", name=name,
        summary=_pick(meta.get("comments"), langs) or e0.get("comment") or "",
        icon=meta.get("icon_file") or e0.get("icon") or "", icon_file=meta.get("icon_file") or "",
        package=path, version=version, path=path, folder=folder, size=st.st_size, mtime=int(st.st_mtime),
        type=appimage_type(path), menu=[e["desktop"] for e in entries], menu_ours=[e["desktop"] for e in entries
                                                                                     if e["ours"]],
        source=source, terminal=bool(meta.get("terminal")), categories=meta.get("categories") or [],
        wmclass=meta.get("wmclass") or "", read=not meta.get("why"), why=meta.get("why") or "",
        executable=os.access(path, os.X_OK), writable=os.access(folder, os.W_OK) and os.access(path, os.W_OK),
        backup=backup_of(path), orphan=False, repo_alias="", installation="user")


def _orphan(entries):
    e0 = entries[0]
    path = e0["target"]
    return dict(
        id="appimage-orphan:" + e0["desktop"], kind="appimage", origin="appimage", origin_name="AppImage",
        name=e0["name"] or pretty_name(path), summary=e0["comment"], icon=e0["icon"], icon_file="", package=path,
        version=e0["version"], path=path, folder=os.path.dirname(path), size=0, mtime=0, type=0,
        menu=[e["desktop"] for e in entries], menu_ours=[e["desktop"] for e in entries if e["ours"]], source=None,
        terminal=False, categories=[], wmclass="", read=False, why="gone", executable=False, writable=False,
        backup="", orphan=True, repo_alias="", installation="user")


def scan(extra=(), download=None):
    """[app dict] — every AppImage found (kind and origin 'appimage', package = its path), and the menu entries that
    run an AppImage that is gone (orphan=True). Reads names and the first bytes of files only; what is inside each
    AppImage is read once and cached. Slow the first time (unsquashfs): call it in a thread."""
    found = {}  # real path → the path as it was found
    for folder, depth in folders(extra, download):
        for p in _files_in(folder, depth):
            real = os.path.realpath(p)
            if real not in found and appimage_type(p):
                found[real] = p
    by_target, gone = {}, {}
    for e in menu_entries():
        t = e["target"]
        if os.path.isfile(t) and appimage_type(t):
            real = os.path.realpath(t)
            found.setdefault(real, t)
            by_target.setdefault(real, []).append(e)
        elif not os.path.lexists(t) and t.lower().endswith(".appimage"):
            gone.setdefault(t, []).append(e)
    langs = _langs()
    out, used = [], set()
    for real, path in found.items():
        try:
            a = describe(path, by_target.get(real, []), langs)
        except OSError:
            continue
        out.append(a)
        try:
            used.add(_cache_key(path, os.stat(path)))
        except OSError:
            pass
    out += [_orphan(entries) for entries in gone.values()]
    _clean_cache(used)
    return sorted(out, key=lambda a: (a["orphan"], a["name"].lower()))


def _clean_cache(used):
    """Forget what was read from AppImages that changed or went away a month ago (only Zypp Desk's own cache)."""
    try:
        base = cache_dir()
        now = time.time()
        for d in base.iterdir():
            if d.name in used or d.is_symlink() or not d.is_dir():
                continue
            if now - d.stat().st_mtime > CACHE_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


# ------------------------------------------------------------------ opening

def terminal_command(path):
    """A terminal that runs a command-line AppImage and stays open, or [] when there is none."""
    for program, args in (("xdg-terminal-exec", []), ("konsole", ["--noclose", "-e"]),
                          ("gnome-terminal", ["--"]), ("xfce4-terminal", ["--hold", "-x"]),
                          ("xterm", ["-hold", "-e"])):
        found = shutil.which(program)
        if found:
            return [found] + args + [path]
    return []


def open_command(app):
    """The command that opens it: the file itself, or a terminal for a command-line tool."""
    if app.get("terminal"):
        words = terminal_command(app["path"])
        if words:
            return words
    return [app["path"]]


def make_executable(path):
    """chmod +x as the user: execute where it can be read."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    add = stat.S_IXUSR | (stat.S_IXGRP if mode & stat.S_IRGRP else 0) | (stat.S_IXOTH if mode & stat.S_IROTH else 0)
    os.chmod(path, mode | add)


def process_alive(pid):
    """True while the process started by "Open it" is still running (the old version can then go)."""
    try:
        return bool(pid) and os.path.exists(f"/proc/{int(pid)}")
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ new versions

_memo = {}


def _machine():
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def _arch_of(name):
    low = name.lower()
    for arch, words in _ARCH_WORDS.items():
        if any(re.search(rf"(?<![0-9a-z]){re.escape(w)}(?![0-9a-z])", low) for w in words):
            return arch
    return ""


def pick_asset(assets, pattern, local_name):
    """The AppImage of a release for this computer: the one the update information names (a pattern with *), else
    the one for this machine's architecture most like the file you have."""
    files = [a for a in assets if a.get("name", "").lower().endswith(".appimage")]
    if pattern:
        rx = re.compile(fnmatch.translate(pattern))
        named = [a for a in files if rx.match(a["name"])]
        if named:
            return named[0]
    mine = _machine()
    fitting = [a for a in files if _arch_of(a["name"]) in (mine, "")]
    if not fitting:
        return None
    stem = re.sub(r"\.appimage$", "", local_name, flags=re.I).lower()
    return max(fitting, key=lambda a: (_arch_of(a["name"]) == mine,
                                       difflib.SequenceMatcher(None, stem, a["name"].lower()).ratio()))


def _release(source):
    repo, tag = source["repo"], source.get("tag") or "latest"
    base = f"https://api.github.com/repos/{repo}/releases"
    if tag == "latest":
        return websources._get_json(base + "/latest")
    if tag in ("latest-pre", "latest-all"):
        rels = websources._get_json(base + "?per_page=10")
        rels = [r for r in rels if isinstance(r, dict) and not r.get("draft") and
                (tag == "latest-all" or r.get("prerelease"))]
        if not rels:
            raise ValueError(_("{repo} has no release yet.").format(repo=repo))
        return rels[0]
    return websources._get_json(base + "/tags/" + urllib.parse.quote(tag, safe=""))


def sha256_of(path):
    return websources.sha256_of(path)


def check(app, source, force=False):
    """Ask GitHub for the newest release of an AppImage (never downloads; only when the user asks).

    Returns {"version", "tag", "asset", "url", "size", "sha256", "page", "published", "newer", "same"}:
    'same' is True when GitHub's SHA-256 is the one of your file; 'newer' True or False when it is known, None when
    neither the versions nor the SHA-256 tell. GitHub's answers are kept ten minutes and asked with their ETag
    (websources), so asking again costs nothing."""
    if not source or source.get("kind") != "github":
        raise ValueError(_("This AppImage does not say where its new versions are published on GitHub."))
    key = json.dumps([source["repo"], source.get("tag"), source.get("pattern"), Path(app["path"]).name])
    memo = _memo.get(key)
    if memo and not force and time.monotonic() - memo[0] < websources.CHECK_TTL:
        rel = memo[1]
    else:
        rel = _release(source)
        _memo[key] = (time.monotonic(), rel)
    asset = pick_asset(rel.get("assets") or [], source.get("pattern") or "", Path(app["path"]).name)
    if not asset:
        raise ValueError(_("The latest release of {repo} has no AppImage for this computer.")
                         .format(repo=source["repo"]))
    tag = rel.get("tag_name") or ""
    m = _VERSION_RE.search(tag)
    version = m.group(1) if m else version_from_name(asset["name"])
    digest = asset.get("digest") or ""
    sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None
    url = asset.get("browser_download_url") or ""
    if not url.startswith("https://"):
        raise ValueError(_("Only https downloads are allowed."))
    found = {"version": version, "tag": tag, "asset": asset["name"], "url": url, "size": asset.get("size"),
             "sha256": sha, "page": rel.get("html_url") or f"https://github.com/{source['repo']}/releases",
             "published": (rel.get("published_at") or "")[:10], "same": False, "newer": None}
    if sha and os.path.isfile(app["path"]) and sha256_of(app["path"]) == sha:
        found["same"], found["newer"] = True, False
    elif version and app.get("version"):
        found["newer"] = vercmp(version, app["version"]) > 0
        if not found["newer"] and sha:
            found["newer"] = None  # same version number but another file: say so, the user decides
    elif sha:
        found["newer"] = None
    return found


def _open_url(url):
    """The download (https only, redirects included): websources' request, a function of its own for the tests."""
    return websources._request(url)


def download_new(app, found, progress=None):
    """Download the new version next to the old one as <file>.new, check it and make it executable.

    The SHA-256 is compared with GitHub's when GitHub gives it; the file must be an AppImage. Returns {"path",
    "sha256", "sha256_checked", "version"}. A file that fails a check, or half a download, is deleted (it was
    never anything but ours: <file>.new.part)."""
    old = Path(app["path"])
    folder = old.parent
    if not os.access(folder, os.W_OK) or not os.access(old, os.W_OK):
        raise ValueError(_("Only the administrator can change the folder {folder}: Zypp Desk does not update "
                           "AppImages there.").format(folder=display_path(folder)))
    size = int(found.get("size") or 0)
    if size and shutil.disk_usage(folder).free < size + (64 << 20):
        raise ValueError(_("There is not enough free space in {folder}.").format(folder=display_path(folder)))
    new = old.with_name(old.name + ".new")
    part = old.with_name(old.name + ".new.part")
    h = hashlib.sha256()
    done = 0
    part.unlink(missing_ok=True)  # one left by an earlier try (a link is removed, never followed)
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with _open_url(found["url"]) as r, os.fdopen(fd, "wb") as out:
            total = int(r.headers.get("Content-Length") or size or 0) if hasattr(r, "headers") else size
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > websources.MAX_DOWNLOAD:
                    raise ValueError(_("The file is bigger than 2 GiB: it was not downloaded."))
                if progress:
                    progress(done, total)
        digest = h.hexdigest()
        if found.get("sha256") and digest != found["sha256"]:
            raise ValueError(_("The SHA-256 does not match: GitHub publishes {expected}, the file has {got}. "
                               "The file was deleted.").format(expected=found["sha256"], got=digest))
        if not appimage_type(part):
            raise ValueError(_("The file downloaded is not an AppImage. It was deleted."))
        os.chmod(part, stat.S_IMODE(old.stat().st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        part.replace(new)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    meta = read_embedded(str(new))
    return {"path": str(new), "sha256": digest, "sha256_checked": bool(found.get("sha256")),
            "version": meta.get("version") or found.get("version") or ""}


def replace_with_new(path, new_path):
    """The new version takes the place of the old one, which stays as <file>.old (same folder, same name, so the
    menu entries still work). Returns the path of the old one. The page moves an earlier <file>.old to the trash
    first."""
    old = Path(path)
    backup = old.with_name(old.name + ".old")
    if os.path.lexists(backup):
        raise FileExistsError(str(backup))
    os.replace(old, backup)
    try:
        os.replace(new_path, old)
    except OSError:
        os.replace(backup, old)
        raise
    return str(backup)


def restore_backup(path):
    """Back to the version kept as <file>.old (the page moved the new one to the trash first)."""
    backup = Path(path + ".old")
    if os.path.lexists(path):
        raise FileExistsError(path)
    os.replace(backup, path)
    return path


# ------------------------------------------------------------------ adding one

def adopt(src, folder, move=True, add_to_menu=True):
    """"Add an AppImage…": move or copy the file into folder (nothing is done when it is already there), make it
    executable and put it in the menu. Returns its app dict. The page has checked that it is an AppImage and that
    no other file of the same name is in the way."""
    src = os.path.abspath(src)
    if not appimage_type(src):
        raise ValueError(_("{file} is not an AppImage.").format(file=Path(src).name))
    folder = Path(os.path.expanduser(folder))
    dest = folder / Path(src).name
    if os.path.dirname(src) == str(folder) or (dest.exists() and os.path.samefile(src, dest)):
        dest = Path(src)
    else:
        folder.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise FileExistsError(str(dest))
        if move:
            shutil.move(src, dest)
        else:
            part = dest.with_name("." + dest.name + ".part")
            try:
                shutil.copy2(src, part)
                part.replace(dest)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
    make_executable(dest)
    entries = [e for e in menu_entries() if os.path.realpath(e["target"]) == os.path.realpath(dest)]
    app = describe(str(dest), entries)
    if add_to_menu and not app["menu"]:
        write_menu_entry(app)
        app = describe(str(dest), [e for e in menu_entries()
                                   if os.path.realpath(e["target"]) == os.path.realpath(dest)])
    return app
