# SPDX-License-Identifier: GPL-3.0-or-later
"""Home, locks, services, languages, history, maintenance and settings."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QScrollArea, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, routine
from ..i18n import _
from ..runner import Step
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Card, Page, ask, button, error_box, heading, muted, tool


def kind_label(kind):
    return {"package": _("package"), "pattern": _("pattern"), "patch": _("patch"), "product": _("product"),
            "srcpackage": _("source package")}.get(kind, kind)


def simple_tree(headers, parent):
    t = QTreeWidget(parent)
    t.setHeaderLabels(headers)
    t.setRootIsDecorated(False)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(True)
    return t


# ============================================================ locks
class LocksPage(Page):
    key = "locks"
    title = _("Locks")
    icon_name = "object-locked"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Name"), _("Type"), _("Repository"), _("Pending")], self)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("A locked package is never installed, removed or updated by any zypper command "
                              "until you unlock it.")))
        lay.addWidget(self.tree, 1)
        self.items = []
        ctx.queue.changed.connect(self._fill)

    def header_actions(self):
        return [tool(_("Lock…"), "list-add", self.add), tool(_("Unlock"), "list-remove", self.remove),
                tool(_("Clean useless locks"), "edit-clear",
                     lambda: self.ctx.run([Step("zypper", ["cleanlocks"])], _("Clean useless locks"),
                                          on_done=lambda ok: self.reload()))]

    def reload(self):
        run_async(backend.locks, on_done=self._loaded)

    def _loaded(self, items, err):
        if err:
            error_box(self, self.title, str(err))
            return
        self.items = items
        self._fill()

    def _fill(self):
        self.tree.clear()
        names = set()
        for lk in self.items:
            names.add(lk["name"])
            pend = _("will be unlocked") if any(a.target == lk["name"] for a in self.ctx.queue.of("unlock")) else ""
            it = QTreeWidgetItem(self.tree, [lk["name"], kind_label(lk["type"]), lk["repo"], pend])
            it.setData(0, Qt.ItemDataRole.UserRole, lk["type"])
            it.setData(1, Qt.ItemDataRole.UserRole, lk["repo"])
        for a in self.ctx.queue.of("lock"):
            if a.target not in names:
                QTreeWidgetItem(self.tree, [a.target, kind_label("package"), "", _("will be locked")])
        self.tree.resizeColumnToContents(0)

    def add(self):
        name, ok = QInputDialog.getText(self, _("Lock"), _("Name of the package (wildcards like vlc-* work):"))
        if ok and name.strip():
            self.ctx.queue.add(Action("lock", name.strip()))

    def remove(self):
        it = self.tree.currentItem()
        if it:
            # a lock limited to a repository is only removed with that repository named (-r)
            self.ctx.queue.add(Action("unlock", it.text(0), type=it.data(0, Qt.ItemDataRole.UserRole) or "package",
                                      repo=it.data(1, Qt.ItemDataRole.UserRole) or ""))


# ============================================================ services
class ServicesPage(Page):
    key = "services"
    title = _("Services")
    icon_name = "network-server"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Name"), _("Enabled"), _("Refresh"), _("Type"), _("Repositories"),
                                 _("Address")], self)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("A service is a list of repositories kept up to date by its provider (openSUSE's "
                              "own repositories come from one).")))
        lay.addWidget(self.tree, 1)

    def header_actions(self):
        return [tool(_("Refresh services"), "view-refresh",
                     lambda: self.ctx.run([Step("zypper", ["-n", "-v", "refresh-services"])], _("Refresh services"),
                                          on_done=lambda ok: self.reload())),
                tool(_("Enable / disable"), "system-run", self.toggle)]

    def reload(self):
        run_async(backend.services, on_done=self._loaded)

    def _loaded(self, items, err):
        if err:
            error_box(self, self.title, str(err))
            return
        self.tree.clear()
        for s in items:
            it = QTreeWidgetItem(self.tree, [s["name"], _("yes") if s["enabled"] else _("no"),
                                             _("yes") if s["autorefresh"] else _("no"), s["type"],
                                             str(len(s["repos"])), s["url"]])
            it.setData(0, Qt.ItemDataRole.UserRole, s)
        for c in range(5):
            self.tree.resizeColumnToContents(c)

    def toggle(self):
        it = self.tree.currentItem()
        if not it:
            return
        s = it.data(0, Qt.ItemDataRole.UserRole)
        flag = "--disable" if s["enabled"] else "--enable"
        if ask(self, self.title, _("Run 'zypper modifyservice {flag} {alias}'?").format(flag=flag, alias=s["alias"])):
            self.ctx.run([Step("zypper", ["modifyservice", flag, s["alias"]])], _("Change a service"),
                         on_done=lambda ok: self.reload())


# ============================================================ languages
class LocalesPage(Page):
    key = "locales"
    title = _("Languages")
    icon_name = "preferences-desktop-locale"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Code"), _("Language"), _("Pending")], self)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("Requested languages make zypper install the translation packages (-lang) of the "
                              "programs you have.")))
        lay.addWidget(self.tree, 1)
        self.items = []
        ctx.queue.changed.connect(self._fill)

    def header_actions(self):
        return [tool(_("Add…"), "list-add", self.add), tool(_("Remove"), "list-remove", self.remove)]

    def reload(self):
        run_async(backend.locales, on_done=self._loaded)

    def _loaded(self, items, err):
        if not err:
            self.items = items
            self._fill()

    def _fill(self):
        self.tree.clear()
        for lc in self.items:
            pend = _("will be removed") if any(a.target == lc["code"] for a in self.ctx.queue.of("locale-remove")) else ""
            QTreeWidgetItem(self.tree, [lc["code"], lc["name"], pend])
        for a in self.ctx.queue.of("locale-add"):
            QTreeWidgetItem(self.tree, [a.target, "", _("will be added")])
        if not self.items and not self.ctx.queue.of("locale-add"):
            empty = QTreeWidgetItem(self.tree, [_("No language has been requested.")])
            empty.setFirstColumnSpanned(True)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)

    def add(self):
        code, ok = QInputDialog.getText(self, _("Add a language"), _("Language code, for example ca or ca_ES:"))
        if ok and code.strip():
            self.ctx.queue.add(Action("locale-add", code.strip()))

    def remove(self):
        it = self.tree.currentItem()
        if it and it.text(0):
            self.ctx.queue.add(Action("locale-remove", it.text(0)))


# ============================================================ history
class HistoryPage(Page):
    key = "history"
    title = _("History")
    icon_name = "view-history"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Date"), _("Action"), _("Package"), _("Version"), _("Details")], self)
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText(_("Filter"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("zypper's history file (/var/log/zypp/history) can only be read by root, so loading "
                              "it asks for the password.")))
        lay.addWidget(self.filter)
        lay.addWidget(self.tree, 1)
        self.loaded = True  # loading needs the password: only on request

    def header_actions(self):
        return [tool(_("Load the history"), "view-refresh", self.load)]

    def load(self):
        st = Step("history", [], capture=True)
        self.ctx.run([st], _("Read the history"), on_done=lambda ok: self._loaded(st))

    def _loaded(self, st):
        if st.code != 0:
            return  # the password was not given or reading failed: keep what is shown
        rows = backend.parse_history(st.output)
        labels = {"install": _("installed"), "remove": _("removed"), "command": _("command"),
                  "radd": _("repository added"), "rremove": _("repository removed")}
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for r in reversed(rows[-5000:]):
            QTreeWidgetItem(self.tree, [r["date"], labels.get(r["action"], r["action"]), r["name"], r["edition"],
                                        r["detail"]])
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        for c in range(4):
            self.tree.resizeColumnToContents(c)

    def _apply_filter(self, text):
        t = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setHidden(bool(t) and t not in " ".join(it.text(c) for c in range(5)).lower())


# ============================================================ maintenance
MAINTENANCE = [
    ("verify", _("Check dependencies"),
     _("Looks for installed packages whose dependencies are broken and proposes how to fix them."),
     ["verify", "--dry-run"], ["-n", "verify"]),
    ("inr", _("Install new recommended packages"),
     _("Installs packages recommended by what you have, added since it was installed."),
     ["install-new-recommends", "--dry-run"], ["-n", "install-new-recommends"]),
    ("purge", _("Remove old kernels"),
     _("Keeps the kernels set in zypp.conf (multiversion.kernels; on Tumbleweed /usr/etc/zypp/zypp.conf and "
       "/etc/zypp/zypp.conf.d) and removes the rest."),
     ["purge-kernels", "--dry-run"], ["-n", "purge-kernels"]),
    ("clean", _("Clean the cache"),
     _("Deletes downloaded packages and cached metadata. They are downloaded again when needed."),
     None, ["clean", "--all"]),
    ("cleanlocks", _("Clean useless locks"),
     _("Removes locks that no longer match any package."), None, ["cleanlocks"]),
    ("ps", _("Programs using deleted files"),
     _("Programs that still use libraries replaced by an update and should be restarted."), None, ["ps", "-s"]),
    ("reboot", _("Is a restart needed?"),
     _("Asks zypper whether an update requires restarting the computer."), None, ["needs-rebooting"]),
    ("licenses", _("Licenses report"),
     _("Lists the licenses of the installed packages."), None, ["licenses"]),
]


class MaintenancePage(Page):
    key = "maintenance"
    title = _("Maintenance")
    icon_name = "tools"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        inner = QWidget(self)
        grid = QGridLayout(inner)
        for i, (key, title, desc, sim, run) in enumerate(MAINTENANCE):
            card = Card(title, inner)
            card.lay.addWidget(muted(desc))
            cmd = muted("zypper " + " ".join(run))
            cmd.setStyleSheet("color: palette(link); font-family: monospace;")
            card.lay.addWidget(cmd)
            row = QHBoxLayout()
            if sim:
                row.addWidget(button(_("Simulate"), "media-playback-start",
                                     lambda _c=False, a=sim, t=title: self.ctx.run([Step("zypper", ["-n", "-v", *a])], t)))
            row.addWidget(button(_("Run"), "system-run",
                                 lambda _c=False, a=run, t=title, s=sim: self._run(a, t, s)))
            row.addStretch(1)
            card.lay.addLayout(row)
            grid.addWidget(card, i // 2, i % 2)
        grid.setRowStretch(len(MAINTENANCE) // 2 + 1, 1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)

    def _run(self, args, title, sim):
        if sim and not ask(self, title, _("Run it for real? Use Simulate first to see what it would do.")):
            return
        self.ctx.run([Step("zypper", ["-v", *(["-n"] if args[0] != "-n" else []), *args])], title)


# ============================================================ settings
class SettingsPage(Page):
    key = "settings"
    title = _("Settings")
    icon_name = "configure"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        s = ctx.settings
        lay = QVBoxLayout(self)
        c1 = Card(_("When applying the queue"), self)
        self.no_rec = QCheckBox(_("Do not install recommended packages (--no-recommends)"), self)
        self.no_rec.setChecked(s["apply_no_recommends"])
        self.lic = QCheckBox(_("Accept licenses without asking (--auto-agree-with-licenses)"), self)
        self.lic.setChecked(s["apply_accept_licenses"])
        c1.lay.addWidget(self.no_rec)
        c1.lay.addWidget(self.lic)
        c1.lay.addWidget(muted(_("Changes are applied without questions (-n), after you have seen the simulation. "
                                 "If a license must be accepted and this option is off, zypper stops and nothing "
                                 "is installed.")))
        lay.addWidget(c1)
        c2 = Card(_("Terminal"), self)
        self.fold = QCheckBox(_("Fold a line repeated many times in a row into one"), self)
        self.fold.setChecked(s["fold_repeated_lines"])
        self.after = QCheckBox(_("After the routine, check what must be restarted (zypper ps)"), self)
        self.after.setChecked(s["after_routine_check_restart"])
        c2.lay.addWidget(self.fold)
        c2.lay.addWidget(self.after)
        lay.addWidget(c2)
        c3 = Card(_("zypper and libzypp configuration"), self)
        c3.lay.addWidget(muted(_("Editing /etc/zypp/zypp.conf and /etc/zypp/zypper.conf from here (recommended "
                                 "packages, kernels to keep, parallel downloads…) will come in a later version.")))
        lay.addWidget(c3)
        lay.addStretch(1)
        for cb, key in ((self.no_rec, "apply_no_recommends"), (self.lic, "apply_accept_licenses"),
                        (self.fold, "fold_repeated_lines"), (self.after, "after_routine_check_restart")):
            cb.toggled.connect(lambda on, k=key: self._set(k, on))

    def _set(self, key, value):
        self.ctx.settings[key] = value
        self.ctx.settings.save()
        if key == "fold_repeated_lines":
            self.ctx.terminal.view.fold = value
