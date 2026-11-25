# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates: your routine (run step by step) and a simulation of 'zypper dup' with its options."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, routine
from ..i18n import _
from ..runner import Step
from ..simulation import CATEGORIES, human_size, parse_dry_run, summary_lines
from ..tasks import run_async
from ..ui import Card, Page, button, error_box, heading, mono, muted

STATE_STYLE = {
    "pending": ("", "palette(mid)", ""),
    "running": ("…", "palette(highlight)", "rgba(61,174,233,0.14)"),
    "done": ("✓", "#27ae60", "rgba(39,174,96,0.08)"),
    "failed": ("✕", "#da4453", "rgba(218,68,83,0.08)"),
    "skipped": ("–", "palette(mid)", ""),
}


class StepBox(QFrame):
    def __init__(self, number, step, parent=None):
        super().__init__(parent)
        self.setObjectName("step")
        self.number = number
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 8, 10, 8)
        self.badge = QLabel(str(number), self)
        self.badge.setFixedSize(24, 24)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(1)
        v.addWidget(QLabel(routine.describe(step), self))
        cmd = QLabel(step.display(), self)
        cmd.setFont(mono())
        cmd.setStyleSheet("color: palette(link);")
        v.addWidget(cmd)
        cond = _("runs even if the previous one failed ( ; )") if step.cond == "always" and number > 1 else (
            _("only if the previous one worked ( && )") if step.cond == "success" else "")
        self.state_lbl = muted(cond, self)
        v.addWidget(self.state_lbl)
        h.addLayout(v, 1)
        self.set_state("pending")

    def set_state(self, state, text=None):
        mark, border, bg = STATE_STYLE.get(state, STATE_STYLE["pending"])
        self.setStyleSheet(f"#step {{ border: 1px solid {border}; border-radius: 5px; background: {bg or 'none'}; }}")
        color = border if state != "pending" else "palette(highlight)"
        self.badge.setText(mark or str(self.number))
        self.badge.setStyleSheet(f"border-radius: 12px; background: {color}; color: white; font-weight: bold;")
        if text:
            self.state_lbl.setText(text)


class UpdatesPage(Page):
    key = "updates"
    title = _("Updates")
    icon_name = "system-software-update"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)

        # ------------------------------------------------ routine
        rc = Card(_("Your routine"), self)
        self.routine_lbl = QLabel(self)
        self.routine_lbl.setFont(mono())
        self.routine_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rc.lay.addWidget(self.routine_lbl)
        self.strip = QHBoxLayout()
        rc.lay.addLayout(self.strip)
        rb = QHBoxLayout()
        self.run_btn = button(_("Run the routine"), "system-software-update", self.run_routine, primary=True)
        self.stop_btn = button(_("Stop after this step"), "media-playback-pause", self.ctx.runner.stop_after_current)
        self.stop_btn.setEnabled(False)
        rb.addWidget(self.run_btn)
        rb.addWidget(self.stop_btn)
        rb.addWidget(button(_("Edit the routine…"), "document-edit", self.edit_routine))
        rb.addStretch(1)
        self.result_lbl = QLabel("", self)
        self.result_lbl.setWordWrap(True)
        rc.lay.addLayout(rb)
        rc.lay.addWidget(self.result_lbl)
        lay.addWidget(rc)

        # ------------------------------------------------ dup simulation
        sc = Card(_("What would 'zypper dup' do?"), self)
        s = ctx.settings
        self.o_vendor = QCheckBox(_("Allow vendor change (--allow-vendor-change)"), self)
        self.o_vendor.setChecked(s["dup_allow_vendor_change"])
        self.o_norec = QCheckBox(_("Do not install new recommended packages (--no-recommends)"), self)
        self.o_norec.setChecked(s["dup_no_recommends"])
        self.o_down = QCheckBox(_("Allow downgrades (--allow-downgrade)"), self)
        self.o_down.setChecked(s["dup_allow_downgrade"])
        self.o_from = QComboBox(self)
        self.o_from.addItem(_("From every enabled repository"), "")
        for cb in (self.o_vendor, self.o_norec, self.o_down):
            sc.lay.addWidget(cb)
            cb.toggled.connect(self._save_options)
        fr = QHBoxLayout()
        fr.addWidget(QLabel(_("Only from:"), self))
        fr.addWidget(self.o_from, 1)
        sc.lay.addLayout(fr)
        bb = QHBoxLayout()
        bb.addWidget(button(_("Simulate"), "media-playback-start", self.simulate_dup))
        bb.addWidget(button(_("Run 'zypper dup' with these options"), "system-run", self.run_dup))
        bb.addStretch(1)
        sc.lay.addLayout(bb)
        self.sim_lbl = muted(_("The simulation asks for the administrator password: zypper needs root even "
                               "to simulate."), self)
        sc.lay.addWidget(self.sim_lbl)
        self.sim_tree = QTreeWidget(self)
        self.sim_tree.setHeaderLabels([_("Package"), _("Version"), _("Repository")])
        self.sim_tree.setMinimumHeight(160)
        sc.lay.addWidget(self.sim_tree, 1)
        lay.addWidget(sc, 1)

        self.boxes = []
        self.steps = []
        self.show_routine()
        ctx.runner.step_started.connect(self._step_started)
        ctx.runner.step_finished.connect(self._step_finished)

    # ------------------------------------------------ routine
    def show_routine(self):
        text = self.ctx.settings["routine"]
        self.routine_lbl.setText(text)
        while self.strip.count():
            w = self.strip.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.boxes = []
        try:
            self.steps = routine.parse(text)
        except routine.RoutineError as e:
            self.steps = []
            self.strip.addWidget(QLabel(str(e), self))
            return
        for i, st in enumerate(self.steps, 1):
            box = StepBox(i, st, self)
            self.boxes.append(box)
            self.strip.addWidget(box, 1)

    def edit_routine(self):
        text, ok = QInputDialog.getText(
            self, _("Edit the routine"),
            _("Write it as in a terminal. ';' runs the next command anyway, '&&' only if the previous one "
              "worked. Only zypper and flatpak commands are allowed."),
            text=self.ctx.settings["routine"])
        if not ok:
            return
        try:
            routine.parse(text)
        except routine.RoutineError as e:
            error_box(self, _("Edit the routine"), str(e))
            return
        self.ctx.settings["routine"] = text.strip()
        self.ctx.settings.save()
        self.show_routine()

    def run_routine(self):
        self.show_routine()
        if not self.steps:
            return
        self._running = True
        self.result_lbl.setText("")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.ctx.run(self.steps, _("Routine: {text}").format(text=self.ctx.settings["routine"]),
                     on_done=self._routine_done)

    def _step_started(self, i):
        if getattr(self, "_running", False) and i < len(self.boxes):
            self.boxes[i].set_state("running", _("running…"))

    def _step_finished(self, i, code):
        if not getattr(self, "_running", False) or i >= len(self.boxes):
            return
        st = self.steps[i]
        if st.state == "skipped":
            self.boxes[i].set_state("skipped", _("skipped"))
        elif st.state == "done":
            self.boxes[i].set_state("done", _("done (exit code {code})").format(code=code))
        else:
            self.boxes[i].set_state("failed", _("failed (exit code {code})").format(code=code))

    def _routine_done(self, ok):
        self._running = False
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.result_lbl.setText(_("Routine finished.") if ok else _("The routine stopped with errors: look at "
                                                                     "the terminal."))
        if ok and self.ctx.settings["after_routine_check_restart"]:
            run_async(backend.processes_using_deleted_files, on_done=self._ps_done)

    def _ps_done(self, ps, err):
        if err or not ps:
            return
        if ps["core_updated"]:
            msg = _("Core libraries or services were updated: restarting the computer is recommended.")
        elif ps["processes"]:
            names = sorted({p["command"] for p in ps["processes"]})
            msg = _("No need to restart the computer. These programs still use old files; restart them: "
                    "{names}").format(names=", ".join(names[:12]))
        else:
            msg = _("No need to restart anything.")
        self.result_lbl.setText(self.result_lbl.text() + " " + msg)

    # ------------------------------------------------ dup simulation
    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)

    def _repos_loaded(self, repos, err):
        if err:
            return
        self.o_from.clear()
        self.o_from.addItem(_("From every enabled repository"), "")
        for r in sorted((r for r in repos if r["enabled"]), key=lambda r: r["name"].lower()):
            self.o_from.addItem(r["name"], r["alias"])

    def _save_options(self):
        s = self.ctx.settings
        s["dup_allow_vendor_change"] = self.o_vendor.isChecked()
        s["dup_no_recommends"] = self.o_norec.isChecked()
        s["dup_allow_downgrade"] = self.o_down.isChecked()
        s.save()

    def dup_args(self):
        args = ["dist-upgrade"]
        args.append("--allow-vendor-change" if self.o_vendor.isChecked() else "--no-allow-vendor-change")
        if self.o_norec.isChecked():
            args.append("--no-recommends")
        args.append("--allow-downgrade" if self.o_down.isChecked() else "--no-allow-downgrade")
        if self.o_from.currentData():
            args += ["--from", self.o_from.currentData()]
        return args

    def simulate_dup(self):
        st = Step("zypper", ["--xmlout", "-n", *self.dup_args(), "--dry-run"], lang="C", capture=True)
        self.sim_lbl.setText(_("Simulating…"))
        self.sim_tree.clear()
        self.ctx.run([st], _("Simulation of zypper dup"), on_done=lambda ok: self._simulated(st))

    def _simulated(self, st):
        res = parse_dry_run(st.output)
        for line in summary_lines(res):
            self.ctx.terminal.view.note(line, "ok")
        self.sim_tree.clear()
        counts = []
        for key, label in CATEGORIES:
            items = res["groups"].get(key, [])
            if not items:
                continue
            counts.append(f"{label}: {len(items)}")
            top = QTreeWidgetItem(self.sim_tree, [f"{label} ({len(items)})"])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for it in items:
                ver = f"{it['old']} → {it['edition']}" if it["old"] else it["edition"]
                QTreeWidgetItem(top, [it["name"], ver, it["repo"]])
            top.setExpanded(len(items) <= 50)
        for c in range(3):
            self.sim_tree.resizeColumnToContents(c)
        if res["download"]:
            counts.append(_("Download: {size}").format(size=human_size(res["download"])))
        if res["disk"]:
            counts.append(_("Disk: {size}").format(size=human_size(res["disk"])))
        if res["problems"]:
            self.sim_lbl.setText(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:5]))
        elif not counts:
            self.sim_lbl.setText(_("Nothing to do: the system is up to date."))
        else:
            self.sim_lbl.setText(_("Nothing has been changed.") + "  " + "  ·  ".join(counts))

    def run_dup(self):
        lic = ["--auto-agree-with-licenses"] if self.ctx.settings["apply_accept_licenses"] else []
        self.ctx.run([Step("zypper", ["-n", "-v", *self.dup_args(), *lic])], _("zypper dup"))
