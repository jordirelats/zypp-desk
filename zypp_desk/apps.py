# SPDX-License-Identifier: GPL-3.0-or-later
"""Applications: the programs installed on the computer and where each one comes from, and the
catalogues of programs to discover by theme.

Everything here only reads (files, rpm, zypper and flatpak queries) and is meant to run in a thread.

- Installed RPM applications are the visible .desktop files in /usr/share/applications; rpm says
  which package owns each one, and 'zypper search --installed-only --details' which repository the
  installed version belongs to. A package from a local folder repository (plaindir, dir:) or from
  no repository at all came from a file downloaded from the web.
- Installed Flatpak apps come from 'flatpak list'.
- The catalogues are the AppStream files that openSUSE keeps for its repositories
  (/var/cache/swcatalog) and that Flatpak keeps for each remote (Flathub).
"""

import configparser
import gzip
import os
import re
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import appicons, backend, flatpak, websources
from .i18n import user_lang

DESKTOP_DIRS = (Path.home() / ".local/share/applications", Path("/usr/local/share/applications"),
                Path("/usr/share/applications"))
# web apps made by a browser ("install this site"): they belong to the browser package, they are not programs
WEB_APP_RE = re.compile(r"--app-id=|--app=|--profile-directory=")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
MAIN_CATEGORIES = ("AudioVideo", "Development", "Education", "Game", "Graphics", "Network", "Office", "Science",
                   "Settings", "System", "Utility")
# Categories that only say which toolkit or desktop an app uses: not a theme.
# the desktops the menu shows entries for (KDE on Plasma; GNOME, XFCE… elsewhere)
CURRENT_DESKTOPS = {d for d in os.environ.get("XDG_CURRENT_DESKTOP", "KDE").split(":") if d} or {"KDE"}
FLATPAK_ARCH = {"aarch64": "aarch64", "arm64": "aarch64", "i686": "i386", "i386": "i386"}.get(os.uname().machine,
                                                                                            os.uname().machine)
NOT_THEMES = {"Qt", "KDE", "GTK", "GNOME", "XFCE", "LXQt", "Java", "Motif", "ConsoleOnly", "Application",
              "X-SuSE-Core-Office", "Core"}
FLATPAK_APPSTREAM_DIRS = (Path("/var/lib/flatpak/appstream"), Path.home() / ".local/share/flatpak/appstream")


def lang_keys():
    """'ca_ES.UTF-8' → ['ca_ES', 'ca'], the keys to look for in .desktop and AppStream files."""
    lang = user_lang().split(".")[0].split("@")[0]
    if lang in ("C", "POSIX", ""):
        return []
    keys = [lang]
    if "_" in lang:
        keys.append(lang.split("_")[0])
    return keys


# ------------------------------------------------------------------ installed applications

def _desktop_entry(path, langs):
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # keep Name[ca] as it is
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return None
    if not cp.has_section("Desktop Entry"):
        return None
    e = cp["Desktop Entry"]
    if e.get("Type", "") != "Application" or not e.get("Exec"):
        return None
    if e.get("NoDisplay", "").lower() == "true" or e.get("Hidden", "").lower() == "true":
        return None
    only = [x for x in e.get("OnlyShowIn", "").split(";") if x]
    if only and not CURRENT_DESKTOPS & set(only):
        return None
    if CURRENT_DESKTOPS & set(e.get("NotShowIn", "").split(";")):
        return None

    def loc(key):
        for lg in langs:
            v = e.get(f"{key}[{lg}]")
            if v:
                return v
        return e.get(key, "")

    return {"name": loc("Name"), "summary": loc("Comment") or loc("GenericName"), "icon": e.get("Icon", ""),
            "categories": [c for c in e.get("Categories", "").split(";") if c], "desktop": path.name,
            "exec": e.get("Exec", "")}


def _exec_path(exec_line):
    """The program a .desktop file starts, as written and with links resolved."""
    try:
        words = shlex.split(exec_line)
    except ValueError:
        return None
    while words and (words[0] == "env" or "=" in words[0]):
        words.pop(0)
    if not words:
        return None
    prog = words[0] if words[0].startswith("/") else shutil.which(words[0])
    if not prog:
        return None
    return prog, os.path.realpath(prog)


def _owners(files):
    """.desktop file → (package, version-release): two rpm calls that cannot get out of step."""
    if not files:
        return {}
    p = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\n", *map(str, files)], capture_output=True, text=True,
                       env=backend.ENV_C)
    # one name per line; "file … is not owned by any package" lines have spaces
    names = sorted({n.strip() for n in p.stdout.splitlines() if n.strip() and " " not in n.strip()})
    if not names:
        return {}
    p = subprocess.run(["rpm", "-q", "--qf", "[%{=NAME}\t%{=VERSION}-%{=RELEASE}\t%{FILENAMES}\n]", *names],
                       capture_output=True, text=True, env=backend.ENV_C)
    wanted = {str(f) for f in files}
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] in wanted:
            out.setdefault(parts[2], (parts[0], parts[1]))
    return out


def _installed_repos(repo_names):
    """Installed package name → name of the repository its installed version belongs to.

    A package whose installed version no repository carries any more shows as "(System Packages)": on
    Tumbleweed that happens to every package between refreshing a new snapshot and running dup. Those
    take the repository that carries another version of them, if there is one."""
    root, _code = backend.xml(["search", "--installed-only", "--details", "-t", "package"])
    out = {}
    for s in root.iter("solvable"):
        if s.get("status") == "installed":
            out.setdefault(s.get("name"), s.get("repository", ""))
    orphans = sorted(n for n, r in out.items() if r not in repo_names)
    for i in range(0, len(orphans), 200):
        try:
            root, _code = backend.xml(["search", "--details", "--match-exact", "-t", "package", "--", *orphans[i:i + 200]])
        except backend.ZypperError:
            continue
        for s in root.iter("solvable"):
            name, repo = s.get("name"), s.get("repository", "")
            if name in out and out[name] not in repo_names and repo in repo_names:
                out[name] = repo
    return out


def installed():
    """Every installed application, RPM and Flatpak, with where it comes from.

    Returns a list of dicts: id, kind ('rpm'|'flatpak'), name, summary, icon (theme name or path),
    package (rpm name or Flatpak app ID), version, origin ('repo'|'flatpak'|'web'), origin_name
    (repository name, remote, or the web source), repo_alias, installation, categories.
    """
    langs = lang_keys()
    by_name = {}
    for d in DESKTOP_DIRS:  # the first folder wins: the user's copy hides the system one, as in the menu
        if d.is_dir():
            for f in sorted(d.glob("*.desktop")):
                by_name.setdefault(f.name, f)
    entries = {}
    for f in by_name.values():
        e = _desktop_entry(f, langs)
        if e and not WEB_APP_RE.search(e["exec"]):
            entries[str(f)] = e
    owners = _owners(list(entries))
    # .desktop files copied by an install script (RustDesk, Weasis…) belong to no package: ask for the program
    execs = {}
    for path, e in entries.items():
        if path not in owners:
            prog = _exec_path(e["exec"])
            if prog:
                execs[path] = prog
    prog_owners = _owners(sorted({p for pair in execs.values() for p in pair}))
    for path, pair in execs.items():
        owner = prog_owners.get(pair[0]) or prog_owners.get(pair[1])
        if owner:
            owners[path] = owner
    repos = {r["name"]: r for r in backend.repos()}
    pkg_repo = _installed_repos(set(repos))
    followed = {s["package"]: s for s in websources.load()}
    out = []
    for path, e in entries.items():
        if path not in owners:
            continue  # a .desktop file no package owns (made by hand, an AppImage…)
        pkg, ver = owners[path]
        repo_name = pkg_repo.get(pkg, "")
        r = repos.get(repo_name)
        src = None
        if r is None or r["type"] == "plaindir" or r["url"].startswith("dir:"):
            origin = "web"
            src = followed.get(pkg)
            origin_name = websources.origin_text(src) if src and src.get("type", "github") != "manual" else ""
        else:
            origin, origin_name = "repo", r["name"]
        out.append(dict(id=e["desktop"], kind="rpm", name=e["name"] or pkg, summary=e["summary"], icon=e["icon"],
                        package=pkg, version=ver, origin=origin, origin_name=origin_name,
                        repo_alias=r["alias"] if r else "", installation="", categories=e["categories"],
                        web_kind=src.get("type", "github") if src else "", web_note=src.get("note", "") if src else ""))
    if flatpak.available():
        for a in flatpak.installed():
            out.append(dict(id=a["application"], kind="flatpak", name=a["name"] or a["application"], summary="",
                            icon=a["application"], package=a["application"], version=a["version"],
                            origin="flatpak", origin_name=a["origin"], repo_alias="",
                            installation=a["installation"], categories=[], web_kind="", web_note=""))
        _flatpak_summaries([a for a in out if a["kind"] == "flatpak"], langs)
    out.sort(key=lambda a: a["name"].lower())
    return out


def _flatpak_summaries(apps, langs):
    """Name and summary of installed Flatpak apps from the remote's catalogue, in the user's language."""
    if not apps:
        return
    want = {a["package"]: a for a in apps}
    for remote, path, _icons in _flatpak_catalogues():
        try:
            for comp in _components(path):
                app_id = _flatpak_id(comp)
                a = want.get(app_id)
                if a and not a["summary"]:
                    a["summary"] = _loc_text(comp, "summary", langs)
                    a["name"] = _loc_text(comp, "name", langs) or a["name"]
                    a["categories"] = [c.text for c in comp.findall("categories/category") if c.text]
        except (OSError, ET.ParseError, EOFError):
            continue


def updates():
    """What can be updated: {'rpm': {name: new edition}, 'flatpak': {app ID: version}}."""
    rpm = {}
    try:
        rpm = {u["name"]: u["edition"] for u in backend.list_updates()}
    except backend.ZypperError:
        pass
    fp = {}
    if flatpak.available():
        fp = {u["application"]: u["version"] for u in flatpak.updates()}
    return {"rpm": rpm, "flatpak": fp}


def web_updates():
    """Programs from the web with a newer version where Web sources looks: {package: version}."""
    out = {}
    for src in websources.load():
        if src.get("type", "github") == "manual":
            continue
        try:
            found = websources.check(src)
        except Exception:  # a site that does not answer only means no news here; Web sources shows the error
            continue
        if found.get("newer") and found.get("version"):
            out[src["package"]] = found["version"]
    return out


# ------------------------------------------------------------------ catalogues (discover by theme)

def _components(path):
    """AppStream components of a catalogue file, one by one, cleared after use."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as fh:
        origin = None
        for event, el in ET.iterparse(fh, events=("start", "end")):
            if event == "start":
                if el.tag == "components":
                    origin = el.get("origin")
                continue
            if el.tag == "component":
                el.set("_origin", origin or "")
                yield el
                el.clear()


def _loc_text(comp, tag, langs):
    default = ""
    found = {}
    for el in comp.findall(tag):
        lg = el.get(XML_LANG)
        if lg is None:
            default = (el.text or "").strip()
        else:
            found[lg] = (el.text or "").strip()
    for lg in langs:
        if found.get(lg):
            return found[lg]
    return default


def _flatpak_id(comp):
    b = comp.find("bundle[@type='flatpak']")
    if b is not None and b.text and b.text.startswith("app/"):
        return b.text.split("/")[1]
    cid = comp.findtext("id") or ""
    return cid[:-8] if cid.endswith(".desktop") else cid


def _flatpak_catalogues():
    """(remote, catalogue file, icons folder) for every Flatpak remote with a catalogue on disk."""
    seen = set()
    for base in FLATPAK_APPSTREAM_DIRS:
        if not base.is_dir():
            continue
        for remote_dir in sorted(base.iterdir()):
            active = remote_dir / FLATPAK_ARCH / "active"
            for name in ("appstream.xml.gz", "appstream.xml"):
                f = active / name
                if f.is_file() and remote_dir.name not in seen:
                    seen.add(remote_dir.name)
                    yield remote_dir.name, f, active / "icons"
                    break


def _flatpak_icon(comp, icons_dir):
    for ic in comp.findall("icon"):
        if ic.get("type") == "cached" and ic.text:
            for size in ("64x64", "128x128"):
                p = icons_dir / size / ic.text.strip()
                if p.is_file():
                    return str(p)
    return ""


def catalogue():
    """Programs to discover: openSUSE repositories and Flatpak remotes.

    Returns a list of dicts: id, kind ('rpm'|'flatpak'), name, summary, icon (path or theme name),
    package (rpm name or app ID), remote, categories, origin_name.
    """
    langs = lang_keys()
    out = []
    seen_rpm = set()
    for base in appicons.CATALOG_DIRS:
        xml_dir = base / "xml"
        if not xml_dir.is_dir():
            continue
        for f in sorted(xml_dir.iterdir()):
            if not f.name.endswith((".xml", ".xml.gz")):
                continue
            try:
                for comp in _components(f):
                    if comp.get("type") not in ("desktop", "desktop-application"):
                        continue
                    pkg = comp.findtext("pkgname")
                    if not pkg or pkg in seen_rpm:
                        continue
                    seen_rpm.add(pkg)
                    ic = appicons._pick_icon(base, comp.get("_origin"), comp)
                    out.append(dict(id=comp.findtext("id") or pkg, kind="rpm", name=_loc_text(comp, "name", langs) or pkg,
                                    summary=_loc_text(comp, "summary", langs),
                                    icon=ic[1] if ic else "", package=pkg, remote="",
                                    categories=[c.text for c in comp.findall("categories/category") if c.text],
                                    origin_name="openSUSE"))
            except (OSError, ET.ParseError, EOFError):
                continue
    for remote, f, icons_dir in _flatpak_catalogues():
        seen = set()
        try:
            for comp in _components(f):
                if comp.get("type") not in ("desktop", "desktop-application"):
                    continue
                app_id = _flatpak_id(comp)
                if not app_id or app_id in seen:
                    continue
                seen.add(app_id)
                out.append(dict(id=app_id, kind="flatpak", name=_loc_text(comp, "name", langs) or app_id,
                                summary=_loc_text(comp, "summary", langs), icon=_flatpak_icon(comp, icons_dir),
                                package=app_id, remote=remote,
                                categories=[c.text for c in comp.findall("categories/category") if c.text],
                                origin_name=remote.capitalize() if remote == "flathub" else remote))
        except (OSError, ET.ParseError, EOFError):
            continue
    out.sort(key=lambda a: (a["name"].lower(), a["kind"] != "rpm"))
    return out


def installed_names():
    """Installed RPM package names and installed Flatpak app IDs, to mark the catalogue."""
    p = subprocess.run(["rpm", "-qa", "--qf", "%{NAME}\n"], capture_output=True, text=True, env=backend.ENV_C)
    rpms = set(p.stdout.split())
    fps = {a["application"] for a in flatpak.installed()} if flatpak.available() else set()
    return rpms, fps


def main_categories(app):
    return [c for c in app["categories"] if c in MAIN_CATEGORIES]


def sub_categories(app):
    return [c for c in app["categories"] if c not in MAIN_CATEGORIES and c not in NOT_THEMES
            and not c.startswith("X-")]
