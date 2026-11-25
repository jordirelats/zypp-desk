# SPDX-License-Identifier: GPL-3.0-or-later
"""Web sources: programs installed from RPM files downloaded from the web, and their new versions."""

import html
import json
import os
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, websources
from ..i18n import _
from ..style import BLUE, GREEN, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Page, ask, button, error_box, muted, tool

GREY = "#7f8c8d"
SIG_TEXT = {
    "verified": _("signed, and rpm verified the signature"),
    "unsigned": _("not signed"),
    "bad": _("the signature does NOT verify"),
}
KINDS = [
    ("github", _("GitHub releases")),
    ("url", _("A fixed download address")),
    ("page", _("A download page")),
    ("manual", _("Do not follow it")),
]
KIND_SHORT = {"github": _("GitHub"), "url": _("Address"), "page": _("Page"), "manual": _("Not followed")}


def sig_text(sig):
    if sig and sig.startswith("nokey:"):
        return _("signed with a key rpm does not know ({key})").format(key=sig[6:])
    return SIG_TEXT.get(sig, sig or "")


def home_short(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def local_packages():
    """Installed packages that came from a local folder (plaindir) repository: the usual web downloads."""
    return local_state()["packages"]


def local_state():
    """The local folder repositories ({folder: repo}) and the installed packages that come from them."""
    folders, packages, aliases = {}, {}, set()
    for r in backend.repos():
        aliases.add(r["alias"])
        if r["type"] != "plaindir" or not r["url"].startswith("dir:"):
            continue
        folder = urllib.parse.unquote(r["url"][4:]).rstrip("/") or "/"
        folders[folder] = r
        try:
            root, _code = backend.xml(["search", "--installed-only", "--details", "--repo", r["alias"]])
        except backend.ZypperError:
            continue
        for s in root.iter("solvable"):
            if s.get("status") == "installed":
                packages[s.get("name")] = folder
    return {"folders": folders, "packages": packages, "aliases": aliases}


def github_guess(name):
    """The most starred GitHub project with this name, as a suggestion the user confirms."""
    q = urllib.parse.quote(f"{name} in:name")
    req = urllib.request.Request(f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=3",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": websources.USER_AGENT})
    with websources.https_open(req, timeout=20) as r:
        items = json.load(r).get("items", [])
    return [i["full_name"] for i in items]


class SourceDialog(QDialog):
    """Where a program is published: GitHub, a fixed address, a download page, or not followed."""

    def __init__(self, local, source=None, default_folder="", package=None, parent=None):
        super().__init__(parent)
        self.local = local
        self.setWindowTitle(_("Web source"))
        self.resize(660, 0)
        form = QFormLayout(self)
        self.package = QComboBox(self)
        self.package.setEditable(True)
        self.package.addItems(sorted(local))
        self.kind = QComboBox(self)
        for k, label in KINDS:
            self.kind.addItem(label, k)
        form.addRow(_("Installed package:"), self.package)
        form.addRow(_("Where it is published:"), self.kind)

        self.stack = QStackedWidget(self)
        # GitHub
        g = QWidget(self)
        gf = QFormLayout(g)
        gf.setContentsMargins(0, 0, 0, 0)
        self.repo = QLineEdit(self)
        self.repo.setPlaceholderText(_("owner/project, for example rustdesk/rustdesk"))
        find = QPushButton(_("Look for it on GitHub"), self)
        find.clicked.connect(self._find)
        rrow = QHBoxLayout()
        rrow.addWidget(self.repo, 1)
        rrow.addWidget(find)
        self.gpattern = QLineEdit(self)
        gf.addRow(_("GitHub project:"), rrow)
        gf.addRow(_("File to take (regular expression):"), self.gpattern)
        # fixed address
        u = QWidget(self)
        uf = QFormLayout(u)
        uf.setContentsMargins(0, 0, 0, 0)
        self.url = QLineEdit(self)
        self.url.setPlaceholderText("https://zoom.us/client/latest/zoom_openSUSE_x86_64.rpm")
        uf.addRow(_("Download address:"), self.url)
        uf.addRow(muted(_("An address that always gives the latest file. When it does not say the version, "
                          "Zypp Desk notices when the file changes.")))
        # page
        p = QWidget(self)
        pf = QFormLayout(p)
        pf.setContentsMargins(0, 0, 0, 0)
        self.page = QLineEdit(self)
        self.page.setPlaceholderText("https://iriun.com/")
        self.ppattern = QLineEdit(self)
        self.ppattern.setPlaceholderText(r"iriunwebcam-[\d.]+\.rpm$")
        pf.addRow(_("Download page:"), self.page)
        pf.addRow(_("Link to take (regular expression):"), self.ppattern)
        pf.addRow(muted(_("Zypp Desk reads the links written on the page and takes the newest one that matches. "
                          "A .zip with the RPM inside also works.")))
        # manual
        m = QWidget(self)
        mf = QFormLayout(m)
        mf.setContentsMargins(0, 0, 0, 0)
        self.note = QLineEdit(self)
        self.note.setPlaceholderText(_("for example: made on this computer"))
        self.mpage = QLineEdit(self)
        self.mpage.setPlaceholderText("https://…")
        mf.addRow(_("Note:"), self.note)
        mf.addRow(_("Page to look at by hand:"), self.mpage)
        for w in (g, u, p, m):
            self.stack.addWidget(w)
        form.addRow(self.stack)

        self.folder = QLineEdit(self)
        pick = QPushButton(_("Choose…"), self)
        pick.clicked.connect(self._pick)
        frow = QHBoxLayout()
        frow.addWidget(self.folder, 1)
        frow.addWidget(pick)
        self.folder_lbl = QLabel(_("Download into:"), self)
        form.addRow(self.folder_lbl, frow)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.package.currentTextChanged.connect(self._package_changed)
        if source:
            self.package.setCurrentText(source["package"])
            self.kind.setCurrentIndex(max(0, [k for k, _l in KINDS].index(source.get("type", "github"))))
            self.repo.setText(source.get("repo", ""))
            self.gpattern.setText(source.get("pattern", "") if source.get("type", "github") == "github" else "")
            self.url.setText(source.get("url", ""))
            self.page.setText(source.get("page", "") if source.get("type") == "page" else "")
            self.ppattern.setText(source.get("pattern", "") if source.get("type") == "page" else "")
            self.note.setText(source.get("note", ""))
            self.mpage.setText(source.get("page", "") if source.get("type") == "manual" else "")
            self.folder.setText(source.get("folder", "") or default_folder)
        else:
            if package:
                self.package.setCurrentText(package)
            self._package_changed(self.package.currentText())
            if not self.folder.text():
                self.folder.setText(default_folder)
        self._kind_changed()

    def _kind_changed(self):
        i = self.kind.currentIndex()
        self.stack.setCurrentIndex(i)
        manual = self.kind.currentData() == "manual"
        self.folder.setEnabled(not manual)
        self.folder_lbl.setEnabled(not manual)
        self.adjustSize()

    def _package_changed(self, name):
        folder = self.local.get(name, "")
        if folder:
            self.folder.setText(home_short(folder))
        example = None
        if folder and Path(folder).is_dir():
            files = sorted(f.name for f in Path(folder).glob(f"{name}-*.rpm"))
            example = files[-1] if files else None
        self.gpattern.setText(websources.guess_pattern(name, example))

    def _find(self):
        name = self.package.currentText().strip()
        if not name:
            return
        self.repo.setPlaceholderText(_("Searching…"))
        run_async(github_guess, name, on_done=self._found)

    def _found(self, hits, err):
        self.repo.setPlaceholderText(_("owner/project, for example rustdesk/rustdesk"))
        if err:
            error_box(self, self.windowTitle(), str(err)[:300])
            return
        if hits:
            self.repo.setText(hits[0])
            self.repo.setToolTip(_("Other matches: {list}").format(list=", ".join(hits[1:])))

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, _("Download into:"), os.path.expanduser(self.folder.text()))
        if d:
            self.folder.setText(home_short(d))

    def _ok(self):
        kind = self.kind.currentData()
        name = self.package.currentText().strip()
        need = {
            "github": "/" in self.repo.text() and self.gpattern.text().strip(),
            "url": self.url.text().strip().startswith("https://"),
            "page": self.page.text().strip().startswith("https://") and self.ppattern.text().strip(),
            "manual": True,
        }[kind]
        if not name or not need or (kind != "manual" and not self.folder.text().strip()):
            error_box(self, self.windowTitle(), _("Fill in every field. Addresses must start with https://."))
            return
        if kind in ("github", "page"):
            import re
            try:
                re.compile(self.gpattern.text() if kind == "github" else self.ppattern.text())
            except re.error as e:
                error_box(self, self.windowTitle(), _("The regular expression is not valid: {error}").format(error=e))
                return
        self.accept()

    def source(self):
        kind = self.kind.currentData()
        name = self.package.currentText().strip()
        folder = self.folder.text().strip()
        if kind == "github":
            return websources.new_source(name, kind, folder, repo=self.repo.text().strip(),
                                         pattern=self.gpattern.text().strip())
        if kind == "url":
            return websources.new_source(name, kind, folder, url=self.url.text().strip())
        if kind == "page":
            return websources.new_source(name, kind, folder, page=self.page.text().strip(),
                                         pattern=self.ppattern.text().strip())
        return websources.new_source(name, "manual", folder, note=self.note.text().strip(),
                                     page=self.mpage.text().strip())


class WebSourcesPage(Page):
    key = "websources"
    title = _("Web sources")
    icon_name = "internet-web-browser"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.sources = websources.load()
        self.found = {}
        self.downloading = False
        self._dl_queue = []
        self._dl_current = None
        self.aliases = set()
        self.local = {}
        self.folders = {}

        self.folder_lbl = QLabel(self)
        self.folder_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.folder_lbl.setWordWrap(True)
        fbar = QHBoxLayout()
        fbar.addWidget(self.folder_lbl, 1)
        fbar.addWidget(button(_("Change the folder…"), "folder-open", self.change_folder))

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("Program"), _("Installed"), _("Latest"), _("Where it is published"),
                                   _("Status")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.currentItemChanged.connect(lambda cur, _p: self._show(cur))
        self.details = QTextBrowser(self)
        self.details.setOpenExternalLinks(True)
        self.follow_btn = button(_("Follow it…"), "list-add", self.follow)
        self.get_btn = button(_("Download, verify and add to the queue"), "download", self.download, primary=True)
        self.follow_btn.hide()
        self.get_btn.setEnabled(False)
        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.tree, 1)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(self.follow_btn)
        brow.addWidget(self.get_btn)
        lv.addLayout(brow)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(left)
        split.addWidget(self.details)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 3)
        split.setSizes([640, 380])
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("zypper cannot know about new versions of programs you installed from a file "
                              "downloaded from the web. Here are all of them: say where each one is published "
                              "(GitHub, a fixed address or a download page) and Zypp Desk looks for new versions, "
                              "downloads them, checks them and puts them in the queue.")))
        lay.addLayout(fbar)
        lay.addWidget(split, 1)
        self._show_folder()

    def header_actions(self):
        return [tool(_("Add…"), "list-add", self.add), tool(_("Edit…"), "document-edit", self.edit),
                tool(_("Remove"), "list-remove", self.remove),
                tool(_("Check now"), "view-refresh", lambda: self.check_all(force=True))]

    def reload(self):
        self.sources = websources.load()
        self._fill()
        self.check_all()
        run_async(local_state, on_done=self._local_loaded)

    def _local_loaded(self, res, err):
        if err or not res:
            return
        self.local = res["packages"]
        self.folders = res["folders"]
        self.aliases = res.get("aliases", set())
        self._show_folder()
        self._fill()

    # ------------------------------------------------------------ folder
    def default_folder(self):
        chosen = self.ctx.settings.get("web_folder") or ""
        if chosen:
            return chosen
        counts = Counter(self.local.values())
        if counts:
            return home_short(counts.most_common(1)[0][0])
        used = Counter(s.get("folder") for s in self.sources if s.get("folder"))
        return used.most_common(1)[0][0] if used else "~/RPM"

    def _repo_of(self, folder):
        path = os.path.expanduser(folder).rstrip("/")
        return self.folders.get(path)

    def _show_folder(self):
        folder = self.default_folder()
        repo = self._repo_of(folder)
        if repo:
            where = _("zypper uses it as the repository “{name}” ({alias}), so every version stays together and zypper "
                      "sees them.").format(name=html.escape(repo["name"]), alias=html.escape(repo["alias"]))
        elif self.folders or self.local:
            where = _("It is not a zypper repository.")
        else:
            where = ""
        self.folder_lbl.setText(_("New versions are saved in <b>{folder}</b>.").format(folder=html.escape(folder))
                                + " " + where)

    def change_folder(self):
        cur = os.path.expanduser(self.default_folder())
        d = QFileDialog.getExistingDirectory(self, _("Folder for the programs from the web"), cur)
        if not d:
            return
        short = home_short(d)
        self.ctx.settings["web_folder"] = short
        self.ctx.settings.save()
        followed = [s for s in self.sources if s.get("type") != "manual" and s.get("folder") != short]
        if followed and ask(self, self.title, _("Save the new versions of the {n} programs you follow in {folder} "
                                                "too?").format(n=len(followed), folder=short)):
            for s in self.sources:
                if s.get("type") != "manual":
                    s["folder"] = short
            websources.save(self.sources)
        if not self._repo_of(short) and ask(self, self.title, _(
                "{folder} is not a zypper repository. Add it as one? Then zypper sees every RPM you keep there, "
                "and you can install them by name.").format(folder=short)):
            aliases = set(self.aliases) | {a.target for a in self.ctx.queue.of("repo-add")}
            alias, n = "web-programs", 2
            while alias in aliases:
                alias, n = f"web-programs-{n}", n + 1
            self.ctx.queue.add(Action("repo-add", alias, url="dir:" + urllib.parse.quote(d),
                                      name=_("Programs from the web"),
                                      autorefresh=True, priority=99, gpg="default"))
        self._show_folder()
        self._fill()

    # ------------------------------------------------------------ table
    def _status(self, s, f):
        """(text, colour) of a row."""
        if s is None:
            return _("not followed yet"), GREY
        if s.get("type") == "manual":
            return _("not followed"), GREY
        if f.get("error"):
            return _("could not check: {error}").format(error=f["error"]), RED
        if not f:
            return _("checking…"), None
        if not f.get("installed"):
            return _("not installed"), None
        if f.get("newer"):
            return _("new version"), BLUE
        if f.get("newer") is None:
            if f.get("changed"):
                return _("file changed"), ORANGE
            return _("same file as last time"), GREEN
        return _("up to date"), GREEN

    def _fill(self):
        cur = self.tree.currentItem()
        cur_pkg = cur.data(0, Qt.ItemDataRole.UserRole + 1) if cur else None
        self.tree.clear()
        followed = {s["package"] for s in self.sources}
        rows = [(s["package"], s) for s in self.sources] + [(p, None) for p in self.local if p not in followed]
        order = {"github": 0, "url": 0, "page": 0, "manual": 2}
        rows.sort(key=lambda r: (1 if r[1] is None else order.get(r[1].get("type", "github"), 0), r[0].lower()))
        for name, s in rows:
            f = self.found.get(name, {}) if s else {}
            text, color = self._status(s, f)
            installed = f.get("installed") or ""
            if s is None or s.get("type") == "manual":
                installed = installed or self._installed_cache(name)
            origin = ""
            if s and s.get("type") != "manual":
                where = websources.origin_text(s)
                origin = KIND_SHORT.get(s.get("type", "github"), "") + " · " + where.removeprefix("github.com/")
            it = QTreeWidgetItem(self.tree, [name, installed, f.get("version") or "", origin, text])
            it.setData(0, Qt.ItemDataRole.UserRole, s)
            it.setData(0, Qt.ItemDataRole.UserRole + 1, name)
            it.setToolTip(3, websources.origin_text(s) if s else "")
            it.setToolTip(4, (s or {}).get("note") or text)
            if color:
                it.setForeground(4, QBrush(QColor(color)))
            if s is None or s.get("type") == "manual":
                for c in range(4):
                    it.setForeground(c, QBrush(QColor(GREY)))
            if name == cur_pkg:
                self.tree.setCurrentItem(it)
        for c in range(4):
            self.tree.resizeColumnToContents(c)
        if not rows:
            empty = QTreeWidgetItem(self.tree, [_("No program from the web yet. Use “Add…”.")])
            empty.setFirstColumnSpanned(True)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
        elif self.tree.currentItem() is None:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _installed_cache(self, name):
        cache = self.__dict__.setdefault("_inst", {})
        if name not in cache:
            cache[name] = websources.installed_version(name) or ""
        return cache[name]

    def _current(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _current_name(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole + 1) if it else None

    def _show(self, item):
        name = item.data(0, Qt.ItemDataRole.UserRole + 1) if item else None
        s = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        self.follow_btn.setVisible(bool(name) and s is None)
        if not name:
            self.details.setHtml("")
            self.get_btn.setEnabled(False)
            return
        e = html.escape
        if s is None:
            folder = self.local.get(name, "")
            self.details.setHtml(
                f"<h3>{e(name)}</h3><p>" + e(_("Installed from {folder}. Zypp Desk does not know yet where it is "
                                               "published.").format(folder=home_short(folder))) + "</p><p>"
                + e(_("Use “Follow it…” to say where (GitHub, a fixed address or a download page), or choose “Do "
                      "not follow it” there if it does not come from the web.")) + "</p>")
            self.get_btn.setEnabled(False)
            return
        f = self.found.get(name, {})
        kind = s.get("type", "github")

        def link(url, text=None):
            return f"<a href='{e(url)}'>{e(text or url)}</a>" if url else ""

        rows = []
        if kind == "github":
            rows.append((_("GitHub project"), link(f"https://github.com/{s['repo']}", s["repo"])))
        elif kind == "url":
            rows.append((_("Download address"), link(s["url"])))
        elif kind == "page":
            rows += [(_("Download page"), link(s["page"])), (_("Link to take"), f"<code>{e(s['pattern'])}</code>")]
        else:
            rows += [(_("Note"), e(s.get("note") or "")), (_("Page to look at by hand"), link(s.get("page", "")))]
        if kind != "manual":
            if f.get("url") and kind != "github":
                rows.append((_("File found"), link(f["url"], f.get("asset"))))
            else:
                rows.append((_("File"), e(f.get("asset") or (_("no file matches {pattern}").format(
                    pattern=s.get("pattern", "")) if f and not f.get("error") else ""))))
            rows.append((_("Size"), f"{f['size'] / 1048576:.1f} MiB" if f.get("size") else ""))
            rows.append((_("Published"), e(f.get("published") or "")))
            if kind == "github":
                rows.append((_("SHA-256 published by GitHub"), f"<code>{f['sha256']}</code>" if f.get("sha256") else
                             _("none: the download cannot be checked")))
            if f.get("version") is None and f and not f.get("error"):
                rows.append((_("Version"), e(_("the address does not say it: Zypp Desk compares the file with the "
                                               "one seen last time"))))
            rows.append((_("Download into"), e(s.get("folder", ""))))
            if kind == "github" and f.get("page"):
                rows.append((_("Release page"), link(f["page"], f.get("tag"))))
        self.details.setHtml(f"<h3>{e(name)}</h3><table>" + "".join(
            f"<tr><td style='color:gray;padding-right:12px'>{k}</td><td>{v}</td></tr>" for k, v in rows if v)
            + "</table>")
        self.get_btn.setText(_("Download it to see the version") if f.get("newer") is None and f.get("changed")
                             else _("Download, verify and add to the queue"))
        self.get_btn.setEnabled(kind != "manual" and bool(f.get("url")) and not self.ctx.runner.busy
                                and not self.downloading)

    # ------------------------------------------------------------ checking
    def check_all(self, force=False):
        self.found = {}
        self._fill()
        for s in self.sources:
            if s.get("type") == "manual":
                self.found[s["package"]] = websources.check(s)
                continue
            run_async(websources.check, s, force,
                      on_done=lambda res, err, p=s["package"]: self._checked(p, res, err))
        self._fill()

    def _checked(self, package, res, err):
        self.found[package] = res if not err else {"error": str(err)}
        self._fill()
        self._show(self.tree.currentItem())
        newer = [p for p, f in self.found.items() if f.get("newer")]
        self.ctx.set_badge("websources", str(len(newer)) if newer else "")

    # ------------------------------------------------------------ editing
    def _save_source(self, s, old_name=None):
        self.sources = [x for x in self.sources if x["package"] not in (s["package"], old_name)] + [s]
        websources.save(self.sources)
        self.check_all()

    def add(self, package=None):
        dlg = SourceDialog(self.local, default_folder=self.default_folder(), package=package, parent=self)
        if dlg.exec():
            self._save_source(dlg.source())

    def follow(self):
        name = self._current_name()
        if name:
            self.add(name)

    def edit(self):
        s = self._current()
        if not s:
            if self._current_name():
                self.follow()
            return
        dlg = SourceDialog(self.local, s, default_folder=self.default_folder(), parent=self)
        if dlg.exec():
            new = dlg.source()
            new["seen"] = s.get("seen", "") if new.get("type") == s.get("type") else ""
            if not new["seen"]:
                new.pop("seen")
            # Qt gives back copies of the stored dicts: compare by package name
            self._save_source(new, s["package"])

    def remove(self):
        s = self._current()
        if s and ask(self, _("Remove"), _("Stop looking for new versions of {name}? Nothing is uninstalled.")
                     .format(name=s["package"])):
            self.sources = [x for x in self.sources if x["package"] != s["package"]]
            websources.save(self.sources)
            self._fill()

    def _remember_file(self, package, fingerprint):
        """Remember the file seen, for addresses that do not say the version."""
        if not fingerprint:
            return
        for x in self.sources:
            if x["package"] == package:
                x["seen"] = fingerprint
        websources.save(self.sources)

    # ------------------------------------------------------------ download
    def download(self):
        s = self._current()
        if s:
            self.get_new_versions([s["package"]])

    def get_new_versions(self, names):
        """Download, check and queue the new version of these programs, one after the other.

        Used by the button here and by the Updates and Applications pages. Each file goes through the same
        checks and questions as always; only one download runs at a time.
        """
        queued = {x["package"] for x in self._dl_queue} | ({self._dl_current} if self._dl_current else set())
        for n in names:
            src = next((x for x in self.sources if x["package"] == n and x.get("type", "github") != "manual"), None)
            if src and n not in queued:
                self._dl_queue.append(src)
                queued.add(n)
        self._next_download()

    def _next_download(self):
        if self.downloading or not self._dl_queue:
            self.get_btn.setEnabled(bool(self._current()) and not self.downloading)
            return
        s = self._dl_queue.pop(0)
        self.downloading = True
        self._dl_current = s["package"]
        self.get_btn.setEnabled(False)
        f = self.found.get(s["package"]) or {}
        if f.get("url"):
            self._start_download(s, f)
        else:  # not looked at yet (the page was never opened): look first
            run_async(websources.check, s, on_done=lambda res, err: self._checked_for_download(s, res, err))

    def _checked_for_download(self, s, res, err):
        if err or not (res or {}).get("url"):
            self.ctx.terminal.view.note(_("{name}: could not find the file to download: {error}").format(
                name=s["package"], error=err or _("no file matches")), "error")
            self.downloading = False
            self._dl_current = None
            self._next_download()
            return
        self.found[s["package"]] = res
        self._start_download(s, res)

    def _start_download(self, s, f):
        view = self.ctx.terminal.view
        self.ctx.term_toggle.setChecked(True)
        self.ctx.terminal.set_title(_("Download {file}").format(file=f.get("asset") or s["package"]))
        view.prompt(f"$ curl -LO {f['url']}")
        view.note(_("Downloading {size} MiB…").format(size=f"{(f.get('size') or 0) / 1048576:.1f}"), "info")
        run_async(websources.download, s, f, on_done=lambda res, err: self._downloaded(s, f, res, err))

    def _downloaded(self, s, f, res, err):
        try:
            self._handle_download(s, f, res, err)
        finally:
            self.downloading = False
            self._dl_current = None
            self._next_download()

    def _handle_download(self, s, f, res, err):
        view = self.ctx.terminal.view
        if err:
            view.note(_("Download failed: {error}").format(error=err), "error")
            error_box(self, self.title, str(err))
            return
        view.note(_("Downloaded to {path}").format(path=res["path"]), "ok")
        view.note(_("Version in the file: {version}").format(version=res["version"]), "ok")
        if res["same"]:
            os.unlink(res["path"])
            self._remember_file(s["package"], f.get("fingerprint"))
            view.note(_("You already have this version: the file was deleted."), "info")
            QMessageBox.information(self, self.title, _("{name}: you already have version {version}.").format(
                name=s["package"], version=res["version"]))
            self.check_all()
            return
        if res["sha256_checked"]:
            view.note(_("SHA-256 matches the one GitHub publishes: {sum}").format(sum=res["sha256"]), "ok")
        elif s.get("type", "github") == "github":
            view.note(_("GitHub publishes no SHA-256 for this file: it could not be checked."), "error")
        else:
            view.note(_("The web publishes no SHA-256 for this file: SHA-256 {sum}").format(sum=res["sha256"]), "info")
        sig = res["signature"]
        view.note(_("Signature: {text}").format(text=sig_text(sig)), "ok" if sig == "verified" else "error")
        allow_unsigned = False
        if sig == "bad":
            os.unlink(res["path"])
            error_box(self, self.title, _("The signature of the file does not verify: it was deleted."))
            return
        if sig != "verified":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(_("Package not verified by rpm"))
            box.setText(_("{name} {version}: {sig}.").format(name=s["package"], version=res["version"],
                                                             sig=sig_text(sig)))
            if res["sha256_checked"]:
                why = _("The file is exactly the one GitHub publishes (SHA-256 checked), but rpm cannot confirm who "
                        "made it.")
            elif s.get("type", "github") == "github":
                why = _("Nothing confirms that the file is the original.")
            else:
                why = _("It was downloaded over https from {host}, but nothing else confirms that the file is the "
                        "original.").format(host=urllib.parse.urlsplit(f["url"]).netloc)
            box.setInformativeText(why + " " + _("Add it to the queue anyway?"))
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if box.exec() != QMessageBox.StandardButton.Yes:
                os.unlink(res["path"])
                view.note(_("Not accepted: the downloaded file was deleted."), "info")
                return
            allow_unsigned = sig == "unsigned" or sig.startswith("nokey:")
        path = websources.place(res["path"], s)
        self._remember_file(s["package"], f.get("fingerprint"))
        view.note(_("Saved as {path}").format(path=path), "ok")
        self.ctx.queue.add(Action("install", path, package=s["package"], allow_unsigned=allow_unsigned))
        view.note(_("Added to the queue. Simulate it, then apply it."), "info")
