# SPDX-License-Identifier: GPL-3.0-or-later
"""Packages: search with every zypper option, the package card, and adding changes to the queue."""


from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import appicons, backend
from ..style import BLUE, GREEN, RED, dot
from PySide6.QtGui import QIcon
from ..i18n import _, ngettext
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Chip, Page, button, icon, muted
from .pkgdetails import PackageDetails

KIND_LABELS = [("package", _("Packages")), ("pattern", _("Patterns")), ("patch", _("Patches")),
               ("product", _("Products")), ("srcpackage", _("Source packages"))]
STATUS_LABELS = [("all", _("Any status")), ("installed", _("Installed")), ("not-installed", _("Not installed"))]


class PackagesPage(Page):
    key = "packages"
    title = _("Packages")
    icon_name = "package-x-generic"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.results = []
        self.search_id = 0
        self.locked_names = set()
        self.updates = {}
        self.desktop = {}
        self._icons = {}

        self.term = QLineEdit(self)
        self.term.setPlaceholderText(_("Search packages, for example vlc"))
        self.term.setClearButtonEnabled(True)
        self.term.returnPressed.connect(self.search)
        self.kind = QComboBox(self)
        for k, label in KIND_LABELS:
            self.kind.addItem(label, k)
        self.status = QComboBox(self)
        for k, label in STATUS_LABELS:
            self.status.addItem(label, k)
        self.repo = QComboBox(self)
        self.repo.addItem(_("All repositories"), "")
        search_btn = button(_("Search"), "edit-find", self.search)
        self.online_btn = button(_("Search online…"), "internet-services", self.search_online)
        self.online_btn.setToolTip(_("Look for it in the openSUSE Build Service, in Fedora COPR and on Flathub"))

        top = QHBoxLayout()
        top.addWidget(self.term, 1)
        for w in (self.kind, self.status, self.repo, search_btn, self.online_btn):
            top.addWidget(w)

        self.chips = {
            "descriptions": Chip(_("Summary and description")),
            "provides": Chip(_("Provides")),
            "requires": Chip(_("Requires")),
            "recommends": Chip(_("Recommends")),
            "files": Chip(_("Files")),
            "exact": Chip(_("Exact match")),
            "case": Chip(_("Case-sensitive")),
        }
        for key, tip in (("descriptions", _("Also look in the summary and the description, not only the name")),
                         ("provides", _("Packages that provide this name (a library, a command, a capability)")),
                         ("requires", _("Packages that need this name")),
                         ("recommends", _("Packages that recommend this name")),
                         ("files", _("Packages that contain a file with this name or path")),
                         ("exact", _("Only the exact name, not names that contain it")),
                         ("case", _("Tell capitals and small letters apart"))):
            self.chips[key].setToolTip(tip)
        chips = QHBoxLayout()
        chips.addWidget(muted(_("Search in:"), wrap=False))
        for c in self.chips.values():
            chips.addWidget(c)
        chips.addStretch(1)
        lists = QHBoxLayout()
        lists.addWidget(muted(_("Lists:"), wrap=False))
        tips = {"orphaned": _("Orphaned packages (installed, but in no repository)"),
                "unneeded": _("Unneeded packages (nothing requires them any more)"),
                "recommended": _("Recommended packages that are not installed"),
                "suggested": _("Suggested packages that are not installed")}
        for key, label in (("orphaned", _("Orphaned")), ("unneeded", _("Unneeded")),
                           ("recommended", _("Recommended")), ("suggested", _("Suggested"))):
            b = Chip(label)
            b.setToolTip(tips[key])
            b.setCheckable(False)
            b.clicked.connect(lambda _c=False, k=key: self.search(special=k))
            lists.addWidget(b)
        lists.addStretch(1)

        self.info = muted("", self, wrap=False)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("Status"), _("Name"), _("Summary"), _("Version"), _("Repository")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(22, 22))
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self.show_details(cur))
        self.tree.setColumnWidth(0, 125)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 250)
        self.tree.setColumnWidth(3, 150)

        self.details = PackageDetails(self, ctx, self)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.tree)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(chips)
        lay.addLayout(lists)
        lay.addWidget(self.info)
        lay.addWidget(split, 1)

        # the queue changes often (and in bursts): repaint once, later, and only if this page is in front
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.update_statuses)
        ctx.queue.changed.connect(self._queue_changed)
        # details are asked for only when the selection stops moving (arrow keys would start dozens of queries)
        self._details_timer = QTimer(self)
        self._details_timer.setSingleShot(True)
        self._details_timer.setInterval(200)
        self._details_timer.timeout.connect(lambda: self._load_details(self.tree.currentItem()))
        self._statuses_stale = False
        self._dots = {}

    # ------------------------------------------------------------ data
    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)
        run_async(backend.locks, on_done=self._locks_loaded)
        run_async(backend.list_updates, on_done=self._updates_loaded)

    def _updates_loaded(self, updates, err):
        if err:
            return
        self.updates = {u["name"]: u for u in updates}
        self.update_statuses()

    def _updatable(self, d):
        # Qt hands back a copy of the dict stored in an item, so this is computed, never stored.
        return bool(d and d["installed"] and d["name"] in self.updates)

    def _repos_loaded(self, repos, err):
        if err:
            return
        cur = self.repo.currentData()
        self.repo.blockSignals(True)
        self.repo.clear()
        self.repo.addItem(_("All repositories"), "")
        for r in sorted((r for r in repos if r["enabled"]), key=lambda r: r["name"].lower()):
            self.repo.addItem(r["name"], r["alias"])
        i = self.repo.findData(cur)
        self.repo.setCurrentIndex(max(i, 0))
        self.repo.blockSignals(False)

    def _locks_loaded(self, locks, err):
        if not err:
            self.locked_names = {lk["name"] for lk in locks}
            self.update_statuses()

    def search(self, special=None):
        term = self.term.text().strip()
        if not term and not special:
            self.term.setFocus()
            return
        self.search_id += 1
        sid = self.search_id
        opts = {k: c.isChecked() for k, c in self.chips.items()}
        self.info.setText(_("Searching…"))
        self.tree.clear()
        run_async(backend.search, term, kind=self.kind.currentData(), status=self.status.currentData(),
                  repo=self.repo.currentData() or None, special=special, **opts,
                  on_done=lambda res, err: self._searched(sid, res, err, special))

    def search_online(self):
        try:
            from .findrepo import FindRepoDialog
        except ImportError:
            return
        FindRepoDialog(self.ctx, term=self.term.text().strip(), parent=self).exec()

    def _searched(self, sid, results, err, special):
        if sid != self.search_id:
            return  # an older search finished late
        if err:
            self.info.setText(_("The search failed: {error}").format(error=str(err)[:200]))
            return
        self.results = results
        what = {"orphaned": _("Orphaned packages (installed, but in no repository)"),
                "unneeded": _("Unneeded packages (nothing requires them any more)"),
                "recommended": _("Recommended packages that are not installed"),
                "suggested": _("Suggested packages that are not installed")}.get(special)
        n = len(results)
        self.info.setText((what + " · " if what else "") + ngettext("{n} found", "{n} found", n).format(n=n))
        term = self.term.text().strip().lower()
        # the package with exactly the name you typed first ("vlc" before "libextractor-plugins-vlc")
        results = sorted(results, key=lambda d: d["name"].lower() != term)
        missing = n == 0 and not special
        if missing:
            self.info.setText(_("Not found in your repositories. “Search online…” looks for it in the openSUSE Build "
                                "Service, in Fedora COPR and on Flathub."))
        self.online_btn.setProperty("primary", missing)
        self.online_btn.style().unpolish(self.online_btn)
        self.online_btn.style().polish(self.online_btn)
        self.tree.setSortingEnabled(False)
        for d in results:
            ref = d["installed"] or d["best"] or (d["versions"][0] if d["versions"] else None)
            it = QTreeWidgetItem(self.tree, ["", d["name"], d["summary"], ref["edition"] if ref else "",
                                             ref["repo"] if ref else ""])
            it.setData(0, Qt.ItemDataRole.UserRole, d)
            it.setIcon(1, self.pkg_icon(d))
            if len(d["versions"]) > 1:
                it.setToolTip(3, _("{n} versions available").format(n=len(d["versions"])))
            self._paint_status(it, d)
        # keep the order of the search (the exact name first); clicking a header sorts as usual
        self.tree.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.tree.setSortingEnabled(True)
        if n:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.details.clear()
        names = [d["name"] for d in results if d["installed"] and d["name"] not in self.desktop][:400]
        if names:
            run_async(appicons.desktop_icons, names, on_done=self._desktop_icons_loaded)

    def _desktop_icons_loaded(self, icons, err):
        if icons:
            self.desktop.update(icons)
            self.refresh_icons()

    def pkg_icon(self, d):
        name = d["name"]
        if name in self._icons:
            return self._icons[name]
        ic = QIcon()
        found = self.ctx.icon_catalog.get(name)
        if found and found[0] == "file":
            ic = QIcon(found[1])
        elif found and found[0] == "theme":
            ic = QIcon.fromTheme(found[1])
        if ic.isNull() and name in self.desktop:
            ic = QIcon.fromTheme(self.desktop[name])
        if ic.isNull():
            generic = "application-x-sharedlib" if name.startswith("lib") else "package-x-generic"
            return icon(generic, "package-x-generic")  # not cached: a better icon may appear later
        self._icons[name] = ic
        return ic

    def refresh_icons(self):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setIcon(1, self.pkg_icon(it.data(0, Qt.ItemDataRole.UserRole)))
        self.details.refresh_icon()

    def _queue_changed(self):
        if self.isVisible():
            self._status_timer.start(0)
        else:
            self._statuses_stale = True

    def activated(self):
        if self._statuses_stale:
            self._statuses_stale = False
            self._status_timer.start(0)
        super().activated()

    def update_statuses(self):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            self._paint_status(it, it.data(0, Qt.ItemDataRole.UserRole))
        self.details.refresh()

    def _status_text(self, d):
        return self.details.status_text(d)

    def _paint_status(self, it, d):
        text, color, ic = self._status_text(d)
        it.setText(0, text)
        dots = {"checkmark": GREEN, "update-low": BLUE, "list-add": GREEN, "list-remove": RED}
        if ic in dots:
            if dots[ic] not in self._dots:
                self._dots[dots[ic]] = dot(dots[ic])
            it.setIcon(0, self._dots[dots[ic]])
        else:
            it.setIcon(0, icon(ic) if ic else QIcon())
        brush = QBrush(QColor(color)) if color else QBrush()
        it.setForeground(0, brush)
        f = it.font(0)
        f.setBold(bool(color and color != "#1d99f3"))
        it.setFont(0, f)

    # ------------------------------------------------------------ details
    def show_details(self, item):
        if not item:
            return
        self._details_timer.start()  # the queries wait until the selection stops moving

    def _load_details(self, item):
        d = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if d:
            self.details.load(d)

    @property
    def current(self):
        return self.details.current
