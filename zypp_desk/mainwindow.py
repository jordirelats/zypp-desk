# SPDX-License-Identifier: GPL-3.0-or-later
"""The main window: sidebar, page header, pages, terminal panel and the queue bar."""

import html
import os
import shutil

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QScrollArea, QSizePolicy,
                               QListWidgetItem, QMainWindow, QMessageBox, QSplitter, QStackedWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_NAME, appicons, backend, websources
from .i18n import _
from .pages.apps import AppsPage
from .pages.flatpak import FlatpakPage
from .pages.keys import KeysPage
from .pages.packages import PackagesPage
from .pages.repos import ReposPage
from .pages.home import HomePage
from .pages.history import HistoryPage
from .pages.system import LocalesPage, LocksPage, MaintenancePage, ServicesPage, SettingsPage
from .pages.updates import UpdatesPage
from .pages.websources import WebSourcesPage
from .runner import Runner, Step, helper_path
from .settings import Settings
from .simulation import SimulationDialog, merge_results, parse_dry_run, summary_lines
from .tasks import run_async
from .terminal import TerminalPanel
from .txqueue import Action, TxQueue
from .ui import ask, button, error_box, heading, icon, info_box, mono, muted

SIDEBAR = [
    (None, ["home", "apps", "packages", "updates", "flatpak"]),
    (_("Sources"), ["repos", "keys", "services", "websources"]),
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
        drop = bb.addButton(_("Drop the selected change"), QDialogButtonBox.ButtonRole.ActionRole)
        drop.clicked.connect(self.drop)
        drop.setAutoDefault(False)  # Enter must never throw a change away
        close = bb.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        close.setDefault(True)
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
        self._terminal_used = False
        self.icon_catalog = {}

        # ------------------------------------------------ pages
        page_classes = [HomePage, AppsPage, PackagesPage, UpdatesPage, FlatpakPage, ReposPage, KeysPage, ServicesPage,
                        WebSourcesPage, LocksPage, LocalesPage, HistoryPage, MaintenancePage, SettingsPage]
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
        self.sidebar = QListWidget(self)
        self.sidebar.setIconSize(QSize(22, 22))
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setObjectName("sidebar")
        self._rows = {}
        for group, keys in SIDEBAR:
            if group:
                hdr = QListWidgetItem(group)
                hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                hdr.setSizeHint(QSize(0, 32))  # fixed heights: the highlight never covers the next row
                f = hdr.font()
                f.setPointSizeF(f.pointSizeF() * 0.9)
                hdr.setFont(f)
                self.sidebar.addItem(hdr)
            for k in keys:
                p = self.pages[k]
                it = QListWidgetItem(icon(p.icon_name, "application-vnd.flatpak.ref", "package-x-generic"), p.title)
                it.setData(Qt.ItemDataRole.UserRole, k)
                it.setSizeHint(QSize(0, 38))
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
        self.page_title = heading("", 1.35)
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

        self.q_summary = QLabel(self)
        # never wider than this: a long summary (German, many changes) is shortened instead of widening the window
        self.q_summary.setMaximumWidth(380)
        self.q_cmd = QLabel(self)
        self.q_cmd.setFont(mono())
        self.q_cmd.setStyleSheet("background: #2a2e32; color: #e6e9ec; border-radius: 4px; padding: 4px 8px;")
        self.q_cmd.setMinimumWidth(200)
        self.b_review = button(_("Review"), "view-list-details", self.review_queue)
        self.b_clear = button(_("Clear"), "edit-clear", self.queue.clear)
        self.b_sim = button(_("Simulate"), "media-playback-start", lambda: self.simulate_queue())
        self.b_apply = button(_("Apply"), "dialog-ok-apply", lambda: self.apply_queue(), primary=True)
        qbar = QFrame(self)
        qbar.setObjectName("queuebar")
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
        run_async(appicons.load_catalog, on_done=self._catalog_loaded)
        run_async(websources.clean_downloads, on_done=lambda _r, _e: None)
        self.sidebar.setCurrentItem(self._rows["home"])
        self.terminal.view.note(_("zypper's own output appears here while it works."), "info")
        if not helper_path():
            QTimer.singleShot(400, self._warn_no_helper)

    # ------------------------------------------------ navigation
    def go(self, key, run_routine=False):
        page = self.pages[key]
        if self.sidebar.currentItem() is not self._rows[key]:
            self.sidebar.setCurrentItem(self._rows[key])
        self.stack.setCurrentWidget(self._wrap[key])
        self.page_title.setText(page.title)
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
            page.run_routine()

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
        """Downloaded .rpm files (opened from Dolphin, or given on the command line)."""
        try:
            from .pages.localrpm import LocalRpmDialog
        except ImportError as e:
            error_box(self, APP_NAME, _("This part is not available: {error}").format(error=e))
            return
        LocalRpmDialog(self, paths, self).exec()

    def _catalog_loaded(self, catalog, err):
        if catalog:
            self.icon_catalog = catalog
            self.pages["packages"].refresh_icons()

    def set_badge(self, key, text):
        """A number after a section's name in the sidebar (for example, new versions found)."""
        page = self.pages[key]
        self._rows[key].setText(f"{page.title}  ({text})" if text else page.title)

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
        if self.runner.busy:
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
        self.runner.run(steps)
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
        self._set_busy(False)
        self.terminal.view.note(_("Finished.") if ok else _("Finished with errors."), "ok" if ok else "error")
        self._explain_run()
        cb, self._on_done = self._on_done, None
        if cb:
            cb(ok)
        self.update_status()

    def _set_busy(self, busy):
        for b in (self.b_sim, self.b_apply, self.b_clear):
            b.setEnabled(not busy and len(self.queue) > 0)
        self.status_lbl.setText(_("Working…") if busy else self.status_lbl.text())
        if not busy:
            self._queue_changed()  # the queue may have changed while it ran

    # ------------------------------------------------ queue
    def _queue_changed(self):
        n = len(self.queue)
        parts = self.queue.summary()
        rest = " · ".join(parts) if parts else _("empty")
        fm_s = QFontMetrics(self.q_summary.font())
        room = 380 - fm_s.horizontalAdvance(_("Queue") + "    ") - 8
        self.q_summary.setText("<b>" + html.escape(_("Queue")) + "</b>  "
                               + html.escape(fm_s.elidedText(rest, Qt.TextElideMode.ElideRight, room)))
        self.q_summary.setToolTip(" · ".join(parts))
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
        if rc == QDialog.DialogCode.Accepted:
            self.apply_queue(confirm=False, expected=snapshot)
        elif rc == getattr(SimulationDialog, "RESOLVE", 2):
            self.apply_solutions(getattr(dlg, "chosen", None) or [], then=self.simulate_queue)

    def apply_solutions(self, solutions, then=None, now=False):
        """What Jordi chose for each problem of a simulation, then `then` (simulate again).

        Queue simulation (now=False): removals and installs join the queue, a "discard" takes an install or a
        removal out of it, and locks and unlocks run at once, because a dry run only knows the locks that exist.
        Dup simulation (now=True): there is no queue to edit, so everything runs at once (a discarded install
        becomes a lock: zypper must not install it)."""
        def pkg(a):
            return a.opts.get("package") or a.target.split("=")[0]
        run_now = []
        with self.queue.batch():
            for sol in solutions:
                for a in sol.get("actions") or []:
                    if isinstance(a, dict):
                        if a.get("kind") != "discard":
                            continue
                        if now:
                            if a.get("of", "install") == "install":
                                run_now.append(Action("lock", a["target"]))
                            continue
                        kinds = (a["of"],) if a.get("of") else ("install", "remove")
                        for x in list(self.queue.of(*kinds)):
                            if pkg(x) == a.get("target"):
                                self.queue.discard_action(x)
                    elif a.kind in ("lock", "unlock") or (now and a.kind in ("remove", "install")):
                        run_now.append(a)
                    else:
                        self.queue.add(a)
        steps = []
        for kind, sub in (("unlock", "removelock"), ("lock", "addlock")):
            groups = {}
            for a in run_now:
                if a.kind == kind:
                    groups.setdefault(a.opts.get("type", "package"), []).append(a.target)
            for typ, names in groups.items():
                steps.append(Step("zypper", ["-n", sub, *([] if typ == "package" else ["-t", typ]), "--", *names]))
        for a in run_now:
            if a.kind in ("remove", "install"):
                steps.append(Step("zypper", ["-n", a.kind, *(["--oldpackage"] if a.opts.get("oldpackage") else []),
                                             "--", a.target]))
        if steps:
            text = "\n".join(s.display() for s in steps)
            locks = any(a.kind == "lock" for a in run_now)
            if not ask(self, _("Choose a solution"), _("These commands run now, before simulating again:") + "\n\n"
                       + text + ("\n\n" + _("You can unlock it later in Locks.") if locks else "")):
                return
            for i, st in enumerate(steps):
                st.cond = "always" if i == 0 else "success"
            self.run(steps, _("Choose a solution"), on_done=lambda ok: ok and then and then())
        elif then:
            then()

    def apply_queue(self, confirm=True, expected=None):
        actions = list(self.queue.actions)
        steps = self.queue.steps()
        if not steps:
            return
        if expected is not None and actions != expected:
            # something joined or left the queue after the simulation (a download finished): show it and ask again
            confirm = True
        if confirm and not ask(self, _("Apply"), _("Run these commands now?") + "\n\n" + self.queue.preview()):
            return
        if list(self.queue.actions) != actions:  # it changed while the question was open
            return self.apply_queue(confirm=True)
        if not self._files_unchanged(actions):
            return
        self.run(steps, _("Applying the queue"), on_done=lambda ok: self._applied(ok, actions))

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

    def _applied(self, ok, applied):
        if ok:
            with self.queue.batch():
                for a in applied:  # changes added while it ran stay in the queue
                    self.queue.discard_action(a)
            # files from the web wait in the private download folder until they are installed; now they join the
            # folder where you keep them (often a local zypper repository, synced with a cloud)
            web = []
            for a in applied:
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
            if web:  # new versions of programs from the web: offer to throw the old ones away
                QTimer.singleShot(800, lambda pk=set(filter(None, web)): self.pages["websources"].tidy(packages=pk))
        from . import tray
        tray.poke("check")  # the tray icon counts again (nothing happens if there is none)
        # the page in front looks again now; the others when you open them (not all at once)
        current = self.stack.currentWidget()
        for key, p in self.pages.items():
            if not p.loaded or key == "history":
                continue
            if self._wrap[key] is current:
                p.reload()
                if key == "packages" and p.results:
                    p.search()
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
