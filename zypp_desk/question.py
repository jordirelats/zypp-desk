# SPDX-License-Identifier: GPL-3.0-or-later
"""The question before a change to the computer, in words for someone who does not know zypper.

One pattern for every question that changes the system (Jordi, 26/11/2026: "Run these commands?" was not clear
for a person who does not know): the title says what will be done; under it, each change in words with an icon;
"What it means for you"; a warning when it can hurt; the exact commands underneath, small, as information; the
password note; and buttons that say what they do. The mockups are preguntes-v3-*.png.

Enter never starts a change: the default button is the first one that is not primary (usually "Not yet").
Every text is plain text: package and repository names come from the network.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QIcon
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from .i18n import _
from .style import BLUE, GREEN, ORANGE, RED
from .ui import icon as theme_icon
from .ui import mono

GREY = "#7f8c8d"


def _qicon(ic):
    if isinstance(ic, QIcon):
        return ic
    return theme_icon(*str(ic).split("|")) if ic else QIcon()


def label(text, *, bold=False, small=False, muted=False, color=None, wrap=True, selectable=False, font=None):
    """A plain-text label (never rich text: names come from the network)."""
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(wrap)
    if font is not None:
        lbl.setFont(font)
    f = lbl.font()
    if bold:
        f.setBold(True)
    if small:
        f.setPointSizeF(f.pointSizeF() * 0.92)
    lbl.setFont(f)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    elif muted:
        lbl.setStyleSheet("color: palette(placeholder-text);")
    if selectable:
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


def _frame(name, color=None, alpha_bg=0.08, alpha_border=0.40, radius=10, bg=None):
    f = QFrame()
    f.setObjectName(name)
    if color:
        c = QColor(color)
        rgb = f"{c.red()}, {c.green()}, {c.blue()}"
        f.setStyleSheet(f"QFrame#{name} {{ background: rgba({rgb}, {alpha_bg}); border: 1px solid rgba({rgb}, "
                        f"{alpha_border}); border-radius: {radius}px; }}")
    else:
        f.setStyleSheet(f"QFrame#{name} {{ background: {bg or 'palette(base)'}; border: 1px solid rgba(127, 140, "
                        f"141, 0.35); border-radius: {radius}px; }}")
    return f


class _More(QLabel):
    """'▸ Show them' under an item: a click shows the full list (names), a second one hides it."""

    def __init__(self, text_closed, text_open, target, parent=None):
        super().__init__(text_closed, parent)
        self._closed, self._open, self._target = text_closed, text_open, target
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet(f"color: {BLUE};")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def _toggle(self):
        on = not self._target.isVisible()
        self._target.setVisible(on)
        self.setText(self._open if on else self._closed)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._toggle()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._toggle()
            return
        super().keyPressEvent(ev)


def item(ic, title, detail="", more=None, right=None):
    """One change in words: an icon, a title in bold, a line that explains it. more: a list of names shown under
    '▸ Show them' (packages of an update…). right: a widget on the right (a button)."""
    f = _frame("qitem")
    h = QHBoxLayout(f)
    h.setContentsMargins(12, 9, 12, 9)
    h.setSpacing(12)
    if ic is not None:
        pic = QLabel()
        pic.setPixmap(_qicon(ic).pixmap(32, 32))
        pic.setFixedSize(32, 32)
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
    v = QVBoxLayout()
    v.setSpacing(2)
    v.addWidget(label(title, bold=True))
    if detail:
        v.addWidget(label(detail, small=True, muted=True))
    if more:
        names = label("\n".join(more) if len(more) <= 400 else "\n".join(list(more[:400]) + ["…"]),
                      small=True, selectable=True, font=mono())
        names.setVisible(False)
        v.addWidget(_More(_("▸ Show them ({n})").format(n=len(more)), _("▾ Hide them"), names))
        v.addWidget(names)
    h.addLayout(v, 1)
    if right is not None:
        h.addWidget(right, 0, Qt.AlignmentFlag.AlignVCenter)
    return f


def box(kind, text, title=None):
    """A coloured note: kind 'info' (what it means for you), 'warn' (careful), 'error' or 'ok'."""
    color = {"info": BLUE, "warn": ORANGE, "error": RED, "ok": GREEN}.get(kind, BLUE)
    f = _frame("qbox_" + kind, color)
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 8, 12, 8)
    v.setSpacing(2)
    if title:
        v.addWidget(label(title, bold=True))
    v.addWidget(label(text, small=True))
    return f


def reasons(title, rows, tip=None):
    """A box of reasons with coloured dots ('Is it worth waiting?'): rows are (color, title, text)."""
    f = _frame("qreasons")
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 9, 12, 10)
    v.setSpacing(4)
    if title:
        v.addWidget(label(title, bold=True))
    for color, t, d in rows:
        h = QHBoxLayout()
        h.setSpacing(10)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        h.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        tv.setSpacing(0)
        tv.addWidget(label(t, bold=True))
        if d:
            tv.addWidget(label(d, small=True, muted=True))
        h.addLayout(tv, 1)
        v.addLayout(h)
    if tip:
        v.addWidget(label(tip, small=True, color="#2980b9"))
    return f


class Bar(QWidget):
    """A disk bar: the part in use (grey) and the part a change would take (orange), out of the whole."""

    def __init__(self, used, extra, total, parent=None):
        super().__init__(parent)
        self.used, self.extra, self.total = used, extra, max(total, 1)
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.setClipPath(path)
        p.fillRect(r, QColor(127, 140, 141, 40))
        w = r.width()
        u = int(w * min(1.0, self.used / self.total))
        e = int(w * min(1.0, (self.used + self.extra) / self.total)) - u
        p.fillRect(0, 0, u, r.height(), QColor("#95a5a6"))
        p.fillRect(u, 0, max(0, e), r.height(), QColor(ORANGE if self.used + self.extra < self.total else RED))
        p.end()


def commands(cmds, title=None):
    """The exact commands, small and grey, as information. cmds: a list of strings (numbered when more than one),
    or a list of (caption, [commands]) for a question whose buttons run different commands."""
    f = QFrame()
    f.setObjectName("qcmds")
    f.setStyleSheet("QFrame#qcmds { background: rgba(127, 140, 141, 0.10); border: none; border-radius: 10px; }")
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 8, 12, 9)
    v.setSpacing(2)
    grouped = bool(cmds) and isinstance(cmds[0], (tuple, list))
    if title is None:
        if grouped:
            title = _("The command each choice uses:")
        elif len(cmds) > 1:
            title = _("The commands it will use, in this order:")
        else:
            title = _("The command it will use:")
    v.addWidget(label(title, small=True, muted=True))

    def add_lines(lines):
        for i, c in enumerate(lines, 1):
            text = f"{i}. {c}" if len(lines) > 1 else c
            lbl = label(text, small=True, muted=True, selectable=True, font=mono())
            v.addWidget(lbl)

    if grouped:
        for caption, lines in cmds:
            cap = label(caption, small=True, muted=True)
            cap.setContentsMargins(0, 4, 0, 0)
            v.addWidget(cap)
            add_lines(list(lines))
    else:
        add_lines(list(cmds))
    return f


class Question(QDialog):
    """The question. buttons: a list of (key, text, primary); exec() returns the key of the button pressed (None
    when the window is closed). The default (Enter) is the first button that is not primary."""

    def __init__(self, parent, title, *, window_title=None, icon_name="system-software-update", lead=None,
                 items=(), means=None, warning=None, extra=(), commands_=None, commands_title=None, password=True,
                 buttons=None, width=660):
        super().__init__(parent)
        self.setWindowTitle(window_title or title)
        self.setModal(True)
        self.result_key = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(14)
        pic = QLabel()
        pic.setPixmap(_qicon(icon_name).pixmap(44, 44))
        head.addWidget(pic, 0, Qt.AlignmentFlag.AlignVCenter)
        t = label(title, bold=True)
        tf = t.font()
        tf.setPointSizeF(tf.pointSizeF() * 1.35)
        t.setFont(tf)
        head.addWidget(t, 1)
        outer.addLayout(head)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(10)
        if lead:
            bv.addWidget(label(lead))
        for it in items:
            bv.addWidget(it if isinstance(it, QWidget) else item(*it))
        for w in extra:
            bv.addWidget(w)
        if means:
            bv.addWidget(_means(means))
        if warning:
            bv.addWidget(box("warn", "⚠ " + warning))
        if commands_:
            bv.addWidget(commands(commands_, commands_title))
        bv.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        self._scroll, self._body = scroll, body
        outer.addWidget(scroll, 1)

        if password:
            ph = QHBoxLayout()
            ph.setSpacing(8)
            lock = QLabel()
            lock.setPixmap(theme_icon("dialog-password", "lock", "object-locked").pixmap(18, 18))
            ph.addWidget(lock)
            ph.addWidget(label(_("It will ask for the administrator password."), small=True, muted=True), 1)
            outer.addLayout(ph)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)
        bh = QHBoxLayout()
        bh.addStretch(1)
        buttons = buttons or [("no", _("Not yet"), False), ("yes", _("Yes"), True)]
        default_set = False
        self.buttons = {}
        for key, text, primary in buttons:
            b = QPushButton(text)
            if primary:
                b.setProperty("primary", True)
                b.setAutoDefault(False)  # Enter never starts a change
            elif not default_set:
                b.setDefault(True)
                default_set = True
            else:
                b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, k=key: self._press(k))
            bh.addWidget(b)
            self.buttons[key] = b
        outer.addLayout(bh)
        self.resize(width, 200)
        self._width = width
        self._fitted = False

    def press(self, key):
        """Close the question as if the button `key` was pressed (for a button inside the body, like "Free up
        space")."""
        self.result_key = key
        self.accept()

    _press = press

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._fitted:
            self._fitted = True
            scr = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
            self.resize(min(self._width, scr.width() - 40), self.height())
            # twice: the first pass may measure with a scroll bar that then goes away (wider, so shorter)
            QTimer.singleShot(0, self, self._fit)
            QTimer.singleShot(30, self, self._fit)

    def _fit(self):
        """As tall as the content, up to most of the screen; then the middle scrolls. Measured from where the
        widgets really are (wrapped labels: their height depends on the width they get)."""
        scr = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        lay = self._body.layout()
        bottom = 0
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None and not w.isHidden():
                bottom = max(bottom, w.geometry().bottom() + 1)
        rest = self.height() - self._scroll.height()
        want = min(bottom + 2, max(160, int(scr.height() * 0.85) - rest))
        self._scroll.setMinimumHeight(want)
        self.resize(self.width(), rest + want)

    def exec(self):
        super().exec()
        return self.result_key


More = _More  # public name: the queue review uses it for "Show the exact commands"


def _means(text):
    """'What it means for you: …' in a blue box."""
    f = _frame("qmeans", BLUE, alpha_bg=0.07, alpha_border=0.30)
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 8, 12, 8)
    lbl = QLabel()
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(True)
    lbl.setText(_("What it means for you: {text}").format(text=text))
    fnt = lbl.font()
    fnt.setPointSizeF(fnt.pointSizeF() * 0.92)
    lbl.setFont(fnt)
    v.addWidget(lbl)
    return f


def ask_change(parent, title, **kw):
    """Show a Question and return the key of the button pressed, or None. Every argument of Question."""
    dlg = Question(parent, title, **kw)
    try:
        return dlg.exec()
    finally:
        dlg.deleteLater()


def confirm(parent, title, yes, **kw):
    """The common case: 'Not yet' and one primary button. True when the primary one is pressed."""
    kw.setdefault("buttons", [("no", _("Not yet"), False), ("yes", yes, True)])
    return ask_change(parent, title, **kw) == "yes"
