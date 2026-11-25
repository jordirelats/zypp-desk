# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the XML of a '--dry-run' and show what would change, without changing anything."""

import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .i18n import _, ngettext

PROMPT_CONTINUE, PROMPT_DEP_RESOLVE, PROMPT_LICENSE = 0, 1, 10

CATEGORIES = [
    ("to-install", _("Will be installed")),
    ("to-upgrade", _("Will be upgraded")),
    ("to-downgrade", _("Will be downgraded")),
    ("to-reinstall", _("Will be reinstalled")),
    ("to-change-vendor", _("Will change vendor")),
    ("to-change-arch", _("Will change architecture")),
    ("to-remove", _("Will be removed")),
]


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    sign = "-" if n < 0 else ""
    n = abs(n)
    from PySide6.QtCore import QLocale
    loc = QLocale()
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{sign}{loc.toString(n, 'f', 1)} {unit}" if unit != "B" else f"{sign}{int(n)} B"
        n /= 1024


def parse_dry_run(text):
    """Return dict(groups, download, disk, messages, problems, ok)."""
    m = re.search(r"<stream>.*</stream>", text, re.S)
    res = dict(groups={}, download=None, disk=None, messages=[], problems=[], ok=False, raw=text)
    if not m:
        res["problems"].append(text.strip()[-3000:] or _("zypper returned no output."))
        return res
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError as e:
        res["problems"].append(str(e))
        return res
    for msg in root.iter("message"):
        kind, t = msg.get("type", "info"), (msg.text or "").strip()
        if not t:
            continue
        res["messages"].append((kind, t))
        if kind == "error":
            res["problems"].append(t)
    # Prompt ids come from 'zypper --promptids'. 0 is the ordinary "Continue?" before committing,
    # which a dry run always shows: it is not a problem.
    for p in root.iter("prompt"):
        try:
            pid = int(p.get("id", "-1"))
        except ValueError:
            pid = -1
        if pid == PROMPT_CONTINUE:
            continue
        desc = " ".join((x.text or "").strip() for x in p.iter("text")) or (p.text or "").strip()
        opts = [o.get("desc") or o.get("value") or "" for o in p.iter("option")]
        if pid == PROMPT_DEP_RESOLVE:
            detail = [t for k, t in res["messages"] if re.match(r"^\s*(Problem|Solution|\d+:)", t)]
            res["problems"].append("\n".join(detail + [desc]).strip() or desc)
        elif pid == PROMPT_LICENSE:
            res["problems"].append(_("A license must be accepted. Turn on “Accept licenses without asking” in "
                                     "Settings, or install it from a terminal.") + "\n" + desc)
        elif desc:
            res["problems"].append(desc + (("\n  " + "\n  ".join(o for o in opts if o)) if opts else ""))
    for s in root.iter("solverproblem") if hasattr(root, "iter") else []:
        res["problems"].append(ET.tostring(s, encoding="unicode", method="text").strip())
    summ = next(root.iter("install-summary"), None)
    if summ is not None:
        res["download"] = summ.get("download-size")
        res["disk"] = summ.get("space-usage-diff")
        for child in summ:
            items = []
            for s in child.iter("solvable"):
                items.append(dict(name=s.get("name", ""), edition=s.get("edition", ""),
                                  old=s.get("edition-old", ""), arch=s.get("arch", ""),
                                  repo=s.get("repository", ""), kind=s.get("type", "package"),
                                  vendor=s.get("vendor", ""), old_vendor=s.get("vendor-old", "")))
            if items:
                res["groups"].setdefault(child.tag, []).extend(items)
        res["ok"] = not res["problems"]
    elif not res["problems"]:
        res["ok"] = True  # nothing to do
    return res


def summary_lines(result):
    """A plain-text summary of a dry run, for the terminal panel."""
    lines = []
    for key, label in CATEGORIES:
        items = result["groups"].get(key, [])
        if items:
            names = ", ".join(f"{i['name']} {i['edition']}" for i in items[:40])
            more = "" if len(items) <= 40 else " …"
            lines.append(f"{label} ({len(items)}): {names}{more}")
    if result["download"]:
        lines.append(_("Download: {size}").format(size=human_size(result["download"])))
    if result["disk"]:
        lines.append(_("Disk: {size}").format(size=human_size(result["disk"])))
    for p in result["problems"]:
        lines.append(_("Problem: {text}").format(text=p))
    if not lines:
        lines.append(_("Nothing to do."))
    return lines


class SimulationDialog(QDialog):
    """What a dry-run says would happen. 'Apply' runs the real command."""

    def __init__(self, result, command, *, can_apply=True, extra_steps=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Simulation"))
        self.resize(900, 640)
        lay = QVBoxLayout(self)

        banner = QFrame(self)
        banner.setObjectName("banner")
        ok = result["ok"]
        color = "39,174,96" if ok else "246,116,0"
        banner.setStyleSheet(f"#banner {{ border: 1px solid rgba({color},0.55); border-radius: 5px;"
                             f" background: rgba({color},0.09); }}")
        bl = QVBoxLayout(banner)
        title = QLabel(_("It can be done. Nothing has been changed.") if ok
                       else _("zypper found a problem. Nothing has been changed."), banner)
        f = title.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        title.setFont(f)
        bl.addWidget(title)
        counts = []
        for key, label in CATEGORIES:
            n = len(result["groups"].get(key, []))
            if n:
                counts.append(f"{label}: {n}")
        if result["download"]:
            counts.append(_("Download: {size}").format(size=human_size(result["download"])))
        if result["disk"]:
            counts.append(_("Disk: {size}").format(size=human_size(result["disk"])))
        if not counts and ok:
            counts.append(_("Nothing to do."))
        bl.addWidget(QLabel("  ·  ".join(counts), banner))
        cmd = QLabel(command, banner)
        cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        cmd.setFont(mono)
        cmd.setWordWrap(True)
        bl.addWidget(cmd)
        lay.addWidget(banner)

        if result["problems"]:
            lay.addWidget(QLabel("<b>" + _("Problems") + "</b>", self))
            box = QPlainTextEdit("\n\n".join(result["problems"]), self)
            box.setReadOnly(True)
            box.setMaximumHeight(200)
            lay.addWidget(box)
            hint = QLabel(_("Change the queue (for example, lock or remove a package) and simulate again. "
                            "Choosing among zypper's proposed solutions inside the window will come in a "
                            "later version."), self)
            hint.setWordWrap(True)
            lay.addWidget(hint)

        tree = QTreeWidget(self)
        tree.setHeaderLabels([_("Package"), _("Version"), _("Repository")])
        tree.setRootIsDecorated(True)
        for key, label in CATEGORIES + [(k, k) for k in result["groups"] if k not in dict(CATEGORIES)]:
            items = result["groups"].get(key)
            if not items:
                continue
            top = QTreeWidgetItem(tree, [f"{label} ({len(items)})"])
            ft = top.font(0)
            ft.setBold(True)
            top.setFont(0, ft)
            for it in items:
                ver = f"{it['old']} → {it['edition']}" if it["old"] else it["edition"]
                if it.get("old_vendor") and it.get("vendor"):
                    ver += f"  ({it['old_vendor']} → {it['vendor']})"
                QTreeWidgetItem(top, [it["name"], ver, it["repo"]])
            top.setExpanded(True)
        tree.resizeColumnToContents(0)
        lay.addWidget(tree, 1)

        if extra_steps:
            lbl = QLabel(_("These commands are also in the queue. zypper cannot simulate them; they will run "
                           "before the packages:") + "\n" + "\n".join(s.display() for s in extra_steps), self)
            lbl.setWordWrap(True)
            lay.addWidget(lbl)

        buttons = QDialogButtonBox(self)
        self.apply_btn = buttons.addButton(_("Apply"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_btn.setEnabled(can_apply and ok)
        buttons.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
