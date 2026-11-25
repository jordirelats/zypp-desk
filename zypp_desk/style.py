# SPDX-License-Identifier: GPL-3.0-or-later
"""The look of Zypp Desk: the Plasma theme, plus the green of the icon and softer, rounder shapes.

Colours that come from the palette (palette(...)) follow the light or dark theme; only the brand
green and the status colours are fixed.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

GREEN = "#27ae60"
GREEN_DARK = "#1c9450"
BLUE = "#1d99f3"
ORANGE = "#f67400"
RED = "#da4453"
PURPLE = "#9b59b6"

STYLESHEET = f"""
/* sidebar */
QListWidget#sidebar {{ background: transparent; border: none; outline: 0; }}
QListWidget#sidebar::item {{ padding: 7px 8px; margin: 1px 2px; border-radius: 8px; }}
QListWidget#sidebar::item:hover {{ background: rgba(39, 174, 96, 0.10); }}
QListWidget#sidebar::item:selected {{ background: rgba(39, 174, 96, 0.22); color: palette(text); }}
QListWidget#sidebar::item:disabled {{ color: palette(placeholder-text); padding-top: 12px; }}

/* the main action of each place */
QPushButton[primary="true"] {{ background: {GREEN}; color: white; border: 1px solid {GREEN_DARK};
  border-radius: 7px; padding: 6px 16px; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: #2ec36c; }}
QPushButton[primary="true"]:pressed {{ background: {GREEN_DARK}; }}
QPushButton[primary="true"]:disabled {{ background: rgba(39, 174, 96, 0.35); border-color: transparent; }}

/* cards */
QFrame#card {{ border: 1px solid rgba(127, 140, 141, 0.35); border-radius: 12px; background: palette(base); }}
QFrame#tile {{ border: 1px solid rgba(127, 140, 141, 0.30); border-radius: 14px; background: palette(base); }}
QFrame#tile:hover {{ border-color: {GREEN}; }}

/* the queue bar */
QFrame#queuebar {{ border: none; border-top: 1px solid rgba(127, 140, 141, 0.35); background: palette(window); }}

/* page header */
QWidget#pageheader {{ border-bottom: 1px solid rgba(127, 140, 141, 0.25); }}

/* big search field on the home page */
QLineEdit#bigsearch {{ border: 2px solid rgba(39, 174, 96, 0.55); border-radius: 22px; padding: 9px 18px;
  font-size: 15px; background: palette(base); }}
QLineEdit#bigsearch:focus {{ border-color: {GREEN}; }}

/* tables: a little more air */
QTreeView::item {{ padding: 3px 2px; }}
"""


def dot(color, canvas=22, diameter=10):
    """A coloured dot in the middle of an icon-sized square, used as the status icon in lists."""
    pm = QPixmap(canvas * 2, canvas * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    off = canvas - diameter
    p.drawEllipse(off, off, diameter * 2, diameter * 2)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def apply(app):
    app.setStyleSheet(STYLESHEET)
