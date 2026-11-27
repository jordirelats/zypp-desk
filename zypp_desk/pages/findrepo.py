# SPDX-License-Identifier: GPL-3.0-or-later
"""Find software on line: openSUSE Build Service, Fedora COPR and Flathub.

Type a program name and see who publishes it, with how much each source can be trusted. The
buttons only put changes in the queue (add a repository, install a package or a Flatpak app, import
the signing key of a repository that was just queued, after its own question); nothing runs until
Apply. The queue imports keys first, so zypper already trusts the key when it adds the repository.

As in the other pages, the list is on the left and the card of the selected result on the right: what it
is, its description and its details. Flathub and COPR send the description with the results; for the Build
Service it is asked for after (the first rows at once, the others when the mouse reaches them or they are
selected).
"""

import html

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, flatpak, keys, online
from ..explain import SERIOUS, WARN, severity_label
from ..i18n import _, ngettext, pgettext
from ..style import BLUE, GREEN, ORANGE, RED, dot
from ..tasks import run_async
from ..txqueue import Action, pkg_ident
from ..question import ask_change, confirm
from ..ui import button, icon, muted, rich_tip
from .apps import Details
from .markbar import flash_queued, queue_label
from .repotools import REPO_ICON, commands_of, personal_project_question, short

OBS_DESC_FIRST = 40  # Build Service results whose description is asked for as soon as they show

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


class ResultCard(Details):
    """The card of the selected result, as in Applications, with its description under what it is."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.desc = QLabel(self)
        self.desc.setWordWrap(True)
        self.desc.setTextFormat(Qt.TextFormat.PlainText)  # it comes from the network
        self.desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout().insertWidget(self.layout().indexOf(self.summary) + 1, self.desc)
        self.info.setOpenExternalLinks(True)  # the home page; only http(s) addresses get a link
        self.clear(_("Choose a result from the list."))

    def clear(self, text=""):
        super().clear(text)
        if hasattr(self, "desc"):
            self.desc.setText("")

    def show_result(self, ic, name, summary, description, rows, note=""):
        self.show_app(ic, name, summary, rows, note)
        self.desc.setText(description or "")


class ResultPanel(QWidget):
    """One source: a status line, the list of results with the card of the selected one beside it, what the
    selected one means and its buttons."""

    def __init__(self, kind, columns, parent=None):
        super().__init__(parent)
        self.kind = kind
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        self.status = muted("", self)
        lay.addWidget(self.status)
        body = QHBoxLayout()
        body.setSpacing(12)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(columns)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        body.addWidget(self.tree, 1)
        self.card = ResultCard(self)
        scroll = QScrollArea(self)  # a long description scrolls inside the card's column
        scroll.setWidget(self.card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(self.card.width() + scroll.verticalScrollBar().sizeHint().width() + 2)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        body.addWidget(scroll)
        lay.addLayout(body, 1)
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
        self.queue_lbl = queue_label(self)  # in front of the buttons that only put something in the queue

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
        self.resize(1120, 720)  # the list and, beside it, the card of the selected result
        self.generation = 0
        self.repos = []
        self.flatpak_remotes = []
        self.flatpak_installed = set()
        self.icons = {}
        self.obs_desc = {}  # (project, package) → obs_description() or None: kept while the window is open
        self._obs_asking = set()
        self._pkg_term = None  # the package name looked for, when it is not what was typed

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.term = QLineEdit(self)
        self.term.setObjectName("bigsearch")
        self.term.setPlaceholderText(_("Name of the program or package, for example zed"))
        self.term.addAction(icon("edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.term.setText(term)
        self.term.returnPressed.connect(self.search)
        top.addWidget(self.term, 1)
        # the window's one green button: the buttons below only put something in the queue
        b_search = button(_("Search"), "edit-find", self.search, primary=True)
        b_search.setToolTip(_("Look for this name in the openSUSE Build Service, Fedora COPR and Flathub. Only the "
                              "name is sent. It changes nothing."))
        top.addWidget(b_search)
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

        # buttons of each source: the ones after “To the queue:” only put changes in the queue, so none is green;
        # their labels name the selected result (_selected)
        self.b_obs_install = button(_("Add the repository and install…"), "list-add", lambda: self.obs_add(True))
        self.b_obs_repo = button(_("Only add the repository…"), "repository", lambda: self.obs_add(False))
        self.b_obs_page = button(_("Open the project page"), "internet-web-browser", self.obs_page)
        for b in (self.b_obs_page, self.obs.queue_lbl, self.b_obs_repo, self.b_obs_install):
            self.obs.buttons.addWidget(b)
        self.b_copr_add = button(_("Add the repository and install…"), "list-add", self.copr_add)
        self.b_copr_page = button(_("Open the project page"), "internet-web-browser", self.copr_page)
        for b in (self.b_copr_page, self.copr.queue_lbl, self.b_copr_add):
            self.copr.buttons.addWidget(b)
        self.b_fh_page = button(_("Open on Flathub"), "internet-web-browser", self.flathub_page)
        self.b_fh_go = button(_("Open the Flatpak page"), "go-next", self.go_flatpak)
        self.b_fh_go.setToolTip(_("Close this window and go to Flatpak, where the Flathub remote can be added."))
        self.b_fh_install = button(_("Install"), "list-add", self.flathub_install)
        for b in (self.b_fh_page, self.b_fh_go, self.flathub.queue_lbl, self.b_fh_install):
            self.flathub.buttons.addWidget(b)
        self.b_fh_go.hide()

        self.notice = muted("", self)
        bottom = QHBoxLayout()
        bottom.addWidget(self.notice, 1)
        b_close = button(_("Close"), "dialog-close", self.accept)
        b_close.setToolTip(_("Close this window. What you put in the queue stays there."))
        bottom.addWidget(b_close)
        lay.addLayout(bottom)

        for panel in (self.obs, self.copr, self.flathub):
            panel.tree.currentItemChanged.connect(lambda cur, _p, pn=panel: self._selected(pn))
            self._selected(panel)
        # the description of a Build Service result is asked for when the mouse reaches it (or it is selected)
        self.obs.tree.setMouseTracking(True)
        self.obs.tree.itemEntered.connect(lambda it, _c: self._obs_desc_near(it))
        self.obs.tree.currentItemChanged.connect(lambda cur, _p: self._obs_desc_near(cur))
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
        pkg = online.package_term(term)  # 'obs studio' is looked for as 'obs-studio'
        self._pkg_term = pkg if pkg != term else None
        if pkg:
            run_async(online.search_obs, pkg, on_done=lambda r, e: self._got(gen, self.obs, r, e))
            run_async(online.search_copr, pkg, on_done=lambda r, e: self._got(gen, self.copr, r, e))
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
                             "summary": r["description"], "description": "", "developer": "", "icon_url": "",
                             "verified": False,
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
        if self._pkg_term and panel.kind in ("obs", "copr"):
            panel.status.setText(panel.status.text() + " " + _("Looked for as the package name “{name}”.").format(
                name=self._pkg_term))
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
        if panel.kind == "obs":
            self._want_obs_desc([self._obs_key(r) for r in rows[:OBS_DESC_FIRST]])
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
        self._set_tip(panel, it, r)

    # ------------------------------------------------------------ what each result is (its card and tooltip)
    @staticmethod
    def _obs_key(r):
        return (r["project"], r.get("package") or r["name"])

    def _about(self, kind, r):
        """(what it is, its description, notes) of a result; for the Build Service once its description is in."""
        if kind == "obs":
            key = self._obs_key(r)
            if key not in self.obs_desc:
                return _("Looking up its description…"), "", []
            d = self.obs_desc[key]
            if not d:
                return _("Its description could not be read."), "", []
            pkg = key[1].split(":", 1)[0]
            return d.get("title", ""), d.get("description", ""), [
                _("Built from the sources of {package}.").format(package=pkg) if pkg != r["name"] else ""]
        if kind == "copr":
            return _("COPR project {project}").format(project=r["project"]), r.get("description") or "", [
                _("This project has no description: it is often a test build or a rebuild of Fedora's own "
                  "packages.") if r.get("description") == "" else "",
                _("Its newest build is for Fedora {n}, which Fedora no longer keeps: the project may have been "
                  "left behind.").format(n=r["fedora"]) if r.get("stale") else ""]
        return r.get("summary", ""), r.get("description", ""), []

    def _set_tip(self, panel, it, r):
        """A short tooltip, the name and what it is: the description is in the card beside the list."""
        summary, _desc, _notes = self._about(panel.kind, r)
        tip = rich_tip(r["name"], summary)
        for c in range(panel.tree.columnCount()):
            it.setToolTip(c, tip)

    @staticmethod
    def _link(url):
        if not url.startswith(("https://", "http://")):
            return ""
        return f"<a href='{html.escape(url, quote=True)}'>{html.escape(url)}</a>"

    def _show_card(self, panel, it):
        r = it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None
        if not r:
            panel.card.clear(_("Choose a result from the list."))
            return
        summary, desc, notes = self._about(panel.kind, r)
        e = html.escape
        if panel.kind == "obs":
            d = self.obs_desc.get(self._obs_key(r)) or {}
            rows = [(_("Project"), e(r["project"])), (_("Version"), e(r["version"])),
                    (_("Home page"), self._link(d.get("url", "")))]
        elif panel.kind == "copr":
            rows = [(_("Built for"), e(r["chroot"])), (_("Version"), e(r["version"])),
                    (_("Home page"), self._link(r.get("homepage") or ""))]
        else:
            rows = [(_("Developer"), e(r.get("developer") or "")), (_("App ID"), e(r["app_id"]))]
        panel.card.show_result(it.icon(0), r["name"], summary, desc, rows, "\n\n".join(n for n in notes if n))

    def _want_obs_desc(self, keys):
        keys = [k for k in dict.fromkeys(keys) if k not in self.obs_desc and k not in self._obs_asking]
        if not keys:
            return
        self._obs_asking.update(keys)
        run_async(online.obs_descriptions, keys, on_done=lambda res, err, ks=keys: self._obs_described(ks, res))

    def _obs_desc_near(self, it):
        """The mouse reached a result (or it was selected): its description, and the next rows' ones."""
        if it is None or self._closed:
            return
        tree = self.obs.tree
        start = tree.indexOfTopLevelItem(it)
        rows = [tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                for i in range(max(0, start), min(tree.topLevelItemCount(), start + 12))]
        self._want_obs_desc([self._obs_key(r) for r in rows if r])

    def _obs_described(self, keys, res):
        if self._closed:
            return
        self._obs_asking.difference_update(keys)
        for k in keys:
            self.obs_desc[k] = (res or {}).get(k)
        tree, done = self.obs.tree, set(keys)
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            r = it.data(0, Qt.ItemDataRole.UserRole)
            if r and self._obs_key(r) in done:
                self._set_tip(self.obs, it, r)
        cur = tree.currentItem()
        r = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        if r and self._obs_key(r) in done:
            self._show_card(self.obs, cur)

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
            if panel.tree.currentItem() is not None:
                self._show_card(panel, panel.tree.currentItem())

    def _icon(self, gen, app, path, *_):
        if self._closed or gen != self.generation or not path:
            return
        ic = QIcon(path)
        for i in range(self.flathub.tree.topLevelItemCount()):
            it = self.flathub.tree.topLevelItem(i)
            r = it.data(0, Qt.ItemDataRole.UserRole)
            if r and r["app_id"] == app:
                it.setIcon(0, ic)
                if it is self.flathub.tree.currentItem():
                    self._show_card(self.flathub, it)

    # ------------------------------------------------------------ the selected result
    def _no_selection_labels(self, panel):
        """The buttons of a source with no result selected: disabled, with what they would do."""
        pick = _("Select a result in the list first.")
        if panel.kind == "obs":
            self.b_obs_install.setText(_("Add the repository and install…"))
            self.b_obs_repo.setText(_("Only add the repository…"))
            buttons = (self.b_obs_install, self.b_obs_repo, self.b_obs_page)
        elif panel.kind == "copr":
            self.b_copr_add.setText(_("Add the repository and install…"))
            buttons = (self.b_copr_add, self.b_copr_page)
        else:
            self.b_fh_install.setText(_("Install"))
            buttons = (self.b_fh_install, self.b_fh_page)
        for b in buttons:
            b.setToolTip(pick)

    def _label_buttons(self, panel, r):
        """Labels and tooltips that name the selected result, say that they only fill the queue, and show the
        exact commands. “…” only when a question comes first (a personal project, a signing key to trust)."""
        try:
            self._label_buttons_of(panel, r)
        except (KeyError, TypeError, ValueError):  # a result without some field: the buttons keep plain labels
            pass

    def _label_buttons_of(self, panel, r):
        later = _("Nothing changes until you apply the queue.")
        s = short(r["name"])
        if panel.kind == "obs":
            have = self._repo_with_url(r["url"])
            self.b_obs_page.setToolTip(
                _("Open the page of {project} in the web browser.").format(project=r["project"])
                + "\n" + f"https://build.opensuse.org/package/show/{r['project']}/{r['package']}")
            if r["trust"] == online.OFFICIAL:
                self.b_obs_install.setText(_("Install {name}").format(name=s) if s else _("Install it"))
                self.b_obs_install.setToolTip(
                    _("Puts in the queue: install {name} from your openSUSE repositories.").format(name=r["name"])
                    + " " + later + "\n" + "\n".join(commands_of([Action("install", r["name"], package=r["name"])])))
                self.b_obs_repo.setText(_("Only add the repository"))
                self.b_obs_repo.setToolTip(_("It is already in your openSUSE repositories: no repository needs to be "
                                             "added."))
                return
            personal = r["trust"] == online.PERSONAL  # a question about the person comes first
            alias = have["alias"] if have else self._free_alias(online.obs_alias(r["project"]))
            add = [] if have else [Action("repo-add", alias, url=r["url"], name=r["project"], autorefresh=True,
                                          priority=99, gpg="default")]
            install = [Action("install", f"{alias}:{r['name']}", package=r["name"])]
            key = "" if have else " " + _("Then it offers to trust the signing key of the project.")
            if have:
                text = _("Install {name} from it").format(name=s) if s else _("Install it from this repository")
                tip = _("Puts in the queue: install {name} from the repository you already have.").format(
                    name=r["name"])
            else:
                text = (_("Add the repository and install {name}").format(name=s) if s else
                        _("Add the repository and install it"))
                tip = _("Puts in the queue: add the repository {project} and install {name} from it.").format(
                    project=r["project"], name=r["name"])
            self.b_obs_install.setText(text + ("…" if personal or not have else ""))
            self.b_obs_install.setToolTip(tip + key + " " + later + "\n" + "\n".join(commands_of(add + install)))
            self.b_obs_repo.setText(_("Only add the repository") + ("" if have else "…"))
            self.b_obs_repo.setToolTip(
                _("This repository is already added here.") if have else
                _("Puts in the queue: add the repository {project}, without installing anything.").format(
                    project=r["project"]) + key + " " + later + "\n" + "\n".join(commands_of(add)))
        elif panel.kind == "copr":
            have = self._repo_with_url(r["url"])
            alias = have["alias"] if have else self._free_alias(online.copr_alias(r))
            add = [] if have else [Action("repo-add", alias, url=r["url"], name=f"COPR {r['project']} ({r['chroot']})",
                                          autorefresh=True, priority=99, gpg="allow-unsigned-repo")]
            install = [Action("install", f"{alias}:{r['name']}", package=r["name"])]
            self.b_copr_add.setText(_("Add the repository and install {name}…").format(name=s) if s else
                                    _("Add the repository and install it…"))
            self.b_copr_add.setToolTip(
                _("Asks first, then puts in the queue: add the COPR repository {project} and install {name} from "
                  "it.").format(project=r["project"], name=r["name"]) + " " + later + "\n"
                + "\n".join(commands_of(add + install)))
            self.b_copr_page.setToolTip(_("Open the page of {project} in the web browser.").format(project=r["project"])
                                        + "\n" + (r.get("page") or ""))
        else:
            self.b_fh_install.setText(_("Install {name}").format(name=s) if s else _("Install it"))
            local_remote = r.get("remote") != "flathub"
            hub = self._flathub_remote()
            if local_remote or hub:
                if local_remote:
                    remote = r["remote"]
                    match = [x for x in self.flatpak_remotes if x["name"] == remote]
                    inst = match[0].get("installation", "system") if match else "system"
                else:
                    remote, inst = hub["name"], hub.get("installation", "system")
                self.b_fh_install.setToolTip(
                    _("Puts in the queue: install the Flatpak app {name} from {remote}.").format(
                        name=r["name"], remote=remote) + " " + later + "\n"
                    + "\n".join(commands_of([Action("flatpak-install", r["app_id"], remote=remote,
                                                    installation=inst)])))
            else:
                self.b_fh_install.setToolTip(_("The Flathub remote is not configured here: add it in the Flatpak "
                                               "page first."))
            self.b_fh_page.setToolTip(_("Remotes other than Flathub have no page there.") if local_remote else
                                      _("Open the page of {name} on Flathub in the web browser.").format(name=r["name"])
                                      + "\n" + f"https://flathub.org/apps/{r['app_id']}")

    def _selected(self, panel):
        r = panel.current()
        self._show_card(panel, panel.tree.currentItem())
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
            self._no_selection_labels(panel)
            panel.about.setText("")
            panel.about.setStyleSheet("")
            return
        self._label_buttons(panel, r)
        color, short_, title, text = trust_of(r, panel.kind)
        extra = ""
        if panel.kind == "obs":
            have = self._repo_with_url(r["url"])
            if r["trust"] == online.OFFICIAL:
                self.b_obs_repo.setEnabled(False)
            else:
                self.b_obs_repo.setEnabled(not have)
            if have:
                extra = "<br><i>" + html.escape(_("This repository is already added here as “{name}”.").format(
                    name=have.get("name") or have["alias"])) + "</i>"  # one waiting in the queue has only its alias
            if r["trust"] == online.PERSONAL:
                short_ = severity_label(WARN) + " · " + short_
        elif panel.kind == "copr":
            if not r.get("tumbleweed"):
                short_ = severity_label(SERIOUS) + " · " + short_
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
        panel.set_about(color, short_, title, text, extra)

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
            flash_queued(self.b_obs_install, lambda: self._selected(self.obs))
            return
        if r["trust"] == online.PERSONAL and not personal_project_question(
                self, r["project"], r["url"], install=r["name"] if install else None):
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
        flash_queued(self.b_obs_install if install else self.b_obs_repo, lambda: self._selected(self.obs))
        if not have:
            self._offer_key(online.obs_key_url(r), alias, r["project"])

    def copr_add(self):
        r = self.copr.current()
        if not r:
            return
        color, short, title, text = trust_of(r, "copr")
        if not confirm(
                self, _("Add the repository {project} ({chroot}) and install {name} from it?").format(
                    project=r["project"], chroot=r["chroot"], name=r["name"]), _("Add them to the queue"),
                window_title=_("Fedora COPR"), icon_name="dialog-warning" if r.get("tumbleweed") else "dialog-error",
                items=[(REPO_ICON, pgettext("change", "Add the repository “{name}”").format(
                            name=f"COPR {r['project']} ({r['chroot']})"), r["url"]),
                       ("application-x-rpm|package-x-generic", pgettext("change", "Install {name}").format(
                           name=r["name"]), _("From this repository."))],
                warning=title + "\n" + text,
                means=_("it only goes to the queue: nothing changes until you apply it."),
                password=False, help_topic="repos", queue=True):
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
        flash_queued(self.b_copr_add, lambda: self._selected(self.copr))
        if not have:
            self._offer_key(r["key_url"], alias, r["project"], packages_only=True)

    def _install(self, name, target):
        """Queue the install of a package, replacing a queued install of the same package from elsewhere.
        package= is the name: the rest of the window (Packages, solutions) finds 'alias:name' by it."""
        for a in list(self.ctx.queue.of("install")):
            if pkg_ident(a) == name or a.opts.get("package") == name:
                self.ctx.queue.discard_action(a)
        self.ctx.queue.add(Action("install", target, package=name))

    def _free_alias(self, alias):
        used = {r["alias"] for r in self.repos} | {a.target for a in self.ctx.queue.of("repo-add")}
        base, n = alias, 2
        while alias in used:
            alias, n = f"{base}-{n}", n + 1
        return alias

    def _offer_key(self, url, alias, project, packages_only=False):
        """zypper -n refuses a repository whose key it does not know: offer to put its import in the queue too."""
        run_async(self._key_info, url,
                  on_done=lambda res, err: self._key_ready(res, err, project, packages_only, alias))

    @staticmethod
    def _key_info(url):
        k = online.fetch_key(url)  # read with a throwaway gpg home; gpg's own copy kept for the queue
        k["known"] = k["fingerprint"] in keys.installed_keys()
        return k

    def _key_ready(self, k, err, project, packages_only=False, alias=None):
        # Only while the dialog is open and the repository is still waiting in the queue: a key for a repository
        # that was taken out again would make rpm trust it for nothing.
        if self._closed:
            return
        if alias and not any(a.target == alias for a in self.ctx.queue.of("repo-add")):
            return
        if err:
            self.notice.setText(self.notice.text() + " " + _("Its signing key could not be read ({error}): zypper "
                                                             "will ask about it when it refreshes.").format(error=err))
            return
        fpr = k["fingerprint"]
        if k["known"] or self.ctx.queue.key_state(fpr) == "key-import":
            return
        if packages_only:
            lead = _("The packages of {project} are signed with this key. Add its import to the queue, so that they "
                     "can be checked?").format(project=project)
        else:
            lead = _("zypper must trust the signing key of {project} before it can read the repository. Add its "
                     "import to the queue?").format(project=project)
        detail = _("Fingerprint: {fpr}").format(fpr=keys.nice_fpr(fpr))
        if k["uids"]:
            detail += "\n" + _("Name in the key: {uid}").format(uid=k["uids"][0])
        warning = _("From then on, rpm trusts every package signed with this key.")
        if k.get("revoked") or k.get("expired"):
            warning = (_("Its owner revoked this key: it must not be used.") if k.get("revoked") else
                       _("This key has expired.")) + "\n\n" + warning
        buttons = [("no", _("Not yet"), False)]
        if not k.get("revoked"):
            buttons.append(("yes", _("Add to the queue"), True))
        answer = ask_change(
            self, _("Trust the signing key of {project}?").format(project=project),
            window_title=_("Import a signing key"),
            icon_name="security-high|object-locked|dialog-password", lead=lead,
            items=[("security-high|object-locked|dialog-password",
                    pgettext("change", "Trust the signing key “{name}”").format(name=k.get("name") or project),
                    detail)],
            means=_("it is imported first when you apply the queue, before the repository is added. Nothing changes "
                    "until then."),
            warning=warning, password=False, buttons=buttons, help_topic="repos", queue=True)
        if answer != "yes":
            return
        link = {"for_repo": alias} if alias else {}  # the queue review: the repository needs this key
        self.ctx.queue.add(Action("key-import", fpr, path=k["path"], name=k.get("name") or project, **link))
        self._queued(self.notice.text() + " " + _("Its signing key too."))

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
        flash_queued(self.b_fh_install, lambda: self._selected(self.flathub))

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
