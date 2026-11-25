# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the XML of a '--dry-run' and show what would change, without changing anything."""

import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .i18n import _, ngettext

PROMPT_CONTINUE, PROMPT_DEP_RESOLVE, PROMPT_LICENSE = 0, 1, 10

CATEGORIES = [
    ("to-install", _("To install")),
    ("to-upgrade", _("To upgrade")),
    ("to-downgrade", _("To downgrade")),
    ("to-reinstall", _("To reinstall")),
    ("to-change-vendor", _("To change vendor")),
    ("to-change-arch", _("To change architecture")),
    ("to-remove", _("To remove")),
]


def human_size(n):
    """A size in the user's own format (QLocale: '4,4 MiB' in Catalan)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    sign = "-" if n < 0 else ""
    return sign + QLocale().formattedDataSize(int(abs(n)))


def disk_text(value):
    """What a transaction does to the disk, in words: frees space or uses more."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return _("Frees {size} on disk").format(size=human_size(-n))
    return _("Uses {size} more on disk").format(size=human_size(n))


def cancelled(steps):
    """True when the password was refused or cancelled (pkexec 126/127): there is nothing to show."""
    return any(getattr(s, "code", None) in (126, 127) for s in steps)


# Lines of zypper's dependency solver (the dry run runs in English) said in plain words.
_SOLVER = [
    (r"nothing provides '?([^'\s]+)'? needed by the to be installed (\S+)",
     lambda m: _("{pkg} needs {what}, and no repository provides it").format(pkg=m[2], what=m[1])),
    (r"nothing provides '?([^'\s]+)'? needed by the installed (\S+)",
     lambda m: _("the installed {pkg} needs {what}, and no repository provides it").format(pkg=m[2], what=m[1])),
    (r"the to be installed (\S+) conflicts with '?([^'\s]+)'? provided by the installed (\S+)",
     lambda m: _("{new} cannot be installed together with the installed {old}").format(new=m[1], old=m[3])),
    (r"the installed (\S+) conflicts with '?([^'\s]+)'? provided by the to be installed (\S+)",
     lambda m: _("{new} cannot be installed together with the installed {old}").format(new=m[3], old=m[1])),
    (r"(\S+) requires (.+), but this requirement cannot be provided",
     lambda m: _("{pkg} needs {what}, which cannot be installed").format(pkg=m[1], what=m[2])),
    (r"break (\S+) by ignoring some of its dependencies",
     lambda m: _("install {pkg} anyway, ignoring some of its dependencies (it may not work)").format(pkg=m[1])),
    (r"deinstallation of (\S+)", lambda m: _("remove {pkg}").format(pkg=m[1])),
    (r"do not keep (\S+) installed", lambda m: _("remove {pkg}").format(pkg=m[1])),
    (r"do not install (\S+)", lambda m: _("do not install {pkg}").format(pkg=m[1])),
    (r"keep obsolete (\S+)", lambda m: _("keep the old {pkg}").format(pkg=m[1])),
    (r"downgrade of (\S+) to (\S+)", lambda m: _("go back from {old} to {new}").format(old=m[1], new=m[2])),
]
_PROMPT_LINE = re.compile(r"Choose from above solutions|\[\d+(/\d+)*/[a-z?/]+\]\s*\(\w\)", re.I)


def plain_problem(text):
    """A solver problem with zypper's question removed and its lines said in plain words."""
    out = []
    for line in text.splitlines():
        if _PROMPT_LINE.search(line):
            continue  # zypper's own question (which solution?) cannot be answered here
        s = line.strip()
        for rx, fn in _SOLVER:
            s = re.sub(rx, fn, s)
        m = re.match(r"^Problem:\s*(\d+):\s*(.*)$", s)
        if m:
            s = _("Problem {n}: {text}").format(n=m[1], text=m[2])
        elif (m := re.match(r"^Problem:\s*(.*)$", s)):
            s = _("Problem: {text}").format(text=m[1])
        m = re.match(r"^Solution\s+(\d+):\s*(.*)$", s)
        if m:
            s = "  " + _("Possible solution {n}: {text}").format(n=m[1], text=m[2])
        elif s and line[:1].isspace() and out:
            s = "    " + s
        if s:
            out.append(s)
    return "\n".join(out)


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
        # zypper puts the problem and its numbered solutions in <description>; <text> is only the question
        question = " ".join((x.text or "").strip() for x in p.iter("text")) or (p.text or "").strip()
        description = (p.findtext("description") or "").strip()
        desc = "\n".join(x for x in (description, question) if x)
        opts = [o.get("desc") or o.get("value") or "" for o in p.iter("option")]
        if pid == PROMPT_DEP_RESOLVE:
            detail = [t for k, t in res["messages"] if re.match(r"^\s*(Problem|Solution|\d+:)", t)]
            res["problems"].append(plain_problem("\n".join(detail + [description]).strip() or desc))
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


def merge_results(results):
    """One result for several dry runs of the same queue (removals and installs run as separate steps)."""
    if len(results) == 1:
        return results[0]
    out = dict(groups={}, download=None, disk=None, messages=[], problems=[], ok=True,
               raw="\n".join(r["raw"] for r in results))

    def add(a, b):
        try:
            return str(int(a or 0) + int(b or 0))
        except ValueError:
            return a or b
    for r in results:
        for k, items in r["groups"].items():
            out["groups"].setdefault(k, []).extend(items)
        out["messages"] += r["messages"]
        out["problems"] += r["problems"]
        out["ok"] = out["ok"] and r["ok"]
        if r["download"] is not None:
            out["download"] = add(out["download"], r["download"])
        if r["disk"] is not None:
            out["disk"] = add(out["disk"], r["disk"])
    return out


def self_order(extra_steps, command):
    return command.replace(" && ", "\n")


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
        lines.append(disk_text(result["disk"]))
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
            counts.append(disk_text(result["disk"]))
        if not counts and ok:
            counts.append(_("Nothing to do."))
        bl.addWidget(QLabel("  ·  ".join(counts), banner))
        cmd = QLabel(command, banner)
        cmd.setTextFormat(Qt.TextFormat.PlainText)  # package and repository names come from outside
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
                repo = _("installed") if it["repo"] == "@System" else it["repo"]
                QTreeWidgetItem(top, [it["name"], ver, repo])
            top.setExpanded(True)
        tree.resizeColumnToContents(0)
        tree.resizeColumnToContents(1)
        lay.addWidget(tree, 1)

        if extra_steps:
            lbl = QLabel(_("These commands are also in the queue and cannot be simulated. Everything runs in "
                           "this order:") + "\n" + self_order(extra_steps, command), self)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setWordWrap(True)
            lay.addWidget(lbl)

        buttons = QDialogButtonBox(self)
        self.apply_btn = buttons.addButton(_("Apply"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_btn.setEnabled(can_apply and ok)
        buttons.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
