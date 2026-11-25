# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates: your routine (run step by step) and a simulation of 'zypper dup' with its options."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend, flatpak, routine, websources
from ..i18n import _, ngettext
from ..txqueue import Action
from ..runner import Step
from ..simulation import CATEGORIES, disk_text, human_size, parse_dry_run, summary_lines
from ..tasks import run_async
from ..ui import Card, Page, ask, button, error_box, heading, mono, muted

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
        nb = "\u00a0"  # keep "( ; )" together when the line wraps
        cond = (_("runs even if the previous one failed ( ; )").replace("( ; )", f"({nb};{nb})")
                if step.cond == "always" and number > 1 else
                _("only if the previous one worked ( && )").replace("( && )", f"({nb}&&{nb})")
                if step.cond == "success" else "")
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
        self.edit_btn = button(_("Edit the routine…"), "document-edit", self.edit_routine)
        rb.addWidget(self.edit_btn)
        rb.addStretch(1)
        self.result_lbl = QLabel("", self)
        self.result_lbl.setWordWrap(True)
        rc.lay.addLayout(rb)
        rc.lay.addWidget(muted(_("Nobody can answer zypper in the middle of a routine, so it runs with -n: it takes "
                                 "its default answers (“yes, continue”) and stops without changing anything when you "
                                 "would have to choose, for example in a conflict."), self))
        rc.lay.addWidget(self.result_lbl)
        lay.addWidget(rc)

        # ------------------------------------------------ what there is to update
        uc = Card(_("What there is to update"), self)
        self.up_lbl = muted(_("Looking…"), self)
        uc.lay.addWidget(self.up_lbl)
        self.up_tree = QTreeWidget(self)
        self.up_tree.setHeaderLabels([_("Program"), _("Version"), _("From")])
        self.up_tree.setMinimumHeight(160)
        self.up_tree.itemDoubleClicked.connect(self._up_double)
        uc.lay.addWidget(self.up_tree, 1)
        ub = QHBoxLayout()
        self.up_queue_btn = button(_("Add to the queue"), "list-add", self.queue_ticked, primary=True)
        self.up_queue_btn.setToolTip(_("Add the ticked ones to the queue"))
        ub.addWidget(self.up_queue_btn)
        self.up_sim_btn = button(_("Simulate the ticked ones"), "media-playback-start",
                                 lambda: self.queue_ticked(simulate=True))
        self.up_sim_btn.setToolTip(_("Programs from the web are downloaded and checked first: zypper can only "
                                     "simulate them with the file. Then the whole queue is simulated."))
        ub.addWidget(self.up_sim_btn)
        ub.addWidget(button(_("Look again"), "view-refresh", self.load_updates))
        ub.addStretch(1)
        uc.lay.addLayout(ub)
        uc.lay.addWidget(muted(_("Since the last refresh of the repositories. On Tumbleweed your routine (zypper dup) "
                                 "updates everything at once; here you can pick only some.")))

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
                               "to simulate. Programs from the web are not in it, because zypper does not know them "
                               "until their file is downloaded: use “Simulate the ticked ones” on the left."), self)
        sc.lay.addWidget(self.sim_lbl)
        self.sim_tree = QTreeWidget(self)
        self.sim_tree.setHeaderLabels([_("Package"), _("Version"), _("Repository")])
        self.sim_tree.setMinimumHeight(160)
        sc.lay.addWidget(self.sim_tree, 1)
        both = QHBoxLayout()
        both.addWidget(uc, 1)
        both.addWidget(sc, 1)
        lay.addLayout(both, 1)

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
        # the label treats '&' as a keyboard accelerator: '&&' must be written '&&&&' to show '&&'
        text, ok = QInputDialog.getText(
            self, _("Edit the routine"),
            _("Write it as in a terminal. ';' runs the next command anyway, '&&' only if the previous one "
              "worked. Only zypper and flatpak commands are allowed.").replace("&", "&&"),
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
        if self.ctx.runner.busy:
            self.result_lbl.setText(_("Wait until the current command finishes."))
            return
        self.show_routine()
        if not self.steps:
            return
        self.run_steps = self.steps
        self._running = True
        if not self.ctx.run(self.run_steps, _("Routine: {text}").format(text=self.ctx.settings["routine"]),
                            on_done=self._routine_done):
            self._running = False
            return
        self.result_lbl.setText("")
        self.run_btn.setEnabled(False)
        self.edit_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _step_started(self, i):
        if getattr(self, "_running", False) and i < len(self.boxes):
            self.boxes[i].set_state("running", _("running…"))

    def _step_finished(self, i, code):
        if not getattr(self, "_running", False) or i >= len(self.boxes):
            return
        st = self.run_steps[i]
        if st.state == "skipped":
            self.boxes[i].set_state("skipped", _("skipped"))
        elif st.state == "done":
            self.boxes[i].set_state("done", _("done (exit code {code})").format(code=code))
        else:
            self.boxes[i].set_state("failed", _("failed (exit code {code})").format(code=code))

    def _routine_done(self, ok):
        self._running = False
        self.run_btn.setEnabled(True)
        self.edit_btn.setEnabled(True)
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

    # ------------------------------------------------ what there is to update
    def activated(self):
        if self.loaded:
            self.load_updates()
        super().activated()

    def load_updates(self):
        self.up_lbl.setText(_("Looking…"))
        # the last answers stay on screen until the new ones arrive
        if not hasattr(self, "up_found"):
            self.up_found = {"repo": None, "flatpak": None, "web": None}
        self.up_errors = {}
        self.up_pending = {"repo", "flatpak", "web"}
        self.up_round = getattr(self, "up_round", 0) + 1
        rnd = self.up_round
        run_async(backend.list_updates, on_done=lambda res, err: self._up_part(rnd, "repo", res, err))
        if flatpak.available():
            run_async(flatpak.updates, on_done=lambda res, err: self._up_part(rnd, "flatpak", res, err))
        else:
            self.up_found["flatpak"] = []
            self.up_pending.discard("flatpak")
        sources = [x for x in websources.load() if x.get("type") != "manual"]

        def web():
            out = []
            for src in sources:
                try:
                    f = websources.check(src)
                except Exception as e:  # one site that does not answer must not hide the others
                    out.append(dict(name=src["package"], old="", new="", where=str(e), error=True))
                    continue
                if f.get("newer") or (f.get("newer") is None and f.get("changed")):  # = WebSourcesPage.counts_as_new
                    out.append(dict(name=src["package"], old=f.get("installed") or "", new=f.get("version") or "",
                                    where=websources.origin_text(src), changed=f.get("newer") is None))
            return out
        run_async(web, on_done=lambda res, err: self._up_part(rnd, "web", res, err))

    def _web_changed(self):
        if self.loaded:
            self.load_updates()

    def _up_part(self, rnd, part, res, err):
        if rnd != self.up_round:
            return  # an older round answered late
        self.up_pending.discard(part)
        self.up_found[part] = res or []
        if part == "web" and not err:
            n = len([u for u in res or [] if not u.get("error")])
            self.ctx.set_badge("websources", str(n) if n else "")
        if err:
            self.up_errors[part] = str(err)
        self._up_fill()

    def _up_fill(self):
        f = self.up_found
        # what you ticked or unticked stays as it was when the list fills again
        kept = {}
        for i in range(self.up_tree.topLevelItemCount()):
            top = self.up_tree.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                data = it.data(0, Qt.ItemDataRole.UserRole)
                if data and it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    kept[(data[0], it.text(0))] = it.checkState(0)
        self.up_tree.clear()
        groups = [
            ("repo", _("Packages from the repositories"), "system-software-update",
             [(u["name"], f"{u['old']} → {u['edition']}" if u["old"] else u["edition"], u["repo"], u)
              for u in (f["repo"] or [])]),
            ("flatpak", _("Flatpak apps"), "flatpak-discover",
             [(u.get("application"), u.get("version") or u.get("branch") or "", u.get("origin") or "", u)
              for u in (f["flatpak"] or [])]),
            ("web", _("Programs from the web"), "internet-web-browser",
             [(u["name"], _("could not check") if u.get("error") else
               (f"{u['old']} → {u['new']}" if u["new"] else _("{old} → file changed").format(old=u["old"])),
               u["where"], u) for u in (f["web"] or [])]),
        ]
        total = 0
        for key, label, icon_name, rows in groups:
            if f[key] is None and key not in self.up_errors:
                text = label + "  " + _("(looking…)")
            elif key in self.up_errors:
                text = label + "  " + _("(could not look: {error})").format(error=self.up_errors[key][:80])
            else:
                text = f"{label} ({len([r for r in rows if not r[3].get('error')])})"
            top = QTreeWidgetItem(self.up_tree, [text])
            top.setFirstColumnSpanned(True)
            top.setIcon(0, QIcon.fromTheme(icon_name))
            fnt = top.font(0)
            fnt.setBold(True)
            top.setFont(0, fnt)
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for name, ver, where, data in rows:
                it = QTreeWidgetItem(top, [name, ver, where])
                it.setData(0, Qt.ItemDataRole.UserRole, (key, data))
                if data.get("error"):
                    it.setToolTip(2, where)
                    for c in range(3):
                        it.setForeground(c, QBrush(QColor("#7f8c8d")))
                    continue
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, kept.get((key, name), Qt.CheckState.Checked))
                if key == "web":
                    it.setToolTip(0, _("Ticked, it is downloaded and checked (SHA-256, package name, signature) "
                                       "before going to the queue. Double-click to open Web sources."))
                    it.setForeground(0, QBrush(QColor("#f67400")))
                total += 1
            top.setExpanded(True)
        for c in range(3):
            self.up_tree.resizeColumnToContents(c)
        busy = bool(self.up_pending)
        if busy:
            self.up_lbl.setText(_("Looking…"))
        elif total:
            self.up_lbl.setText(ngettext("{n} update waiting.", "{n} updates waiting.", total).format(n=total))
        else:
            self.up_lbl.setText(_("Everything is up to date."))
        self.up_queue_btn.setEnabled(total > 0)
        self.up_sim_btn.setEnabled(total > 0)

    def _up_double(self, item, _col):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == "web":
            self.ctx.go("websources")

    def queue_ticked(self, simulate=False):
        n, web = 0, []
        with self.ctx.queue.batch():
            for i in range(self.up_tree.topLevelItemCount()):
                top = self.up_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    it = top.child(j)
                    data = it.data(0, Qt.ItemDataRole.UserRole)
                    if not data or it.checkState(0) != Qt.CheckState.Checked:
                        continue
                    kind, u = data
                    if kind == "repo":
                        self.ctx.queue.add(Action("install", u["name"]))
                        n += 1
                    elif kind == "flatpak":
                        self.ctx.queue.add(Action("flatpak-update", u.get("application")))
                        n += 1
                    else:
                        web.append(u["name"])
        text = ngettext("{n} update added to the queue: simulate it or apply it below.",
                        "{n} updates added to the queue: simulate them or apply them below.", n).format(n=n) if n else ""
        if web:
            text += " " + ngettext("{n} program from the web is being downloaded and checked; it joins the queue "
                                   "when it is ready (look at the terminal).",
                                   "{n} programs from the web are being downloaded and checked one after the "
                                   "other; each joins the queue when it is ready (look at the terminal).",
                                   len(web)).format(n=len(web))
        self.up_lbl.setText(text.strip())  # first: the simulation below may write its own message
        if simulate:
            if self.ctx.runner.busy:
                self.up_lbl.setText(_("Wait until the current command finishes."))
                return
            # the simulation runs once every program from the web is downloaded, checked and queued
            self.ctx.pages["websources"].get_new_versions(web, on_finished=self._simulate_queue)
        elif web:
            self.ctx.pages["websources"].get_new_versions(web)

    def _simulate_queue(self):
        if not self.ctx.queue.actions:
            self.up_lbl.setText(_("Nothing reached the queue, so there is nothing to simulate."))
            return
        self.ctx.simulate_queue()

    # ------------------------------------------------ dup simulation
    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)
        self.load_updates()

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
        if self.ctx.runner.busy:
            self.sim_lbl.setText(_("Wait until the current command finishes."))
            return
        st = Step("zypper", ["--xmlout", "-n", *self.dup_args(), "--dry-run"], lang="C", capture=True)
        if self.ctx.run([st], _("Simulation of zypper dup"), on_done=lambda ok: self._simulated(st)):
            self.sim_lbl.setText(_("Simulating…"))

    def _simulated(self, st):
        if st.code in (126, 127):
            self.sim_lbl.setText(_("Not simulated: the administrator password was not given."))
            return
        self.sim_tree.clear()
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
            counts.append(disk_text(res["disk"]))
        if res["problems"]:
            self.sim_lbl.setText(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:5]))
        elif not counts:
            self.sim_lbl.setText(_("Nothing to do: the system is up to date."))
        else:
            self.sim_lbl.setText(_("Nothing has been changed.") + "  " + "  ·  ".join(counts))

    def run_dup(self):
        lic = ["--auto-agree-with-licenses"] if self.ctx.settings["apply_accept_licenses"] else []
        st = Step("zypper", ["-n", "-v", *self.dup_args(), *lic])
        if ask(self, _("zypper dup"), _("Run this command now? It upgrades the whole system.") + "\n\n"
               + st.display()):
            self.ctx.run([st], _("zypper dup"))
