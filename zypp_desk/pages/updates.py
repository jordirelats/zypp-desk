# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates: your routine (run step by step), what there is to update with the package card next to it,
and a simulation of 'zypper dup' with its options in its own window."""

import html

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import appicons, backend, firmware, flatpak, routine, websources, zyppconf
from ..i18n import _, ngettext
from ..txqueue import Action
from ..runner import Step
from ..simulation import CATEGORIES, disk_text, human_size, parse_dry_run, summary_lines
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..ui import Card, Page, RoomSplitter, ask, button, heading, icon, mono, muted
from .pkgdetails import PackageDetails, table

FIRMWARE_COLOR = "#16a085"
ORIGIN_COLORS = {"repo": BLUE, "flatpak": PURPLE, "web": ORANGE, "firmware": FIRMWARE_COLOR}
ROW_ROLE = Qt.ItemDataRole.UserRole
# a warning inside the card: a tint of orange, readable in the light and the dark theme
WARN_HTML = ("<table width='100%' cellpadding='7' cellspacing='0' style='margin-top:8px;"
             "background-color:rgba(246,116,0,0.16)'><tr><td>⚠ {text}</td></tr></table>")


def is_checked(value):
    """A check state as PySide hands it back (the enum, or its number)."""
    return getattr(value, "value", value) == Qt.CheckState.Checked.value


class UpdateDelegate(QStyledItemDelegate):
    """A row of the list: tick, icon, name in bold, 'old → new' below, where it comes from on the right.
    Group titles are small grey capitals."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        row = index.data(ROW_ROLE)
        return QSize(200, 30 if row and row.get("header") else 54)

    @staticmethod
    def check_rect(r):
        return QRect(r.left() + 10, r.center().y() - 8, 16, 16)

    def paint(self, p, option, index):
        row = index.data(ROW_ROLE)
        if not row:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        pal = option.palette
        if row.get("header"):
            f = QFont(option.font)
            f.setPointSizeF(option.font.pointSizeF() * 0.85)
            f.setBold(True)
            f.setCapitalization(QFont.Capitalization.AllUppercase)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            p.setFont(f)
            p.setPen(pal.placeholderText().color())
            p.drawText(r.adjusted(8, 0, 0, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, row["header"])
            p.restore()
            return
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 60))
            p.drawRoundedRect(r, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 22))
            p.drawRoundedRect(r, 10, 10)
        x = r.left()
        if not row.get("error"):
            cr = QRectF(self.check_rect(r))
            if is_checked(index.data(Qt.ItemDataRole.CheckStateRole)):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(GREEN))
                p.drawRoundedRect(cr, 4, 4)
                pen = QPen(QColor("white"), 2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.drawPolyline([QPointF(cr.left() + 4, cr.center().y()), QPointF(cr.left() + 7, cr.bottom() - 4),
                                QPointF(cr.right() - 3.5, cr.top() + 4.5)])
            else:
                p.setPen(QPen(pal.placeholderText().color(), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(cr.adjusted(0.7, 0.7, -0.7, -0.7), 4, 4)
        self.page.row_icon(row).paint(p, QRect(x + 36, r.center().y() - 16, 32, 32))
        tx = x + 78
        # the pill on the right
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        sfm = QFontMetrics(small)
        pill = row["where"] if not row.get("error") else _("could not check")
        pill = sfm.elidedText(pill, Qt.TextElideMode.ElideMiddle, 230)
        pw = sfm.horizontalAdvance(pill) + 18
        color = QColor("#7f8c8d" if row.get("error") else ORIGIN_COLORS.get(row["kind"], BLUE))
        pr = QRect(r.right() - 8 - pw, r.center().y() - 11, pw, 22)
        bg = QColor(color)
        bg.setAlpha(40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(pr, 11, 11)
        p.setPen(color)
        p.setFont(small)
        p.drawText(pr, Qt.AlignmentFlag.AlignCenter, pill)
        # name and versions
        room = pr.left() - tx - 10
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(pal.text().color())
        p.drawText(QRect(tx, r.top() + 6, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(bold).elidedText(row["name"], Qt.TextElideMode.ElideRight, room))
        p.setFont(mono())
        p.setPen(pal.placeholderText().color())
        p.drawText(QRect(tx, r.top() + 26, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(mono()).elidedText(row["version"], Qt.TextElideMode.ElideRight, room))
        p.restore()

    def editorEvent(self, event, model, option, index):
        row = index.data(ROW_ROLE)
        if not row or row.get("header") or row.get("error"):
            return False
        toggle = False
        if event.type() == QEvent.Type.MouseButtonRelease and self.check_rect(option.rect.adjusted(4, 2, -4, -2)) \
                .adjusted(-4, -4, 4, 4).contains(event.position().toPoint()):
            toggle = True
        elif event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            toggle = True
        if toggle:
            on = is_checked(index.data(Qt.ItemDataRole.CheckStateRole))
            model.setData(index, Qt.CheckState.Unchecked if on else Qt.CheckState.Checked,
                          Qt.ItemDataRole.CheckStateRole)
            return True
        return False

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
        h.setContentsMargins(10, 6, 10, 6)
        self.badge = QLabel(str(number), self)
        self.badge.setFixedSize(24, 24)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(1)
        title = QLabel(routine.describe(step), self)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        title.setWordWrap(True)  # German titles are long: they wrap instead of widening the window
        cmd = QLabel(step.display(), self)
        cmd.setTextFormat(Qt.TextFormat.PlainText)
        cmd.setFont(mono())
        cmd.setStyleSheet("color: palette(link);")
        cmd.setToolTip(step.display())
        nb = "\u00a0"  # keep "( ; )" together when the line wraps
        cond = (_("runs even if the previous one failed ( ; )").replace("( ; )", f"({nb};{nb})")
                if step.cond == "always" and number > 1 else
                _("only if the previous one worked ( && )").replace("( && )", f"({nb}&&{nb})")
                if step.cond == "success" else "")
        cmd.setWordWrap(True)
        self.state_lbl = muted(cond, self)
        v.addWidget(title)
        v.addWidget(cmd)
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
    # what the list found, each time it fills: {"total", "repo", "flatpak", "web", "firmware", "busy",
    # "pending" (parts still looking), "errors", "flatpak_ids"}; the Home page shows the same numbers
    counted = Signal(object)

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)

        # ------------------------------------------------ routine: one band
        rc = Card(None, self)
        rc.lay.setContentsMargins(16, 10, 16, 10)
        head = QHBoxLayout()
        self.routine_lbl = heading(_("Your routine"), 1.05, self)  # the whole routine is in its tooltip
        head.addWidget(self.routine_lbl)
        head.addStretch(1)
        info = QLabel(self)
        info.setPixmap(icon("help-about", "dialog-information").pixmap(18, 18))
        info.setToolTip(_("Nobody can answer zypper in the middle of a routine, so it runs with -n: it takes "
                          "its default answers (“yes, continue”) and stops without changing anything when you "
                          "would have to choose, for example in a conflict."))
        head.addWidget(info)
        self.edit_btn = button(_("Edit the routine…"), "document-edit", self.edit_routine)
        self.dup_btn = button(_("Simulate the dup…"), "system-run", self.open_dup)
        self.dup_btn.setToolTip(_("What would 'zypper dup' do?"))
        self.stop_btn = button(_("Stop after this step"), "media-playback-pause", self.ctx.runner.stop_after_current)
        self.stop_btn.hide()  # it takes the place of Run while the routine runs
        self.run_btn = button(_("Run the routine"), "system-software-update", self.run_routine, primary=True)
        for b in (self.edit_btn, self.dup_btn, self.stop_btn, self.run_btn):
            head.addWidget(b)
        rc.lay.addLayout(head)
        self.strip = QHBoxLayout()
        self.strip.setSpacing(8)
        rc.lay.addLayout(self.strip)
        self.result_lbl = QLabel("", self)
        self.result_lbl.setWordWrap(True)
        self.result_lbl.hide()
        rc.lay.addWidget(self.result_lbl)
        lay.addWidget(rc)

        # ------------------------------------------------ below: the list | the package card
        lc = Card(None, self)
        lc.lay.setContentsMargins(12, 10, 12, 10)
        lh = QHBoxLayout()
        lh.addWidget(heading(_("What there is to update"), 1.05, self))
        self.up_count = QLabel("", self)
        self.up_count.setStyleSheet(f"background: {GREEN}; color: white; border-radius: 9px; padding: 1px 8px;"
                                    " font-weight: bold;")
        self.up_count.hide()
        lh.addWidget(self.up_count)
        lh.addStretch(1)
        again = button(_("Look again"), "view-refresh", lambda: self.load_updates())
        again.setToolTip(_("Since the last refresh of the repositories. On Tumbleweed your routine (zypper dup) "
                           "updates everything at once; here you can pick only some."))
        lh.addWidget(again)
        lc.lay.addLayout(lh)
        self.up_lbl = muted(_("Looking…"), self)
        lc.lay.addWidget(self.up_lbl)
        self.up_list = QListWidget(self)
        self.up_list.setFrameShape(QFrame.Shape.NoFrame)
        self.up_list.setItemDelegate(UpdateDelegate(self))
        self.up_list.setMouseTracking(True)
        self.up_list.setStyleSheet("QListWidget { background: transparent; }")
        self.up_list.currentItemChanged.connect(lambda cur, _p: self._show_row(cur))
        self.up_list.itemDoubleClicked.connect(self._up_double)
        lc.lay.addWidget(self.up_list, 1)
        ub = QHBoxLayout()
        ub.addStretch(1)
        self.up_sim_btn = button(_("Simulate the ticked ones"), "media-playback-start",
                                 lambda: self.queue_ticked(simulate=True))
        self.up_sim_btn.setToolTip(_("Programs from the web are downloaded and checked first: zypper can only "
                                     "simulate them with the file. Then the whole queue is simulated."))
        self.up_queue_btn = button(_("Add to the queue"), "list-add", self.queue_ticked, primary=True)
        self.up_queue_btn.setToolTip(_("Add the ticked ones to the queue"))
        ub.addWidget(self.up_sim_btn)
        ub.addWidget(self.up_queue_btn)
        lc.lay.addLayout(ub)

        dc = Card(None, self)
        dc.lay.setContentsMargins(10, 12, 12, 10)
        self.updates = {}
        self.locked_names = set()
        self.details = PackageDetails(self, ctx, dc)
        dc.lay.addWidget(self.details)
        split = RoomSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(lc)
        split.addWidget(dc)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([620, 440])
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)
        ctx.queue.changed.connect(self.details.refresh)

        # ------------------------------------------------ the whole dup, in its own window
        self.dup_dialog = self._build_dup_dialog()

        self.boxes = []
        self.steps = []
        self.show_routine()
        ctx.runner.step_started.connect(self._step_started)
        ctx.runner.step_finished.connect(self._step_finished)

    # ------------------------------------------------ routine
    def show_routine(self):
        text = self.ctx.settings["routine"]
        self.routine_lbl.setToolTip(text)
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
        from .routine_editor import RoutineEditorDialog
        dlg = RoutineEditorDialog(self.ctx.settings["routine"], self)
        if dlg.exec():
            self.ctx.settings["routine"] = dlg.text()
            self.ctx.settings.save()
            self.show_routine()
        dlg.deleteLater()

    def _set_running(self, running):
        """The buttons of the routine band while it runs (Stop takes the place of Run) and after."""
        self._running = running
        self.run_btn.setVisible(not running)
        self.stop_btn.setVisible(running)
        self.stop_btn.setEnabled(running)
        self.edit_btn.setEnabled(not running)

    def run_routine(self):
        if self.ctx.runner.busy:
            self.result_lbl.setText(_("Wait until the current command finishes."))
            self.result_lbl.show()
            return
        self.show_routine()
        if not self.steps:
            return
        self.run_steps = self.steps
        self.result_lbl.setText("")
        self.result_lbl.hide()
        # before ctx.run: the routine may end inside it (no helper, no password) and _routine_done
        # must find the buttons as they are while it runs
        self._set_running(True)
        if not self.ctx.run(self.run_steps, _("Routine: {text}").format(text=self.ctx.settings["routine"]),
                            on_done=self._routine_done):
            self._set_running(False)

    def _after_change(self):
        """The system changed (the routine, a dup): the pages look again (the main window knows which)."""
        fn = getattr(self.ctx, "after_change", None)
        if callable(fn):
            fn()
        else:
            from .. import tray
            tray.poke("check")
            self.reload()

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
        self._set_running(False)
        if not ok and getattr(self.ctx.runner, "stopped", False):
            self.result_lbl.setText(_("The routine was stopped: the steps after it did not run."))
        else:
            self.result_lbl.setText(_("Routine finished.") if ok else _("The routine stopped with errors: look "
                                                                         "at the terminal."))
        self.result_lbl.show()
        if ok and self.ctx.settings["after_routine_check_restart"]:
            run_async(backend.processes_using_deleted_files, on_done=self._ps_done)
        self._after_change()  # what it updated is not waiting any more: the list (and the tray) look again

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
        self._say(_("Looking…"))
        # the last answers stay on screen until the new ones arrive
        if not hasattr(self, "up_found"):
            self.up_found = {"repo": None, "flatpak": None, "web": None, "firmware": None}
        self.up_errors = {}
        self.up_pending = {"repo", "flatpak", "web", "firmware"}
        self.up_round = getattr(self, "up_round", 0) + 1
        rnd = self.up_round
        run_async(backend.list_updates, on_done=lambda res, err: self._up_part(rnd, "repo", res, err))
        if flatpak.available():
            run_async(flatpak.updates, on_done=lambda res, err: self._up_part(rnd, "flatpak", res, err))
        else:
            self.up_found["flatpak"] = []
            self.up_pending.discard("flatpak")
        if firmware.available():
            run_async(firmware.updates, on_done=lambda res, err: self._up_part(rnd, "firmware", res, err))
        else:
            self.up_found["firmware"] = []
            self.up_pending.discard("firmware")
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

    def _say(self, text):
        """The line under the title of the list: only when there is something to say."""
        self.up_lbl.setText(text)
        self.up_lbl.setVisible(bool(text))

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
        # what you ticked or unticked, and what is selected, stay as they were when the list fills again
        kept, cur_key = {}, None
        for i in range(self.up_list.count()):
            it = self.up_list.item(i)
            row = it.data(ROW_ROLE)
            if row and not row.get("header"):
                kept[(row["kind"], row["name"])] = it.checkState()
        cur = self.up_list.currentItem()
        if cur and cur.data(ROW_ROLE) and not cur.data(ROW_ROLE).get("header"):
            cur_key = (cur.data(ROW_ROLE)["kind"], cur.data(ROW_ROLE)["name"])
        self.up_list.blockSignals(True)
        self.up_list.clear()
        self.updates = {u["name"]: u for u in (f["repo"] or [])}
        groups = [
            ("repo", _("Packages from the repositories"),
             [dict(kind="repo", name=u["name"], version=f"{u['old']} → {u['edition']}" if u["old"] else u["edition"],
                   where=u["repo"], data=u) for u in (f["repo"] or [])]),
            ("flatpak", _("Flatpak apps"),
             [dict(kind="flatpak", name=u.get("application") or "", version=u.get("version") or u.get("branch") or "",
                   where=u.get("origin") or "", data=u) for u in (f["flatpak"] or [])]),
            ("web", _("Programs from the web"),
             [dict(kind="web", name=u["name"], error=bool(u.get("error")), where=u["where"], data=u,
                   version=(_("could not check") if u.get("error") else
                            f"{u['old']} → {u['new']}" if u["new"] else _("{old} → file changed").format(old=u["old"])))
              for u in (f["web"] or [])]),
            ("firmware", _("Firmware"),
             [dict(kind="firmware", name=u["name"], version=f"{u['version']} → {u['new_version']}",
                   where=" · ".join(x for x in (u.get("release_vendor") or u.get("vendor"), "LVFS") if x), data=u)
              for u in (f.get("firmware") or [])]),
        ]
        total, select = 0, None
        for key, label, rows in groups:
            if not rows:
                continue
            hdr = QListWidgetItem()
            hdr.setData(ROW_ROLE, {"header": label})
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            self.up_list.addItem(hdr)
            for row in rows:
                it = QListWidgetItem()
                it.setData(ROW_ROLE, row)
                # the row elides long names and versions: the tooltip has them whole
                it.setToolTip(f"{row['name']}\n{row['where']}" if row.get("error") else
                              "\n".join(x for x in (row["name"], row["version"], row["where"]) if x))
                if row.get("error"):
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                else:
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(kept.get((key, row["name"]), Qt.CheckState.Checked))
                    total += 1
                self.up_list.addItem(it)
                if (key, row["name"]) == cur_key or select is None:
                    select = it if (key, row["name"]) == cur_key or select is None else select
        self.up_list.blockSignals(False)
        if select is not None and self.up_list.currentItem() is None:
            self.up_list.setCurrentItem(select)
        if not hasattr(self, "_desk_icons"):
            self._desk_icons = {}
        names = sorted({r["name"] for _k, _l, rows in groups for r in rows if r["kind"] in ("repo", "web")}
                       - set(self._desk_icons))
        if names:
            run_async(appicons.desktop_icons, names, on_done=self._icons_loaded)
        busy = bool(self.up_pending)
        self.up_count.setText(str(total))
        self.up_count.setVisible(total > 0)
        counts = {key: sum(1 for r in rows if not r.get("error")) for key, _l, rows in groups}
        if not busy:  # the sidebar says it as in the approved mockup: "Updates (4)"
            self.ctx.set_badge(self.key, str(total) if total else "")
        self.counted.emit(dict(counts, total=total, busy=busy, pending=sorted(self.up_pending),
                               errors=dict(self.up_errors),
                               flatpak_ids=[u.get("application") for u in (f["flatpak"] or [])]))
        errors = [f"{k}: {v[:80]}" for k, v in self.up_errors.items()]
        if busy:
            text = _("Looking…")
        elif total:
            text = ""  # the green number next to the title says it (a line less: the list keeps its room)
            self.up_count.setToolTip(ngettext("{n} update waiting.", "{n} updates waiting.", total).format(n=total))
        else:
            text = _("Everything is up to date.")
            self.details.show_simple(_("Everything is up to date."), "", icon("checkmark", "dialog-ok"), "")
        if errors:
            text = (text + "  " + _("(could not look: {error})").format(error="; ".join(errors))).strip()
        self._say(text)
        self.up_queue_btn.setEnabled(total > 0)
        self.up_sim_btn.setEnabled(total > 0)

    # ------------------------------------------------ the package card
    def pkg_icon(self, d):
        name = self._desk_icons.get(d["name"]) if hasattr(self, "_desk_icons") else None
        if name:
            ic = QIcon(name) if name.startswith("/") else QIcon.fromTheme(name)
            if not ic.isNull():
                return ic
        return self.ctx.pages["packages"].pkg_icon(d)

    def _icons_loaded(self, icons, err):
        if icons:
            self._desk_icons.update(icons)
            self.up_list.viewport().update()
            self.details.refresh_icon()

    def row_icon(self, row):
        if row["kind"] == "firmware":
            return icon(*(row["data"].get("icons") or []), "computer", "cpu", "application-x-firmware")
        if row["kind"] == "flatpak":
            ic = QIcon.fromTheme(row["name"])
            if ic.isNull():
                path = appicons.flatpak_icon_file(row["name"])
                ic = QIcon(path) if path else icon("flatpak-discover", "package-x-generic")
            return ic
        return self.pkg_icon({"name": row["name"]})

    def _show_row(self, item):
        row = item.data(ROW_ROLE) if item else None
        if not row or row.get("header"):
            return
        self.details.set_custom(None)
        if row.get("error"):
            self._detail_for = None
            self.details.show_simple(row["name"], _("could not check"), self.row_icon(row), html.escape(row["where"]))
            return
        if row["kind"] == "firmware":
            self._show_firmware(row)
            return
        if row["kind"] == "flatpak":
            self._detail_for = None
            u = row["data"]
            e = html.escape
            rows = [(_("New version"), f"<b>{e(u.get('version') or '')}</b>"), (_("Branch"), e(u.get("branch") or "")),
                    (_("Comes from"), e(u.get("origin") or ""))]
            self.details.show_simple(row["name"], _("Flatpak app"), self.row_icon(row), table(rows))
            return
        name = row["name"]
        if getattr(self, "_detail_for", None) == (row["kind"], name) and self.details.current \
                and self.details.current["name"] == name:
            return  # already shown (the list was only filled again)
        self._detail_for = (row["kind"], name)
        u = row["data"]
        # what is known already shows at once; the search (a few seconds) completes the card later
        old = u.get("old") or ""
        quick = {"name": name, "kind": "package", "summary": "", "versions": [], "best": None,
                 "installed": {"edition": old, "arch": u.get("arch", ""), "repo": "", "status": "installed"}
                 if old else None}
        overrides = {}
        if row["kind"] == "web":
            overrides = {"from": u["where"],
                         "new": u["new"] or _("the file changed: download it to see its version")}
            self.details.set_custom(_("Download the new version"),
                                    lambda n=name: self.ctx.pages["websources"].get_new_versions([n]))
        self.details.load(quick, overrides)
        run_async(backend.search, name, exact=True, on_done=lambda res, err, r=row: self._row_package(r, res, err))

    def _show_firmware(self, row):
        """The approved firmware card: Information / What changes / All devices."""
        u = row["data"]
        self._detail_for = ("firmware", row["name"])
        esc = html.escape
        rows = [(_("You have"), esc(u["version"])), (_("New version"), f"<b>{esc(u['new_version'])}</b>")]
        if u.get("size"):
            rows.append((_("Size"), esc(human_size(u["size"]))))
        rows.append((_("Comes from"), esc(_("LVFS (fwupd.org), where {vendor} publishes its firmware").format(
            vendor=u.get("release_vendor") or u.get("vendor") or "?"))))
        rows.append((_("Installed"), esc(_("when the computer restarts, before Linux starts") if u.get("needs_reboot")
                                        else _("at once, while you work"))))
        if u.get("urgency") in ("high", "critical"):
            rows.append((_("Urgency"), f"<b style='color:{RED}'>{esc(_('high: install it soon'))}</b>"))
        warn = WARN_HTML.format(text=esc(_("Plug in the charger and do not switch the computer off while it "
                                           "updates.")))
        # the exact command the queue runs (txqueue: firmware), shown whole
        cmd = Step("fwupdmgr", ["update", u["id"], "--assume-yes", "--no-reboot-check", "--no-unreported-check"])
        cmd_html = f"<p style='color:gray;font-family:monospace'>{esc(cmd.display())}</p>"
        tabs = []
        what = u.get("description") or u.get("release_summary") or ""
        if what:
            tabs.append((_("What changes"), "<p>" + "<br>".join(esc(x) for x in what.split("\n") if x.strip()) + "</p>"))
        if getattr(self, "_fw_devices", None):
            items = "".join(f"<li>{esc(d['name'])} <span style='color:gray'>{esc(d['version'])}</span></li>"
                            for d in self._fw_devices)
            tabs.append((_("All devices"), f"<p>{esc(_('Every device fwupd knows'))}</p><ul>{items}</ul>"))
        elif firmware.available():
            tabs.append((_("All devices"), f"<i>{esc(_('Loading…'))}</i>"))
            if not getattr(self, "_fw_devices_asked", False):
                self._fw_devices_asked = True
                run_async(firmware.devices, on_done=lambda res, err, r=row: self._fw_devices_loaded(r, res))
        self.details.show_simple(u["name"], u.get("release_summary") or u.get("vendor") or _("Firmware"),
                                 self.row_icon(row), table(rows) + warn + cmd_html, tabs)
        self.details.set_custom(_("Add it to the queue"),
                                lambda: self.ctx.queue.add(Action("firmware", u["id"], name=u["name"],
                                                                  version=u["new_version"])))

    def _fw_devices_loaded(self, row, res):
        self._fw_devices = res or []
        if getattr(self, "_detail_for", None) == ("firmware", row["name"]):
            tab = self.details.tabs.currentIndex()
            self._show_firmware(row)
            self.details.tabs.setCurrentIndex(tab)  # stay on the tab that was open

    def _row_package(self, row, res, err):
        if getattr(self, "_detail_for", None) != (row["kind"], row["name"]):
            return  # another row was selected meanwhile
        d = next((x for x in res or [] if x["name"] == row["name"]), None)
        if d is not None:
            self.details.refine(d)

    def _up_double(self, item):
        row = item.data(ROW_ROLE)
        if row and row.get("kind") == "web":
            self.ctx.go("websources")

    def queue_ticked(self, simulate=False):
        n, web = 0, []
        with self.ctx.queue.batch():
            for i in range(self.up_list.count()):
                it = self.up_list.item(i)
                row = it.data(ROW_ROLE)
                if not row or row.get("header") or row.get("error") or not is_checked(it.checkState()):
                    continue
                if row["kind"] == "repo":
                    self.ctx.queue.add(Action("install", row["name"]))
                    n += 1
                elif row["kind"] == "flatpak":
                    self.ctx.queue.add(Action("flatpak-update", row["name"]))
                    n += 1
                elif row["kind"] == "firmware":
                    u = row["data"]
                    self.ctx.queue.add(Action("firmware", u["id"], name=u["name"], version=u["new_version"]))
                    n += 1
                else:
                    web.append(row["name"])
        text = ngettext("{n} update added to the queue: simulate it or apply it below.",
                        "{n} updates added to the queue: simulate them or apply them below.", n).format(n=n) if n else ""
        if web:
            text += " " + ngettext("{n} program from the web is being downloaded and checked; it joins the queue "
                                   "when it is ready (look at the terminal).",
                                   "{n} programs from the web are being downloaded and checked one after the "
                                   "other; each joins the queue when it is ready (look at the terminal).",
                                   len(web)).format(n=len(web))
        self._say(text.strip())  # first: the simulation below may write its own message
        if simulate:
            if self.ctx.runner.busy:
                self._say(_("Wait until the current command finishes."))
                return
            # the simulation runs once every program from the web is downloaded, checked and queued
            self.ctx.pages["websources"].get_new_versions(web, on_finished=self._simulate_queue)
        elif web:
            self.ctx.pages["websources"].get_new_versions(web)

    def _simulate_queue(self):
        if not self.ctx.queue.actions:
            self._say(_("Nothing reached the queue, so there is nothing to simulate."))
            return
        self.ctx.simulate_queue()

    # ------------------------------------------------ dup simulation, in its own window
    def _build_dup_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(_("What would 'zypper dup' do?"))
        dlg.resize(820, 560)
        dl = QVBoxLayout(dlg)
        s = self.ctx.settings
        # the same names as the options of the routine editor
        self._vendor_conf = self._read_vendor_conf()
        self.o_vendor = QCheckBox(_("Allow a change of vendor (--allow-vendor-change)"), dlg)
        self.o_vendor.setToolTip(_("Let a package move to another vendor, for example from openSUSE to Packman. "
                                   "It starts as zypp.conf says (solver.dupAllowVendorChange): the command gets an "
                                   "option only when you choose otherwise."))
        self.o_vendor.setChecked(self._vendor_conf)
        self.o_norec = QCheckBox(_("Without recommended packages (--no-recommends)"), dlg)
        self.o_norec.setToolTip(_("Install only what the packages need, not what they suggest (--no-recommends)."))
        self.o_norec.setChecked(s["dup_no_recommends"])
        self.o_down = QCheckBox(_("Allow downgrades (--allow-downgrade)"), dlg)
        self.o_down.setToolTip(_("Let zypper install an older version when a repository only has that one "
                                 "(--allow-downgrade)."))
        self.o_down.setChecked(s["dup_allow_downgrade"])
        self.o_from = QComboBox(dlg)
        self.o_from.addItem(_("From every enabled repository"), "")
        self.o_from.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.o_from.setMinimumContentsLength(12)
        opts = QGridLayout()
        opts.setHorizontalSpacing(24)
        opts.addWidget(self.o_vendor, 0, 0)
        opts.addWidget(self.o_norec, 0, 1)
        opts.addWidget(self.o_down, 1, 0)
        fr = QHBoxLayout()
        fr.addWidget(QLabel(_("Only from:"), dlg))
        fr.addWidget(self.o_from, 1)
        opts.addLayout(fr, 1, 1)
        for cb in (self.o_norec, self.o_down):
            cb.toggled.connect(self._save_options)
        for cb in (self.o_vendor, self.o_norec, self.o_down):
            cb.toggled.connect(self._show_dup_cmd)
        self.o_from.currentIndexChanged.connect(self._show_dup_cmd)
        dl.addLayout(opts)
        # the exact commands the two buttons run, as they change with the options
        self.dup_cmd = QLabel(dlg)
        self.dup_cmd.setFont(mono())
        self.dup_cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.dup_cmd.setWordWrap(True)
        self.dup_cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dup_cmd.setStyleSheet("color: palette(placeholder-text);")
        dl.addWidget(self.dup_cmd)
        self.sim_lbl = muted(_("It asks for the administrator password. Programs from the web are not in it."), dlg)
        self.sim_lbl.setToolTip(_("zypper needs root even to simulate, and it does not know a program from the web "
                                  "until its file is downloaded: simulate those with “Simulate the ticked ones”."))
        dl.addWidget(self.sim_lbl)
        self.sim_tree = QTreeWidget(dlg)
        self.sim_tree.setHeaderLabels([_("Package"), _("Version"), _("Repository")])
        self.sim_tree.setAlternatingRowColors(True)
        dl.addWidget(self.sim_tree, 1)
        bb = QHBoxLayout()
        bb.addStretch(1)
        # Enter simulates (nothing changes); the real dup needs a click, and then its question answers No to Enter
        run_btn = button(_("Run 'zypper dup' with these options"), "system-run", self.run_dup)
        run_btn.setAutoDefault(False)
        sim_btn = button(_("Simulate"), "media-playback-start", self.simulate_dup, primary=True)
        sim_btn.setDefault(True)
        close_btn = button(_("Close"), "window-close", dlg.close)
        close_btn.setAutoDefault(False)
        for b in (run_btn, sim_btn, close_btn):
            bb.addWidget(b)
        dl.addLayout(bb)
        self._show_dup_cmd()
        return dlg

    @staticmethod
    def _read_vendor_conf():
        """solver.dupAllowVendorChange as libzypp reads it now (zypp.conf and its drop-ins)."""
        try:
            return zyppconf.read_effective()["solver.dupAllowVendorChange"]["value"] == "true"
        except Exception:  # an unreadable file: libzypp's own default
            return False

    def open_dup(self):
        if not self.dup_dialog.isVisible():
            # as the routine and a dup in a terminal do: what zypp.conf says, unless you tick otherwise
            self._vendor_conf = self._read_vendor_conf()
            self.o_vendor.setChecked(self._vendor_conf)
            self._show_dup_cmd()
        self.dup_dialog.show()
        self.dup_dialog.raise_()
        self.dup_dialog.activateWindow()

    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)
        run_async(backend.locks, on_done=self._locks_loaded)
        self.load_updates()

    def _locks_loaded(self, locks, err):
        if not err:
            self.locked_names = {lk["name"] for lk in locks or []}
            self.details.refresh()

    def _repos_loaded(self, repos, err):
        if err:
            return
        self.o_from.clear()
        self.o_from.addItem(_("From every enabled repository"), "")
        for r in sorted((r for r in repos if r["enabled"]), key=lambda r: r["name"].lower()):
            self.o_from.addItem(r["name"], r["alias"])

    def _save_options(self):
        # the change of vendor is not kept: it starts from zypp.conf each time (see open_dup)
        s = self.ctx.settings
        s["dup_no_recommends"] = self.o_norec.isChecked()
        s["dup_allow_downgrade"] = self.o_down.isChecked()
        s.save()

    def dup_args(self):
        args = ["dist-upgrade"]
        vendor = self.o_vendor.isChecked()
        if vendor != self._vendor_conf:  # the same as zypp.conf: no option, as in the routine
            args.append("--allow-vendor-change" if vendor else "--no-allow-vendor-change")
        if self.o_norec.isChecked():
            args.append("--no-recommends")
        args.append("--allow-downgrade" if self.o_down.isChecked() else "--no-allow-downgrade")
        if self.o_from.currentData():
            args += ["--from", self.o_from.currentData()]
        return args

    def _dup_steps(self):
        """(simulation, real run): the commands the two buttons run, made in one place so that what the window
        shows is exactly what runs."""
        sim = Step("zypper", ["--xmlout", "-n", *self.dup_args(), "--dry-run"], lang="C", capture=True)
        lic = ["--auto-agree-with-licenses"] if self.ctx.settings["apply_accept_licenses"] else []
        run = Step("zypper", ["-n", "-v", *self.dup_args(), *lic])
        return sim, run

    def _show_dup_cmd(self, *_args):
        sim, run = self._dup_steps()
        self.dup_cmd.setText(_("Simulate: {command}").format(command=sim.display()) + "\n"
                             + _("Run: {command}").format(command=run.display()))

    def simulate_dup(self):
        if self.ctx.runner.busy:
            self.sim_lbl.setText(_("Wait until the current command finishes."))
            return
        st, _run = self._dup_steps()
        if self.ctx.run([st], _("Simulation of zypper dup"), on_done=lambda ok: self._simulated(st)):
            self.sim_lbl.setText(_("Simulating…"))

    def _simulated(self, st):
        if st.code in (126, 127):
            self.sim_lbl.setText(_("Not simulated: the administrator password was not given."))
            return
        self.sim_tree.clear()
        res = parse_dry_run(st.output, source="zypper dup")
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
        items = [p for p in res.get("problem_items") or [] if any(x.get("possible") for x in p.get("solutions") or [])]
        if items:
            self.sim_lbl.setText(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:3]))
            QTimer.singleShot(0, lambda: self._resolve(items))
        elif res["problems"]:
            self.sim_lbl.setText(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:5]))
        elif not counts:
            self.sim_lbl.setText(_("Nothing to do: the system is up to date."))
        else:
            self.sim_lbl.setText(_("Nothing has been changed.") + "  " + "  ·  ".join(counts))

    def _resolve(self, items):
        """The dup cannot go on: choose a solution; it runs at once (a lock, a removal), then simulate again."""
        try:
            from .problems import ProblemDialog
        except ImportError:
            return
        dlg = ProblemDialog(items, self.dup_dialog, apply_label=_("Do it now and simulate again"),
                            note=_("The dup is only simulated: what you choose here runs at once (with the "
                                   "password), and then the dup is simulated again."), now=True)
        accepted = dlg.exec()
        chosen = dlg.chosen()
        dlg.deleteLater()
        if accepted:
            self.ctx.apply_solutions(chosen, then=self.simulate_dup, now=True)

    def run_dup(self):
        _sim, st = self._dup_steps()
        # a real dup as root: Enter answers No
        if ask(self.dup_dialog, _("zypper dup"), _("Run this command now? It upgrades the whole system.") + "\n\n"
               + st.display(), default_no=True):
            self.ctx.run([st], _("zypper dup"), on_done=self._dup_done)

    def _dup_done(self, ok):
        # the simulation shown is old now
        self.sim_tree.clear()
        self.sim_lbl.setText(_("zypper dup finished.") if ok else _("zypper dup stopped with errors: look at the "
                                                                    "terminal."))
        self._after_change()
