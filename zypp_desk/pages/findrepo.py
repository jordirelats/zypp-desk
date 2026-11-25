# SPDX-License-Identifier: GPL-3.0-or-later
"""Find software on line: openSUSE Build Service, Fedora COPR and Flathub.

Type a program name and see who publishes it, with how much each source can be trusted. The
buttons only put changes in the queue (add a repository, install a package or a Flatpak app);
nothing runs until Apply. The only thing that runs at once, after its own question, is importing
the signing key of a repository that was just queued, so zypper can read it.
"""

import html
import subprocess

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, flatpak, online
from ..explain import SERIOUS, WARN, severity_label
from ..i18n import _, ngettext
from ..runner import Step
from ..style import BLUE, GREEN, ORANGE, RED, dot
from ..tasks import run_async
from ..txqueue import Action
from ..ui import ask, button, error_box, icon, muted, plain

TRUST = {
    online.OFFICIAL: (GREEN, _("Official"), _("Official Tumbleweed package"),
                      _("Built and tested by openSUSE for Tumbleweed. It is already in your openSUSE repositories: "
                        "no repository needs to be added.")),
    online.TEAM: (BLUE, _("Community team"), _("openSUSE community project"),
                  _("A project of the openSUSE Build Service kept by a team, often the place where the official "
                    "package is prepared. It is built for Tumbleweed and is usually safe, but it can bring newer "
                    "libraries than the official ones.")),
    online.PERSONAL: (ORANGE, _("One person"), _("Personal project of one person"),
                      _("Projects whose name starts with home: belong to one person of the openSUSE Build Service. "
                        "It is built for Tumbleweed, but nobody else reviews it and it can stop being updated at any "
                        "time. Use it only if you trust this person or nothing better exists.")),
    online.FOREIGN: (RED, _("Made for Fedora"), _("Built for Fedora, not for openSUSE"),
                     _("COPR builds packages for Fedora. Library names and dependencies are different in openSUSE, "
                       "so installing them can fail or break other programs. Use it only when nothing else exists; "
                       "a Flatpak app is a safer choice.")),
    online.VERIFIED: (GREEN, _("Verified"), _("Verified on Flathub"),
                      _("The developers of the program confirmed on Flathub that this app is theirs. Flatpak apps "
                        "run apart from the system and do not touch openSUSE packages.")),
    online.UNVERIFIED: (BLUE, _("Not verified"), _("Not verified on Flathub"),
                        _("Someone packaged it for Flathub, but the developers of the program have not confirmed "
                          "it. Flatpak apps run apart from the system and do not touch openSUSE packages.")),
}
COPR_TUMBLEWEED = (ORANGE, _("Built for Tumbleweed on COPR"),
                   _("This COPR project also builds for openSUSE Tumbleweed, so the dependencies fit, but it is made "
                     "by one person or team on Fedora's build service and nobody at openSUSE reviews it."))


def trust_of(row, kind):
    if kind == "copr" and row.get("tumbleweed"):
        color, title, text = COPR_TUMBLEWEED
        return color, _("Tumbleweed build"), title, text
    return TRUST[row["trust"]]


def _rpm_has_key(fingerprint):
    """Whether rpm already trusts this key (its packages are called gpg-pubkey-<last 8 hex digits>)."""
    p = subprocess.run(["rpm", "-q", "gpg-pubkey", "--qf", "%{VERSION}\n"], capture_output=True, text=True,
                       env=backend.ENV_C)
    return fingerprint[-8:].lower() in {v.strip().lower() for v in p.stdout.splitlines()}


class ResultPanel(QWidget):
    """One source: a status line, the list of results, what the selected one means and its buttons."""

    def __init__(self, kind, columns, parent=None):
        super().__init__(parent)
        self.kind = kind
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        self.status = muted("", self)
        lay.addWidget(self.status)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(columns)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        lay.addWidget(self.tree, 1)
        self.about = QLabel(self)
        self.about.setWordWrap(True)
        self.about.setTextFormat(Qt.TextFormat.RichText)
        self.about.setOpenExternalLinks(True)
        self.about.setMinimumHeight(64)
        self.about.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.about)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        lay.addLayout(self.buttons)

    def current(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def set_about(self, color, label, title, text, extra=""):
        self.about.setText(f"<b style='color:{color}'>{html.escape(label)}</b> · <b>{html.escape(title)}</b><br>"
                           f"{html.escape(text)}{extra}")
        self.about.setStyleSheet(f"QLabel {{ border-left: 4px solid {color}; padding: 6px 10px; "
                                 "background: palette(base); border-radius: 6px; }")


class FindRepoDialog(QDialog):
    def __init__(self, ctx, term="", parent=None):
        super().__init__(parent or ctx)
        self.ctx = ctx
        # Freed when closed; answers that arrive later (searches, icons, keys) see _closed and do nothing.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._closed = False
        self.setWindowTitle(_("Find software on line"))
        self.resize(1040, 700)
        self.generation = 0
        self.repos = []
        self.flatpak_remotes = []
        self.flatpak_installed = set()
        self.icons = {}

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.term = QLineEdit(self)
        self.term.setObjectName("bigsearch")
        self.term.setPlaceholderText(_("Name of the program or package, for example zed"))
        self.term.addAction(icon("edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.term.setText(term)
        self.term.returnPressed.connect(self.search)
        top.addWidget(self.term, 1)
        top.addWidget(button(_("Search"), "edit-find", self.search, primary=True))
        lay.addLayout(top)
        lay.addWidget(muted(_("Looks in the openSUSE Build Service, in Fedora COPR and in Flathub. Only the name you "
                              "type is sent. Nothing changes until you press Apply in the queue bar.")))

        self.tabs = QTabWidget(self)
        self.obs = ResultPanel("obs", [_("Package"), _("Project"), _("Version"), _("Trust")], self)
        self.copr = ResultPanel("copr", [_("Package"), _("COPR project"), _("Built for"), _("Version"), _("Trust")],
                                self)
        self.flathub = ResultPanel("flathub", [_("App"), _("Summary"), _("Developer"), _("Trust")], self)
        self.tabs.addTab(self.obs, icon("repository", "folder-download"), _("openSUSE Build Service"))
        self.tabs.addTab(self.copr, icon("fedora", "folder-download"), _("Fedora COPR"))
        self.tabs.addTab(self.flathub, icon("flatpak-discover", "application-vnd.flatpak.ref"), _("Flathub"))
        lay.addWidget(self.tabs, 1)

        # buttons of each source
        self.b_obs_install = button(_("Add the repository and install"), "list-add", lambda: self.obs_add(True),
                                    primary=True)
        self.b_obs_repo = button(_("Only add the repository"), "repository", lambda: self.obs_add(False))
        self.b_obs_page = button(_("Open the project page"), "internet-web-browser", self.obs_page)
        for b in (self.b_obs_page, self.b_obs_repo, self.b_obs_install):
            self.obs.buttons.addWidget(b)
        self.b_copr_add = button(_("Add the repository and install…"), "list-add", self.copr_add)
        self.b_copr_page = button(_("Open the project page"), "internet-web-browser", self.copr_page)
        for b in (self.b_copr_page, self.b_copr_add):
            self.copr.buttons.addWidget(b)
        self.b_fh_page = button(_("Open on Flathub"), "internet-web-browser", self.flathub_page)
        self.b_fh_go = button(_("Open the Flatpak page"), "go-next", self.go_flatpak)
        self.b_fh_install = button(_("Install"), "list-add", self.flathub_install, primary=True)
        for b in (self.b_fh_page, self.b_fh_go, self.b_fh_install):
            self.flathub.buttons.addWidget(b)
        self.b_fh_go.hide()

        self.notice = muted("", self)
        bottom = QHBoxLayout()
        bottom.addWidget(self.notice, 1)
        bottom.addWidget(button(_("Close"), "dialog-close", self.accept))
        lay.addLayout(bottom)

        for panel in (self.obs, self.copr, self.flathub):
            panel.tree.currentItemChanged.connect(lambda cur, _p, pn=panel: self._selected(pn))
            self._selected(panel)
        run_async(self._local_state, on_done=self._local_loaded)
        if term:
            self.search()

    def done(self, result):
        self._closed = True
        super().done(result)

    def closeEvent(self, ev):
        self._closed = True
        super().closeEvent(ev)

    # ------------------------------------------------------------ what is here already
    @staticmethod
    def _local_state():
        repos = backend.repos()
        remotes, installed = [], set()
        if flatpak.available():
            remotes = flatpak.remotes()
            installed = {a["application"] for a in flatpak.installed()}
        return repos, remotes, installed

    def _local_loaded(self, res, err):
        if self._closed or err or not res:
            return
        self.repos, self.flatpak_remotes, self.flatpak_installed = res
        self._selected(self.flathub)

    def _repo_with_url(self, url):
        """A repository with this address, on the system or already waiting in the queue."""
        want = url.rstrip("/")
        have = next((r for r in self.repos if (r.get("url") or "").rstrip("/") == want), None)
        if have:
            return have
        queued = next((a for a in self.ctx.queue.of("repo-add") if (a.opts.get("url") or "").rstrip("/") == want),
                      None)
        return {"alias": queued.target, "queued": True} if queued else None

    def _flathub_remote(self):
        """The Flathub remote to install from: the system one if there is one."""
        hub = [r for r in self.flatpak_remotes if "flathub.org" in (r.get("url") or "") and r.get("enabled", True)]
        hub.sort(key=lambda r: r.get("installation") != "system")
        return hub[0] if hub else None

    # ------------------------------------------------------------ searching
    def search(self):
        term = self.term.text().strip()
        if not term:
            return
        self.generation += 1
        gen = self.generation
        for panel, label in ((self.obs, _("openSUSE Build Service")), (self.copr, _("Fedora COPR")),
                             (self.flathub, _("Flathub"))):
            panel.tree.clear()
            panel.status.setText(_("Searching…"))
            self.tabs.setTabText(self.tabs.indexOf(panel), label)
        if online.valid_term(term):
            run_async(online.search_obs, term, on_done=lambda r, e: self._got(gen, self.obs, r, e))
            run_async(online.search_copr, term, on_done=lambda r, e: self._got(gen, self.copr, r, e))
        else:
            msg = _("Type a package name: letters, digits and . _ + - only.")
            self.obs.status.setText(msg)
            self.copr.status.setText(msg)
        run_async(self._search_flatpak, term, on_done=lambda r, e: self._got(gen, self.flathub, r, e))

    def _search_flatpak(self, term):
        installed = self.flatpak_installed
        rows = online.search_flathub(term, installed)
        seen = {r["app_id"] for r in rows}
        # apps of other remotes configured here (Flathub is already covered by its own search)
        if flatpak.available():
            for r in flatpak.search(term):
                remotes = [x for x in r["remotes"].split(",") if x and "flathub" not in x.lower()]
                if r["application"] in seen or not remotes:
                    continue
                rows.append({"app_id": r["application"], "name": r["name"] or r["application"],
                             "summary": r["description"], "developer": "", "icon_url": "", "verified": False,
                             "installs": 0, "installed": r["application"] in installed, "remote": remotes[0],
                             "trust": online.UNVERIFIED})
        return rows

    def _got(self, gen, panel, rows, err):
        if self._closed or gen != self.generation:
            return  # the dialog is closed, or an older search
        idx = self.tabs.indexOf(panel)
        base = {"obs": _("openSUSE Build Service"), "copr": _("Fedora COPR"), "flathub": _("Flathub")}[panel.kind]
        if err:
            panel.status.setText(_("Could not search: {error}").format(error=err))
            panel.status.setStyleSheet(f"color: {RED};")
            return
        panel.status.setStyleSheet("color: palette(placeholder-text);")
        rows = rows or []
        self.tabs.setTabText(idx, f"{base} ({len(rows)})")
        if not rows:
            panel.status.setText(_("Nothing found with this name."))
        elif panel.kind == "obs":
            exact = sum(1 for r in rows if r["exact"])
            panel.status.setText(ngettext("{n} project publishes exactly this name for Tumbleweed; similar names "
                                          "follow.", "{n} projects publish exactly this name for Tumbleweed; similar "
                                          "names follow.", exact).format(n=exact) if exact else
                                 _("No project publishes exactly this name; these names are similar."))
        elif panel.kind == "copr":
            panel.status.setText(ngettext("{n} COPR project built a package with this exact name.",
                                          "{n} COPR projects built a package with this exact name.",
                                          len(rows)).format(n=len(rows)))
        else:
            panel.status.setText(ngettext("{n} app found.", "{n} apps found.", len(rows)).format(n=len(rows)))
        for r in rows:
            self._add_row(panel, r)
        for c in range(panel.tree.columnCount()):
            panel.tree.resizeColumnToContents(c)
        if panel.tree.columnWidth(1) > 380:
            panel.tree.setColumnWidth(1, 380)
        if panel.tree.topLevelItemCount():
            panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
        if panel.kind in ("obs", "copr") and not self.ctx.icon_catalog:
            self._icon_tries = 0
            QTimer.singleShot(2000, self, self._refresh_pkg_icons)  # the dialog as context: no call once it is gone
        if panel.kind == "flathub":
            for r in rows[:40]:
                if r.get("icon_url"):
                    run_async(online.icon_file, r["icon_url"],
                              on_done=lambda path, e, app=r["app_id"], g=gen: self._icon(g, app, path))

    def _add_row(self, panel, r):
        color, short, _title, _text = trust_of(r, panel.kind)
        if panel.kind == "obs":
            vals = [r["name"], r["project"], r["version"] + ("  ✓ " + _("installed") if r["installed"] and r["exact"]
                                                              else ""), short]
            ic = self._pkg_icon(r["name"])
        elif panel.kind == "copr":
            vals = [r["name"], r["project"], r["chroot"].rsplit("-", 1)[0], r["version"], short]
            ic = self._pkg_icon(r["name"])
        else:
            vals = [r["name"] + ("  ✓ " + _("installed") if r["installed"] else ""), r["summary"], r["developer"]
                    or r["remote"], short]
            ic = icon("application-vnd.flatpak.ref", "package-x-generic")
        it = QTreeWidgetItem(panel.tree, vals)
        it.setData(0, Qt.ItemDataRole.UserRole, r)
        it.setIcon(0, ic)
        it.setIcon(len(vals) - 1, dot(color))
        if panel.kind == "obs" and r["exact"]:
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
        if panel.kind == "flathub":
            it.setToolTip(1, r["summary"])

    def _pkg_icon(self, name):
        found = (self.ctx.icon_catalog or {}).get(name)
        ic = QIcon()
        if found and found[0] == "file":
            ic = QIcon(found[1])
        elif found and found[0] == "theme":
            ic = QIcon.fromTheme(found[1])
        return ic if not ic.isNull() else icon("package-x-generic")

    def _refresh_pkg_icons(self):
        """The icon catalogue loads in the background when the window opens: use it once it is there."""
        if self._closed:
            return
        if not self.ctx.icon_catalog:
            self._icon_tries = getattr(self, "_icon_tries", 0) + 1
            if self._icon_tries < 15:
                QTimer.singleShot(2000, self, self._refresh_pkg_icons)
            return
        for panel in (self.obs, self.copr):
            for i in range(panel.tree.topLevelItemCount()):
                it = panel.tree.topLevelItem(i)
                it.setIcon(0, self._pkg_icon(it.data(0, Qt.ItemDataRole.UserRole)["name"]))

    def _icon(self, gen, app, path, *_):
        if self._closed or gen != self.generation or not path:
            return
        ic = QIcon(path)
        for i in range(self.flathub.tree.topLevelItemCount()):
            it = self.flathub.tree.topLevelItem(i)
            r = it.data(0, Qt.ItemDataRole.UserRole)
            if r and r["app_id"] == app:
                it.setIcon(0, ic)

    # ------------------------------------------------------------ the selected result
    def _selected(self, panel):
        r = panel.current()
        if panel.kind == "obs":
            for b in (self.b_obs_install, self.b_obs_repo, self.b_obs_page):
                b.setEnabled(bool(r))
        elif panel.kind == "copr":
            for b in (self.b_copr_add, self.b_copr_page):
                b.setEnabled(bool(r))
        else:
            hub = self._flathub_remote()
            local_remote = r and r.get("remote") != "flathub"
            self.b_fh_install.setEnabled(bool(r) and (bool(hub) or bool(local_remote)))
            self.b_fh_page.setEnabled(bool(r) and not local_remote)
            self.b_fh_go.setVisible(bool(r) and not hub and not local_remote)
        if not r:
            panel.about.setText("")
            panel.about.setStyleSheet("")
            return
        color, short, title, text = trust_of(r, panel.kind)
        extra = ""
        if panel.kind == "obs":
            have = self._repo_with_url(r["url"])
            if r["trust"] == online.OFFICIAL:
                self.b_obs_install.setText(_("Install"))
                self.b_obs_repo.setEnabled(False)
            else:
                self.b_obs_install.setText(_("Add the repository and install"))
                self.b_obs_repo.setEnabled(not have)
            if have:
                extra = "<br><i>" + html.escape(_("This repository is already added here as “{name}”.").format(
                    name=have["name"])) + "</i>"
            if r["trust"] == online.PERSONAL:
                short = severity_label(WARN) + " · " + short
        elif panel.kind == "copr":
            if not r.get("tumbleweed"):
                short = severity_label(SERIOUS) + " · " + short
            extra = "<br>" + html.escape(_("Repository: {url}").format(url=r["url"]))
        else:
            if r.get("remote") != "flathub":
                extra = "<br>" + html.escape(_("From the Flatpak remote “{remote}” configured here.").format(
                    remote=r["remote"]))
            elif not self._flathub_remote():
                extra = "<br><b>" + html.escape(_("The Flathub remote is not configured here: add it in the Flatpak "
                                                  "page first.")) + "</b>"
            if r.get("installs"):
                extra += "<br>" + html.escape(ngettext("{n} installation last month", "{n} installations last month",
                                                       r["installs"]).format(n=r["installs"]))
        panel.set_about(color, short, title, text, extra)

    # ------------------------------------------------------------ actions
    def _queued(self, text):
        self.notice.setText(text)
        self.notice.setStyleSheet(f"color: {GREEN}; font-weight: 600;")

    def obs_add(self, install):
        r = self.obs.current()
        if not r:
            return
        if r["trust"] == online.OFFICIAL:
            self._install(r["name"], r["name"])
            self._queued(_("Added to the queue: install {name}.").format(name=r["name"]))
            return
        if r["trust"] == online.PERSONAL and not ask(self, _("Personal project"), _(
                "{project} belongs to one person and nobody else reviews it. Add it anyway?").format(
                project=r["project"])):
            return
        have = self._repo_with_url(r["url"])
        alias = have["alias"] if have else self._free_alias(online.obs_alias(r["project"]))
        if not have:
            self.ctx.queue.add(Action("repo-add", alias, url=r["url"], name=r["project"], autorefresh=True,
                                      priority=99, gpg="default"))
        if install:
            self._install(r["name"], f"{alias}:{r['name']}")
        self._queued(_("Added to the queue: the repository {project}{and_install}.").format(
            project=r["project"], and_install=(_(" and {name} from it").format(name=r["name"]) if install else "")))
        if not have:
            self._offer_key(online.obs_key_url(r), alias, r["project"])

    def copr_add(self):
        r = self.copr.current()
        if not r:
            return
        color, short, title, text = trust_of(r, "copr")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning if r.get("tumbleweed") else QMessageBox.Icon.Critical)
        box.setWindowTitle(_("Fedora COPR"))
        box.setText(title)
        box.setInformativeText(text + "\n\n" + _("Add the repository {project} ({chroot}) and install {name} from "
                                                 "it?").format(project=r["project"], chroot=r["chroot"],
                                                               name=r["name"]))
        plain(box)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        have = self._repo_with_url(r["url"])
        alias = have["alias"] if have else self._free_alias(online.copr_alias(r))
        if not have:
            # COPR does not sign its repository metadata, only the packages: zypper -n would refuse the
            # repository; the packages are still checked against the project's key.
            self.ctx.queue.add(Action("repo-add", alias, url=r["url"], name=f"COPR {r['project']} ({r['chroot']})",
                                      autorefresh=True, priority=99, gpg="allow-unsigned-repo"))
        self._install(r["name"], f"{alias}:{r['name']}")
        self._queued(_("Added to the queue: the repository {project} and {name} from it.").format(
            project=r["project"], name=r["name"]))
        if not have:
            self._offer_key(r["key_url"], alias, r["project"], packages_only=True)

    def _install(self, name, target):
        """Queue the install of a package, replacing a queued install of the same package from elsewhere."""
        for a in list(self.ctx.queue.of("install")):
            if a.target == name or a.target.endswith(":" + name):
                self.ctx.queue.discard_action(a)
        self.ctx.queue.add(Action("install", target))

    def _free_alias(self, alias):
        used = {r["alias"] for r in self.repos} | {a.target for a in self.ctx.queue.of("repo-add")}
        base, n = alias, 2
        while alias in used:
            alias, n = f"{base}-{n}", n + 1
        return alias

    def _offer_key(self, url, alias, project, packages_only=False):
        """zypper -n refuses a repository whose key it does not know: offer to import it now."""
        run_async(lambda: self._key_info(url, alias),
                  on_done=lambda res, err: self._key_ready(res, err, project, packages_only, alias))

    @staticmethod
    def _key_info(url, alias):
        k = online.fetch_key(url, alias)
        k["known"] = _rpm_has_key(k["fingerprint"])
        return k

    def _key_ready(self, k, err, project, packages_only=False, alias=None):
        # Only while the dialog is open and the repository is still waiting in the queue: importing a key
        # for a repository that was taken out again would make rpm trust it for nothing.
        if self._closed:
            return
        if alias and not any(a.target == alias for a in self.ctx.queue.of("repo-add")):
            return
        if err:
            self.notice.setText(self.notice.text() + " " + _("Its signing key could not be read ({error}): zypper "
                                                             "will ask about it when it refreshes.").format(error=err))
            return
        if k["known"]:
            return
        fpr = k["fingerprint"]
        nice = " ".join(fpr[i:i + 4] for i in range(0, len(fpr), 4))
        box = QMessageBox(self)
        box.setWindowTitle(_("Import a signing key"))
        if packages_only:
            box.setText(_("The packages of {project} are signed with this key. Import it now, so that they can be "
                          "checked? It asks for the administrator password. From then on, rpm trusts every package "
                          "signed with this key.").format(project=project))
        else:
            box.setText(_("zypper must trust the signing key of {project} before it can read the repository. Import "
                          "it now? It asks for the administrator password. From then on, rpm trusts every package "
                          "signed with this key.").format(project=project))
        box.setInformativeText(_("Fingerprint: {fpr}").format(fpr=nice) + ("\n\n" + _("Name in the key: {uid}").format(
            uid=k["uids"][0]) if k["uids"] else ""))
        plain(box)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if not self.ctx.run([Step("rpm-import", [k["path"], fpr])], _("Import a signing key")):
            error_box(self, _("Import a signing key"), _("Wait until the current command finishes, then add the "
                                                         "repository again to import its key."))

    def flathub_install(self):
        r = self.flathub.current()
        if not r:
            return
        if r.get("remote") != "flathub":
            remote, inst = r["remote"], "system"
            match = [x for x in self.flatpak_remotes if x["name"] == remote]
            if match:
                inst = match[0].get("installation", "system")
        else:
            hub = self._flathub_remote()
            if not hub:
                self.go_flatpak()
                return
            remote, inst = hub["name"], hub.get("installation", "system")
        self.ctx.queue.add(Action("flatpak-install", r["app_id"], remote=remote, installation=inst))
        self._queued(_("Added to the queue: install the Flatpak app {name}.").format(name=r["name"]))

    def obs_page(self):
        r = self.obs.current()
        if r:
            QDesktopServices.openUrl(QUrl(f"https://build.opensuse.org/package/show/{r['project']}/{r['package']}"))

    def copr_page(self):
        r = self.copr.current()
        if r:
            QDesktopServices.openUrl(QUrl(r["page"]))

    def flathub_page(self):
        r = self.flathub.current()
        if r and r.get("remote") == "flathub":
            QDesktopServices.openUrl(QUrl(f"https://flathub.org/apps/{r['app_id']}"))

    def go_flatpak(self):
        self.accept()
        self.ctx.go("flatpak")
