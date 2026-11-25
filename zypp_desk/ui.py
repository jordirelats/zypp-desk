# SPDX-License-Identifier: GPL-3.0-or-later
"""Small shared widgets and helpers."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QToolButton, QVBoxLayout,
                               QWidget)


def icon(*names):
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QIcon()


def mono():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def heading(text, scale=1.15, parent=None):
    lbl = QLabel(text, parent)
    f = lbl.font()
    f.setBold(True)
    f.setPointSizeF(f.pointSizeF() * scale)
    lbl.setFont(f)
    return lbl


def muted(text, parent=None, wrap=True):
    lbl = QLabel(text, parent)
    lbl.setWordWrap(wrap)
    lbl.setForegroundRole(lbl.foregroundRole())
    lbl.setStyleSheet("color: palette(placeholder-text);")
    return lbl


def button(text, icon_name=None, slot=None, primary=False, parent=None):
    b = QPushButton(text, parent)
    if icon_name:
        b.setIcon(icon(icon_name))
    if slot:
        b.clicked.connect(slot)
    if primary:
        b.setProperty("primary", True)
    return b


def tool(text, icon_name=None, slot=None, parent=None):
    b = QToolButton(parent)
    b.setText(text)
    if icon_name:
        b.setIcon(icon(icon_name))
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setAutoRaise(True)
    if slot:
        b.clicked.connect(slot)
    return b


class Chip(QToolButton):
    """A small checkable rounded button, used for search options."""

    def __init__(self, text, checked=False, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setProperty("chip", True)  # the look is in style.py (QToolButton[chip="true"])


def hline():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


def plain(box):
    """Show every text of a message box as plain text. Qt guesses "rich text" when a text looks like HTML,
    so a key name or a package name from the network could otherwise draw anything (a fake fingerprint)."""
    for lbl in box.findChildren(QLabel):
        lbl.setTextFormat(Qt.TextFormat.PlainText)
    return box


def error_box(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, parent)
    plain(box).exec()


def info_box(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok, parent)
    plain(box).exec()


def ask(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    return plain(box).exec() == QMessageBox.StandardButton.Yes


class Page(QWidget):
    """A section of the sidebar. 'ctx' is the main window (settings, queue, run…)."""

    key = ""
    title = ""
    icon_name = ""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.loaded = False

    def header_actions(self):
        return []

    def activated(self):
        if not self.loaded:
            self.loaded = True
            self.reload()

    def reload(self):
        pass


class Card(QFrame):
    def __init__(self, title=None, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 12, 16, 14)
        if title:
            self.lay.addWidget(heading(title, 1.0))


def row(*widgets, stretch_at=None):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for i, x in enumerate(widgets):
        if x is None:
            h.addStretch(1)
        else:
            h.addWidget(x, 1 if i == stretch_at else 0)
    return w
