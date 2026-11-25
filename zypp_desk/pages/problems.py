# SPDX-License-Identifier: GPL-3.0-or-later
"""Choose one of the solutions zypper offers for a dependency problem, said in plain words.

The problems come from simulation.problem_items(): each one has a plain explanation, zypper's own text and its
solutions, with the queue actions that carry each one out."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QRadioButton, QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget)

from ..i18n import _, ngettext
from ..style import BLUE, GREEN
from ..ui import button, heading, hline, icon, mono, muted

CSS = f"""
QFrame#solution {{ border: 1px solid rgba(127, 140, 141, 0.40); border-radius: 10px; background: palette(base); }}
QFrame#solution[on="true"] {{ border: 1px solid {GREEN}; background: rgba(39, 174, 96, 0.08); }}
QLabel#explain {{ background: rgba(127, 140, 141, 0.13); border-radius: 8px; padding: 10px 12px; }}
QFrame#cannot {{ border: 1px dashed rgba(127, 140, 141, 0.45); border-radius: 10px; }}
QLabel#pill {{ background: {GREEN}; color: white; border-radius: 8px; padding: 2px 9px; font-weight: 600; }}
QToolButton#link {{ color: {BLUE}; border: none; padding: 2px 0px; }}
QToolButton#link:hover {{ text-decoration: underline; }}
"""


def _plain(text, parent=None, wrap=True):
    """A label for text that comes from zypper (package names): never read as rich text."""
    lbl = QLabel(text, parent)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(wrap)
    return lbl


def _link(text, parent=None):
    """A small text button with an arrow that opens and closes something below it."""
    b = QToolButton(parent)
    b.setObjectName("link")
    b.setText(text)
    b.setCheckable(True)
    b.setAutoRaise(True)
    b.setArrowType(Qt.ArrowType.RightArrow)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.toggled.connect(lambda on, b=b: b.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow))
    return b


def queue_lines(sol):
    """What a solution does to the queue, one line each: 'To the queue: zypper addlock vlc'."""
    lines = [_("To the queue: {command}").format(command=c) for c in sol.get("commands", [])]
    for a in sol.get("actions", []):
        if isinstance(a, dict) and a.get("kind") == "discard":
            cmd = ("zypper remove " if a.get("of") == "remove" else "zypper install ") + a["target"]
            lines.append(_("Out of the queue: {command}").format(command=cmd))
    return lines


class SolutionCard(QFrame):
    """One solution: a radio button, the text in bold (and 'recommended'), what it means, and the commands."""

    def __init__(self, sol, group, parent=None):
        super().__init__(parent)
        self.setObjectName("solution")
        self.sol = sol
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 14, 10)
        h.setSpacing(10)
        self.radio = QRadioButton(self)
        self.radio.setAccessibleName(sol["text"])
        group.addButton(self.radio)
        h.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        title = _plain(sol["text"], self)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        # a wrapped label next to a stretch gets squeezed: keep a short text on one line, wrap a long one
        title.setMinimumWidth(min(title.fontMetrics().horizontalAdvance(sol["text"]) + 4, 400))
        top.addWidget(title)
        if sol.get("recommended"):
            pill = QLabel(_("recommended"), self)
            pill.setObjectName("pill")
            pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            top.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)
        top.addStretch(1)
        v.addLayout(top)
        if sol.get("detail"):
            v.addWidget(_plain(sol["detail"], self))
        lines = queue_lines(sol)
        if lines:
            cmd = _plain("\n".join(lines), self)
            cmd.setFont(mono())
            cmd.setStyleSheet(f"color: {BLUE};")
            v.addWidget(cmd)
        h.addLayout(v, 1)
        self.radio.toggled.connect(self._toggled)

    def _toggled(self, on):
        self.setProperty("on", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.radio.setChecked(True)
            self.radio.setFocus(Qt.FocusReason.MouseFocusReason)
            ev.accept()
            return
        super().mousePressEvent(ev)


class ProblemDialog(QDialog):
    """The problems a simulation found, one at a time, each with the solutions that can be chosen here.

    items: simulation.problem_items(). After exec() returns Accepted, chosen() gives one solution per problem
    that has any (the dicts from items, with their 'actions')."""

    def __init__(self, items, parent=None, apply_label=None, note=None):
        super().__init__(parent)
        self.items = list(items)
        self.setWindowTitle(_("Choose a solution"))
        self.setStyleSheet(CSS)
        self.setMinimumWidth(660)
        self.groups = []  # one (QButtonGroup, {radio: solution}) per problem; None when nothing can be chosen
        n = len(self.items)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(12)

        # header: warning, title, "1 of 2 · zypper dup", previous / next
        head = QHBoxLayout()
        head.setSpacing(12)
        ic = icon("dialog-warning")
        if ic.isNull():
            ic = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        pic = QLabel(self)
        pic.setPixmap(ic.pixmap(40, 40))
        head.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        tl = QVBoxLayout()
        tl.setSpacing(1)
        title = (_("The simulation found a problem") if n == 1 else
                 ngettext("The simulation found {n} problem", "The simulation found {n} problems", n).format(n=n))
        tl.addWidget(heading(title, 1.3, self))
        self.pos = muted("", self)
        self.pos.setTextFormat(Qt.TextFormat.PlainText)
        tl.addWidget(self.pos)
        head.addLayout(tl, 1)
        self.prev = QToolButton(self)
        self.prev.setIcon(icon("go-previous"))
        if self.prev.icon().isNull():
            self.prev.setArrowType(Qt.ArrowType.LeftArrow)
        self.prev.setToolTip(_("Previous problem"))
        self.prev.clicked.connect(lambda: self.show_problem(self.current - 1))
        self.next = QToolButton(self)
        self.next.setIcon(icon("go-next"))
        if self.next.icon().isNull():
            self.next.setArrowType(Qt.ArrowType.RightArrow)
        self.next.setToolTip(_("Next problem"))
        self.next.clicked.connect(lambda: self.show_problem(self.current + 1))
        for b in (self.prev, self.next):
            b.setAutoRaise(True)
            b.setVisible(n > 1)
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        # one page per problem; only the one shown counts for the height of the window
        self.current = 0
        self.pages = [self._page(item) for item in self.items]
        holder = QWidget(self)
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        for page in self.pages:
            page.hide()
            hl.addWidget(page)
        lay.addWidget(holder)

        lay.addWidget(hline())
        self.raw = QPlainTextEdit(self)
        self.raw.setReadOnly(True)
        self.raw.setFont(mono())
        self.raw.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.raw.setFixedHeight(170)
        self.raw.hide()
        lay.addWidget(self.raw)
        if note:
            lbl = muted(note, self)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lay.addWidget(lbl)
        bottom = QHBoxLayout()
        self.show_raw = _link(_("Show zypper's text"), self)
        self.show_raw.toggled.connect(self._raw_toggled)
        bottom.addWidget(self.show_raw)
        bottom.addStretch(1)
        cancel = QPushButton(_("Cancel"), self)
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self.apply = button(apply_label or _("Add to the queue and simulate again"), primary=True, parent=self)
        self.apply.setDefault(True)
        self.apply.clicked.connect(self.accept)
        bottom.addWidget(self.apply)
        lay.addLayout(bottom)

        self.resize(740, 10)
        self.show_problem(0)
        self._update_apply()

    # ------------------------------------------------------------------ one page per problem
    def _page(self, item):
        page = QWidget(self)
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        explain = _plain(item["text"], page)
        explain.setObjectName("explain")
        v.addWidget(explain)
        sols = item.get("solutions", [])
        good = [s for s in sols if s["possible"]]
        bad = [s for s in sols if not s["possible"]]
        if good:
            v.addWidget(heading(_("What do you want to do?"), 1.05, page))
            group = QButtonGroup(page)
            group.setExclusive(True)
            cards = {}
            for sol in sorted(good, key=lambda s: not s.get("recommended")):  # the recommended one first
                card = SolutionCard(sol, group, page)
                cards[card.radio] = sol
                v.addWidget(card)
                if sol.get("recommended"):
                    card.radio.setChecked(True)
            group.buttonToggled.connect(lambda *_a: self._update_apply())
            self.groups.append((group, cards))
        else:
            self.groups.append(None)
            if bad:
                text = _("None of the solutions that zypper offers can be chosen here. You can change the queue "
                         "yourself (for example, lock or remove a package) and simulate again.")
            else:
                text = _("zypper shows the solutions of one problem at a time. Solve the one before and simulate "
                         "again: if this problem is still there, its solutions will appear.")
            v.addWidget(muted(text, page))
        if bad:
            more = _link(ngettext("{n} more option that cannot be chosen here",
                                  "{n} more options that cannot be chosen here", len(bad)).format(n=len(bad)), page)
            v.addWidget(more)
            box = QFrame(page)
            box.setObjectName("cannot")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(14, 10, 14, 10)
            bl.setSpacing(2)
            for i, sol in enumerate(bad):
                t = _plain(sol["text"], box)
                f = t.font()
                f.setBold(True)
                t.setFont(f)
                if i:
                    bl.addSpacing(6)
                bl.addWidget(t)
                d = muted(sol.get("detail", ""), box)
                d.setTextFormat(Qt.TextFormat.PlainText)
                bl.addWidget(d)
            box.hide()
            v.addWidget(box)
            more.toggled.connect(box.setVisible)
            more.toggled.connect(lambda *_a: self._fit())
        v.addStretch(1)
        return page

    # ------------------------------------------------------------------ state
    def show_problem(self, i):
        n = len(self.items)
        if not n:
            self.pos.setText(_("There is nothing to choose."))
            self.prev.setEnabled(False)
            self.next.setEnabled(False)
            return
        i = max(0, min(n - 1, i))
        self.current = i
        for k, page in enumerate(self.pages):
            page.setVisible(k == i)
        text = _("{i} of {n}").format(i=i + 1, n=n)
        if self.items[i].get("source"):
            text += " · " + self.items[i]["source"]
        self.pos.setText(text)
        self.prev.setEnabled(i > 0)
        self.next.setEnabled(i < n - 1)
        self.raw.setPlainText(self.items[i].get("raw", ""))
        self._fit()

    def _raw_toggled(self, on):
        self.raw.setVisible(on)
        self._fit()

    def _fit(self):
        """Grow or shrink to what is shown (the width stays)."""
        def fit():
            lay = self.layout()
            lay.activate()
            h = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
            self.resize(self.width(), max(h, lay.totalMinimumSize().height()))
        fit()
        QTimer.singleShot(0, fit)

    def _update_apply(self):
        groups = [g for g in self.groups if g]
        self.apply.setEnabled(bool(groups) and all(g.checkedButton() is not None for g, _c in groups))

    def chosen(self):
        """The chosen solution of every problem that has solutions, in the order of the problems."""
        out = []
        for g in self.groups:
            if g and g[0].checkedButton() is not None:
                out.append(g[1][g[0].checkedButton()])
        return out
