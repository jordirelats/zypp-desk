# SPDX-License-Identifier: GPL-3.0-or-later
"""Icons for packages and Flatpak apps.

- Packages: the AppStream catalogue the repositories publish (openSUSE keeps it in
  /var/cache/swcatalog: an XML per repository and the icons already cut to 64 and 128 px), and,
  for installed packages, the Icon= of the .desktop files they ship.
- Flatpak apps: their exported icon (the theme finds it by the app ID) or the Flathub catalogue.

Everything here only reads files; loading the catalogue happens once, in a thread.
"""

import gzip
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .backend import ENV_C

CATALOG_DIRS = (Path("/var/cache/swcatalog"), Path("/usr/share/swcatalog"), Path("/var/cache/app-info"))
FLATPAK_APPSTREAM = (Path("/var/lib/flatpak/appstream"), Path.home() / ".local/share/flatpak/appstream")


def load_catalog():
    """pkgname -> ('file', path) or ('theme', icon name)."""
    out = {}
    for base in CATALOG_DIRS:
        xml_dir = base / "xml"
        if not xml_dir.is_dir():
            continue
        for f in sorted(xml_dir.iterdir()):
            if not f.name.endswith((".xml", ".xml.gz")):
                continue
            try:
                _read_catalog_file(base, f, out)
            except (OSError, ET.ParseError):
                continue
    return out


def _read_catalog_file(base, f, out):
    opener = gzip.open if f.name.endswith(".gz") else open
    origin = None
    with opener(f, "rb") as fh:
        for event, el in ET.iterparse(fh, events=("start", "end")):
            if event == "start" and el.tag == "components":
                origin = el.get("origin")
                continue
            if event != "end" or el.tag != "component":
                continue
            pkg = el.findtext("pkgname")
            if pkg and pkg not in out:
                icon = _pick_icon(base, origin, el)
                if icon:
                    out[pkg] = icon
            el.clear()


def _pick_icon(base, origin, comp):
    cached = {}
    stock = None
    for ic in comp.findall("icon"):
        t = ic.get("type")
        if t == "cached" and ic.text:
            cached[ic.get("width") or "64"] = ic.text.strip()
        elif t == "stock" and ic.text:
            stock = ic.text.strip()
        elif t == "local" and ic.text and Path(ic.text).is_file():
            return ("file", ic.text.strip())
    for size in ("64", "128", "48"):
        name = cached.get(size)
        if name and origin:
            p = base / "icons" / origin / f"{size}x{size}" / name
            if p.is_file():
                return ("file", str(p))
    return ("theme", stock) if stock else None


def _in_theme(name):
    """The icon to use for this Icon= name: the name itself when an icon theme has it, the file when only
    /usr/share/pixmaps has it, or None. Files only: this runs in a worker thread, where Qt's icon loader must
    not be used."""
    import glob
    for pattern in (f"/usr/share/icons/*/*/apps/{name}.*", f"/usr/share/icons/*/apps/*/{name}.*",
                    os.path.expanduser(f"~/.local/share/icons/*/*/apps/{name}.*")):
        if glob.glob(pattern):
            return name
    found = sorted(glob.glob(f"/usr/share/pixmaps/{name}.*"))
    return found[0] if found else None


def _desktop_icon(path):
    try:
        for row in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if row.startswith("Icon="):
                return row[5:].strip()
    except OSError:
        pass
    return None


def desktop_icons(names):
    """The icon of each of these installed packages: name -> an icon name of the theme or the path of a file.

    First the Icon= of a .desktop file the package ships (in any …/applications folder: programs from the web often
    use /usr/local/share), or one named after the package that its scripts wrote (rustdesk.desktop); when the icon
    name is not in the theme, a file of the package with that name; and for a package with no .desktop file at
    all, an icon file named after it (Weasis keeps /opt/weasis/lib/Weasis.png) — Jordi, 26/11/2026: "falten icones
    als rpm web"."""
    if not names:
        return {}
    p = subprocess.run(["rpm", "-q", "--qf", "[%{=NAME}\t%{FILENAMES}\n]", *names], capture_output=True, text=True,
                       env=ENV_C)
    files = {}
    for line in p.stdout.splitlines():
        name, _, path = line.partition("\t")
        if path:
            files.setdefault(name, []).append(path)
    out = {}
    for name in names:
        paths = files.get(name, [])
        desktops = [f for f in paths if "/applications/" in f and f.endswith(".desktop")]
        for extra in (f"/usr/share/applications/{name}.desktop", f"/usr/local/share/applications/{name}.desktop"):
            if extra not in desktops and Path(extra).is_file():
                desktops.append(extra)
        images = [f for f in paths if f.lower().endswith((".svg", ".png", ".xpm")) and "/doc/" not in f]
        found = None
        for d in desktops:
            ic = _desktop_icon(d)
            if not ic:
                continue
            usable = ic if ic.startswith("/") else _in_theme(ic)
            if usable:
                found = usable
                break
            # a name the theme does not know: a file of the package with that name
            match = [f for f in images if Path(f).stem.lower() == ic.lower()]
            if match:
                found = sorted(match, key=lambda f: (not f.endswith(".svg"), len(f)))[0]
                break
        if not found:
            match = [f for f in images if Path(f).stem.lower() == name.lower()
                     or Path(f).stem.lower().replace("-", "") == name.lower().replace("-", "")]
            if match:
                found = sorted(match, key=lambda f: (not f.endswith(".svg"), "/hicolor/" not in f, len(f)))[0]
        if found:
            out[name] = found
    return out


def flatpak_icon_file(app_id):
    """The Flathub catalogue icon of an app that may not be installed."""
    for base in FLATPAK_APPSTREAM:
        for p in base.glob(f"*/*/active/icons/64x64/{app_id}.png"):
            return str(p)
    return None
