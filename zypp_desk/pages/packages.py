# SPDX-License-Identifier: GPL-3.0-or-later
"""Packages: search with every zypper option, the package card, and adding changes to the queue."""


from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .. import appicons, backend
from ..i18n import _, ngettext
from ..style import BLUE, GREEN, ORANGE, RED, dot
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Chip, FlowLayout, Page, RoomSplitter, button, icon, muted, tool
from .markbar import MarkBar, amp, is_checked
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
        self._last_special = None  # the list shown last (orphaned, unneeded…), None for a search by name
        self._searched_once = False
        self._reselect = None  # after looking again: the package that was selected stays selected
        self.locked_names = set()
        self.updates = {}
        self.desktop = {}
        self._icons = {}

        self.term = QLineEdit(self)
        self.term.setPlaceholderText(_("Search packages, for example vlc"))
        self.term.setClearButtonEnabled(True)
        self.term.setMinimumWidth(150)
        self.term.returnPressed.connect(self.search)
        self.kind = QComboBox(self)
        for k, label in KIND_LABELS:
            self.kind.addItem(label, k)
        self.status = QComboBox(self)
        for k, label in STATUS_LABELS:
            self.status.addItem(label, k)
        self.repo = QComboBox(self)
        self.repo.addItem(_("All repositories"), "")
        # the combos give way when the window is narrow (a long repository name, German words): the whole
        # text is in the open list and in the tooltip
        for combo in (self.kind, self.status, self.repo):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(6)
            combo.currentTextChanged.connect(lambda text, c=combo: c.setToolTip(text))
            combo.setToolTip(combo.currentText())
        self.search_btn = button(_("Search"), "edit-find", self.search)
        self.search_btn.setToolTip(_("Look for this text in your repositories, with the options below (zypper "
                                     "search). It only shows: nothing is installed or removed."))
        self.online_btn = button(_("Search online…"), "internet-services", self.search_online)
        self.online_btn.setToolTip(_("Look for it in the openSUSE Build Service, in Fedora COPR and on Flathub"))

        top = QHBoxLayout()
        top.addWidget(self.term, 3)
        for w, stretch in ((self.kind, 1), (self.status, 1), (self.repo, 2), (self.search_btn, 0),
                           (self.online_btn, 0)):
            top.addWidget(w, stretch)

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
        # the chips wrap onto a second line when the window is narrow
        chips_box = QWidget(self)
        chips = FlowLayout(chips_box)
        chips.addWidget(self._flow_label(_("Search in:")))
        for c in self.chips.values():
            chips.addWidget(c)
        lists_box = QWidget(self)
        lists = FlowLayout(lists_box)
        lists.addWidget(self._flow_label(_("Lists:")))
        # a list is a view, like a filter: its chip stays lit while the list is shown (a search by name puts
        # them all out), so that they do not look like buttons that change something
        tips = {"orphaned": _("Show the orphaned packages: installed, but in no repository (zypper packages "
                              "--orphaned). It only shows them."),
                "unneeded": _("Show the unneeded packages: nothing requires them any more (zypper packages "
                              "--unneeded). It only shows them."),
                "recommended": _("Show the recommended packages that are not installed (zypper packages "
                                 "--recommended). It only shows them."),
                "suggested": _("Show the suggested packages that are not installed (zypper packages --suggested). "
                               "It only shows them.")}
        self.list_chips = {}
        for key, label in (("orphaned", _("Orphaned")), ("unneeded", _("Unneeded")),
                           ("recommended", _("Recommended")), ("suggested", _("Suggested"))):
            b = Chip(label)
            b.setToolTip(tips[key])
            b.clicked.connect(lambda _c=False, k=key: self.search(special=k))
            self.list_chips[key] = b
            lists.addWidget(b)

        self.info = muted("", self, wrap=False)
        # mark several and do the same with all of them
        self.mark_all_btn = tool(_("Mark all"), "edit-select-all", self.toggle_mark_all)
        self.mark_all_btn.setToolTip(_("Tick every package in the list, to do the same with all of them. Nothing "
                                       "changes until you choose what to do with them."))
        self.mark_all_btn.setVisible(False)

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
        self.markbar = MarkBar(self.mark_action, self.unmark_all, self, to_queue=True)
        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        lv.addWidget(self.tree, 1)
        lv.addWidget(self.markbar)
        self._mark_timer = QTimer(self)  # "Mark all" ticks hundreds of rows: count them once
        self._mark_timer.setSingleShot(True)
        self._mark_timer.timeout.connect(self._update_markbar)
        self.tree.itemChanged.connect(lambda _it, col: col == 0 and self._mark_timer.start(0))
        split = RoomSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(left)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(chips_box)
        lay.addWidget(lists_box)
        info_row = QHBoxLayout()
        info_row.addWidget(self.info, 1)
        info_row.addWidget(self.mark_all_btn)
        lay.addLayout(info_row)
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

    @staticmethod
    def _flow_label(text):
        """The grey word before a row of chips, as tall as a chip so it stays in line with them."""
        lbl = muted(text, wrap=False)
        probe = Chip("x")
        probe.ensurePolished()
        lbl.setMinimumHeight(probe.sizeHint().height())
        probe.deleteLater()
        lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return lbl

    # ------------------------------------------------------------ data
    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)
        run_async(backend.locks, on_done=self._locks_loaded)
        run_async(backend.list_updates, on_done=self._updates_loaded)
        self.search_again()

    def search_again(self):
        """After a change (Apply, the routine…): the same search or list again, so that a package just removed or
        installed shows as it is now, and nothing stays ticked. The selected package stays selected."""
        if not self._searched_once:
            return
        cur = self.tree.currentItem()
        d = cur.data(0, Qt.ItemDataRole.UserRole) if cur is not None else None
        self._reselect = d["name"] if d else None
        self.search(special=self._last_special)

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
        for k, c in self.list_chips.items():  # the list shown is lit; a search by name puts them out
            c.setChecked(k == special)
        self.search_id += 1
        sid = self.search_id
        self._last_special = special
        self._searched_once = True
        opts = {k: c.isChecked() for k, c in self.chips.items()}
        self.info.setText(_("Searching…"))
        self.tree.clear()
        self.mark_all_btn.setVisible(False)
        self._update_markbar()
        run_async(backend.search, term, kind=self.kind.currentData(), status=self.status.currentData(),
                  repo=self.repo.currentData() or None, special=special, **opts,
                  on_done=lambda res, err: self._searched(sid, res, err, special))

    def search_online(self):
        try:
            from .findrepo import FindRepoDialog
        except ImportError:
            return
        dlg = FindRepoDialog(self.ctx, term=self.term.text().strip(), parent=self)
        dlg.exec()
        dlg.deleteLater()

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
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Unchecked)
            it.setIcon(1, self.pkg_icon(d))
            if len(d["versions"]) > 1:
                it.setToolTip(3, _("{n} versions available").format(n=len(d["versions"])))
            self._paint_status(it, d)
        self.mark_all_btn.setVisible(n > 0)
        self._update_markbar()
        # keep the order of the search (the exact name first); clicking a header sorts as usual
        self.tree.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.tree.setSortingEnabled(True)
        again, self._reselect = self._reselect, None
        if n:
            first = self.tree.topLevelItem(0)
            if again:
                first = next((self.tree.topLevelItem(i) for i in range(n)
                              if self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)["name"] == again), first)
            self.tree.setCurrentItem(first)
        else:
            self.details.clear(_("Nothing was found."))
        names = [d["name"] for d in results if d["installed"] and d["name"] not in self.desktop][:400]
        if names:
            run_async(appicons.desktop_icons, names, on_done=self._desktop_icons_loaded)
        if special == "orphaned" and results:
            # "orphaned" only means that no repository has it any more: something installed may still need it
            # (removing it would take that away too), so those cannot be ticked
            run_async(backend.needed_by, [d["name"] for d in results if d["installed"]],
                      on_done=lambda res, err, s=sid: self._orphans_needed(s, res or {}))

    def _orphans_needed(self, sid, needed):
        if sid != self.search_id or not needed:
            return
        n = 0
        self.tree.blockSignals(True)
        try:
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                d = it.data(0, Qt.ItemDataRole.UserRole) or {}
                who = needed.get(d.get("name"))
                if not who:
                    continue
                n += 1
                it.setCheckState(0, Qt.CheckState.Unchecked)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                it.setData(0, Qt.ItemDataRole.CheckStateRole, None)  # no box: it cannot be removed from here
                more = len(who) - 3
                shown = ", ".join(who[:3]) + (" " + ngettext("and {n} more", "and {n} more", more).format(n=more)
                                              if more > 0 else "")
                it.setText(2, _("Still needed by {names}").format(names=shown))
                it.setForeground(2, QColor(ORANGE))
                tip = _("No repository has {name} any more, but these installed packages still need it: {names}. "
                        "Removing it would remove them too. It usually stops being needed after the next update of "
                        "the system.").format(name=d.get("name"), names=", ".join(who[:30]))
                for col in range(self.tree.columnCount()):
                    it.setToolTip(col, tip)
        finally:
            self.tree.blockSignals(False)
        if n:
            self.info.setText(self.info.text() + " · " + ngettext(
                "{n} is still needed by other packages: it cannot be ticked",
                "{n} are still needed by other packages: they cannot be ticked", n).format(n=n))
        self._update_markbar()

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

    # ------------------------------------------------------------ marking several
    def _items(self):
        """The rows that can be ticked (an orphan something installed still needs cannot)."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                out.append(it)
        return out

    def marked(self):
        """The packages ticked in the list (their dicts)."""
        return [it.data(0, Qt.ItemDataRole.UserRole) for it in self._items()
                if is_checked(it.checkState(0)) and it.data(0, Qt.ItemDataRole.UserRole)]

    def _set_all(self, on):
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.tree.blockSignals(True)  # one count at the end, not one per row
        try:
            for it in self._items():
                it.setCheckState(0, state)
        finally:
            self.tree.blockSignals(False)
        self.tree.viewport().update()
        self._update_markbar()

    def toggle_mark_all(self):
        items = self._items()
        self._set_all(not (items and all(is_checked(it.checkState(0)) for it in items)))

    def unmark_all(self):
        self._set_all(False)

    def _update_markbar(self):
        marked = self.marked()
        n = len(marked)
        items = self._items()
        self.mark_all_btn.setText(_("Unmark all") if items and n == len(items) else _("Mark all"))
        to_install = [d for d in marked if not d["installed"]]
        have = [d for d in marked if d["installed"]]
        lockable = [d for d in marked if d["kind"] == "package" and d["name"] not in self.locked_names]
        parts = []
        if to_install:
            parts.append(ngettext("{n} to install", "{n} to install", len(to_install)).format(n=len(to_install)))
        if have:
            parts.append(ngettext("{n} you have", "{n} you have", len(have)).format(n=len(have)))
        # only what applies to the ticked ones shows (never "Install the 0"); each says what it acts on, and
        # none is green: they only add to the queue ("To the queue:" in front of them)
        actions = []
        for key, group, cmd in (("lock", lockable, "zypper addlock"), ("remove", have, "zypper remove"),
                                ("install", to_install, "zypper install")):
            if group:
                actions.append((key, self._mark_label(key, group, n), True, False, self._mark_tip(key, group, cmd)))
        self.markbar.set_state(n, ngettext("{n} marked", "{n} marked", n).format(n=n), ", ".join(parts), actions)

    @staticmethod
    def _mark_label(key, group, n):
        """'Install vlc' for one, 'Install the 3 ticked' when it is every ticked one, 'Install the 2 not installed'
        when it is only some of them."""
        k = len(group)
        if k == 1 and len(group[0]["name"]) <= 24:
            return {"install": _("Install {name}"), "remove": _("Remove {name}"),
                    "lock": _("Lock {name}")}[key].format(name=amp(group[0]["name"]))
        if k == n:
            return {"install": ngettext("Install the ticked one", "Install the {n} ticked", k),
                    "remove": ngettext("Remove the ticked one", "Remove the {n} ticked", k),
                    "lock": ngettext("Lock the ticked one", "Lock the {n} ticked", k)}[key].format(n=k)
        return {"install": ngettext("Install the one not installed", "Install the {n} not installed", k),
                "remove": ngettext("Remove the one installed", "Remove the {n} installed", k),
                "lock": ngettext("Lock the one not locked", "Lock the {n} not locked", k)}[key].format(n=k)

    @staticmethod
    def _mark_tip(key, group, cmd):
        targets = [d["name"] if d["kind"] == "package" else f"{d['kind']}:{d['name']}" for d in group]
        names = ", ".join(d["name"] for d in group[:3]) + (", …" if len(group) > 3 else "")
        command = " ".join([cmd] + targets[:4]) + (" …" if len(targets) > 4 else "")
        k = len(group)
        if key == "install":
            text = _("Puts {names} in the queue to be installed ({command}).")
        elif key == "remove":
            text = ngettext("Puts the removal of {names} in the queue ({command}). Simulate shows what else would "
                            "go with it.",
                            "Puts the removal of {names} in the queue ({command}). Simulate shows what else would "
                            "go with them.", k)
        else:
            text = ngettext("Puts a lock on {names} in the queue ({command}): then zypper does not install, update "
                            "or remove it until you unlock it.",
                            "Puts a lock on {names} in the queue ({command}): then zypper does not install, update "
                            "or remove them until you unlock them.", k)
        return text.format(names=names, command=command) + " " + _("Nothing changes until you apply the queue.")

    def mark_action(self, key):
        """Every marked package to the queue: install the ones you do not have, remove or lock. Nothing changes until
        the queue is applied, as with one package."""
        marked = self.marked()
        with self.ctx.queue.batch():
            for d in marked:
                target = d["name"] if d["kind"] == "package" else f"{d['kind']}:{d['name']}"
                if key == "install" and not d["installed"]:
                    self.ctx.queue.add(Action("install", target, package=d["name"]))
                elif key == "remove" and d["installed"]:
                    self.ctx.queue.add(Action("remove", target, package=d["name"]))
                elif key == "lock" and d["kind"] == "package" and d["name"] not in self.locked_names:
                    self.ctx.queue.add(Action("lock", d["name"]))
        self.unmark_all()

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
