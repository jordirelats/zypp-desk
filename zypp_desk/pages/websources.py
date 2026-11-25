# SPDX-License-Identifier: GPL-3.0-or-later
"""Web sources: new versions of packages that were installed from RPM files downloaded from the web."""

import html
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLineEdit, QMessageBox, QPushButton, QSplitter, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, websources
from ..i18n import _
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Page, ask, button, error_box, muted, tool

SIG_TEXT = {
    "verified": _("signed, and rpm verified the signature"),
    "unsigned": _("not signed"),
    "bad": _("the signature does NOT verify"),
}


def sig_text(sig):
    if sig and sig.startswith("nokey:"):
        return _("signed with a key rpm does not know ({key})").format(key=sig[6:])
    return SIG_TEXT.get(sig, sig or "")


def local_packages():
    """Installed packages that came from a local folder (plaindir) repository: the usual web downloads."""
    out = {}
    for r in backend.repos():
        if r["type"] != "plaindir" or not r["url"].startswith("dir:"):
            continue
        folder = urllib.parse.unquote(r["url"][4:])
        try:
            root, _code = backend.xml(["search", "--installed-only", "--details", "--repo", r["alias"]])
        except backend.ZypperError:
            continue
        for s in root.iter("solvable"):
            if s.get("status") == "installed":
                out[s.get("name")] = folder
    return out


def github_guess(name):
    """The most starred GitHub project with this name, as a suggestion the user confirms."""
    q = urllib.parse.quote(f"{name} in:name")
    req = urllib.request.Request(f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=3",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": websources.USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        items = json.load(r).get("items", [])
    return [i["full_name"] for i in items]


class SourceDialog(QDialog):
    def __init__(self, local, source=None, parent=None):
        super().__init__(parent)
        self.local = local
        self.setWindowTitle(_("Web source"))
        self.resize(620, 0)
        form = QFormLayout(self)
        self.package = QComboBox(self)
        self.package.setEditable(True)
        self.package.addItems(sorted(local))
        self.repo = QLineEdit(self)
        self.repo.setPlaceholderText(_("owner/project, for example rustdesk/rustdesk"))
        find = QPushButton(_("Look for it on GitHub"), self)
        find.clicked.connect(self._find)
        rrow = QHBoxLayout()
        rrow.addWidget(self.repo, 1)
        rrow.addWidget(find)
        self.pattern = QLineEdit(self)
        self.folder = QLineEdit(self)
        pick = QPushButton(_("Choose…"), self)
        pick.clicked.connect(self._pick)
        frow = QHBoxLayout()
        frow.addWidget(self.folder, 1)
        frow.addWidget(pick)
        form.addRow(_("Installed package:"), self.package)
        form.addRow(_("GitHub project:"), rrow)
        form.addRow(_("File to take (regular expression):"), self.pattern)
        form.addRow(_("Download into:"), frow)
        form.addRow(muted(_("Downloading into the folder of a local repository keeps every version together, "
                            "as you do now.")))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.package.currentTextChanged.connect(self._package_changed)
        if source:
            self.package.setCurrentText(source["package"])
            self.repo.setText(source["repo"])
            self.pattern.setText(source["pattern"])
            self.folder.setText(source["folder"])
        else:
            self._package_changed(self.package.currentText())

    def _package_changed(self, name):
        folder = self.local.get(name, "")
        if folder:
            self.folder.setText(folder.replace(str(Path.home()), "~", 1))
        example = None
        if folder and Path(folder).is_dir():
            files = sorted(f.name for f in Path(folder).glob(f"{name}-*.rpm"))
            example = files[-1] if files else None
        self.pattern.setText(websources.guess_pattern(name, example))

    def _find(self):
        name = self.package.currentText().strip()
        if not name:
            return
        try:
            hits = github_guess(name)
        except OSError as e:
            error_box(self, self.windowTitle(), str(e))
            return
        if hits:
            self.repo.setText(hits[0])
            self.repo.setToolTip(_("Other matches: {list}").format(list=", ".join(hits[1:])))

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, _("Download into:"), os.path.expanduser(self.folder.text()))
        if d:
            self.folder.setText(d)

    def _ok(self):
        if not (self.package.currentText().strip() and "/" in self.repo.text() and self.pattern.text().strip()
                and self.folder.text().strip()):
            error_box(self, self.windowTitle(), _("Fill in the package, the GitHub project (owner/project), the file "
                                                  "and the folder."))
            return
        self.accept()

    def source(self):
        return websources.new_source(self.package.currentText().strip(), self.repo.text().strip(),
                                     self.pattern.text().strip(), self.folder.text().strip())


class WebSourcesPage(Page):
    key = "websources"
    title = _("Web sources")
    icon_name = "internet-web-browser"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.sources = websources.load()
        self.found = {}
        self.local = {}
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("Package"), _("Installed"), _("Latest"), _("Published"), _("Source"),
                                   _("Status")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.currentItemChanged.connect(lambda cur, _p: self._show(cur))
        self.details = QTextBrowser(self)
        self.details.setOpenExternalLinks(True)
        self.get_btn = button(_("Download, verify and add to the queue"), "download", self.download)
        self.get_btn.setEnabled(False)
        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.tree, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.get_btn)
        lv.addLayout(row)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(left)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("zypper cannot know about new versions of programs you installed from a file "
                              "downloaded from the web. Here you say where each one is published (for now: GitHub "
                              "releases), and Zypp Desk looks for new versions, downloads them, checks them and "
                              "puts them in the queue.")))
        lay.addWidget(split, 1)

    def header_actions(self):
        return [tool(_("Add…"), "list-add", self.add), tool(_("Edit…"), "document-edit", self.edit),
                tool(_("Remove"), "list-remove", self.remove), tool(_("Check now"), "view-refresh", self.check_all)]

    def reload(self):
        self._fill()
        self.check_all()
        run_async(local_packages, on_done=lambda res, err: setattr(self, "local", res or {}))

    # ------------------------------------------------------------ table
    def _fill(self):
        cur = self.tree.currentItem()
        cur_pkg = cur.data(0, Qt.ItemDataRole.UserRole)["package"] if cur else None
        self.tree.clear()
        for s in self.sources:
            f = self.found.get(s["package"], {})
            if f.get("error"):
                status, color = _("could not check: {error}").format(error=f["error"]), "#da4453"
            elif not f:
                status, color = _("checking…"), None
            elif f.get("newer"):
                status, color = _("new version available"), "#1d99f3"
            elif not f.get("installed"):
                status, color = _("not installed"), None
            else:
                status, color = _("up to date"), "#27ae60"
            it = QTreeWidgetItem(self.tree, [s["package"], f.get("installed") or "", f.get("version") or "",
                                             f.get("published") or "", "github.com/" + s["repo"], status])
            it.setData(0, Qt.ItemDataRole.UserRole, s)
            if color:
                it.setForeground(5, QBrush(QColor(color)))
            if s["package"] == cur_pkg:
                self.tree.setCurrentItem(it)
        for c in range(6):
            self.tree.resizeColumnToContents(c)
        if not self.sources:
            QTreeWidgetItem(self.tree, [_("No web source yet. Use “Add…”."), "", "", "", "", ""])

    def _current(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _show(self, item):
        s = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not s:
            self.details.setHtml("")
            self.get_btn.setEnabled(False)
            return
        f = self.found.get(s["package"], {})
        rows = [(_("GitHub project"), f"<a href='https://github.com/{html.escape(s['repo'])}'>{html.escape(s['repo'])}</a>"),
                (_("File"), html.escape(f.get("asset") or _("no file matches {pattern}").format(pattern=s["pattern"]))),
                (_("Size"), f"{(f.get('size') or 0) / 1048576:.1f} MiB" if f.get("size") else ""),
                (_("SHA-256 published by GitHub"), f"<code>{f['sha256']}</code>" if f.get("sha256") else
                 _("none: the download cannot be checked")),
                (_("Download into"), html.escape(s["folder"])),
                (_("Release page"), f"<a href='{html.escape(f.get('page', ''))}'>{html.escape(f.get('tag', ''))}</a>")]
        self.details.setHtml(f"<h3>{html.escape(s['package'])}</h3><table>" + "".join(
            f"<tr><td style='color:gray;padding-right:12px'>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>")
        self.get_btn.setEnabled(bool(f.get("asset")) and not self.ctx.runner.busy)

    # ------------------------------------------------------------ checking
    def check_all(self):
        self.found = {}
        self._fill()
        for s in self.sources:
            run_async(websources.check, s, on_done=lambda res, err, p=s["package"]: self._checked(p, res, err))

    def _checked(self, package, res, err):
        self.found[package] = res if not err else {"error": str(err)}
        self._fill()
        self._show(self.tree.currentItem())
        newer = [p for p, f in self.found.items() if f.get("newer")]
        self.ctx.set_badge("websources", str(len(newer)) if newer else "")

    # ------------------------------------------------------------ editing
    def add(self):
        dlg = SourceDialog(self.local, parent=self)
        if dlg.exec():
            s = dlg.source()
            self.sources = [x for x in self.sources if x["package"] != s["package"]] + [s]
            websources.save(self.sources)
            self.check_all()

    def edit(self):
        s = self._current()
        if not s:
            return
        dlg = SourceDialog(self.local, s, parent=self)
        if dlg.exec():
            new = dlg.source()
            self.sources = [new if x is s else x for x in self.sources]
            websources.save(self.sources)
            self.check_all()

    def remove(self):
        s = self._current()
        if s and ask(self, _("Remove"), _("Stop looking for new versions of {name}? Nothing is uninstalled.")
                     .format(name=s["package"])):
            self.sources = [x for x in self.sources if x is not s]
            websources.save(self.sources)
            self._fill()

    # ------------------------------------------------------------ download
    def download(self):
        s = self._current()
        f = self.found.get(s["package"], {}) if s else {}
        if not f.get("asset"):
            return
        view = self.ctx.terminal.view
        self.ctx.term_toggle.setChecked(True)
        self.ctx.terminal.set_title(_("Download {file}").format(file=f["asset"]))
        view.prompt(f"$ curl -LO {f['url']}")
        self.get_btn.setEnabled(False)
        view.note(_("Downloading {size} MiB…").format(size=f"{(f.get('size') or 0) / 1048576:.1f}"), "info")
        run_async(websources.download, s, f, on_done=lambda res, err: self._downloaded(s, f, res, err))

    def _downloaded(self, s, f, res, err):
        view = self.ctx.terminal.view
        self.get_btn.setEnabled(True)
        if err:
            view.note(_("Download failed: {error}").format(error=err), "error")
            error_box(self, self.title, str(err))
            return
        view.note(_("Saved as {path}").format(path=res["path"]), "ok")
        if res["sha256_checked"]:
            view.note(_("SHA-256 matches the one GitHub publishes: {sum}").format(sum=res["sha256"]), "ok")
        else:
            view.note(_("GitHub publishes no SHA-256 for this file: it could not be checked."), "error")
        sig = res["signature"]
        view.note(_("Signature: {text}").format(text=sig_text(sig)), "ok" if sig == "verified" else "error")
        allow_unsigned = False
        if sig != "verified":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(_("Package not verified by rpm"))
            box.setText(_("{name} {version}: {sig}.").format(name=s["package"], version=f["version"],
                                                             sig=sig_text(sig)))
            box.setInformativeText(
                (_("The file is exactly the one GitHub publishes (SHA-256 checked), but rpm cannot confirm who "
                   "made it.") if res["sha256_checked"] else _("Nothing confirms that the file is the original."))
                + " " + _("Add it to the queue anyway?"))
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            allow_unsigned = sig in ("unsigned", "bad") or sig.startswith("nokey:")
        self.ctx.queue.add(Action("install", res["path"], package=s["package"], allow_unsigned=allow_unsigned))
        view.note(_("Added to the queue. Simulate it, then apply it."), "info")
