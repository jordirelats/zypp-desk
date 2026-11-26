# SPDX-License-Identifier: GPL-3.0-or-later
"""What a change did, and "Undo…" (operations.py keeps what each change did to the installed packages).

OperationBand: the band over the page after a change that touched packages: what it did (done, stopped half way or
ended with errors), "Show it" and "Undo…". OperationsDialog: every operation kept, each with what it did and "Undo…"
(History has a button for it). undo(): what undoing means now, a question, then the changes go to the queue and the
whole queue is simulated: nothing changes until it is applied, as for any other change.
"""

import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QSplitter, QToolButton, QVBoxLayout)

from .. import operations
from ..i18n import _, ngettext
from ..tasks import run_async
from ..txqueue import Action
from ..ui import button, icon


def when_text(t):
    d = datetime.datetime.fromtimestamp(t)
    today = datetime.date.today()
    if d.date() == today:
        return _("today at {time}").format(time=d.strftime("%H:%M"))
    if d.date() == today - datetime.timedelta(days=1):
        return _("yesterday at {time}").format(time=d.strftime("%H:%M"))
    return d.strftime("%d/%m/%Y %H:%M")


def what_text(op):
    """'36 removed, 2 installed, 1 updated'."""
    parts = []
    if op.get("removed"):
        n = len(op["removed"])
        parts.append(ngettext("{n} package removed", "{n} packages removed", n).format(n=n))
    if op.get("installed"):
        n = len(op["installed"])
        parts.append(ngettext("{n} package installed", "{n} packages installed", n).format(n=n))
    if op.get("changed"):
        n = len(op["changed"])
        parts.append(ngettext("{n} package updated or changed", "{n} packages updated or changed", n).format(n=n))
    return ", ".join(parts)


def names_text(names, n=3):
    shown, more = operations.short_names(names, n)
    text = ", ".join(shown)
    if more:
        text = ngettext("{names} and {n} more", "{names} and {n} more", more).format(names=text, n=more)
    return text


def headline(op):
    what = what_text(op)
    return {"done": _("Done: {what}."), "stopped": _("Stopped half way: {what}."),
            "failed": _("Ended with errors: {what}.")}.get(op.get("outcome"), _("Done: {what}.")).format(what=what)


def details_text(op):
    """Everything an operation did, one package per line, for "Show it"."""
    lines = [headline(op), when_text(op["time"]) + " · " + op.get("title", ""), ""]
    for key, title in (("removed", _("Removed")), ("installed", _("Installed")), ("changed", _("Changed"))):
        items = op.get(key) or []
        if not items:
            continue
        lines.append(f"{title} ({len(items)}):")
        for it in items:
            if key == "changed":
                lines.append(f"  {it[0]}  {operations.plain_evr(it[1])} → {operations.plain_evr(it[2])}")
            else:
                lines.append(f"  {it[0]}  {operations.plain_evr(it[1])}")
        lines.append("")
    if op.get("commands"):
        lines.append(_("Commands:"))
        lines += ["  " + c for c in op["commands"]]
    return "\n".join(lines)


class OperationBand(QFrame):
    """Over the page, after a change: what it did, "Show it" and "Undo…". Closed with ×, or by the next one."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.op = None
        self.setObjectName("opband")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(10)
        self.pic = QLabel(self)
        lay.addWidget(self.pic)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = QLabel(self)
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = QLabel(self)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)  # package names come from outside
        self.detail.setStyleSheet("color: palette(placeholder-text);")
        self.detail.setWordWrap(True)
        col.addWidget(self.title)
        col.addWidget(self.detail)
        lay.addLayout(col, 1)
        self.b_show = button(_("Show what it did"), "view-list-details", self._show)
        self.b_undo = button(_("Undo…"), "edit-undo", self._undo)
        self.b_undo.setToolTip(_("Puts back what this change removed and removes what it installed (after a "
                                 "question, through the queue and its simulation)."))
        close = QToolButton(self)
        close.setText("×")
        close.setAutoRaise(True)
        close.setToolTip(_("Hide this band (History → “Zypp Desk changes…” still has it)"))
        close.clicked.connect(self.hide)
        for w in (self.b_show, self.b_undo, close):
            lay.addWidget(w)
        self.hide()

    def show_operation(self, op):
        self.op = op
        ok = op.get("outcome") == "done"
        color = "39,174,96" if ok else "246,116,0"
        self.setStyleSheet(f"QFrame#opband {{ border: 1px solid rgba({color},0.6); border-radius: 10px;"
                           f" background: rgba({color},0.08); }}")
        self.pic.setPixmap(icon("dialog-ok-apply" if ok else "dialog-warning", "emblem-ok").pixmap(28, 28))
        self.title.setText(headline(op))
        names = [x[0] for x in (op.get("removed") or [])] + [x[0] for x in (op.get("installed") or [])] + \
                [x[0] for x in (op.get("changed") or [])]
        extra = _("If something does not work as before, you can undo it.") if ok else \
            _("You can undo what was done.")
        self.detail.setText(names_text(names) + ". " + extra)
        self.show()

    def _show(self):
        if self.op:
            OperationsDialog.open(self.ctx, self, select=self.op["id"])

    def _undo(self):
        if self.op:
            undo(self.ctx, self.op, self)


class OperationsDialog(QDialog):
    """Every change kept, the newest first; the chosen one's packages and "Undo…"."""

    @classmethod
    def open(cls, ctx, parent=None, select=None):
        dlg = cls(ctx, parent, select)
        dlg.exec()
        dlg.deleteLater()

    def __init__(self, ctx, parent=None, select=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(_("Changes made with Zypp Desk"))
        self.resize(860, 560)
        lay = QVBoxLayout(self)
        intro = QLabel(_("Every change that installed, removed or updated packages, with what it did. “Undo…” "
                         "puts back what it removed and removes what it installed."), self)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        self.list = QListWidget(split)
        self.text = QPlainTextEdit(split)
        self.text.setReadOnly(True)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)
        self.ops = operations.load_all()
        for op in self.ops:
            it = QListWidgetItem(f"{when_text(op['time'])}\n{what_text(op)}")
            it.setData(Qt.ItemDataRole.UserRole, op["id"])
            it.setIcon(icon("dialog-ok-apply" if op.get("outcome") == "done" else "dialog-warning", "emblem-ok"))
            self.list.addItem(it)
        if not self.ops:
            self.text.setPlainText(_("Nothing yet: changes made with Zypp Desk will show here."))
        self.list.currentItemChanged.connect(lambda cur, _p: self._show(cur))
        buttons = QDialogButtonBox(self)
        self.b_undo = button(_("Undo…"), "edit-undo", self._undo)
        self.b_undo.setEnabled(False)
        buttons.addButton(self.b_undo, QDialogButtonBox.ButtonRole.ActionRole)
        close = buttons.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        close.setDefault(True)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        idx = next((i for i, op in enumerate(self.ops) if op["id"] == select), 0)
        if self.ops:
            self.list.setCurrentRow(idx)

    def _op(self):
        it = self.list.currentItem()
        oid = it.data(Qt.ItemDataRole.UserRole) if it else None
        return next((op for op in self.ops if op["id"] == oid), None)

    def _show(self, item):
        op = self._op()
        self.b_undo.setEnabled(op is not None)
        if op:
            self.text.setPlainText(details_text(op))

    def _undo(self):
        op = self._op()
        if op:
            self.accept()
            undo(self.ctx, op, self.parent())


def undo(ctx, op, parent=None):
    """What undoing means now (in a thread: zypper and rpm), then the question; yes puts it in the queue and the
    whole queue is simulated."""
    if getattr(ctx, "terminal", None) is not None:
        ctx.terminal.view.note(_("Looking at what undoing it means now…"), "info")
    run_async(operations.undo_plan, op, on_done=lambda plan, err: _ask(ctx, op, plan, err, parent))


def _ask(ctx, op, plan, err, parent):
    from .. import question
    from ..ui import info_box
    if err is not None or plan is None:
        info_box(parent, _("Undo"), _("It could not be worked out: {error}").format(error=err))
        return
    reinstall = [n for n in plan["reinstall"] if operations.safe_name(n)]
    remove = [n for n in plan["remove"] if operations.safe_name(n)]
    downgrade = [d for d in plan["downgrade"] if operations.safe_name(d[0])]
    if not (reinstall or remove or downgrade):
        why = names_text([c[0] for c in plan["cannot"]]) if plan["cannot"] else ""
        info_box(parent, _("Undo"), (_("There is nothing to undo: it is already as before.") if not why else
                                     _("It cannot be undone: no repository has the earlier versions any more "
                                       "({names}).").format(names=why)))
        return
    items = []
    if reinstall:
        items.append(("system-software-install|list-add", ngettext(
            "Install again {n} package it removed", "Install again {n} packages it removed", len(reinstall)).format(
            n=len(reinstall)), names_text(reinstall, 6) + " · " + _("the version the repositories have now"),
            reinstall))
    if remove:
        items.append(("edit-delete", ngettext("Remove {n} package it installed", "Remove {n} packages it installed",
                                              len(remove)).format(n=len(remove)), names_text(remove, 6), remove))
    if downgrade:
        names = [d[0] for d in downgrade]
        items.append(("go-down", ngettext("Put back the earlier version of {n} package",
                                          "Put back the earlier versions of {n} packages", len(downgrade)).format(
            n=len(downgrade)), names_text(names, 6), [f"{n} {v}" for n, v in downgrade]))
    warning = None
    if plan["cannot"]:
        warning = ngettext("{n} change cannot be undone: no repository has its earlier version any more ({names}).",
                           "{n} changes cannot be undone: no repository has their earlier versions any more "
                           "({names}).", len(plan["cannot"])).format(n=len(plan["cannot"]),
                                                                     names=names_text([c[0] for c in plan["cannot"]]))
    title = _("Undo the change of {when}?").format(when=when_text(op["time"]))
    if not question.confirm(
            parent, title, _("Put the undo in the queue"), window_title=_("Undo"), icon_name="edit-undo",
            items=items, warning=warning, password=False, help_topic="queue", queue=True,
            means=_("it goes to the queue, and the whole queue is simulated at once: you see everything that would "
                    "change, and nothing changes until you apply it.")):
        return
    with ctx.queue.batch():
        for n in reinstall:
            ctx.queue.add(Action("install", n))
        for n in remove:
            ctx.queue.add(Action("remove", n))
        for n, v in downgrade:
            ctx.queue.add(Action("install", f"{n}={v}", oldpackage=True, package=n))
    ctx.simulate_queue()
