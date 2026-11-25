# SPDX-License-Identifier: GPL-3.0-or-later
"""Repositories: enable, priority, refresh, keep packages, add, remove, signing keys.

Each repository (and each Flatpak remote) is also checked in the background: does its address still
answer? The list can be shown as a table or as cards.
"""

import threading
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMenu, QMessageBox, QScrollArea, QSpinBox, QStackedWidget, QTabWidget,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, flatpak, repocheck
from ..i18n import _, ngettext
from ..runner import Step
from ..style import GREEN, RED, dot
from ..tasks import emit_safely, run_async
from ..txqueue import Action
from ..ui import Chip, Page, ask, button, error_box, icon, info_box, muted, plain, tool

COL_ON, COL_NAME, COL_HEALTH, COL_PRIO, COL_AUTO, COL_KEEP, COL_GPG, COL_URL = range(8)
FCOL_NAME, FCOL_HEALTH, FCOL_ON, FCOL_INST, FCOL_URL = range(5)
GREY = "#95a5a6"
STATE_COLOR = {"ok": GREEN, "fail": RED, "skip": GREY, None: GREY}
FAIL_TINT = QColor(218, 68, 83, 30)
MAX_CHECKS = 8
CARD_MIN = 290


def health_text(res):
    return res["text"] if res else _("not checked yet")


def short_url(url, home=str(Path.home())):
    u = urllib.parse.unquote(url)
    for prefix in ("https://", "http://", "dir:", "file://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith(home):
        u = "~" + u[len(home):]
    return u.rstrip("/")


# ------------------------------------------------------------------ background checks

class Checker(QObject):
    """Runs repocheck functions in daemon threads, at most MAX_CHECKS at once, and reports each result.

    Daemon threads: closing the window never waits for a slow server. Results of an older run
    (another "Check them" was pressed meanwhile) carry an older generation and are ignored.
    """

    result = Signal(int, str, object)  # generation, key, result

    def __init__(self, parent=None):
        super().__init__(parent)
        self.generation = 0
        self._sem = threading.BoundedSemaphore(MAX_CHECKS)

    def start(self, jobs, new=True):
        """jobs: list of (key, function, argument). new=False adds them to the current run."""
        if new:
            self.generation += 1
        for key, fn, arg in jobs:
            threading.Thread(target=self._one, args=(self.generation, key, fn, arg), daemon=True).start()

    def _one(self, gen, key, fn, arg):
        with self._sem:
            if gen != self.generation:
                return
            try:
                res = fn(arg)
            except Exception as e:  # a check must never take the window down
                res = {"state": "fail", "text": _("could not check"), "detail": str(e)}
        try:
            emit_safely(self.result, gen, key, res)  # nothing is emitted once the program is quitting
        except RuntimeError:
            pass  # the window was closed meanwhile


# ------------------------------------------------------------------ add dialog

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


# ------------------------------------------------------------------ table rows and cards

class RepoItem(QTreeWidgetItem):
    """Sorts by what the column means: enabled first (then name), priority as a number, failing first."""

    def __lt__(self, other):
        col = self.treeWidget().sortColumn() if self.treeWidget() else COL_ON
        a, b = getattr(self, "keys", None), getattr(other, "keys", None)
        if a and b and col in a and col in b:
            return a[col] < b[col]
        return super().__lt__(other)


class RepoCard(QFrame):
    """One repository (or Flatpak remote) as a rounded card, like the tiles of the home page."""

    clicked = Signal(str)

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("repocard")
        self.setMinimumWidth(CARD_MIN)
        self._name_full = self._url_full = ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.selected = False
        self.failing = False
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel(self)
        self.dot.setFixedSize(16, 16)
        self.name = QLabel(self)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        top.addWidget(self.dot)
        top.addWidget(self.name, 1)
        self.health = QLabel(self)
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addLayout(top)
        left.addWidget(self.health)
        head = QHBoxLayout()
        head.addLayout(left, 1)
        self.side = QVBoxLayout()  # the page puts the priority here
        head.addLayout(self.side)
        v.addLayout(head)
        self.url = muted("", self, wrap=False)
        self.url_row = QHBoxLayout()
        self.url_row.setSpacing(6)
        self.url_row.addWidget(self.url, 1)
        v.addLayout(self.url_row)
        self.side_width = 0  # room taken by the priority, for shortening the name
        self.facts = muted("", self)
        v.addWidget(self.facts)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(10)
        v.addLayout(self.controls)

    def set_header(self, name, url, facts, enabled, style=None):
        self._name_full, self._url_full = name, short_url(url)
        self.name.setToolTip(name)
        if style:
            self.name.setStyleSheet(style)
        elif not enabled:
            self.name.setStyleSheet("color: palette(placeholder-text);")
        self.url.setToolTip(url)
        self.facts.setText(facts)
        self._elide()

    def _elide(self):
        room = max(80, self.width() - 28)
        self.name.setText(QFontMetrics(self.name.font()).elidedText(self._name_full, Qt.TextElideMode.ElideRight,
                                                                    room - 26 - self.side_width))
        link = sum(self.url_row.itemAt(i).widget().sizeHint().width() + 6 for i in range(1, self.url_row.count())
                   if self.url_row.itemAt(i).widget())
        self.url.setText(QFontMetrics(self.url.font()).elidedText(self._url_full, Qt.TextElideMode.ElideMiddle,
                                                                  max(60, room - link)))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def set_health(self, res):
        state = res["state"] if res else None
        self.dot.setPixmap(dot(STATE_COLOR[state], 16, 7).pixmap(16, 16))
        self.health.setText(health_text(res))
        self.health.setStyleSheet(f"color: {STATE_COLOR[state]};" if state in ("ok", "fail") else
                                  "color: palette(placeholder-text);")
        tip = res["detail"] if res else ""
        self.health.setToolTip(tip)
        self.dot.setToolTip(tip)
        self.failing = state == "fail"
        self.restyle()

    def restyle(self):
        border = GREEN if self.selected else ("rgba(218, 68, 83, 0.55)" if self.failing else
                                              "rgba(127, 140, 141, 0.35)")
        bg = "rgba(218, 68, 83, 0.08)" if self.failing else "palette(base)"
        width = 2 if self.selected else 1
        self.setStyleSheet(f"QFrame#repocard {{ border: {width}px solid {border}; border-radius: 14px;"
                           f" background: {bg}; }}")

    def set_selected(self, on):
        self.selected = on
        self.restyle()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(ev)


class CardGrid(QScrollArea):
    """Cards in as many columns as fit."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.inner = QWidget(self)
        self.grid = QGridLayout(self.inner)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setWidget(self.inner)
        self.cards = []
        self.empty = muted("", self.inner)

    def set_cards(self, cards, empty_text=""):
        for c in self.cards:
            c.setParent(None)
            c.deleteLater()
        self.cards = cards
        self.empty.setText(empty_text)
        self.relayout()

    def relayout(self):
        while self.grid.count():
            self.grid.takeAt(0)
        visible = [c for c in self.cards if not c.isHidden()]
        if not visible:
            self.grid.addWidget(self.empty, 0, 0)
            self.empty.show()
            return
        self.empty.hide()
        room = self.viewport().width() - 4
        cols = max(1, (room + 12) // (CARD_MIN + 12))
        width = max(CARD_MIN, (room - 12 * (cols - 1)) // cols)
        for i, c in enumerate(visible):
            c.setFixedWidth(width)
            self.grid.addWidget(c, i // cols, i % cols)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self.relayout)


# ------------------------------------------------------------------ the page

class ReposPage(Page):
    key = "repos"
    title = _("Repositories")
    icon_name = "repository"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.repos = []
        self.remotes = []
        self.health = {}  # "zypper:alias" / "flatpak:installation:name" -> result
        self._filling = False
        self._current = None  # alias of the repository selected in the cards view
        self.items = {}
        self.cards = {}
        self.checker = Checker(self)
        self.checker.result.connect(self._checked)
        self._total = self._done = 0
        self._fill_pending = False
        self._stale = False
        self._cards_stale = True
        self._fcards_stale = True

        # ---- top row: filter, "only failing", view switch
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText(_("Filter by name or address"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.only_fail = Chip(_("Only those that fail"))
        self.only_fail.toggled.connect(lambda _on: self._apply_filter(self.filter.text()))
        self.v_list = self._view_button(_("List"), "view-list-details")
        self.v_cards = self._view_button(_("Cards"), "view-list-icons")
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.v_list)
        group.addButton(self.v_cards)
        (self.v_cards if ctx.settings.get("repos_view", "list") == "cards" else self.v_list).setChecked(True)
        self.v_list.toggled.connect(self._view_changed)
        self.sort = QComboBox(self)
        self.sort.addItem(_("Sort by name"), "name")
        self.sort.addItem(_("Sort by priority"), "priority")
        self.sort.setCurrentIndex(1 if ctx.settings.get("repos_sort") == "priority" else 0)
        self.sort.currentIndexChanged.connect(self._sort_changed)
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.sort)
        top.addWidget(self.only_fail)
        top.addSpacing(12)
        top.addWidget(self.v_list)
        top.addWidget(self.v_cards)

        # ---- status row: what the check found, and what to do with the failing ones
        self.status = QLabel(self)
        self.fix_btn = button(_("Deal with the ones that fail…"), "edit-delete", self.deal_with_failing)
        self.fix_btn.hide()
        srow = QHBoxLayout()
        srow.addWidget(self.status, 1)
        srow.addWidget(self.fix_btn)

        # ---- zypper repositories: table and cards
        self.tree = QTreeWidget(self)
        # TRANSLATORS: column headers; "Refresh" means "refreshed automatically", "Works?" whether the address answers.
        self.tree.setHeaderLabels([_("Enabled"), _("Name"), _("Works?"), _("Priority"), _("Refresh"),
                                   _("Keep packages"), _("Signatures"), _("Address")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_ON, Qt.SortOrder.AscendingOrder)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.currentItemChanged.connect(self._tree_current)
        self.grid = CardGrid(self)
        self.z_stack = QStackedWidget(self)
        self.z_stack.addWidget(self.tree)
        self.z_stack.addWidget(self.grid)
        zpage = QWidget(self)
        zl = QVBoxLayout(zpage)
        zl.setContentsMargins(0, 6, 0, 0)
        zl.addWidget(self.z_stack, 1)
        self.hint = muted(_("Tick or untick to queue a change. Double-click the priority to change it. "
                            "Lower priority numbers win."), self)
        zl.insertWidget(0, self._priority_band())
        zl.addWidget(self.hint)

        # ---- Flatpak remotes
        self.ftree = QTreeWidget(self)
        self.ftree.setHeaderLabels([_("Name"), _("Works?"), _("Enabled"), _("Installation"), _("Address")])
        self.ftree.setRootIsDecorated(False)
        self.ftree.setAlternatingRowColors(True)
        self.fgrid = CardGrid(self)
        self.f_stack = QStackedWidget(self)
        self.f_stack.addWidget(self.ftree)
        self.f_stack.addWidget(self.fgrid)
        fpage = QWidget(self)
        fl = QVBoxLayout(fpage)
        fl.setContentsMargins(0, 6, 0, 0)
        frow = QHBoxLayout()
        frow.addWidget(muted(_("Flatpak apps come from these remotes. They are added, removed and changed in the "
                               "Flatpak section."), self), 1)
        frow.addWidget(button(_("Open the Flatpak section"), "flatpak-discover", lambda: ctx.go("flatpak")))
        fl.addLayout(frow)
        fl.addWidget(self.f_stack, 1)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(zpage, icon("repository", "folder-download"), "zypper")
        self.tabs.addTab(fpage, icon("flatpak-discover", "application-vnd.flatpak.ref"), "Flatpak")

        lay = QVBoxLayout(self)
        lay.addWidget(self._online_banner())
        lay.addLayout(top)
        lay.addLayout(srow)
        lay.addWidget(self.tabs, 1)
        self._view_changed()
        ctx.queue.changed.connect(self._queue_changed)

    def _view_button(self, text, icon_name):
        b = QToolButton(self)
        b.setText(text)
        b.setIcon(icon(icon_name))
        b.setCheckable(True)
        b.setAutoRaise(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        return b

    def header_actions(self):
        more = QToolButton(self)
        more.setText(_("More"))
        more.setIcon(icon("application-menu", "overflow-menu", "open-menu-symbolic"))
        more.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        more.setAutoRaise(True)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction(icon("view-refresh"), _("Refresh all"), self.refresh_all)
        menu.addAction(icon("edit-clear"), _("Clean the cache"), self.clean_cache)
        menu.addAction(icon("document-edit"), _("Change the address…"), self.change_url)
        menu.addAction(icon("view-sort-ascending"), _("Suggest an order of priority…"), self.propose_order)
        menu.addSeparator()
        menu.addAction(icon("object-locked"), _("Import a signing key…"), self.import_key)
        more.setMenu(menu)
        return [tool(_("Add…"), "list-add", self.add_repo),
                tool(_("Search online…"), "edit-find", self.search_online),
                tool(_("Remove"), "list-remove", self.remove_repo),
                tool(_("Refresh"), "view-refresh", self.refresh_selected),
                tool(_("Check them"), "network-connect", self.check_all),
                more]

    def refresh_all(self):
        self.ctx.run([Step("zypper", ["-n", "-v", "refresh"])], _("Refresh every repository"),
                     on_done=lambda ok: self.reload())

    # ------------------------------------------------------------ loading
    def reload(self):
        first = not self.repos
        run_async(backend.repos, on_done=lambda res, err: self._loaded(res, err, first))
        if flatpak.available():
            run_async(flatpak.remotes, on_done=self._remotes_loaded)

    def _loaded(self, repos, err, check=False):
        if err:
            error_box(self, self.title, str(err))
            return
        self.repos = repos
        if check:
            self.check_all()  # it fills the list too
        else:
            self._fill()

    def _remotes_loaded(self, remotes, err):
        if err or remotes is None:
            return
        first = not self.remotes
        self.remotes = remotes
        self._fill_flatpak()
        if first:
            self.checker_extra([(self._fkey(r), repocheck.check_flatpak_remote, r) for r in remotes])

    @staticmethod
    def _fkey(r):
        return f"flatpak:{r['installation']}:{r['name']}"

    # ------------------------------------------------------------ checks
    def check_all(self):
        jobs = [(f"zypper:{r['alias']}", repocheck.check_repo, r) for r in self.repos]
        jobs += [(self._fkey(r), repocheck.check_flatpak_remote, r) for r in self.remotes]
        self.health = {}
        self._fill()
        self._fill_flatpak()
        self._total, self._done = len(jobs), 0
        self.checker.start(jobs)
        self._progress()

    def checker_extra(self, jobs):
        """Remotes that arrive after the repositories: check them too, in the same run while it goes on."""
        busy = self._done < self._total
        if not busy:
            self._total = self._done = 0
        self._total += len(jobs)
        self.checker.start(jobs, new=not busy)
        self._progress()

    def _checked(self, gen, key, res):
        if gen != self.checker.generation:
            return
        self.health[key] = res
        self._done += 1
        if key.startswith("zypper:"):
            self._show_health(key[7:])
        else:
            self._show_fhealth(key)
        self._apply_filter(self.filter.text())
        self._progress()

    def _progress(self):
        done, total = self._done, self._total
        zypper = {k: v for k, v in self.health.items() if k.startswith("zypper:")}
        fails = [k for k, v in zypper.items() if v["state"] == "fail"]
        if done < total:
            self.status.setStyleSheet("color: palette(placeholder-text);")
            self.status.setText(_("Checking the addresses… {done} of {total}").format(done=done, total=total))
        else:
            ok = sum(1 for v in zypper.values() if v["state"] == "ok")
            text = f"<span style='color:{GREEN}'>● " + ngettext("{n} works", "{n} work", ok).format(n=ok) + "</span>"
            if fails:
                text += (f"  &nbsp;<span style='color:{RED}'>● " + ngettext("{n} does not work", "{n} do not work",
                                                                         len(fails)).format(n=len(fails)) + "</span>")
            self.status.setStyleSheet("")
            self.status.setText(text)
            self.tree.resizeColumnToContents(COL_HEALTH)
            self.ftree.resizeColumnToContents(FCOL_HEALTH)
            self._fit_columns()
        self.fix_btn.setVisible(done >= total and bool(fails))
        self._update_tab_titles()

    def _update_tab_titles(self):
        def bad(prefix):
            return sum(1 for k, v in self.health.items() if k.startswith(prefix) and v["state"] == "fail")
        z, f = bad("zypper:"), bad("flatpak:")
        self.tabs.setTabText(0, _("Repositories ({n})").format(n=len(self.repos)) + (
            " · " + ngettext("{n} does not work", "{n} do not work", z).format(n=z) if z else ""))
        self.tabs.setTabText(1, f"Flatpak ({len(self.remotes)})" + (
            " · " + ngettext("{n} does not work", "{n} do not work", f).format(n=f) if f else ""))

    # ------------------------------------------------------------ zypper: table and cards
    def _gpg_text(self, r):
        # TRANSLATORS: what is checked with GPG signatures in a repository.
        return (_("repository and packages") if r["repo_gpgcheck"] and r["pkg_gpgcheck"] else
                _("packages") if r["pkg_gpgcheck"] else _("repository") if r["repo_gpgcheck"] else
                _("on") if r["gpgcheck"] else _("off"))

    def _state(self, r):
        """What the queue changes in a repository: (opts, removing, enabled, autorefresh, priority, card style)."""
        ch = self.ctx.queue.repo_change(r["alias"])
        o = ch.opts if ch and ch.kind == "repo-modify" else {}
        removing = bool(ch and ch.kind == "repo-remove")
        style = None
        if o or removing:
            color = RED if removing else "#1d99f3"
            style = f"color: {color}; font-style: italic;" + (" text-decoration: line-through;" if removing else "")
        return (o, removing, o.get("enabled", r["enabled"]), o.get("autorefresh", r["autorefresh"]),
                o.get("priority", r["priority"]), style)

    def _ordered(self):
        if self.sort.currentData() == "priority":
            return sorted(self.repos, key=lambda r: (not r["enabled"], int(self._state(r)[4]), r["name"].lower()))
        return sorted(self.repos, key=lambda r: (not r["enabled"], r["name"].lower()))

    def _sort_changed(self, *_a):
        self.ctx.settings["repos_sort"] = self.sort.currentData()
        self.ctx.settings.save()
        if self.sort.currentData() == "priority":
            self.tree.sortByColumn(COL_PRIO, Qt.SortOrder.AscendingOrder)
        else:
            self.tree.sortByColumn(COL_ON, Qt.SortOrder.AscendingOrder)
        self._fill()

    def _priority_band(self):
        band = QFrame(self)
        band.setObjectName("card")
        h = QHBoxLayout(band)
        h.setContentsMargins(12, 8, 12, 8)
        arrows = QLabel("⇅", band)
        f = arrows.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        arrows.setFont(f)
        arrows.setStyleSheet("color: #1d99f3;")
        h.addWidget(arrows)
        v = QVBoxLayout()
        v.setSpacing(0)
        title = QLabel("<b>" + _("Priority: the lowest number wins.") + "</b>", band)
        v.addWidget(title)
        self.prio_text = muted("", band)
        v.addWidget(self.prio_text)
        h.addLayout(v, 1)
        h.addWidget(button(_("How it works"), "help-about", self._how_it_works))
        h.addWidget(button(_("Suggest an order…"), "view-sort-ascending", self.propose_order, primary=True))
        return band

    def _update_priority_text(self):
        prios = [int(r["priority"]) for r in self.repos if r["enabled"]]
        same = max(set(prios), key=prios.count) if prios else 99
        n_same = prios.count(same)
        if prios and n_same >= len(prios) - 1:
            text = _("When a package is in more than one repository, zypper takes it from the one with the lowest "
                     "number. Now almost all of yours are {n}: the newest version wins, wherever it comes "
                     "from.").format(n=same)
        else:
            text = _("When a package is in more than one repository, zypper takes it from the one with the lowest "
                     "number.")
        self.prio_text.setText(text)

    def _how_it_works(self):
        from .repotools import _how_it_works
        _how_it_works(self)

    def propose_order(self):
        from .repotools import ProposeOrderDialog
        dlg = ProposeOrderDialog(self.repos, lambda r: self._state(r)[4], self)
        if dlg.exec():
            with self.ctx.queue.batch():
                for r, prio in dlg.chosen():
                    self._queue_change(r, "priority", prio)

    def change_url(self, r=None):
        r = r or self._selected()
        if not r:
            info_box(self, _("Change the address…"), _("Choose a repository first."))
            return
        from .repotools import ChangeUrlDialog
        dlg = ChangeUrlDialog(r, self.health.get(f"zypper:{r['alias']}"), self)
        if dlg.exec():
            ch = self.ctx.queue.repo_change(r["alias"])
            pending = dict(ch.opts) if ch and ch.kind in ("repo-modify", "repo-url") else {}
            self.ctx.queue.add(dlg.action(pending))

    def _fill(self):
        self._update_priority_text()
        self._fill_pending = False
        self._filling = True
        col, order = self.tree.sortColumn(), self.tree.header().sortIndicatorOrder()
        cur_alias = self._current
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self.items = {}
        for r in self._ordered():
            o, removing, enabled, auto, prio, style = self._state(r)
            gpg = self._gpg_text(r)
            it = RepoItem(["", r["name"], "", "", "", "", gpg, r["url"]])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            it.setCheckState(COL_ON, Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
            it.setCheckState(COL_AUTO, Qt.CheckState.Checked if auto else Qt.CheckState.Unchecked)
            it.setCheckState(COL_KEEP, Qt.CheckState.Checked if o.get("keeppackages", r["keeppackages"])
                             else Qt.CheckState.Unchecked)
            it.setData(COL_PRIO, Qt.ItemDataRole.DisplayRole, prio)
            it.setToolTip(COL_NAME, r["alias"] + (f"  ({_('service')}: {r['service']})" if r["service"] else ""))
            it.setToolTip(COL_URL, r["url"])
            if o or removing:
                f = it.font(COL_NAME)
                f.setItalic(True)
                f.setStrikeOut(removing)
                it.setFont(COL_NAME, f)
                it.setForeground(COL_NAME, QBrush(QColor(RED if removing else "#1d99f3")))
            if not r["enabled"]:
                for c in (COL_NAME, COL_URL):
                    it.setForeground(c, QBrush(QColor("gray")) if not o else it.foreground(c))
            self.tree.addTopLevelItem(it)
            self.items[r["alias"]] = it
            self._show_health(r["alias"])
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(col, order)
        for c in range(7):
            self.tree.resizeColumnToContents(c)
        self._fit_columns()
        if cur_alias in self.items:
            self.tree.setCurrentItem(self.items[cur_alias])
        # The cards are only built when they are shown: in the list view they would cost hundreds of widgets.
        if self.v_cards.isChecked():
            self._build_cards()
        else:
            self.cards = {}
            self.grid.set_cards([], "")
            self._cards_stale = True
        self._apply_filter(self.filter.text())
        self._update_tab_titles()
        self._filling = False

    def _build_cards(self):
        self._cards_stale = False
        cards = []
        self.cards = {}
        for r in self._ordered():
            _o, _removing, enabled, auto, prio, style = self._state(r)
            card = RepoCard(r["alias"], self.grid.inner)
            facts = _("signatures: {what}").format(what=self._gpg_text(r))
            card.set_header(r["name"], r["url"], facts, r["enabled"], style)
            on = QCheckBox(_("Enabled"), card)
            on.setChecked(enabled)
            on.toggled.connect(lambda v, r=r: self._queue_change(r, "enabled", v))
            ref = QCheckBox(_("Refresh"), card)
            ref.setToolTip(_("Refresh it automatically"))
            ref.setChecked(auto)
            ref.toggled.connect(lambda v, r=r: self._queue_change(r, "autorefresh", v))
            from .repotools import PriorityStepper
            pr = PriorityStepper(prio, card)
            pr.changed.connect(lambda val, r=r: self._queue_change(r, "priority", val))
            card.side.addWidget(pr, 0, Qt.AlignmentFlag.AlignTop)
            card.side_width = pr.sizeHint().width() + 8
            addr = QToolButton(card)
            addr.setText(_("✎ Address…"))
            addr.setAutoRaise(True)
            addr.setToolTip(_("Change the address of this repository, or find it again"))
            addr.setStyleSheet("color: #2980b9;")
            addr.clicked.connect(lambda _c=False, r=r: self.change_url(r))
            card.url_row.addWidget(addr)
            if r["service"]:
                addr.setEnabled(False)
            card.controls.addWidget(on)
            card.controls.addWidget(ref)
            card.controls.addStretch(1)
            card.clicked.connect(self._card_clicked)
            card.set_selected(self._current == r["alias"])
            card.set_health(self.health.get(f"zypper:{r['alias']}"))
            cards.append(card)
            self.cards[r["alias"]] = card
        self.grid.set_cards(cards, _("No repository matches."))

    def _queue_changed(self):
        """The queue changed: redraw once, after the click has finished (rebuilding the list inside itemChanged
        deletes the item Qt is still handling, and crashes), and only when the page is in sight."""
        if self._fill_pending:
            return
        self._fill_pending = True
        QTimer.singleShot(0, self._fill_if_visible)

    def _fill_if_visible(self):
        if not self.isVisible():
            self._fill_pending = False
            self._stale = True
            return
        self._fill()

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._stale:
            self._stale = False
            QTimer.singleShot(0, self._fill)

    def _fit_columns(self):
        """Give the name what is left after the short columns, so the address stays in sight."""
        room = self.tree.viewport().width()
        if room < 200:
            return
        others = sum(self.tree.columnWidth(c) for c in range(COL_URL) if c != COL_NAME)
        want = max(self.tree.sizeHintForColumn(COL_NAME), 120)
        self.tree.setColumnWidth(COL_NAME, max(180, min(want + 12, room - others - 220)))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._fit_columns)

    def _sort_keys(self, r, res):
        state_order = {"fail": 0, "skip": 1, "ok": 2}
        name = r["name"].lower()
        return {COL_ON: (not r["enabled"], name), COL_NAME: name,
                COL_HEALTH: (state_order.get(res["state"], 3) if res else 3, name),
                COL_PRIO: (int(r["priority"]), name), COL_URL: r["url"].lower()}

    def _show_health(self, alias):
        r = next((x for x in self.repos if x["alias"] == alias), None)
        if not r:
            return
        res = self.health.get(f"zypper:{alias}")
        it = self.items.get(alias)
        if it is not None:
            was = self._filling
            self._filling = True  # every change to an item emits itemChanged: do not take it as a click
            state = res["state"] if res else None
            it.setIcon(COL_HEALTH, dot(STATE_COLOR[state]))
            it.setText(COL_HEALTH, health_text(res))
            it.setToolTip(COL_HEALTH, res["detail"] if res else "")
            it.keys = self._sort_keys(r, res)
            brush = QBrush(FAIL_TINT) if state == "fail" else QBrush()
            for c in range(8):
                it.setBackground(c, brush)
            if state in ("ok", "fail"):
                it.setForeground(COL_HEALTH, QBrush(QColor(STATE_COLOR[state])))
            self._filling = was
        card = self.cards.get(alias)
        if card is not None:
            card.set_health(res)

    def _apply_filter(self, text):
        t = text.lower().strip()
        only_fail = self.only_fail.isChecked()

        def hidden(key, words):
            if t and t not in words.lower():
                return True
            return only_fail and self.health.get(key, {}).get("state") != "fail"

        for alias, it in self.items.items():
            r = it.data(0, Qt.ItemDataRole.UserRole)
            h = hidden(f"zypper:{alias}", r["name"] + " " + r["alias"] + " " + r["url"])
            it.setHidden(h)
            if alias in self.cards:
                self.cards[alias].setHidden(h)
        self.grid.relayout()
        for i in range(self.ftree.topLevelItemCount()):
            it = self.ftree.topLevelItem(i)
            rm = it.data(0, Qt.ItemDataRole.UserRole)
            it.setHidden(hidden(self._fkey(rm), rm["name"] + " " + rm.get("title", "") + " " + rm["url"]))
        for c in self.fgrid.cards:
            rm = c.property("remote")
            c.setHidden(hidden(self._fkey(rm), rm["name"] + " " + rm.get("title", "") + " " + rm["url"]))
        self.fgrid.relayout()

    def _view_changed(self, *_a):
        cards = self.v_cards.isChecked()
        if cards and self._cards_stale and self.repos:
            self._build_cards()
        if cards and self._fcards_stale and self.remotes:
            self._build_fcards()
        if cards:
            self._apply_filter(self.filter.text())
        self.z_stack.setCurrentIndex(1 if cards else 0)
        self.f_stack.setCurrentIndex(1 if cards else 0)
        self.hint.setText(_("Tick or untick to queue a change. Use − and + to change the priority. "
                            "Lower priority numbers win.") if cards else
                          _("Tick or untick to queue a change. Double-click the priority to change it. "
                            "Lower priority numbers win."))
        view = "cards" if cards else "list"
        if self.ctx.settings.get("repos_view", "list") != view:
            self.ctx.settings["repos_view"] = view
            self.ctx.settings.save()
        QTimer.singleShot(0, self.grid.relayout)
        QTimer.singleShot(0, self.fgrid.relayout)

    def _card_clicked(self, alias):
        self._current = alias
        for a, c in self.cards.items():
            c.set_selected(a == alias)
        if alias in self.items:
            self.tree.setCurrentItem(self.items[alias])

    def _selected(self):
        if self.tabs.currentIndex() != 0:  # the Flatpak tab: remotes are changed in the Flatpak section
            return None
        return next((r for r in self.repos if r["alias"] == self._current), None)

    def _tree_current(self, cur, _prev):
        if cur is not None and not self._filling:
            self._current = cur.data(0, Qt.ItemDataRole.UserRole)["alias"]
            for a, c in self.cards.items():
                c.set_selected(a == self._current)

    # ------------------------------------------------------------ Flatpak remotes
    def _fill_flatpak(self):
        self.ftree.clear()
        for rm in sorted(self.remotes, key=lambda x: (not x["enabled"], x["name"].lower())):
            inst = _("system") if rm["installation"] == "system" else _("user")
            it = QTreeWidgetItem([rm.get("title") or rm["name"], "", _("yes") if rm["enabled"] else _("no"), inst,
                                  rm["url"]])
            it.setData(0, Qt.ItemDataRole.UserRole, rm)
            it.setToolTip(FCOL_NAME, rm["name"])
            if not rm["enabled"]:
                it.setForeground(FCOL_NAME, QBrush(QColor("gray")))
            self.ftree.addTopLevelItem(it)
        if self.v_cards.isChecked():
            self._build_fcards()
        else:
            self.fgrid.set_cards([], "")
            self._fcards_stale = True
        for rm in self.remotes:
            self._show_fhealth(self._fkey(rm))
        for c in range(4):
            self.ftree.resizeColumnToContents(c)
        self._update_tab_titles()
        self._apply_filter(self.filter.text())  # new cards start hidden until the filter shows them

    def _build_fcards(self):
        self._fcards_stale = False
        cards = []
        for rm in sorted(self.remotes, key=lambda x: (not x["enabled"], x["name"].lower())):
            inst = _("system") if rm["installation"] == "system" else _("user")
            card = RepoCard(self._fkey(rm), self.fgrid.inner)
            card.setProperty("remote", rm)
            card.set_header(rm.get("title") or rm["name"], rm["url"],
                            "  ·  ".join([inst, _("enabled") if rm["enabled"] else _("disabled")]), rm["enabled"])
            card.setCursor(Qt.CursorShape.ArrowCursor)
            card.set_health(self.health.get(self._fkey(rm)))
            cards.append(card)
        self.fgrid.set_cards(cards, _("No Flatpak remote matches.") if self.remotes else
                             _("There is no Flatpak remote."))

    def _show_fhealth(self, key):
        res = self.health.get(key)
        state = res["state"] if res else None
        for i in range(self.ftree.topLevelItemCount()):
            it = self.ftree.topLevelItem(i)
            if self._fkey(it.data(0, Qt.ItemDataRole.UserRole)) == key:
                it.setIcon(FCOL_HEALTH, dot(STATE_COLOR[state]))
                it.setText(FCOL_HEALTH, health_text(res))
                it.setToolTip(FCOL_HEALTH, res["detail"] if res else "")
                brush = QBrush(FAIL_TINT) if state == "fail" else QBrush()
                for c in range(5):
                    it.setBackground(c, brush)
                if state in ("ok", "fail"):
                    it.setForeground(FCOL_HEALTH, QBrush(QColor(STATE_COLOR[state])))
        for c in self.fgrid.cards:
            if c.key == key:
                c.set_health(res)

    # ------------------------------------------------------------ changes
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
            QTimer.singleShot(0, self._fill)

    def _double_clicked(self, it, col):
        if col == COL_PRIO:
            self._ask_priority(it.data(0, Qt.ItemDataRole.UserRole))

    def _ask_priority(self, r):
        ch = self.ctx.queue.repo_change(r["alias"])
        cur = (ch.opts.get("priority") if ch and ch.kind == "repo-modify" else None) or r["priority"]
        val, ok = QInputDialog.getInt(self, _("Priority"), _("Priority of {name} (1–200, lower wins):")
                                      .format(name=r["name"]), int(cur), 1, 200)
        if ok:
            self._queue_change(r, "priority", val)

    def deal_with_failing(self):
        failing = [r for r in self.repos if self.health.get(f"zypper:{r['alias']}", {}).get("state") == "fail"]
        if not failing:
            return
        own = [r for r in failing if not r["service"]]
        lines = "\n".join(f"• {r['name']} — {self.health['zypper:' + r['alias']]['text']}"
                          + ("" if r["enabled"] else "  " + _("(already disabled)")) for r in failing)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(_("Repositories that do not work"))
        box.setText(ngettext("This repository does not answer:", "These repositories do not answer:", len(failing)))
        box.setInformativeText(lines + "\n\n" + _(
            "Disabling keeps them in the list, in case they come back; removing deletes them. Nothing changes "
            "until you apply the queue."))
        b_off = box.addButton(_("Disable them"), QMessageBox.ButtonRole.AcceptRole)
        b_rm = box.addButton(_("Remove them"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)  # Enter must never remove repositories
        box.setEscapeButton(cancel)
        if not any(r["enabled"] for r in failing):
            b_off.setEnabled(False)
        if not own:
            b_rm.setEnabled(False)
            b_rm.setToolTip(_("They belong to a service: the service would add them again."))
        plain(box).exec()
        if box.clickedButton() is b_off:
            for r in failing:
                if r["enabled"]:
                    self._queue_change(r, "enabled", False)
        elif box.clickedButton() is b_rm:
            for r in own:
                self.ctx.queue.add(Action("repo-remove", r["alias"]))

    def add_repo(self):
        dlg = AddRepoDialog(self)
        if dlg.exec():
            self.ctx.queue.add(dlg.action())

    def _online_banner(self):
        """Searching online for a repository is what people come here for most: say it big, on top."""
        box = QFrame(self)
        box.setObjectName("onlinebanner")
        box.setStyleSheet("#onlinebanner { background: rgba(39, 174, 96, 0.10); border: 1px solid rgba(39, 174, 96, 0.35);"
                          " border-radius: 12px; }")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 10, 14, 10)
        ic = QLabel(box)
        ic.setPixmap(icon("internet-services", "edit-find").pixmap(40, 40))
        h.addWidget(ic)
        v = QVBoxLayout()
        v.setSpacing(2)
        title = QLabel(_("Looking for a program your repositories do not have?"), box)
        f = title.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.1)
        title.setFont(f)
        v.addWidget(title)
        v.addWidget(muted(_("Search the openSUSE Build Service, Fedora COPR and Flathub. Zypp Desk says how far each "
                            "source can be trusted, and adds the repository for you."), box))
        h.addLayout(v, 1)
        b = button(_("Search online…"), "edit-find", self.search_online, primary=True)
        b.setMinimumHeight(38)
        h.addWidget(b)
        return box

    def search_online(self):
        """Look for a repository on OBS, Fedora COPR or Flathub (the dialog lives in findrepo.py)."""
        try:
            from .findrepo import FindRepoDialog
        except ImportError as e:
            error_box(self, _("Search online…"), _("This part is not available: {error}").format(error=e))
            return
        FindRepoDialog(self.ctx, term="", parent=self).exec()

    def remove_repo(self):
        r = self._selected()
        if r and ask(self, _("Remove"), _("Queue the removal of the repository {name}?").format(name=r["name"])):
            self.ctx.queue.add(Action("repo-remove", r["alias"]))

    def refresh_selected(self):
        r = self._selected()
        if r:
            self.ctx.run([Step("zypper", ["-n", "-v", "refresh", r["alias"]])],
                         _("Refresh {name}").format(name=r["name"]))

    def clean_cache(self):
        if ask(self, _("Clean the cache"), _("Delete the downloaded packages and the cached metadata of every "
                                             "repository? They will be downloaded again when needed.")):
            self.ctx.run([Step("zypper", ["clean", "--all"])], _("Clean the cache"))

    def import_key(self):
        from .keys import open_import_dialog
        open_import_dialog(self.ctx, self)
