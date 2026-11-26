# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove a package, and if you want also what only it needed (zypper remove --clean-deps).

A program that has a bridge package (bridge.py: Zypp Desk made it so that a program made for Fedora could be
installed) offers to remove its bridge too; removing a bridge that its program still needs says that the
program goes with it.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget)

from .. import bridge, cleandeps
from ..i18n import _, ngettext
from ..simulation import human_size
from ..style import ORANGE
from ..tasks import run_async
from ..txqueue import Action, TxQueue
from ..ui import button, heading, mono, muted


class RemoveDialog(QDialog):
    def __init__(self, ctx, d, qicon, target, parent=None):
        super().__init__(parent)
        self.ctx, self.d, self.target = ctx, d, target
        self._closed = False  # the first look (a thread) may answer after the window is gone
        self.setWindowTitle(_("Remove {name}").format(name=d["name"]))
        self.resize(560, 480)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        top = QHBoxLayout()
        pic = QLabel(self)
        pic.setPixmap(qicon.pixmap(48, 48))
        top.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        title = heading(_("Remove {name}").format(name=d["name"]), 1.3, self)
        title.setTextFormat(Qt.TextFormat.PlainText)
        tv.addWidget(title)
        inst = d.get("installed") or {}
        self.sub = muted(f"{d['name']} {inst.get('edition', '')}", self)
        self.sub.setTextFormat(Qt.TextFormat.PlainText)
        tv.addWidget(self.sub)
        top.addLayout(tv, 1)
        lay.addLayout(top)

        box = QFrame(self)
        box.setObjectName("card")
        bv = QVBoxLayout(box)
        self.clean = QCheckBox(_("Also remove what only {name} needed").format(name=d["name"]), box)
        f = self.clean.font()
        f.setBold(True)
        self.clean.setFont(f)
        self.clean.setChecked(True)
        self.clean.toggled.connect(self._update)
        bv.addWidget(self.clean)
        self.list_lbl = QLabel(_("Looking at what it uses…"), box)
        self.list_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.list_lbl.setWordWrap(True)
        sc = QScrollArea(box)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(24, 0, 0, 0)
        iv.addWidget(self.list_lbl)
        iv.addStretch(1)
        sc.setWidget(inner)
        bv.addWidget(sc, 1)
        lay.addWidget(box, 1)

        # its bridge package (shown once it is known that there is one)
        self.bridges, self.bridge_users = [], []
        self.with_bridge = QCheckBox(self)
        self.with_bridge.setChecked(True)
        self.with_bridge.toggled.connect(self._update)
        self.with_bridge.hide()
        self.bridge_note = QLabel(self)
        self.bridge_note.setTextFormat(Qt.TextFormat.PlainText)
        self.bridge_note.setWordWrap(True)
        self.bridge_note.setStyleSheet("color: palette(placeholder-text);")
        self.bridge_note.hide()
        lay.addWidget(self.with_bridge)
        lay.addWidget(self.bridge_note)

        self.total = QLabel(self)
        lay.addWidget(self.total)
        self.cmd = QLabel(self)
        self.cmd.setFont(mono())
        self.cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cmd.setWordWrap(True)
        self.cmd.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(self.cmd)
        note = muted(_("Only packages that came in as dependencies are listed: the ones you installed yourself "
                       "always stay. This is a first look at what {name} uses directly; Simulate gives the exact "
                       "list, which can be longer.").format(name=d["name"]), self)
        note.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(button(_("Cancel"), None, self.reject))
        btns.addWidget(button(_("Simulate"), "media-playback-start", lambda: self._queue(simulate=True)))
        ok = button(_("Add to the queue"), "list-add", lambda: self._queue(), primary=True)
        ok.setDefault(True)
        btns.addWidget(ok)
        lay.addLayout(btns)

        self.deps, self.own = None, 0
        self._update()
        run_async(self._look, d["name"], on_done=self._loaded)

    @staticmethod
    def _look(name):
        """(size, what only it needed, its bridges, who else needs them, the bridge itself when it is one), in a
        thread."""
        bridges = [b for b in bridge.bridges_of(name) if b["name"] != name]
        users = sorted({u for b in bridges for u in bridge.users_of(b)})
        leaving = [b["name"] for b in bridges] if not users else []
        own_bridge = None
        if name.startswith(bridge.PREFIX):
            mine = [b for b in bridge.installed_bridges() if b["name"] == name]
            own_bridge = bridge.check_bridges(mine)[0] if mine else None
        return (cleandeps.size_of(name), cleandeps.only_needed_by(name, also=leaving), bridges, users,
                own_bridge)

    def done(self, result):
        self._closed = True
        super().done(result)

    def _loaded(self, res, err):
        if self._closed:
            return
        if err or res is None:
            self.list_lbl.setText(_("Could not look ({error}). Simulate shows what would be removed.").format(
                error=err))
            self.deps = []
        else:
            self.own, self.deps, self.bridges, self.bridge_users, own_bridge = res
            self._show_bridges(own_bridge)
            if self.own:
                self.sub.setText(f"{self.sub.text()} · {human_size(self.own)}")
            self.list_lbl.setText("\n".join(f"{n}    {human_size(s)}" for n, s in self.deps) if self.deps else
                                  _("Nothing: what it uses is also used by other packages, or you installed it "
                                    "yourself."))
        self._update()

    def _show_bridges(self, own_bridge):
        if self.bridges:
            names = ", ".join(b["name"] for b in self.bridges)
            self.with_bridge.setText(ngettext("Also remove its bridge package, {names}",
                                              "Also remove its bridge packages, {names}", len(self.bridges))
                                     .format(names=names))
            if self.bridge_users:
                # another program needs the same names: removing the bridge would take it away too
                self.with_bridge.setChecked(False)
                self.with_bridge.setEnabled(False)
                note = _("{others} also use it, so it stays: removing it would remove them too.").format(
                    others=", ".join(self.bridge_users))
            else:
                note = _("Zypp Desk made it so that {name}, a program made for Fedora, could be installed. Nothing "
                         "else needs it.").format(name=self.d["name"])
            self.bridge_note.setText(note)
            self.with_bridge.show()
            self.bridge_note.show()
        elif own_bridge is not None and own_bridge.get("state") == "needed":
            self.bridge_note.setText(_("This is the bridge package of {program}, which still needs it: removing it "
                                       "removes {program} too.").format(program=own_bridge["program"]))
            self.bridge_note.setStyleSheet(f"color: {ORANGE};")
            self.bridge_note.show()
        self._update()

    def _action(self):
        return Action("remove", self.target, package=self.d["name"], clean_deps=self.clean.isChecked())

    def _actions(self):
        out = [self._action()]
        if self.bridges and self.with_bridge.isEnabled() and self.with_bridge.isChecked():
            out += [Action("remove", b["name"], package=b["name"], clean_deps=self.clean.isChecked())
                    for b in self.bridges]
        return out

    def _update(self):
        clean = self.clean.isChecked()
        q = TxQueue(getattr(self.ctx.queue, "settings", None) or {})  # the queue's own command for this removal
        for a in self._actions():
            q.add(a)
        self.cmd.setText("\n".join(st.display() for st in q.steps()))
        if self.deps is None:
            self.total.setText("")
            return
        freed = self.own + (sum(s for _n, s in self.deps) if clean else 0)
        self.total.setText(_("In all, {size} are freed.").format(size=human_size(freed)) if freed else "")

    def _queue(self, simulate=False):
        with self.ctx.queue.batch():
            for a in self._actions():
                self.ctx.queue.add(a)
        self.accept()
        if simulate:
            self.ctx.simulate_queue()
