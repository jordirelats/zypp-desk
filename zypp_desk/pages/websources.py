# SPDX-License-Identifier: GPL-3.0-or-later
"""Web sources: programs installed from RPM files downloaded from the web, and their new versions.

Under the list, "Bridge packages" (pages/bridges.py): the packages Zypp Desk made so that programs made for
Fedora could be installed, and whether each one is still needed. A new version of a program from the web is
looked at before it goes to the queue: when it asks for things by their Fedora names, the question of the
bridge comes first, and the bridge (new, or a new release of it) goes to the queue with the program.
"""

import html
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QFile, QLocale, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSplitter, QTextBrowser, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, bridge, question, websources
from ..i18n import _, ngettext, pgettext
from ..style import BLUE, GREEN, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Page, ask, button, error_box, info_box, muted, tool
from .bridges import RPM_ICON, BridgesBox, ask_bridge, ask_unmapped, queue_bridge

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
    """Where a program is published: GitHub, a fixed address, a download page, or not followed.

    One form: the rows that do not belong to the chosen kind are hidden. The patterns that choose the file
    are filled in from the package name and kept under “Advanced”, because they are regular expressions."""

    def __init__(self, local, source=None, default_folder="", package=None, parent=None):
        super().__init__(parent)
        self.local = local
        self.setWindowTitle(_("Web source"))
        self.setMinimumWidth(640)
        self.form = form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.package = QComboBox(self)
        self.package.setEditable(True)
        self.package.addItems(sorted(local))
        self.kind = QComboBox(self)
        for k, label in KINDS:
            self.kind.addItem(label, k)
        form.addRow(_("Installed package:"), self.package)
        form.addRow(_("Where it is published:"), self.kind)

        # GitHub
        self.repo = QLineEdit(self)
        self.repo.setPlaceholderText(_("owner/project on github.com"))
        find = QPushButton(_("Look for it on GitHub"), self)
        find.clicked.connect(self._find)
        self.r_repo = self._row(_("GitHub project:"), self._hbox(self.repo, find))
        # fixed address
        self.url = QLineEdit(self)
        self.r_url = self._row(_("Download address:"), self.url)
        # download page
        self.page = QLineEdit(self)
        self.page.setPlaceholderText("https://…")
        self.r_page = self._row(_("Download page:"), self.page)
        # not followed
        self.note = QLineEdit(self)
        self.note.setPlaceholderText(_("for example: made on this computer"))
        self.r_note = self._row(_("Note:"), self.note)
        self.mpage = QLineEdit(self)
        self.mpage.setPlaceholderText("https://…")
        self.r_mpage = self._row(_("Page to look at by hand:"), self.mpage)
        # what Zypp Desk does with it, in plain words
        self.help = muted("", self)
        form.addRow(self.help)
        self.r_help = form.rowCount() - 1
        # where new versions go
        self.folder = QLineEdit(self)
        pick = QPushButton(_("Choose…"), self)
        pick.clicked.connect(self._pick)
        self.r_folder = self._row(_("Download into:"), self._hbox(self.folder, pick))
        # the patterns, for those who want them
        self.adv = QToolButton(self)
        self.adv.setText(_("Advanced"))
        self.adv.setCheckable(True)
        self.adv.setAutoRaise(True)
        self.adv.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv.setArrowType(Qt.ArrowType.RightArrow)
        self.adv.toggled.connect(self._update_rows)
        form.addRow(self.adv)
        self.r_adv = form.rowCount() - 1
        self.gpattern = QLineEdit(self)
        self.r_gpat = self._row(_("File to take (regular expression):"), self.gpattern)
        self.ppattern = QLineEdit(self)
        self.r_ppat = self._row(_("Link to take (regular expression):"), self.ppattern)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.kind.currentIndexChanged.connect(self._update_rows)
        self.package.currentTextChanged.connect(self._package_changed)
        if source:
            kind = source.get("type", "github")
            self.package.setCurrentText(source["package"])
            self.kind.setCurrentIndex(max(0, [k for k, _l in KINDS].index(kind)))
            self._package_changed(source["package"], keep_folder=True)
            self.repo.setText(source.get("repo", ""))
            if kind == "github" and source.get("pattern"):
                self.gpattern.setText(source["pattern"])
            self.url.setText(source.get("url", ""))
            self.page.setText(source.get("page", "") if kind == "page" else "")
            if kind == "page" and source.get("pattern"):
                self.ppattern.setText(source["pattern"])
            self.note.setText(source.get("note", ""))
            self.mpage.setText(source.get("page", "") if kind == "manual" else "")
            self.folder.setText(source.get("folder", "") or default_folder)
        else:
            if package:
                self.package.setCurrentText(package)
            self._package_changed(self.package.currentText())
            if not self.folder.text():
                self.folder.setText(default_folder)
        self._update_rows()

    def _row(self, label, widget):
        self.form.addRow(label, widget)
        return self.form.rowCount() - 1

    def _hbox(self, *widgets):
        w = QWidget(self)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for i, x in enumerate(widgets):
            h.addWidget(x, 1 if i == 0 else 0)
        return w

    def _update_rows(self, *_a):
        kind = self.kind.currentData()
        adv = self.adv.isChecked()
        self.adv.setArrowType(Qt.ArrowType.DownArrow if adv else Qt.ArrowType.RightArrow)
        shown = {
            self.r_repo: kind == "github", self.r_url: kind == "url", self.r_page: kind == "page",
            self.r_note: kind == "manual", self.r_mpage: kind == "manual", self.r_folder: kind != "manual",
            self.r_adv: kind in ("github", "page"), self.r_gpat: kind == "github" and adv,
            self.r_ppat: kind == "page" and adv,
        }
        for row, on in shown.items():
            self.form.setRowVisible(row, on)
        self.help.setText({
            "github": _("Zypp Desk looks at the latest release of the project and takes the file of this package."),
            "url": _("An address that always gives the latest file. When it does not say the version, Zypp Desk "
                     "notices when the file changes."),
            "page": _("Zypp Desk reads the links written on the page and takes the newest one that matches. A .zip "
                      "with the RPM inside also works."),
            "manual": _("Zypp Desk will not look for new versions of this package."),
        }[kind])
        self.adjustSize()

    def _package_changed(self, name, keep_folder=False):
        name = name.strip()
        folder = self.local.get(name, "")
        if folder and not keep_folder:
            self.folder.setText(home_short(folder))
        example = None
        if folder and Path(folder).is_dir():
            files = sorted(f.name for f in Path(folder).glob(f"{name}-*.rpm"))
            example = files[-1] if files else None
        self.gpattern.setText(websources.guess_pattern(name, example) if name else "")
        self.ppattern.setText(websources.guess_link_pattern(name) if name else "")
        self.url.setPlaceholderText("https://…/" + (name or "program") + ".rpm")

    def _find(self):
        name = self.package.currentText().strip()
        if not name:
            return
        self.repo.setPlaceholderText(_("Searching…"))
        run_async(github_guess, name, on_done=self._found)

    def _found(self, hits, err):
        self.repo.setPlaceholderText(_("owner/project on github.com"))
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
        if kind == "github" and not self.gpattern.text().strip():
            self.gpattern.setText(websources.guess_pattern(name))
        if kind == "page" and not self.ppattern.text().strip():
            self.ppattern.setText(websources.guess_link_pattern(name))
        need = {
            "github": "/" in self.repo.text(),
            "url": self.url.text().strip().startswith("https://"),
            "page": self.page.text().strip().startswith("https://"),
            "manual": True,
        }[kind]
        if not name or not need or (kind != "manual" and not self.folder.text().strip()):
            error_box(self, self.windowTitle(), _("Fill in every field. Addresses must start with https://."))
            return
        if kind in ("github", "page"):
            try:
                re.compile(self.gpattern.text() if kind == "github" else self.ppattern.text())
            except re.error as e:
                self.adv.setChecked(True)
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


def fmt_size(n):
    """A size the way the user's language writes it (63,7 MiB in Catalan)."""
    return QLocale().formattedDataSize(int(n or 0))


class TidyDialog(QDialog):
    """Old versions that pile up in the folder of programs from the web, to move to the trash."""

    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Tidy up old versions"))
        self.resize(940, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("Every new version you download stays in the folder. The ticked files are moved to the "
                              "trash, so you can still get them back. The version you have installed always stays.")))
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("File"), _("Version"), _("You have"), _("Size")])
        groups = [
            ("older", _("Older than the version you have"), True),
            ("not-installed", _("Programs you no longer have installed"), False),
        ]
        for state, label, ticked in groups:
            rows = [f for f in files if f["state"] == state]
            if not rows:
                continue
            top = QTreeWidgetItem(self.tree, [f"{label} ({len(rows)})"])
            top.setFirstColumnSpanned(True)
            top.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            for f in sorted(rows, key=lambda r: (r["name"], r["path"])):
                it = QTreeWidgetItem(top, [Path(f["path"]).name, f["version"], f["installed"] or "—",
                                           fmt_size(f["size"])])
                it.setData(0, Qt.ItemDataRole.UserRole, f)
                it.setToolTip(0, f["path"])
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            top.setExpanded(True)
        for c in range(4):
            self.tree.resizeColumnToContents(c)
        lay.addWidget(self.tree, 1)
        self.total = QLabel(self)
        lay.addWidget(self.total)
        self.tree.itemChanged.connect(lambda *_a: self._update_total())  # once the list is filled
        bb = QDialogButtonBox(self)
        self.go = bb.addButton(_("Move to the trash"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setAutoDefault(False)  # Enter must never throw files away: it presses Cancel
        cancel = bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._update_total()

    def ticked(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                if it.checkState(0) == Qt.CheckState.Checked:
                    out.append(it.data(0, Qt.ItemDataRole.UserRole))
        return out

    def _update_total(self):
        files = self.ticked()
        size = sum(f["size"] for f in files)
        self.total.setText(ngettext("{n} file ticked, {size}.", "{n} files ticked, {size}.", len(files)).format(
            n=len(files), size=fmt_size(size)))
        self.go.setEnabled(bool(files))


class WebSourcesPage(Page):
    # what is known about the programs from the web changed (a file was remembered, a source edited,
    # a new version queued): the Updates and Applications pages look again
    sources_changed = Signal()
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
        self._dl_finished = []
        self.aliases = set()
        self.local = {}
        self.folders = {}
        self._round = 0      # each check of every source is a round: late answers of an older one are dropped
        self._inst = {}      # installed version of the packages that are not checked (filled in a thread)
        self._inst_round = 0

        self.folder_lbl = QLabel(self)
        self.folder_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.folder_lbl.setWordWrap(True)
        fbar = QHBoxLayout()
        fbar.addWidget(self.folder_lbl, 1)
        fbar.addWidget(button(_("Change the folder…"), "folder-open", self.change_folder))
        # next to the folder it tidies (in the page header it made the window wider in German and French)
        fbar.addWidget(button(_("Tidy up old versions…"), "edit-clear-history", lambda: self.tidy()))

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("Program"), _("Status"), _("Installed"), _("Latest"),
                                   _("Where it is published")])
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
        self.bridges = BridgesBox(ctx, self)  # hidden while there is no bridge
        lay.addWidget(self.bridges)
        self._show_folder()

    def header_actions(self):
        return [tool(_("Add…"), "list-add", self.add), tool(_("Edit…"), "document-edit", self.edit),
                tool(_("Remove"), "list-remove", self.remove),
                tool(_("Check now"), "view-refresh", lambda: self.check_all(force=True))]

    def reload(self):
        self.sources = websources.load()
        self._inst = {}  # after an Apply the installed versions change
        self._fill()
        self.check_all()
        self._load_installed()
        run_async(local_state, on_done=self._local_loaded)
        self.bridges.load()

    def _load_installed(self):
        """rpm -q for the rows that are not checked (not followed, or made here), never on the GUI thread."""
        names = sorted(set(self.local) | {s["package"] for s in self.sources})
        self._inst_round += 1
        rnd = self._inst_round

        def query():
            return {n: websources.installed_version(n) or "" for n in names}
        run_async(query, on_done=lambda res, err: self._installed_loaded(rnd, res, err))

    def _installed_loaded(self, rnd, res, err):
        if rnd != self._inst_round or err or res is None:
            return
        self._inst = res
        self._fill()

    def _local_loaded(self, res, err):
        if err or not res:
            return
        # the bridge packages live in the same folder, but they are not programs: they have their own list
        self.local = {k: v for k, v in res["packages"].items() if not k.startswith(bridge.PREFIX)}
        self.folders = res["folders"]
        self.aliases = res.get("aliases", set())
        self._show_folder()
        self._fill()
        if set(self.local) - set(self._inst):
            self._load_installed()

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
        if followed and question.ask_change(
                self, ngettext("Save the new version of the program you follow there too?",
                               "Save the new versions of the {n} programs you follow there too?",
                               len(followed)).format(n=len(followed)),
                window_title=self.title, icon_name="folder-download|folder",
                items=[("folder-download|folder", short, _("The folder for the programs from the web."))],
                means=ngettext("the next version of {list} is saved there too. The files you already have stay where "
                               "they are.", "the next versions of {list} are saved there too. The files you already "
                               "have stay where they are.", len(followed)).format(
                    list=", ".join(x["package"] for x in followed)),
                password=False, help_topic="web",
                buttons=[("no", _("Only for new programs"), False), ("yes", _("Yes, for all"), True)]) == "yes":
            for s in self.sources:
                if s.get("type") != "manual":
                    s["folder"] = short
            websources.save(self.sources)
        if not self._repo_of(short) and question.confirm(
                self, _("Add {folder} as a zypper repository?").format(folder=short), _("Add it to the queue"),
                window_title=self.title, icon_name="folder",
                items=[("folder", pgettext("change", "Add the repository “{name}”").format(
                    name=_("Programs from the web")), "dir:" + d)],
                means=_("zypper sees every RPM you keep there, and you can install them by name. Nothing changes "
                        "until you apply the queue."),
                password=False, help_topic="web"):
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
            installed = f.get("installed") or self._inst.get(name, "")
            origin = ""
            if s and s.get("type") != "manual":
                where = websources.origin_text(s)
                origin = KIND_SHORT.get(s.get("type", "github"), "") + " · " + where.removeprefix("github.com/")
            it = QTreeWidgetItem(self.tree, [name, text, installed, f.get("version") or "", origin])
            it.setData(0, Qt.ItemDataRole.UserRole, s)
            it.setData(0, Qt.ItemDataRole.UserRole + 1, name)
            it.setToolTip(1, (s or {}).get("note") or text)
            it.setToolTip(4, websources.origin_text(s) if s else "")
            if color:
                it.setForeground(1, QBrush(QColor(color)))
            if s is None or s.get("type") == "manual":
                for c in (0, 2, 3, 4):
                    it.setForeground(c, QBrush(QColor(GREY)))
            if name == cur_pkg:
                self.tree.setCurrentItem(it)
        for c in range(4):
            self.tree.resizeColumnToContents(c)
        if self.tree.columnWidth(1) > 240:  # a long error stays in the tooltip
            self.tree.setColumnWidth(1, 240)
        if not rows:
            empty = QTreeWidgetItem(self.tree, [_("No program from the web yet. Use “Add…”.")])
            empty.setFirstColumnSpanned(True)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
        elif self.tree.currentItem() is None:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

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
            rows.append((_("Download page"), link(s["page"])))
        else:
            rows += [(_("Note"), e(s.get("note") or "")), (_("Page to look at by hand"), link(s.get("page", "")))]
        if kind != "manual":
            if f.get("url") and kind != "github":
                rows.append((_("File found"), link(f["url"], f.get("asset"))))
            else:
                rows.append((_("File"), e(f.get("asset") or (_("no file of the latest release matches")
                                                             if f and not f.get("error") else ""))))
            rows.append((_("Size"), e(fmt_size(f["size"])) if f.get("size") else ""))
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
        advanced = ""
        if kind in ("github", "page") and s.get("pattern"):
            advanced = (f"<p style='color:gray'>{e(_('Advanced'))} · {e(_('pattern that chooses the file'))}: "
                        f"<code>{e(s['pattern'])}</code></p>")
        self.details.setHtml(f"<h3>{e(name)}</h3><table>" + "".join(
            f"<tr><td style='color:gray;padding-right:12px'>{k}</td><td>{v}</td></tr>" for k, v in rows if v)
            + "</table>" + advanced)
        self.get_btn.setText(_("Download it to see the version") if f.get("newer") is None and f.get("changed")
                             else _("Download, verify and add to the queue"))
        self._update_get_btn()

    def _worth_downloading(self, s, f):
        """A newer version, a changed file whose version is unknown, or a program that is not installed: an
        up-to-date program is not downloaded again (Weasis is 160 MiB)."""
        if not s or s.get("type", "github") == "manual" or not f or f.get("error") or not f.get("url"):
            return False
        return self.counts_as_new(f) or not f.get("installed")

    def _update_get_btn(self):
        s = self._current()
        f = self.found.get(s["package"], {}) if s else {}
        worth = self._worth_downloading(s, f)
        self.get_btn.setEnabled(worth and not self.ctx.runner.busy and not self.downloading)
        if s and f and not f.get("error") and f.get("url") and not worth:
            self.get_btn.setToolTip(_("You already have the latest version."))
        else:
            self.get_btn.setToolTip("")

    # ------------------------------------------------------------ checking
    def check_all(self, force=False):
        self._round += 1
        rnd = self._round
        self.found = {}
        self._fill()
        for s in self.sources:  # the manual ones too: they only ask rpm, but never on the GUI thread
            run_async(websources.check, s, force,
                      on_done=lambda res, err, p=s["package"]: self._checked(rnd, p, res, err))

    def _checked(self, rnd, package, res, err):
        if rnd != self._round:
            return  # an answer of an older round (the source was edited, or checked again meanwhile)
        self.found[package] = res if not err else {"error": str(err)}
        self._fill()
        self._show(self.tree.currentItem())
        self.set_badge_from(list(self.found.values()))

    @staticmethod
    def counts_as_new(found):
        """What every page counts as news from the web: a newer version, or a changed file whose version is
        not written in its address."""
        return bool(found) and not found.get("error") and bool(
            found.get("newer") or (found.get("newer") is None and found.get("changed")))

    def set_badge_from(self, results):
        """The number after “Web sources” in the sidebar, from a list of dicts returned by websources.check
        (the Home and Updates pages call it after their own checks, so it shows before a visit here)."""
        n = sum(1 for f in results if self.counts_as_new(f))
        self.ctx.set_badge("websources", str(n) if n else "")

    # ------------------------------------------------------------ editing
    def _save_source(self, s, old_name=None):
        self.sources = [x for x in self.sources if x["package"] not in (s["package"], old_name)] + [s]
        websources.save(self.sources)
        self.sources_changed.emit()
        self.check_all()

    def add(self, package=None):
        dlg = SourceDialog(self.local, default_folder=self.default_folder(), package=package, parent=self)
        if dlg.exec():
            self._save_source(dlg.source())
        dlg.deleteLater()

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
        dlg.deleteLater()

    def remove(self):
        s = self._current()
        if s and ask(self, _("Remove"), _("Stop looking for new versions of {name}? Nothing is uninstalled.")
                     .format(name=s["package"])):
            self.sources = [x for x in self.sources if x["package"] != s["package"]]
            websources.save(self.sources)
            self._fill()
            self.sources_changed.emit()

    def _remember_file(self, package, fingerprint):
        """Remember the file seen, for addresses that do not say the version."""
        if not fingerprint:
            return
        for x in self.sources:
            if x["package"] == package:
                x["seen"] = fingerprint
        websources.save(self.sources)
        self.sources_changed.emit()

    # ------------------------------------------------------------ old versions
    def tidy(self, packages=None):
        """Offer to move old versions to the trash. With packages (after installing new versions of them), only
        their older versions are offered, and nothing is shown when there are none."""
        if getattr(self, "_tidying", False):
            return
        self._tidying = True
        folders = {s.get("folder") for s in self.sources if s.get("folder")} | {self.default_folder()}

        def scan():
            folders.update(local_state()["folders"])
            return websources.folder_versions(sorted(folders))
        if packages is None:
            self.ctx.terminal.view.note(_("Looking for old versions in {folders}…").format(
                folders=", ".join(sorted(home_short(os.path.expanduser(f)) for f in folders))), "info")
        run_async(scan, on_done=lambda res, err: self._tidy_found(res, err, packages))

    def _tidy_found(self, files, err, packages):
        self._tidying = False
        if err:
            error_box(self, _("Tidy up old versions"), str(err))
            return
        files = files or []
        if packages is not None:
            files = [f for f in files if f["name"] in packages and f["state"] == "older"]
            if not files:
                return
        if not any(f["state"] in ("older", "not-installed") for f in files):
            info_box(self, _("Tidy up old versions"), _("There are no old versions to throw away."))
            return
        dlg = TidyDialog(files, self)
        accepted = dlg.exec()
        ticked = dlg.ticked()
        dlg.deleteLater()
        if not accepted:
            return
        moved, failed = 0, []
        for f in ticked:
            if f["state"] == "installed":
                continue  # never the version you have
            if QFile.moveToTrash(f["path"])[0]:
                moved += 1
            else:
                failed.append(f["path"])
        view = self.ctx.terminal.view
        view.note(ngettext("{n} old file moved to the trash.", "{n} old files moved to the trash.", moved)
                  .format(n=moved), "ok")
        if failed:
            error_box(self, _("Tidy up old versions"), _("These files could not be moved to the trash:") + "\n"
                      + "\n".join(failed))

    # ------------------------------------------------------------ download
    def download(self):
        s = self._current()
        if s:
            self.get_new_versions([s["package"]])

    def get_new_versions(self, names, on_finished=None):
        """Download, check and queue the new version of these programs, one after the other.

        Used by the button here and by the Updates and Applications pages. Each file goes through the same
        checks and questions as always; only one download runs at a time. on_finished() is called once
        nothing is waiting or downloading any more (the Updates page then simulates the queue).
        """
        if on_finished:
            self._dl_finished.append(on_finished)
        queued = {x["package"] for x in self._dl_queue} | ({self._dl_current} if self._dl_current else set())
        for n in names:
            src = next((x for x in self.sources if x["package"] == n and x.get("type", "github") != "manual"), None)
            if src and n not in queued:
                self._dl_queue.append(src)
                queued.add(n)
        self._next_download()

    def _next_download(self):
        if self.downloading or not self._dl_queue:
            self._update_get_btn()
            if not self.downloading and not self._dl_queue:
                done, self._dl_finished = self._dl_finished, []
                for fn in done:
                    fn()
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
        self.ctx.show_terminal()
        self.ctx.terminal.set_title(_("Download {file}").format(file=f.get("asset") or s["package"]))
        view.prompt(f"$ curl -LO {f['url']}")
        view.note(_("Downloading {size}…").format(size=fmt_size(f["size"])) if f.get("size") else _("Downloading…"),
                  "info")
        run_async(websources.download, s, f, on_done=lambda res, err: self._downloaded(s, f, res, err))

    def _downloaded(self, s, f, res, err):
        pending = False
        try:
            pending = self._handle_download(s, f, res, err)
        finally:
            if not pending:  # else the look at what it needs ends it (_bridge_checked)
                self._download_done()

    def _download_done(self):
        self.downloading = False
        self._dl_current = None
        self._next_download()

    def _handle_download(self, s, f, res, err):
        """Check a finished download and, when it is accepted, look at what it needs (in a thread) before it
        goes to the queue. True while that look runs: it ends the download itself."""
        view = self.ctx.terminal.view
        if err:
            view.note(_("Download failed: {error}").format(error=err), "error")
            error_box(self, self.title, str(err))
            return
        view.note(_("Downloaded to {path}").format(path=res["path"]), "ok")
        view.note(_("Version in the file: {version}").format(version=res["version"]), "ok")
        if res["same"]:
            self._drop(res["path"])
            self._remember_file(s["package"], f.get("fingerprint"))
            view.note(_("You already have this version: the file was deleted."), "info")
            info_box(self, self.title, _("{name}: you already have version {version}.").format(
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
            self._drop(res["path"])
            error_box(self, self.title, _("The signature of the file does not verify: it was deleted."))
            return
        if sig.startswith("nokey:"):
            # signed, but with a key rpm does not know: zypper refuses it (--allow-unsigned-rpm is only for
            # packages without any signature). The vendor's key must be imported first.
            self._drop(res["path"])
            error_box(self, self.title, _(
                "{name} {version} is signed with the key {key}, which this computer does not know yet, so zypper "
                "would refuse to install it. Get the vendor's public key from its official web site, check the "
                "fingerprint there, import it (Signing keys → Import a key…) and download it again. "
                "The downloaded file was deleted.").format(name=s["package"], version=res["version"], key=sig[6:]))
            return
        pinned = s.get("signer")
        if sig == "verified" and pinned and res.get("signer") and res["signer"] != pinned:
            # the same program, signed now by another key that rpm also trusts: it may be a renewed key, or not
            if not question.confirm(
                    self, _("{name} {version} is signed with a different key").format(
                        name=s["package"], version=res["version"]), _("Add it to the queue anyway"),
                    window_title=_("A different signing key"), icon_name="security-low|dialog-warning",
                    items=[(RPM_ICON, "{name} {version}".format(name=s["package"], version=res["version"]),
                            _("Signed now with the key {new}; until now it came signed with {old}.").format(
                                new=res["signer"], old=pinned))],
                    warning=_("A vendor sometimes renews its key, but a different key can also mean that the file "
                              "does not come from the vendor. Check the vendor's web site before going on."),
                    means=_("it goes to the queue, and from now on the new key is the one expected for this "
                            "program. Nothing is installed until you apply the queue."),
                    password=False, help_topic="web"):
                self._drop(res["path"])
                view.note(_("Not accepted: the downloaded file was deleted."), "info")
                return
        if sig not in ("verified", "unsigned"):
            # anything else (a damaged or bad signature, an answer rpm never gave before) is never offered
            self._drop(res["path"])
            error_box(self, self.title, _("The signature of the file does not verify: it was deleted."))
            return
        pending = {"version": res["version"], "fingerprint": f.get("fingerprint"), "unsigned": ""}
        if sig == "unsigned":
            # asked once, after the look at what it needs: in the bridge's question when it needs one (visual
            # review 0.5, #7), else in a question of its own
            if res["sha256_checked"]:
                why = _("The file is exactly the one GitHub publishes (SHA-256 checked), but rpm cannot confirm who "
                        "made it.")
            elif s.get("type", "github") == "github":
                why = _("Nothing confirms that the file is the original.")
            else:
                why = _("It was downloaded over https from {host}, but nothing else confirms that the file is the "
                        "original.").format(host=urllib.parse.urlsplit(f["url"]).netloc)
            pending["unsigned"] = why
            allow_unsigned = True
        if sig == "verified" and res.get("signer") and res["signer"] != pinned:
            for x in self.sources:  # remember which key signs this program
                if x["package"] == s["package"]:
                    x["signer"] = res["signer"]
            websources.save(self.sources)
        # The file stays in the private download folder until it is installed, and the queue keeps its
        # SHA-256: Apply checks it again, and only then it moves to your folder (which a cloud may sync).
        path = res["path"]
        action = Action("install", path, package=s["package"], allow_unsigned=allow_unsigned,
                        sha256=res["file_sha256"], move_to=s["folder"], signer=res.get("signer") or "")
        # a program made for Fedora may ask for things by their Fedora names (a bridge package, new or a new
        # release of it): look before it goes to the queue
        view.note(_("Looking at what it needs…"), "info")
        run_async(bridge.analyse_file, path, os.path.expanduser(s["folder"]),
                  on_done=lambda plan, e: self._bridge_checked(s, action, plan, e, pending))
        return True

    @staticmethod
    def _drop(path):
        """Delete a download that was not accepted (it may already be gone: code review 0.5, #12)."""
        try:
            os.unlink(path)
        except OSError:
            pass

    def _bridge_checked(self, s, action, plan, err, pending):
        try:
            self._queue_download(s, action, plan, err, pending)
        finally:
            self._download_done()

    def _ask_unsigned(self, s, pending):
        """The question before a file without a signature goes to the queue (when no bridge question says it)."""
        return question.confirm(
            self, _("{name} {version} is not signed").format(name=s["package"], version=pending["version"]),
            _("Add it to the queue anyway"), window_title=_("Package not verified by rpm"),
            icon_name="security-low|dialog-warning",
            items=[(RPM_ICON, "{name} {version}".format(name=s["package"], version=pending["version"]),
                    _("From {where}.").format(where=websources.origin_text(s)))],
            warning=pending["unsigned"],
            means=_("it only goes to the queue: nothing is installed until you apply it."),
            password=False, help_topic="web")

    def _queue_download(self, s, action, plan, err, pending):
        view = self.ctx.terminal.view
        path = action.target
        warnings = [_("It is not signed.") + " " + pending["unsigned"]] if pending["unsigned"] else []
        if err or not plan:
            # the look failed (rpm or zypper): the program goes to the queue as before, Simulate tells the rest
            view.note(_("Could not look at what it needs: {error}").format(error=err or "?"), "error")
        elif plan.get("needs_bridge") or plan.get("unmapped"):
            info = plan.get("file") or {"name": s["package"]}
            if plan.get("unmapped"):
                if not ask_unmapped(self, plan, info, warnings=warnings):
                    self._drop(path)
                    view.note(_("Not accepted: the downloaded file was deleted."), "info")
                    return
            elif not ask_bridge(self, plan, info, action, self.ctx.settings, s["folder"], warnings=warnings):
                self._drop(path)
                view.note(_("Not accepted: the downloaded file was deleted."), "info")
                return
            else:
                try:
                    written = queue_bridge(self.ctx, plan, action, os.path.expanduser(s["folder"]))
                except (OSError, ValueError) as e:
                    self._drop(path)
                    error_box(self, self.title, _("The bridge package could not be written: {error}").format(
                        error=e))
                    return
                self._remember_file(s["package"], pending["fingerprint"])
                view.note(_("Bridge package written: {path}").format(path=written), "ok")
                self.sources_changed.emit()
                view.note(_("Added to the queue with its bridge. Simulate it, then apply it."), "info")
                return
            warnings = []  # said in the question above
        if warnings and not self._ask_unsigned(s, pending):
            self._drop(path)
            view.note(_("Not accepted: the downloaded file was deleted."), "info")
            return
        self._remember_file(s["package"], pending["fingerprint"])
        view.note(_("Ready to install: {path}. After installing, it moves to {folder}.").format(
            path=path, folder=s["folder"]), "ok")
        self.ctx.queue.add(action)
        self.sources_changed.emit()
        view.note(_("Added to the queue. Simulate it, then apply it."), "info")
