# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit the update routine with the mouse: one card per step, how each step joins the previous one, and the
line it makes in a terminal. "Write it by hand" switches to the text and back.

    dlg = RoutineEditorDialog(settings["routine"], parent)
    if dlg.exec():
        settings["routine"] = dlg.text()
"""

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
                               QLabel, QLayout, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import routine
from ..i18n import _
from ..style import BLUE, GREEN, RED
from ..ui import button, heading, hline, icon, mono, muted, plain, tool

MIME = "application/x-zypp-desk-routine-step"


kind_title = routine.kind_title  # one name per kind of step, also in the routine band of Updates


def add_title(kind):
    """The shorter name on the "+ Add a step" buttons."""
    if kind == "download":
        return _("Download the updates")
    if kind == "custom":
        return _("Own command…")
    return kind_title(kind)


def kind_help(kind):
    return {
        "refresh": _("Get the newest package lists from your repositories."),
        "download": _("Fetch everything the update needs. A later step installs it quickly."),
        "dup": _("Bring the whole system to the newest version. This is how Tumbleweed is updated."),
        "update": _("Update the installed packages, without the bigger changes that dup makes."),
        "flatpak": _("Update the apps you installed with Flatpak."),
        "firmware": _("Install new firmware for the computer and its devices. fwupd asks for the password "
                      "itself."),
        "restart-check": _("List the programs that still use old files and should be restarted."),
        "purge-kernels": _("Remove the kernels you do not need any more. The one running now stays."),
        "custom": _("Any zypper, flatpak or fwupdmgr command, written as in a terminal."),
    }[kind]


def option_label(opt):
    return {
        "verbose": _("Explain everything (-v)"),
        "force": _("Force it (-f)"),
        "licenses": _("Accept licenses (-l)"),
        "no-recommends": _("Without recommended packages"),
        "vendor-change": _("Allow a change of vendor"),
        "no-refresh": _("Do not refresh again"),
    }[opt]


def option_help(opt):
    return {
        "verbose": _("The terminal shows more of what zypper does (-v)."),
        "force": _("Download the lists again, even if they look up to date (-f)."),
        "licenses": _("Some packages (drivers, fonts) ask you to accept a license. With this the answer is "
                      "yes (-l). The update steps after this one say yes too."),
        "no-recommends": _("Install only what the packages need, not what they suggest (--no-recommends)."),
        "vendor-change": _("Let a package move to another vendor, for example from openSUSE to Packman "
                           "(--allow-vendor-change)."),
        "no-refresh": _("Install just what the steps before downloaded, without looking at the lists again "
                        "(--no-refresh)."),
    }[opt]


class FlowLayout(QLayout):
    """Widgets in a row that wraps onto the next line when there is no room."""

    def __init__(self, parent=None, hspacing=6, vspacing=6):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
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
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _arrange(self, rect, apply):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            if it.widget() is not None and it.widget().isHidden():
                continue
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._v, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line = max(line, hint.height())
        return y + line - rect.y()


class DragHandle(QLabel):
    """The ⋮⋮ on the left of a step: drag it to move the step."""

    def __init__(self, card):
        super().__init__("⋮⋮", card)
        self.card = card
        self.setStyleSheet("color: palette(placeholder-text); font-size: 15px; padding-top: 3px;")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(_("Drag to change the order"))
        self._start = None

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._start = ev.position().toPoint()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._start is None or not ev.buttons() & Qt.MouseButton.LeftButton:
            return
        if (ev.position().toPoint() - self._start).manhattanLength() < QApplication.startDragDistance():
            return
        self._start = None
        mime = QMimeData()
        mime.setData(MIME, str(self.card.index).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self.card.frame.grab()
        if pm.width() > 420:
            pm = pm.scaledToWidth(420, Qt.TransformationMode.SmoothTransformation)
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(16, 16))
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, ev):
        self._start = None
        super().mouseReleaseEvent(ev)


class StepCard(QWidget):
    """One step: how it joins the previous one (not for the first), then the card itself."""

    def __init__(self, editor, index):
        super().__init__()
        self.editor = editor
        self.index = index
        item = editor.items[index]
        count = len(editor.items)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        if index > 0:
            v.addWidget(self._link(item))

        self.frame = QFrame(self)
        self.frame.setObjectName("card")
        h = QHBoxLayout(self.frame)
        h.setContentsMargins(10, 10, 8, 10)
        h.setSpacing(10)
        h.addWidget(DragHandle(self), 0, Qt.AlignmentFlag.AlignTop)
        badge = QLabel(str(index + 1), self.frame)
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"border-radius: 13px; background: {BLUE}; color: white; font-weight: bold;")
        h.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(8)
        self.combo = QComboBox(self.frame)
        for k in routine.KINDS:
            self.combo.addItem(kind_title(k), k)
            self.combo.setItemData(self.combo.count() - 1, kind_help(k), Qt.ItemDataRole.ToolTipRole)
        self.combo.setMinimumWidth(330)
        f = self.combo.font()
        f.setBold(True)
        self.combo.setFont(f)
        self.combo.setCurrentIndex(routine.KINDS.index(item["kind"]) if item["kind"] in routine.KINDS
                                   else routine.KINDS.index("custom"))
        self.combo.currentIndexChanged.connect(self._kind_changed)
        body.addWidget(self.combo, 0, Qt.AlignmentFlag.AlignLeft)

        self.opts_box = QWidget(self.frame)
        self.opts_flow = FlowLayout(self.opts_box, hspacing=16, vspacing=4)
        body.addWidget(self.opts_box)
        self.help_lbl = muted("", self.frame)
        self.help_lbl.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.help_lbl)
        self.custom = QLineEdit(self.frame)
        self.custom.setFont(mono())
        self.custom.setPlaceholderText(_("For example: zypper -v patch"))
        self.custom.setClearButtonEnabled(True)
        self.custom.textEdited.connect(self._custom_edited)
        body.addWidget(self.custom)
        self.custom_err = QLabel("", self.frame)
        self.custom_err.setTextFormat(Qt.TextFormat.PlainText)
        self.custom_err.setWordWrap(True)
        self.custom_err.setStyleSheet(f"color: {RED};")
        body.addWidget(self.custom_err)
        h.addLayout(body, 1)

        tools = QHBoxLayout()
        tools.setSpacing(0)
        for text, tip, slot, enabled in (
                ("↑", _("Move step {n} up").format(n=index + 1),
                 lambda: editor.move_step(self.index, self.index - 1), index > 0),
                ("↓", _("Move step {n} down").format(n=index + 1),
                 lambda: editor.move_step(self.index, self.index + 1), index < count - 1),
                ("✕", _("Take step {n} out of the routine").format(n=index + 1),
                 lambda: editor.remove_step(self.index), True)):
            b = QToolButton(self.frame)
            b.setText(text)
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.setAutoRaise(True)
            b.setEnabled(enabled)
            b.clicked.connect(slot)
            tools.addWidget(b)
        h.addLayout(tools)
        h.setAlignment(tools, Qt.AlignmentFlag.AlignTop)
        v.addWidget(self.frame)
        self._fill()

    def _link(self, item):
        """The two-button choice between this step and the one before."""
        w = QWidget(self)
        h = QHBoxLayout(w)
        h.setContentsMargins(48, 6, 0, 6)
        h.setSpacing(10)
        h.addWidget(muted("↓", w, wrap=False))
        seg = QHBoxLayout()
        seg.setSpacing(0)
        group = QButtonGroup(w)
        for cond, side, text, tip in (
                ("always", "left", _("Afterwards, whatever happens"),
                 _("Like ';' in a terminal: this step runs even if the one before went wrong.")),
                ("success", "right", _("Only if it went well"),
                 _("Like '&&' in a terminal: this step runs only if the one before went well."))):
            b = QPushButton(text, w)
            b.setProperty("seg", side)
            b.setCheckable(True)
            b.setAutoDefault(False)
            b.setToolTip(tip)
            b.setChecked(item["cond"] == cond)
            b.toggled.connect(lambda on, c=cond: on and self._set_cond(c))
            group.addButton(b)
            seg.addWidget(b)
        h.addLayout(seg)
        h.addStretch(1)
        return w

    def _set_cond(self, cond):
        self.editor.items[self.index]["cond"] = cond
        self.editor.changed()

    def _fill(self):
        """The part of the card that depends on the kind: options, help line or the command field."""
        item = self.editor.items[self.index]
        kind = item["kind"]
        while self.opts_flow.count():
            w = self.opts_flow.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        for opt in routine.KIND_OPTIONS.get(kind, ()):
            cb = QCheckBox(option_label(opt), self.opts_box)
            cb.setToolTip(option_help(opt))
            cb.setChecked(opt in item["opts"])
            cb.toggled.connect(lambda on, o=opt: self._set_opt(o, on))
            self.opts_flow.addWidget(cb)
        self.opts_box.setVisible(bool(routine.KIND_OPTIONS.get(kind)))
        self.help_lbl.setText(kind_help(kind) if kind != "custom" else "")
        self.help_lbl.setVisible(kind != "custom" and not routine.KIND_OPTIONS.get(kind))
        self.custom.setVisible(kind == "custom")
        if kind == "custom":
            self.custom.setText(item.get("raw") or "")
        self._check()

    def _check(self):
        item = self.editor.items[self.index]
        err = routine.item_error(item) if item["kind"] == "custom" else None
        self.custom_err.setText(err or "")
        self.custom_err.setVisible(bool(err))

    def _set_opt(self, opt, on):
        opts = self.editor.items[self.index]["opts"]
        if on:
            opts.add(opt)
        else:
            opts.discard(opt)
        self.editor.changed()

    def _kind_changed(self):
        item = self.editor.items[self.index]
        kind, old = self.combo.currentData(), item["kind"]
        if kind == old:
            return
        if kind == "custom":
            item["raw"] = routine.item_command(item)  # start from what the step did
            item["opts"] = set()
        else:
            keep = set() if old == "custom" else item["opts"] & set(routine.KIND_OPTIONS[kind])
            item["opts"] = keep or set(routine.DEFAULT_OPTIONS.get(kind, ()))
        item["kind"] = kind
        self._fill()
        if kind == "custom":
            self.custom.setFocus()
        self.editor.changed()

    def _custom_edited(self, text):
        self.editor.items[self.index]["raw"] = text
        self._check()
        self.editor.changed()


class StepList(QWidget):
    """The steps, one under the other. Takes the drops of a dragged step and shows where it would go."""

    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setAcceptDrops(True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 4, 0)
        self.lay.setSpacing(0)
        self.lay.addStretch(1)
        self.line = QFrame(self)
        self.line.setStyleSheet(f"background: {GREEN}; border-radius: 2px;")
        self.line.hide()

    def slot_at(self, y):
        """Where a step dropped at height y would go (0 = first)."""
        cards = self.editor.cards
        for i, c in enumerate(cards):
            frame = c.frame.geometry().translated(c.pos())
            if y < frame.center().y():
                return i
        return len(cards)

    def _show_line(self, slot):
        cards = self.editor.cards
        if not cards:
            return
        if slot < len(cards):
            c = cards[slot]
            y = c.frame.geometry().translated(c.pos()).top() - 4
        else:
            c = cards[-1]
            y = c.geometry().bottom() + 3
        self.line.setGeometry(40, max(0, y - 2), self.width() - 60, 4)
        self.line.raise_()
        self.line.show()

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            self._show_line(self.slot_at(ev.position().toPoint().y()))
            ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        self.line.hide()

    def dropEvent(self, ev):
        self.line.hide()
        if not ev.mimeData().hasFormat(MIME):
            return
        src = int(bytes(ev.mimeData().data(MIME)).decode() or -1)
        slot = self.slot_at(ev.position().toPoint().y())
        dst = slot - 1 if slot > src else slot
        ev.acceptProposedAction()
        # the card being dragged is the source of the drag: rebuild only once the drag is over
        QTimer.singleShot(0, lambda: self.editor.move_step(src, dst))


class RoutineEditorDialog(QDialog):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Edit the routine"))
        self._text = (text or "").strip()
        self.items = []
        self.cards = []
        self.setStyleSheet(
            "QPushButton[seg] { border: 1px solid rgba(127, 140, 141, 0.55); padding: 4px 12px;"
            " background: palette(base); }"
            "QPushButton[seg=\"left\"] { border-top-left-radius: 7px; border-bottom-left-radius: 7px;"
            " border-right: none; }"
            "QPushButton[seg=\"right\"] { border-top-right-radius: 7px; border-bottom-right-radius: 7px; }"
            # the chosen way to join two steps: the selection colour, as the chips; green is kept for the one
            # main button ("Save the routine")
            "QPushButton[seg]:checked { background: palette(highlight); color: palette(highlighted-text);"
            " border-color: palette(highlight); }"
            "QFrame#preview { background: rgba(127, 140, 141, 0.13); border-radius: 10px; }")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(heading(_("Edit the routine"), 1.3, self))
        self.subtitle = muted("", self)
        lay.addWidget(self.subtitle)

        self.stack = QStackedWidget(self)
        lay.addWidget(self.stack, 1)

        # ------------------------------------------------ page 0: the steps
        steps_page = QWidget(self)
        sv = QVBoxLayout(steps_page)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(10)
        self.scroll = QScrollArea(steps_page)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(200)
        self.list = StepList(self)
        self.scroll.setWidget(self.list)
        self.scroll.viewport().setAutoFillBackground(False)
        self.list.setAutoFillBackground(False)
        self.empty = muted(_("There are no steps yet. Add one below."), self.list)
        self.list.lay.insertWidget(0, self.empty)
        sv.addWidget(self.scroll, 1)

        # every button after this label only adds a step at the end of the routine: nothing runs (as "To the
        # queue:" on the package card)
        add_lbl = QLabel(_("+ Add a step:"), steps_page)
        add_lbl.setToolTip(_("These buttons add a step at the end of the routine. Nothing runs now."))
        f = add_lbl.font()
        f.setBold(True)
        add_lbl.setFont(f)
        sv.addWidget(add_lbl)
        add_box = QWidget(steps_page)
        add_flow = FlowLayout(add_box, hspacing=6, vspacing=6)
        for kind in routine.KINDS:
            b = QPushButton(add_title(kind), add_box)
            b.setToolTip(_("Adds this step at the end of the routine. Nothing runs now.") + "\n" + kind_help(kind))
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, k=kind: self.add_step(k))
            add_flow.addWidget(b)
        sv.addWidget(add_box)

        preview = QFrame(steps_page)
        preview.setObjectName("preview")
        preview.setToolTip(_("When it runs, zypper also gets -n, flatpak -y and fwupdmgr --assume-yes "
                             "--no-unreported-check: nobody can answer questions in the middle."))
        pv = QVBoxLayout(preview)
        pv.setContentsMargins(12, 8, 12, 10)
        pv.setSpacing(3)
        pt = muted(_("As written in a terminal"), preview)
        pv.addWidget(pt)
        self.preview = QLabel("", preview)
        self.preview.setFont(mono())
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setStyleSheet("color: palette(link);")
        pv.addWidget(self.preview)
        sv.addWidget(preview)
        self.stack.addWidget(steps_page)

        # ------------------------------------------------ page 1: by hand
        hand_page = QWidget(self)
        hv = QVBoxLayout(hand_page)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(6)
        self.text_edit = QPlainTextEdit(hand_page)
        self.text_edit.setFont(mono())
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.textChanged.connect(self._check_text)
        self.text_edit.setFixedHeight(150)
        hv.addWidget(self.text_edit)
        self.text_err = QLabel("", hand_page)
        self.text_err.setTextFormat(Qt.TextFormat.PlainText)
        self.text_err.setWordWrap(True)
        self.text_err.setStyleSheet(f"color: {RED};")
        self.text_err.hide()
        hv.addWidget(self.text_err)
        hv.addWidget(muted(_("A new line counts as ';'. Sudo is not needed: Zypp Desk asks for the password."),
                           hand_page))
        hv.addStretch(1)
        self.stack.addWidget(hand_page)

        # ------------------------------------------------ errors on saving, and the buttons
        self.error = QLabel("", self)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {RED}; font-weight: 600;")
        self.error.hide()
        lay.addWidget(self.error)
        lay.addWidget(hline())
        foot = QHBoxLayout()
        self.hand_btn = tool(_("Write it by hand"), "document-edit", self.toggle_hand, self)
        self.hand_btn.setStyleSheet("color: palette(link);")
        foot.addWidget(self.hand_btn)
        proposed = tool(_("Proposed routines…"), "view-list-details", self.open_proposals, self)
        proposed.setStyleSheet("color: palette(link);")
        proposed.setToolTip(_("Opens ready routines to start from, the first one made for this computer. The one "
                              "you choose replaces these steps; nothing is saved until you save the routine."))
        foot.addWidget(proposed)
        foot.addStretch(1)
        usual = button(_("Back to the usual routine…"), None, self.back_to_usual)
        usual.setToolTip(_("Asks first, then replaces these steps with the usual routine (nothing is saved until "
                           "you save the routine):") + "\n" + routine.DEFAULT_ROUTINE)
        cancel = button(_("Cancel"), None, self.reject)
        cancel.setToolTip(_("Close without saving: your routine stays as it was."))
        # it saves the routine only: nothing runs (that is "Update everything" on the Updates page)
        save = button(_("Save the routine"), None, self.save, primary=True)
        save.setToolTip(_("Saves these steps as your routine. Nothing runs now."))
        for b in (usual, cancel):
            b.setAutoDefault(False)
        save.setDefault(True)
        for b in (usual, cancel, save):
            foot.addWidget(b)
        lay.addLayout(foot)

        try:
            self.items = routine.to_items(self._text)
        except routine.RoutineError:
            # it cannot be split into steps: open it as text, with the problem under it
            self.text_edit.setPlainText(self._text)
            self._show_hand(True)
            self._check_text()
        else:
            self._show_hand(False)
            self.rebuild()

        size = QSize(860, 760)
        scr = (parent.screen() if parent is not None else None) or QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            size = QSize(min(size.width(), avail.width() - 40), min(size.height(), avail.height() - 50))
        self.resize(size)

    # ------------------------------------------------ the result
    def text(self):
        """The routine: the new one after Save, the one given otherwise."""
        return self._text

    # ------------------------------------------------ steps
    def rebuild(self, focus=None):
        for c in self.cards:
            self.list.lay.removeWidget(c)
            c.hide()
            c.deleteLater()
        self.cards = []
        for i in range(len(self.items)):
            card = StepCard(self, i)
            self.list.lay.insertWidget(i, card)
            self.cards.append(card)
        self.empty.setVisible(not self.items)
        self.changed()
        if focus is not None and 0 <= focus < len(self.cards):
            card = self.cards[focus]
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(card, 0, 10))

    def changed(self):
        self.preview.setText(routine.from_items(self.items) or "—")
        self.error.hide()

    def add_step(self, kind):
        self.items.append(routine.new_item(kind))
        self.rebuild(focus=len(self.items) - 1)
        if kind == "custom":
            self.cards[-1].custom.setFocus()

    def move_step(self, src, dst):
        if not (0 <= src < len(self.items) and 0 <= dst < len(self.items)) or src == dst:
            return
        self.items.insert(dst, self.items.pop(src))
        self.rebuild(focus=dst)

    def remove_step(self, index):
        if 0 <= index < len(self.items):
            del self.items[index]
            self.rebuild()

    # ------------------------------------------------ by hand
    def _show_hand(self, hand):
        self.stack.setCurrentIndex(1 if hand else 0)
        self.hand_btn.setText(_("Use the steps") if hand else _("Write it by hand"))
        self.hand_btn.setIcon(icon("view-list-details", "view-list-text") if hand else icon("document-edit"))
        self.hand_btn.setToolTip(_("Back to the steps, one card each.") if hand else
                                 _("Shows the routine as one line of text, to write it as in a terminal."))
        self.subtitle.setText(
            _("Write it as in a terminal. ';' runs the next command anyway, '&&' only if the previous one "
              "worked. Only zypper, flatpak and fwupdmgr commands are allowed.") if hand else
            _("The steps run one after the other. Drag them to change the order."))
        self.error.hide()

    def in_hand_mode(self):
        return self.stack.currentIndex() == 1

    def toggle_hand(self):
        if not self.in_hand_mode():
            self.text_edit.setPlainText(routine.from_items(self.items))
            self._show_hand(True)
            self.text_edit.setFocus()
            return
        try:
            items = routine.to_items(join_lines(self.text_edit.toPlainText()))
        except routine.RoutineError as e:
            self.text_err.setText(str(e))
            self.text_err.show()
            return
        self.items = items
        self._show_hand(False)
        self.rebuild()

    def _check_text(self):
        text = join_lines(self.text_edit.toPlainText())
        try:
            routine.parse(text)
        except routine.RoutineError as e:
            self.text_err.setText(str(e))
            self.text_err.show()
        else:
            self.text_err.hide()
        self.error.hide()

    # ------------------------------------------------ buttons
    def back_to_usual(self):
        if not ask_usual(self):
            return
        if self.in_hand_mode():
            self.text_edit.setPlainText(routine.DEFAULT_ROUTINE)
        else:
            self.items = routine.to_items(routine.DEFAULT_ROUTINE)
            self.rebuild()

    def open_proposals(self):
        """Ready routines, the first one made for this computer; the chosen one replaces the steps (not saved
        until Save)."""
        from .routine_proposals import ProposalsDialog
        current = join_lines(self.text_edit.toPlainText()) if self.in_hand_mode() else routine.from_items(self.items)
        dlg = ProposalsDialog(current, self)
        try:
            if not dlg.exec() or not dlg.chosen:
                return
            text = dlg.chosen
        finally:
            dlg.deleteLater()
        if self.in_hand_mode():
            self.text_edit.setPlainText(text)
        else:
            self.items = routine.to_items(text)
            self.rebuild()

    def save(self):
        if self.in_hand_mode():
            text = join_lines(self.text_edit.toPlainText())
        else:
            for i, it in enumerate(self.items):
                err = routine.item_error(it)
                if err:
                    self._fail(_("Step {n}: {error}").format(n=i + 1, error=err))
                    self.scroll.ensureWidgetVisible(self.cards[i], 0, 10)
                    return
            text = routine.from_items(self.items)
        try:
            routine.parse(text)
        except routine.RoutineError as e:
            self._fail(str(e))
            return
        self._text = text.strip()
        self.accept()

    def _fail(self, message):
        if self.in_hand_mode():  # the problem is already in red under the text
            self.text_err.setText(message)
            self.text_err.show()
            return
        self.error.setText(_("Not saved. {problem}").format(problem=message))
        self.error.show()


def ask_usual(parent):
    """"Replace these steps with the usual routine?": buttons that say what they do, and Enter keeps the steps."""
    box = QMessageBox(QMessageBox.Icon.Question, _("Back to the usual routine"),
                      _("Replace these steps with the usual routine? Nothing is saved until you save the routine.")
                      + "\n\n" + routine.DEFAULT_ROUTINE, QMessageBox.StandardButton.NoButton, parent)
    keep = box.addButton(_("Keep these steps"), QMessageBox.ButtonRole.RejectRole)
    use = box.addButton(_("Use the usual routine"), QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    try:
        plain(box).exec()
        return box.clickedButton() is use
    finally:
        box.deleteLater()


def join_lines(text):
    """A routine written on several lines: as in a shell, a new line is a ';', unless the line before ends
    with ';' or '&&' or the next one starts with them."""
    out = ""
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if out and not (out.endswith((";", "&&")) or line.startswith((";", "&&"))):
            out += " ;"
        out += (" " if out else "") + line
    return out
