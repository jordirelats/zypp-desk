# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak: installed apps, search, updates and remotes, through the same queue as zypper."""

import html

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QBrush, QColor, QPalette, QTextOption
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLineEdit, QSplitter, QTabWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import flatpak, question
from ..i18n import _, ngettext, pgettext
from ..runner import Step
from ..tasks import run_async
from ..txqueue import Action, installation_label
from ..ui import Page, button, error_box, muted, tool
from .markbar import amp, flash_queued, queue_label

FLATHUB = ("flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo")
ORANGE = "#f67400"
NAME_FITS = 22  # a longer name in a button label: "the selected app" instead (the button would get too wide)

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
        self._updates_known = False  # 'flatpak remote-ls --updates' answered: an app without an update is known
        self._want_app = None  # select_app(): the app to select once the list is read
        self._up_all = None  # the header's "Put every update in the queue" shown now

        if not flatpak.available():
            lay = QVBoxLayout(self)
            lay.addWidget(muted(_("Flatpak is not installed. Flatpak apps run isolated from the system and come "
                                  "from their own repositories, such as Flathub.")))
            b = button(_("Put Flatpak in the queue"), "list-add", lambda: ctx.queue.add(Action("install", "flatpak")))
            b.setToolTip(_("Puts the installation of the flatpak package in the queue (zypper install flatpak). "
                           "Nothing changes until you apply the queue."))
            b.clicked.connect(lambda: flash_queued(b))
            lay.addWidget(b)
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
        self.t_inst.currentItemChanged.connect(lambda _c, _p: self._inst_buttons())
        # the switch above the list, the actions below it: one row of everything was too wide in French.
        # Both actions only add to the queue: "To the queue:" in front of the row says so (none is green)
        il.addWidget(self.show_runtimes)
        ib = QHBoxLayout()
        ib.addWidget(queue_label(self))
        self.up_btn = button(_("Update the selected app"), "system-software-update", self.queue_update)
        ib.addWidget(self.up_btn)
        ib.addStretch(1)
        self.delete_data = QCheckBox(_("Also delete its data"), self)
        self.delete_data.setToolTip(_("With the app, its data in your home folder goes too (--delete-data)."))
        ib.addWidget(self.delete_data)
        self.un_btn = button(_("Uninstall the selected app"), "edit-delete", self.queue_uninstall)
        ib.addWidget(self.un_btn)
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
        self.search_btn = button(_("Search"), "edit-find", self.search)
        self.search_btn.setToolTip(_("Look for this text in the catalogue of your Flatpak remotes (flatpak search). "
                                     "It only shows: nothing is installed."))
        sr.addWidget(self.search_btn)
        sl.addLayout(sr)
        self.s_info = muted("", self, wrap=False)
        sl.addWidget(self.s_info)
        self.t_search = _tree([_("Name"), _("ID"), _("Version"), _("Branch"), _("Remote"), _("Description")], srch)
        self.t_search.currentItemChanged.connect(lambda cur, _p: self._select("search", cur))
        self.t_search.currentItemChanged.connect(lambda _c, _p: self._install_button())
        sl.addWidget(self.t_search, 1)
        sb = QHBoxLayout()
        sb.addStretch(1)
        sb.addWidget(queue_label(self))
        self.target = QComboBox(self)
        self.target.addItem(installation_label("system"), "system")
        self.target.addItem(installation_label("user"), "user")
        self.target.setToolTip(_("For every user of this computer, or only for you"))
        self.target.currentIndexChanged.connect(lambda _i: self._install_button())
        sb.addWidget(self.target)
        self.inst_btn = button(_("Install the selected app"), "list-add", self.queue_install)
        sb.addWidget(self.inst_btn)
        sl.addLayout(sb)
        self.tabs.addTab(srch, _("Search"))
        # remotes
        rem = QWidget(self)
        rl = QVBoxLayout(rem)
        self.t_rem = _tree([_("Name"), _("Title"), _("Address"), _("Installation"), _("Enabled")], rem)
        self.t_rem.currentItemChanged.connect(lambda _c, _p: self._remote_buttons())
        rl.addWidget(self.t_rem, 1)
        rb = QHBoxLayout()
        rb.addStretch(1)
        # a remote changes at once (after its question): these do not go through the queue
        self.rem_add_btn = button(_("Add a remote…"), "list-add", self.add_remote)
        self.rem_add_btn.setToolTip(_("A window asks for its name and address, then adds it at once (flatpak "
                                      "remote-add). It does not go through the queue."))
        self.rem_toggle_btn = button(_("Enable or disable a remote"), "system-run", self.toggle_remote)
        self.rem_remove_btn = button(_("Remove a remote…"), "list-remove", self.remove_remote)
        for b in (self.rem_add_btn, self.rem_toggle_btn, self.rem_remove_btn):
            rb.addWidget(b)
        rl.addLayout(rb)
        self.tabs.addTab(rem, _("Remotes"))

        self.details = QTextBrowser(self)
        self.details.setOpenExternalLinks(True)
        # lines break between words only ("org.kde.Platform 6.11" is never cut in two)
        self.details.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.perm_btn = button(_("Show permissions"), "security-medium", self._toggle_perms)
        self.perm_btn.setToolTip(_("What this app may use outside its sandbox (flatpak info --show-permissions). "
                                   "It only shows them."))
        self.perm_btn.setCheckable(True)
        self.perm_btn.hide()
        dbox = QWidget(self)
        dbox.setMinimumWidth(240)
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
        split.setSizes([560, 320])
        split.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("Flatpak apps run isolated from the system and update separately from zypper. "
                              "Changes go to the same queue; flatpak asks for the password itself when the app is "
                              "for every user.")))
        lay.addWidget(split, 1)
        ctx.queue.changed.connect(self._paint_installed)
        self._inst_buttons()
        self._install_button()
        self._remote_buttons()

    def header_actions(self):
        if not flatpak.available():
            return []
        self._up_all = tool(_("Put every update in the queue"), "system-software-update", self._queue_update_all)
        self._update_all_label()
        unused = tool(_("Remove unused runtimes…"), "edit-clear", self.remove_unused)
        unused.setToolTip(_("Asks first, then uninstalls at once the runtimes and extensions no app uses any more "
                            "(flatpak uninstall --unused). It does not go through the queue."))
        fresh = tool(_("Refresh the catalogue"), "view-refresh",
                     lambda: self.ctx.run([Step("flatpak", ["update", "--appstream"])], _("Refresh the catalogue"),
                                          on_done=lambda ok: self.reload()))
        fresh.setToolTip(_("Downloads again the list of apps of every remote now (flatpak update --appstream). "
                           "Nothing is installed or updated."))
        return [self._up_all, unused, fresh]

    # ------------------------------------------------------------ the labels of the buttons (botons/NORMES.md)
    @staticmethod
    def _alive(b):
        """False once Qt has deleted the widget (a header button of an earlier visit)."""
        try:
            b.objectName()
            return True
        except RuntimeError:
            return False

    def _update_all_label(self):
        b = self._up_all
        if b is None or not self._alive(b) or b.property("flashing"):
            return
        n = len(self.updates)
        if n:
            b.setText(ngettext("Put the {n} update in the queue", "Put the {n} updates in the queue", n).format(n=n))
        else:
            b.setText(_("Put every update in the queue"))
        b.setEnabled(bool(n) or not self._updates_known)
        if self._updates_known and not n:
            b.setToolTip(_("No Flatpak app or runtime has an update waiting."))
        else:
            b.setToolTip(_("Puts “flatpak update” in the queue: every app and runtime with an update waiting. "
                           "Nothing changes until you apply the queue."))

    def _queue_update_all(self):
        self.ctx.queue.add(Action("flatpak-update", "*"))
        if self._up_all is not None and self._alive(self._up_all):
            flash_queued(self._up_all, self._update_all_label)

    @staticmethod
    def _name(r):
        """The name of a row for a button, or None when it is too long for one."""
        name = (r or {}).get("name") or (r or {}).get("application") or ""
        return amp(name) if name and len(name) <= NAME_FITS else None

    def _inst_buttons(self):
        """Update X and Uninstall X say the app selected in Installed, live."""
        r = self._current_installed()
        name = self._name(r)
        nothing_yet = _("Nothing changes until you apply the queue.")
        if not self.up_btn.property("flashing"):
            self.up_btn.setText(_("Update {name}").format(name=name) if name else _("Update the selected app"))
            waiting = bool(r) and (not self._updates_known or r["application"] in self.updates)
            self.up_btn.setEnabled(waiting)
            if not r:
                self.up_btn.setToolTip(_("Choose an app in the list first."))
            elif not waiting:
                self.up_btn.setToolTip(_("{name} has no update waiting.").format(name=r["name"] or r["application"]))
            else:
                self.up_btn.setToolTip(_("Puts the update of {name} in the queue ({command}).").format(
                    name=r["name"] or r["application"], command=f"flatpak update {r['application']}")
                    + " " + nothing_yet)
        if not self.un_btn.property("flashing"):
            self.un_btn.setText(_("Uninstall {name}").format(name=name) if name else _("Uninstall the selected app"))
            self.un_btn.setEnabled(bool(r))
            self.un_btn.setToolTip(
                _("Puts the removal of {name} in the queue ({command}). With “Also delete its data”, its data in "
                  "your home folder goes too.").format(name=r["name"] or r["application"],
                                                       command=f"flatpak uninstall --{r['installation']} "
                                                               f"{r['application']}") + " " + nothing_yet
                if r else _("Choose an app in the list first."))

    def _install_button(self):
        if self.inst_btn.property("flashing"):
            return
        it = self.t_search.currentItem()
        r = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        name = self._name(r)
        self.inst_btn.setText(_("Install {name}").format(name=name) if name else _("Install the selected app"))
        self.inst_btn.setEnabled(bool(r))
        if r:
            self.inst_btn.setToolTip(_("Puts {name} in the queue to be installed ({command}).").format(
                name=r["name"] or r["application"],
                command=f"flatpak install --{self.target.currentData()} {r['remotes'].split(',')[0]} "
                        f"{r['application']}") + " " + _("Nothing changes until you apply the queue."))
        else:
            self.inst_btn.setToolTip(_("Search, then choose an app in the list first."))

    def _remote_buttons(self):
        r = self._current_remote()
        name = amp(r["name"]) if r and len(r["name"]) <= NAME_FITS else None
        if r:
            flag = "--disable" if r["enabled"] else "--enable"
            if r["enabled"]:
                self.rem_toggle_btn.setText(_("Disable {name}").format(name=name) if name
                                            else _("Disable the selected remote"))
                tip = _("Disables the remote {name} at once ({command}): no new apps or updates come from it until "
                        "you enable it again.")
            else:
                self.rem_toggle_btn.setText(_("Enable {name}").format(name=name) if name
                                            else _("Enable the selected remote"))
                tip = _("Enables the remote {name} at once ({command}).")
            self.rem_toggle_btn.setToolTip(tip.format(
                name=r["name"], command=f"flatpak remote-modify {flag} --{r['installation']} {r['name']}") + " "
                + _("It does not go through the queue."))
            self.rem_remove_btn.setText(_("Remove {name}…").format(name=name) if name
                                        else _("Remove the selected remote…"))
            self.rem_remove_btn.setToolTip(_("Asks first, then removes the remote {name} at once (flatpak "
                                             "remote-delete). It does not go through the queue.").format(
                                                 name=r["name"]))
        else:
            self.rem_toggle_btn.setText(_("Enable or disable a remote"))
            self.rem_remove_btn.setText(_("Remove a remote…"))
            for b in (self.rem_toggle_btn, self.rem_remove_btn):
                b.setToolTip(_("Choose a remote in the list first."))
        self.rem_toggle_btn.setEnabled(bool(r))
        self.rem_remove_btn.setEnabled(bool(r))

    def select_app(self, app_id):
        """Show this installed app (Applications → "Show it in Flatpak"), now or once the list is read."""
        if not flatpak.available():
            return
        self.tabs.setCurrentIndex(0)
        self._want_app = app_id
        self._select_wanted()

    def _select_wanted(self):
        if not self._want_app:
            return
        for i in range(self.t_inst.topLevelItemCount()):
            it = self.t_inst.topLevelItem(i)
            r = it.data(0, Qt.ItemDataRole.UserRole)
            if r and r["application"] == self._want_app:
                self._want_app = None
                self.t_inst.setCurrentItem(it)
                self.t_inst.scrollToItem(it)
                return

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
        self._select_wanted()
        self._want_app = None  # not installed (any more): nothing to wait for
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
            self._updates_known = True
            self._paint_installed()
            self._inst_buttons()
            self._update_all_label()

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
                # the theme's link colour: a fixed blue is hard to read on a dark background
                text, color = _("Update available"), self.palette().color(QPalette.ColorRole.Link).name()
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
        self._remote_buttons()

    def search(self):
        term = self.term.text().strip()
        if not term:
            return
        self.s_info.setText(_("Searching…"))
        self.t_search.clear()
        self._install_button()
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
        grey = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        body += "<table>" + "".join(f"<tr><td style='color:{grey};padding-right:12px'>{e(k)}</td><td>{e(v)}</td></tr>"
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
            flash_queued(self.up_btn, self._inst_buttons)

    def queue_uninstall(self):
        r = self._current_installed()
        if r:
            self.ctx.queue.add(Action("flatpak-uninstall", r["application"], installation=r["installation"],
                                      delete_data=self.delete_data.isChecked()))
            flash_queued(self.un_btn, self._inst_buttons)

    def queue_install(self):
        it = self.t_search.currentItem()
        if not it:
            return
        r = it.data(0, Qt.ItemDataRole.UserRole)
        # with its branch: an app published in several branches (org.gimp.GIMP.Plugin…) is otherwise ambiguous
        ref = r["application"] + (f"//{r['branch']}" if r.get("branch") and r["branch"] != "stable" else "")
        self.ctx.queue.add(Action("flatpak-install", ref, remote=r["remotes"].split(",")[0],
                                  installation=self.target.currentData()))
        flash_queued(self.inst_btn, self._install_button)

    # ------------------------------------------------------------ immediate actions
    def _changed(self, _ok=True):
        """After a flatpak command that changed something: the pages look again (Updates and Home count the
        Flatpak updates too)."""
        fn = getattr(self.ctx, "after_change", None)
        if callable(fn):
            fn()
        else:
            self.reload()

    def remove_unused(self):
        st = Step("flatpak", ["uninstall", "--unused", "--noninteractive", "-y"])
        if question.confirm(
                self, _("Remove the unused runtimes?"), _("Yes, remove them"), window_title=_("Remove unused runtimes"),
                icon_name="flatpak-discover",
                items=[("flatpak-discover", pgettext("change", "Uninstall the runtimes and extensions that no app "
                                                          "uses any more"),
                        _("A runtime is the shared base Flatpak apps run on, such as org.kde.Platform."))],
                means=_("your apps keep working: only what none of them uses goes. If an app needs one again, "
                        "Flatpak downloads it."),
                commands_=[st.display()], help_topic="programs"):
            self.ctx.run([st], _("Remove unused runtimes"), on_done=self._changed)

    def add_remote(self):
        dlg = AddRemoteDialog(self)
        accepted = dlg.exec()
        name, url, inst = dlg.name.text().strip(), dlg.url.text().strip(), dlg.inst.currentData()
        dlg.deleteLater()
        if not accepted or not name or not url:
            return
        self.ctx.run([Step("flatpak", ["remote-add", "--if-not-exists", f"--{inst}", name, url])],
                     _("Add a Flatpak remote"), on_done=self._changed)

    def _current_remote(self):
        it = self.t_rem.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def toggle_remote(self):
        r = self._current_remote()
        if r:
            flag = "--disable" if r["enabled"] else "--enable"
            self.ctx.run([Step("flatpak", ["remote-modify", flag, f"--{r['installation']}", r["name"]])],
                         _("Change a Flatpak remote"), on_done=self._changed)

    def remove_remote(self):
        """flatpak refuses to delete a remote that installed apps or runtimes come from, unless --force: first
        look at what comes from it, then say it and show the command."""
        r = self._current_remote()
        if r:
            run_async(flatpak.installed_from, r["name"], r["installation"],
                      on_done=lambda refs, err, r=r: self._remove_remote(r, refs or [], err))

    def _remove_remote(self, r, refs, err):
        if err:
            error_box(self, _("Remove a Flatpak remote"), str(err))
            return
        args = ["remote-delete", f"--{r['installation']}"]
        name, inst = r["name"], r["installation"]
        items = [("flatpak-discover", pgettext("change", "Remove the Flatpak remote “{name}”").format(name=name),
                  _("A remote is a place Flatpak apps come from, like Flathub. This one is {installation}.").format(
                      installation=installation_label(inst)))]
        warning = None
        if refs:
            # flatpak removes a remote that apps or runtimes come from only with --force. "No more updates" is
            # said once, in the warning
            args.append("--force")
            apps = sorted({x["name"] or x["application"] for x in refs if x["kind"] == "app"})
            runtimes = sorted({x["application"] for x in refs if x["kind"] != "app"})
            n = len(apps) + len(runtimes)
            items.append(("dialog-warning", ngettext("{n} app or runtime comes from it",
                                                     "{n} apps and runtimes come from it", n).format(n=n),
                          "", apps + runtimes))
            means = _("nothing is uninstalled: what came from it stays and keeps working as it is now.")
            warning = _("What came from it gets no more updates, not even for security problems.")
        else:
            means = _("nothing installed comes from it, so nothing else changes.")
        st = Step("flatpak", [*args, "--", name])
        if question.confirm(self, _("Remove the Flatpak remote “{name}”?").format(name=name), _("Yes, remove it"),
                            window_title=_("Remove a Flatpak remote"), icon_name="flatpak-discover", items=items,
                            means=means, warning=warning, commands_=[st.display()], password=inst != "user",
                            help_topic="programs"):
            self.ctx.run([st], _("Remove a Flatpak remote"), on_done=self._changed)
