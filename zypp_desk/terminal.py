# SPDX-License-Identifier: GPL-3.0-or-later
"""The terminal panel: zypper's own output, as it would look in Konsole.

Lines are coloured with simple rules, a line repeated many times in a row is folded into one
("the same line 25 more times"), and a filter can hide everything except warnings and errors.
"""

import re
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette, QTextCharFormat, QTextCursor
import html

from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QSplitter, QTextBrowser,
                               QToolButton, QVBoxLayout, QWidget, QApplication)

from .i18n import _

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BG, FG = "#232627", "#eff0f1"
COLORS = {"green": "#1cdc9a", "yellow": "#fdbc4b", "red": "#ed1515", "blue": "#1d99f3", "cyan": "#1abc9c",
          "magenta": "#c792ea", "grey": "#7f8c8d", "white": "#ffffff"}

# Words zypper prints at the end of a progress bar, in a few languages.
DONE_WORDS = ("done", "fet", "hecho", "fertig", "fait", "fatto", "concluído", "klaar", "gotowe")
WARN_RE = re.compile(r"^(warning|avís|aviso|warnung|attention|avvertimento)\b", re.I)
ERR_RE = re.compile(r"(^|\s)(error|err|problem|problema|fehler|erreur|errore)\s*[:!]|\bfailed\b|\bha fallat\b"
                    r"|\bfalló\b", re.I)


def _fmt(color=None, bold=False, italic=False):
    f = QTextCharFormat()
    if color:
        f.setForeground(QColor(COLORS.get(color, color)))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class TerminalView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setMaximumBlockCount(20000)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.setFont(font)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(BG))
        pal.setColor(QPalette.ColorRole.Text, QColor(FG))
        self.setPalette(pal)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {BG}; color: {FG}; border: none; padding: 6px; }}")
        self.fold = True
        self.only_problems = False
        self._partial = False      # the last block is an unfinished line
        self._last_line = None
        self._repeats = 0
        self._fold_block = None    # block number of the "same line N more times" marker
        self.problem_lines = 0
        self._pending_cr = False
        self._relayout_pending = False

    # ------------------------------------------------------------ public
    def prompt(self, text):
        self._end_partial()
        self._append_line(text, [(0, len(text), _fmt("blue", bold=True))], kind="prompt")
        self._last_line = None

    def note(self, text, kind="info"):
        self._end_partial()
        color = {"error": "red", "info": "grey", "ok": "green"}.get(kind, "grey")
        self._append_line(text, [(0, len(text), _fmt(color, italic=(kind == "info")))], kind=kind)
        self._last_line = None

    def feed(self, text):
        text = ANSI_RE.sub("", text)
        # "\r\n" is a normal end of line, even when the "\r" and the "\n" arrive in different chunks
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        text = text.replace("\r\n", "\n")
        if text.endswith("\r"):
            self._pending_cr = True
            text = text[:-1]
        pieces = re.split(r"(\n|\r)", text)
        for piece in pieces:
            if piece == "\n":
                self._finish_line()
            elif piece == "\r":
                self._clear_partial()
            elif piece:
                self._add_partial(piece)
        self._scroll()

    def clear_all(self):
        self.clear()
        self._partial = False
        self._last_line = None
        self._repeats = 0
        self._fold_block = None
        self.problem_lines = 0

    def set_only_problems(self, on):
        self.only_problems = on
        doc = self.document()
        b = doc.begin()
        while b.isValid():
            kind = b.userState()
            b.setVisible(not on or kind in (1, 2, 3))
            b = b.next()
        doc.markContentsDirty(0, doc.characterCount())
        self.viewport().update()
        self.updateGeometry()

    # ------------------------------------------------------------ lines
    def _cursor_end(self):
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        return c

    def _add_partial(self, piece):
        c = self._cursor_end()
        if not self._partial:
            if not self.document().isEmpty():
                c.insertBlock()
            self._partial = True
        c.insertText(piece, _fmt())
        self.setTextCursor(c)

    def _clear_partial(self):
        if not self._partial:
            return
        c = self._cursor_end()
        c.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()

    def _end_partial(self):
        if self._partial:
            self._finish_line()

    def _finish_line(self):
        if not self._partial:
            self._add_partial("")
        block = self.document().lastBlock()
        line = block.text()
        self._partial = False
        if self.fold and line.strip() and line == self._last_line:
            # Same line again: drop it and count it in the marker.
            c = self._cursor_end()
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
            c.removeSelectedText()
            c.deletePreviousChar()
            self._repeats += 1
            self._update_fold_marker()
            return
        self._repeats = 0
        self._fold_block = None
        self._last_line = line
        kind = self._classify(line)
        self._colorize(block, line, kind)

    def _update_fold_marker(self):
        text = "  ⋯ " + _("the same line {n} more times").format(n=self._repeats)
        doc = self.document()
        if self._fold_block is None:
            c = self._cursor_end()
            c.insertBlock()
            c.insertText(text, _fmt("grey", italic=True))
            self._fold_block = True
            doc.lastBlock().setUserState(0)
        else:
            # the marker is always the last block when it is updated (the repeated line was just removed);
            # block numbers are not used because old lines are dropped after 20,000
            c = QTextCursor(doc.lastBlock())
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            c.insertText(text, _fmt("grey", italic=True))
        if self.only_problems:
            self._hide(doc.lastBlock())
        # the marker stands in for the repeated line; the next different line starts fresh
        self._partial = False

    def _append_line(self, text, spans, kind="info"):
        c = self._cursor_end()
        if not self.document().isEmpty():
            c.insertBlock()
        c.insertText(text, _fmt())
        block = self.document().lastBlock()
        for start, length, f in spans:
            cc = QTextCursor(block)
            cc.setPosition(block.position() + start)
            cc.setPosition(block.position() + start + length, QTextCursor.MoveMode.KeepAnchor)
            cc.setCharFormat(f)
        block.setUserState({"prompt": 3, "error": 2}.get(kind, 0))
        if self.only_problems and kind not in ("prompt", "error"):
            self._hide(block)
        self._scroll()

    def _hide(self, block):
        block.setVisible(False)
        # Hiding lines while text streams in leaves the view's layout stale (the first visible lines end up
        # above the top and cannot be scrolled to). Relayout the whole document once, shortly after.
        if not self._relayout_pending:
            self._relayout_pending = True
            QTimer.singleShot(60, self._relayout)

    def _relayout(self):
        self._relayout_pending = False
        if self.only_problems:
            # re-applying the filter from scratch is what resets QPlainTextEdit's first visible line
            self.set_only_problems(False)
            self.set_only_problems(True)
        self._scroll()

    @staticmethod
    def _classify(line):
        from .explain import INFO, NONE, SERIOUS, WARN, severity_of_line
        s = line.strip()
        known = severity_of_line(s)
        if known in (NONE, INFO):
            return "known-" + known
        if known == WARN:
            return "warning"
        if known == SERIOUS:
            return "error"
        if ERR_RE.search(s) and not s.startswith("CommitResult"):
            return "error"
        if WARN_RE.match(s) or s.lower().startswith("warning") or "egrep: warning" in s:
            return "warning"
        return "normal"

    def _colorize(self, block, line, kind):
        spans = []
        s = line.strip()
        if kind == "error":
            spans.append((0, len(line), _fmt("red")))
            self.problem_lines += 1
        elif kind == "warning":
            spans.append((0, len(line), _fmt("yellow")))
            self.problem_lines += 1
        elif kind == "known-none":
            spans.append((0, len(line), _fmt("grey")))
        elif kind == "known-info":
            spans.append((0, len(line), _fmt("cyan")))
        elif s.startswith("CommitResult"):
            spans.append((0, len(line), _fmt("green" if "error 0" in s else "red")))
        elif "->" in line:
            i = line.index("->")
            spans.append((i, 2, _fmt("grey")))
            spans.append((i + 2, len(line) - i - 2, _fmt("green")))
        elif re.match(r"^\(\d+/\d+\)", s):
            spans.append((0, len(line), _fmt("white", bold=True)))
        elif s.endswith("?") or re.search(r"\[[a-z/.?…]+\]\s*\(\w\):", s):
            spans.append((0, len(line), _fmt("magenta")))
        for w in DONE_WORDS:
            for m in re.finditer(re.escape(w) + r"\]", line):
                spans.append((m.start(), len(w), _fmt("green")))
        for m in re.finditer(r":\s(OK)\b", line):
            spans.append((m.start(1), 2, _fmt("green")))
        for start, length, f in spans:
            c = QTextCursor(block)
            c.setPosition(block.position() + start)
            c.setPosition(block.position() + start + length, QTextCursor.MoveMode.KeepAnchor)
            c.mergeCharFormat(f)
        block.setUserState({"error": 2, "warning": 1, "known-info": 1}.get(kind, 0))
        if self.only_problems and kind not in ("error", "warning", "known-info"):
            self._hide(block)

    def _scroll(self):
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


class TerminalPanel(QWidget):
    """The terminal with its small toolbar (title, filter, copy, save, clear, hide)."""

    hide_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = TerminalView(self)
        bar = QWidget(self)
        bar.setStyleSheet("QWidget { background: #31363b; color: #fcfcfc; }"
                          "QToolButton { border: 1px solid #4d5257; border-radius: 3px; padding: 2px 8px;"
                          " color: #eff0f1; background: #3b4045; }"
                          "QToolButton:checked { background: rgba(61,174,233,0.35); border-color: #3daee9; }")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(10, 4, 6, 4)
        self.title = QLabel(_("Terminal"), bar)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.title.setFont(mono)
        hb.addWidget(self.title, 1)
        self.only = self._button(_("Only warnings and errors"), checkable=True)
        self.only.toggled.connect(self.view.set_only_problems)
        hb.addWidget(self.only)
        self.only.setToolTip(_("Show only the lines with a warning or an error."))
        # each says it acts on the terminal: the queue bar under it has its own "Empty the queue"
        for text, slot, tip in (
                (_("Copy the output"), self.copy_all, _("Copy everything the terminal shows.")),
                (_("Save the output…"), self.save, _("Save everything the terminal shows in a text file.")),
                (_("Clear the terminal"), self.view.clear_all, _("Clear what the terminal shows. Nothing else "
                                                                  "changes.")),
                (_("Hide the terminal"), self.hide_requested.emit, _("Hide the terminal; “Show the terminal” "
                                                                     "brings it back."))):
            b = self._button(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            hb.addWidget(b)
        self.notes = QTextBrowser(self)
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumWidth(280)
        self.notes.hide()
        self.split = QSplitter(Qt.Orientation.Horizontal, self)
        self.split.addWidget(self.view)
        self.split.addWidget(self.notes)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(bar)
        lay.addWidget(self.split, 1)

    def _button(self, text, checkable=False):
        b = QToolButton(self)
        b.setText(text)
        b.setCheckable(checkable)
        b.setAutoRaise(False)
        return b

    def show_explanations(self, items):
        """The warnings of the last run, explained, next to the terminal."""
        from .explain import severity_color, severity_label
        if not items:
            self.notes.hide()
            return
        parts = [f"<h3 style='margin:4px 0'>{html.escape(_('What the messages mean'))}</h3>"]
        for d in items:
            color = severity_color(d["severity"])
            times = f" <span style='color:gray'>×{d['count']}</span>" if d["count"] > 1 else ""
            parts.append(
                f"<p style='margin:10px 0 2px'><span style='background-color:{color};color:white;font-weight:bold'>"
                f"&nbsp;{html.escape(severity_label(d['severity']))}&nbsp;</span>&nbsp; <b>{html.escape(d['title'])}</b>"
                f"{times}</p><p style='margin:2px 0'>{html.escape(d['what'])}</p>"
                f"<p style='margin:2px 0'><b>{html.escape(_('What to do:'))}</b> {html.escape(d['todo'])}</p>"
                f"<p style='margin:2px 0 8px;color:gray;font-family:monospace;font-size:small'>"
                f"{html.escape(d['example'][:200])}</p>")
        self.notes.setHtml("".join(parts))
        self.notes.show()

    def set_title(self, text):
        self.title.setText(text)

    def copy_all(self):
        QApplication.clipboard().setText(self.view.toPlainText())

    def save(self):
        name = time.strftime("zypp-desk-%Y-%m-%d-%H%M.txt")
        path, _filter = QFileDialog.getSaveFileName(self, _("Save the terminal output"), name,
                                                    _("Text files (*.txt)"))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.view.toPlainText() + "\n")
