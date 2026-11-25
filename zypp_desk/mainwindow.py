# SPDX-License-Identifier: GPL-3.0-or-later
"""The main window: sidebar, page header, pages, terminal panel and the queue bar."""

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QSplitter, QStackedWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_NAME, backend
from .i18n import _
from .pages.packages import PackagesPage
from .pages.repos import ReposPage
from .pages.system import (HistoryPage, HomePage, LocalesPage, LocksPage, MaintenancePage, ServicesPage,
                           SettingsPage)
from .pages.updates import UpdatesPage
from .runner import Runner, helper_path
from .settings import Settings
from .simulation import SimulationDialog, parse_dry_run, summary_lines
from .tasks import run_async
from .terminal import TerminalPanel
from .txqueue import TxQueue
from .ui import button, heading, icon, mono, muted

SIDEBAR = [
    (None, ["home", "packages", "updates"]),
    (_("Sources"), ["repos", "services"]),
    (_("System"), ["locks", "locales", "history", "maintenance", "settings"]),
]


class QueueDialog(QDialog):
    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self.queue = queue
        self.setWindowTitle(_("Pending changes"))
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("Change")])
        self.tree.setRootIsDecorated(False)
        lay.addWidget(self.tree, 1)
        self.cmd = QLabel(self)
        self.cmd.setFont(mono())
        self.cmd.setWordWrap(True)
        self.cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.cmd)
        bb = QDialogButtonBox(self)
        bb.addButton(_("Drop the selected change"), QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(self.drop)
        bb.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.fill()

    def fill(self):
        self.tree.clear()
        for a in self.queue.actions:
            it = QTreeWidgetItem(self.tree, [a.describe()])
            it.setData(0, Qt.ItemDataRole.UserRole, a)
        self.cmd.setText(self.queue.preview())

    def drop(self):
        it = self.tree.currentItem()
        if it:
            self.queue.discard_action(it.data(0, Qt.ItemDataRole.UserRole))
            self.fill()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 860)
        self.settings = Settings()
        self.queue = TxQueue(self.settings, self)
        self.runner = Runner(self)
        self._on_done = None

        # ------------------------------------------------ pages
        page_classes = [HomePage, PackagesPage, UpdatesPage, ReposPage, ServicesPage, LocksPage, LocalesPage,
                        HistoryPage, MaintenancePage, SettingsPage]
        self.terminal = TerminalPanel(self)
        self.terminal.view.fold = self.settings["fold_repeated_lines"]
        self.pages = {}
        self.stack = QStackedWidget(self)
        for cls in page_classes:
            p = cls(self)
            self.pages[cls.key] = p
            self.stack.addWidget(p)

        # ------------------------------------------------ sidebar
        self.sidebar = QListWidget(self)
        self.sidebar.setIconSize(QSize(22, 22))
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setStyleSheet("QListWidget { background: transparent; } QListWidget::item { padding: 5px; }")
        self._rows = {}
        for group, keys in SIDEBAR:
            if group:
                hdr = QListWidgetItem(group)
                hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                f = hdr.font()
                f.setPointSizeF(f.pointSizeF() * 0.9)
                hdr.setFont(f)
                self.sidebar.addItem(hdr)
            for k in keys:
                p = self.pages[k]
                it = QListWidgetItem(icon(p.icon_name, "package-x-generic"), p.title)
                it.setData(Qt.ItemDataRole.UserRole, k)
                self.sidebar.addItem(it)
                self._rows[k] = it
        self.sidebar.currentItemChanged.connect(lambda cur, _p: cur and self.go(cur.data(Qt.ItemDataRole.UserRole)))
        self.status_lbl = muted("", self)
        self.term_toggle = QCheckBox(_("Show the terminal"), self)
        self.term_toggle.setChecked(self.settings["show_terminal"])
        self.term_toggle.toggled.connect(self._toggle_terminal)
        side = QWidget(self)
        side.setFixedWidth(240)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(6, 8, 6, 8)
        sv.addWidget(self.sidebar, 1)
        sv.addWidget(self.status_lbl)
        sv.addWidget(self.term_toggle)

        # ------------------------------------------------ header, terminal, queue bar
        self.page_title = heading("", 1.3)
        self.actions_box = QHBoxLayout()
        header = QWidget(self)
        hh = QHBoxLayout(header)
        hh.setContentsMargins(12, 6, 8, 6)
        hh.addWidget(self.page_title, 1)
        hh.addLayout(self.actions_box)

        self.split = QSplitter(Qt.Orientation.Vertical, self)
        self.split.addWidget(self.stack)
        self.split.addWidget(self.terminal)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.terminal.hide_requested.connect(lambda: self.term_toggle.setChecked(False))
        self.terminal.setVisible(self.settings["show_terminal"])

        self.q_summary = QLabel(self)
        self.q_cmd = QLabel(self)
        self.q_cmd.setFont(mono())
        self.q_cmd.setStyleSheet("background: #2a2e32; color: #e6e9ec; border-radius: 4px; padding: 4px 8px;")
        self.q_cmd.setMinimumWidth(200)
        self.b_review = button(_("Review"), "view-list-details", self.review_queue)
        self.b_clear = button(_("Clear"), "edit-clear", self.queue.clear)
        self.b_sim = button(_("Simulate"), "media-playback-start", self.simulate_queue)
        self.b_apply = button(_("Apply"), "dialog-ok-apply", self.apply_queue, primary=True)
        qbar = QFrame(self)
        qbar.setFrameShape(QFrame.Shape.StyledPanel)
        qh = QHBoxLayout(qbar)
        qh.setContentsMargins(10, 6, 10, 6)
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
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(30000)
        self.update_status()
        self.sidebar.setCurrentItem(self._rows["home"])
        self.terminal.view.note(_("zypper's own output appears here while it works."), "info")
        if not helper_path():
            QTimer.singleShot(400, self._warn_no_helper)

    # ------------------------------------------------ navigation
    def go(self, key, run_routine=False):
        page = self.pages[key]
        if self.sidebar.currentItem() is not self._rows[key]:
            self.sidebar.setCurrentItem(self._rows[key])
        self.stack.setCurrentWidget(page)
        self.page_title.setText(page.title)
        while self.actions_box.count():
            w = self.actions_box.takeAt(0).widget()
            if w:
                w.setParent(None)
        for w in page.header_actions():
            self.actions_box.addWidget(w)
        page.activated()
        if run_routine and key == "updates":
            page.run_routine()

    def _toggle_terminal(self, on):
        self.terminal.setVisible(on)
        self.settings["show_terminal"] = on
        self.settings.save()

    # ------------------------------------------------ running
    def run(self, steps, title, on_done=None):
        if self.runner.busy:
            QMessageBox.information(self, APP_NAME, _("Wait until the current command finishes."))
            return False
        if not any(s.capture for s in steps):
            self.term_toggle.setChecked(True)
        self.terminal.set_title(title)
        self._on_done = on_done
        self._set_busy(True)
        self.runner.run(steps)
        return True

    def _note(self, text, kind):
        if kind == "prompt":
            self.terminal.view.prompt(text)
        else:
            self.terminal.view.note(text, kind)

    def _finished(self, ok):
        self._set_busy(False)
        self.terminal.view.note(_("Finished.") if ok else _("Finished with errors."), "ok" if ok else "error")
        cb, self._on_done = self._on_done, None
        if cb:
            cb(ok)
        self.update_status()

    def _set_busy(self, busy):
        for b in (self.b_sim, self.b_apply, self.b_clear):
            b.setEnabled(not busy and len(self.queue) > 0)
        self.status_lbl.setText(_("Working…") if busy else self.status_lbl.text())

    # ------------------------------------------------ queue
    def _queue_changed(self):
        n = len(self.queue)
        parts = self.queue.summary()
        self.q_summary.setText("<b>" + _("Queue") + "</b>  " + (" · ".join(parts) if parts else _("empty")))
        preview = self.queue.preview() or _("Nothing to do yet: choose packages, locks or repository changes.")
        fm = QFontMetrics(self.q_cmd.font())
        self.q_cmd.setText(fm.elidedText(preview, Qt.TextElideMode.ElideRight, max(200, self.q_cmd.width() - 16)))
        self.q_cmd.setToolTip(preview)
        busy = self.runner.busy
        for b in (self.b_review, self.b_clear, self.b_sim, self.b_apply):
            b.setEnabled(n > 0 and not busy)

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._queue_changed)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._queue_changed()

    def review_queue(self):
        QueueDialog(self.queue, self).exec()

    def simulate_queue(self):
        st = self.queue.simulation_step()
        others = [s for s in self.queue.steps() if not s.args[1:2] or s.args[1] not in ("install", "remove")]
        if st is None:
            QMessageBox.information(self, _("Simulate"), _("zypper cannot simulate changes to repositories, locks "
                                                           "or languages. These commands will run:") + "\n\n"
                                    + self.queue.preview())
            return
        self.run([st], _("Simulation of the queue"), on_done=lambda ok: self._simulated(st, others))

    def _simulated(self, st, others):
        res = parse_dry_run(st.output)
        for line in summary_lines(res):
            self.terminal.view.note(line, "error" if res["problems"] and line.startswith(_("Problem: {text}").split("{")[0]) else "ok")
        dlg = SimulationDialog(res, self.queue.preview(), extra_steps=others, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.apply_queue(confirm=False)

    def apply_queue(self, confirm=True):
        steps = self.queue.steps()
        if not steps:
            return
        if confirm and not QMessageBox.question(
                self, _("Apply"), _("Run these commands now?") + "\n\n" + self.queue.preview()) == \
                QMessageBox.StandardButton.Yes:
            return
        self.run(steps, _("Applying the queue"), on_done=self._applied)

    def _applied(self, ok):
        if ok:
            self.queue.clear()
        for p in self.pages.values():
            if p.loaded and p is not self.pages["history"]:
                p.reload()
        if self.pages["packages"].results:
            self.pages["packages"].search()

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

    def _warn_no_helper(self):
        QMessageBox.information(self, APP_NAME, _("The helper that runs zypper as root is not installed, so the "
                                                  "window can search and show information but cannot change "
                                                  "anything yet. Run install.sh from the source folder."))

    def closeEvent(self, ev):
        if self.runner.busy:
            if QMessageBox.question(self, APP_NAME, _("zypper is still running. Close the window anyway? zypper "
                                                      "will be stopped.")) != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        super().closeEvent(ev)
