# SPDX-License-Identifier: GPL-3.0-or-later
"""Open downloaded .rpm files (Dolphin: “Open with Zypp Desk”): what each one is, and install it through the queue.

Nothing is installed here. What goes to the queue is a private copy of the file, in the 0700 download folder
(localrpm.copy_for_queue), with its SHA-256 (Apply checks it again), like the programs from the web: nobody else
can change it between the check and the moment root installs it. The file itself stays
where it is. Before that the window says what the file is, who made it, the version you have now,
whether its signature can be checked and what it needs. Files that must not be installed are refused, with
the reason: not an RPM package, a source package, another processor, damaged, or a signature that does not
verify. A file signed with a key that rpm does not know waits until the key is imported; a file without a
signature goes to the queue only after a clear question.

When the file was built by the openSUSE Build Service, the window offers to add the project's repository (then
the routine keeps the program up to date) and, when the key is missing, to import the project's key.

A file made for Fedora may ask for things openSUSE has under other names (fontpackages-filesystem is filesystem
here): bridge.analyse finds them, and adding the file to the queue offers to make a bridge package for it
(pages/bridges.py), which goes to the queue with it.
"""

import os
from collections import Counter

from PySide6.QtCore import QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPalette, QTextOption
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea, QSplitter, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from .. import backend, bridge, localrpm, question, websources
from ..i18n import _, ngettext
from ..runner import Step
from ..style import BLUE, GREEN, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action, TxQueue
from ..ui import button, error_box, heading, hline, icon, info_box, mono, muted
from . import bridges as bridgeui
from .repotools import personal_project_question
from .websources import fmt_size, home_short, local_state, short_name

GREY = "#7f8c8d"
RPM_ICON = bridgeui.RPM_ICON
ITEM_ROLE = Qt.ItemDataRole.UserRole
MAX_PROVIDERS = 6
MAX_MISSING = 5


def _label(text, wrap=True, bold=False, color=None, selectable=False):
    """A label for text that comes from a file or the network: always plain text, never rich text."""
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(wrap)
    if bold:
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    if selectable:
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


class _Link(QLabel):
    """An address shown as plain text in the link colour; a click opens it (http and https only)."""

    def __init__(self, url, parent=None):
        shown = url.split("://", 1)[-1].rstrip("/")
        super().__init__(shown, parent)
        self.url = url
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setStyleSheet(f"color: {BLUE};")
        self.setToolTip(url)
        if url.startswith(("https://", "http://")):
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.url.startswith(("https://", "http://")):
            QDesktopServices.openUrl(QUrl(self.url))
        super().mouseReleaseEvent(ev)


class _Note(QFrame):
    """A coloured note: an icon, a title in bold, a text, and buttons on the right (all plain text)."""

    def __init__(self, color, icon_name, title, text, buttons=(), parent=None):
        super().__init__(parent)
        c = QColor(color)
        self.setObjectName("rpmnote")
        self.setStyleSheet(f"QFrame#rpmnote {{ background: rgba({c.red()}, {c.green()}, {c.blue()}, 0.10); "
                           f"border: 1px solid rgba({c.red()}, {c.green()}, {c.blue()}, 0.40); border-radius: 8px; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 9, 12, 9)
        h.setSpacing(10)
        if icon_name:
            ic = QLabel(self)
            ic.setPixmap(icon(*icon_name.split("|")).pixmap(22, 22))
            h.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        if title:
            v.addWidget(_label(title, bold=True))
        if text:
            v.addWidget(_label(text))
        h.addLayout(v, 1)
        if len(buttons) == 1:
            h.addWidget(buttons[0], 0, Qt.AlignmentFlag.AlignVCenter)
        elif buttons:  # several: in a row under the text, so the text keeps its width
            bh = QHBoxLayout()
            bh.setContentsMargins(0, 4, 0, 0)
            for b in buttons:
                bh.addWidget(b)
            bh.addStretch(1)
            v.addLayout(bh)


class _CommandView(QPlainTextEdit):
    """The exact command, in monospace, wrapped anywhere (paths have no spaces to break at) and exactly as tall
    as its lines."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setFont(mono())
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setStyleSheet("QPlainTextEdit { background: transparent; color: palette(placeholder-text); }")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self.setFixedHeight(self.fontMetrics().lineSpacing() + 8)
        # a new text (the other windows change it as you type): fit it once it is laid out
        self.textChanged.connect(lambda: QTimer.singleShot(0, self, self._fit))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _fit(self):
        lines = max(1, int(self.document().documentLayout().documentSize().height()))  # lines, in this layout
        h = self.fontMetrics().lineSpacing() * lines + 8
        if h != self.height():
            self.setFixedHeight(h)


REFUSE_SHORT = {
    "missing": _("not found"),
    "not-file": _("not a file"),
    "unreadable": _("cannot be read"),
    "not-rpm": _("not an RPM package"),
    "source": _("source package"),
    "arch": _("for another processor"),
    "damaged": _("damaged or changed"),
    "changed": _("changed while it was read"),
}


def verdict(it):
    """(colour, short state, can it go to the queue) of a file."""
    info = it.get("info")
    if it.get("error"):
        return RED, _("cannot be read"), False
    if info is None:
        return GREY, _("reading…"), False
    if info["refuse"]:
        return RED, REFUSE_SHORT.get(info.get("refuse_kind"), _("cannot be installed")), False
    if info["compare"] == "same":
        return GREY, _("you already have it"), False
    sig = info["signature"]
    if sig.startswith("nokey:"):
        return ORANGE, (_("key in the queue") if it.get("key_queued") else _("key missing")), False
    if it.get("plan_busy"):
        return GREY, _("looking at what it needs…"), False
    plan = it.get("plan") or {}
    if plan.get("unmapped"):
        return ORANGE, _("openSUSE lacks something it needs"), True
    if plan.get("needs_bridge"):
        return ORANGE, _("needs a bridge package"), True
    if sig == "unsigned":
        return ORANGE, (_("older, not signed") if info["compare"] == "older" else _("not signed")), True
    if info["compare"] == "older":
        return ORANGE, _("older than yours"), True
    if info["compare"] == "newer":
        return GREEN, _("updates yours"), True
    return GREEN, _("new"), True


class _FileDelegate(QStyledItemDelegate):
    """A file in the list: a coloured dot, the name and version in bold, its state below."""

    def sizeHint(self, option, index):
        return QSize(200, 50)

    def paint(self, p, option, index):
        row = index.data(ITEM_ROLE)
        if not row:
            return super().paint(p, option, index)
        title, color, state = row["title"], row["color"], row["state"]
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 60))
            p.drawRoundedRect(r, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 22))
            p.drawRoundedRect(r, 10, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QRect(r.left() + 10, r.center().y() - 5, 10, 10))
        f = QFont(option.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(option.palette.text().color())
        text_r = QRect(r.left() + 30, r.top() + 4, r.width() - 36, r.height() // 2 - 2)
        p.drawText(text_r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, text_r.width()))
        p.setFont(option.font)
        p.setPen(QColor(color) if color != GREY else option.palette.placeholderText().color())
        state_r = QRect(r.left() + 30, r.center().y(), r.width() - 36, r.height() // 2 - 2)
        p.drawText(state_r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(state, Qt.TextElideMode.ElideRight, state_r.width()))
        p.restore()


class LocalRpmDialog(QDialog):
    """What one or more downloaded .rpm files are, and the button that puts them in the queue."""

    def __init__(self, ctx, paths, parent=None, note=None):
        super().__init__(parent or ctx)
        self.ctx = ctx
        self._closed = False
        self.repos = None  # the system's repositories, once read (for the web folder and the OBS offers)
        self.folder = ""
        self.items = []
        seen = set()
        for p in paths or []:
            p = self._local_path(p)
            if p and p not in seen:
                seen.add(p)
                self.items.append({"path": p, "info": None, "error": None, "obs": None, "obs_check": None,
                                   "key_queued": False, "busy": False, "plan": None, "plan_busy": False})
        self.multi = len(self.items) > 1
        # freed when it closes (the window opens it with exec() and keeps no reference); answers that arrive later
        # see _closed and do nothing
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(ngettext("Install a downloaded package", "Install downloaded packages", len(self.items)))
        self.setWindowIcon(icon("application-x-rpm", "package-x-generic"))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(muted(note if note is not None else _("Opened from the file manager: “Open with Zypp Desk”.")))

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if self.multi:
            self.list = QListWidget(self)
            self.list.setItemDelegate(_FileDelegate(self.list))
            self.list.setMouseTracking(True)
            self.list.setFrameShape(QFrame.Shape.NoFrame)
            self.rows = []
            for it in self.items:
                li = QListWidgetItem()
                li.setToolTip(it["path"])
                self.list.addItem(li)
                self.rows.append(li)
                self._paint_row(it)
            self.list.currentRowChanged.connect(lambda _r: self._show_current())
            left = QFrame(self)
            left.setObjectName("card")
            lv = QVBoxLayout(left)
            lv.setContentsMargins(4, 6, 4, 6)
            lv.addWidget(self.list)
            right = QFrame(self)
            right.setObjectName("card")
            rv = QVBoxLayout(right)
            rv.setContentsMargins(6, 8, 6, 8)
            rv.addWidget(self.scroll)
            split = QSplitter(Qt.Orientation.Horizontal, self)
            split.addWidget(left)
            split.addWidget(right)
            split.setStretchFactor(0, 2)
            split.setStretchFactor(1, 5)
            split.setSizes([300, 720])
            split.setChildrenCollapsible(False)
            lay.addWidget(split, 1)
        else:
            self.list = None
            lay.addWidget(self.scroll, 1)

        # keep a copy in the folder of programs from the web
        self.copy = QCheckBox(_("Keep a copy in the folder of programs from the web"), self)
        self.copy.setChecked(bool(self.ctx.settings.get("local_rpm_keep_copy", True)))
        self.copy_where = muted("", self)
        self.copy_where.setTextFormat(Qt.TextFormat.PlainText)
        crow = QHBoxLayout()
        crow.addWidget(self.copy)
        crow.addWidget(self.copy_where, 1)
        self.copy_row = QWidget(self)
        self.copy_row.setLayout(crow)
        crow.setContentsMargins(0, 4, 0, 0)
        lay.addWidget(self.copy_row)

        lay.addWidget(hline())
        bottom = QHBoxLayout()
        self.summary = muted("", self)
        bottom.addWidget(self.summary, 1)
        self.b_cancel = button(_("Cancel"), "dialog-cancel", self.reject)
        # both buttons only put the files in the queue (nothing is installed here): never the green one
        self.b_sim = button(_("Put it in the queue and simulate"), "media-playback-start",
                            lambda: self.add_to_queue(simulate=True))
        self.b_add = button(_("Put it in the queue"), "list-add", lambda: self.add_to_queue())
        self.b_add.setDefault(True)  # Enter only puts it in the queue (and a file that needs it asks first)
        for b in (self.b_cancel, self.b_sim, self.b_add):
            bottom.addWidget(b)
        lay.addLayout(bottom)

        if self.multi:
            self.scroll.viewport().setBackgroundRole(QPalette.ColorRole.Base)  # the card's colour
            self.resize(1080, 700)
        else:
            self.resize(760, 440)  # it grows to fit the details once the file is read (_fit)
        self._set_folder(self.ctx.settings.get("web_folder") or self._loaded_page_folder())
        if self.list is not None and self.items:
            self.list.setCurrentRow(0)
        self._show_current()
        self._update_buttons()
        need_folder = not self.folder and not self._loaded_page_folder()
        run_async(self._system_state, need_folder, on_done=self._state_loaded)
        self._read_next()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _local_path(p):
        """A path from the command line: plain, or a file:// address (what file managers sometimes pass)."""
        p = str(p or "").strip()
        if p.startswith("file://"):
            p = QUrl(p).toLocalFile()
        return os.path.abspath(os.path.expanduser(p)) if p else ""

    def done(self, result):
        self._closed = True
        self._drop_copies()
        super().done(result)

    def _drop_copies(self):
        """The private copies of files that did not go to the queue are deleted when the window closes."""
        for it in self.items:
            self._drop_unqueued((it.get("info") or {}).get("private"))

    def _drop_unqueued(self, path):
        """Delete a private copy unless the queue uses it (the same file may be queued from another window)."""
        if path and not any(a.target == path for a in self.ctx.queue.actions):
            localrpm.drop_private_copy(path)

    def closeEvent(self, ev):
        self._closed = True
        super().closeEvent(ev)

    def current(self):
        if not self.items:
            return None
        if self.list is None:
            return self.items[0]
        row = self.list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _paint_row(self, it):
        """What the list shows of a file (the delegate draws it): name and version, colour and state."""
        info = it.get("info") or {}
        color, state, _ok = verdict(it)
        title = " ".join(x for x in (info.get("name") or os.path.basename(it["path"]), info.get("version") or "") if x)
        self.rows[self.items.index(it)].setData(ITEM_ROLE, {"title": title, "color": color, "state": state})

    def _redraw(self, it):
        if it is self.current():
            self._show_current()
        if self.list is not None:
            self._paint_row(it)
        self._update_buttons()

    # ------------------------------------------------------------ reading
    def _read_next(self):
        """One file after the other: the first one shows quickly, and a dozen files do not start a dozen rpm."""
        it = next((x for x in self.items if x["info"] is None and not x["error"] and not x["busy"]), None)
        if it is None or self._closed:
            return
        it["busy"] = True
        run_async(localrpm.read_rpm, it["path"], on_done=lambda res, err, it=it: self._read(it, res, err))

    def _read(self, it, res, err):
        if self._closed:
            if res and res.get("private"):  # read after the window closed: its copy is not needed
                self._drop_unqueued(res["private"])
            return
        it["busy"] = False
        if err:
            it["error"] = str(err)
        else:
            it["info"] = res
            it["key_queued"] = self._key_in_queue(res)
            obs = localrpm.obs_repo(res) if not res["refuse"] else None
            it["obs"] = obs
            if obs:
                keyid = res["keyid"] if res["signature"].startswith("nokey:") else ""
                run_async(localrpm.check_obs, obs, keyid, on_done=lambda r, e, it=it: self._obs_checked(it, r, e))
            if not res["refuse"] and res["compare"] != "same" and res["missing"]:
                # something it asks for is not on this computer: the repositories, or another name (a bridge)
                it["plan_busy"] = True
                run_async(bridge.analyse, res, os.path.expanduser(self.folder or ""),
                          on_done=lambda r, e, it=it: self._planned(it, r, e))
        self._redraw(it)
        self._read_next()

    def _planned(self, it, res, err):
        if self._closed:
            return
        it["plan_busy"] = False
        it["plan"] = res if not err and res else {"error": str(err or "?")}
        self._redraw(it)

    def _obs_checked(self, it, res, err):
        if self._closed:
            return
        it["obs_check"] = res if not err else {"exists": None, "error": str(err)}
        self._redraw(it)

    @staticmethod
    def _system_state(need_folder):
        """The repositories, and (when nothing says it yet) the local folder repository most of your programs
        from the web were installed from: the folder the Programs from the web page uses."""
        repos = backend.repos()
        folder = ""
        if need_folder:
            try:
                counts = Counter(local_state()["packages"].values())
            except backend.ZypperError:
                counts = Counter()
            if counts:
                folder = home_short(counts.most_common(1)[0][0])
        return repos, folder

    def _state_loaded(self, res, err):
        if self._closed:
            return
        repos, folder = res if not err and res else ([], "")
        self.repos = repos or []
        if not self.folder:
            self._set_folder(self._loaded_page_folder() or folder or self._page_folder() or "~/RPM")
        for it in self.items:
            if it.get("obs"):
                self._redraw(it)

    # ------------------------------------------------------------ the folder of programs from the web
    def _loaded_page_folder(self):
        """The folder of the Programs from the web page, when that page has already looked at the system."""
        page = getattr(self.ctx, "pages", {}).get("websources")
        if page is not None and getattr(page, "local", None):
            return self._page_folder()
        return ""

    def _page_folder(self):
        page = getattr(self.ctx, "pages", {}).get("websources")
        try:
            return page.default_folder() if page is not None else ""
        except Exception:  # the page is someone else's: never let it stop this window
            return ""

    def _set_folder(self, folder):
        self.folder = folder or ""
        self._update_copy()

    def _in_folder(self, path):
        if not self.folder:
            return False
        try:
            return os.path.realpath(os.path.dirname(path)) == os.path.realpath(os.path.expanduser(self.folder))
        except OSError:
            return False

    def _update_copy(self):
        targets = [it for it in self.items if verdict(it)[2]]
        if not self.folder or not targets:  # nothing to install: nothing to keep either
            self.copy_row.hide()
            return
        self.copy_row.show()
        inside = [it for it in targets if self._in_folder(it["path"])]
        if targets and len(inside) == len(targets):
            self.copy.setEnabled(False)
            self.copy.hide()
            self.copy_where.setText(ngettext("The file is already in the folder of programs from the web ({folder}).",
                                             "The files are already in the folder of programs from the web "
                                             "({folder}).", len(targets)).format(folder=self.folder))
        else:
            self.copy.setEnabled(True)
            self.copy.show()
            self.copy_where.setText(f"({self.folder})")

    def _copy_to(self, it):
        if not (self.folder and self.copy.isEnabled() and self.copy.isChecked()) or self._in_folder(it["path"]):
            return None
        return os.path.expanduser(self.folder)

    # ------------------------------------------------------------ queue
    def _action(self, it, target=None):
        """The install for the queue. target: the private copy root installs (copy_for_queue); without it (the
        command shown before anything is copied) the path that copy will have."""
        info = it["info"]
        return Action("install", target or localrpm.planned_copy(info), package=info["name"], sha256=info["sha256"],
                      allow_unsigned=info["signature"] == "unsigned", oldpackage=info["compare"] == "older",
                      copy_to=self._copy_to(it), keyid=info.get("keyid") or "")  # keyid: the review links the key

    def _command(self, it):
        """The exact zypper command for this file alone (the queue joins it with the other installs); with a
        bridge, the bridge Zypp Desk writes first and the command that installs both."""
        plan = it.get("plan") or {}
        if plan.get("needs_bridge"):
            return "\n".join(f"{i}. {c}" for i, c in enumerate(
                bridgeui.commands_for(plan, self._action(it), self.ctx.settings), 1))
        q = TxQueue(self.ctx.settings)
        q.actions = [self._action(it)]
        home = os.path.expanduser("~") + "/"
        return "\n".join(Step("zypper", ["-n", *args]).display().replace(" " + home, " ~/")
                         for args in q.package_args())

    def _bridge_folder(self):
        """Where Apply moves a bridge: the folder of programs from the web (a local zypper repository)."""
        return self.folder or self._page_folder() or "~/RPM"

    def _queueable(self):
        if self.multi:
            return [it for it in self.items if verdict(it)[2]]
        it = self.current()
        return [it] if it and verdict(it)[2] else []

    def _update_buttons(self):
        ready = self._queueable()
        reading = any((it["info"] is None and not it["error"]) or it.get("plan_busy") for it in self.items)
        ok = bool(ready) and not (self.multi and reading)
        self.b_add.setEnabled(ok)
        self.b_sim.setEnabled(ok)
        self.b_cancel.setText(_("Cancel") if ready or reading else _("Close"))
        self.b_cancel.setToolTip(_("Close this window: nothing goes to the queue."))
        self._label_buttons(ready)
        if self.multi:
            if reading:
                self.summary.setText(_("Reading the files…"))
            else:
                self.summary.setText(ngettext("{n} of {total} file can go to the queue.",
                                              "{n} of {total} files can go to the queue.", len(self.items))
                                     .format(n=len(ready), total=len(self.items)))
        self._update_copy()

    def _asks_first(self, targets):
        """Whether putting these files in the queue asks a question first (as add_to_queue does): a bridge, a file
        without a signature, or (several files) an older version."""
        for it in targets:
            info = it.get("info") or {}
            if (it.get("plan") or {}).get("needs_bridge") or info.get("signature") == "unsigned":
                return True
            if self.multi and info.get("compare") == "older":
                return True
        return False

    def _label_buttons(self, ready):
        """The two buttons say which file (by name) or how many go to the queue, and "…" when a question comes
        first."""
        dots = "…" if self._asks_first(ready) else ""
        n = len(ready)
        if self.multi:
            if n and n == len(self.items):
                add = ngettext("Put the file in the queue", "Put all {n} in the queue", n).format(n=n)
            elif n:
                add = ngettext("Put the {n} that can go in the queue", "Put the {n} that can go in the queue",
                               n).format(n=n)
            else:
                add = _("Put them in the queue")
            sim = ngettext("Put the file in the queue and simulate", "Put them in the queue and simulate", n)
            what = ngettext("{n} file", "{n} files", n).format(n=n)
        else:
            info = (ready[0].get("info") if ready else None) or (self.current() or {}).get("info") or {}
            short = short_name(info.get("name") or "")
            add = _("Put {name} in the queue").format(name=short) if short else _("Put it in the queue")
            sim = _("Put it in the queue and simulate")
            what = info.get("name") or _("the file")
        self.b_add.setText(add + dots)
        self.b_sim.setText(sim + dots)
        self.b_add.setToolTip(_("Puts {what} in the queue: a private copy of the file, with its checksum. Nothing is "
                                "installed until you apply the queue.").format(what=what))
        self.b_sim.setToolTip(_("Puts {what} in the queue and simulates the whole queue at once (zypper --dry-run): "
                                "it shows what zypper would do, and nothing is installed.").format(what=what))

    def add_to_queue(self, simulate=False):
        targets = self._queueable()
        if not targets:
            return
        # one question per decision: a file that needs a bridge says in the bridge's own
        # question that it is not signed or older; the other files share one question
        bridged = [it for it in targets if (it.get("plan") or {}).get("needs_bridge")]
        rest = [it for it in targets if not (it.get("plan") or {}).get("needs_bridge")]
        unsigned = [it for it in rest if it["info"]["signature"] == "unsigned"]
        older = [it for it in rest if it["info"]["compare"] == "older"]
        if unsigned or (self.multi and older):
            if not self._confirm(unsigned, older):
                return
        for it in bridged:
            if not bridgeui.ask_bridge(self, it["plan"], it["info"], self._action(it), self.ctx.settings,
                                       self._bridge_folder(), warnings=self._file_warnings(it)):
                return
        # what root will install: a private copy of each file, exactly as it was read
        copies = {}
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for it in targets:
                copies[id(it)] = localrpm.copy_for_queue(it["info"])
        except localrpm.FileChanged as e:
            QApplication.restoreOverrideCursor()
            error_box(self, self.windowTitle(), _(
                "{file} changed after Zypp Desk read it, so nothing went to the queue. Close this window and open "
                "the file again.").format(file=home_short(str(e))))
            return
        except OSError as e:
            QApplication.restoreOverrideCursor()
            error_box(self, self.windowTitle(), _("The file cannot be read: {error}").format(
                error=e.strerror or e))
            return
        QApplication.restoreOverrideCursor()
        built = {}
        folder = os.path.expanduser(self._bridge_folder())
        for it in bridged:  # written first: if one cannot be written, nothing goes to the queue
            try:
                built[id(it)] = bridge.build_bridge(it["plan"], str(websources.download_dir()), web_folder=folder)
            except (OSError, ValueError) as e:
                error_box(self, _("Bridge package"), _("The bridge package could not be written: {error}").format(
                    error=e))
                return
        with self.ctx.queue.batch():
            for it in targets:
                action = self._action(it, copies[id(it)])
                if id(it) in built:
                    for a in bridgeui.bridge_actions(it["plan"], action, folder, built[id(it)]):
                        self.ctx.queue.add(a)
                else:
                    self.ctx.queue.add(action)
        if not self.copy_row.isHidden() and self.copy.isEnabled():
            self.ctx.settings["local_rpm_keep_copy"] = self.copy.isChecked()
            self.ctx.settings.save()
        self.accept()
        if simulate:
            QTimer.singleShot(0, self.ctx.simulate_queue)

    @staticmethod
    def _file_warnings(it):
        """What to know about a file before it goes to the queue: not signed, older than yours."""
        i = it["info"]
        out = []
        if i["signature"] == "unsigned":
            out.append(_("It is not signed: nothing confirms who made this file, or that nobody changed it after it "
                         "was published. Install it only if you trust the place you downloaded it from."))
        if i["compare"] == "older":
            out.append(_("It is older than the version you have ({version}): installing it goes back to an older "
                         "version.").format(version=i["installed_evr"]))
        return out

    def _confirm(self, unsigned, older):
        """The question before files that are not signed (or older than yours) go to the queue."""
        no_sig, old = {id(x) for x in unsigned}, {id(x) for x in older}
        files = [x for x in self.items if id(x) in no_sig | old]
        items = []
        for it in files:
            i = it["info"]
            why = []
            if id(it) in no_sig:
                why.append(_("not signed"))
            if id(it) in old:
                why.append(_("older than yours ({version})").format(version=i["installed_evr"]))
            items.append((RPM_ICON, "{name} {version}".format(name=i["name"], version=i["evr"]),
                          "{file} · {why}".format(file=i["file_name"], why=", ".join(why))))
        warnings = []
        if unsigned:
            warnings.append(ngettext(
                "Nothing confirms who made this file, or that nobody changed it after it was published. Install it "
                "only if you trust the place you downloaded it from.",
                "Nothing confirms who made a file without a signature, or that nobody changed it after it was "
                "published. Install them only if you trust the place you downloaded them from.", len(unsigned)))
        if older:
            warnings.append(ngettext("An older version replaces the newer one you have.",
                                     "Older versions replace the newer ones you have.", len(older)))
        if len(files) == 1:
            i = files[0]["info"]
            title = (_("{name} {version} is not signed").format(name=i["name"], version=i["evr"]) if unsigned else
                     _("{name} {version} is older than yours").format(name=i["name"], version=i["evr"]))
        else:
            title = ngettext("{n} file needs your OK", "{n} files need your OK", len(files)).format(n=len(files))
        yes = _("Add it to the queue anyway") if len(files) == 1 else _("Add them to the queue anyway")
        return question.confirm(
            self, title, yes, buttons=bridgeui.queue_buttons(yes), window_title=self.windowTitle(),
            icon_name="security-low|dialog-warning", items=items,
            means=_("it only goes to the queue: nothing is installed until you apply it."),
            warning="\n\n".join(warnings), password=False, help_topic="web")

    # ------------------------------------------------------------ OBS repository and key
    def _repo_with_url(self, url):
        want = url.rstrip("/")
        have = next((r for r in (self.repos or []) if (r.get("url") or "").rstrip("/") == want), None)
        if have:
            return have
        queued = next((a for a in self.ctx.queue.of("repo-add") if (a.opts.get("url") or "").rstrip("/") == want),
                      None)
        return {"alias": queued.target, "name": queued.opts.get("name") or queued.target, "queued": True} \
            if queued else None

    def _free_alias(self, alias):
        used = {r["alias"] for r in (self.repos or [])} | {a.target for a in self.ctx.queue.of("repo-add")}
        base, n = alias, 2
        while alias in used:
            alias, n = f"{base}-{n}", n + 1
        return alias

    def add_repo(self, it):
        obs = it["obs"]
        if not obs or self._repo_with_url(obs["url"]):
            return
        if obs["project"].startswith("home:") and not personal_project_question(self, obs["project"], obs["url"]):
            return
        self.ctx.queue.add(Action("repo-add", self._free_alias(obs["alias"]), url=obs["url"], name=obs["name"],
                                  autorefresh=True, priority=99, gpg="default"))
        self._redraw(it)

    def import_key(self, it):
        obs, chk = it["obs"], it.get("obs_check") or {}
        try:
            from .keys import open_import_dialog
        except ImportError:
            info_box(self, _("Import a signing key"), _(
                "This version of Zypp Desk cannot import a key from here yet. Import it in Signing keys → Import a "
                "key…, with this address:") + "\n\n" + obs["key_url"])
            return
        # the fingerprint read from the project's repository, already compared with the key that signed the file:
        # the import window compares it again with the key it downloads
        open_import_dialog(self.ctx, self, url=obs["key_url"], fingerprint=chk.get("fingerprint") or "",
                           hint=obs["project"])
        self.check_again(it)  # imported at once, only queued, or not at all: rpm says which

    def check_again(self, it):
        if it["busy"] or not it.get("info"):
            return
        it["busy"] = True
        self._redraw(it)
        # the private copy when there is one: the check is about what will be installed
        where = it["info"].get("private") or it["path"]
        run_async(localrpm.signature_now, where, on_done=lambda res, err, it=it: self._rechecked(it, res, err))

    def _rechecked(self, it, res, err):
        if self._closed:
            return
        it["busy"] = False
        if not err:
            sig, who = res
            info = it["info"]
            if sig == "bad":
                info["refuse"], info["refuse_kind"] = localrpm.DAMAGED, "damaged"
            info["signature"] = sig
            info["signer_name"] = who
            it["key_queued"] = self._key_in_queue(info)
        self._redraw(it)

    def _key_in_queue(self, info):
        """Whether the key that signed the file waits in the queue (imported only when the queue is applied)."""
        keyid = (info.get("keyid") or "").upper()
        return bool(keyid) and info["signature"].startswith("nokey:") and any(
            a.target.upper().endswith(keyid) for a in self.ctx.queue.of("key-import"))

    # ------------------------------------------------------------ the details of a file
    def _show_current(self):
        it = self.current()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4 if not self.multi else 14, 6, 8, 6)
        v.setSpacing(10)
        if it is None:
            v.addWidget(muted(_("No file was given.")))
        elif it["error"]:
            v.addWidget(_label(os.path.basename(it["path"]), bold=True))
            v.addWidget(_Note(RED, "dialog-error", _("The file could not be read"), it["error"]))
        elif it["info"] is None:
            v.addWidget(_label(os.path.basename(it["path"]), bold=True))
            v.addWidget(muted(_("Reading the file: its name, who made it, its signature and what it needs…")))
        else:
            self._fill(v, it)
        v.addStretch(1)
        self.scroll.setWidget(w)
        QTimer.singleShot(0, self._fit)

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._fit)

    def _fit(self):
        """One file: make the window as tall as its details (never shorter, never taller than the screen)."""
        if self.multi or self._closed or not self.isVisible():
            return
        w = self.scroll.widget()
        if w is None:
            return
        lay = w.layout()
        vw = self.scroll.viewport().width()
        need = lay.heightForWidth(vw) if lay.hasHeightForWidth() else w.sizeHint().height()
        need += self.height() - self.scroll.viewport().height() + 2
        screen = self.screen()
        top = int(screen.availableGeometry().height() * 0.9) if screen else 900
        h = min(max(self.height(), need), top)
        if h != self.height():
            self.resize(self.width(), h)

    def _fill(self, v, it):
        info = it["info"]
        # name, version and summary
        top = QHBoxLayout()
        top.setSpacing(14)
        pic = QLabel()
        pic.setPixmap((icon("application-x-rpm", "package-x-generic") if info["name"] else
                       icon("unknown", "application-octet-stream", "dialog-error")).pixmap(48, 48))
        top.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(2)
        title = heading(" ".join(x for x in (info["name"], info["version"]) if x) or info["file_name"], 1.35)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        names.addWidget(title)
        if info["summary"]:
            s = _label(info["summary"])
            s.setStyleSheet("color: palette(placeholder-text);")
            if info["description"]:
                s.setToolTip(info["description"])
            names.addWidget(s)
        top.addLayout(names, 1)
        v.addLayout(top)

        # the facts
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        def add(label, *widgets):
            r = grid.rowCount()
            k = muted(label)
            k.setWordWrap(False)
            grid.addWidget(k, r, 0, Qt.AlignmentFlag.AlignTop)
            if len(widgets) == 1:
                grid.addWidget(widgets[0], r, 1)
            else:
                box = QWidget()
                h = QVBoxLayout(box)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(2)
                for x in widgets:
                    h.addWidget(x)
                grid.addWidget(box, r, 1)

        add(_("File"), _label(home_short(info["path"]), selectable=True))
        if info["name"]:
            add(_("You have now"), self._installed_label(info) if not info["refuse"] else _label(
                _("version {version}").format(version=info["installed_evr"]) if info["installed_evr"]
                else _("not installed")))
            obs = it["obs"]
            if obs:
                by = QWidget()
                h = QHBoxLayout(by)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(4)
                h.addWidget(_label(_("openSUSE Build Service, project"), wrap=False))
                h.addWidget(_label(obs["project"], wrap=False, bold=True, selectable=True))
                h.addStretch(1)
                add(_("Made by"), by)
            else:
                add(_("Made by"), _label(info["vendor"] or info["packager"] or _("the file does not say"),
                                         selectable=True))
            if info["built_for"]:
                add(_("Built for"), _label(info["built_for"].replace("_", " ")))
            if info["url"]:
                add(_("Web"), _Link(info["url"]))
            add(_("Size"), _label(_("{size} once installed (the file: {file})").format(
                size=fmt_size(info["size"]), file=fmt_size(info["file_size"]))))
            if info["signature"] == "verified" and not info["refuse"]:
                who = info["signer_name"].split(" <")[0].strip() or _("a key rpm trusts")
                lbl = _label("✓ " + _("signed by {who} (key {key}); rpm checked it").format(
                    who=who, key=info["keyid"][-8:]), color=GREEN)
                lbl.setToolTip(info["signer_name"])
                add(_("Signature"), lbl)
            if not info["refuse"]:
                add(_("Needs"), *self._needs_labels(it))
        v.addLayout(grid)

        # what stops it, or what to know before installing it
        for note in self._notes(it):
            v.addWidget(note)

        if verdict(it)[2]:
            v.addWidget(muted(_("The commands it will use, in this order:") if (it.get("plan") or {}).get(
                "needs_bridge") else _("Command that will run:")))
            v.addWidget(_CommandView(self._command(it)))
            if not info.get("exposed"):  # the note above says it for a file others can change
                v.addWidget(muted(_("zypper installs a copy of the file, made when it goes to the queue and kept where "
                                    "only you can change it. The file itself stays where it is.")))

    def _installed_label(self, info):
        c, have = info["compare"], info["installed_evr"]
        if c == "not-installed":
            return _label(_("not installed"))
        if c == "same":
            return _label(_("this same version ({version})").format(version=have))
        if c == "newer":
            return _label(_("version {version}; this file is newer, so installing it updates the program").format(
                version=have))
        return _label(_("version {version}, newer than this file").format(version=have), color=ORANGE)

    def _needs_labels(self, it):
        info = it["info"]
        out = []
        have = sorted(info["provided"], key=str.lower)
        if not have and not info["missing"]:
            return [_label(_("nothing that you do not have already"))]
        if have:
            shown = ", ".join(have[:MAX_PROVIDERS])
            if len(have) > MAX_PROVIDERS:
                shown += " " + ngettext("and {n} more", "and {n} more", len(have) - MAX_PROVIDERS).format(
                    n=len(have) - MAX_PROVIDERS)
            ok = _label("✓ " + ngettext("you have it", "you have them", len(have)), wrap=False, color=GREEN)
            names = _label(shown, wrap=len(shown) > 60)
            names.setToolTip("\n".join(have))
            if len(shown) > 60:  # a long list wraps, and the tick goes under it
                out += [names, ok]
            else:
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(6)
                h.addWidget(names)
                h.addWidget(ok)
                h.addStretch(1)
                out.append(row)
        plan = it.get("plan")
        if it.get("plan_busy"):
            out.append(muted(_("Looking in your repositories for what you do not have…")))
            return out
        if plan and not plan.get("error"):
            return out + self._plan_labels(plan)
        miss = info["missing"]
        if miss:
            shown = ", ".join(miss[:MAX_MISSING]) + (" …" if len(miss) > MAX_MISSING else "")
            lbl = _label(ngettext("{n} thing you do not have: zypper will look for it in your repositories ({list}). "
                                  "Simulate to see whether it is found.",
                                  "{n} things you do not have: zypper will look for them in your repositories "
                                  "({list}). Simulate to see whether they are found.", len(miss))
                         .format(n=len(miss), list=shown), color=ORANGE)
            lbl.setToolTip("\n".join(miss))
            out.append(lbl)
        return out

    @staticmethod
    def _short_list(items, most=MAX_MISSING):
        return ", ".join(items[:most]) + (" …" if len(items) > most else "")

    def _plan_labels(self, plan):
        """The lines of "Needs" once bridge.analyse has looked: what the repositories have, what a bridge says
        (or will say), and what openSUSE does not have."""
        out = []
        if plan["available"]:
            lbl = _label(ngettext("{list}: your repositories have it; zypper installs it with the program.",
                                  "{list}: your repositories have them; zypper installs them with the program.",
                                  len(plan["available"])).format(list=self._short_list(plan["available"])),
                         color=GREEN)
            lbl.setToolTip("\n".join(plan["available"]))
            out.append(lbl)
        if plan["kept"]:
            names = [m["text"] for m in plan["kept"]]
            out.append(_label(_("{list}: your bridge {bridge} says openSUSE has it.").format(
                list=self._short_list(names), bridge=plan["kept"][0].get("bridge") or plan["bridge_name"]),
                color=GREEN))
        if plan["mapped"]:
            lines = ["{fedora} → {opensuse}".format(fedora=m["text"], opensuse=", ".join(
                t["name"] for t in m["targets"])) for m in plan["mapped"]]
            short = bridgeui.system_words(plan)[1]
            out.append(_label((ngettext("{n} thing by its {system} name; openSUSE calls it:",
                                        "{n} things by their {system} names; openSUSE calls them:",
                                        len(lines)) if short else
                               ngettext("{n} thing under another name; openSUSE calls it:",
                                        "{n} things under other names; openSUSE calls them:", len(lines)))
                              .format(n=len(lines), system=short) + "\n" + "\n".join(lines), color=ORANGE))
        if plan["unmapped"]:
            missing = [r for r, _w in plan["unmapped"]]
            lbl = _label(ngettext("✗ openSUSE does not have: {list}", "✗ openSUSE does not have: {list}",
                                  len(missing)).format(list=self._short_list(missing)), color=RED)
            lbl.setToolTip("\n".join(f"{r}: {w}" for r, w in plan["unmapped"]))
            out.append(lbl)
        if not out:
            out.append(_label(_("nothing that you do not have already")))
        return out

    def _plan_notes(self, it):
        plan = it.get("plan") or {}
        if plan.get("error") or not (plan.get("mapped") or plan.get("unmapped")):
            return []
        if plan["unmapped"]:
            lines = "\n".join(f"• {r}: {w}" for r, w in plan["unmapped"])
            return [_Note(RED, "dialog-error", _("openSUSE does not have everything it asks for"), lines + "\n\n" + _(
                "zypper will not be able to install it as it is, and a bridge package cannot help: openSUSE has "
                "these things under no name Zypp Desk knows. Look for a version of the program made for "
                "openSUSE."))]
        n = len(plan["mapped"])
        short = bridgeui.system_words(plan)[1]
        text = (ngettext("It asks for {n} thing by its {system} name, which openSUSE has under another name.",
                         "It asks for {n} things by their {system} names, which openSUSE has under other names.", n)
                if short else
                ngettext("It asks for {n} thing under a name openSUSE does not use; openSUSE has it under another "
                         "name.", "It asks for {n} things under names openSUSE does not use; openSUSE has them "
                         "under other names.", n)).format(n=n, system=short)
        if plan.get("bridge"):
            text += " " + _("Your bridge {name} does not cover this version: adding it to the queue offers to make "
                            "release {release} of it.").format(name=plan["bridge"]["name"], release=plan["release"])
        else:
            text += " " + _("Adding it to the queue offers to make a bridge package ({name}): a small empty package "
                            "that tells zypper so.").format(name=plan["bridge_name"])
        return [_Note(ORANGE, "dialog-warning", bridgeui._title(plan), text)]

    def _notes(self, it):
        info, obs, chk = it["info"], it["obs"], it.get("obs_check")
        notes = []
        if info["refuse"]:
            notes.append(_Note(RED, "dialog-error", _("This file cannot be installed"), info["refuse"]))
            return notes
        if info["compare"] != "same":
            notes += self._plan_notes(it)
        if info.get("exposed") and info["compare"] != "same":
            text = info["exposed"] + " " + _("Zypp Desk read a copy of it that only you can change, and that copy is "
                                             "what gets installed: a change made to the file from now on does not "
                                             "reach the computer.")
            if info["signature"] == "unsigned":
                text += " " + _("It is not signed, so nothing tells whether somebody changed it before you opened it.")
            notes.append(_Note(ORANGE, "security-medium|dialog-warning", _("Other users can change this file"), text))
        # nothing to install (the same version): its signature does not matter
        sig = info["signature"] if info["compare"] != "same" else ""
        key8 = (info["keyid"] or sig[6:])[-8:].upper()
        if sig.startswith("nokey:"):
            again = button(_("Check the signature again"), "view-refresh", lambda: self.check_again(it))
            again.setToolTip(_("Ask rpm again whether it knows the key that signed this file (after you import it)."))
            again.setEnabled(not it["busy"])
            if it["key_queued"]:
                notes.append(_Note(ORANGE, "security-medium|dialog-warning", _("The key is in the queue"), _(
                    "Apply the queue first, then open the file again: until the key is imported, nobody can tell "
                    "whether the file comes from who it says."), [again]))
            else:
                text = _("Without the key nobody can tell whether the file comes from who it says, so it does not go "
                         "to the queue until the key is imported.")
                buttons = []
                if obs and chk is None:
                    text += " " + _("Looking at the key that {project} publishes…").format(project=obs["project"])
                elif obs and chk.get("key_matches"):
                    text += " " + _("The project {project} publishes this key in its repository.").format(
                        project=obs["project"])
                    buttons.append(self._import_button(it))
                elif obs and chk.get("key_matches") is False:
                    text += " " + _("The key that {project} publishes now ({fpr}) is not the one that signed this "
                                    "file. The project may have changed its key: download the file again from its "
                                    "repository.").format(project=obs["project"], fpr=chk.get("fingerprint", "")[-8:])
                elif obs and chk.get("exists") and chk.get("key_error"):
                    text += " " + _("The key of {project} could not be read ({error}).").format(
                        project=obs["project"], error=chk["key_error"])
                    buttons.append(self._import_button(it))
                else:
                    text += " " + _("Get the key from the official web site of whoever made it, check its fingerprint "
                                    "there and import it (Signing keys → Import a key…). Then press “Check the "
                                    "signature again”.")
                    if bridge.system_of(info)[0] == "fedora":
                        text += " " + _("Fedora publishes its keys, with their fingerprints, at {url}.").format(
                            url="https://fedoraproject.org/security/")
                notes.append(_Note(ORANGE, "security-medium|dialog-warning",
                                   _("It is signed, but you do not have the key ({key})").format(key=key8), text,
                                   buttons + [again]))
        elif sig == "unsigned":
            notes.append(_Note(ORANGE, "security-low|dialog-warning", _("It is not signed"), _(
                "Nothing confirms who made this file, or that nobody changed it after it was published. Zypp Desk "
                "asks you before it goes to the queue.")))
        if info["compare"] == "older":
            notes.append(_Note(ORANGE, "dialog-warning", _("This is an older version"), _(
                "You have {have}, and this file is {this}. Installing it goes back to the older version (zypper "
                "--oldpackage). Do it only if the new version gives you problems; the routine may update it again "
                "later.").format(have=info["installed_evr"], this=info["evr"])))
        elif info["compare"] == "same":
            notes.append(_Note(BLUE, "dialog-information", _("You already have this version"), _(
                "Version {version} is installed. Installing the file again would change nothing.").format(
                version=info["evr"])))
        if info["built_for"] and not any(x in info["built_for"] for x in ("Tumbleweed", "Factory")):
            notes.append(_Note(ORANGE, "dialog-warning", _("Built for another system"), _(
                "This file was built for {system}, not for Tumbleweed. It may not work, or it may need libraries "
                "that Tumbleweed does not have.").format(system=info["built_for"].replace("_", " "))))
        if obs:
            notes += self._obs_notes(it)
        return notes

    def _obs_notes(self, it):
        obs, chk = it["obs"], it.get("obs_check")
        project = obs["project"]
        if chk is None:
            return [_Note(BLUE, "", "", _("This program comes from the project {project} of the openSUSE Build "
                                          "Service. Looking for its repository for Tumbleweed…").format(
                project=project))]
        if chk.get("exists") is False:
            return [_Note(BLUE, "", "", _("The project {project} has no repository for Tumbleweed now, so this "
                                          "program cannot be kept up to date from there.").format(project=project))]
        if chk.get("exists") is None:
            return [_Note(BLUE, "", "", _("The repository of {project} could not be reached ({error}).").format(
                project=project, error=chk.get("error") or "?"))]
        have = self._repo_with_url(obs["url"])
        if have and have.get("queued"):
            return [_Note(BLUE, "", "", _("The repository of {project} is in the queue: after Apply, the routine "
                                          "keeps this program up to date.").format(project=project))]
        if have:
            return [_Note(BLUE, "", "", _("You already have the repository of {project} (“{name}”): the routine "
                                          "keeps this program up to date from there.").format(
                project=project, name=have.get("name") or have.get("alias")))]
        if self.repos is None:
            return []
        text = _("This program is in a repository of the openSUSE Build Service. If you add the repository, the "
                 "routine keeps it up to date.")
        if project.startswith("home:"):
            text += " " + _("It is the personal project of one person: nobody else reviews it.")
        add = button(_("Put the repository in the queue…") if project.startswith("home:") else
                     _("Put the repository in the queue"), "list-add", lambda: self.add_repo(it))
        add.setToolTip(_("Adds the repository of {project} ({url}) to the queue. After you apply the queue, the "
                         "routine keeps this program up to date.").format(project=project, url=obs["url"]))
        return [_Note(BLUE, "", "", text, [add])]

    def _import_button(self, it):
        obs = it["obs"]
        b = button(_("Import the key of {project}…").format(project=obs["project"]), "document-import",
                   lambda: self.import_key(it))
        b.setToolTip(_("Opens the window to import the key that {project} publishes ({url}), where you compare its "
                       "fingerprint before it is trusted.").format(project=obs["project"], url=obs["key_url"]))
        return b
