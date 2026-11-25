# SPDX-License-Identifier: GPL-3.0-or-later
"""Signing keys: every key rpm trusts, grouped by who uses it, with a card to delete the ones nobody needs,
and the dialog to import a new key after looking at it (owner, fingerprint, the files it signs)."""

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QDateTime, QLocale, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
                               QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from .. import keys as keyring
from ..i18n import _, ngettext
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Card, Page, button, error_box, heading, hline, mono, muted, plain

GREY = "#7f8c8d"
AMBER = "#f39c12"
ROW_ROLE = Qt.ItemDataRole.UserRole
MARKS = {"queued": ("new", BLUE), "repos": ("ok", GREEN), "packages": ("warn", AMBER), "opensuse": ("ok", GREEN),
         "unused": ("bad", RED)}

DANGER_CSS = (f"QPushButton {{ background: {RED}; color: white; border: 1px solid #b83845; border-radius: 7px;"
              " padding: 7px 16px; font-weight: 600; }"
              " QPushButton:hover { background: #e35d6a; }"
              " QPushButton:disabled { background: palette(button); color: palette(placeholder-text);"
              " border: 1px solid rgba(127, 140, 141, 0.35); }")
NOTE_CSS = {  # the sentence about what deleting means: tinted, readable in light and dark themes
    "green": "background: rgba(39, 174, 96, 0.13); border-radius: 8px; padding: 10px 12px;",
    "orange": "background: rgba(246, 116, 0, 0.12); border-radius: 8px; padding: 10px 12px;",
    "grey": "background: rgba(127, 140, 141, 0.13); border-radius: 8px; padding: 10px 12px;",
    "red": "background: rgba(218, 68, 83, 0.12); border-radius: 8px; padding: 10px 12px;",
}


def group_titles(unknown_packages=False):
    return {
        "queued": _("Will be imported"),
        "repos": _("Used by your repositories"),
        "packages": _("Sign packages you have installed"),
        "opensuse": _("Shipped by openSUSE"),
        # when rpm could not tell which packages each key signed, only the repositories are sure
        "unused": _("Used by no repository") if unknown_packages else _("Used by nobody"),
    }


@lru_cache(maxsize=32)
def shield(mark, color, size=32):
    """A shield with a mark: 'ok' (tick), 'warn' (!), 'bad' (cross) or 'new' (+)."""
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size * 2 / 32, size * 2 / 32)
    path = QPainterPath()
    path.moveTo(16, 2)
    path.lineTo(28, 6)
    path.lineTo(28, 15)
    path.cubicTo(28, 23, 22.5, 28, 16, 31)
    path.cubicTo(9.5, 28, 4, 23, 4, 15)
    path.lineTo(4, 6)
    path.closeSubpath()
    base = QColor(color)
    grad = QLinearGradient(0, 2, 0, 31)
    grad.setColorAt(0, base.lighter(118))
    grad.setColorAt(1, base.darker(112))
    p.setPen(QPen(base.darker(125), 1))
    p.setBrush(grad)
    p.drawPath(path)
    pen = QPen(QColor("white"), 2.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if mark == "ok":
        p.drawPolyline([QPointF(10.5, 16), QPointF(14.5, 20.5), QPointF(21.5, 11)])
    elif mark == "warn":
        p.drawLine(QPointF(16, 9.5), QPointF(16, 17.5))
        p.drawPoint(QPointF(16, 22.5))
    elif mark == "bad":
        p.drawLine(QPointF(11.5, 11), QPointF(20.5, 20))
        p.drawLine(QPointF(20.5, 11), QPointF(11.5, 20))
    else:
        p.drawLine(QPointF(16, 10), QPointF(16, 22))
        p.drawLine(QPointF(10, 16), QPointF(22, 16))
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def group_icon(group, size=32):
    mark, color = MARKS.get(group, ("ok", GREEN))
    return shield(mark, color, size)


def fmt_date(epoch):
    if not epoch:
        return ""
    loc = QLocale()
    fmt = loc.dateFormat(QLocale.FormatType.ShortFormat)
    if "yyyy" not in fmt:
        fmt = fmt.replace("yy", "yyyy")  # 2021, not 21: keys live for decades
    return loc.toString(QDateTime.fromSecsSinceEpoch(int(epoch)).date(), fmt)


def plain_label(text="", parent=None, wrap=True):
    lbl = QLabel(text, parent)
    lbl.setTextFormat(Qt.TextFormat.PlainText)  # key names and e-mails come from the key: never rich text
    lbl.setWordWrap(wrap)
    return lbl


def key_title(k):
    return k.get("name") or k.get("email") or k.get("keyid") or "?"


# ---------------------------------------------------------------------------------------------- the list

class KeyDelegate(QStyledItemDelegate):
    """A row: shield, name in bold with the key ID below (blue, monospace), pills on the right.
    Group titles are small grey capitals."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        row = index.data(ROW_ROLE)
        return QSize(200, 32 if row and row.get("header") else 52)

    def paint(self, p, option, index):
        row = index.data(ROW_ROLE)
        if not row:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 1, -4, -1)
        pal = option.palette
        if row.get("header"):
            f = QFont(option.font)
            f.setPointSizeF(option.font.pointSizeF() * 0.85)
            f.setBold(True)
            f.setCapitalization(QFont.Capitalization.AllUppercase)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            p.setFont(f)
            p.setPen(pal.placeholderText().color())
            p.drawText(r.adjusted(8, 0, 0, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       row["header"])
            p.restore()
            return
        k = row["key"]
        if option.state & QStyle.StateFlag.State_Selected:
            bg = QColor(39, 174, 96, 60)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            bg = QColor(39, 174, 96, 22)
        elif row.get("odd"):
            bg = QColor(127, 140, 141, 20)
        else:
            bg = None
        if bg is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 9, 9)
        group_icon(k["group"]).paint(p, QRect(r.left() + 12, r.center().y() - 15, 30, 30))
        tx = r.left() + 56
        # the pills, from the right
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        sfm = QFontMetrics(small)
        right = r.right() - 10
        room_for_pills = max(80, int(r.width() * 0.5))
        for text, color in self.page.pills(k):
            text = sfm.elidedText(text, Qt.TextElideMode.ElideRight, 190)
            pw = sfm.horizontalAdvance(text) + 18
            if r.right() - 10 - (right - pw) > room_for_pills:
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
        room = right - tx - 8
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(pal.text().color())
        p.drawText(QRect(tx, r.top() + 6, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(bold).elidedText(key_title(k), Qt.TextElideMode.ElideRight, room))
        p.setFont(mono())
        p.setPen(QColor(BLUE))
        p.drawText(QRect(tx, r.top() + 26, room, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   k.get("keyid") or "")
        p.restore()


# ---------------------------------------------------------------------------------------------- the card

class KeyDetails(QWidget):
    """Who the key belongs to, its dates, who uses it, what deleting it means, and the button to do it."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.key = None
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 4, 6, 4)
        top = QHBoxLayout()
        self.icon = QLabel(self)
        self.icon.setFixedSize(52, 52)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.name = heading("", 1.3, self)
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setWordWrap(True)
        self.email = muted("", self)
        self.email.setTextFormat(Qt.TextFormat.PlainText)
        names.addWidget(self.name)
        names.addWidget(self.email)
        top.addLayout(names, 1)
        v.addLayout(top)
        v.addSpacing(6)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(7)
        self.grid.setColumnStretch(1, 1)
        self.rows = {}
        for key, label in (("id", _("ID")), ("fpr", _("Fingerprint")), ("created", _("Created")),
                           ("expires", _("Expires")), ("installed", _("Imported")), ("repos", _("Used by")),
                           ("packages", _("Signed"))):
            lbl = muted(label, self)
            val = plain_label("", self)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            n = self.grid.rowCount()
            self.grid.addWidget(lbl, n, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(val, n, 1)
            self.rows[key] = (lbl, val)
        self.rows["id"][1].setFont(mono())
        self.rows["id"][1].setStyleSheet(f"color: {BLUE};")
        self.rows["fpr"][1].setFont(mono())
        v.addLayout(self.grid)
        v.addSpacing(10)
        self.note = plain_label("", self)
        v.addWidget(self.note)
        v.addStretch(1)
        self.cmd = muted("", self)
        self.cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.cmd.setFont(mono())
        self.cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.cmd)
        self.b_delete = QPushButton(_("Delete it"), self)
        self.b_delete.setStyleSheet(DANGER_CSS)
        self.b_delete.clicked.connect(self._delete)
        self.b_keep = button(_("Keep it"), "edit-undo", self._keep)
        v.addWidget(self.b_delete)
        v.addWidget(self.b_keep)
        self.show_message(_("Loading…"))

    def show_message(self, text):
        self.key = None
        self.icon.clear()
        self.name.setText(text)
        self.email.setText("")
        for lbl, val in self.rows.values():
            lbl.hide()
            val.hide()
        self.note.hide()
        self.cmd.setText("")
        self.b_delete.hide()
        self.b_keep.hide()

    def _row(self, key, text, color=None):
        lbl, val = self.rows[key]
        lbl.setVisible(bool(text))
        val.setVisible(bool(text))
        val.setText(text or "")
        if key not in ("id", "fpr"):
            val.setStyleSheet(f"color: {color};" if color else "")

    def show_key(self, k):
        self.key = k
        state = self.page.ctx.queue.key_state(k["fpr"]) if k.get("fpr") else None
        self.icon.setPixmap(group_icon(k["group"], 48).pixmap(48, 48))
        self.name.setText(key_title(k))
        self.email.setText(k.get("email") or "")
        self._row("id", k.get("keyid"))
        self._row("fpr", keyring.nice_fpr(k["fpr"], 2) if k.get("fpr") else "")
        if k.get("pending"):
            for key in ("created", "expires", "installed", "repos", "packages"):
                self._row(key, "")
            self._note(_("It will be imported when you apply the queue. Until then, nothing changes."), "green")
            self.cmd.setText(_("rpm --import (key {keyid}, checked)").format(keyid=k.get("keyid", "")))
            self.cmd.show()
            self.b_delete.hide()
            self.b_keep.setText(_("Do not import it"))
            self.b_keep.show()
            return
        self._row("created", fmt_date(k.get("created")))
        if k.get("revoked"):
            self._row("expires", _("revoked by its owner"), RED)
        elif k.get("expired"):
            self._row("expires", _("expired on {date}").format(date=fmt_date(k.get("expires"))), ORANGE)
        else:
            self._row("expires", fmt_date(k["expires"]) if k.get("expires") else _("never"))
        self._row("installed", fmt_date(k.get("installed")) or _("unknown"))
        repos = k.get("repos") or []
        self._row("repos", "\n".join(name if enabled else _("{name} (disabled)").format(name=name)
                                     for _alias, name, enabled in repos) or _("no repository"))
        n = k.get("packages")
        self._row("packages", _("unknown: rpm could not tell") if n is None else
                  ngettext("{n} of the packages you have", "{n} of the packages you have", n).format(n=n) if n else
                  _("none of the packages you have"))
        self._note(*self.page.delete_meaning(k, state))
        self.cmd.setText(k.get("remove_command") or "")
        self.cmd.setVisible(not k.get("shipped"))
        if state == "key-remove":
            self.b_delete.hide()
            self.b_keep.setText(_("Keep it"))
            self.b_keep.show()
        else:
            self.b_keep.hide()
            self.b_delete.show()
            self.b_delete.setEnabled(not k.get("shipped") and bool(k.get("fpr")))
            self.b_delete.setToolTip(_("openSUSE ships this key: it is never deleted from here.")
                                     if k.get("shipped") else "")

    def _note(self, text, tone):
        self.note.setText(text)
        self.note.setStyleSheet(NOTE_CSS.get(tone, NOTE_CSS["grey"]))
        self.note.show()

    def _delete(self):
        if self.key:
            self.page.queue_delete([self.key])

    def _keep(self):
        k = self.key
        if not k:
            return
        self.page.ctx.queue.discard("key-import" if k.get("pending") else "key-remove", k["fpr"])


# ---------------------------------------------------------------------------------------------- the page

class KeysPage(Page):
    key = "keys"
    title = _("Signing keys")
    icon_name = "security-high"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.keys = []
        self.busy = False
        self.error = ""
        self._round = 0
        lay = QVBoxLayout(self)
        self.summary = muted(_("Reading the keys…"), self)
        lay.addWidget(self.summary)

        left = Card(None, self)
        left.lay.setContentsMargins(8, 6, 8, 8)
        self.list = QListWidget(self)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(KeyDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setStyleSheet("QListWidget { background: transparent; }")
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show(cur))
        left.lay.addWidget(self.list, 1)

        right = Card(None, self)
        right.lay.setContentsMargins(12, 14, 14, 12)
        self.details = KeyDetails(self, right)
        right.lay.addWidget(self.details)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([640, 430])
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)

        # the header buttons live as long as the page (the window takes them away and puts them back); without a
        # parent until then, so they never show up inside the page
        self.b_unused = button("", "edit-delete", self.delete_unused)
        self.b_import = button(_("Import a key…"), "list-add", self.import_key, primary=True)
        self._update_unused_button()
        ctx.queue.changed.connect(self._queue_changed)

    def header_actions(self):
        return [self.b_unused, self.b_import]

    # ------------------------------------------------------------ loading
    def reload(self):
        self._round += 1
        rnd = self._round
        self.busy = True
        self.error = ""
        if not self.keys:
            self.summary.setText(_("Reading the keys…"))
            self.details.show_message(_("Loading…"))
        self._update_unused_button()
        run_async(keyring.list_keys, on_done=lambda res, err: self._loaded(rnd, res, err))

    def _loaded(self, rnd, res, err):
        if rnd != self._round:
            return  # an older reload answered late
        self.busy = False
        if err:
            self.error = str(err)
            self.summary.setText(_("The keys could not be read: {error}").format(error=self.error))
            if not self.keys:
                self.details.show_message(_("The keys could not be read."))
        else:
            self.keys = res or []
            n = len(self.keys)
            self.summary.setText(ngettext("You have {n} key. Each one lets you install what it signs.",
                                          "You have {n} keys. Each one lets you install what it signs.", n)
                                 .format(n=n))
        self._fill()

    # ------------------------------------------------------------ what each key means
    def pills(self, k):
        out = []
        state = self.ctx.queue.key_state(k["fpr"]) if k.get("fpr") else None
        if state == "key-remove":
            out.append((_("will be deleted"), RED))
        if k.get("pending") or state == "key-import":
            out.append((_("will be imported"), GREEN))
            return out
        if k.get("revoked"):
            out.append((_("revoked"), RED))
        elif k.get("expired"):
            out.append((_("expired"), ORANGE))
        if k["group"] == "repos":
            repos = k["repos"]
            text = repos[0][0] + (f" +{len(repos) - 1}" if len(repos) > 1 else "")
            out.append((text, BLUE if any(e for _a, _n, e in repos) else GREY))
        elif k["group"] == "packages":
            n = k["packages"]
            out.append((ngettext("{n} package", "{n} packages", n).format(n=n), PURPLE))
        if k.get("shipped"):
            out.append((_("kept"), GREY))
        return out

    def delete_meaning(self, k, state=None):
        """A plain sentence about what deleting this key means, and its tone."""
        if state == "key-remove":
            return _("It will be deleted when you apply the queue. Until then, nothing changes."), "red"
        if k.get("shipped"):
            return _("openSUSE ships this key with the system, so it stays: Zypp Desk never deletes it. If it "
                     "were deleted, the next update of openSUSE's keys would bring it back."), "grey"
        repos = k.get("repos") or []
        n = k.get("packages")
        if repos:
            names = ", ".join(name for _a, name, _e in repos)
            return ngettext("Deleting it removes no program, but {names} uses it: at its next refresh, zypper will "
                            "ask you again whether to trust this key, and will not update from it until you say yes.",
                            "Deleting it removes no program, but {names} use it: at their next refresh, zypper will "
                            "ask you again whether to trust this key, and will not update from them until you say "
                            "yes.", len(repos)).format(names=names), "orange"
        if n:
            return ngettext("Deleting it removes no program, but rpm can no longer check the package it signed, "
                            "and a new version signed with this key will be refused until you import it again.",
                            "Deleting it removes no program, but rpm can no longer check the {n} packages it "
                            "signed, and new versions signed with this key will be refused until you import it "
                            "again.", n).format(n=n), "orange"
        text = _("Deleting it removes no program. If you ever add this repository again, zypper will ask you for "
                 "it again.")
        if n is None:
            text = _("Zypp Desk could not read which packages this key signed. If one of yours is signed with it, "
                     "rpm can no longer check it after you delete the key.")
            return text, "orange"
        if k.get("expired"):
            text += " " + _("It has expired anyway.")
        return text, "green"

    # ------------------------------------------------------------ the list
    def _pending_imports(self):
        have = {k["fpr"] for k in self.keys}
        out = []
        for a in self.ctx.queue.of("key-import"):
            if a.target in have:
                continue
            out.append({"fpr": a.target, "keyid": a.target[-8:], "name": a.opts.get("name") or "", "email": "",
                        "group": "queued", "pending": True, "repos": [], "packages": 0, "shipped": False})
        return out

    def _fill(self):
        cur = self.list.currentItem()
        cur_fpr = cur.data(ROW_ROLE)["key"].get("fpr") if cur and cur.data(ROW_ROLE) and \
            not cur.data(ROW_ROLE).get("header") else None
        unknown = any(k.get("packages") is None for k in self.keys)
        titles = group_titles(unknown)
        rows = self._pending_imports() + self.keys
        self.list.blockSignals(True)
        self.list.clear()
        select = None
        for group in ("queued", *keyring.GROUPS):
            members = [k for k in rows if k["group"] == group]
            if not members:
                continue
            hdr = QListWidgetItem()
            hdr.setData(ROW_ROLE, {"header": f"{titles[group]} · {len(members)}"})
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(hdr)
            for i, k in enumerate(members):
                it = QListWidgetItem(key_title(k))  # the text is drawn by the delegate; it is there for screen
                it.setData(ROW_ROLE, {"key": k, "odd": i % 2 == 0})  # readers and to find a key by typing
                tip = [key_title(k)]
                if k.get("email"):
                    tip.append(k["email"])
                if k.get("fpr"):
                    tip.append(keyring.nice_fpr(k["fpr"]))
                it.setToolTip("\n".join(tip))
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.list.addItem(it)
                if select is None or (cur_fpr and k.get("fpr") == cur_fpr):
                    select = it  # the first key, or the one that was selected before
        self.list.blockSignals(False)
        if select is not None:
            self.list.setCurrentItem(select)
            self._show(select)
            if self.list.row(select) <= 1:
                self.list.scrollToTop()  # the group title above the first key stays in sight
        elif not self.busy:
            self.details.show_message(_("No signing keys.") if not self.error else _("The keys could not be read."))
        self._update_unused_button()

    def _show(self, item):
        row = item.data(ROW_ROLE) if item else None
        if row and not row.get("header"):
            self.details.show_key(row["key"])

    def _queue_changed(self):
        self.list.viewport().update()
        if self.keys or self._pending_imports():
            self._fill()

    # ------------------------------------------------------------ actions
    def unused(self):
        """Keys nobody uses that can be deleted (not already waiting in the queue)."""
        return [k for k in self.keys if k["group"] == "unused" and k.get("fpr") and not k.get("shipped")
                and k.get("packages") is not None and self.ctx.queue.key_state(k["fpr"]) != "key-remove"]

    def _update_unused_button(self):
        n = len(self.unused())
        self.b_unused.setText(_("Delete the unused ones") if self.busy and not self.keys else
                              _("Delete the unused ones ({n})").format(n=n))
        self.b_unused.setEnabled(n > 0 and not self.busy)
        self.b_unused.setToolTip(_("Keys no repository uses and that signed none of your packages."))

    def queue_delete(self, keys):
        with self.ctx.queue.batch():
            for k in keys:
                if k.get("fpr") and not k.get("shipped"):
                    self.ctx.queue.add(Action("key-remove", k["fpr"], name=key_title(k), keyid=k.get("keyid", ""),
                                              nvr=k.get("nvr", "")))

    def delete_unused(self):
        keys = self.unused()
        if not keys:
            return
        lines = [f"{k['keyid']}  {key_title(k)}" + (" — " + _("expired") if k.get("expired") else "") for k in keys]
        box = QMessageBox(QMessageBox.Icon.Question, _("Delete the unused keys"),
                          ngettext("Delete {n} key that no repository uses and that signed none of your packages?",
                                   "Delete these {n} keys that no repository uses and that signed none of your "
                                   "packages?", len(keys)).format(n=len(keys)),
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        shown = lines[:12]
        if len(lines) > len(shown):
            shown.append(_("… and {n} more").format(n=len(lines) - len(shown)))
        box.setInformativeText("\n".join(shown) + "\n\n" + _("No program is removed. They go to the queue: nothing "
                                                             "changes until you apply it."))
        box.setDetailedText("\n".join(lines))
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if plain(box).exec() == QMessageBox.StandardButton.Yes:
            self.queue_delete(keys)

    def import_key(self):
        open_import_dialog(self.ctx, self)


# ---------------------------------------------------------------------------------------------- import dialog

SEG_CSS = """
QPushButton[seg="true"] { border: 1px solid rgba(127, 140, 141, 0.45); border-radius: 0px; padding: 6px 14px;
  background: palette(base); }
QPushButton[seg="true"]:hover { background: rgba(39, 174, 96, 0.10); }
QPushButton[seg="true"]:checked { background: #27ae60; color: white; border-color: #1c9450; }
QPushButton[segpos="first"] { border-top-left-radius: 7px; border-bottom-left-radius: 7px; }
QPushButton[segpos="last"] { border-top-right-radius: 7px; border-bottom-right-radius: 7px; }
"""


class ImportKeyDialog(QDialog):
    """Get a key (from a web address, a file or its fingerprint), look at it, and put its import in the queue.

    url / fingerprint: start fetching at once. hint: who publishes the key (for example "Zoom"), shown in the
    label of the field where the fingerprint published by the vendor can be pasted to compare.
    """

    MODES = ("url", "file", "fpr")

    def __init__(self, ctx, parent=None, url="", fingerprint="", hint=""):
        super().__init__(parent)
        self.ctx = ctx
        self.data = None
        self.found = None
        self._round = 0
        self._closed = False
        self.setWindowTitle(_("Import a signing key"))
        self.setMinimumWidth(680)
        self.setStyleSheet(SEG_CSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        lay.addWidget(heading(_("Import a signing key"), 1.2, self))

        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.seg = QButtonGroup(self)
        self.seg.setExclusive(True)
        for i, (mode, text) in enumerate((("url", _("From a web address")), ("file", _("From a file")),
                                          ("fpr", _("By its fingerprint")))):
            b = QPushButton(text, self)
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setProperty("segpos", "first" if i == 0 else "last" if i == 2 else "middle")
            b.setAutoDefault(False)
            self.seg.addButton(b, i)
            seg.addWidget(b)
        seg.addStretch(1)
        lay.addLayout(seg)

        self.inputs = QStackedWidget(self)
        self.url = QLineEdit(url, self)
        self.url.setPlaceholderText("https://…")
        self.url.returnPressed.connect(self.fetch)
        self.file = QLineEdit(self)
        self.file.setReadOnly(True)
        self.file.setPlaceholderText(_("Choose the key file (.asc, .key, .gpg, .pub)"))
        self.fpr_in = QLineEdit(fingerprint, self)
        self.fpr_in.setPlaceholderText(_("40 hexadecimal digits, spaces allowed"))
        self.fpr_in.returnPressed.connect(self.fetch)
        for edit, text, slot in ((self.url, _("Download it"), self.fetch), (self.file, _("Choose…"), self.choose_file),
                                 (self.fpr_in, _("Look it up"), self.fetch)):
            w = QWidget(self)
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(edit, 1)
            b = button(text, None, slot)
            b.setAutoDefault(False)
            h.addWidget(b)
            self.inputs.addWidget(w)
        self.seg.idClicked.connect(self._mode_changed)
        lay.addWidget(self.inputs)
        self.fpr_note = muted(_("It is looked up on keys.openpgp.org, a public key server. Only the key with exactly "
                                "this fingerprint is accepted."), self)
        self.fpr_note.hide()
        lay.addWidget(self.fpr_note)

        self.status = plain_label("", self)
        self.status.hide()
        lay.addWidget(self.status)

        # what the key is
        self.card = Card(None, self)
        self.card.lay.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        self.k_icon = QLabel(self)
        self.k_icon.setFixedSize(40, 40)
        top.addWidget(self.k_icon, 0, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(2)
        self.k_name = plain_label("", self)
        f = self.k_name.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.12)
        self.k_name.setFont(f)
        self.k_email = muted("", self)
        self.k_email.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.k_name)
        body.addWidget(self.k_email)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.k_rows = {}
        for key, label in (("fpr", _("Fingerprint")), ("created", _("Created")), ("expires", _("Expires")),
                           ("signs", _("Signs"))):
            lbl = muted(label, self)
            val = plain_label("", self)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            n = grid.rowCount()
            grid.addWidget(lbl, n, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(val, n, 1)
            self.k_rows[key] = (lbl, val)
        self.k_rows["fpr"][1].setFont(mono())
        body.addSpacing(6)
        body.addLayout(grid)
        self.k_have = plain_label("", self)
        self.k_have.hide()
        body.addWidget(self.k_have)
        top.addLayout(body, 1)
        self.card.lay.addLayout(top)
        self.card.hide()
        lay.addWidget(self.card)

        # compare with what the vendor publishes
        who = hint.strip()
        self.cmp_lbl = plain_label(_("Fingerprint published by {who} (optional, to compare):").format(who=who) if who
                                   else _("Fingerprint the vendor publishes (optional, to compare):"), self)
        lay.addWidget(self.cmp_lbl)
        ch = QHBoxLayout()
        self.cmp = QLineEdit(self)
        self.cmp.setFont(mono())
        self.cmp.setPlaceholderText(_("Paste it from the vendor's web site"))
        self.cmp.textChanged.connect(self._compare)
        self.cmp_mark = QLabel(self)
        self.cmp_mark.setMinimumWidth(130)
        ch.addWidget(self.cmp, 1)
        ch.addWidget(self.cmp_mark)
        lay.addLayout(ch)

        warn = plain_label(_("A key lets rpm install as root anything it signs. Import only keys of sites you "
                             "trust."), self)
        warn.setStyleSheet(NOTE_CSS["orange"])
        lay.addWidget(warn)
        lay.addWidget(hline())

        foot = QHBoxLayout()
        self.cmd = muted("", self, wrap=False)
        self.cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.cmd.setFont(mono())
        foot.addWidget(self.cmd, 1)
        cancel = button(_("Cancel"), None, self.reject)
        cancel.setAutoDefault(False)
        self.b_add = button(_("Add it to the queue"), "list-add", self.add_to_queue, primary=True)
        self.b_add.setAutoDefault(False)
        foot.addWidget(cancel)
        foot.addWidget(self.b_add)
        lay.addLayout(foot)

        start = "fpr" if fingerprint and not url else "url"
        self.seg.button(self.MODES.index(start)).setChecked(True)
        self.inputs.setCurrentIndex(self.MODES.index(start))
        self.fpr_note.setVisible(start == "fpr")
        if fingerprint:
            self.cmp.setText(fingerprint)
        self._update_footer()
        if url or fingerprint:
            QTimer.singleShot(0, self.fetch)

    # ------------------------------------------------------------ getting the key
    def mode(self):
        return self.MODES[self.inputs.currentIndex()]

    def _mode_changed(self, i):
        self.inputs.setCurrentIndex(i)
        self.fpr_note.setVisible(self.MODES[i] == "fpr")
        if self.MODES[i] == "file" and not self.file.text():
            QTimer.singleShot(0, self.choose_file)

    def _set_status(self, text, color=None):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};" if color else "color: palette(placeholder-text);")
        self.status.setVisible(bool(text))

    def _clear(self):
        self.data = None
        self.found = None
        self.card.hide()
        self._update_footer()

    def choose_file(self):
        path, _f = QFileDialog.getOpenFileName(self, _("Choose the public key file"), self.file.text() or "",
                                               _("Keys (*.asc *.key *.pub *.gpg);;All files (*)"))
        if not path:
            return
        self.file.setText(path)
        try:
            with open(path, "rb") as f:
                data = f.read(keyring.MAX_KEY + 1)
        except OSError as e:
            self._clear()
            self._set_status(str(e), RED)
            return
        if len(data) > keyring.MAX_KEY:
            self._clear()
            self._set_status(_("The file is bigger than 1 MiB: it is not a signing key."), RED)
            return
        self._start("file", data)

    def fetch(self):
        mode = self.mode()
        if mode == "url":
            value = self.url.text().strip()
            if not value:
                return
        elif mode == "fpr":
            value = self.fpr_in.text().strip()
            if not keyring.normalize_fpr(value):
                self._clear()
                self._set_status(_("A fingerprint has 40 hexadecimal digits (0-9, A-F)."), RED)
                return
            self.cmp.setText(value)  # the key that comes back must be exactly this one
        else:
            self.choose_file()
            return
        self._start(mode, value)

    def _start(self, mode, value):
        self._round += 1
        rnd = self._round
        self._clear()
        self._set_status(_("Downloading…") if mode == "url" else _("Looking it up on keys.openpgp.org…")
                         if mode == "fpr" else _("Reading the file…"))
        run_async(self._job, mode, value, self.ctx.settings,
                  on_done=lambda res, err: self._done(rnd, res, err))

    @staticmethod
    def _job(mode, value, settings):
        """In a thread: get the bytes, read the key, and look at what it means here."""
        if mode == "url":
            data = keyring.fetch_url(value)
        elif mode == "fpr":
            data = keyring.fetch_fingerprint(value)
        else:
            data = value
        found = keyring.inspect_key(data)
        res = {"data": data, "keys": found}
        if len(found) != 1:
            return res
        k = found[0]
        if mode == "fpr" and k["fpr"] != keyring.normalize_fpr(value):
            raise ValueError(_("The key server sent a different key: it was not used."))
        res["data"] = keyring.armored(data, k["fpr"])
        res["installed"] = keyring.installed_keys().get(k["fpr"])
        try:
            res["files"] = keyring.local_files_signed_by(k["ids"] | k["fprs"], keyring.web_folders(settings))
        except OSError:
            res["files"] = []
        return res

    def _done(self, rnd, res, err):
        if self._closed or rnd != self._round:
            return
        if err:
            self._set_status(str(err), RED)
            return
        found = res["keys"]
        if not found:
            self._set_status(_("There is no signing key in it."), RED)
            return
        if len(found) > 1:
            self._set_status(ngettext("It holds {n} keys: import them one at a time, so that you can check each "
                                      "fingerprint.", "It holds {n} keys: import them one at a time, so that you can "
                                      "check each fingerprint.", len(found)).format(n=len(found)), RED)
            return
        self._set_status("")
        self.data = res["data"]
        self.found = dict(found[0], installed_key=res.get("installed"), files=res.get("files") or [])
        self._show_found()

    def _show_found(self):
        k = self.found
        have = k.get("installed_key")
        self.k_icon.setPixmap(shield("bad" if k["expired"] or k["revoked"] else "ok",
                                     RED if k["expired"] or k["revoked"] else GREEN, 40).pixmap(40, 40))
        self.k_name.setText(k["name"] or _("(no name in the key)"))
        self.k_email.setText(k["email"])
        self.k_email.setVisible(bool(k["email"]))
        self.k_rows["fpr"][1].setText(keyring.nice_fpr(k["fpr"]))
        self.k_rows["created"][1].setText(fmt_date(k["created"]))
        exp = self.k_rows["expires"][1]
        if k["revoked"]:
            exp.setText(_("revoked by its owner"))
            exp.setStyleSheet(f"color: {RED};")
        elif k["expired"]:
            exp.setText(_("expired on {date}").format(date=fmt_date(k["expires"])))
            exp.setStyleSheet(f"color: {ORANGE};")
        else:
            exp.setText(fmt_date(k["expires"]) if k["expires"] else _("never"))
            exp.setStyleSheet("")
        files = [Path(f).name for f in k["files"]]
        sign_lbl, sign = self.k_rows["signs"]
        if files:
            # one file a line: file names have no spaces, a list of them would not wrap
            lines = ["✓ " + _("{file}, in your folder").format(file=f) for f in files[:3]]
            if len(files) > 3:
                lines.append(_("… and {n} more").format(n=len(files) - 3))
            sign.setText("\n".join(lines))
            sign.setStyleSheet(f"color: {GREEN};")
        sign_lbl.setVisible(bool(files))
        sign.setVisible(bool(files))
        if have:
            longer = (k["expires"] is None and have.get("expires")) or \
                (k["expires"] and have.get("expires") and k["expires"] > have["expires"])
            self.k_have.setText(_("You already have it. This copy is valid for longer: importing it updates it.")
                                if longer else _("You already have it."))
            self.k_have.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
            self.k_have.show()
            k["update"] = bool(longer)
        else:
            self.k_have.hide()
        self.card.show()
        self._compare()
        QTimer.singleShot(0, self._grow)

    def _grow(self):
        """Make room for the key card (word-wrapped labels do not ask the dialog to grow by themselves)."""
        lay = self.layout()
        lay.activate()
        need = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        if need > self.height():
            self.resize(self.width(), need)

    # ------------------------------------------------------------ compare and add
    def _compare(self, *_a):
        text = self.cmp.text().strip()
        if not text or not self.found:
            self.cmp_mark.setText("")
        elif keyring.normalize_fpr(text) == self.found["fpr"]:
            self.cmp_mark.setText("✓ " + _("matches"))
            self.cmp_mark.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
        else:
            self.cmp_mark.setText("✗ " + _("does not match"))
            self.cmp_mark.setStyleSheet(f"color: {RED}; font-weight: 600;")
        self._update_footer()

    def _compared(self):
        """True: the pasted fingerprint matches; False: it does not; None: nothing pasted."""
        text = self.cmp.text().strip()
        if not text or not self.found:
            return None
        return keyring.normalize_fpr(text) == self.found["fpr"]

    def _update_footer(self):
        k = self.found
        cmp = self._compared()
        if not k:
            self.cmd.setText("rpm --import")
            self.b_add.setEnabled(False)
            self.b_add.setToolTip("")
            return
        self.cmd.setText(_("rpm --import (key {keyid}, checked)").format(keyid=k["fpr"][-8:]) if cmp else
                         _("rpm --import (key {keyid})").format(keyid=k["fpr"][-8:]))
        self.cmd.setToolTip(_("Before importing it, Zypp Desk checks as root that the file still holds exactly the "
                              "key with the fingerprint {fpr}.").format(fpr=keyring.nice_fpr(k["fpr"])))
        reason = ""
        if cmp is False:
            reason = _("The fingerprint you pasted does not match this key.")
        elif k.get("installed_key") and not k.get("update"):
            reason = _("You already have it.")
        elif k["revoked"]:
            reason = _("Its owner revoked this key: it must not be used.")
        self.b_add.setEnabled(not reason)
        self.b_add.setToolTip(reason)

    def add_to_queue(self):
        k = self.found
        if not k or not self.data or not self.b_add.isEnabled():
            return
        try:
            path = keyring.save_for_import(self.data, k["fpr"])
        except (OSError, ValueError) as e:
            error_box(self, self.windowTitle(), str(e))
            return
        self.ctx.queue.add(Action("key-import", k["fpr"], path=path, name=k["name"] or k["email"] or k["fpr"][-8:]))
        self.accept()

    def done(self, r):
        self._closed = True  # answers that arrive later are dropped
        super().done(r)


def open_import_dialog(ctx, parent=None, **kw):
    """Open the import dialog (url=, fingerprint=, hint=); True when a key was put in the queue."""
    d = ImportKeyDialog(ctx, parent, **kw)
    try:
        return d.exec() == QDialog.DialogCode.Accepted
    finally:
        d.deleteLater()
