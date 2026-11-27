# SPDX-License-Identifier: GPL-3.0-or-later
"""Small shared widgets and helpers."""

import html

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QMessageBox, QPushButton, QSplitter,
                               QToolButton, QVBoxLayout, QWidget)


def icon(*names):
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QIcon()


def rich_tip(title, summary="", description="", notes=()):
    """A tooltip for a program or package: its name in bold and what it is, its description, then notes in
    italics. Text from outside is escaped; rich text, so that Qt wraps it at a width that reads well."""
    parts = ["<b>" + html.escape(title) + "</b>" + (" — " + html.escape(summary) if summary else "")]
    if description:
        parts.append(html.escape(description).replace("\n", "<br>"))
    parts += ["<i>" + html.escape(n) + "</i>" for n in notes if n]
    return "<qt>" + "<br><br>".join(parts) + "</qt>"


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


class FlowLayout(QLayout):
    """Widgets in a row that wraps onto the next line when there is no room (chips, the buttons of a card):
    the window can be narrow, and German and French words are long. Hidden widgets take no room."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        # the widest widget: narrower than that, it would be cut
        size = QSize()
        for it in self._items:
            if not it.isEmpty():
                size = size.expandedTo(it.sizeHint())
        return size

    def _arrange(self, rect, apply):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            if it.isEmpty():
                continue
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._spacing, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y()


class RoomSplitter(QSplitter):
    """A list | card split that takes the height that is left. A page sits in a scroll area that asks it how
    tall it would like to be: if the splitter said "as tall as the whole list", the page would scroll and the
    buttons under the list would go below the window (a 1100×700 window with the terminal open). The list
    scrolls by itself instead."""

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(s.width(), self.minimumSizeHint().height())


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
    box.deleteLater()


def info_box(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok, parent)
    plain(box).exec()
    box.deleteLater()


def ask(parent, title, text, default_no=False):
    """A yes/no question. default_no=True for every question that starts a real change as root (or throws
    something away): then Enter answers No, and only a click (or Alt+Y) on Yes goes on."""
    box = QMessageBox(QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    if default_no:
        box.setDefaultButton(QMessageBox.StandardButton.No)
    box.setEscapeButton(QMessageBox.StandardButton.No)
    try:
        return plain(box).exec() == QMessageBox.StandardButton.Yes
    finally:
        box.deleteLater()


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
