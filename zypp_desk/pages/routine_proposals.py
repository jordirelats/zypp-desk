# SPDX-License-Identifier: GPL-3.0-or-later
"""Proposed routines (Jordi, 26/11/2026: "podem proposar rutines?"; mockup maquetes/rutines-v1-A-proposades.png).

A few ready routines, each with what it does, why, its steps and the line it makes in a terminal. The first one is
made for this computer from what it has (routine.machine_facts: Flatpak apps, fwupd, free space). "Use this one"
loads it into the routine editor; nothing is saved until the editor's "Save".
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QRadioButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import routine
from ..i18n import _
from ..style import BLUE, GREEN
from ..tasks import run_async
from ..ui import FlowLayout, button, heading, hline, mono, muted


def _plain(text, *, bold=False, small=False, color=None, wrap=True):
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(wrap)
    f = lbl.font()
    if bold:
        f.setBold(True)
    if small:
        f.setPointSizeF(f.pointSizeF() * 0.92)
    lbl.setFont(f)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    return lbl


def _step_chip(n, title):
    w = QFrame()
    w.setObjectName("stepchip")
    w.setStyleSheet("QFrame#stepchip { border: 1px solid rgba(127, 140, 141, 0.40); border-radius: 12px; }")
    h = QHBoxLayout(w)
    h.setContentsMargins(3, 2, 10, 2)
    h.setSpacing(5)
    num = QLabel(str(n))
    num.setAlignment(Qt.AlignmentFlag.AlignCenter)
    num.setFixedSize(18, 18)
    num.setStyleSheet(f"background: {BLUE}; color: white; border-radius: 9px; font-size: 11px; font-weight: 700;")
    h.addWidget(num)
    h.addWidget(_plain(title, small=True, wrap=False))
    return w


class _Card(QFrame):
    def __init__(self, prop, radio, parent=None):
        super().__init__(parent)
        self.prop = prop
        self.radio = radio
        self.setObjectName("propcard")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 14, 10)
        v.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(radio)
        radio.setText(prop["title"])
        f = radio.font()
        f.setBold(True)
        radio.setFont(f)
        tag = {"suggested": _("Proposed for this computer"), "suggested-yours": _("Proposed for this computer · "
                                                                                    "the one you have"),
               "yours": _("The one you have")}.get(prop["tag"])
        if tag:
            pill = QLabel(tag)
            color = GREEN if prop["tag"].startswith("suggested") else "#7f8c8d"
            pill.setStyleSheet(f"background: {color}; color: white; border-radius: 9px; padding: 1px 8px; "
                               "font-size: 11px;")
            head.addWidget(pill)
        head.addStretch(1)
        v.addLayout(head)
        v.addWidget(_plain(prop["what"], small=True, color="palette(placeholder-text)"))
        self.why = QWidget()
        wv = QVBoxLayout(self.why)
        wv.setContentsMargins(0, 2, 0, 2)
        wv.setSpacing(1)
        for w in prop["why"]:
            wv.addWidget(_plain("✓ " + w, small=True, color="#2980b9"))
        v.addWidget(self.why)
        chips = QWidget()
        flow = FlowLayout(chips, spacing=4)
        try:
            items = routine.to_items(prop["text"])
        except routine.RoutineError:
            items = []
        for i, it in enumerate(items, 1):
            if i > 1:
                flow.addWidget(_plain(_("only if it worked →") if it.get("cond") == "success" else "→", small=True,
                                      color="palette(placeholder-text)", wrap=False))
            flow.addWidget(_step_chip(i, routine.kind_title(it["kind"]) if it["kind"] != "custom"
                                      else it.get("raw") or ""))
        v.addWidget(chips)
        self.cmd = _plain(prop["text"], small=True, color="palette(placeholder-text)")
        self.cmd.setFont(mono())
        self.cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.cmd)
        radio.toggled.connect(self._mark)
        self._mark(radio.isChecked())

    def _mark(self, on):
        self.setStyleSheet("QFrame#propcard { border: 2px solid %s; border-radius: 12px; background: palette(base); }"
                           % GREEN if on else
                           "QFrame#propcard { border: 1px solid rgba(127, 140, 141, 0.35); border-radius: 12px; "
                           "background: palette(base); }")
        self.why.setVisible(on and bool(self.prop["why"]))
        self.cmd.setVisible(on)

    def mousePressEvent(self, ev):
        self.radio.setChecked(True)
        super().mousePressEvent(ev)


class ProposalsDialog(QDialog):
    """exec() returns Accepted with self.chosen = the routine text to load."""

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Proposed routines"))
        self.chosen = None
        self.current = current
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(heading(_("Proposed routines"), 1.3))
        lay.addWidget(muted(_("Choose one, and change it in the editor afterwards if you like. Nothing is saved "
                              "until you press “Save”.")))
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body = QWidget()
        self.bv = QVBoxLayout(self.body)
        self.bv.setContentsMargins(0, 0, 0, 0)
        self.bv.setSpacing(10)
        self.bv.addWidget(muted(_("Looking at this computer…")))
        self.bv.addStretch(1)
        self.scroll.setWidget(self.body)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(hline())
        foot = QHBoxLayout()
        foot.addStretch(1)
        cancel = button(_("Cancel"), None, self.reject)
        cancel.setDefault(True)
        self.use = button(_("Use this one"), None, self.accept_choice, primary=True)
        self.use.setAutoDefault(False)
        self.use.setEnabled(False)
        foot.addWidget(cancel)
        foot.addWidget(self.use)
        lay.addLayout(foot)
        self.group = QButtonGroup(self)
        self.cards = []
        self.resize(820, 720)
        run_async(routine.machine_facts, on_done=self._facts)

    def _facts(self, facts, err):
        while self.bv.count():
            w = self.bv.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for i, prop in enumerate(routine.proposals(self.current, facts or {})):
            rb = QRadioButton()
            self.group.addButton(rb)
            card = _Card(prop, rb, self.body)
            self.cards.append(card)
            self.bv.addWidget(card)
            if i == 0:
                rb.setChecked(True)
        self.bv.addStretch(1)
        self.use.setEnabled(bool(self.cards))

    def accept_choice(self):
        card = next((c for c in self.cards if c.radio.isChecked()), None)
        if card is None:
            return
        self.chosen = card.prop["text"]
        self.accept()
