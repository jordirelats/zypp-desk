# SPDX-License-Identifier: GPL-3.0-or-later
"""Icons for packages and Flatpak apps.

- Packages: the AppStream catalogue the repositories publish (openSUSE keeps it in
  /var/cache/swcatalog: an XML per repository and the icons already cut to 64 and 128 px), and,
  for installed packages, the Icon= of the .desktop files they ship.
- Flatpak apps: their exported icon (the theme finds it by the app ID) or the Flathub catalogue.

Everything here only reads files; loading the catalogue happens once, in a thread.
"""

import gzip
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


def desktop_icons(names):
    """Icon= of the .desktop files shipped by these installed packages: name -> icon."""
    if not names:
        return {}
    p = subprocess.run(["rpm", "-q", "--qf", "[%{=NAME}\t%{FILENAMES}\n]", *names], capture_output=True, text=True,
                       env=ENV_C)
    out = {}
    for line in p.stdout.splitlines():
        name, _, path = line.partition("\t")
        if name in out or not path.startswith("/usr/share/applications/") or not path.endswith(".desktop"):
            continue
        try:
            for row in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
                if row.startswith("Icon="):
                    out[name] = row[5:].strip()
                    break
        except OSError:
            pass
    return out


def flatpak_icon_file(app_id):
    """The Flathub catalogue icon of an app that may not be installed."""
    for base in FLATPAK_APPSTREAM:
        for p in base.glob(f"*/*/active/icons/64x64/{app_id}.png"):
            return str(p)
    return None
