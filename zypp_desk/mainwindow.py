# SPDX-License-Identifier: GPL-3.0-or-later
"""The main window: sidebar, page header, pages, terminal panel and the queue bar."""

import os
import shutil

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, VERSION, appicons, backend, question, repostate, websources
from .i18n import _, ngettext, pgettext
from .pages.apps import AppsPage
from .pages.flatpak import FlatpakPage
from .pages.keys import KeysPage
from .pages.packages import PackagesPage
from .pages.repos import ReposPage
from .pages.home import HomePage
from .pages.help import AboutPage, HelpPage
from .pages.history import HistoryPage
from .pages.drivers import DriversPage
from .pages.expert import ExpertPage
from .pages.locales import LocalesPage
from .pages.scripts import ScriptsPage
from .pages.locks import LocksPage
from .pages.system import MaintenancePage, ServicesPage, SettingsPage
from .pages.updates import UpdatesPage
from .pages.websources import WebSourcesPage
from .runner import Runner, helper_path, helper_version
from .settings import Settings
from .simulation import SimulationDialog, merge_results, parse_dry_run, summary_lines
from .tasks import run_async
from .terminal import TerminalPanel
from .style import ORANGE
from .txqueue import (PKG_KINDS, REVIEW_GROUP_FROM, Action, Needs, TxQueue, describe_actions, grouped,
                      installed_names, is_file_target, pkg_ident, quoted, repo_info, repo_name, step_actions)
from .ui import button, error_box, icon, info_box, mono, muted

SIDEBAR = [
    (None, ["home", "apps", "packages", "updates", "flatpak"]),
    (_("Sources"), ["repos", "keys", "services", "websources", "scripts"]),
    (_("System"), ["locks", "locales", "history", "maintenance", "drivers", "settings", "expert"]),
]
# Help and About are small buttons under the list (and F1 opens Help): with them in the list, it did not fit a
# window lower than ~850 px, and Help was hidden below the fold
SIDEBAR_FOOT = ["help", "about"]
# the heights of a row and of a group's header, from roomy to tight: the first that lets every row be seen
ROW_HEIGHTS = ((38, 32), (36, 30), (34, 28), (32, 26), (30, 24))
FOOT_STYLE = ("QToolButton { border: none; border-radius: 8px; padding: 4px 8px; }"
              "QToolButton:hover { background: rgba(39, 174, 96, 0.10); }"
              "QToolButton:checked { background: rgba(39, 174, 96, 0.22); }")


class QueueBar(QFrame):
    """The bar of the queue at the bottom; fit() is called when its width changes (its buttons get shorter labels in
    a narrow window)."""

    fit = None

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.fit:
            self.fit()


class Sidebar(QListWidget):
    """The list of places. Its rows get lower when the window does, so that every place shows without scrolling
    (1100×700, or a 1080p laptop at 125 %); only below that does it scroll."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._heights = None

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.fit_rows()

    def fit_rows(self):
        items = [self.item(i) for i in range(self.count())]
        headers = [it for it in items if it.data(Qt.ItemDataRole.UserRole) is None]
        rows = len(items) - len(headers)
        room = self.viewport().height()
        heights = next((rh for rh in ROW_HEIGHTS if rows * rh[0] + len(headers) * rh[1] <= room), ROW_HEIGHTS[-1])
        if heights == self._heights:
            return
        self._heights = heights
        for it in items:
            it.setSizeHint(QSize(0, heights[1] if it.data(Qt.ItemDataRole.UserRole) is None else heights[0]))


class ElidedLabel(QLabel):
    """One line of plain text that shortens itself with "…" to the width the layout gives it, also after the
    window changes size. It asks for its whole text (at most `cap` pixels wide) but can be narrower, so a long
    text never makes the window wider."""

    def __init__(self, parent=None, cap=None):
        super().__init__(parent)
        self._full = ""
        self._cap = cap
        self.setTextFormat(Qt.TextFormat.PlainText)  # names from repositories and files are never markup
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_full_text(self, text):
        self._full = text or ""
        self.updateGeometry()
        self._elide()

    def full_text(self):
        return self._full

    def sizeHint(self):
        m = self.contentsMargins()
        w = self.fontMetrics().horizontalAdvance(self._full) + m.left() + m.right() + 2 * self.margin() + 2
        if self._cap:
            w = min(w, self._cap)
        return QSize(w, super().sizeHint().height())

    def minimumSizeHint(self):
        m = self.contentsMargins()
        w = self.fontMetrics().horizontalAdvance("……") + m.left() + m.right() + 2 * self.margin()
        return QSize(min(w, self.sizeHint().width()), super().minimumSizeHint().height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def _elide(self):
        room = max(0, self.contentsRect().width() - 2 * self.margin())
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, room)
        if shown != self.text():
            super().setText(shown)


def word_icon(words, catalog=None):
    """The icon of a change in words: the program's own icon when the catalogue has it, else the theme's."""
    pkg = words.get("package")
    found = (catalog or {}).get(pkg) if pkg else None
    if found:
        ic = QIcon(found[1]) if found[0] == "file" else QIcon.fromTheme(found[1])
        if not ic.isNull():
            return ic
    return words["icon"]


def question_items(words, catalog=None):
    """txqueue's changes in words → question.item arguments (icon, title, detail, more)."""
    return [(word_icon(w, catalog), w["title"], w.get("detail") or "", w.get("more")) for w in words]


def _steps_list(nums):
    """'1', or '1, 2 and 3': the steps a change needs."""
    nums = [str(n) for n in nums]
    if len(nums) == 1:
        return _("⤷ Needs step {n}").format(n=nums[0])
    return _("⤷ Needs steps {list} and {last}").format(list=", ".join(nums[:-1]), last=nums[-1])


def _small_button(text):
    b = QPushButton(text)
    b.setAutoDefault(False)  # Enter never takes a change out
    f = b.font()
    f.setPointSizeF(f.pointSizeF() * 0.92)
    b.setFont(f)
    return b


class TakeOutDialog(QDialog):
    """Taking out a change that others in the queue need: says what would fail, and offers
    to take them all out, only this one, or to keep it. "Keep it in the queue" is the default (Enter, Escape)."""

    def __init__(self, parent, action, deps, actions, installed=None):
        super().__init__(parent)
        self.result_key = None
        self.setModal(True)
        k = action.kind
        title = {"key-import": _("Take the key out of the queue?"),
                 "repo-add": _("Take the repository out of the queue?")}.get(k, _("Take this change out of the queue?"))
        self.setWindowTitle(title)
        if k == "key-import":
            repos = [d.opts.get("name") or d.target for d in deps if d.kind == "repo-add"]
            if repos:
                lead = ngettext("Without this key, zypper will not trust the repository {repos}. These changes in "
                                "the queue need it:",
                                "Without this key, zypper will not trust the repositories {repos}. These changes in "
                                "the queue need it:", len(repos)).format(repos=", ".join(quoted(r) for r in repos))
            else:
                lead = _("Without this key, what it signed cannot be checked. These changes in the queue need it:")
        elif k == "repo-add":
            lead = _("Without this repository, these changes in the queue would fail:")
        else:
            lead = _("These changes in the queue need it and would fail without it:")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(14)
        pic = QLabel(self)
        pic.setPixmap(icon("dialog-warning").pixmap(40, 40))
        head.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(6)
        t = question.label(title, bold=True)
        tf = t.font()
        tf.setPointSizeF(tf.pointSizeF() * 1.25)
        t.setFont(tf)
        text.addWidget(t)
        text.addWidget(question.label(lead))
        for d in deps:
            text.addWidget(question.label("•  " + d.words(actions, installed)["title"]))
        head.addLayout(text, 1)
        outer.addLayout(head)
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)
        total = 1 + len(deps)
        b_all = QPushButton(_("Take both out") if total == 2 else
                            ngettext("Take all {n} out", "Take all {n} out", total).format(n=total))
        b_one = QPushButton({"key-import": _("Take only the key out"),
                             "repo-add": _("Take only the repository out")}.get(k, _("Take only this one out")))
        b_keep = QPushButton(_("Keep it in the queue"))
        b_keep.setProperty("primary", True)
        for b in (b_all, b_one):
            b.setAutoDefault(False)
        b_keep.setDefault(True)  # the answer that does no harm
        bh = QHBoxLayout()
        bh.addWidget(b_all)
        bh.addStretch(1)
        bh.addWidget(b_one)
        bh.addWidget(b_keep)
        outer.addLayout(bh)
        for b, key in ((b_all, "all"), (b_one, "one"), (b_keep, "keep")):
            b.clicked.connect(lambda _c=False, k_=key: self._press(k_))
        self.buttons = {"all": b_all, "one": b_one, "keep": b_keep}
        self.resize(560, 10)

    def showEvent(self, ev):
        super().showEvent(ev)
        # wrapped lines: as tall as the text needs at this width
        lay = self.layout()
        h = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        if h > self.height():
            self.resize(self.width(), h)

    def _press(self, key):
        self.result_key = key
        self.accept()

    def exec(self):
        super().exec()
        return self.result_key


class QueueDialog(QDialog):
    """What is in the queue, change by change: numbered in the order they run, each with
    its own button to take it out, nothing selected, and what each change needs from an earlier one. Many changes
    of one kind (installs, updates, removals…) are one row with "Show them" and one button, so that a queue of
    thousands of packages opens at once. Taking out a change that others need asks first
    (TakeOutDialog). Close is the default button: Enter never takes anything out of the queue. "Apply…" closes the
    window and asks the Apply question (apply_requested)."""

    def __init__(self, queue, parent=None, catalog=None):
        super().__init__(parent)
        self.queue = queue
        self.catalog = catalog or {}
        self.apply_requested = False
        # while a row's own question is open, a change of the queue (a download that ends) does not draw the rows
        # again: that would delete the button whose click is still being handled
        self._holding = 0
        self._pending = False
        self.setWindowTitle(_("Pending changes"))
        self.resize(760, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(10)
        title = question.label(_("What is in the queue"), bold=True)
        tf = title.font()
        tf.setPointSizeF(tf.pointSizeF() * 1.35)
        title.setFont(tf)
        outer.addWidget(title)
        outer.addWidget(question.label(_("Nothing has changed yet. Taking a change out of the queue only leaves it "
                                         "out: it undoes nothing."), muted=True))
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)
        bh = QHBoxLayout()
        b_help = QPushButton(icon("help-contents", "help-about"), _("Help"), self)
        b_help.setAutoDefault(False)
        b_help.setFlat(True)
        b_help.clicked.connect(lambda: question.show_help(self, "queue"))
        self.b_empty = QPushButton(_("Empty the queue"), self)
        self.b_empty.setAutoDefault(False)
        self.b_empty.clicked.connect(self.queue.clear)
        self.b_close = QPushButton(_("Close"), self)
        self.b_close.setDefault(True)
        self.b_close.clicked.connect(self.reject)
        self.b_apply = QPushButton(_("Apply the queue…"), self)
        self.b_apply.setProperty("primary", True)
        self.b_apply.setAutoDefault(False)
        self.b_apply.clicked.connect(self._apply)
        bh.addWidget(b_help)
        bh.addWidget(self.b_empty)
        bh.addStretch(1)
        bh.addWidget(self.b_close)
        bh.addWidget(self.b_apply)
        outer.addLayout(bh)
        # the rows are drawn again after the click that changed the queue has finished: a row's own button must
        # not be deleted while it is still handling its click
        self._refill = QTimer(self)
        self._refill.setSingleShot(True)
        self._refill.setInterval(0)
        self._refill.timeout.connect(self.fill)
        queue.changed.connect(self._refill.start)
        self._sized = False
        self.fill()

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._sized:
            return
        self._sized = True
        # as tall as the rows, up to most of the screen; then the rows scroll
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        body = self.scroll.widget()
        want = body.layout().totalHeightForWidth(self.scroll.viewport().width()) if body else 0
        chrome = self.height() - self.scroll.viewport().height()
        self.resize(min(self.width(), scr.width() - 40), max(320, min(int(scr.height() * 0.85), want + chrome + 4)))

    def done(self, r):
        try:
            self.queue.changed.disconnect(self._refill.start)
        except (RuntimeError, TypeError):
            pass
        super().done(r)

    def fill(self):
        if self._holding:
            self._pending = True  # drawn again when the question closes
            return
        acts = self.queue.ordered()
        installed = installed_names(TxQueue.update_candidates(acts))
        rows = grouped(acts, installed=installed, group_from=REVIEW_GROUP_FROM)
        links = Needs(acts)  # what needs what, worked out once for every row
        row_of = {}
        for i, (_words, members) in enumerate(rows, 1):
            for a in members:
                row_of[id(a)] = i
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(10)
        if not acts:
            v.addWidget(question.label(_("The queue is empty."), muted=True))
        for i, (words, members) in enumerate(rows, 1):
            nums = sorted({row_of[id(n)] for a in members for n in links.all_needs(a) if id(n) in row_of} - {i})
            v.addWidget(self._row(i, members, words, nums))
        steps = self.queue.steps()
        if steps:
            cmds = question.commands([s.display() for s in steps])
            cmds.setVisible(False)
            more = question.More(_("▸ Show the exact commands ({n})").format(n=len(steps)),
                                 _("▾ Hide the exact commands"), cmds)
            v.addWidget(more)
            v.addWidget(cmds)
        v.addStretch(1)
        self.scroll.setWidget(body)  # the old rows are deleted
        self.b_empty.setEnabled(bool(acts))
        self.b_apply.setEnabled(bool(acts))

    def _row(self, n, members, words, nums):
        f = QFrame()
        f.setObjectName("qrow")
        f.setStyleSheet("QFrame#qrow { background: palette(base); border: 1px solid rgba(127, 140, 141, 0.35); "
                        "border-radius: 10px; }")
        h = QHBoxLayout(f)
        h.setContentsMargins(12, 9, 12, 9)
        h.setSpacing(12)
        num = QLabel(str(n))
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setFixedSize(24, 24)
        num.setStyleSheet("background: rgba(127, 140, 141, 0.18); border-radius: 12px; font-weight: 600;")
        h.addWidget(num, 0, Qt.AlignmentFlag.AlignVCenter)
        pic = QLabel()
        ic = word_icon(words, self.catalog)
        pic.setPixmap((ic if isinstance(ic, QIcon) else icon(*ic.split("|"))).pixmap(32, 32))
        pic.setFixedSize(32, 32)
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tv.addWidget(question.label(words["title"], bold=True))
        if words.get("detail"):
            tv.addWidget(question.label(words["detail"], small=True, muted=True))
        more = words.get("more")
        if more and len(members) > 1:  # the names of a group, under "Show them"
            names = question.label("\n".join(more) if len(more) <= 400 else "\n".join(list(more[:400]) + ["…"]),
                                   small=True, selectable=True, font=mono())
            names.setVisible(False)
            tv.addWidget(question.More(_("▸ Show them ({n})").format(n=len(more)), _("▾ Hide them"), names))
            tv.addWidget(names)
        if nums:
            tv.addWidget(question.label(_steps_list(nums), small=True, color=ORANGE))
        h.addLayout(tv, 1)
        b = _small_button(_("✕ Take it out of the queue") if len(members) == 1 else
                          _("✕ Take them out of the queue"))
        b.clicked.connect(lambda _c=False, m=members: self.take_out(m))
        h.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        return f

    def take_out(self, members):
        """Take a change (or a row's group of changes) out of the queue; when others need it, ask first."""
        members = [members] if isinstance(members, Action) else list(members)
        queued = set(map(id, self.queue.actions))
        members = [a for a in members if id(a) in queued]
        if not members:
            return
        links = self.queue.links()
        mine = set(map(id, members))
        dep_ids = {id(d) for a in members for d in links.needed_by(a) if id(d) not in mine}
        if not dep_ids:
            self.queue.discard_actions(members)
            return
        acts = self.queue.ordered()
        deps = [a for a in acts if id(a) in dep_ids]  # in the order they run
        dlg = TakeOutDialog(self, members[0], deps, acts, installed_names(TxQueue.update_candidates(acts)))
        self._holding += 1
        try:
            choice = dlg.exec()
        finally:
            self._holding -= 1
            dlg.deleteLater()
        if choice == "all":
            self.queue.discard_actions([*members, *deps])
        elif choice == "one":
            self.queue.discard_actions(members)
        if self._pending and not self._holding:
            self._pending = False
            self._refill.start()  # after this click has been handled

    def _apply(self):
        self.apply_requested = True
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")  # the version in the title bar, so you always know which one runs
        self.resize(1360, 860)
        self.settings = Settings()
        self.queue = TxQueue(self.settings, self)
        self.queue.refused.connect(lambda text: info_box(self, APP_NAME, text))
        self.runner = Runner(self)
        self._on_done = None
        self._starting = False  # inside self.runner.run(): a run that ends at once is answered a moment later
        self._finish_pending = False
        self._terminal_used = False
        self.icon_catalog = {}

        # ------------------------------------------------ pages
        page_classes = [HomePage, AppsPage, PackagesPage, UpdatesPage, FlatpakPage, ReposPage, KeysPage, ServicesPage,
                        WebSourcesPage, ScriptsPage, LocksPage, LocalesPage, HistoryPage, MaintenancePage,
                        DriversPage, SettingsPage, ExpertPage, HelpPage, AboutPage]
        self.terminal = TerminalPanel(self)
        self.terminal.view.fold = self.settings["fold_repeated_lines"]
        self.pages = {}
        self.stack = QStackedWidget(self)
        self._wrap = {}
        for cls in page_classes:
            p = cls(self)
            self.pages[cls.key] = p
            # each page scrolls on its own when the window is smaller than it (a laptop screen, a big font):
            # the queue bar with Apply must always stay visible
            sa = QScrollArea(self)
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setWidget(p)
            self._wrap[cls.key] = sa
            self.stack.addWidget(sa)
        # when what is known about the programs from the web changes, the list of updates looks again
        self.pages["websources"].sources_changed.connect(self.pages["updates"]._web_changed)

        # ------------------------------------------------ sidebar
        self.sidebar = Sidebar(self)
        self.sidebar.setIconSize(QSize(22, 22))
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setObjectName("sidebar")
        self._rows = {}
        for group, keys in SIDEBAR:
            if group:
                hdr = QListWidgetItem(group)
                hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                hdr.setSizeHint(QSize(0, ROW_HEIGHTS[0][1]))  # fixed heights: the highlight never covers the next row
                f = hdr.font()
                f.setPointSizeF(f.pointSizeF() * 0.9)
                hdr.setFont(f)
                self.sidebar.addItem(hdr)
            for k in keys:
                p = self.pages[k]
                it = QListWidgetItem(icon(p.icon_name, "application-vnd.flatpak.ref", "package-x-generic"), p.title)
                it.setData(Qt.ItemDataRole.UserRole, k)
                it.setSizeHint(QSize(0, ROW_HEIGHTS[0][0]))
                it.setToolTip(p.title)  # a long name (German) is shortened with "…"
                self.sidebar.addItem(it)
                self._rows[k] = it
        self.sidebar.currentItemChanged.connect(lambda cur, _p: cur and self.go(cur.data(Qt.ItemDataRole.UserRole)))
        # Help and About: small buttons under the list, always in sight
        foot = QHBoxLayout()
        foot.setContentsMargins(2, 0, 2, 0)
        foot.setSpacing(2)
        self.foot = {}
        for k in SIDEBAR_FOOT:
            p = self.pages[k]
            b = QToolButton(self)
            b.setText(p.title)
            b.setIcon(icon(p.icon_name, "help-about", "help-contents"))
            b.setIconSize(QSize(18, 18))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setCheckable(True)
            b.setStyleSheet(FOOT_STYLE)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, key=k: self.go(key))
            foot.addWidget(b)
            self.foot[k] = b
        foot.addStretch(1)
        self.foot["help"].setToolTip(_("Help (F1)"))
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(127, 140, 141, 0.35);")
        self.status_lbl = muted("", self)
        self.term_toggle = QCheckBox(_("Show the terminal"), self)
        self.term_toggle.setChecked(self.settings["show_terminal"])
        self.term_toggle.toggled.connect(self._toggle_terminal)
        side = QWidget(self)
        side.setFixedWidth(240)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(6, 8, 6, 8)
        sv.setSpacing(4)
        sv.addWidget(self.sidebar, 1)
        sv.addWidget(line)
        sv.addLayout(foot)
        sv.addSpacing(2)
        sv.addWidget(self.status_lbl)
        sv.addWidget(self.term_toggle)

        # ------------------------------------------------ header, terminal, queue bar
        # the title gives way ("…") before the page's buttons make the window wider than the screen (German
        # Repositories widened a 1100 px window to 1113)
        self.page_title = ElidedLabel(self)
        f = self.page_title.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.35)
        self.page_title.setFont(f)
        self.page_icon = QLabel(self)
        self.actions_box = QHBoxLayout()
        header = QWidget(self)
        header.setObjectName("pageheader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        hh = QHBoxLayout(header)
        hh.setContentsMargins(14, 8, 10, 8)
        hh.addWidget(self.page_icon)
        hh.addWidget(self.page_title, 1)
        hh.addLayout(self.actions_box)

        self.split = QSplitter(Qt.Orientation.Vertical, self)
        self.split.addWidget(self.stack)
        self.split.addWidget(self.terminal)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.terminal.hide_requested.connect(lambda: self.term_toggle.setChecked(False))
        self.terminal.setVisible(self.settings["show_terminal"])

        self.q_title = QLabel(_("Queue"), self)
        f = self.q_title.font()
        f.setBold(True)
        self.q_title.setFont(f)
        # a long summary (German, many changes) is shortened with "…" instead of widening the window
        self.q_summary = ElidedLabel(self, cap=300)
        self.q_cmd = ElidedLabel(self)
        self.q_cmd.setFont(mono())
        self.q_cmd.setStyleSheet("background: #2a2e32; color: #e6e9ec; border-radius: 4px; padding: 4px 8px;")
        # the summary gives way first; the command shrinks to a short start (all of it in its tooltip) so that the
        # bar, whose buttons say "the queue", does not make the window wider
        self.q_cmd.setMinimumWidth(110)
        self.q_cmd.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        # every button of the bar says it acts on the queue (the pages have their own Simulate and Clear buttons)
        self.b_review = button(_("Review the queue…"), "view-list-details", self.review_queue)
        self.b_review.setToolTip(_("See every change in the queue, in the order it runs, and take some out."))
        self.b_clear = button(_("Empty the queue"), "edit-clear", self.queue.clear)
        self.b_clear.setToolTip(_("Take every change out of the queue. Nothing on the computer changes."))
        self.b_sim = button(_("Simulate the queue"), "media-playback-start", lambda: self.simulate_queue())
        self.b_sim.setToolTip(_("zypper --dry-run of the queue: what would be installed, updated or removed, without "
                                "changing anything (with the administrator password)."))
        self.b_apply = button(_("Apply the queue…"), "dialog-ok-apply", lambda: self.apply_queue(), primary=True)
        self.b_apply.setToolTip(_("Make the changes in the queue: Zypp Desk asks first, then runs the commands shown "
                                  "on the left."))
        # in a narrow window (or a language with long words) the labels get short: the bar is titled "Queue" on the
        # left, and each tooltip says the rest. Each button's minimum is its short label, so the window can shrink.
        self._qbar_labels = [(self.b_review, _("Review the queue…"), pgettext("queue bar", "Review…")),
                             (self.b_clear, _("Empty the queue"), pgettext("queue bar", "Empty")),
                             (self.b_sim, _("Simulate the queue"), pgettext("queue bar", "Simulate")),
                             (self.b_apply, _("Apply the queue…"), pgettext("queue bar", "Apply…"))]
        self._qbar_full = {}
        for b, full, short in self._qbar_labels:
            b.setText(short)
            b.setMinimumWidth(b.sizeHint().width())
            b.setText(full)
            self._qbar_full[b] = b.sizeHint().width()
        qbar = QueueBar(self)
        qbar.setObjectName("queuebar")
        self.qbar = qbar
        qbar.fit = self._fit_qbar
        qh = QHBoxLayout(qbar)
        qh.setContentsMargins(10, 6, 10, 6)
        qh.addWidget(self.q_title)
        qh.addWidget(self.q_summary)
        qh.addWidget(self.q_cmd, 1)
        for b in (self.b_review, self.b_clear, self.b_sim, self.b_apply):
            qh.addWidget(b)

        right = QWidget(self)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(header)
        rv.addWidget(self.split, 1)
        rv.addWidget(qbar)

        central = QWidget(self)
        ch = QHBoxLayout(central)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)
        ch.addWidget(side)
        ch.addWidget(right, 1)
        self.setCentralWidget(central)

        # ------------------------------------------------ wiring
        self.runner.output.connect(self.terminal.view.feed)
        self.runner.note.connect(self._note)
        self.runner.finished.connect(self._finished)
        self.queue.changed.connect(self._queue_changed)
        self._queue_changed()
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.go("packages"))
        QShortcut(QKeySequence(QKeySequence.StandardKey.HelpContents), self, activated=lambda: self.go("help"))
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(30000)
        self.update_status()
        run_async(appicons.load_catalog, on_done=self._catalog_loaded)
        run_async(websources.clean_downloads, on_done=lambda _r, _e: None)
        self.sidebar.setCurrentItem(self._rows["home"])
        self.terminal.view.note(_("zypper's own output appears here while it works."), "info")
        if not helper_path():
            QTimer.singleShot(400, self._warn_no_helper)
        elif helper_version() != VERSION:
            QTimer.singleShot(400, self._warn_old_helper)

    # ------------------------------------------------ navigation
    def go(self, key, run_routine=False):
        page = self.pages[key]
        row = self._rows.get(key)
        if row is not None:
            if self.sidebar.currentItem() is not row:
                self.sidebar.setCurrentItem(row)
        elif self.sidebar.currentItem() is not None:  # Help or About: a button under the list, not a row
            self.sidebar.setCurrentRow(-1)
            self.sidebar.clearSelection()
        for k, b in self.foot.items():
            b.setChecked(k == key)
        self.stack.setCurrentWidget(self._wrap[key])
        self.page_title.set_full_text(page.title)
        self.page_title.setToolTip(page.title)
        self.page_icon.setPixmap(icon(page.icon_name, "application-vnd.flatpak.ref", "package-x-generic").pixmap(28, 28))
        while self.actions_box.count():
            w = self.actions_box.takeAt(0).widget()
            if w:
                w.setParent(None)
        for w in page.header_actions():
            self.actions_box.addWidget(w)
        # an empty terminal only takes room: it appears the first time something runs, and then stays
        self.terminal.setVisible(self.term_toggle.isChecked() and (self.runner.busy or self._terminal_used))
        page.activated()
        if run_routine and key == "updates":
            # the tray and the command line: the same as the green button, which asks first when some rows of
            # Updates are unticked (the routine updates everything)
            (getattr(page, "update_everything", None) or page.run_routine)()

    def handle_request(self, req):
        """What the command line asked for, from this start or from a later one (app.py forwards it)."""
        token = req.get("token")
        if isinstance(token, str) and token:
            os.environ["XDG_ACTIVATION_TOKEN"] = token  # Qt uses it once, to bring this window to the front
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        page = req.get("page")
        if page in self.pages:
            self.go(page)
        if req.get("search"):
            self.go("packages")
            self.pages["packages"].term.setText(str(req["search"]))
            self.pages["packages"].search()
        if req.get("run_routine"):
            self.go("updates", run_routine=not self.runner.busy)
        files = [f for f in req.get("files") or [] if isinstance(f, str)]
        if files:
            QTimer.singleShot(0, lambda: self.open_files(files))

    def open_files(self, paths):
        """Downloaded .rpm files and software lists of "Move to another computer" (.zdesk), opened from Dolphin or
        given on the command line."""
        lists = [p for p in paths if p.lower().endswith(".zdesk")]
        paths = [p for p in paths if p not in lists]
        for path in lists[:1]:  # one list at a time: each one fills the queue
            from .pages.migrate import open_import
            open_import(self, self, path)
        if not paths:
            return
        try:
            from .pages.localrpm import LocalRpmDialog
        except ImportError as e:
            error_box(self, APP_NAME, _("This part is not available: {error}").format(error=e))
            return
        dlg = LocalRpmDialog(self, paths, self)
        dlg.exec()
        dlg.deleteLater()

    def _catalog_loaded(self, catalog, err):
        if catalog:
            self.icon_catalog = catalog
            self.pages["packages"].refresh_icons()

    def set_badge(self, key, text):
        """A number after a section's name in the sidebar (for example, new versions found)."""
        page = self.pages[key]
        row = self._rows.get(key)
        if row is not None:
            row.setText(f"{page.title}  ({text})" if text else page.title)

    def show_terminal(self):
        """Bring the terminal in (a download or a run writes there)."""
        self._terminal_used = True
        self.term_toggle.setChecked(True)
        self.terminal.setVisible(True)

    def _toggle_terminal(self, on):
        if on:
            self._terminal_used = True  # asked for: show it, even empty
        self.terminal.setVisible(on)
        self.settings["show_terminal"] = on
        self.settings.save()

    # ------------------------------------------------ running
    def run(self, steps, title, on_done=None):
        """Run the steps now, with their output in the terminal. on_done(ok) is called when they end, always
        after run() has returned (also when they end at once, for example without the helper)."""
        if self.runner.busy or self._finish_pending:
            QMessageBox.information(self, APP_NAME, _("Wait until the current command finishes."))
            return False
        self._terminal_used = True
        if not any(s.capture for s in steps):
            self.term_toggle.setChecked(True)
        self.terminal.setVisible(self.term_toggle.isChecked())
        self.terminal.set_title(title)
        self.terminal.notes.hide()
        self._run_start = self.terminal.view.document().blockCount()
        self._on_done = on_done
        self._set_busy(True)
        self._starting = True
        try:
            self.runner.run(steps)
        finally:
            self._starting = False
        return True

    def _explain_run(self):
        from .explain import explain
        text = "\n".join(s.output for s in self.runner.steps if not s.capture)
        run_async(explain, text, on_done=lambda items, err: self._explained(items or []))

    def _explained(self, items):
        self.terminal.show_explanations(items)
        if items:  # give the explanations room to be read
            sizes = self.split.sizes()
            total = sum(sizes)
            if total and sizes[1] < total * 0.4:
                self.split.setSizes([int(total * 0.55), int(total * 0.45)])

    def _note(self, text, kind):
        if kind == "prompt":
            self.terminal.view.prompt(text)
        else:
            self.terminal.view.note(text, kind)

    def _finished(self, ok):
        if self._starting:
            # it ended inside run(): the caller has not seen run() return yet, so it hears about it later
            self._finish_pending = True
            QTimer.singleShot(0, lambda: self._finished_later(ok))
            return
        self._set_busy(False)
        if ok:
            self.terminal.view.note(_("Finished."), "ok")
        elif self.runner.stopped:
            self.terminal.view.note(_("Stopped as you asked: the steps after it did not run."), "info")
        else:
            self.terminal.view.note(_("Finished with errors."), "error")
        self._explain_run()
        cb, self._on_done = self._on_done, None
        if cb:
            cb(ok)
        self.update_status()

    def _finished_later(self, ok):
        self._finish_pending = False
        self._finished(ok)

    def _set_busy(self, busy):
        for b in (self.b_sim, self.b_apply, self.b_clear):
            b.setEnabled(not busy and len(self.queue) > 0)
        self._paint_apply()
        self.status_lbl.setText(_("Working…") if busy else self.status_lbl.text())
        if not busy:
            self._queue_changed()  # the queue may have changed while it ran

    # ------------------------------------------------ queue
    def _queue_changed(self):
        n = len(self.queue)
        parts = self.queue.summary()
        self.q_summary.set_full_text(" · ".join(parts) if parts else _("empty"))
        self.q_summary.setToolTip(" · ".join(parts))
        preview = self.queue.preview() or _("Nothing to do yet: choose packages, locks or repository changes.")
        self.q_cmd.set_full_text(preview)  # shortened with "…" to the width it gets, also after a resize
        self.q_cmd.setToolTip(preview)  # a tool tip that looks like markup would be shown as markup: never is
        busy = self.runner.busy
        for b in (self.b_review, self.b_clear, self.b_sim, self.b_apply):
            b.setEnabled(n > 0 and not busy)
        self._paint_apply()

    def _fit_qbar(self):
        """Full labels ("Review the queue…") when they fit in the bar, short ones ("Review…") when they do not."""
        lay = self.qbar.layout()
        if lay is None:
            return
        m = lay.contentsMargins()
        need = (m.left() + m.right() + lay.spacing() * max(0, lay.count() - 1) + self.q_title.sizeHint().width()
                + self.q_summary.minimumSizeHint().width() + self.q_cmd.minimumWidth() + sum(self._qbar_full.values()))
        full = self.qbar.width() >= need
        for b, long_text, short in self._qbar_labels:
            want = long_text if full else short
            if b.text() != want:
                b.setText(want)

    def _paint_apply(self):
        """The "Apply the queue…" button is green only when it can be pressed: an empty queue has nothing to do."""
        on = self.b_apply.isEnabled()
        if bool(self.b_apply.property("primary")) != on:
            self.b_apply.setProperty("primary", on)
            self.b_apply.style().unpolish(self.b_apply)
            self.b_apply.style().polish(self.b_apply)

    def review_queue(self):
        dlg = QueueDialog(self.queue, self, self.icon_catalog)
        dlg.exec()
        want = dlg.apply_requested
        dlg.deleteLater()
        if want and len(self.queue):
            self.apply_queue()

    def _drop_stale_removals(self):
        """Removals of packages that are no longer installed leave the queue, and the terminal says so: zypper
        would stop on them (code 104, "No provider found"). It happens when a page still showed a package an
        earlier Apply removed."""
        from .txqueue import SOLVABLE_KINDS
        by_name = {}
        for a in self.queue.actions:
            if a.kind != "remove" or a.opts.get("type", "package") != "package":
                continue
            target = a.opts.get("package") or a.target
            head, _sep, rest = target.partition(":")
            if rest and head in SOLVABLE_KINDS and head != "package":
                continue  # a pattern, a product…: not an rpm package
            name = (rest or head).split("=", 1)[0].strip()
            if name and not any(c in name for c in "*?[<>"):
                by_name.setdefault(name, []).append(a)
        if not by_name:
            return
        have = backend.installed_among(list(by_name))
        gone = [n for n in by_name if n not in have]
        if gone:
            self.queue.discard_actions([a for n in gone for a in by_name[n]])
            self.terminal.view.note(ngettext("{names} is no longer installed: it left the queue.",
                                             "{names} are no longer installed: they left the queue.",
                                             len(gone)).format(names=", ".join(gone)), "info")

    def simulate_queue(self):
        self._drop_stale_removals()
        # what is simulated and shown is what will be applied: a snapshot of the queue at this moment
        snapshot = list(self.queue.actions)
        preview = self.queue.preview()
        sims = self.queue.simulation_steps()
        others = [s for s in self.queue.steps() if not s.args[1:2] or s.args[1] not in ("install", "remove")]
        later = self.queue.not_simulable()
        if later:
            self.terminal.view.note(_("Not simulated, because their repository is only added when the queue is "
                                      "applied: {list}").format(list=", ".join(a.target for a in later)), "info")
        if not sims:
            info_box(self, _("Simulate"), _("Changes to repositories, locks, languages and Flatpak apps cannot be "
                                            "simulated. These commands will run:") + "\n\n" + preview)
            return
        self.run(sims, _("Simulation of the queue"), on_done=lambda ok: self._simulated(sims, others, snapshot, preview))

    def _simulated(self, sims, others, snapshot, preview):
        if any(st.code in (126, 127) for st in sims):
            return  # the password was not given: nothing was simulated, nothing to show
        res = merge_results([parse_dry_run(st.output, source="zypper " + (st.args[2] if len(st.args) > 2 else ""))
                             for st in sims])
        for line in summary_lines(res):
            self.terminal.view.note(line, "error" if res["problems"] and line.startswith(_("Problem: {text}").split("{")[0]) else "ok")
        dlg = SimulationDialog(res, preview, extra_steps=others, parent=self,
                               problem_note=_("A lock or an unlock you choose runs at once (a simulation only knows "
                                              "the locks that exist); the rest goes to the queue."))
        rc = dlg.exec()
        chosen = getattr(dlg, "chosen", None) or []
        dlg.deleteLater()
        if rc == QDialog.DialogCode.Accepted:
            self.apply_queue(confirm=False, expected=snapshot)
        elif rc == getattr(SimulationDialog, "RESOLVE", 2):
            # the problem window already showed what runs now and asked: no second question
            self.apply_solutions(chosen, then=self.simulate_queue, confirmed=True)

    def apply_solutions(self, solutions, then=None, now=False, confirmed=False):
        """What you chose for each problem of a simulation, then `then` (simulate again). confirmed: the window
        the choice came from already showed what runs now and asked for it (the problem window), so the question
        before the commands that run at once is not asked again.

        Queue simulation (now=False): installs and removals join the queue, a "discard" takes an install or a
        removal out of it, and locks and unlocks run at once, because a dry run only knows the locks that exist.
        Dup simulation (now=True): there is no queue to edit, so everything runs at once: installs and removals
        as one 'zypper install -- new '!old'' (zypper refuses, and changes nothing, if something installed still
        needs a package to remove), and "do not install X" or "keep X" become a lock of X.
        pages/problems.solution_plan makes the commands, the same ones the problem window shows."""
        from .pages.problems import solution_plan
        steps, add, discard = solution_plan(solutions, now)
        with self.queue.batch():
            for a in add:
                self.queue.add(a)
            for d in discard:
                # 'pattern:kde' only matches the pattern; 'packman:vlc' (queued from Search online) matches vlc
                want = pkg_ident(Action("install", d.get("target") or ""))
                kinds = (d["of"],) if d.get("of") else ("install", "remove")
                self.queue.discard_actions([x for x in self.queue.of(*kinds) if pkg_ident(x) == want])
        if not steps:
            if then:
                then()
            return
        if not confirmed and not self._ask_solutions(steps, then, now, queued=bool(add or discard)):
            return
        self.run(steps, _("Choose a solution"), on_done=lambda ok: self._solutions_done(ok, then))

    def _ask_solutions(self, steps, then, now, queued=False):
        """The question before the solutions that run at once (locks, unlocks and, for an upgrade, installs and
        removals): what each one does in words, what it means, the commands underneath."""
        acts = [a for st in steps for a in step_actions(st)]
        items = question_items(describe_actions(acts, in_transaction=True), self.icon_catalog)
        means = [_("an upgrade has no queue, so these changes are made now.") if now else
                 _("locks and unlocks are made now, not in the queue, because a simulation only knows the locks "
                   "that exist.")]
        if queued:
            means.append(_("The other choices went to the queue."))
        # a removal written as '!name' says under its own title that it goes only if nothing still needs it
        locked = sum(len(st.args) - st.args.index("--") - 1 for st in steps if "addlock" in st.args and "--" in st.args)
        if locked:
            means.append(ngettext("You can unlock it later in Locks.", "You can unlock them later in Locks.", locked))
        if then:
            title, lead = _("Do this now, then simulate again?"), _("Zypp Desk will do this now, then simulate again:")
        else:
            title, lead = _("Do this now?"), _("Zypp Desk will do this now:")
        return question.confirm(self, title, _("Yes, do it now"), window_title=_("Choose a solution"), lead=lead,
                                items=items, means=" ".join(means), commands_=[s.display() for s in steps],
                                help_topic="problems")

    def _solutions_done(self, ok, then):
        self.after_change()  # locks, installs or removals may have changed: the pages look again
        if ok and then:
            then()

    def apply_queue(self, confirm=True, expected=None, why=None):
        """Apply the queue, after the Apply question (confirm=False: the simulation window already asked, for the
        queue it simulated: expected). why: why the question is asked again ("simulation", "question")."""
        self._drop_stale_removals()
        actions = list(self.queue.actions)
        steps = self.queue.steps()
        if not steps:
            return
        if expected is not None and actions != expected:
            # something joined or left the queue after the simulation (a download finished): show it and ask again
            confirm, why = True, why or "simulation"
        if confirm and not self._ask_apply(steps, changed=why):
            return
        if list(self.queue.actions) != actions:
            # it changed while the question was open: ask again, and say why
            return self.apply_queue(confirm=True, why="question")
        if not self._files_unchanged(actions):
            return
        self.run(steps, _("Applying the queue"), on_done=lambda ok: self._applied(ok, actions, steps))

    def _ask_apply(self, steps, changed=None):
        """The Apply question: each change in words, in the order it runs, what it means
        for you, a warning when it can hurt, and the exact commands underneath, small. changed: why it is asked
        again ("simulation": the queue changed after the simulation; "question": while the question was open)."""
        acts = self.queue.actions
        n = len(acts)
        items = question_items(self.queue.words(), self.icon_catalog)
        extra = []
        if changed == "simulation":
            extra.append(question.box("info", _("The queue changed after the simulation: this is what it holds "
                                                 "now.")))
        elif changed:
            extra.append(question.box("info", _("The queue changed while the question was open: this is what it "
                                                 "holds now.")))
        # what really happens (runner.py): every step after the first runs only if the one before it succeeded,
        # and Apply takes out of the queue only the changes whose commands all ended well (_applied)
        means = [_("it is what you put in the queue.")]
        if len(steps) > 1:
            means.append(_("If a step fails, Zypp Desk stops there: what was done stays done, and the rest stays in "
                           "the queue."))
        else:
            means.append(_("If it fails, it stays in the queue."))
        if any(a.kind in PKG_KINDS for a in acts):
            means.append(_("“Simulate” shows first what else zypper would install or remove with these changes."))
        warnings = []
        for a in acts:
            if a.kind == "repo-remove" or (a.kind == "repo-modify" and a.opts.get("enabled") is False):
                if repostate.is_tumbleweed({"alias": a.target, "url": repo_info(a.target).get("url", "")}):
                    warnings.append(_("Without the repository “{name}”, Tumbleweed is not updated.").format(
                        name=repo_name(a.target)))
        # --allow-unsigned-rpm: say which files go in without a signature (a bridge says it under its own title)
        loose = [a for a in self.queue.unsigned_files() if not a.opts.get("bridge_for")]
        if loose:
            warnings.append(ngettext("This file is not signed, so nothing can check who made it: {files}.",
                                     "These files are not signed, so nothing can check who made them: {files}.",
                                     len(loose)).format(files=", ".join(os.path.basename(a.target) for a in loose)))
        if any(a.kind == "firmware" for a in acts):
            warnings.append(_("Keep the computer plugged in and do not turn it off while the firmware is updated."))
        password = any(s.root or (s.program in ("flatpak", "fwupdmgr") and "--user" not in s.args) for s in steps)
        return question.confirm(
            self, ngettext("Make this change to the computer?", "Make these {n} changes to the computer?", n).format(n=n),
            ngettext("Yes, make the change", "Yes, make the {n} changes", n).format(n=n),
            window_title=_("Apply"),
            lead=_("Zypp Desk will do this, in this order:") if len(items) > 1 else _("Zypp Desk will do this:"),
            items=items, extra=extra, means=" ".join(means), warning="\n⚠ ".join(warnings) or None,
            commands_=[s.display() for s in steps], password=password, help_topic="queue")

    def _files_unchanged(self, actions):
        """Downloaded files must still be exactly the ones that were checked and accepted."""
        for a in actions:
            want = a.opts.get("sha256")
            if a.kind != "install" or not want:
                continue
            try:
                got = websources.sha256_of(a.target)
            except OSError as e:
                error_box(self, _("Apply"), _("{file} cannot be read any more: {error}").format(file=a.target, error=e))
                return False
            if got != want:
                error_box(self, _("Apply"), _("{file} changed after it was checked, so it is not installed. Take it "
                                              "out of the queue and download it again.").format(file=a.target))
                return False
        return True

    def _applied(self, ok, applied, steps=()):
        """After Apply: the changes that were made leave the queue. When a step failed, the steps after it did not
        run (runner.py): only the changes whose commands all ended well leave, and the rest stays for another
        try (a repository added again would fail: its alias exists)."""
        if ok:
            done = list(applied)
        else:
            done = [a for a in applied if self._made(a, steps)]
        if done:
            self.queue.discard_actions(done)  # changes added while it ran stay in the queue
            # files from the web wait in the private download folder until they are installed; now they join the
            # folder where you keep them (often a local zypper repository, synced with a cloud)
            web = []
            for a in done:
                if a.kind == "install" and a.opts.get("copy_to"):  # a file you opened: keep a copy with the others
                    try:
                        dest = os.path.join(a.opts["copy_to"], os.path.basename(a.target))
                        if os.path.abspath(dest) != os.path.abspath(a.target) and not os.path.exists(dest):
                            shutil.copy2(a.target, dest)
                    except OSError as e:
                        self.terminal.view.note(_("{file} could not be copied to {folder}: {error}").format(
                            file=a.target, folder=a.opts["copy_to"], error=e), "error")
                if a.kind == "install" and a.opts.get("move_to"):
                    web.append(a.opts.get("package"))
                    try:
                        websources.place(a.target, {"folder": a.opts["move_to"]})
                    except OSError as e:
                        self.terminal.view.note(_("{file} could not be moved to {folder}: {error}").format(
                            file=a.target, folder=a.opts["move_to"], error=e), "error")
                elif a.kind == "install" and is_file_target(a.target, a.opts):
                    # the private copy of a file you opened (what root read): installed, and kept above if asked
                    try:
                        from . import localrpm
                        if localrpm.is_private_copy(a.target):
                            localrpm.drop_private_copy(a.target)
                    except (ImportError, AttributeError, OSError):
                        pass
            if web:  # new versions of programs from the web: offer to throw the old ones away
                QTimer.singleShot(800, lambda pk=set(filter(None, web)): self.pages["websources"].tidy(packages=pk))
        self.after_change()

    @staticmethod
    def _made(action, steps):
        """True when every command of this change ran and ended well."""
        mine = [s for s in steps if any(x is action for x in getattr(s, "actions", ()))]
        return bool(mine) and all(s.state == "done" for s in mine)

    def after_change(self):
        """Call after anything that may have changed the system (Apply, the routine, zypper dup, maintenance,
        solutions run at once): the tray counts again, the page in front looks again now and the others when
        you open them (not all at once)."""
        from . import tray
        tray.poke("check")  # nothing happens if there is no tray icon
        current = self.stack.currentWidget()
        for key, p in self.pages.items():
            if not p.loaded or key == "history":
                continue
            if self._wrap[key] is current:
                p.reload()  # Packages also looks again for what it showed (a search or a list)
            else:
                p.loaded = False

    # ------------------------------------------------ status
    def update_status(self):
        run_async(lambda: (backend.zypp_lock(), backend.run_c(["needs-rebooting"]).returncode),
                  on_done=self._status_loaded)

    def _status_loaded(self, res, err):
        if err or not res or self.runner.busy:
            return
        lock, nr = res
        lines = [_("Package manager in use by {proc}").format(proc=lock) if lock else _("Package manager free"),
                 _("Restart needed") if nr == 102 else _("No restart needed")]
        self.status_lbl.setText("\n".join(lines))

    def _warn_old_helper(self):
        """The helper is from another version than this window (updated from the source, install.sh not run
        again): new commands would fail later with "unknown command", after the password."""
        path = helper_path() or ""
        found = helper_version(path) or _("older than 0.4.1")
        if path.startswith("/usr/local/"):
            todo = _("Run install.sh from the source folder again.")
        else:
            todo = _("Update the zypp-desk package.")
        QMessageBox.warning(self, APP_NAME, _("The helper that runs zypper as root ({path}) is from another "
                                              "version of Zypp Desk: helper {found}, window {version}. Some "
                                              "changes will fail until both match.").format(
                                                  path=path, found=found, version=VERSION) + "\n\n" + todo)

    def _warn_no_helper(self):
        QMessageBox.information(self, APP_NAME, _("The helper that runs zypper as root is not installed, so the "
                                                  "window can search and show information but cannot change "
                                                  "anything yet. Run install.sh from the source folder."))

    def closeEvent(self, ev):
        if self.runner.busy:
            # Stopping zypper half-way through installing could leave the system half updated.
            QMessageBox.information(self, APP_NAME, _("zypper is still working. Wait until it finishes before "
                                                      "closing the window."))
            ev.ignore()
            return
        web = self.pages.get("websources")
        if web is not None and web.downloading and QMessageBox.question(
                self, APP_NAME, _("A program from the web is still downloading. Close anyway? The download stops "
                                  "and nothing is added to the queue.")) != QMessageBox.StandardButton.Yes:
            ev.ignore()
            return
        super().closeEvent(ev)
