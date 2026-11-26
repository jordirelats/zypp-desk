# SPDX-License-Identifier: GPL-3.0-or-later
"""Web apps (PWA) that browsers install (mockup pwa-v1-A-aplicacions-web.png).

Chromium-based browsers (Chrome, Chromium, Edge, Brave, Vivaldi, Opera) put a .desktop file for each web app in
the user's applications folder, named <browser>-<app id>-<profile>.desktop, whose Exec starts the browser with
--app-id=<id> (and --profile-directory=<profile>). Firefox has none of its own; the PWAsForFirefox extension
writes FFPWA-<id>.desktop files that run `firefoxpwa site launch <id>`. The browser installs, updates and removes
them: Zypp Desk lists them, opens them, opens the browser's own page to remove one, and removes the menu entries
of web apps whose browser is gone (orphans). Nothing here needs root. No Qt.
"""

import configparser
import os
import re
import shlex
import shutil
from pathlib import Path
from urllib.parse import urlparse

# the start of the .desktop file name → (browser id, name shown, colour, the page where it manages its apps)
BROWSERS = {
    "chrome": ("chrome", "Chrome", "#1a73e8", "chrome://apps"),
    "chromium": ("chromium", "Chromium", "#4285f4", "chrome://apps"),
    "msedge": ("edge", "Edge", "#0c59a4", "edge://apps"),
    "brave": ("brave", "Brave", "#fb542b", "brave://apps"),
    "vivaldi": ("vivaldi", "Vivaldi", "#ef3939", "vivaldi://apps"),
    "opera": ("opera", "Opera", "#ff1b2d", "opera://apps"),
}
_NAME_RE = re.compile(r"^(chrome|chromium|msedge|brave|vivaldi|opera)-([a-p]{32})-(.+)\.desktop$")
_FFPWA_RE = re.compile(r"^FFPWA-([0-9A-Z]{26})\.desktop$")


def applications_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications"


def _read(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    return cp


def _exec_words(text):
    try:
        return shlex.split(re.sub(r"%[a-zA-Z]", "", text or ""))
    except ValueError:
        return (text or "").split()


def _program_present(word):
    if not word:
        return False
    if os.path.isabs(word):
        return os.path.isfile(word) and os.access(word, os.X_OK)
    return shutil.which(word) is not None


def scan(folder=None):
    """[dict(id, kind='webapp', name, summary, icon, package, version, origin='webapp', origin_name (browser
    name), browser, color, profile, app_id, program (the browser's launcher), actions [names], host, desktop
    (path), orphan, manage (the browser page))] — every web app in the user's applications folder."""
    folder = Path(folder) if folder else applications_dir()
    out = []
    try:
        files = sorted(folder.glob("*.desktop"))
    except OSError:
        return out
    for f in files:
        m = _NAME_RE.match(f.name)
        ff = _FFPWA_RE.match(f.name)
        if not (m or ff):
            continue
        cp = _read(f)
        if cp is None or not cp.has_section("Desktop Entry"):
            continue
        e = cp["Desktop Entry"]
        words = _exec_words(e.get("Exec", ""))
        program = words[0] if words else ""
        if m:
            browser_key, app_id, profile = m.group(1), m.group(2), m.group(3)
            bid, bname, color, manage = BROWSERS[browser_key]
            pdir = next((w.split("=", 1)[1] for w in words if w.startswith("--profile-directory=")), profile)
        else:
            bid, bname, color, manage = "firefoxpwa", "Firefox (PWAsForFirefox)", "#ff7139", ""
            app_id, pdir = ff.group(1), ""
        actions, urls = [], []
        for name in (e.get("Actions", "") or "").split(";"):
            sec = f"Desktop Action {name}" if name else ""
            if sec and cp.has_section(sec):
                actions.append(cp[sec].get("Name", name))
                for w in _exec_words(cp[sec].get("Exec", "")):
                    for part in w.split("=", 1)[-1:]:
                        if part.startswith(("http://", "https://")):
                            urls.append(part)
        host = urlparse(urls[0]).netloc if urls else ""
        out.append(dict(id=f.name, kind="webapp", name=e.get("Name", app_id), summary=e.get("Comment", "") or "",
                        icon=e.get("Icon", "") or "", package=app_id, version="", origin="webapp",
                        origin_name=bname, browser=bid, color=color, profile=pdir, app_id=app_id, program=program,
                        actions=actions, host=host, desktop=str(f), orphan=not _program_present(program),
                        manage=manage, categories=[], repo_alias="", installation="user"))
    return out


def open_command(app):
    """The command that opens the web app, as its menu entry does."""
    cp = _read(app["desktop"])
    words = _exec_words(cp["Desktop Entry"].get("Exec", "")) if cp is not None else []
    return words


def manage_command(app):
    """The browser opened on its own page of apps, where it removes one (removing only the menu entry would not
    do: the browser writes it again)."""
    if not app.get("manage") or not app.get("program"):
        return []
    words = [app["program"]]
    if app.get("profile"):
        words.append(f"--profile-directory={app['profile']}")
    return words + [app["manage"]]


def leftovers(app):
    """The files a web app of a browser that is gone left: its menu entry and its icons."""
    files = [app["desktop"]]
    stem = Path(app["desktop"]).stem
    base = Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")) / "icons"
    if base.is_dir():
        for p in base.glob(f"*/*/apps/{stem}.*"):
            files.append(str(p))
    return files
