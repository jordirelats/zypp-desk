# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove a package, and if you want also what only it needed (zypper remove --clean-deps)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget)

from .. import cleandeps
from ..i18n import _
from ..simulation import human_size
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
        run_async(lambda n: (cleandeps.size_of(n), cleandeps.only_needed_by(n)), d["name"], on_done=self._loaded)

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
            self.own, self.deps = res
            if self.own:
                self.sub.setText(f"{self.sub.text()} · {human_size(self.own)}")
            self.list_lbl.setText("\n".join(f"{n}    {human_size(s)}" for n, s in self.deps) if self.deps else
                                  _("Nothing: what it uses is also used by other packages, or you installed it "
                                    "yourself."))
        self._update()

    def _action(self):
        return Action("remove", self.target, package=self.d["name"], clean_deps=self.clean.isChecked())

    def _update(self):
        clean = self.clean.isChecked()
        q = TxQueue(getattr(self.ctx.queue, "settings", None) or {})  # the queue's own command for this removal
        q.add(self._action())
        self.cmd.setText("\n".join(st.display() for st in q.steps()))
        if self.deps is None:
            self.total.setText("")
            return
        freed = self.own + (sum(s for _n, s in self.deps) if clean else 0)
        self.total.setText(_("In all, {size} are freed.").format(size=human_size(freed)) if freed else "")

    def _queue(self, simulate=False):
        self.ctx.queue.add(self._action())
        self.accept()
        if simulate:
            self.ctx.simulate_queue()
