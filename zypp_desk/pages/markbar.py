# SPDX-License-Identifier: GPL-3.0-or-later
"""Marking several programs or packages and doing the same with all of them at once (installing several in one
go, for example).

MarkBar is the band under a list that appears when something is ticked: how many, what they are, and buttons.
paint_check draws the same box the Updates list uses, for lists that paint their own rows.

Also the small pieces the button rules (botons/NORMES.md) use on several pages: queue_label, the "To the queue:"
in front of buttons that only add to the queue (rule 3); flash_queued, "In the queue ✓" on such a button for a
moment (rule 6); amp, a name made safe for a button label.
"""

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ..i18n import _
from ..style import GREEN, ORANGE
from ..ui import button


def amp(text):
    """A name inside a button label: '&' would underline the next letter (a mnemonic) instead of showing."""
    return (text or "").replace("&", "&&")


def queue_label(parent=None):
    """'To the queue:' in front of buttons that only add to the queue (the package card has the same)."""
    lbl = QLabel(_("To the queue:"), parent)
    lbl.setObjectName("toqueue")
    lbl.setStyleSheet(f"color: {ORANGE}; font-weight: bold;")
    lbl.setToolTip(_("These buttons only add to the queue: nothing changes until you apply the queue."))
    return lbl


def flash_queued(btn, restore=None, ms=1400):
    """The button says for a moment that its change went to the queue, then shows its label again (restore()
    computes it again: the selection may have changed meanwhile)."""
    text = btn.text()
    btn.setText(_("In the queue ✓"))
    btn.setEnabled(False)
    btn.setProperty("flashing", True)

    def back():
        btn.setProperty("flashing", False)
        if restore:
            restore()
        else:
            btn.setText(text)
            btn.setEnabled(True)
    QTimer.singleShot(ms, btn, back)


def is_checked(value):
    """A check state as PySide hands it back (the enum, or its number)."""
    return getattr(value, "value", value) == Qt.CheckState.Checked.value


def check_rect(r, left=10):
    """Where the box of a row goes: on the left, in the middle of the row."""
    return QRect(r.left() + left, r.center().y() - 8, 16, 16)


def paint_check(p, rect, on, palette):
    """A green box with a tick, or an empty one (as in Updates)."""
    cr = QRectF(rect)
    p.save()
    p.setRenderHint(p.RenderHint.Antialiasing)
    if on:
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
        p.setPen(QPen(palette.placeholderText().color(), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(cr.adjusted(0.7, 0.7, -0.7, -0.7), 4, 4)
    p.restore()


class MarkBar(QFrame):
    """'3 marked · 2 to install, 1 you have' and buttons. set_state(count, detail, actions): actions is a list of
    (key, text, enabled, primary) or (key, text, enabled, primary, tooltip); clicking calls on_action(key).
    Hidden while nothing is marked. to_queue=True: the buttons only add to the queue, and "To the queue:" says so
    in front of them (they are then never green, rule 2)."""

    def __init__(self, on_action, on_clear, parent=None, to_queue=False):
        super().__init__(parent)
        self.setObjectName("markbar")
        self.setStyleSheet("QFrame#markbar { background: rgba(39, 174, 96, 0.10); border: 1px solid "
                           "rgba(39, 174, 96, 0.35); border-radius: 10px; }")
        self._on_action = on_action
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 9)
        v.setSpacing(6)
        top = QHBoxLayout()
        self.count = QLabel(self)
        f = self.count.font()
        f.setBold(True)
        self.count.setFont(f)
        self.detail = QLabel(self)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setStyleSheet("color: palette(placeholder-text);")
        top.addWidget(self.count)
        top.addWidget(self.detail, 1)
        v.addLayout(top)
        self.row = QHBoxLayout()
        self.clear_btn = button(_("Unmark all"), "edit-clear", on_clear)
        self.clear_btn.setToolTip(_("Untick every row. Nothing changes in the queue."))
        self.row.addWidget(self.clear_btn)
        self.row.addStretch(1)
        self.to_queue = queue_label(self) if to_queue else None
        if self.to_queue:
            self.row.addWidget(self.to_queue)
        v.addLayout(self.row)
        self._buttons = []
        self.setVisible(False)

    def set_state(self, count, count_text, detail, actions):
        self.setVisible(count > 0)
        if not count:
            return
        self.count.setText(count_text)
        self.detail.setText(detail)
        for b in self._buttons:
            self.row.removeWidget(b)
            b.deleteLater()
        self._buttons = []
        for key, text, enabled, primary, *tip in actions:
            b = button(text, None, lambda _c=False, k=key: self._on_action(k),
                       primary=primary and not self.to_queue)
            b.setObjectName(f"mark_{key}")
            b.setEnabled(enabled)
            b.setAutoDefault(False)
            if tip and tip[0]:
                b.setToolTip(tip[0])
            self.row.addWidget(b)
            self._buttons.append(b)
        if self.to_queue:
            self.to_queue.setVisible(bool(self._buttons))
