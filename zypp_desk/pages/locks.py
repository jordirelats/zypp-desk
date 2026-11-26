# SPDX-License-Identifier: GPL-3.0-or-later
"""Locks: every lock of /etc/zypp/locks with its package's icon on the left and a card for the chosen one on the
right, like Packages (mockup seccions-v1-B-bloquejos.png; Jordi, 26/11/2026: "bloquejos haurien de sortir les
icones i ser com les altres pàgines"). "Lock a package…" opens a search where several can be ticked and locked at
once, and where a name with wildcards (vlc-*) can be locked as it is.

The window stays light (Jordi's computer froze when many zypper processes started at once): the list needs one
zypper search for the summaries of every lock, and the card asks zypper about the chosen lock only, once the
selection stops moving. Nothing changes the system here: locking and unlocking go to the queue.
"""

import fnmatch
import re

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from .. import appicons, backend
from ..i18n import _, ngettext, pgettext
from ..runner import Step, zypper_subcommand
from ..style import BLUE, GREEN, ORANGE, PURPLE
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Card, Page, RoomSplitter, button, heading, icon, mono, muted
from .markbar import MarkBar, check_rect, is_checked, paint_check
from .system import ask_clean_locks

GREY = "#7f8c8d"
ROW_ROLE = Qt.ItemDataRole.UserRole
NO_REPO = ("(System Packages)", "@System")  # what zypper calls an installed package no repository carries
SEARCH_LIMIT = 300  # rows of the search in "Lock a package…" ("lib" finds thousands)
NOTE_CSS = {  # the sentence under the name: tinted, readable in light and dark themes
    "orange": "QFrame#locknote { background: rgba(246, 116, 0, 0.12); border-radius: 8px; }",
    "green": "QFrame#locknote { background: rgba(39, 174, 96, 0.13); border-radius: 8px; }",
    "blue": "QFrame#locknote { background: rgba(29, 153, 243, 0.12); border-radius: 8px; }",
}
BAND_CSS = "QFrame#tempband { background: rgba(246, 116, 0, 0.12); border-radius: 10px; }"


def kind_label(kind):
    return {"package": _("package"), "pattern": _("pattern"), "patch": _("patch"), "product": _("product"),
            "srcpackage": _("source package")}.get(kind, kind)


def is_pattern(name):
    """True for a lock written with wildcards (vlc-*): it is for every name that matches."""
    return any(c in (name or "") for c in "*?[")


def name_of(spec):
    """The name of 'tool < 2.0' (a lock with a version range): 'tool'."""
    return re.split(r"\s*[<>=!]", spec or "", maxsplit=1)[0].strip() or spec


def in_range(rng, edition):
    """True when this edition is one of the versions a lock's range covers ('< 2.0', '= 1.0-1'), or when the lock
    has no range. A range written without a release compares the version alone, as zypper does."""
    if not rng or not edition:
        return True
    flag, _sp, want = rng.partition(" ")
    want = want.strip()
    ed = edition
    if "-" not in want:
        ed = ed.rpartition("-")[0] or ed
    c = backend.vercmp(ed, want)
    return {"<": c < 0, "<=": c <= 0, "=": c == 0, "==": c == 0, ">=": c >= 0, ">": c > 0,
            "!=": c != 0}.get(flag, True)


def icon_candidates(name):
    """Names to look for an icon under: 'vlc-codecs', then 'vlc'; for 'vlc-*', 'vlc' (a library keeps its own)."""
    base = re.split(r"[*?\[]", name or "", maxsplit=1)[0].rstrip("-_.")
    if not base:
        return []
    out = [base]
    if not base.startswith("lib"):
        parts = base.split("-")
        out += ["-".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]
    return out


def lock_command(kind, lk):
    """The command the queue runs for this lock, as text: 'zypper -n removelock -- geeqie' (with -t and -r when the
    lock has them: zypper finds a lock only as it is written)."""
    typ, repo = lk.get("type") or "package", lk.get("repo") or ""
    sub = "addlock" if kind == "lock" else "removelock"
    return Step("zypper", ["-n", sub, *([] if typ == "package" else ["-t", typ]), *(["-r", repo] if repo else []),
                           "--", lk["spec"]]).display()


def lock_summaries(locks):
    """{(name, kind): summary} of the locked names, with one 'zypper search' per kind (not one per lock). Names
    with wildcards are left out: they are about many packages."""
    by_kind = {}
    for lk in locks:
        if not is_pattern(lk["name"]):
            by_kind.setdefault(lk["type"] or "package", set()).add(lk["name"])
    out = {}
    for kind, names in by_kind.items():
        root, code = backend.xml(["search", "-t", kind, "--match-exact", "--", *sorted(names)])
        if code == backend.EXIT_NO_MATCH:
            continue
        for s in root.iter("solvable"):
            out.setdefault((s.get("name"), s.get("kind") or kind), s.get("summary", ""))
    return out


def read_locks(settings):
    """Everything the list needs, in one go (in a thread): the locks, the temporary ones Zypp Desk made, the names of
    the repositories (only when a lock or a record names one), the summaries and the icons of installed packages."""
    from .. import problemgroups
    locks = backend.locks()
    repos = []
    if any(lk["repo"] for lk in locks) or settings.get("temp_locks"):
        try:
            repos = backend.repos()
        except Exception:  # only names: the list of locks matters more
            repos = []
    try:
        temp = problemgroups.temp_locks(settings, locks, repos)
    except Exception:  # only a note: the list of locks matters more
        temp = []
    try:
        summaries, ok = lock_summaries(locks), True
    except Exception:
        summaries, ok = {}, False
    names = [lk["name"] for lk in locks if not is_pattern(lk["name"]) and (lk["type"] or "package") == "package"]
    try:
        desktop = appicons.desktop_icons(names)
    except Exception:
        desktop = {}
    return dict(locks=locks, temp=temp, repos={r["alias"]: r["name"] for r in repos}, summaries=summaries,
                summaries_ok=ok, searched={(lk["name"], lk["type"] or "package") for lk in locks
                                           if not is_pattern(lk["name"])}, desktop=desktop)


def lock_facts(lk):
    """The packages a lock is about, as backend.search returns them (in a thread: one search, two zypper runs)."""
    name, kind = lk["name"], lk.get("type") or "package"
    if is_pattern(name):
        low = name.lower()
        return [d for d in backend.search(name, kind=kind) if fnmatch.fnmatchcase(d["name"].lower(), low)]
    found = backend.search(name, kind=kind, exact=True)
    return [d for d in found if d["name"] == name] or found


def set_primary(b, on):
    b.setProperty("primary", bool(on))
    b.style().unpolish(b)
    b.style().polish(b)


def plain_label(text="", parent=None, wrap=True):
    lbl = QLabel(text, parent)
    lbl.setTextFormat(Qt.TextFormat.PlainText)  # names and summaries come from the repositories: never rich text
    lbl.setWordWrap(wrap)
    return lbl


def row_background(option, odd):
    if option.state & QStyle.StateFlag.State_Selected:
        return QColor(39, 174, 96, 60)
    if option.state & QStyle.StateFlag.State_MouseOver:
        return QColor(39, 174, 96, 22)
    return QColor(127, 140, 141, 20) if odd else None


def draw_pills(p, r, pills, font, room):
    """Rounded pills from the right edge of r while they fit in 'room' pixels; returns where they start."""
    small = QFont(font)
    small.setPointSizeF(font.pointSizeF() * 0.85)
    fm = QFontMetrics(small)
    right = r.right() - 10
    for text, color in pills:
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, 200)
        pw = fm.horizontalAdvance(text) + 18
        if r.right() - 10 - (right - pw) > room:
            break
        pr = QRect(right - pw, r.center().y() - 10, pw, 20)
        fill = QColor(color)
        fill.setAlpha(38)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(pr, 10, 10)
        p.setPen(QColor(color))
        p.setFont(small)
        p.drawText(pr, Qt.AlignmentFlag.AlignCenter, text)
        right = pr.left() - 6
    return right


def draw_two_lines(p, option, x, r, right, title, sub):
    """The name in bold and a grey line under it, cut with '…' before the pills."""
    room = max(20, right - x - 8)
    bold = QFont(option.font)
    bold.setBold(True)
    p.setFont(bold)
    p.setPen(option.palette.text().color())
    p.drawText(QRect(x, r.top() + 6, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
               QFontMetrics(bold).elidedText(title, Qt.TextElideMode.ElideRight, room))
    p.setFont(option.font)
    p.setPen(option.palette.placeholderText().color())
    p.drawText(QRect(x, r.top() + 26, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
               QFontMetrics(option.font).elidedText(sub, Qt.TextElideMode.ElideRight, room))


# ---------------------------------------------------------------------------------------------- the list

class LockDelegate(QStyledItemDelegate):
    """A row: the package's icon, the name in bold with its summary below, pills on the right."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(200, 56)

    def paint(self, p, option, index):
        e = self.page.entry_at(index.data(ROW_ROLE))
        if e is None:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(2, 2, -2, -2)
        bg = row_background(option, e.get("odd"))
        if bg is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 9, 9)
        lk = e["lock"]
        self.page.lock_icon(lk["name"]).paint(p, QRect(r.left() + 12, r.center().y() - 16, 32, 32))
        right = draw_pills(p, r, self.page.pills(e), option.font, max(90, int(r.width() * 0.45)))
        draw_two_lines(p, option, r.left() + 58, r, right, lk["spec"], self.page.subtitle(e))
        p.restore()


# ---------------------------------------------------------------------------------------------- the card

class LockCard(QWidget):
    """The chosen lock: icon, name, summary, what the lock means, what you have and what it holds back, how it is
    written, and the buttons (Unlock, Show in Packages) with the command under them."""

    ROWS = ("now", "new", "avail", "scope", "kind", "versions", "origin")

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.entry = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(14)
        self.icon = QLabel(self)
        self.icon.setFixedSize(48, 48)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.name = heading("", 1.45, self)
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setWordWrap(True)
        self.summary = muted("", self)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        names.addWidget(self.name)
        names.addWidget(self.summary)
        top.addLayout(names, 1)
        v.addLayout(top)
        v.addSpacing(14)

        self.note = QFrame(self)
        self.note.setObjectName("locknote")
        nh = QHBoxLayout(self.note)
        nh.setContentsMargins(12, 9, 12, 9)
        nh.setSpacing(8)
        self.note_icon = QLabel(self.note)
        self.note_icon.setFixedSize(16, 16)
        nh.addWidget(self.note_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.note_text = plain_label("", self.note)
        nh.addWidget(self.note_text, 1)
        v.addWidget(self.note)
        v.addSpacing(14)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(1, 1)
        self.rows = {}
        labels = {"now": _("Now"), "new": _("New version"), "avail": _("In your repositories"),
                  # TRANSLATORS: a row of the lock's card (a noun: what the lock covers), not the button
                  "scope": pgettext("noun", "Lock"), "kind": _("Type"), "versions": _("Versions"),
                  "origin": _("Origin")}
        for i, key in enumerate(self.ROWS):
            lbl = muted(labels[key], self, wrap=False)
            val = plain_label("", self)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.grid.addWidget(lbl, i, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(val, i, 1)
            self.rows[key] = (lbl, val)
        v.addLayout(self.grid)
        v.addSpacing(16)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.b_main = button(_("Unlock"), None, self._main, primary=True, parent=self)
        self.b_show = button(_("Show in Packages"), None, self._show, parent=self)
        btns.addWidget(self.b_main)
        btns.addWidget(self.b_show)
        btns.addStretch(1)
        v.addLayout(btns)
        v.addSpacing(12)
        self.cmd = plain_label("", self)
        f = mono()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        self.cmd.setFont(f)
        self.cmd.setStyleSheet("color: palette(placeholder-text);")
        self.cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.cmd)
        v.addStretch(1)
        self.show_message(_("Loading…"), "")

    # ------------------------------------------------------------ states
    def show_message(self, title, text):
        self.entry = None
        self.icon.setPixmap(icon("object-locked").pixmap(48, 48))
        self.name.setText(title)
        self.summary.setText(text)
        self.summary.setVisible(bool(text))
        self.note.hide()
        for key in self.ROWS:
            self._row(key, "")
        self.b_main.hide()
        self.b_show.hide()
        self.cmd.setText("")

    def _row(self, key, text):
        lbl, val = self.rows[key]
        lbl.setVisible(bool(text))
        val.setVisible(bool(text))
        val.setText(text or "")

    def _note(self, text, tone, icon_name):
        self.note_text.setText(text)
        self.note.setStyleSheet(NOTE_CSS[tone])
        self.note_icon.setPixmap(icon(icon_name, "object-locked").pixmap(16, 16))
        self.note.show()

    def refresh_icon(self):
        if self.entry is not None:
            self.icon.setPixmap(self.page.lock_icon(self.entry["lock"]["name"]).pixmap(48, 48))

    def show_entry(self, e):
        """What is known without asking zypper: the lock as it is written and what is queued for it."""
        self.entry = e
        lk, pend = e["lock"], e["pending"]
        self.refresh_icon()
        self.name.setText(lk["spec"])
        self.summary.setText(self.page.subtitle(e))
        self.summary.show()
        pattern = is_pattern(lk["name"])
        if pend == "lock":
            self._note(_("It will be locked when you apply the queue. Until then, nothing changes."), "blue",
                       "object-locked")
        elif pend == "unlock":
            self._note(_("It will be unlocked when you apply the queue. Then zypper may install, update or remove "
                         "it again."), "green", "object-unlocked")
        elif pattern:
            self._note(_("Locked: zypper does not install, remove or update any package whose name matches it "
                         "until you unlock it."), "orange", "object-locked")
        else:
            self._note(_("Locked: zypper does not install, remove or update it until you unlock it."), "orange",
                       "object-locked")
        self._row("now", _("Loading…"))
        self._row("new", "")
        self._row("avail", "")
        repo = lk.get("repo") or ""
        self._row("scope", _("only from the repository {repo}").format(repo=self.page.repo_name(repo)) if repo
                  else _("from any repository"))
        self._row("kind", kind_label(lk.get("type") or "package"))
        self._row("versions", _("only {range}").format(range=lk["range"]) if lk.get("range") else "")
        self._row("origin", self.page.origin_text(e))
        if pend == "lock":
            self.b_main.setText(_("Do not lock it"))
        elif pend == "unlock":
            self.b_main.setText(_("Keep the lock"))
        else:
            self.b_main.setText(_("Unlock"))
        set_primary(self.b_main, not pend)
        self.b_main.show()
        self.b_show.show()
        self.cmd.setText(lock_command("lock" if pend == "lock" else "unlock", lk))

    def show_facts(self, e, found):
        """What zypper said about the packages of the lock (lock_facts), or the error it gave."""
        if self.entry is None or self.page.key_of(self.entry) != self.page.key_of(e):
            return
        lk = e["lock"]
        if isinstance(found, Exception):
            self._row("now", _("Could not be read: {error}").format(error=str(found)[:300]))
            return
        if is_pattern(lk["name"]):
            self._pattern_facts(lk, found)
            return
        d = found[0] if found else None
        if d is None:
            self._row("now", _("not installed"))
            self._row("avail", _("no package has this name now: “Clean useless locks” takes this lock away"))
            return
        if d.get("summary"):
            self.summary.setText(d["summary"])
        inst = d.get("installed")
        repo = lk.get("repo") or ""
        # zypper search names a repository by its name, a lock by its alias
        want = {repo, self.page.repo_name(repo)} if repo else None
        cands = [v for v in d.get("versions") or [] if v["repo"] not in NO_REPO and (not want or v["repo"] in want)
                 and not (inst and v["edition"] == inst["edition"] and v["arch"] == inst["arch"])]
        newest = None
        for v in cands:
            if newest is None or backend.vercmp(v["edition"], newest["edition"]) > 0:
                newest = v
        rng = lk.get("range") or ""
        if inst:
            self._row("now", _("installed, {version}").format(version=inst["edition"]))
            if newest and backend.vercmp(newest["edition"], inst["edition"]) > 0:
                held = in_range(rng, inst["edition"]) or in_range(rng, newest["edition"])
                self._row("new", self._held_text(newest["edition"], held))
            else:
                self._row("new", _("none: you have the newest one"))
        else:
            self._row("now", _("not installed"))
            if newest:
                self._row("avail", self._held_text(newest["edition"], in_range(rng, newest["edition"])))
            elif repo:
                self._row("avail", _("none in the repository {repo}").format(repo=self.page.repo_name(repo)))
            else:
                self._row("avail", _("no package has this name now: “Clean useless locks” takes this lock away"))

    @staticmethod
    def _held_text(edition, held):
        if held:
            return _("{version} (the lock keeps it from being installed)").format(version=edition)
        return _("{version} (not one of the locked versions: zypper may install it)").format(version=edition)

    def _pattern_facts(self, lk, found):
        def names(ds):
            shown = [d["name"] for d in ds[:6]]
            return ", ".join(shown) + (", …" if len(ds) > 6 else "")
        inst = [d for d in found if d.get("installed")]
        if inst:
            self._row("now", ngettext("{n} installed package matches it: {names}",
                                      "{n} installed packages match it: {names}", len(inst)).format(
                n=len(inst), names=names(inst)))
        else:
            self._row("now", _("no installed package matches it"))
        if found:
            self._row("avail", ngettext("{n} package matches it: {names}", "{n} packages match it: {names}",
                                        len(found)).format(n=len(found), names=names(found)))
        else:
            self._row("avail", _("no package matches it now: “Clean useless locks” takes this lock away"))

    # ------------------------------------------------------------ buttons
    def _main(self):
        e = self.entry
        if e is None:
            return
        if e["pending"] == "lock":
            self.page.dont_lock(e)
        elif e["pending"] == "unlock":
            self.page.keep_lock(e)
        else:
            self.page.unlock(e)

    def _show(self):
        if self.entry is not None:
            self.page.show_in_packages(self.entry)


# ---------------------------------------------------------------------------------------------- the page

class LocksPage(Page):
    key = "locks"
    title = _("Locks")
    icon_name = "object-locked"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.items = []       # backend.locks()
        self.temp = []        # the records of the locks an earlier "leave these as they are" made
        self.entries = []     # what the list shows: the locks and the locks waiting in the queue
        self.repo_names = {}  # alias -> name
        self.summaries = {}   # (name, kind) -> summary
        self.searched = set()  # the (name, kind) the summaries were looked for
        self.summaries_ok = False
        self.desktop = {}     # name -> icon of an installed package (appicons.desktop_icons)
        self.facts = {}       # key_of(entry) -> lock_facts(); only for the locks that were chosen
        self._icons = {}
        self._round = 0
        self._want = None     # the lock to choose after the next fill (one just queued)
        self._stale = False
        self._catalog_tries = 0

        lay = QVBoxLayout(self)
        # the locks an earlier "leave these as they are" made (problem window): say so, and unlock them in one go
        self.temp_band = QFrame(self)
        self.temp_band.setObjectName("tempband")
        self.temp_band.setStyleSheet(BAND_CSS)
        tb = QHBoxLayout(self.temp_band)
        tb.setContentsMargins(14, 10, 12, 10)
        tb.setSpacing(12)
        self.temp_text = plain_label("", self.temp_band)
        tb.addWidget(self.temp_text, 1)
        self.temp_btn = button(_("Unlock them"), "object-unlocked", self.unlock_temp, parent=self.temp_band)
        tb.addWidget(self.temp_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.temp_band.setVisible(False)
        lay.addWidget(self.temp_band)

        left = Card(None, self)
        left.lay.setContentsMargins(10, 12, 10, 10)
        left.lay.setSpacing(6)
        self.count = muted(_("Reading the locks…"), left)
        self.count.setTextFormat(Qt.TextFormat.PlainText)
        self.count.setContentsMargins(6, 0, 6, 0)
        left.lay.addWidget(self.count)
        self.list = QListWidget(left)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(LockDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setStyleSheet("QListWidget { background: transparent; }")
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._chosen(cur))
        left.lay.addWidget(self.list, 1)
        self.empty = muted(_("A locked package is never installed, removed or updated by any zypper command "
                             "until you unlock it.") + "\n\n" +
                           _("“Lock a package…” finds packages and locks them."), left)
        self.empty.setContentsMargins(6, 0, 6, 0)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.empty.hide()
        left.lay.addWidget(self.empty, 1)

        right = Card(None, self)
        right.lay.setContentsMargins(18, 18, 18, 14)
        self.card = LockCard(self, right)
        right.lay.addWidget(self.card)

        split = RoomSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 11)
        split.setStretchFactor(1, 10)
        split.setSizes([550, 500])
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)

        # the header buttons live as long as the page (the window takes them away and puts them back)
        self.b_lock = button(_("Lock a package…"), "object-locked", self.add)
        self.b_clean = button(_("Clean useless locks"), "edit-clear", self.clean_locks)

        self._fill_timer = QTimer(self)  # the queue changes in bursts: draw the list once
        self._fill_timer.setSingleShot(True)
        self._fill_timer.timeout.connect(self._fill)
        self._facts_timer = QTimer(self)  # zypper is asked only when the selection stops moving
        self._facts_timer.setSingleShot(True)
        self._facts_timer.setInterval(300)
        self._facts_timer.timeout.connect(self._load_facts)
        self._icons_timer = QTimer(self)  # the icon catalogue is read once, at start: it may not be there yet
        self._icons_timer.setSingleShot(True)
        self._icons_timer.setInterval(2000)
        self._icons_timer.timeout.connect(self._catalog_check)
        ctx.queue.changed.connect(self._queue_changed)
        # 'zypper removelock' ends with 0 also when it found no such lock: that is a failure here
        runner = getattr(ctx, "runner", None)
        if runner is not None and hasattr(runner, "step_finished"):
            runner.step_finished.connect(self._step_finished)

    def header_actions(self):
        return [self.b_lock, self.b_clean]

    # ------------------------------------------------------------ loading
    def reload(self):
        self._round += 1
        rnd = self._round
        if not self.items:
            self.count.setText(_("Reading the locks…"))
        run_async(read_locks, self.ctx.settings, on_done=lambda res, err: self._loaded(rnd, res, err))

    def _loaded(self, rnd, res, err):
        if rnd != self._round:
            return  # an older reload answered late
        if err:
            self.count.setText(_("The locks could not be read: {error}").format(error=str(err)[:300]))
            if not self.items:
                self.card.show_message(_("The locks could not be read."), "")
            return
        self.items, self.temp = res["locks"], res["temp"]
        self.repo_names = res["repos"]
        self.summaries, self.searched, self.summaries_ok = res["summaries"], res["searched"], res["summaries_ok"]
        self.desktop.update(res["desktop"])
        self.facts = {}  # what zypper knows may have changed (after Apply, a refresh…)
        self._icons = {}
        # records of temporary locks that are gone (unlocked here or by hand) are forgotten
        from .. import problemgroups
        names = {lk["name"] for lk in self.items}
        gone = [r.get("spec") for r in self.ctx.settings.get("temp_locks") or []
                if isinstance(r, dict) and r.get("spec") not in names]
        if gone:
            try:
                problemgroups.forget_temp_locks(self.ctx.settings, gone)
            except OSError:
                pass
        note = problemgroups.temp_locks_note(self.temp) if self.temp else ""
        self.temp_text.setText(note)
        self.temp_band.setVisible(bool(note))
        self._fill()
        if not getattr(self.ctx, "icon_catalog", None):
            self._catalog_tries = 0
            self._icons_timer.start()

    def _catalog_check(self):
        if getattr(self.ctx, "icon_catalog", None):
            self.refresh_icons()
        elif self._catalog_tries < 15:
            self._catalog_tries += 1
            self._icons_timer.start()

    def refresh_icons(self):
        """Look for the icons again (the window calls it when the catalogue of icons is read)."""
        self._icons = {}
        self.list.viewport().update()
        self.card.refresh_icon()

    # ------------------------------------------------------------ what each lock is
    @staticmethod
    def _same(action, lock):
        """True if a queued unlock is for this lock (the same text, kind and repository)."""
        def norm(text):
            return "".join(text.split())
        return (norm(action.target) == norm(lock["spec"]) and action.opts.get("type", "package") == lock["type"]
                and action.opts.get("repo", "") == lock["repo"])

    @staticmethod
    def key_of(e):
        lk = e["lock"]
        return "".join(lk["spec"].split()), lk.get("type") or "package", lk.get("repo") or ""

    def _entries(self):
        unlocks = self.ctx.queue.of("unlock")
        out, specs = [], set()
        for lk in self.items:
            specs.add((lk["spec"], lk["type"], lk["repo"]))
            pend = next((a for a in unlocks if self._same(a, lk)), None)
            rec = next((r for r in self.temp if r.get("spec") in (lk["name"], lk["spec"])), None)
            out.append(dict(lock=lk, pending="unlock" if pend else "", action=pend, temp=rec))
        for a in self.ctx.queue.of("lock"):
            typ, repo = a.opts.get("type", "package"), a.opts.get("repo", "")
            if (a.target, typ, repo) in specs:
                continue
            name = name_of(a.target)
            lk = dict(number="", name=name, range=a.target[len(name):].strip(), spec=a.target, type=typ, repo=repo)
            out.append(dict(lock=lk, pending="lock", action=a, temp=None))
        return out

    def entry_at(self, i):
        return self.entries[i] if isinstance(i, int) and 0 <= i < len(self.entries) else None

    def repo_name(self, alias):
        return self.repo_names.get(alias) or alias

    def subtitle(self, e):
        """The grey line under the name: the package's summary."""
        lk = e["lock"]
        if is_pattern(lk["name"]):
            return _("every package whose name matches it")
        k = (lk["name"], lk.get("type") or "package")
        if self.summaries.get(k):
            return self.summaries[k]
        if self.summaries_ok and k in self.searched:
            return _("no package has this name now")
        return kind_label(k[1])

    def pills(self, e):
        lk = e["lock"]
        out = []
        if e["pending"] == "unlock":
            out.append((_("will be unlocked"), GREEN))
        elif e["pending"] == "lock":
            out.append((_("will be locked"), BLUE))
        if e["temp"]:
            out.append((_("Zypp Desk · temporary"), ORANGE))
        elif lk.get("repo"):
            out.append((self.repo_name(lk["repo"]), BLUE))
        else:
            out.append((_("any repository"), GREY))
        if (lk.get("type") or "package") != "package":
            out.append((kind_label(lk["type"]), PURPLE))
        return out

    def origin_text(self, e):
        rec = e["temp"]
        if not rec:
            return ""
        from ..problemgroups import _date
        text = _("locked by Zypp Desk on {date}").format(date=_date(rec.get("added") or 0))
        if rec.get("repo_name"):
            text += f" ({rec['repo_name']})"
        if rec.get("caught_up") and rec.get("repo_name"):
            text += "\n" + _("{repo} has published since then: you can probably unlock it.").format(
                repo=rec["repo_name"])
        return text

    def lock_icon(self, name):
        """The package's own icon (the catalogue of icons, or the .desktop file of an installed package; for
        vlc-codecs or vlc-*, VLC's), or the Packages page's generic one."""
        ic = self._icons.get(name)
        if ic is not None:
            return ic
        cands = icon_candidates(name)
        for cand in cands:
            ic = self._found_icon(cand)
            if ic is not None:
                self._icons[name] = ic
                return ic
        pk = (getattr(self.ctx, "pages", None) or {}).get("packages")
        if pk is not None and hasattr(pk, "pkg_icon"):
            try:
                return pk.pkg_icon({"name": cands[0] if cands else name})  # not kept: a better one may come later
            except Exception:
                pass
        return icon("package-x-generic")

    def _found_icon(self, name):
        found = (getattr(self.ctx, "icon_catalog", None) or {}).get(name)
        ic = QIcon()
        if found and found[0] == "file":
            ic = QIcon(found[1])
        elif found and found[0] == "theme":
            ic = QIcon.fromTheme(found[1])
        if ic.isNull() and self.desktop.get(name):
            d = self.desktop[name]
            ic = QIcon(d) if d.startswith("/") else QIcon.fromTheme(d)
        return None if ic.isNull() else ic

    # ------------------------------------------------------------ the list
    def _queue_changed(self):
        if self.isVisible():
            self._fill_timer.start(0)
        else:
            self._stale = True

    def activated(self):
        if self._stale and self.loaded:
            self._stale = False
            self._fill()
        super().activated()

    def _current_key(self):
        e = self.entry_at(self.list.currentItem().data(ROW_ROLE)) if self.list.currentItem() else None
        return self.key_of(e) if e else None

    def _fill(self):
        want = self._want or self._current_key()
        self._want = None
        self.entries = self._entries()
        self.list.blockSignals(True)
        self.list.clear()
        chosen = None
        for i, e in enumerate(self.entries):
            e["odd"] = i % 2 == 1
            lk = e["lock"]
            it = QListWidgetItem(lk["spec"], self.list)  # the delegate paints the row; the text is for typing to
            it.setData(ROW_ROLE, i)                       # find a lock and for screen readers
            if lk.get("range"):
                it.setToolTip(_("Locked only in these versions: {range}").format(range=lk["range"]))
            if want is not None and self.key_of(e) == want:
                chosen = it
        self.list.blockSignals(False)
        n = len(self.items)
        n_lock = sum(1 for e in self.entries if e["pending"] == "lock")
        n_unlock = sum(1 for e in self.entries if e["pending"] == "unlock")
        if self.entries:
            parts = [ngettext("{n} lock", "{n} locks", n).format(n=n)]
            if n_lock:
                parts.append(ngettext("{n} will be locked", "{n} will be locked", n_lock).format(n=n_lock))
            if n_unlock:
                parts.append(ngettext("{n} will be unlocked", "{n} will be unlocked", n_unlock).format(n=n_unlock))
            self.count.setText(" · ".join(parts))
        else:
            self.count.setText(_("You have no locks."))
        self.list.setVisible(bool(self.entries))
        self.empty.setVisible(not self.entries)
        if chosen is None and self.list.count():
            chosen = self.list.item(0)
        if chosen is not None:
            before = self.list.currentItem()
            self.list.setCurrentItem(chosen)
            if before is chosen:  # no signal: the card still has to show what changed
                self._chosen(chosen)
        else:
            self.card.show_message(_("Nothing is locked"), _("zypper may install, update or remove any package."))

    def _chosen(self, item):
        e = self.entry_at(item.data(ROW_ROLE)) if item is not None else None
        if e is None:
            return
        self.card.show_entry(e)
        k = self.key_of(e)
        if k in self.facts:
            self.card.show_facts(e, self.facts[k])
            self._facts_timer.stop()
        else:
            self._facts_timer.start()  # the query waits until the selection stops moving

    def _load_facts(self):
        item = self.list.currentItem()
        e = self.entry_at(item.data(ROW_ROLE)) if item is not None else None
        if e is None:
            return
        k = self.key_of(e)
        if k in self.facts:
            self.card.show_facts(e, self.facts[k])
            return
        run_async(lock_facts, dict(e["lock"]), on_done=lambda res, err: self._facts_loaded(k, e, res, err))

    def _facts_loaded(self, k, e, res, err):
        if err is None:
            self.facts[k] = res or []
            lk = e["lock"]
            if res and not is_pattern(lk["name"]) and res[0].get("summary"):
                self.summaries[(lk["name"], lk.get("type") or "package")] = res[0]["summary"]
                self.list.viewport().update()
        cur = self.list.currentItem()
        now = self.entry_at(cur.data(ROW_ROLE)) if cur is not None else None
        if now is not None and self.key_of(now) == k:
            self.card.show_facts(now, err if err is not None else self.facts[k])

    # ------------------------------------------------------------ changes (to the queue)
    def unlock(self, e):
        # zypper finds a lock only as it is written: with its version range ("tool < 2.0"), its kind (-t) and, for
        # a lock limited to a repository, that repository (-r)
        lk = e["lock"]
        self.ctx.queue.add(Action("unlock", lk["spec"], type=lk["type"] or "package", repo=lk["repo"] or ""))

    def keep_lock(self, e):
        """Take a queued unlock of this lock out of the queue."""
        self.ctx.queue.discard_actions([a for a in self.ctx.queue.of("unlock") if self._same(a, e["lock"])])

    def dont_lock(self, e):
        """Take a queued lock out of the queue."""
        if e.get("action") is not None:
            self.ctx.queue.discard_action(e["action"])

    def unlock_temp(self):
        """Queue the unlock of every lock Zypp Desk made to leave a problem as it was (with the exact spec, kind
        and repository zypper needs to find it)."""
        temp = {r.get("spec") for r in self.temp}
        with self.ctx.queue.batch():
            for lk in self.items:
                if lk["name"] in temp or lk["spec"] in temp:
                    self.ctx.queue.add(Action("unlock", lk["spec"], type=lk["type"] or "package",
                                              repo=lk["repo"] or ""))

    def add(self):
        dlg = LockDialog(self, self.window())
        dlg.exec()
        if dlg.queued:
            self._want = ("".join(dlg.queued[0].split()), "package", "")
            self._fill_timer.start(0)
        dlg.deleteLater()

    def clean_locks(self):
        st = Step("zypper", ["cleanlocks"])
        if ask_clean_locks(self, st):
            self.ctx.run([st], _("Clean useless locks"), on_done=lambda ok: self.ctx.after_change())

    def show_in_packages(self, e):
        """The package in Packages: its name searched, with no filter that would hide it."""
        pk = (getattr(self.ctx, "pages", None) or {}).get("packages")
        if pk is None:
            return
        lk = e["lock"]
        self.ctx.go("packages")
        for combo, value in ((pk.kind, lk.get("type") or "package"), (pk.status, "all"), (pk.repo, lk.get("repo") or "")):
            i = combo.findData(value)
            if i >= 0:
                combo.setCurrentIndex(i)
        pk.term.setText(lk["name"])
        pk.search()

    def _step_finished(self, index, code):
        """After each step of any run: 'zypper removelock' that removed nothing says so and ends with 0. Make it
        a failed step, so that the run ends with errors and the unlock stays in the queue."""
        runner = self.ctx.runner
        try:
            st = runner.steps[index]
        except (AttributeError, IndexError, TypeError):
            return
        if code != 0 or st.program != "zypper" or zypper_subcommand(st.args) not in ("removelock", "rl"):
            return
        if not backend.no_lock_removed(st.output, getattr(st, "lang", None)):
            return
        st.state = "failed"
        runner.last_ok = False  # the next steps that need this one to succeed ('&&') are skipped
        runner.note.emit(_("No lock was removed: zypper found none written exactly like this. A lock can be for "
                           "some versions only, or for one repository: unlock it in Locks, which knows how it "
                           "is written."), "error")


# ---------------------------------------------------------------------------------------------- Lock a package…

class PickDelegate(QStyledItemDelegate):
    """A row of the search: a box to tick, the icon, the name and summary, and on the right whether you have it
    (or a pill when it is already locked or waiting in the queue). A click anywhere on the row ticks it."""

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dlg = dialog

    def sizeHint(self, option, index):
        return QSize(200, 52)

    def paint(self, p, option, index):
        row = self.dlg.row_at(index.data(ROW_ROLE))
        if row is None:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(2, 1, -2, -1)
        bg = row_background(option, row.get("odd"))
        if bg is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 9, 9)
        cr = check_rect(r, 10)
        if row["state"]:  # already locked, or waiting in the queue: ticked, and greyed out
            p.setOpacity(0.4)
            paint_check(p, cr, True, option.palette)
            p.setOpacity(1.0)
        else:
            paint_check(p, cr, is_checked(index.data(Qt.ItemDataRole.CheckStateRole)), option.palette)
        x = cr.right() + 12
        self.dlg.row_icon(row).paint(p, QRect(x, r.center().y() - 14, 28, 28))
        if row["state"] == "locked":
            right = draw_pills(p, r, [(_("locked"), GREY)], option.font, 200)
        elif row["state"] == "queued":
            right = draw_pills(p, r, [(_("will be locked"), BLUE)], option.font, 200)
        else:
            right = r.right() - 10
            if row.get("status"):
                fm = QFontMetrics(option.font)
                text = fm.elidedText(row["status"], Qt.TextElideMode.ElideRight, 200)
                w = fm.horizontalAdvance(text)
                p.setFont(option.font)
                p.setPen(option.palette.placeholderText().color())
                p.drawText(QRect(right - w, r.top(), w, r.height()),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
                right -= w + 8
        draw_two_lines(p, option, x + 40, r, right, row["name"], row["summary"])
        p.restore()

    def editorEvent(self, event, model, option, index):
        flags = index.flags()
        if not flags & Qt.ItemFlag.ItemIsUserCheckable or not flags & Qt.ItemFlag.ItemIsEnabled:
            return False
        t = event.type()
        if t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            self.dlg.toggle(index)
            return True
        if t == QEvent.Type.MouseButtonDblClick:
            return True  # a double click is two clicks already
        if t == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            self.dlg.toggle(index)
            return True
        return False


class LockDialog(QDialog):
    """'Lock a package…': search your repositories, tick one or several, and lock them (they go to the queue).
    A name with wildcards (vlc-*) is locked as it is, for every package whose name matches, now and later."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.rows = []
        self.queued = []  # what was added to the queue (the page chooses the first one)
        self._sid = 0
        self._closed = False  # a search (a thread) may answer after the window is gone
        self.setWindowTitle(_("Lock packages"))
        self.resize(720, 580)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        pic = QLabel(self)
        pic.setPixmap(icon("object-locked").pixmap(40, 40))
        top.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(heading(_("Which packages do you want to lock?"), 1.25, self))
        texts.addWidget(muted(_("A locked package is never installed, removed or updated by any zypper command "
                                "until you unlock it."), self))
        top.addLayout(texts, 1)
        v.addLayout(top)
        row = QHBoxLayout()
        self.term = QLineEdit(self)
        self.term.setPlaceholderText(_("Name of a package, or a pattern like vlc-*"))
        self.term.setClearButtonEnabled(True)
        self.term.returnPressed.connect(self.search)
        self.search_btn = button(_("Search"), "edit-find", self.search, parent=self)
        self.search_btn.setAutoDefault(False)
        row.addWidget(self.term, 1)
        row.addWidget(self.search_btn)
        v.addLayout(row)
        self.info = muted(_("Wildcards work too: vlc-* is every package whose name starts with vlc-."), self)
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.info)
        self.list = QListWidget(self)
        self.list.setItemDelegate(PickDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(lambda _it: self._update_markbar())
        v.addWidget(self.list, 1)
        self.markbar = MarkBar(self._mark_action, self._unmark_all, self)
        self.markbar.clear_btn.setAutoDefault(False)
        v.addWidget(self.markbar)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = button(_("Close"), None, self.reject, parent=self)
        close.setAutoDefault(False)
        bottom.addWidget(close)
        v.addLayout(bottom)
        self.term.setFocus()

    def done(self, r):
        self._closed = True
        super().done(r)

    def row_at(self, i):
        return self.rows[i] if isinstance(i, int) and 0 <= i < len(self.rows) else None

    def row_icon(self, row):
        return self.page.lock_icon(row["name"])

    def search(self):
        term = self.term.text().strip()
        if not term:
            self.term.setFocus()
            return
        self._sid += 1
        sid = self._sid
        self.info.setText(_("Searching…"))
        self.list.clear()
        self.rows = []
        self._update_markbar()
        run_async(backend.search, term, on_done=lambda res, err: self._searched(sid, term, res, err))

    def _searched(self, sid, term, res, err):
        if self._closed or sid != self._sid:
            return  # the window is gone, or an older search finished late
        results = res or []
        locked = {lk["name"] for lk in self.page.items if (lk["type"] or "package") == "package"} | \
                 {lk["spec"] for lk in self.page.items if (lk["type"] or "package") == "package"}
        queued = {a.target for a in self.page.ctx.queue.of("lock") if a.opts.get("type", "package") == "package"}

        def state(name):
            return "locked" if name in locked else "queued" if name in queued else ""

        rows = []
        pattern = is_pattern(term)
        if pattern or not any(d["name"] == term for d in results):
            # the name as you wrote it: a pattern, or a package your repositories do not have (yet)
            rows.append(dict(name=term, typed=True, state=state(term), status="", summary=(
                _("Every package whose name matches it, also the ones that come later") if pattern else
                _("No package has this name now: the lock keeps it from being installed if one comes later."))))
        low = term.lower()
        found = sorted(results, key=lambda d: (d["name"].lower() != low, d["name"].lower()))
        for d in found[:SEARCH_LIMIT]:
            inst = d.get("installed")
            rows.append(dict(name=d["name"], typed=False, state=state(d["name"]), summary=d.get("summary") or "",
                             status=_("installed, {version}").format(version=inst["edition"]) if inst else
                             _("not installed")))
        self.rows = rows
        self.list.blockSignals(True)
        for i, r in enumerate(rows):
            r["odd"] = i % 2 == 1
            it = QListWidgetItem(r["name"], self.list)  # painted by PickDelegate; the text is for screen readers
            it.setData(ROW_ROLE, i)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if not r["state"]:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            it.setFlags(flags)
            it.setCheckState(Qt.CheckState.Unchecked)
            if r["state"] == "locked":
                it.setToolTip(_("It is locked already."))
        self.list.blockSignals(False)
        if err is not None:
            self.info.setText(_("The search failed: {error}").format(error=str(err)[:200]))
        else:
            n = len(results)
            text = ngettext("{n} found", "{n} found", n).format(n=n)
            if n > SEARCH_LIMIT:
                text += " · " + _("showing the first {shown}").format(shown=SEARCH_LIMIT)
            self.info.setText(text)
        self._update_markbar()
        if self.list.count():
            self.list.setCurrentRow(0)

    def toggle(self, index):
        it = self.list.itemFromIndex(index)
        if it is None or not it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        it.setCheckState(Qt.CheckState.Unchecked if is_checked(it.checkState()) else Qt.CheckState.Checked)

    def marked(self):
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            row = self.row_at(it.data(ROW_ROLE))
            if row is not None and not row["state"] and is_checked(it.checkState()):
                out.append(row)
        return out

    def _unmark_all(self):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self.list.viewport().update()
        self._update_markbar()

    def _update_markbar(self):
        marked = self.marked()
        n = len(marked)
        names = [r["name"] for r in marked]
        detail = ", ".join(names[:5]) + (", …" if n > 5 else "")
        self.markbar.set_state(n, ngettext("{n} marked", "{n} marked", n).format(n=n), detail,
                               [("lock", ngettext("Lock it", "Lock them", n), True, True)])

    def _mark_action(self, key):
        if key != "lock":
            return
        marked = self.marked()
        if not marked:
            return
        with self.page.ctx.queue.batch():
            for r in marked:
                self.page.ctx.queue.add(Action("lock", r["name"]))
        self.queued = [r["name"] for r in marked]
        self.accept()
