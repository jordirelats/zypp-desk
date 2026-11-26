# SPDX-License-Identifier: GPL-3.0-or-later
"""Own installers: the programs installed with their own installer (a .sh, a .run, a .tgz with install.pl), the
page of the mockup seccions-v1-C1-installadors, and the question before running an installer (C2).

Each program is a card: where it is, how much room it takes, its menu entries and whether it brings its own
uninstaller; "Open the folder" and "Uninstall…". Uninstall runs the program's own uninstaller in a terminal window,
as you, so that its questions can be answered; without one, and only in your own folder, Zypp Desk moves the
program's folder, menu entries and icons to the trash. A program installed for the whole computer (/opt,
/usr/local) needs the administrator password: Zypp Desk explains it and gives the commands for a terminal instead
of running them. "Install from a folder…" unpacks the chosen archive into Zypp Desk's own folder, runs the
installer in a terminal window as you, and notes what it created when it ends, so that the program can be removed
later even without an uninstaller of its own.

Nothing here runs as root. Names and paths are plain text.
"""

import glob
import os
import shlex
import shutil
import time

from PySide6.QtCore import QDateTime, QLocale, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon
from PySide6.QtWidgets import QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import scripts
from ..i18n import _, ngettext
from ..question import ask_change, label
from ..runner import Step
from ..simulation import human_size
from ..style import BLUE, GREEN, ORANGE
from ..tasks import run_async
from ..ui import Page, button, error_box, icon, info_box, muted
from .apps import CATEGORIES

ARCHIVE_ICON = "application-x-compressed-tar|package-x-generic"
SCRIPT_ICON = "application-x-shellscript|application-x-executable"
FILTER_EXTS = "*.sh *.run *.bin *.pl *.py *.tgz *.tar.gz *.tar.xz *.txz *.tar.bz2 *.tbz2 *.tar *.zip"
CMD_LINES = 12  # paths shown one by one in the commands of a question; the rest is counted


def _qt_trash(path):
    """Move a path to the trash with Qt (when gio is not there). None, or the error."""
    from PySide6.QtCore import QFile
    res = QFile.moveToTrash(path)
    ok = res[0] if isinstance(res, tuple) else bool(res)
    return None if ok else _("it could not be moved to the trash")


def program_qicon(p):
    """The program's own icon: a picture found in your icons, the theme's, or a generic one."""
    f = p.get("icon_file")
    if f:
        ic = QIcon(f)
        if not ic.isNull():
            return ic
    n = p.get("icon") or ""
    if n:
        if os.path.isabs(n):
            if os.path.isfile(n):
                return QIcon(n)
        else:
            ic = QIcon.fromTheme(n)
            if not ic.isNull():
                return ic
    return icon("application-x-executable", "application-x-shellscript")


def icon_pixmap(p, size):
    pm = program_qicon(p).pixmap(size, size)
    if not pm.isNull() and pm.width() < size and pm.height() < size:
        pm = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return pm


def _size_text(size):
    return human_size(size) if size is not None else "…"


def _where(p, places):
    return scripts.home_short(p["folder"], places.home) if p.get("folder") else "?"


def _visible(p):
    return [e for e in p.get("entries", []) if not e.get("hidden")]


def _kind_of(path, p, places):
    if p.get("folder") and os.path.normpath(path) == os.path.normpath(p["folder"]):
        return "folder"
    for kind, d in (("menu", places.app_dir), ("desktop", places.desktop_dir), ("icons", places.icons_dir),
                    ("mime", places.mime_dir)):
        if d is not None and scripts.under(path, d):
            return kind
    return "other"


def trash_items(p, paths, places, size=None):
    """What moving the program to the trash takes away, in words: one item a kind of thing."""
    groups = {}
    for x in paths:
        groups.setdefault(_kind_of(x, p, places), []).append(x)
    def short(xs):
        return [scripts.home_short(x, places.home) for x in xs]

    items = []
    if "folder" in groups:
        detail = _("Its folder, with everything inside it")
        if size is not None:
            detail += " · " + human_size(size)
        items.append(("folder", scripts.home_short(groups["folder"][0], places.home), detail))
    if "menu" in groups:
        n = len(groups["menu"])
        items.append(("application-x-desktop|application-menu",
                      ngettext("{n} menu entry", "{n} menu entries", n).format(n=n),
                      ", ".join(e["name"] for e in p.get("entries", []) if e["file"] in groups["menu"]),
                      short(groups["menu"])))
    if "desktop" in groups:
        n = len(groups["desktop"])
        items.append(("user-desktop", ngettext("{n} shortcut on the desktop", "{n} shortcuts on the desktop", n)
                      .format(n=n), "", short(groups["desktop"])))
    if "icons" in groups:
        n = len(groups["icons"])
        items.append(("image-x-generic", ngettext("{n} icon", "{n} icons", n).format(n=n),
                      _("The pictures of its menu entries and of its files, in several sizes"),
                      short(groups["icons"])))
    if "mime" in groups:
        n = len(groups["mime"])
        items.append(("text-xml|application-xml", ngettext("{n} file type", "{n} file types", n).format(n=n),
                      _("What tells the computer which program opens its files"), short(groups["mime"])))
    if "other" in groups:
        n = len(groups["other"])
        items.append(("document-new", ngettext("{n} more thing it created", "{n} more things it created", n)
                      .format(n=n), _("Zypp Desk noted it when it ran the installer"), short(groups["other"])))
    return items


def _icon_globs(paths, places):
    """The icons of one name in every size as one line (…/hicolor/*/apps/name.png), when that pattern matches
    exactly those files; the other paths as they are. Keeps the order of the first of each group."""
    base = str(places.icons_dir)
    groups, order = {}, []
    for x in paths:
        key = x
        if scripts.under(x, base):
            parts = os.path.relpath(x, base).split(os.sep)
            if len(parts) == 4:  # theme / size / context / file
                key = (parts[0], parts[2], parts[3])
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(x)
    out = []
    for key in order:
        xs = groups[key]
        if isinstance(key, tuple) and len(xs) > 1:
            pattern = os.path.join(glob.escape(base), glob.escape(key[0]), "*", glob.escape(key[1]),
                                   glob.escape(key[2]))
            if set(glob.glob(pattern)) == set(xs):
                out.append("/".join([scripts.shell_path(base, places.home), shlex.quote(key[0]), "*",
                                     shlex.quote(key[1]), shlex.quote(key[2])]))
                continue
        out += [scripts.shell_path(x, places.home) for x in xs]
    return out


def trash_commands(paths, places):
    shown = _icon_globs(paths, places)
    lines = [f"gio trash -- {x}" for x in shown[:CMD_LINES]]
    if len(shown) > CMD_LINES:
        rest = len(shown) - CMD_LINES
        lines.append(ngettext("(and {n} more, the same way)", "(and {n} more, the same way)", rest).format(n=rest))
    for cmd in scripts.after_trash(paths, places):
        lines.append(" ".join([cmd[0], *(scripts.shell_path(a, places.home) for a in cmd[1:])]))
    return lines


def uninstall_question(p, argv, paths, places, size=None, terminal=True):
    """The question when the program has its own uninstaller: run it (in a terminal window), or move it all to the
    trash instead, or not yet. (title, keyword arguments of question.ask_change)."""
    unin = p["uninstaller"]
    detail = _where(p, places)
    if size is not None:
        detail += " · " + human_size(size)
    folder = scripts.home_short(os.path.dirname(unin), places.home)
    if terminal:
        means = _("its own uninstaller runs as you, without the administrator password, in a terminal window "
                  "where you can answer what it asks. When it ends, Zypp Desk looks again.")
    else:
        means = _("its own uninstaller runs as you, without the administrator password, in the terminal below. "
                  "Nobody can type there: if it asks something, it gets no answer. When it ends, Zypp Desk looks "
                  "again.")
    cmds = [(_("Run its uninstaller (in {folder}, as you):").format(folder=folder), [scripts.display(argv)])]
    buttons = [("no", _("Not yet"), False)]
    if paths:
        cmds.append((_("Move it to the trash instead:"), trash_commands(paths, places)))
        buttons.append(("trash", _("Move it to the trash instead"), False))
    buttons.append(("run", _("Run its uninstaller"), True))
    return _("Uninstall {name}?").format(name=p["name"]), dict(
        icon_name="edit-delete", items=[(program_qicon(p), p["name"], detail)], means=means, commands_=cmds,
        password=False, width=720, buttons=buttons)


def trash_question(p, paths, places, size=None):
    """The question when Zypp Desk removes the program itself: everything goes to the trash."""
    if p.get("state") == "missing":
        title = _("Clean up what is left of {name}?").format(name=p["name"])
        means = _("its program is not there any more. Zypp Desk moves what is left to the trash, as you, without "
                  "the administrator password. You can bring it back from the trash.")
    else:
        title = _("Move {name} to the trash?").format(name=p["name"])
        means = _("it has no uninstaller of its own, so Zypp Desk moves it to the trash, as you, without the "
                  "administrator password. Nothing is deleted for good: you can bring it all back from the trash. "
                  "Files you made with it and saved elsewhere are not touched.")
    return title, dict(
        icon_name="user-trash-full|user-trash", items=trash_items(p, paths, places, size), means=means,
        commands_=trash_commands(paths, places), password=False, width=720,
        buttons=[("no", _("Not yet"), False), ("yes", _("Move to the trash"), True)])


def system_question(p, places):
    """A program for the whole computer: explain, and give the commands to run in a terminal (never run here)."""
    cmds = scripts.system_commands(p, places)
    if p.get("state") == "empty":
        title = _("Only empty folders are left of {name}").format(name=p["name"])
    else:
        title = _("{name} is installed for the whole computer").format(name=p["name"])
    if p.get("uninstaller"):
        means = _("removing it needs the administrator password, and Zypp Desk does not run other programs' "
                  "uninstallers as the administrator. If you are sure, run these commands in a terminal: sudo asks "
                  "for the password, and the uninstaller may ask you things.")
        warning = None
    else:
        means = _("removing it needs the administrator password, and Zypp Desk does not delete other programs' "
                  "folders as the administrator. If you are sure, run these commands in a terminal: sudo asks for "
                  "the password.")
        warning = _("rm deletes for good: what the administrator deletes does not go to the trash. Look at the "
                    "folder first.")
    return title, dict(
        icon_name="dialog-password|object-locked", items=[(program_qicon(p), p["name"], _where(p, places))],
        means=means, warning=warning, commands_=cmds, commands_title=_("The commands to run in a terminal:"),
        password=False, width=700,
        buttons=[("close", _("Close"), False), ("copy", _("Copy the commands"), True)])


def install_question(info, places, terminal=True):
    """The question of mockup C2, before running an installer."""
    parts = [_("In {folder}").format(folder=scripts.home_short(info["folder"], places.home)),
             human_size(info["size"])]
    if info["kind"] == "archive":
        parts.append(_("inside there is {file}, its installer").format(file=info["installer"]))
    item = (ARCHIVE_ICON if info["kind"] == "archive" else SCRIPT_ICON, info["name"], " · ".join(parts))
    if terminal:
        means = _("the installer runs as you, without the administrator password, in a terminal window where you "
                  "can answer what it asks. Zypp Desk notes what it creates (folders and menu entries) so that it "
                  "can remove it later.")
    else:
        means = _("the installer runs as you, without the administrator password, in the terminal below. Nobody "
                  "can type there: if it asks something, it gets no answer. Zypp Desk notes what it creates "
                  "(folders and menu entries) so that it can remove it later.")
    cmd = scripts.display(info["argv"])
    if info["kind"] == "archive":
        cmds = [_("Zypp Desk unpacks {file} into {folder}").format(
                    file=info["name"], folder=scripts.home_short(info["dest"], places.home)),
                _("{command}   (in the unpacked folder, as you)").format(command=cmd)]
    elif info.get("dest"):
        cmds = [_("Zypp Desk copies {file} into {folder} and lets it run").format(
                    file=info["name"], folder=scripts.home_short(info["dest"], places.home)),
                _("{command}   (in that folder, as you)").format(command=cmd)]
    else:
        cmds = [_("{command}   (in {folder}, as you)").format(
            command=cmd, folder=scripts.home_short(info["folder"], places.home))]
    return _("Run this installer?"), dict(
        icon_name="application-x-shellscript", items=[item], means=means,
        warning=_("It is not signed: nobody confirms who made it. Run it only if you downloaded it from the "
                  "program's own website."),
        commands_=cmds, password=False, width=740,
        buttons=[("no", _("Not yet"), False), ("yes", _("Run the installer"), True)])


class _Banner(QFrame):
    """'The installer is running in its own window…', with 'It has ended' for when the window is gone."""

    def __init__(self, page):
        super().__init__(page)
        self.setObjectName("runbanner")
        self.setStyleSheet("QFrame#runbanner { background: rgba(29, 153, 243, 0.08); border: 1px solid "
                           "rgba(29, 153, 243, 0.40); border-radius: 10px; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(12)
        pic = QLabel(self)
        pic.setPixmap(icon("utilities-terminal", "application-x-shellscript").pixmap(28, 28))
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        self.text = label("", small=True)
        h.addWidget(self.text, 1)
        self.done = QPushButton(_("It has ended"), self)
        self.done.setAutoDefault(False)
        self.done.setToolTip(_("Zypp Desk waits for the terminal window to close. If it has already closed, or "
                               "never opened, press here."))
        self.done.clicked.connect(lambda: page._poll(force=True))
        h.addWidget(self.done, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()


class ScriptsPage(Page):
    key = "scripts"
    title = _("Own installers")
    icon_name = "application-x-shellscript"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.places = scripts.default_places()
        self.launch = scripts.launch  # how a terminal window starts (the tests put a quiet one here)
        self.mover = scripts.gio_trash if shutil.which("gio") else _qt_trash
        self.terminal_argv = scripts.terminal_argv
        self.programs = []
        self.sizes = {}
        self.error = ""
        self.busy = False
        self.running = None  # the installer or uninstaller running in a terminal window now
        self._round = 0
        self._loaded_at = 0.0
        self._size_labels = {}
        self.poller = QTimer(self)
        self.poller.setInterval(1000)
        self.poller.timeout.connect(self._poll)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self.intro = muted(_("Programs that are not RPM packages or Flatpaks: they install with their own "
                             "installer (a .sh, a .run, a .tgz with install.pl). Zypp Desk cannot update them, but it "
                             "tells you where they are and how much room they take, and it can remove them."), self)
        lay.addWidget(self.intro)
        self.banner = _Banner(self)
        lay.addWidget(self.banner)
        self.note = label("", small=True)
        self.note.hide()
        lay.addWidget(self.note)
        self.status = muted(_("Looking for them…"), self)
        lay.addWidget(self.status)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(10)
        lay.addLayout(self.cards)
        lay.addStretch(1)

        # the header button lives as long as the page; without a parent until the window shows it
        self.b_install = button(_("Install from a folder…"), None, self.install, primary=True)
        self.b_install.setToolTip(_("Run the installer of a program (a .sh, a .run, or a .tgz or .zip with one "
                                    "inside) as you, and note what it creates"))

    def header_actions(self):
        return [self.b_install]

    def activated(self):
        # programs come and go outside Zypp Desk too: look again when the page is shown after a while
        if not self.loaded or (not self.busy and time.monotonic() - self._loaded_at > 20):
            self.loaded = True
            self.reload()

    # ------------------------------------------------------------ looking
    def reload(self):
        self._round += 1
        rnd = self._round
        self.busy = True
        if not self.programs:
            self.status.setText(_("Looking for them…"))
            self.status.show()
        run_async(self._find, self.places, on_done=lambda res, err: self._found(rnd, res, err))

    @staticmethod
    def _find(places):
        try:
            scripts.prune_manifest(places)
        except OSError:
            pass
        return scripts.find_programs(places), scripts.load_manifest(places)["pending"]

    def _found(self, rnd, res, err):
        if rnd != self._round:
            return  # an older look answered late
        self.busy = False
        self._loaded_at = time.monotonic()
        if err:
            self.error = str(err)
            self.status.setText(_("Zypp Desk could not look for them: {error}").format(error=self.error))
            self.status.show()
            return
        self.error = ""
        self.programs, pending = res
        self.status.hide()
        self._fill()
        self._resume(pending)
        folders = [p["folder"] for p in self.programs if p["state"] == "ok" and p["folder"]
                   and p["folder"] not in self.sizes]
        if folders:
            run_async(scripts.sizes, folders, self.places, on_done=lambda r, e: self._sized(r, e))

    def _sized(self, res, err):
        if err or not res:
            return
        self.sizes.update(res)
        for folder, lbl in list(self._size_labels.items()):
            if folder in res:
                try:
                    lbl.setText(_size_text(res[folder]))
                except RuntimeError:  # the card was rebuilt meanwhile
                    self._size_labels.pop(folder, None)

    # ------------------------------------------------------------ the cards
    def _fill(self):
        while self.cards.count():
            w = self.cards.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._size_labels = {}
        found = [p for p in self.programs if p["state"] == "ok"]
        left = [p for p in self.programs if p["state"] != "ok"]
        for p in found:
            self.cards.addWidget(self._program_card(p))
        if left:
            head = label(_("Left behind"), bold=True)
            head.setContentsMargins(2, 8, 0, 0)
            self.cards.addWidget(head)
            self.cards.addWidget(muted(_("Menu entries and folders of programs that are not there any more."),
                                       self))
            for p in left:
                self.cards.addWidget(self._leftover_card(p))
        self.cards.addWidget(self._nothing_else_card(bool(found)))

    def facts(self, p):
        """The rows of a card: (what, text)."""
        rows = []
        where = _where(p, self.places)
        if p["scope"] == "home":
            rows.append((_("Where"), _("{folder} · in your folder, without the administrator password").format(
                folder=where)))
        else:
            rows.append((_("Where"), _("{folder} · for the whole computer: removing it needs the administrator "
                                       "password").format(folder=where)))
        if p.get("folder"):
            rows.append((_("Size"), None))  # filled when du answers
        vis = _visible(p)
        if vis:
            text = ngettext("{n} entry ({names})", "{n} entries ({names})", len(vis)).format(
                n=len(vis), names=", ".join(e["name"] for e in vis))
        else:
            text = _("none")
        if p.get("desktop_copies"):
            text += " · " + _("also on the desktop")
        rows.append((_("In the menu"), text))
        unin = p.get("uninstaller")
        if unin:
            text = _("yes: {file}, from the program itself").format(file=os.path.basename(unin))
        elif p["scope"] == "home":
            text = _("none: Zypp Desk can move it to the trash")
        else:
            text = _("none")
        rows.append((_("Uninstaller"), text))
        rec = p.get("recorded")
        if rec:
            when = QLocale().toString(QDateTime.fromSecsSinceEpoch(int(rec.get("finished") or rec.get("started")
                                                                        or 0)), QLocale.FormatType.ShortFormat)
            rows.append((_("Installed"), _("by Zypp Desk on {date}, from {file}").format(
                date=when, file=rec.get("installer_name") or "?")))
        return rows

    def _program_card(self, p):
        f = QFrame(self)
        f.setObjectName("card")
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 14, 16, 14)
        h.setSpacing(14)
        pic = QLabel(f)
        pic.setPixmap(icon_pixmap(p, 48))
        pic.setFixedSize(48, 48)
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        t = label(p["name"], bold=True)
        tf = t.font()
        tf.setPointSizeF(tf.pointSizeF() * 1.15)
        t.setFont(tf)
        v.addWidget(t)
        sub = p.get("summary") or self._category(p)
        if sub:
            v.addWidget(label(sub, small=True, muted=True))
        v.addSpacing(6)
        g = QGridLayout()
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(4)
        for r, (k, text) in enumerate(self.facts(p)):
            g.addWidget(label(k, muted=True, wrap=False), r, 0, Qt.AlignmentFlag.AlignTop)
            if text is None:  # the size
                lbl = label(_size_text(self.sizes.get(p["folder"])), selectable=True)
                if p.get("folder"):
                    self._size_labels[p["folder"]] = lbl
            else:
                lbl = label(text, selectable=True)
            g.addWidget(lbl, r, 1)
        g.setColumnStretch(1, 1)
        v.addLayout(g)
        h.addLayout(v, 1)
        bv = QVBoxLayout()
        bv.setSpacing(6)
        b_open = QPushButton(_("Open the folder"), f)
        b_open.setEnabled(bool(p.get("folder") and os.path.isdir(p["folder"])))
        b_open.clicked.connect(lambda _c=False, x=p: self.open_folder(x))
        b_rm = QPushButton(_("Uninstall…"), f)
        b_rm.clicked.connect(lambda _c=False, x=p: self.uninstall(x))
        bv.addWidget(b_open)
        bv.addWidget(b_rm)
        bv.addStretch(1)
        h.addLayout(bv)
        f.buttons = {"open": b_open, "uninstall": b_rm}
        f.program = p
        return f

    def _leftover_card(self, p):
        f = QFrame(self)
        f.setObjectName("card")
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(14)
        pic = QLabel(f)
        pic.setPixmap(icon_pixmap(p, 32))
        pic.setFixedSize(32, 32)
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        v.addWidget(label(p["name"], bold=True))
        where = _where(p, self.places)
        if p["state"] == "empty":
            text = _("Only empty folders are left in {folder}. It is for the whole computer: removing it needs the "
                     "administrator password.").format(folder=where)
            btext = _("How to remove it…")
        else:
            n = len(p["entries"])
            text = ngettext("Its program is not in {folder} any more: only its menu entry is left.",
                            "Its program is not in {folder} any more: only its {n} menu entries are left.",
                            n).format(folder=where, n=n)
            btext = _("Clean up…")
        v.addWidget(label(text, small=True, muted=True, selectable=True))
        h.addLayout(v, 1)
        b = QPushButton(btext, f)
        b.clicked.connect(lambda _c=False, x=p: self.uninstall(x))
        h.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        f.buttons = {"uninstall": b}
        f.program = p
        return f

    def _nothing_else_card(self, some):
        f = QFrame(self)
        f.setObjectName("card")
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(14)
        pic = QLabel(f)
        pic.setPixmap(icon("application-x-executable", "dialog-information").pixmap(32, 32))
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignVCenter)
        v = QVBoxLayout()
        v.setSpacing(2)
        v.addWidget(label(_("No other") if some else _("None found"), bold=True))
        v.addWidget(label(_("Zypp Desk looks in your folder, /opt and /usr/local, and at the menu entries that "
                            "belong to no package."), small=True, muted=True))
        h.addLayout(v, 1)
        return f

    @staticmethod
    def _category(p):
        for c in p.get("categories", []):
            if c in CATEGORIES:
                return CATEGORIES[c][0]
        return ""

    def cards_shown(self):
        """The cards on the page (for the tests): [(program, frame)]."""
        out = []
        for i in range(self.cards.count()):
            w = self.cards.itemAt(i).widget()
            if w is not None and hasattr(w, "program"):
                out.append((w.program, w))
        return out

    # ------------------------------------------------------------ open, uninstall
    def open_folder(self, p):
        if p.get("folder") and os.path.isdir(p["folder"]):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p["folder"]))

    def _say(self, text, kind="ok"):
        color = {"ok": GREEN, "warn": ORANGE, "info": BLUE}.get(kind, BLUE)
        self.note.setStyleSheet(f"color: {color};")
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _wait_first(self):
        if self.running:
            info_box(self, self.title, _("Wait until the installer or uninstaller that is running now ends."))
            return True
        return False

    def uninstall(self, p):
        if self._wait_first():
            return
        if p["state"] == "empty" or p["scope"] == "system":
            title, kw = system_question(p, self.places)
            if ask_change(self, title, **kw) == "copy":
                QGuiApplication.clipboard().setText("\n".join(kw["commands_"]))
                self._say(_("The commands are copied: paste them in a terminal."), "info")
            return
        paths = scripts.removal_paths(p, self.places)
        size = self.sizes.get(p.get("folder"))
        unin = p.get("uninstaller") if p["state"] == "ok" else None
        argv = scripts.command_for(unin) if unin else None
        if unin and argv:
            has_term = self.terminal_argv(["true"]) is not None
            title, kw = uninstall_question(p, argv, paths, self.places, size, has_term)
            answer = ask_change(self, title, **kw)
            if answer == "run":
                self._run_in_terminal("uninstall", argv, os.path.dirname(unin), program=p)
            elif answer == "trash":
                self._trash(p, paths)
            return
        if not paths:
            info_box(self, self.title, _("Zypp Desk found nothing of {name} in your folder that it can move to the "
                                         "trash.").format(name=p["name"]))
            return
        title, kw = trash_question(p, paths, self.places, size)
        if ask_change(self, title, **kw) == "yes":
            self._trash(p, paths)

    def _trash(self, p, paths):
        self._say(_("Moving {name} to the trash…").format(name=p["name"]), "info")
        run_async(scripts.trash, paths, self.places, self.mover, on_done=lambda r, e: self._trashed(p, r, e))

    def _trashed(self, p, res, err):
        if err:
            self._say("")
            error_box(self, self.title, str(err))
            self.reload()
            return
        n = len(res["moved"])
        self._say(ngettext("{name}: {n} thing moved to the trash.", "{name}: {n} things moved to the trash.", n)
                  .format(name=p["name"], n=n), "ok" if not res["failed"] else "warn")
        if res["failed"]:
            error_box(self, self.title, _("These could not be moved to the trash:") + "\n" + "\n".join(
                f"{scripts.home_short(x, self.places.home)}: {why}" for x, why in res["failed"]))
        self.reload()

    # ------------------------------------------------------------ install
    def install(self):
        if self._wait_first():
            return
        start = self.places.download_dir if self.places.download_dir and os.path.isdir(
            self.places.download_dir) else self.places.home
        path, _f = QFileDialog.getOpenFileName(
            self, _("Choose the installer"), str(start),
            _("Installers ({patterns})").format(patterns=FILTER_EXTS) + ";;" + _("All files (*)"))
        if not path:
            return
        self.open_installer(path)

    def open_installer(self, path):
        """Look inside the chosen file, then ask (mockup C2)."""
        self.b_install.setEnabled(False)
        self._say(_("Looking inside {file}…").format(file=os.path.basename(path)), "info")
        run_async(scripts.inspect_installer, path, self.places,
                  on_done=lambda info, err: self._inspected(path, info, err))

    def _inspected(self, path, info, err):
        self.b_install.setEnabled(not self.running)
        self._say("")
        name = os.path.basename(path)
        if err:
            error_box(self, _("Install from a folder"), _("{file} could not be read: {error}").format(
                file=name, error=err))
            return
        if not info.get("installer"):
            error_box(self, _("Install from a folder"), _(
                "Zypp Desk found no installer in {file}. It looks for install.sh, install.pl, setup.sh, or a .run "
                "or .bin file.").format(file=name))
            return
        if not info.get("argv"):
            error_box(self, _("Install from a folder"), _("Zypp Desk does not know how to run {file}.").format(
                file=info["installer"]))
            return
        has_term = self.terminal_argv(["true"]) is not None
        title, kw = install_question(info, self.places, has_term)
        if ask_change(self, title, **kw) != "yes":
            return
        self.b_install.setEnabled(False)
        self._say(_("Preparing the installer…"), "info")
        run_async(scripts.prepare_installer, info, self.places,
                  on_done=lambda res, e: self._prepared(info, res, e))

    def _prepared(self, info, res, err):
        self.b_install.setEnabled(not self.running)
        self._say("")
        if err:
            error_box(self, _("Install from a folder"), _("The installer could not be prepared: {error}").format(
                error=err))
            return
        keep = {k: info.get(k) for k in ("path", "name", "kind", "installer")}
        keep.update(argv=res["argv"], workdir=res["workdir"])
        self._run_in_terminal("install", res["argv"], res["workdir"], info=keep, before=res["before"])

    # ------------------------------------------------------------ running in a terminal window
    def _run_in_terminal(self, kind, argv, workdir, program=None, info=None, before=None):
        status = scripts.status_file(self.places, kind)
        inner = scripts.wrapped(argv, workdir, status, ok=_("It has ended."),
                                bad=_("It has ended with an error, code"),
                                bye=_("Press Enter to close this window."))
        term = self.terminal_argv(inner)
        wall = time.time()
        r = {"kind": kind, "status": str(status), "started": time.monotonic(), "wall": wall, "program": program,
             "info": info, "before": before, "proc": None, "pid": None, "id": f"{int(wall * 1000)}",
             "waiting": False}
        name = (program or {}).get("name") or (info or {}).get("name") or ""
        if term:
            try:
                proc = self.launch(term, workdir)
            except OSError as e:
                error_box(self, self.title, _("The terminal window could not be opened: {error}").format(error=e))
                return
            r["proc"], r["pid"] = proc, getattr(proc, "pid", None)
            self.running = r
            self._save_pending(r)
            self.poller.start()
        else:
            # no terminal window program: the terminal panel below, where nobody can type
            self.running = r
            self._save_pending(r)
            title = (_("Installer of {name}") if kind == "install" else _("Uninstaller of {name}")).format(name=name)
            step = Step("sh", inner[1:], cond="always", label=scripts.display(argv), shown=scripts.display(argv))
            if not self.ctx.run([step], title, on_done=lambda ok: self._poll(force=True)):
                self.running = None
                self._drop_pending(r)
        self._show_banner()

    def _save_pending(self, r):
        if r["kind"] != "install":
            return
        try:
            scripts.add_pending(self.places, {"id": r["id"], "status": r["status"], "pid": r["pid"],
                                              "started": int(r["wall"]), "info": r["info"], "before": r["before"]})
        except OSError:
            pass  # only for finishing after a restart: the record itself is made when it ends

    def _drop_pending(self, r):
        try:
            scripts.drop_pending(self.places, r["id"])
        except OSError:
            pass

    def _resume(self, pending):
        """An installer that was running when Zypp Desk closed: finish its record now, or wait for it."""
        if self.running or not pending:
            return
        p = pending[-1]
        for other in pending[:-1]:
            self._drop_pending(other)
        self.running = {"kind": "install", "status": p.get("status", ""), "started": time.monotonic() - 60,
                        "wall": p.get("started") or time.time(), "program": None, "info": p.get("info") or {},
                        "before": p.get("before") or {}, "proc": None, "pid": p.get("pid"), "id": p.get("id"),
                        "waiting": not p.get("pid")}
        self.poller.start()
        self._show_banner()
        self._poll()

    def _show_banner(self):
        r = self.running
        self.b_install.setEnabled(r is None)
        if not r:
            self.banner.hide()
            return
        name = (r.get("program") or {}).get("name") or (r.get("info") or {}).get("name") or ""
        if r["kind"] == "install":
            text = _("The installer {file} is running in its own window. Answer its questions there; when it "
                     "ends, Zypp Desk notes what it created.").format(file=name)
        else:
            text = _("The uninstaller of {name} is running in its own window. When it ends, Zypp Desk looks "
                     "again.").format(name=name)
        if r.get("waiting"):
            text += " " + _("If its window has already closed, press “It has ended”.")
        self.banner.text.setText(text)
        self.banner.show()

    def _poll(self, force=False):
        r = self.running
        if not r:
            self.poller.stop()
            return
        code = scripts.read_status(r["status"])
        if code is not None:
            self._ended(code, True)
            return
        if force:
            self._ended(None, False)
            return
        proc = r.get("proc")
        if proc is not None:
            if proc.poll() is None:
                return
            if time.monotonic() - r["started"] < 3:
                # the launcher handed the window to a terminal that was already open: wait for the code instead
                r["proc"], r["pid"], r["waiting"] = None, None, True
                self._show_banner()
                return
            self._ended(None, False)
        elif r.get("pid") and not scripts.pid_alive(r["pid"]):
            self._ended(None, False)

    def _ended(self, code, finished):
        r, self.running = self.running, None
        self.poller.stop()
        self._show_banner()
        if r["kind"] == "install":
            run_async(self._record, self.places, r, code,
                      on_done=lambda rec, err: self._recorded(r, code, finished, rec, err))
            return
        name = (r.get("program") or {}).get("name", "")
        if not finished:
            self._say(_("The window of the uninstaller of {name} closed before it ended.").format(name=name), "warn")
        elif code:
            self._say(_("The uninstaller of {name} ended with an error (code {code}).").format(name=name, code=code),
                      "warn")
        else:
            self._say(_("The uninstaller of {name} has ended.").format(name=name), "ok")
        self.sizes.pop((r.get("program") or {}).get("folder"), None)
        self.reload()

    @staticmethod
    def _record(places, r, code):
        after = scripts.snapshot(places)
        rec = scripts.make_record(r.get("info") or {}, r.get("before") or {}, after, code, r.get("wall") or 0)
        if rec["created"]:
            scripts.add_record(places, rec)
        scripts.drop_pending(places, r["id"])
        return rec

    def _recorded(self, r, code, finished, rec, err):
        if err:
            self._say(_("What the installer created could not be noted: {error}").format(error=err), "warn")
            self.reload()
            return
        created = rec["created"]
        names = [os.path.basename(x) for k in ("home", "opt") for x in created.get(k, [])
                 if not os.path.basename(x).startswith(".")]
        n_menu = len([x for x in created.get("apps", []) + created.get("local_apps", []) if x.endswith(".desktop")])
        parts = []
        if not finished:
            parts.append(_("The installer's window closed before it ended."))
        elif code:
            parts.append(_("The installer ended with an error (code {code}).").format(code=code))
        else:
            parts.append(_("The installer has ended."))
        if created:
            what = names + ([ngettext("{n} menu entry", "{n} menu entries", n_menu).format(n=n_menu)]
                            if n_menu else [])
            n = sum(len(v) for v in created.values())
            parts.append(ngettext("Zypp Desk noted {n} thing it created ({what}): it can remove it later.",
                                  "Zypp Desk noted {n} things it created ({what}): it can remove them later.", n)
                         .format(n=n, what=", ".join(what) or "…"))
        else:
            parts.append(_("Nothing new appeared in your folder, the menu or /opt."))
        self._say(" ".join(parts), "ok" if finished and not code and created else "warn")
        self.reload()
