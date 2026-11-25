# SPDX-License-Identifier: GPL-3.0-or-later
"""Repositories: enable, priority, refresh, keep packages, add, remove, signing keys."""

import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QInputDialog, QLineEdit, QMessageBox, QSpinBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from .. import backend
from ..i18n import _
from ..runner import Step
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Page, ask, error_box, muted, tool

COL_ON, COL_NAME, COL_PRIO, COL_AUTO, COL_KEEP, COL_GPG, COL_URL = range(7)


class AddRepoDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Add a repository"))
        form = QFormLayout(self)
        self.url = QLineEdit(self)
        self.url.setPlaceholderText("https://download.opensuse.org/repositories/…")
        self.alias = QLineEdit(self)
        self.name = QLineEdit(self)
        self.prio = QSpinBox(self)
        self.prio.setRange(1, 200)
        self.prio.setValue(99)
        self.auto = QCheckBox(_("Refresh it automatically"), self)
        self.auto.setChecked(True)
        self.gpg = QComboBox(self)
        self.gpg.addItem(_("Check signatures (recommended)"), "default")
        self.gpg.addItem(_("Packages signed, repository metadata unsigned"), "allow-unsigned-repo")
        self.gpg.addItem(_("Do not check signatures (not recommended)"), "off")
        form.addRow(_("Address (URL):"), self.url)
        form.addRow(_("Alias (short name, no spaces):"), self.alias)
        form.addRow(_("Name:"), self.name)
        form.addRow(_("Priority (lower wins):"), self.prio)
        form.addRow("", self.auto)
        form.addRow(_("Signatures:"), self.gpg)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        bb.accepted.connect(self._check)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.url.textChanged.connect(self._guess_alias)

    def _guess_alias(self, url):
        if not self.alias.isModified():
            tail = [p for p in url.rstrip("/").split("/") if p][-2:]
            self.alias.setText("-".join(tail).replace(":", "_")[:40])

    def _check(self):
        if not self.url.text().strip() or not self.alias.text().strip() or " " in self.alias.text().strip():
            error_box(self, _("Add a repository"), _("The address and an alias without spaces are needed."))
            return
        self.accept()

    def action(self):
        alias = self.alias.text().strip()
        return Action("repo-add", alias, url=self.url.text().strip(), name=self.name.text().strip() or alias,
                      priority=self.prio.value(), autorefresh=self.auto.isChecked(), gpg=self.gpg.currentData())


class ReposPage(Page):
    key = "repos"
    title = _("Repositories")
    icon_name = "repository"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.repos = []
        self._filling = False
        self.tree = QTreeWidget(self)
        # TRANSLATORS: column headers; "Refresh" means "refreshed automatically".
        self.tree.setHeaderLabels([_("Enabled"), _("Name"), _("Priority"), _("Refresh"), _("Keep packages"),
                                   _("Signatures"), _("Address")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText(_("Filter by name or address"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.hint = muted(_("Tick or untick to queue a change. Double-click the priority to change it. "
                            "Lower priority numbers win."), self)
        lay = QVBoxLayout(self)
        lay.addWidget(self.filter)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.hint)
        ctx.queue.changed.connect(self._fill)

    def header_actions(self):
        return [tool(_("Add…"), "list-add", self.add_repo), tool(_("Remove"), "list-remove", self.remove_repo),
                tool(_("Refresh"), "view-refresh", self.refresh_selected),
                tool(_("Refresh all"), "view-refresh", lambda: self.ctx.run([Step("zypper", ["-v", "refresh"])],
                                                                            _("Refresh every repository"),
                                                                            on_done=lambda ok: self.reload())),
                tool(_("Clean the cache"), "edit-clear", self.clean_cache),
                tool(_("Import a signing key…"), "object-locked", self.import_key)]

    def reload(self):
        run_async(backend.repos, on_done=self._loaded)

    def _loaded(self, repos, err):
        if err:
            error_box(self, self.title, str(err))
            return
        self.repos = repos
        self._fill()

    def _fill(self):
        self._filling = True
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for r in self.repos:
            ch = self.ctx.queue.repo_change(r["alias"])
            o = ch.opts if ch and ch.kind == "repo-modify" else {}
            removing = bool(ch and ch.kind == "repo-remove")
            # TRANSLATORS: what is checked with GPG signatures in a repository.
            gpg = (_("repository and packages") if r["repo_gpgcheck"] and r["pkg_gpgcheck"] else
                   _("packages") if r["pkg_gpgcheck"] else _("repository") if r["repo_gpgcheck"] else
                   _("on") if r["gpgcheck"] else _("off"))
            it = QTreeWidgetItem(["", r["name"], "", "", "", gpg, r["url"]])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            it.setCheckState(COL_ON, Qt.CheckState.Checked if o.get("enabled", r["enabled"]) else Qt.CheckState.Unchecked)
            it.setCheckState(COL_AUTO, Qt.CheckState.Checked if o.get("autorefresh", r["autorefresh"])
                             else Qt.CheckState.Unchecked)
            it.setCheckState(COL_KEEP, Qt.CheckState.Checked if o.get("keeppackages", r["keeppackages"])
                             else Qt.CheckState.Unchecked)
            it.setData(COL_PRIO, Qt.ItemDataRole.DisplayRole, o.get("priority", r["priority"]))
            it.setToolTip(COL_NAME, r["alias"] + (f"  ({_('service')}: {r['service']})" if r["service"] else ""))
            if o or removing:
                f = it.font(COL_NAME)
                f.setItalic(True)
                f.setStrikeOut(removing)
                it.setFont(COL_NAME, f)
                it.setForeground(COL_NAME, QBrush(QColor("#da4453" if removing else "#1d99f3")))
            if not r["enabled"]:
                for c in (COL_NAME, COL_URL):
                    it.setForeground(c, QBrush(QColor("gray")) if not o else it.foreground(c))
            self.tree.addTopLevelItem(it)
        self.tree.setSortingEnabled(True)
        for c in range(6):
            self.tree.resizeColumnToContents(c)
        self._apply_filter(self.filter.text())
        self._filling = False

    def _apply_filter(self, text):
        t = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            r = it.data(0, Qt.ItemDataRole.UserRole)
            it.setHidden(bool(t) and t not in (r["name"] + " " + r["alias"] + " " + r["url"]).lower())

    def _selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _item_changed(self, it, col):
        if self._filling:
            return
        r = it.data(0, Qt.ItemDataRole.UserRole)
        checked = it.checkState(col) == Qt.CheckState.Checked
        field = {COL_ON: "enabled", COL_AUTO: "autorefresh", COL_KEEP: "keeppackages"}.get(col)
        if field:
            self._queue_change(r, field, checked)

    def _queue_change(self, r, field, value):
        ch = self.ctx.queue.repo_change(r["alias"])
        opts = dict(ch.opts) if ch and ch.kind == "repo-modify" else {}
        if value == r[field]:
            opts.pop(field, None)
        else:
            opts[field] = value
        if ch and ch.kind == "repo-modify":
            self.ctx.queue.discard_action(ch)
        if opts:
            self.ctx.queue.add(Action("repo-modify", r["alias"], **opts))
        else:
            self._fill()

    def _double_clicked(self, it, col):
        if col != COL_PRIO:
            return
        r = it.data(0, Qt.ItemDataRole.UserRole)
        val, ok = QInputDialog.getInt(self, _("Priority"), _("Priority of {name} (1–200, lower wins):")
                                      .format(name=r["name"]), int(it.text(COL_PRIO)), 1, 200)
        if ok:
            self._queue_change(r, "priority", val)

    def add_repo(self):
        dlg = AddRepoDialog(self)
        if dlg.exec():
            self.ctx.queue.add(dlg.action())

    def remove_repo(self):
        r = self._selected()
        if r and ask(self, _("Remove"), _("Queue the removal of the repository {name}?").format(name=r["name"])):
            self.ctx.queue.add(Action("repo-remove", r["alias"]))

    def refresh_selected(self):
        r = self._selected()
        if r:
            self.ctx.run([Step("zypper", ["-v", "refresh", r["alias"]])], _("Refresh {name}").format(name=r["name"]))

    def clean_cache(self):
        if ask(self, _("Clean the cache"), _("Delete the downloaded packages and the cached metadata of every "
                                             "repository? They will be downloaded again when needed.")):
            self.ctx.run([Step("zypper", ["clean", "--all"])], _("Clean the cache"))

    def import_key(self):
        path, _f = QFileDialog.getOpenFileName(self, _("Choose the public key file"), "",
                                               _("Keys (*.asc *.key *.pub *.gpg);;All files (*)"))
        if not path:
            return
        p = subprocess.run(["gpg", "--show-keys", "--with-fingerprint", path], capture_output=True, text=True)
        if p.returncode != 0 or "pub" not in p.stdout:
            error_box(self, _("Import a signing key"), _("This file does not look like a public key."))
            return
        box = QMessageBox(self)
        box.setWindowTitle(_("Import a signing key"))
        box.setText(_("Check the fingerprint against the one published by the vendor on its official web site "
                      "before importing it. From then on, rpm trusts every package signed with this key."))
        box.setDetailedText(p.stdout)
        box.setInformativeText(p.stdout.strip())
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.ctx.run([Step("rpm-import", [path])], _("Import a signing key"))
