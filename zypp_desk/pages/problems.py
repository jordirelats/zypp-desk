# SPDX-License-Identifier: GPL-3.0-or-later
"""Choose one of the solutions zypper offers for a dependency problem, said in plain words.

The problems come from simulation.problem_items(): each one has a plain explanation, zypper's own text and its
solutions, with the queue actions that carry each one out. Many problems are first shown grouped by cause
(problemgroups), with what is usually done: wait a day or two, or leave them as they are and update the rest."""

import datetime
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget)

from .. import problemgroups
from ..i18n import _, ngettext
from ..question import label as qlabel
from ..runner import Step
from ..style import BLUE, GREEN
from ..tasks import run_async
from ..txqueue import PKG_KINDS, SOLVABLE_KINDS, Action, TxQueue, pkg_ident
from ..ui import button, heading, hline, icon, mono, muted

ADVICE = "#2980b9"  # "Recommended: wait a day or two", as in the questions (question.reasons)

CSS = f"""
QFrame#solution {{ border: 1px solid rgba(127, 140, 141, 0.40); border-radius: 10px; background: palette(base); }}
QFrame#solution[on="true"] {{ border: 1px solid {GREEN}; background: rgba(39, 174, 96, 0.08); }}
QLabel#explain {{ background: rgba(127, 140, 141, 0.13); border-radius: 8px; padding: 10px 12px; }}
QFrame#cannot {{ border: 1px dashed rgba(127, 140, 141, 0.45); border-radius: 10px; }}
QFrame#group {{ border: 1px solid rgba(127, 140, 141, 0.40); border-radius: 10px; background: palette(base); }}
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


def _kind_name(target):
    """'pattern:kde' → ('pattern', 'kde'); 'vlc' → ('package', 'vlc')."""
    kind, colon, name = target.partition(":")
    return (kind, name) if colon and kind in SOLVABLE_KINDS and name else ("package", target)


def _copy(a):
    return Action(a.kind, a.target, **a.opts)


def solution_plan(solutions, now=False):
    """What the chosen solutions do, exactly as Zypp Desk carries them out (mainwindow.apply_solutions).

    Returns (steps, add, discard):
    - steps: runner.Steps that run at once, in this order: unlocks, locks and, with now=True, one
      'zypper install' with the installs and the removals as '!name' (one transaction: if something still
      needs a package to remove, zypper refuses and changes nothing, instead of removing that too).
    - add: with now=False, the install and remove Actions that go to the queue.
    - discard: with now=False, the {kind: 'discard', target, of} dicts whose queued install or removal leaves
      the queue.
    Locks and unlocks always run at once: a simulation only knows the locks that exist. With now=True (the dup)
    there is no queue: "do not install X" and "keep X" become a lock, so that zypper leaves X as it is."""
    locks, unlocks, pkgs, add, discard = {}, {}, [], [], []
    for sol in solutions:
        for a in sol.get("actions") or []:
            if isinstance(a, dict):
                if a.get("kind") != "discard":
                    continue
                if now:
                    typ, name = _kind_name(a["target"])
                    locks.setdefault(typ, []).append(name)
                else:
                    discard.append(a)
            elif a.kind in ("lock", "unlock"):
                (locks if a.kind == "lock" else unlocks).setdefault(a.opts.get("type", "package"), []).append(
                    a.target)
            elif a.kind in ("install", "remove"):
                (pkgs if now else add).append(a)
    steps = []
    # An unlock must name the lock exactly as it was written (a version range, a repository, a kind), or zypper
    # removes nothing: look the locks up (only when there is something to unlock; zypper locks is quick).
    existing = []
    if unlocks:
        try:
            from .. import backend
            existing = backend.locks()
        except Exception:  # zypper could not tell: unlock by name, the Locks page reports it if nothing went
            existing = []
    by_place = {}
    for typ, names in unlocks.items():
        for name in dict.fromkeys(names):
            found = [lk for lk in existing if lk["name"] == name and lk.get("type", "package") == typ]
            for lk in found or [{"spec": name, "repo": ""}]:
                by_place.setdefault((typ, lk.get("repo") or ""), []).append(lk["spec"])
    for (typ, repo), specs in by_place.items():
        steps.append(Step("zypper", ["-n", "removelock", *([] if typ == "package" else ["-t", typ]),
                                     *(["-r", repo] if repo else []), "--", *dict.fromkeys(specs)]))
    for typ, names in locks.items():
        steps.append(Step("zypper", ["-n", "addlock", *([] if typ == "package" else ["-t", typ]), "--",
                                     *dict.fromkeys(names)]))
    if pkgs:
        args = ["-n", "install", *(["--oldpackage"] if any(a.opts.get("oldpackage") for a in pkgs) else []), "--"]
        steps.append(Step("zypper", args + list(dict.fromkeys(
            ("!" + a.target) if a.kind == "remove" else a.target for a in pkgs))))
    for i, st in enumerate(steps):
        st.cond = "always" if i == 0 else "success"
    return steps, add, discard


def queued_matches(queue, d):
    """The queued installs or removals that a "discard" {target, of} takes out of the queue (as
    mainwindow.apply_solutions does: 'pattern:kde' only matches the pattern, 'packman:vlc' matches vlc)."""
    if queue is None:
        return []
    want = pkg_ident(Action("install", d.get("target") or ""))
    kinds = (d["of"],) if d.get("of") else PKG_KINDS
    return [x for x in queue.of(*kinds) if pkg_ident(x) == want]


def queue_lines(sol, now=False, queue=None):
    """What a solution does, one line per command, exactly as it will run:
    'Runs now: zypper -n addlock -- vlc', 'To the queue: zypper -n install -- fighter '!codecs'',
    'Out of the queue: zypper -n install -- fighter'.

    queue: the window's TxQueue, if known. 'To the queue' then shows the package command that the queue will
    run with this solution in it (the queue's own code, with the user's settings: installs and removals go
    together as one transaction); 'Out of the queue', the queued command that leaves."""
    steps, add, discard = solution_plan([sol], now)
    lines = [_("Runs now: {command}").format(command=st.display()) for st in steps]
    settings = getattr(queue, "settings", None) or {}
    if add:
        q = TxQueue(settings)
        with q.batch():
            for a in (queue.of(*PKG_KINDS) if queue is not None else []) + add:
                q.add(_copy(a))
        mine = {a.target for a in add} | {"!" + a.target for a in add}
        lines += [_("To the queue: {command}").format(command=st.display()) for st in q.steps()
                  if mine & set(st.args)]
    for d in discard:
        leaving = queued_matches(queue, d) or [Action(d.get("of") or "install", d["target"])]
        for a in leaving:
            q = TxQueue(settings)
            q.add(_copy(a))
            lines += [_("Out of the queue: {command}").format(command=st.display()) for st in q.steps()]
    return lines


def _find_queue(widget):
    """The main window's queue, from a dialog's parents (None when there is none, as in tests)."""
    w = widget
    while w is not None:
        q = getattr(w, "queue", None)
        if q is not None and hasattr(q, "of"):
            return q
        w = w.parentWidget()
    return None


class SolutionCard(QFrame):
    """One solution: a radio button, the text in bold (and 'recommended'), what it means, and the commands."""

    def __init__(self, sol, group, parent=None, now=False, queue=None):
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
        detail = (sol.get("detail_now") if now else None) or sol.get("detail")
        if detail:
            v.addWidget(_plain(detail, self))
        lines = queue_lines(sol, now, queue)
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


def reminder_time(now=None):
    """Tomorrow at nine in the morning (local time), as a Unix time: when "Wait and try again tomorrow" reminds."""
    today = datetime.datetime.fromtimestamp(now or time.time()).date()
    return int(datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time(9, 0)).timestamp())


def _find_settings(widget):
    """The main window's settings, from a dialog's parents (None when there is none, as in tests)."""
    w = widget
    while w is not None:
        s = getattr(w, "settings", None)
        if isinstance(s, dict) and hasattr(s, "save"):
            return s
        w = w.parentWidget()
    return None


def remind_tomorrow(widget=None, now=None):
    """Set settings["update_reminder_at"] to tomorrow morning and save it; the tray icon (tray.py) reminds then.
    The tray is started if it is not running, or nobody would remind. Returns the time set."""
    at = reminder_time(now)
    settings = _find_settings(widget)
    if settings is None:
        from ..settings import Settings
        settings = Settings()
    settings["update_reminder_at"] = at
    settings.save()
    try:
        from .. import tray
        if not tray.tray_running():
            tray.start_tray()
    except Exception:  # no tray possible (no session bus…): the setting is saved all the same
        pass
    return at


class _More(QLabel):
    """'▸ Show them (38)' on a group: a click shows the names under it, a second one hides them. The same words
    as question.py's, and the window grows or shrinks with it (on_toggle)."""

    def __init__(self, n, target, on_toggle, parent=None):
        self._closed, self._open = _("▸ Show them ({n})").format(n=n), _("▾ Hide them")
        super().__init__(self._closed, parent)
        self._target, self._on_toggle = target, on_toggle
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet(f"color: {BLUE};")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(self._closed)

    def _toggle(self):
        on = not self._target.isVisible()
        self._target.setVisible(on)
        self.setText(self._open if on else self._closed)
        self._on_toggle()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._toggle()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._toggle()
            return
        super().keyPressEvent(ev)


class GroupCard(QFrame):
    """One cause: a coloured dot, what it is in bold, what it means, what is usually done, and the packages."""

    def __init__(self, g, on_toggle, parent=None):
        super().__init__(parent)
        self.setObjectName("group")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 14, 10)
        h.setSpacing(10)
        dot = QLabel("●", self)
        dot.setStyleSheet(f"color: {g.get('color') or BLUE}; font-size: 13px;")
        h.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(qlabel(g["title"], bold=True), 1)
        more = [m for m in g.get("more") or [] if m]
        names = None
        if more:
            shown = more if len(more) <= 400 else more[:400] + ["…"]
            if g["kind"] == problemgroups.OTHER:  # whole sentences: wrapped, one paragraph each
                names = qlabel("\n\n".join(shown), small=True, selectable=True)
            else:
                names = qlabel("\n".join(shown), small=True, selectable=True, font=mono())
            names.setVisible(False)
            top.addWidget(_More(len(more), names, on_toggle, self), 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)
        if g.get("detail"):
            v.addWidget(qlabel(g["detail"], small=True, muted=True))
        if g.get("advice"):
            v.addWidget(qlabel(g["advice"], small=True, color=ADVICE))
        if names is not None:
            names.setContentsMargins(0, 4, 0, 0)
            v.addWidget(names)
        h.addLayout(v, 1)


class ChoiceCard(QFrame):
    """One answer to "What do you want to do?": a radio button, the answer in bold and what it means."""

    def __init__(self, key, title, detail, group, parent=None):
        super().__init__(parent)
        self.setObjectName("solution")
        self.key = key
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 9, 14, 9)
        h.setSpacing(10)
        self.radio = QRadioButton(self)
        self.radio.setAccessibleName(title)
        group.addButton(self.radio)
        h.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        self.title = qlabel(title, bold=True)
        v.addWidget(self.title)
        self.detail = qlabel(detail, small=True, muted=True)
        v.addWidget(self.detail)
        h.addLayout(v, 1)
        self.radio.toggled.connect(self._toggled)

    def set_text(self, title, detail):
        self.title.setText(title)
        self.radio.setAccessibleName(title)
        self.detail.setText(detail)

    def _toggled(self, on):
        self.setProperty("on", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.radio.isEnabled():
            self.radio.setChecked(True)
            self.radio.setFocus(Qt.FocusReason.MouseFocusReason)
            ev.accept()
            return
        super().mousePressEvent(ev)


class ProblemDialog(QDialog):
    """The problems a simulation found, each with the solutions that can be chosen here.

    items: simulation.problem_items(). After exec() returns Accepted, chosen() gives one solution per problem
    that has any (the dicts from items, with their 'actions'); solution_plan(dlg.chosen(), now) says what to do.
    now: True when what is chosen runs at once (the dup: there is no queue), False when installs and removals
    go to the queue (locks and unlocks run at once in both cases). Each solution shows its exact commands.

    With many problems (problemgroups.MANY or more) the window opens on a summary: the problems grouped by cause
    (problemgroups, looked up in a thread), and three answers:
    - "Wait and try again tomorrow": sets settings["update_reminder_at"] (the tray icon reminds), changes nothing,
      and exec() returns Rejected with wait_requested = True;
    - "Update the rest and leave these as they are": exec() returns Accepted and chosen() gives, for every problem
      that has one, the solution that leaves it as it is (a lock, the removal of a package that no longer exists,
      or a queued install leaving the queue);
    - "Look at them one by one": the problems one at a time, as with few problems.
    With 2 or 3 problems it opens on the problems, and offers the summary when they have fewer causes than
    problems. Enter never starts a change: the default button is Cancel."""

    def __init__(self, items, parent=None, apply_label=None, note=None, *, now=False):
        super().__init__(parent)
        self.items = list(items)
        self.now = now
        self.queue = None if now else _find_queue(parent)
        self.apply_label = apply_label
        self.wait_requested = False
        self.analysis = None  # problemgroups.analyse(): dict(groups, keeps)
        self._keep = None     # the solutions of "leave these as they are", once chosen
        self._closed = False
        self._touched = False  # the answer was chosen by hand: the analysis does not change it any more
        self.mode = "pages"
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
        if n == 1:
            title = _("The simulation found a problem")
        elif now:
            title = ngettext("The update has {n} problem", "The update has {n} problems", n).format(n=n)
        else:
            title = ngettext("The simulation found {n} problem", "The simulation found {n} problems", n).format(n=n)
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

        # the summary by cause (with 2 problems or more), then one page per problem; only what is shown counts
        # for the height of the window
        self.current = 0
        holder = self._holder = QWidget(self)
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        self.summary = self._build_summary(holder) if n > 1 else None
        if self.summary is not None:
            self.summary.hide()
            hl.addWidget(self.summary)
        self.pages = [self._page(item) for item in self.items]
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
        self.to_summary = QToolButton(self)
        self.to_summary.setObjectName("link")
        self.to_summary.setText(_("◂ Grouped by cause"))
        self.to_summary.setAutoRaise(True)
        self.to_summary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.to_summary.clicked.connect(lambda: self.set_mode("summary"))
        self.to_summary.hide()
        bottom.addWidget(self.to_summary)
        bottom.addStretch(1)
        self.cancel = QPushButton(_("Cancel"), self)
        self.cancel.clicked.connect(self.reject)
        self.cancel.setDefault(True)  # Enter never starts a change
        bottom.addWidget(self.cancel)
        self.apply = button(apply_label or _("Add to the queue and simulate again"), primary=True, parent=self)
        self.apply.setAutoDefault(False)
        self.apply.clicked.connect(self._go)
        bottom.addWidget(self.apply)
        lay.addLayout(bottom)

        self.resize(760, 10)
        self.show_problem(0)
        self.set_mode("summary" if n >= problemgroups.MANY else "pages")
        if n > 1:
            queued = set()
            if self.queue is not None:
                queued = {pkg_ident(a) for a in self.queue.of("install")}
            run_async(problemgroups.analyse, self.items, queued, on_done=self._analysed)

    # ------------------------------------------------------------------ the summary by cause
    def _build_summary(self, parent):
        w = QWidget(parent)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.lead = qlabel(_("Looking at what causes them…"))
        v.addWidget(self.lead)
        self.cards = QWidget(w)
        self.cards_lay = QVBoxLayout(self.cards)
        self.cards_lay.setContentsMargins(0, 0, 0, 0)
        self.cards_lay.setSpacing(8)
        self.scroll = QScrollArea(w)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.cards)
        self.scroll.setFixedHeight(0)
        v.addWidget(self.scroll)
        v.addWidget(heading(_("What do you want to do?"), 1.05, w))
        self.choice = QButtonGroup(w)
        self.choice.setExclusive(True)
        n = len(self.items)
        self.c_wait = ChoiceCard("wait", _("Wait and try again tomorrow"),
                                 _("Zypp Desk will remind you tomorrow morning. Until then nothing changes."),
                                 self.choice, w)
        self.c_keep = ChoiceCard("keep", ngettext("Update the rest and leave this one as it is",
                                                  "Update the rest and leave these {n} as they are", n).format(n=n),
                                 _("Looking at what causes them…"), self.choice, w)
        self.c_one = ChoiceCard("one", _("Look at them one by one"),
                                _("For those who know zypper: each problem with the solutions zypper offers."),
                                self.choice, w)
        self.c_keep.radio.setEnabled(False)
        for c in (self.c_wait, self.c_keep, self.c_one):
            v.addWidget(c)
        self.c_wait.radio.setChecked(True)  # the safe answer, until the causes are known
        self.choice.buttonClicked.connect(self._choice_clicked)
        self.choice.buttonToggled.connect(lambda *_a: self._update_apply())
        return w

    def _choice_clicked(self, *_a):
        self._touched = True

    def _choice_key(self):
        for c in (self.c_wait, self.c_keep, self.c_one):
            if c.radio.isChecked():
                return c.key
        return None

    def _analysed(self, result, err):
        if self._closed:
            return
        try:
            if err is not None or not isinstance(result, dict):
                result = problemgroups.fallback(self.items)
            self.analysis = result
            self._fill_summary()
        except RuntimeError:  # the window is already gone
            return

    def _fill_summary(self):
        groups, keeps = self.analysis["groups"], self.analysis["keeps"]
        n, k = len(self.items), len(groups)
        while self.cards_lay.count():
            it = self.cards_lay.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        for g in groups:
            self.cards_lay.addWidget(GroupCard(g, self._fit, self.cards))
        if all(g["kind"] == problemgroups.OTHER for g in groups):
            lead = _("Zypp Desk found no common cause for them:")
        elif k == 1 and n > 1:
            lead = _("They all come from one cause:")
        elif k < n:
            lead = ngettext("There are many, but they come from only {k} cause:",
                            "There are many, but they come from only {k} causes:", k).format(k=k)
        else:
            lead = _("These are their causes:")
        self.lead.setText(lead)
        can = [s for s in keeps if s]
        detail = problemgroups.keep_text(groups, keeps)
        locks = problemgroups.lock_count(keeps)
        if locks:
            detail += " " + ngettext("In all, {n} package is locked; you can unlock it later in Locks.",
                                     "In all, {n} packages are locked; you can unlock them later in Locks.",
                                     locks).format(n=locks)
        if not can:
            detail = _("Zypp Desk cannot leave these problems as they are: look at them one by one.")
        self.c_keep.set_text(self.c_keep.title.text(), detail)
        self.c_keep.radio.setEnabled(bool(can))
        if not self._touched:  # what is usually done
            waiting = any(g["kind"] in (problemgroups.BEHIND, problemgroups.THIRDPARTY) for g in groups)
            if waiting:
                self.c_wait.radio.setChecked(True)
            elif can and len(can) == len(keeps):
                self.c_keep.radio.setChecked(True)
            else:
                self.c_one.radio.setChecked(True)
        if self.mode == "pages" and k < n:
            self.to_summary.show()  # 2 or 3 problems with one cause: the summary is worth a look
        self._update_apply()
        self._fit()

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
                card = SolutionCard(sol, group, page, now=self.now, queue=self.queue)
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
            if good:
                more = _link(ngettext("{n} more option that cannot be chosen here",
                                      "{n} more options that cannot be chosen here", len(bad)).format(n=len(bad)),
                             page)
            else:
                more = _link(ngettext("Show zypper's solution", "Show zypper's {n} solutions", len(bad)).format(
                    n=len(bad)), page)
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
    def set_mode(self, mode):
        """'summary' (the problems grouped by cause) or 'pages' (one problem at a time)."""
        if mode == "summary" and self.summary is None:
            mode = "pages"
        self.mode = mode
        n = len(self.items)
        summary = mode == "summary"
        self.summary is not None and self.summary.setVisible(summary)
        for k, page in enumerate(self.pages):
            page.setVisible(not summary and k == self.current)
        for b in (self.prev, self.next):
            b.setVisible(not summary and n > 1)
        groups = (self.analysis or {}).get("groups")
        self.to_summary.setVisible(not summary and self.summary is not None and (
            n >= problemgroups.MANY or (groups is not None and len(groups) < n)))
        if summary:
            sources = list(dict.fromkeys(it.get("source") for it in self.items if it.get("source")))
            self.pos.setText(" · ".join(sources))
            self.pos.setVisible(bool(sources))
            self.raw.setPlainText("\n\n".join(it.get("raw", "") for it in self.items))
            self._update_apply()
            self._fit()
        else:
            self.pos.setVisible(True)
            self.show_problem(self.current)

    def show_problem(self, i):
        n = len(self.items)
        if not n:
            self.pos.setText(_("There is nothing to choose."))
            self.prev.setEnabled(False)
            self.next.setEnabled(False)
            return
        i = max(0, min(n - 1, i))
        self.current = i
        if self.mode != "pages":
            return
        for k, page in enumerate(self.pages):
            page.setVisible(k == i)
        text = _("{i} of {n}").format(i=i + 1, n=n)
        if self.items[i].get("source"):
            text += " · " + self.items[i]["source"]
        self.pos.setText(text)
        self.prev.setEnabled(i > 0)
        self.next.setEnabled(i < n - 1)
        self.raw.setPlainText(self.items[i].get("raw", ""))
        self._update_apply()
        self._fit()

    def _raw_toggled(self, on):
        self.raw.setVisible(on)
        self._fit()

    def _fit(self):
        """Grow or shrink to what is shown (the width stays); the list of causes scrolls when it is long."""
        def fit():
            try:
                if self.mode == "summary" and self.summary is not None:
                    cl = self.cards_lay
                    cl.activate()
                    width = self.scroll.viewport().width() or (self.width() - 44)
                    want = cl.totalHeightForWidth(width) if cl.hasHeightForWidth() else cl.totalSizeHint().height()
                    scr = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
                    self.scroll.setFixedHeight(max(0, min(want, int(scr.height() * 0.42))))
                for w in (self.summary, self._holder):  # the nested layouts first, or the window uses old sizes
                    if w is not None:
                        w.layout().invalidate()
                        w.layout().activate()
                        w.updateGeometry()
                lay = self.layout()
                lay.activate()
                h = (lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth()
                     else lay.totalSizeHint().height())
                self.resize(self.width(), max(h, lay.totalMinimumSize().height()))
                if self.isVisible():  # it grew when the causes arrived: keep its bottom on the screen
                    scr = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
                    geo = self.frameGeometry()
                    if geo.bottom() > scr.bottom():
                        self.move(geo.x(), max(scr.top(), scr.bottom() - geo.height()))
            except RuntimeError:  # the window is already gone
                pass
        fit()
        QTimer.singleShot(0, fit)

    def _plan_label(self, chosen):
        """What the apply button says for these solutions (when the caller gave no label)."""
        steps, add, discard = solution_plan(chosen, self.now)
        if steps and not (add or discard):
            return _("Do it now and simulate again")
        if steps:
            return _("Go ahead and simulate again")  # some of it runs now, the rest changes the queue
        if add and not discard:
            return _("Add to the queue and simulate again")
        return _("Change the queue and simulate again")  # something leaves the queue

    def _keeps(self):
        return [s for s in (self.analysis or {}).get("keeps") or [] if s]

    def _update_apply(self):
        if self.mode == "summary":
            key = self._choice_key()
            if key == "wait":
                self.apply.setText(_("Remind me tomorrow"))
                self.apply.setEnabled(True)
            elif key == "keep":
                keeps = self._keeps()
                self.apply.setEnabled(bool(keeps))
                self.apply.setText(self.apply_label or (self._plan_label(keeps) if keeps else _("Go ahead")))
            else:
                self.apply.setText(_("Look at them one by one"))
                self.apply.setEnabled(True)
            return
        groups = [g for g in self.groups if g]
        self.apply.setEnabled(bool(groups) and all(g.checkedButton() is not None for g, _c in groups))
        chosen = self._page_choices()
        if self.apply_label or not chosen:
            self.apply.setText(self.apply_label or _("Add to the queue and simulate again"))
            return
        self.apply.setText(self._plan_label(chosen))

    def _go(self):
        """The primary button: what it does depends on the page and the answer."""
        if self.mode == "summary":
            key = self._choice_key()
            if key == "wait":
                remind_tomorrow(self)
                self.wait_requested = True
                self.reject()
            elif key == "keep":
                keeps = self._keeps()
                if keeps:
                    self._keep = keeps
                    self.accept()
            else:
                self.set_mode("pages")
            return
        self._keep = None
        self.accept()

    def done(self, r):
        self._closed = True
        super().done(r)

    def _page_choices(self):
        out = []
        for g in self.groups:
            if g and g[0].checkedButton() is not None:
                out.append(g[1][g[0].checkedButton()])
        return out

    def chosen(self):
        """The chosen solution of every problem that has solutions, in the order of the problems; after "Update
        the rest and leave these as they are", the solution that leaves each problem as it is."""
        if self._keep is not None:
            return list(self._keep)
        return self._page_choices()
