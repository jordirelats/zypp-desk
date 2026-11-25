# SPDX-License-Identifier: GPL-3.0-or-later
"""History: what zypper changed, one transaction per row grouped by day, with the snapshots snapper took
around each one. On the right, the card of the chosen change: what changed, the exact command, and
(with snapper) comparing the files or undoing it. If the computer was started from an older snapshot, a
band on top offers to keep it (snapper rollback).

zypper's history and snapper's list can only be read by root, so nothing is loaded until asked for.
"""

from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, QEvent, QLocale, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
                               QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import snapper
from ..i18n import _, ngettext
from ..runner import PKEXEC_CANCELLED, Step
from ..style import BLUE, ORANGE
from ..tasks import run_async
from ..ui import Card, Page, button, error_box, heading, icon, info_box, mono, muted

ROW_ROLE = Qt.ItemDataRole.UserRole
GREY = "#7f8c8d"
ORANGE_BUTTON = (f"QPushButton {{ background: {ORANGE}; color: white; border: 1px solid #d86400; border-radius: 7px;"
                 f" padding: 6px 16px; font-weight: 600; }}"
                 f"QPushButton:hover {{ background: #ff8a1f; }}"
                 f"QPushButton:pressed {{ background: #d86400; }}"
                 f"QPushButton:disabled {{ background: palette(button); color: palette(placeholder-text);"
                 f" border: 1px solid rgba(127, 140, 141, 0.35); }}")
INFO_BOX = ("background: rgba(29, 153, 243, 0.12); border: 1px solid rgba(29, 153, 243, 0.30); "
            "border-radius: 10px; padding: 8px 10px;")
CHANGES_ORDER = ("update", "install", "remove", "downgrade", "reinstall", "radd", "rremove", "ralias", "rurl",
                 "rprio")


# ------------------------------------------------------------------ words
def group_label(kind, n):
    """The title of a group in the list of changes of the card, with how many: 'Updated (212)'. Its own
    words (a plural here: in Catalan 'Actualitzats', not the 'Actualitzat' of a single package elsewhere)."""
    texts = {
        "update": ngettext("Updated ({n})", "Updated ({n})", n),
        "install": ngettext("Installed ({n})", "Installed ({n})", n),
        "remove": ngettext("Removed ({n})", "Removed ({n})", n),
        "downgrade": ngettext("Back to an older version ({n})", "Back to an older version ({n})", n),
        "reinstall": ngettext("Installed again ({n})", "Installed again ({n})", n),
        "radd": ngettext("Repository added ({n})", "Repositories added ({n})", n),
        "rremove": ngettext("Repository removed ({n})", "Repositories removed ({n})", n),
        "ralias": ngettext("Repository renamed ({n})", "Repositories renamed ({n})", n),
        "rurl": ngettext("Repository address changed ({n})", "Repository addresses changed ({n})", n),
        "rprio": ngettext("Repository priority changed ({n})", "Repository priorities changed ({n})", n),
    }
    return texts[kind].format(n=n) if kind in texts else f"{kind} ({n})"


def join_names(names):
    names = list(names)
    if len(names) == 1:
        return names[0]
    return _("{list} and {last}").format(list=", ".join(names[:-1]), last=names[-1])


def count_text(counts):
    """'212 packages updated, 3 installed, 1 removed'."""
    parts = []
    for k in ("update", "install", "remove", "downgrade", "reinstall"):
        n = counts.get(k)
        if not n:
            continue
        first = not parts
        if k == "update":
            parts.append(ngettext("{n} package updated", "{n} packages updated", n) if first
                         else ngettext("{n} updated", "{n} updated", n))
        elif k == "install":
            parts.append(ngettext("{n} package installed", "{n} packages installed", n) if first
                         else ngettext("{n} installed", "{n} installed", n))
        elif k == "remove":
            parts.append(ngettext("{n} package removed", "{n} packages removed", n) if first
                         else ngettext("{n} removed", "{n} removed", n))
        elif k == "downgrade":
            parts.append(ngettext("{n} package back to an older version", "{n} packages back to an older version", n)
                         if first else ngettext("{n} back to an older version", "{n} back to an older version", n))
        else:
            parts.append(ngettext("{n} package installed again", "{n} packages installed again", n) if first
                         else ngettext("{n} installed again", "{n} installed again", n))
        parts[-1] = parts[-1].format(n=n)
    return ", ".join(parts)


def _name_title(one, many, names):
    if len(names) <= 1:
        return one.format(name=names[0] if names else "?")
    return many(len(names) - 1).format(name=names[0], n=len(names) - 1)


def describe(tx):
    """(title, line below it, icon kind) of a transaction, in plain words."""
    sub = tx.subcommand
    counts = tx.counts()
    kinds = set(counts)
    req = tx.requested_names
    installs = [c.name for c in tx.changes if c.kind == "install"]
    removes = [c.name for c in tx.changes if c.kind == "remove"]

    # what happened, not a command (the same words as "Install {name}" of the queue would read as a button)
    def install_title(names):
        return _name_title(_("Installed {name}"), lambda n: ngettext("Installed {name} and {n} more",
                                                                     "Installed {name} and {n} more", n), names)

    def remove_title(names):
        return _name_title(_("Removed {name}"), lambda n: ngettext("Removed {name} and {n} more",
                                                                   "Removed {name} and {n} more", n), names)

    if tx.via == "zypp-desk" and sub in ("install", "remove"):
        title, kind = _("Applied with Zypp Desk"), "zypp-desk"
    elif sub == "dup":
        title, kind = _("Update of the system (dup)"), "update"
    elif sub == "update":
        title, kind = _("Update (up)"), "update"
    elif sub == "patch":
        title, kind = _("Patches (patch)"), "update"
    elif sub == "install":
        title, kind = install_title(req or installs[:1]), "install"
    elif sub == "remove":
        title, kind = remove_title(req or removes[:1]), "remove"
    elif sub == "addrepo":
        title, kind = _("Repository added"), "repo"
    elif sub == "removerepo":
        title, kind = _("Repository removed"), "repo"
    elif sub in ("modifyrepo", "renamerepo"):
        title, kind = _("Repository changed"), "repo"
    elif sub == "refresh":
        title, kind = _("Repositories refreshed"), "repo"
    elif sub == "verify":
        title, kind = _("Dependencies fixed (verify)"), "update"
    elif sub == "inr":
        title, kind = _("New recommended packages (inr)"), "install"
    elif sub == "purge-kernels":
        title, kind = _("Old kernels removed"), "remove"
    elif sub == "source-install":
        title, kind = _("Source package installed"), "install"
    elif sub in ("addlocale", "removelocale"):
        title, kind = _("Languages changed"), "language"
    elif sub == "services":
        title, kind = _("Services changed"), "repo"
    elif sub == "removeptf":
        title, kind = _("Temporary fix removed (PTF)"), "remove"
    elif tx.is_zypper and sub:
        title, kind = "zypper " + sub, "other"
    # no command line (the start of the file) or another program (YaST, PackageKit): from what changed
    elif tx.repos and not tx.changes:
        rk = {r.kind for r in tx.repos}
        title = (_("Repository added") if rk == {"radd"} else _("Repository removed") if rk == {"rremove"}
                 else _("Repositories changed"))
        kind = "repo"
    elif tx.changes and all(c.name == "gpg-pubkey" for c in tx.changes):
        title, kind = _("Key imported"), "key"
    elif kinds == {"install"}:
        title, kind = (install_title(req) if req else _("Packages installed")), "install"
    elif kinds == {"remove"}:
        title, kind = (remove_title(req) if req else _("Packages removed")), "remove"
    elif kinds and kinds <= {"update", "downgrade", "reinstall"}:
        title, kind = _("Packages updated"), "update"
    elif tx.changes:
        title, kind = _("Packages changed"), "update"
    else:
        title, kind = _("No changes"), "other"
    if tx.dry_run:
        title = _("{title} (simulation)").format(title=title)
    elif tx.download_only:
        title = _("{title} (download only)").format(title=title)

    # the line below: what changed
    if not tx.changes:
        if tx.repos:
            line = ", ".join(dict.fromkeys(r.alias for r in tx.repos))
        elif tx.dry_run:
            line = _("a simulation: nothing changed")
        elif tx.download_only:
            line = _("only downloaded: nothing changed yet")
        else:
            line = _("nothing changed")
    elif len(kinds) == 1 and len(tx.changes) <= 3:
        names = join_names(dict.fromkeys(c.name for c in tx.changes))
        line = {"update": _("{names} updated"), "install": _("{names} installed"), "remove": _("{names} removed"),
                "downgrade": _("{names} back to an older version"),
                "reinstall": _("{names} installed again")}[kinds.pop()].format(names=names)
    elif kinds == {"install"} and req:
        n = len(tx.changes) - 1
        line = ngettext("{name} and {n} more package", "{name} and {n} more packages", n).format(name=req[0], n=n)
    else:
        line = count_text(counts)
    return title, line, kind


def date_text(d):
    """A date in the user's short format, with the whole year (22/11/2026, 11/22/2026, 22.11.2026)."""
    loc = QLocale()
    fmt = loc.dateFormat(QLocale.FormatType.ShortFormat)
    if "yyyy" not in fmt:
        fmt = fmt.replace("yy", "yyyy")
    return loc.toString(QDate(d.year, d.month, d.day), fmt)


def day_label(d, today=None):
    today = today or date.today()
    if d == today:
        return _("Today")
    if d == today - timedelta(days=1):
        return _("Yesterday")
    return date_text(d)


def when_text(dt, today=None, inside=False):
    """'Today at 15:43', 'Yesterday at 21:27', 'On 22/11/2026 at 19:10' to start a line; inside a sentence
    (inside=True) 'today at 15:43', 'yesterday at 21:27', 'on 22/11/2026 at 19:10'."""
    today = today or date.today()
    t = dt.strftime("%H:%M")
    if dt.date() == today:
        return (_("today at {time}") if inside else _("Today at {time}")).format(time=t)
    if dt.date() == today - timedelta(days=1):
        return (_("yesterday at {time}") if inside else _("Yesterday at {time}")).format(time=t)
    return (_("on {date} at {time}") if inside else _("On {date} at {time}")).format(date=date_text(dt.date()),
                                                                                       time=t)


def how_text(tx, when):
    via = tx.via
    if via == "zypp-desk":
        return _("{when}, with Zypp Desk").format(when=when)
    if via == "terminal":
        return _("{when}, from a terminal").format(when=when)
    if via == "yast":
        return _("{when}, with YaST").format(when=when)
    if via == "packagekit":
        return _("{when}, with PackageKit (Discover)").format(when=when)
    if via:
        return _("{when}, by {program}").format(when=when, program=via)
    return when


def snapshot_when(s):
    """When a snapshot was taken, to go inside a sentence ('yesterday at 21:35')."""
    if s is None:
        return ""
    if s.when is not None:
        return when_text(datetime.fromtimestamp(s.when), inside=True)
    if s.naive is not None:
        return when_text(s.naive, inside=True)
    return s.date_text


def short_versions(old, new):
    """'1.147.0 → 1.148.0': the versions without the release when they differ, the whole editions otherwise."""
    def version(e):
        return e.rpartition("-")[0] or e
    if old and new:
        vo, vn = version(old), version(new)
        return f"{vo} → {vn}" if vo != vn else f"{old} → {new}"
    return version(new or old)


def plain_label(text="", parent=None, wrap=True):
    lbl = QLabel(text, parent)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(wrap)
    return lbl


def command_label(text="", parent=None):
    """The exact command, in monospace and grey; it wraps at spaces and never widens the card."""
    lbl = plain_label(text, parent)
    lbl.setFont(mono())
    lbl.setStyleSheet("color: palette(placeholder-text);")
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return lbl


# ------------------------------------------------------------------ the list
class TxDelegate(QStyledItemDelegate):
    """A row: icon, 'time  Title' with the title in bold, what changed below (monospace, grey), and the
    snapshots on the right. Day titles are small grey capitals."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        v = index.data(ROW_ROLE)
        return QSize(200, 30 if isinstance(v, str) else 52)

    def paint(self, p, option, index):
        v = index.data(ROW_ROLE)
        if v is None:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        pal = option.palette
        if isinstance(v, str):
            f = QFont(option.font)
            f.setPointSizeF(option.font.pointSizeF() * 0.85)
            f.setBold(True)
            f.setCapitalization(QFont.Capitalization.AllUppercase)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            p.setFont(f)
            p.setPen(pal.placeholderText().color())
            p.drawText(r.adjusted(8, 0, 0, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, v)
            p.restore()
            return
        tx, d = self.page.txs[v], self.page.desc[v]
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 60))
            p.drawRoundedRect(r, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(127, 140, 141, 30))
            p.drawRoundedRect(r, 10, 10)
        self.page.tx_icon(v).paint(p, QRect(r.left() + 10, r.center().y() - 16, 32, 32))
        tx_left = r.left() + 54
        # the pill on the right
        right = r.right() - 8
        pill = self.page.pill(tx)
        if pill:
            text, color = pill
            small = QFont(option.font)
            small.setPointSizeF(option.font.pointSizeF() * 0.85)
            sfm = QFontMetrics(small)
            pw = sfm.horizontalAdvance(text) + 18
            pr = QRect(right - pw, r.center().y() - 11, pw, 22)
            bg = QColor(color)
            bg.setAlpha(40)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(pr, 11, 11)
            p.setPen(QColor(color))
            p.setFont(small)
            p.drawText(pr, Qt.AlignmentFlag.AlignCenter, text)
            right = pr.left() - 10
        room = max(20, right - tx_left)
        # time, then the title in bold
        dim = pal.placeholderText().color()
        fm = QFontMetrics(option.font)
        p.setFont(option.font)
        p.setPen(dim)
        time = tx.start.strftime("%H:%M")
        p.drawText(QRect(tx_left, r.top() + 5, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   time)
        tw = fm.horizontalAdvance(time + "  ")
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(dim if tx.empty else pal.text().color())
        p.drawText(QRect(tx_left + tw, r.top() + 5, room - tw, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(bold).elidedText(d[0], Qt.TextElideMode.ElideRight, room - tw))
        m = mono()
        p.setFont(m)
        p.setPen(dim)
        p.drawText(QRect(tx_left, r.top() + 25, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(m).elidedText(d[1], Qt.TextElideMode.ElideRight, room))
        p.restore()


# ------------------------------------------------------------------ dialogs
class ConfirmDialog(QDialog):
    """A question in plain words, the exact command, and the button that does it."""

    def __init__(self, parent, title, question, paragraphs, command, ok_text, ok_style=None, subtitle=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        v.setSpacing(10)
        q = heading(question, 1.15, self)
        q.setTextFormat(Qt.TextFormat.PlainText)
        q.setWordWrap(True)
        v.addWidget(q)
        if subtitle:
            sub = muted(subtitle, self)
            sub.setTextFormat(Qt.TextFormat.PlainText)
            v.addWidget(sub)
        for text in paragraphs:
            v.addWidget(plain_label(text, self))
        v.addWidget(command_label(command, self))
        v.addStretch(1)
        box = QDialogButtonBox(self)
        self.ok = QPushButton(ok_text, self)
        if ok_style:
            self.ok.setStyleSheet(ok_style)
        else:
            self.ok.setProperty("primary", True)
        box.addButton(self.ok, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)  # Enter does not undo anything by accident
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)


class FilesDialog(QDialog):
    """The files snapper saw change between two snapshots."""

    def __init__(self, parent, pre, post, rows):
        super().__init__(parent)
        self.setWindowTitle(_("Files changed between #{pre} and #{post}").format(pre=pre, post=post))
        self.resize(780, 560)
        self.rows = rows
        v = QVBoxLayout(self)
        created = sum(1 for f, _p in rows if f.startswith("+"))
        deleted = sum(1 for f, _p in rows if f.startswith("-"))
        summary = ngettext("{n} file changed", "{n} files changed", len(rows)).format(n=len(rows))
        if rows:
            summary += " · " + _("{created} created, {deleted} deleted, {other} modified").format(
                created=created, deleted=deleted, other=len(rows) - created - deleted)
        v.addWidget(heading(summary, 1.05, self))
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText(_("Filter by path"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._fill)
        v.addWidget(self.filter)
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setFont(mono())
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        v.addWidget(self.text, 1)
        legend = muted(_("What the letters mean: + created · - deleted · c content changed · t type changed (for "
                         "example a file became a link) · p permissions · u owner · g group · x extended "
                         "attributes · a access lists (ACL) · . nothing changed in that respect"), self)
        v.addWidget(legend)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        box.rejected.connect(self.reject)
        v.addWidget(box)
        self._fill()

    def _fill(self):
        t = self.filter.text().strip().lower()
        lines = [f"{flags}  {path}" for flags, path in self.rows if not t or t in path.lower()]
        self.text.setPlainText("\n".join(lines) if self.rows else _("snapper found no changed files."))


# ------------------------------------------------------------------ the card
class TxCard(QWidget):
    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.tx = None
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 2, 4, 2)
        v.setSpacing(10)
        top = QHBoxLayout()
        self.icon = QLabel(self)
        self.icon.setFixedSize(48, 48)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.title = heading("", 1.3, self)
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.how = muted("", self)
        self.how.setTextFormat(Qt.TextFormat.PlainText)
        names.addWidget(self.title)
        names.addWidget(self.how)
        top.addLayout(names, 1)
        v.addLayout(top)

        self.grid_box = QWidget(self)
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(1, 1)
        self._kv_labels = []
        v.addWidget(self.grid_box)

        self.changes = QTreeWidget(self)
        self.changes.setHeaderHidden(True)
        self.changes.setColumnCount(2)
        self.changes.setRootIsDecorated(False)
        self.changes.setItemsExpandable(False)
        self.changes.setIndentation(10)
        self.changes.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.changes.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # the versions are shortened (… at the end, the whole text in the tooltip) instead of scrolling sideways
        self.changes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.changes.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.changes.header().setMinimumSectionSize(30)
        self.changes.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.changes.viewport().installEventFilter(self)  # the names never take more than half of a narrow card
        self._name_width = 120
        self.changes.setStyleSheet("QTreeWidget { border: 1px solid rgba(127, 140, 141, 0.35); border-radius: 10px;"
                                   " padding: 4px 6px; background: palette(base); }")
        v.addWidget(self.changes, 1)  # it takes the room there is, and scrolls when it is not enough

        self.command = command_label("", self)
        v.addWidget(self.command)
        self.note = muted("", self)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.note)
        self.info = plain_label("", self)
        self.info.setStyleSheet(INFO_BOX)
        v.addWidget(self.info)
        v.addStretch(0)  # what the list of changes does not need
        self.undo_cmd = command_label("", self)
        v.addWidget(self.undo_cmd)
        btns = QHBoxLayout()
        self.b_compare = button(_("Compare the files…"), "document-compare", self.page.compare)
        self.b_undo = button(_("Undo this change…"), "edit-undo", self.page.undo)
        self.b_undo.setStyleSheet(ORANGE_BUTTON)
        btns.addWidget(self.b_compare, 1)
        btns.addWidget(self.b_undo, 1)
        v.addLayout(btns)
        self.placeholder = muted(_("Choose a change on the left to see what it did."), self)
        v.insertWidget(0, self.placeholder)
        self.show_tx(None)

    def _kv(self, rows):
        """Name: value lines (the same labels are reused: nothing is created or deleted while browsing)."""
        while len(self._kv_labels) < len(rows):
            i = len(self._kv_labels)
            key = muted("", self.grid_box, wrap=False)
            value = plain_label("", self.grid_box)
            self.grid.addWidget(key, i, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, i, 1)
            self._kv_labels.append((key, value))
        for i, (key, value) in enumerate(self._kv_labels):
            if i < len(rows):
                key.setText(rows[i][0])
                value.setText(rows[i][1])
            key.setVisible(i < len(rows))
            value.setVisible(i < len(rows))
        self.grid_box.setVisible(bool(rows))

    def _fill_changes(self, tx):
        t = self.changes
        t.clear()
        groups = {}
        for c in tx.changes:
            groups.setdefault(c.kind, []).append((c.name, short_versions(c.old, c.new), c))
        if tx.changes or len(tx.repos) > 1:  # one repository is already told above (name and address)
            for r in tx.repos:
                groups.setdefault(r.kind, []).append((r.alias, r.detail, None))
        bold = QFont(t.font())
        bold.setBold(True)
        several = len(groups) > 1
        rows = 0
        for kind in CHANGES_ORDER:
            items = groups.get(kind)
            if not items:
                continue
            parent = t
            if several:
                g = QTreeWidgetItem(t, [group_label(kind, len(items)), ""])
                g.setFirstColumnSpanned(True)
                g.setFont(0, bold)
                g.setForeground(0, t.palette().placeholderText())
                parent = g
                rows += 1
            # what was asked for first, then the rest by name
            for name, text, c in sorted(items, key=lambda x: (not (x[2] and x[2].requested), x[0].lower())):
                it = QTreeWidgetItem(parent, [name, text])
                it.setFont(0, bold)
                if c is not None:
                    tip = " → ".join(e for e in (c.old, c.new) if e)
                    it.setToolTip(1, f"{tip}  ({c.arch})" if c.arch else tip)
                    if c.repo:
                        it.setToolTip(0, _("from the repository {repo}").format(repo=c.repo))
                else:
                    it.setToolTip(1, text)
                rows += 1
            if several:
                g.setExpanded(True)
        t.resizeColumnToContents(0)
        self._name_width = min(t.columnWidth(0) + 16, 240)
        self._fit_columns()
        rh = max(t.sizeHintForRow(0), 20) if rows else 20
        t.setMinimumHeight(min(rows, 4) * rh + 14)
        t.setMaximumHeight(rows * rh + 14)
        t.setVisible(rows > 0)

    def _fit_columns(self):
        w = self.changes.viewport().width()
        self.changes.setColumnWidth(0, min(self._name_width, max(80, w // 2)) if w > 0 else self._name_width)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize and obj is self.changes.viewport():
            self._fit_columns()
        return False

    def show_tx(self, tx):
        self.tx = tx
        for w in (self.icon, self.title, self.how, self.grid_box, self.changes, self.command, self.note, self.info,
                  self.undo_cmd, self.b_compare, self.b_undo):
            w.setVisible(tx is not None)
        self.placeholder.setVisible(tx is None)
        if tx is None:
            return
        page = self.page
        i = page.txs.index(tx)
        title, _line, _kind = page.desc[i]
        self.icon.setPixmap(page.tx_icon(i).pixmap(48, 48))
        self.title.setText(title)
        self.how.setText(how_text(tx, when_text(tx.start)))
        rows = []
        pre, post = tx.pre, tx.post
        if pre and post:
            rows.append((_("Snapshots"), _("#{pre} before · #{post} after").format(pre=pre, post=post)))
        elif pre:
            rows.append((_("Snapshots"), _("#{pre} before").format(pre=pre)))
        if tx.changes:
            rows.append((_("Packages"), count_text(tx.counts())))
        if tx.repos:
            r = tx.repos[0]
            if len(tx.repos) == 1:
                rows.append((_("Repository"), r.alias))
                if r.kind in ("radd", "rurl") and r.detail:
                    rows.append((_("Address"), r.detail))
        self._kv(rows)
        self._fill_changes(tx)
        self.command.setText(tx.display_command)
        self.command.setVisible(bool(tx.display_command))
        if not tx.display_command:
            self.command.setText("")

        # snapshots
        note = page.snapshot_note(tx)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        can = bool(pre and post and page.info is not None)
        if pre and page.info is not None:
            self.info.setText(_("If the computer does not start after a change, choose snapshot #{pre} in the boot "
                                "menu. Once it has started, “Keep this one” will appear here.").format(pre=pre))
            self.info.show()
        else:
            self.info.hide()
        self.undo_cmd.setText(Step("snapper-undo", [str(pre), str(post)]).display() if can else "")
        for w in (self.undo_cmd, self.b_compare, self.b_undo):
            w.setVisible(can)
        # started from an older snapshot (read-only): nothing can be put back until it is kept or left
        booted_old = snapper.boot_state(page.info) is not None
        self.b_undo.setEnabled(not booted_old)
        self.b_undo.setToolTip(_("Not now: the computer is running from an older snapshot (see the band above).")
                               if booted_old else "")


# ------------------------------------------------------------------ the page
class HistoryPage(Page):
    key = "history"
    title = _("History")
    icon_name = "view-history"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.loaded = True  # reading needs the password: only on request
        self.txs = []        # newest first
        self.desc = []       # (title, line, icon kind) of each transaction
        self._search = []    # the words the filter looks in, of each transaction
        self.info = None     # snapper.SnapperInfo when snapper's list was read
        self.snap_state = "unknown"  # absent, ok, none (no root config), failed
        self.snap_error = ""
        self.oldest = None
        self._icons = {}
        self._filter_text = ""
        self._show_empty = False

        lay = QVBoxLayout(self)
        # ------------------------------------------------ the band: started from an older snapshot
        self.banner = QFrame(self)
        self.banner.setObjectName("bootbanner")
        self.banner.setStyleSheet("#bootbanner { background: rgba(246, 116, 0, 0.12); border: 1px solid "
                                  "rgba(246, 116, 0, 0.55); border-radius: 12px; }")
        bh = QHBoxLayout(self.banner)
        bh.setContentsMargins(14, 10, 14, 10)
        bi = QLabel(self.banner)
        bi.setPixmap(icon("dialog-warning", "emblem-important").pixmap(32, 32))
        bh.addWidget(bi, 0, Qt.AlignmentFlag.AlignTop)
        bv = QVBoxLayout()
        bv.setSpacing(2)
        self.banner_title = heading("", 1.1, self.banner)
        self.banner_title.setTextFormat(Qt.TextFormat.PlainText)
        self.banner_text = plain_label("", self.banner)
        bv.addWidget(self.banner_title)
        bv.addWidget(self.banner_text)
        bh.addLayout(bv, 1)
        self.b_keep = button(_("Keep this one…"), "dialog-ok-apply", self.keep_snapshot, primary=True)
        bh.addWidget(self.b_keep, 0, Qt.AlignmentFlag.AlignVCenter)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.stack = QStackedWidget(self)
        lay.addWidget(self.stack, 1)

        # ------------------------------------------------ before loading
        empty = Card(None, self)
        column = QWidget(empty)
        column.setMaximumWidth(560)
        ev = QVBoxLayout(column)
        ev.setSpacing(10)
        ei = QLabel(column)
        ei.setPixmap(icon("view-history").pixmap(64, 64))
        ei.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        ev.addWidget(ei)
        et = heading(_("The history needs the administrator password"), 1.2, column)
        et.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        et.setWordWrap(True)
        ev.addWidget(et)
        self.empty_text = muted(self._empty_explanation(), column)
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        ev.addWidget(self.empty_text)
        self.empty_error = plain_label("", column)
        self.empty_error.setStyleSheet("color: #da4453;")
        self.empty_error.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_error.hide()
        ev.addWidget(self.empty_error)
        self.b_load_big = button(_("Load the history"), "view-refresh", self.load, primary=True)
        ev.addWidget(self.b_load_big, 0, Qt.AlignmentFlag.AlignHCenter)
        middle = QHBoxLayout()
        middle.addStretch(1)
        middle.addWidget(column, 3)
        middle.addStretch(1)
        empty.lay.addStretch(1)
        empty.lay.addLayout(middle)
        empty.lay.addStretch(2)
        self.stack.addWidget(empty)

        # ------------------------------------------------ the list | the card
        lc = Card(None, self)
        lc.lay.setContentsMargins(12, 10, 12, 10)
        self.list = QListWidget(self)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(TxDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(False)
        self.list.setStyleSheet("QListWidget { background: transparent; }")
        self.list.currentItemChanged.connect(lambda cur, _p: self._show(cur))
        lc.lay.addWidget(self.list, 1)
        self.list_note = muted("", self)
        self.list_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_note.hide()
        lc.lay.addWidget(self.list_note, 1)  # in the place of the list when nothing is shown
        foot = QHBoxLayout()
        self.count_lbl = muted("", self, wrap=False)
        self.empty_box = QCheckBox("", self)
        self.empty_box.toggled.connect(self._set_show_empty)
        foot.addWidget(self.empty_box)
        foot.addStretch(1)
        foot.addWidget(self.count_lbl)
        lc.lay.addLayout(foot)

        dc = Card(None, self)
        dc.lay.setContentsMargins(14, 14, 14, 14)
        self.card = TxCard(self, dc)
        dc.lay.addWidget(self.card)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(lc)
        split.addWidget(dc)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([620, 460])
        split.setChildrenCollapsible(False)
        self.stack.addWidget(split)

    # ------------------------------------------------ header
    def header_actions(self):
        f = QLineEdit(self)
        f.setPlaceholderText(_("Filter"))
        f.setClearButtonEnabled(True)
        f.addAction(icon("edit-find", "search"), QLineEdit.ActionPosition.LeadingPosition)
        f.setFixedWidth(220)
        f.setToolTip(_("Shows only the changes that touch a package, a repository or a word"))
        f.setText(self._filter_text)
        f.textChanged.connect(self._set_filter)
        return [f, button(_("Load"), "view-refresh", self.load)]

    def _empty_explanation(self):
        text = _("zypper writes what it changes to /var/log/zypp/history, which only root can read. Nothing is "
                 "loaded until you ask, and then the password is asked once.")
        if snapper.snapper_installed():
            text += " " + _("The list of snapshots is read at the same time.")
        return text

    # ------------------------------------------------ loading
    def load(self):
        hist = Step("history", [], capture=True)
        steps = [hist]
        snap = None
        if snapper.snapper_installed():
            snap = Step("snapper-list", [], capture=True, cond="always", lang="C")
            steps.append(snap)
        self.ctx.run(steps, _("Read the history"), on_done=lambda ok: self._read(hist, snap))

    def _read(self, hist, snap):
        if hist.code != 0:
            if hist.code in PKEXEC_CANCELLED or hist.state == "skipped":
                msg = _("The password was not given, so the history was not read.")
            else:
                msg = _("The history could not be read (exit code {code}): look at the terminal.").format(
                    code=hist.code)
            if not self.txs:
                self.empty_error.setText(msg)
                self.empty_error.show()
                self.stack.setCurrentIndex(0)
            else:
                error_box(self, self.title, msg)
            return
        snap_text = None
        if snap is None:
            self.snap_state, self.snap_error = "absent", ""
        elif snap.code == 0:
            self.snap_state, self.snap_error = "ok", ""
            snap_text = snap.output
        else:
            out = snap.output.strip()
            # snapper is there, but / has no snapper configuration (not Btrfs): no snapshots at all
            if "does not exist" in out or "Unknown config" in out or "not installed" in out:
                self.snap_state = "none"
            else:
                self.snap_state = "failed"
            self.snap_error = out.splitlines()[-1] if out else ""
        run_async(snapper.load, hist.output, snap_text, on_done=self._parsed)

    def _parsed(self, result, err):
        if err:
            error_box(self, self.title, _("The history could not be read: {error}").format(error=err))
            return
        txs, info, snap_err = result
        if self.snap_state == "ok" and info is None:
            self.snap_state, self.snap_error = "failed", snap_err
        self.txs = txs
        self.info = info
        self.oldest = snapper.oldest_pre_epoch(info)
        self.desc = [describe(tx) for tx in txs]
        self._search = [" ".join((d[0], d[1], tx.search_text())).lower() for d, tx in zip(self.desc, txs)]
        self._icons = {}
        self.empty_error.hide()
        self.stack.setCurrentIndex(1)
        self._update_banner()
        self._fill()

    # ------------------------------------------------ the list
    def _set_filter(self, text):
        self._filter_text = text
        self._fill()

    def _set_show_empty(self, on):
        self._show_empty = on
        self._fill()

    def _fill(self):
        current = self.card.tx
        # the same change stays chosen after filtering, and after loading again
        cur_key = (current.start, current.cmdline, len(current.changes)) if current else None
        t = self._filter_text.strip().lower()
        self.list.clear()
        today = date.today()
        last_day, shown, select = None, 0, None
        for i, tx in enumerate(self.txs):
            if tx.empty and not self._show_empty:
                continue
            title, line, _kind = self.desc[i]
            if t and t not in self._search[i]:
                continue
            d = tx.start.date()
            if d != last_day:
                hdr = QListWidgetItem()
                hdr.setData(ROW_ROLE, day_label(d, today))
                hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(hdr)
                last_day = d
            it = QListWidgetItem()
            it.setData(ROW_ROLE, i)
            it.setToolTip(f"{title}\n{line}")
            self.list.addItem(it)
            shown += 1
            if select is None and (current is None or (tx.start, tx.cmdline, len(tx.changes)) == cur_key):
                select = it
        n_empty = sum(1 for tx in self.txs if tx.empty)
        self.empty_box.setText(ngettext("Also show the command that changed nothing ({n})",
                                        "Also show the commands that changed nothing ({n})", n_empty).format(n=n_empty))
        self.empty_box.setVisible(n_empty > 0)
        self.count_lbl.setText(ngettext("{n} change", "{n} changes", shown).format(n=shown))
        if not self.txs:
            self.list_note.setText(_("zypper has not written anything in its history yet."))
        elif not shown:
            self.list_note.setText(_("Nothing matches the filter."))
        self.list_note.setVisible(not shown)
        self.list.setVisible(bool(shown))
        if select is None:
            select = next((self.list.item(r) for r in range(self.list.count())
                           if isinstance(self.list.item(r).data(ROW_ROLE), int)), None)
        if select is not None:
            self.list.setCurrentItem(select)
            self.list.scrollToItem(select)
        else:
            self.card.show_tx(None)

    def _show(self, item):
        v = item.data(ROW_ROLE) if item else None
        if isinstance(v, int):
            self.card.show_tx(self.txs[v])

    def tx_icon(self, i):
        if i in self._icons:
            return self._icons[i]
        tx, (_t, _l, kind) = self.txs[i], self.desc[i]
        ic = None
        if kind == "zypp-desk":
            from ..app import app_icon
            ic = app_icon()
        elif kind == "install":
            names = tx.requested_names or [c.name for c in tx.changes if c.kind == "install"][:1]
            pkgs = getattr(self.ctx, "pages", {}).get("packages")
            if names and pkgs is not None and hasattr(pkgs, "pkg_icon"):
                ic = pkgs.pkg_icon({"name": names[0]})
            ic = ic if ic is not None and not ic.isNull() else icon("package-x-generic")
        else:
            ic = icon(*{"update": ("system-software-update", "update-low"),
                        "remove": ("edit-delete", "list-remove"),
                        "repo": ("folder-cloud", "folder-remote", "network-server"),
                        "key": ("security-high", "dialog-password"),
                        "language": ("preferences-desktop-locale",)}.get(kind, ("view-history", "system-run")))
        self._icons[i] = ic
        return ic

    def pill(self, tx):
        """(text, colour) on the right of a row, or None when this computer has no snapshots to speak of."""
        if self.snap_state != "ok" or self.info is None:
            return None
        if tx.pre and tx.post:
            return f"#{tx.pre} → #{tx.post}", BLUE
        if tx.pre:
            return f"#{tx.pre} → ?", ORANGE
        return _("no snapshot"), GREY

    def snapshot_note(self, tx):
        """A short grey line in the card about this change's snapshots ('' when there are some)."""
        if self.snap_state in ("absent", "none"):
            return _("This computer does not take snapshots: they need Btrfs and snapper.")
        if self.snap_state == "failed":
            msg = _("The list of snapshots could not be read, so the snapshots of this change are not shown.")
            return msg + (f" ({self.snap_error})" if self.snap_error else "")
        if tx.pre and not tx.post:
            return _("This change did not finish: there is no snapshot from after it, so it cannot be compared "
                     "or undone from here.")
        if tx.pre:
            return ""
        if not tx.changes:
            return _("No snapshot: zypper only takes snapshots when packages change.")
        if self.oldest is not None and snapper.tx_epoch(tx) < self.oldest:
            return _("Its snapshots have already been deleted: snapper keeps only the most recent ones.")
        return _("No snapshot was taken for this change.")

    # ------------------------------------------------ snapshots: compare, undo, keep
    def compare(self):
        tx = self.card.tx
        if not tx or not (tx.pre and tx.post):
            return
        pre, post = tx.pre, tx.post
        st = Step("snapper-status", [str(pre), str(post)], capture=True, lang="C")
        self.ctx.run([st], _("Compare snapshots #{pre} and #{post}").format(pre=pre, post=post),
                     on_done=lambda ok: self._compared(st, pre, post))

    def _compared(self, st, pre, post):
        if st.code != 0:
            if st.code not in PKEXEC_CANCELLED and st.state != "skipped":
                tail = "\n".join(st.output.strip().splitlines()[-6:])
                error_box(self, _("Compare the files"),
                          _("snapper could not compare the snapshots (exit code {code}).").format(code=st.code)
                          + ("\n\n" + tail if tail else ""))
            return
        FilesDialog(self, pre, post, snapper.parse_status(st.output)).exec()

    def undo_dialog(self, tx, folders=None, outside=None):
        """folders: what the snapshots of / do not hold (snapper.outside_root()); outside: {package: [folders]}
        of this change's programs with files there (snapper.outside_snapshots)."""
        pre, post = tx.pre, tx.post
        title, line, _kind = self.desc[self.txs.index(tx)]
        folders = snapper.outside_root() if folders is None else folders
        outside = outside or {}
        paragraphs = [_("snapper puts the system files back as they were before this change, in snapshot #{pre}. "
                        "The programs of this change go back to their old versions; other changes made later to "
                        "the same files are lost.").format(pre=pre)]
        not_home = [f for f in folders if f != "/home"]
        if not_home:
            paragraphs.append(ngettext(
                "Not everything goes back: {folders} is not in the snapshots, so what this change wrote there "
                "stays as it is now.",
                "Not everything goes back: {folders} are not in the snapshots, so what this change wrote there "
                "stays as it is now.", len(not_home)).format(folders=join_names(not_home)))
        if outside:
            names = list(outside)
            shown = ", ".join(f"{n} ({', '.join(outside[n])})" for n in names[:10])
            if len(names) > 10:
                shown += " " + ngettext("and {n} more", "and {n} more", len(names) - 10).format(n=len(names) - 10)
            paragraphs.append(ngettext(
                "This program of the change is installed there, so it keeps its new files while rpm will say "
                "that the old version is installed: {list}. Reinstall it afterwards, so that its files match.",
                "These programs of the change are installed there, so they keep their new files while rpm will "
                "say that the old versions are installed: {list}. Reinstall them afterwards, so that their files "
                "match.", len(names)).format(list=shown))
        paragraphs.append(_("Your own files in /home are not touched. Restarting the computer afterwards is "
                            "recommended."))
        return ConfirmDialog(
            self, _("Undo this change"), _("Undo “{title}”?").format(title=title), paragraphs,
            Step("snapper-undo", [str(pre), str(post)]).display(), _("Undo the change"), ORANGE_BUTTON,
            subtitle=f"{when_text(tx.start)} · {line}")

    def undo(self):
        tx = self.card.tx
        if not tx or not (tx.pre and tx.post):
            return
        # first, as the user: which programs of this change live in folders the snapshots do not hold (rpm -q)
        names = sorted({c.name for c in tx.changes if c.kind != "remove"})
        was = self.card.b_undo.isEnabled()
        self.card.b_undo.setEnabled(False)
        run_async(snapper.outside_snapshots, names, on_done=lambda res, err: self._undo_ask(tx, was, res, err))

    def _undo_ask(self, tx, was, res, err):
        if tx is not self.card.tx:
            return  # another change was chosen meanwhile (and its card set the button)
        self.card.b_undo.setEnabled(was)
        folders, outside = res if err is None and res else (None, {})
        if self.undo_dialog(tx, folders, outside).exec() != QDialog.DialogCode.Accepted:
            return
        pre, post = tx.pre, tx.post
        st = Step("snapper-undo", [str(pre), str(post)])
        self.ctx.run([st], _("Undo the change #{pre}..#{post}").format(pre=pre, post=post),
                     on_done=lambda ok: self._undone(st))

    def _undone(self, st):
        if st.code == 0:
            info_box(self, _("Undo this change"),
                     _("The files are back as they were before the change. Restart the computer so that every "
                       "program uses them."))

    def _update_banner(self):
        state = snapper.boot_state(self.info) if self.snap_state == "ok" else None
        if not state:
            self.banner.hide()
            return
        kind, active, _default = state
        when = snapshot_when(active)
        if kind == "old":
            self.banner_title.setText(_("You are using snapshot #{n}, taken {when}").format(n=active.number,
                                                                                           when=when))
            self.banner_text.setText(_("The computer was started from an older snapshot chosen in the boot menu. "
                                       "If everything works as it should, keep it; if not, just restart and the "
                                       "computer starts as usual."))
            self.b_keep.show()
        else:
            self.banner_title.setText(_("Snapshot #{n} will be used from the next start").format(n=active.number))
            self.banner_text.setText(_("snapper has made it the default. Restart the computer to finish."))
            self.b_keep.hide()
        self.banner.show()

    def rollback_dialog(self, active):
        return ConfirmDialog(
            self, _("Keep this snapshot"), _("Keep snapshot #{n}?").format(n=active.number),
            [_("The computer was started from snapshot #{n}, taken {when}, chosen in the boot menu.").format(
                n=active.number, when=snapshot_when(active)),
             _("snapper rollback makes it the one the computer starts from next time: it makes a writable copy of "
               "it and sets it as the default. The newer state is kept as a snapshot, so you can go back to it. "
               "Restart the computer afterwards.")],
            Step("snapper-rollback", []).display(), _("Keep this one"))

    def keep_snapshot(self):
        state = snapper.boot_state(self.info)
        if not state or state[0] != "old":
            return
        active = state[1]
        if self.rollback_dialog(active).exec() != QDialog.DialogCode.Accepted:
            return
        st = Step("snapper-rollback", [])
        self.ctx.run([st], _("Keep snapshot #{n}").format(n=active.number), on_done=lambda ok: self._kept(st))

    def _kept(self, st):
        if st.code != 0:
            return
        info_box(self, _("Keep this snapshot"), _("Done. Restart the computer to start from it."))
        self.load()  # the band now says a restart is still to come
