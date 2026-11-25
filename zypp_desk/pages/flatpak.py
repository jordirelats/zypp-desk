# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak: installed apps, search, updates and remotes, through the same queue as zypper."""

import html

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLineEdit, QSplitter, QTabWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import flatpak
from ..i18n import _, ngettext
from ..runner import Step
from ..tasks import run_async
from ..txqueue import Action, installation_label
from ..ui import Page, ask, button, error_box, muted, tool

FLATHUB = ("flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo")
ORANGE = "#f67400"

# the fields of 'flatpak info' shown, in this order, with words people use
INFO_ROWS = [
    ("Version", _("Version")),
    ("Branch", _("Branch")),
    ("Origin", _("Comes from")),
    ("Installation", _("Installed for")),
    ("Installed Size", _("Size on disk")),
    ("Download Size", _("Download")),
    ("License", _("License")),
    ("Runtime", _("Runtime (shared base)")),
    ("Date", _("Last change")),
    ("ID", _("ID")),
]


def nice_size(text):
    """A flatpak size ('142.1 MB') in the user's own format."""
    n = flatpak.parse_size(text)
    return QLocale().formattedDataSize(n) if n else (text or "")


def _tree(headers, parent):
    t = QTreeWidget(parent)
    t.setHeaderLabels(headers)
    t.setRootIsDecorated(False)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(True)
    return t


class AddRemoteDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Add a Flatpak remote"))
        form = QFormLayout(self)
        self.name = QLineEdit(FLATHUB[0], self)
        self.url = QLineEdit(FLATHUB[1], self)
        self.inst = QComboBox(self)
        self.inst.addItem(installation_label("system"), "system")
        self.inst.addItem(installation_label("user"), "user")
        form.addRow(_("Name:"), self.name)
        form.addRow(_("Address (.flatpakrepo or repository URL):"), self.url)
        form.addRow(_("Installation:"), self.inst)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class FlatpakPage(Page):
    key = "flatpak"
    title = _("Flatpak")
    icon_name = "flatpak-discover"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.updates = {}
        self.eol = {}  # app ID → (message, replacement) for apps that are no longer maintained
        self.selected = None  # (kind, row): kind is "installed" or "search"
        self.shown = None  # the details on screen, to draw them again with or without permissions

        if not flatpak.available():
            lay = QVBoxLayout(self)
            lay.addWidget(muted(_("Flatpak is not installed. Flatpak apps run isolated from the system and come "
                                  "from their own repositories, such as Flathub.")))
            lay.addWidget(button(_("Install Flatpak"), "list-add", lambda: ctx.queue.add(Action("install", "flatpak"))))
            lay.addStretch(1)
            return

        self.tabs = QTabWidget(self)
        # installed
        inst = QWidget(self)
        il = QVBoxLayout(inst)
        self.show_runtimes = QCheckBox(_("Show runtimes too"), self)
        self.show_runtimes.toggled.connect(lambda _on: self.reload())
        self.t_inst = _tree([_("Name"), _("ID"), _("Version"), _("Branch"), _("Origin"), _("Installation"),
                             _("Size"), _("Status")], inst)
        self.t_inst.currentItemChanged.connect(lambda cur, _p: self._select("installed", cur))
        ib = QHBoxLayout()
        ib.addWidget(self.show_runtimes)
        ib.addStretch(1)
        ib.addWidget(button(_("Update"), "system-software-update", self.queue_update))
        self.delete_data = QCheckBox(_("Also delete its data"), self)
        ib.addWidget(self.delete_data)
        ib.addWidget(button(_("Uninstall"), "edit-delete", self.queue_uninstall))
        il.addWidget(self.t_inst, 1)
        il.addLayout(ib)
        self.tabs.addTab(inst, _("Installed"))
        # search
        srch = QWidget(self)
        sl = QVBoxLayout(srch)
        sr = QHBoxLayout()
        self.term = QLineEdit(self)
        self.term.setPlaceholderText(_("Search Flatpak apps, for example krita"))
        self.term.setClearButtonEnabled(True)
        self.term.returnPressed.connect(self.search)
        sr.addWidget(self.term, 1)
        sr.addWidget(button(_("Search"), "edit-find", self.search))
        sl.addLayout(sr)
        self.s_info = muted("", self, wrap=False)
        sl.addWidget(self.s_info)
        self.t_search = _tree([_("Name"), _("ID"), _("Version"), _("Branch"), _("Remote"), _("Description")], srch)
        self.t_search.currentItemChanged.connect(lambda cur, _p: self._select("search", cur))
        sl.addWidget(self.t_search, 1)
        sb = QHBoxLayout()
        sb.addStretch(1)
        self.target = QComboBox(self)
        self.target.addItem(installation_label("system"), "system")
        self.target.addItem(installation_label("user"), "user")
        sb.addWidget(self.target)
        sb.addWidget(button(_("Install"), "list-add", self.queue_install))
        sl.addLayout(sb)
        self.tabs.addTab(srch, _("Search"))
        # remotes
        rem = QWidget(self)
        rl = QVBoxLayout(rem)
        self.t_rem = _tree([_("Name"), _("Title"), _("Address"), _("Installation"), _("Enabled")], rem)
        rl.addWidget(self.t_rem, 1)
        rb = QHBoxLayout()
        rb.addStretch(1)
        rb.addWidget(button(_("Add…"), "list-add", self.add_remote))
        rb.addWidget(button(_("Enable / disable"), "system-run", self.toggle_remote))
        rb.addWidget(button(_("Remove"), "list-remove", self.remove_remote))
        rl.addLayout(rb)
        self.tabs.addTab(rem, _("Remotes"))

        self.details = QTextBrowser(self)
        self.details.setOpenExternalLinks(True)
        self.perm_btn = button(_("Show permissions"), "security-medium", self._toggle_perms)
        self.perm_btn.setCheckable(True)
        self.perm_btn.hide()
        dbox = QWidget(self)
        dl = QVBoxLayout(dbox)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.details, 1)
        pr = QHBoxLayout()
        pr.addStretch(1)
        pr.addWidget(self.perm_btn)
        dl.addLayout(pr)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.tabs)
        split.addWidget(dbox)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("Flatpak apps run isolated from the system and update separately from zypper. "
                              "Changes go to the same queue; flatpak asks for the password itself when the app is "
                              "for every user.")))
        lay.addWidget(split, 1)
        ctx.queue.changed.connect(self._paint_installed)

    def header_actions(self):
        if not flatpak.available():
            return []
        return [tool(_("Update everything"), "system-software-update",
                     lambda: self.ctx.queue.add(Action("flatpak-update", "*"))),
                tool(_("Remove unused runtimes"), "edit-clear", self.remove_unused),
                tool(_("Refresh the catalogue"), "view-refresh",
                     lambda: self.ctx.run([Step("flatpak", ["update", "--appstream"])], _("Refresh the catalogue"),
                                          on_done=lambda ok: self.reload()))]

    # ------------------------------------------------------------ data
    def reload(self):
        if not flatpak.available():
            return
        run_async(flatpak.installed, self.show_runtimes.isChecked(), on_done=self._installed_loaded)
        run_async(flatpak.updates, on_done=self._updates_loaded)
        run_async(flatpak.remotes, on_done=self._remotes_loaded)

    def _installed_loaded(self, rows, err):
        if err:
            error_box(self, self.title, str(err))
            return
        self.t_inst.setSortingEnabled(False)
        self.t_inst.clear()
        for r in rows:
            it = QTreeWidgetItem(self.t_inst, [r["name"], r["application"], r["version"], r["branch"], r["origin"],
                                               installation_label(r["installation"]), nice_size(r["size"]), ""])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            it.setData(6, Qt.ItemDataRole.UserRole, r.get("bytes") or 0)
        self.t_inst.setSortingEnabled(True)
        self.t_inst.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._paint_installed()
        if self.t_inst.topLevelItemCount() and self.t_inst.currentItem() is None:
            self.t_inst.setCurrentItem(self.t_inst.topLevelItem(0))
        for c in range(7):
            self.t_inst.resizeColumnToContents(c)
        # which apps are no longer maintained (one 'flatpak info' each, in the background)
        apps = [r for r in rows if flatpak.APP_ID_RE.fullmatch(r["application"])][:80]
        run_async(flatpak.eol_apps, apps, on_done=self._eol_loaded)

    def _eol_loaded(self, eol, err):
        if not err and eol is not None:
            self.eol = eol
            self._paint_installed()

    def _updates_loaded(self, rows, err):
        if not err:
            self.updates = {r["application"]: r for r in rows}
            self._paint_installed()

    def _paint_installed(self):
        if not flatpak.available():
            return
        for i in range(self.t_inst.topLevelItemCount()):
            it = self.t_inst.topLevelItem(i)
            r = it.data(0, Qt.ItemDataRole.UserRole)
            state = self.ctx.queue.flatpak_state(r["application"])
            text, color = "", None
            if state == "flatpak-uninstall":
                text, color = _("Will be uninstalled"), "#da4453"
            elif state == "flatpak-update" and r["application"] in self.updates:
                text, color = _("Will be updated"), "#27ae60"
            elif r["application"] in self.updates:
                text, color = _("Update available"), "#1d99f3"
            elif r["application"] in self.eol:
                text, color = _("no longer maintained"), ORANGE
                it.setToolTip(7, self.eol[r["application"]][0])
            it.setText(7, text)
            it.setForeground(7, QBrush(QColor(color)) if color else QBrush())

    def _remotes_loaded(self, rows, err):
        if err:
            return
        self.t_rem.clear()
        for r in rows:
            it = QTreeWidgetItem(self.t_rem, [r["name"], r["title"], r["url"], installation_label(r["installation"]),
                                              _("yes") if r["enabled"] else _("no")])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
        for c in range(4):
            self.t_rem.resizeColumnToContents(c)

    def search(self):
        term = self.term.text().strip()
        if not term:
            return
        self.s_info.setText(_("Searching…"))
        self.t_search.clear()
        run_async(flatpak.search, term, on_done=self._searched)

    def _searched(self, rows, err):
        if err:
            self.s_info.setText(str(err)[:200])
            return
        self.s_info.setText(ngettext("{n} found", "{n} found", len(rows)).format(n=len(rows)))
        self.t_search.setSortingEnabled(False)
        for r in rows:
            it = QTreeWidgetItem(self.t_search, [r["name"], r["application"], r["version"], r["branch"],
                                                 r["remotes"], r["description"]])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
        self.t_search.setSortingEnabled(True)
        self.t_search.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        for c in range(5):
            self.t_search.resizeColumnToContents(c)

    # ------------------------------------------------------------ details
    def _select(self, kind, item):
        if not item:
            return
        r = item.data(0, Qt.ItemDataRole.UserRole)
        self.selected = (kind, r)
        self.shown = None
        self.perm_btn.hide()
        self.details.setHtml(f"<h3>{html.escape(r['name'])}</h3><p><i>{html.escape(_('Loading…'))}</i></p>")
        app = r["application"]
        if kind == "installed":
            run_async(flatpak.details, app, r["installation"], on_done=lambda res, e, a=app: self._info(a, res, e))
        else:
            remote = r["remotes"].split(",")[0]
            run_async(flatpak.remote_details, remote, app, on_done=lambda res, e, a=app: self._info(a, res, e))

    def _info(self, app, res, err):
        if not self.selected or self.selected[1]["application"] != app:
            return  # another app was selected meanwhile
        if err or not res:
            self.details.setPlainText(str(err or ""))
            return
        self.shown = res
        self.perm_btn.setVisible(bool(res.get("perms")))
        self._draw()

    def _toggle_perms(self):
        self.perm_btn.setText(_("Hide permissions") if self.perm_btn.isChecked() else _("Show permissions"))
        self._draw()

    def _draw(self):
        res = self.shown
        if not res:
            return
        e = html.escape
        f = res["fields"]
        title = res.get("title") or f.get("ID", "")
        name, _sep, summary = title.partition(" - ")
        body = f"<h3>{e(name)}</h3>"
        if summary:
            body += f"<p>{e(summary)}</p>"
        if res.get("eol"):
            msg, rebase = res["eol"]
            body += (f"<p style='background:rgba(246,116,0,0.15); border-left:4px solid {ORANGE}; padding:6px'>"
                     f"<b>{e(_('This app is no longer maintained.'))}</b> "
                     + e(_("It gets no more updates, not even for security problems. Look for another app that "
                           "does the same.")) + (f"<br><i>{e(msg)}</i>" if msg else "")
                     + (f"<br>{e(_('It was replaced by {app}.').format(app=rebase))}" if rebase else "") + "</p>")
        rows = []
        for key, label in INFO_ROWS:
            val = f.get(key)
            if not val:
                continue
            if key in ("Installed Size", "Download Size"):
                val = nice_size(val)
            elif key == "Installation":
                val = installation_label(val)
            elif key == "Runtime":
                val = val.split("/")[0] + " " + val.rsplit("/", 1)[-1]
            elif key == "Date":
                val = val.split(" ")[0]
            rows.append((label, val))
        body += "<table>" + "".join(f"<tr><td style='color:gray;padding-right:12px'>{e(k)}</td><td>{e(v)}</td></tr>"
                                    for k, v in rows) + "</table>"
        if self.perm_btn.isChecked() and res.get("perms"):
            body += f"<h4>{e(_('Permissions'))}</h4><pre>{e(res['perms'])}</pre>"
        self.details.setHtml(body)

    # ------------------------------------------------------------ queue
    def _current_installed(self):
        it = self.t_inst.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def queue_update(self):
        r = self._current_installed()
        if r:
            self.ctx.queue.add(Action("flatpak-update", r["application"]))

    def queue_uninstall(self):
        r = self._current_installed()
        if r:
            self.ctx.queue.add(Action("flatpak-uninstall", r["application"], installation=r["installation"],
                                      delete_data=self.delete_data.isChecked()))

    def queue_install(self):
        it = self.t_search.currentItem()
        if not it:
            return
        r = it.data(0, Qt.ItemDataRole.UserRole)
        # with its branch: an app published in several branches (org.gimp.GIMP.Plugin…) is otherwise ambiguous
        ref = r["application"] + (f"//{r['branch']}" if r.get("branch") and r["branch"] != "stable" else "")
        self.ctx.queue.add(Action("flatpak-install", ref, remote=r["remotes"].split(",")[0],
                                  installation=self.target.currentData()))

    # ------------------------------------------------------------ immediate actions
    def remove_unused(self):
        if ask(self, _("Remove unused runtimes"), _("Uninstall the runtimes and extensions that no installed app "
                                                    "uses any more?")):
            self.ctx.run([Step("flatpak", ["uninstall", "--unused", "--noninteractive", "-y"])],
                         _("Remove unused runtimes"), on_done=lambda ok: self.reload())

    def add_remote(self):
        dlg = AddRemoteDialog(self)
        if not dlg.exec():
            return
        name, url, inst = dlg.name.text().strip(), dlg.url.text().strip(), dlg.inst.currentData()
        if not name or not url:
            return
        self.ctx.run([Step("flatpak", ["remote-add", "--if-not-exists", f"--{inst}", name, url])],
                     _("Add a Flatpak remote"), on_done=lambda ok: self.reload())

    def _current_remote(self):
        it = self.t_rem.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def toggle_remote(self):
        r = self._current_remote()
        if r:
            flag = "--disable" if r["enabled"] else "--enable"
            self.ctx.run([Step("flatpak", ["remote-modify", flag, f"--{r['installation']}", r["name"]])],
                         _("Change a Flatpak remote"), on_done=lambda ok: self.reload())

    def remove_remote(self):
        r = self._current_remote()
        if r and ask(self, _("Remove"), _("Remove the Flatpak remote {name}? Apps installed from it stay, but will "
                                          "not get updates.").format(name=r["name"])):
            self.ctx.run([Step("flatpak", ["remote-delete", f"--{r['installation']}", r["name"]])],
                         _("Remove a Flatpak remote"), on_done=lambda ok: self.reload())
