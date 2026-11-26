# SPDX-License-Identifier: GPL-3.0-or-later
"""Expert: every zypper command, on a form or typed as in a terminal.

For people who know zypper and want all of it without leaving the window. On the left, zypper's commands by
group; on the right, a line to type a command, the form of the chosen command (its options as zypper's help
gives them, in English) and the exact command it will use.

The commands and their options come from zypper's own help (zyppcmds.py). The ones that only read run as the user,
with the output in the terminal panel. The ones that change the system go through the root helper, after the
question of question.py (Enter never starts a change), and always with -n: nobody can answer zypper here.
"""

import os
import shlex

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import backend, question, zyppcmds
from ..i18n import _, ngettext, user_lang
from ..runner import Step
from ..style import GREEN, ORANGE, RED
from ..tasks import run_async
from ..terminal import BG, FG
from ..txqueue import Action, is_file_target
from ..ui import Card, Page, button, heading, mono

TOGGLE = "__all__"  # the "All commands" row of the list

LIST_STYLE = ("QListWidget#expertcmds { background: transparent; border: none; outline: 0; }"
              "QListWidget#expertcmds::item { padding: 4px 8px; margin: 1px 0px; border-radius: 7px; }"
              "QListWidget#expertcmds::item:hover { background: rgba(39, 174, 96, 0.10); }"
              "QListWidget#expertcmds::item:selected { background: rgba(39, 174, 96, 0.22); color: palette(text); }"
              # a thin scroll bar, as the sidebar's
              "QListWidget#expertcmds QScrollBar:vertical { width: 8px; background: transparent; margin: 0; }"
              "QListWidget#expertcmds QScrollBar::handle:vertical { background: rgba(127, 140, 141, 0.45);"
              " border-radius: 4px; min-height: 30px; }"
              "QListWidget#expertcmds QScrollBar::add-line:vertical, QListWidget#expertcmds QScrollBar::sub-line:vertical"
              " { height: 0; }"
              "QListWidget#expertcmds QScrollBar::add-page:vertical, QListWidget#expertcmds QScrollBar::sub-page:vertical"
              " { background: transparent; }")
DARK_STYLE = (f"background: {BG}; color: {FG}; border: 1px solid rgba(127, 140, 141, 0.45); border-radius: 7px;"
              " padding: 7px 10px;")

# Global options that only change how zypper prints: the queue runs its own commands, so they are simply left out.
QUEUE_IGNORES_GLOBALS = {"-v", "--verbose", "-q", "--quiet", "--color", "--no-color", "-A", "--no-abbrev", "-t",
                         "--terse", "-x", "--xmlout", "-n", "--non-interactive"}
# The options of each command the queue can carry (txqueue.Action and its options); the others stay on the form.
QUEUE_CARRIES = {
    "install": {"--oldpackage", "--from", "--repo", "--type", "--details", "--no-confirm"},
    "remove": {"--clean-deps", "--no-clean-deps", "--type", "--details", "--no-confirm"},
    "addlock": {"--type", "--repo"},
    "removelock": {"--type", "--repo"},
    "addlocale": set(),
    "removelocale": set(),
    "addrepo": {"--name", "--priority", "--keep-packages", "--no-keep-packages", "--refresh", "--no-refresh",
                "--enable", "--disable", "--no-gpgcheck", "--gpgcheck-allow-unsigned-repo",
                "--gpgcheck-allow-unsigned-package", "--default-gpgcheck"},
    "removerepo": set(),
    "modifyrepo": {"--name", "--enable", "--disable", "--refresh", "--no-refresh", "--priority", "--keep-packages",
                   "--no-keep-packages", "-r", "-R"},
}
# Shown first under "Options" when the command has them: the options people use most.
PROMOTED = ("--no-recommends", "--oldpackage", "--download-only")
OPPOSITES = [("--enable", "--disable"), ("--refresh", "--no-refresh"), ("--keep-packages", "--no-keep-packages"),
             ("--clean-deps", "--no-clean-deps")]
EFFECT_ICONS = {"packages": "system-software-install|package-x-generic", "sources": "folder-cloud|folder-network",
                "refresh": "view-refresh", "cache": "edit-clear-all|edit-clear", "locks": "object-locked",
                "languages": "preferences-desktop-locale|preferences-desktop-locale-symbolic",
                "download": "folder-download|download"}


def group_title(key):
    return {"packages": _("Packages"), "updates": _("Updates"), "search": _("Search"),
            "repositories": _("Repositories"), "services": _("Services"), "locks": _("Locks and languages"),
            "maintenance": _("Maintenance"), "other": _("Other")}.get(key, key)


def section_title(name):
    """zypper's sections of options, in the language of the window."""
    return {"Command options": _("Options"), "Solver options": _("Solver options"),
            "Expert options": _("Expert options"), "Legacy options": _("Legacy options"),
            "Advanced": _("Advanced options")}.get(name, name)


def arg_label(kind):
    return {"packages": _("Packages"), "search": _("Search for"), "locks": _("What to lock"),
            "unlock": _("Locks to remove (numbers or names)"), "languages": _("Languages (codes such as ca or de)"),
            "address": _("Address and alias"), "rename": _("Repository and its new alias"),
            "repositories": _("Repositories"), "services": _("Services"), "versions": _("Two versions"),
            "command": _("Command"), "patches": _("Patches"), "patterns": _("Patterns"), "products": _("Products"),
            }.get(kind, _("Arguments"))


def in_sentence(label):
    """A label inside a sentence: its first letter in lower case, except in German, where nouns keep theirs."""
    if user_lang().split("_")[0].split(".")[0] == "de":
        return label
    return label[:1].lower() + label[1:]


def means_text(cmd):
    """'What it means for you', for a command that changes the system."""
    text = {
        "packages": _("zypper installs, updates or removes packages as this command says. It cannot ask you "
                      "anything here (-n), so it takes its default answers and does not stop to ask."),
        "sources": _("zypper changes the repositories or services the computer gets its programs from. Nothing is "
                     "installed or removed now."),
        "refresh": _("zypper downloads its lists of packages again. Nothing installed changes."),
        "cache": _("zypper deletes files it keeps to work faster (lists of packages, downloaded packages). Nothing "
                   "installed changes: what is deleted is downloaded again when it is needed."),
        "locks": _("zypper changes which packages it may install, update or remove. Nothing is installed or removed "
                   "now."),
        "languages": _("zypper changes the languages the computer is set up for. Unless --no-packages is there, it "
                       "also installs or removes their translations."),
        "download": _("zypper only downloads packages. Nothing is installed."),
    }.get(cmd.effect, _("zypper changes the system as this command says, with its default answers (-n)."))
    if cmd.has_dry_run:
        text += " " + _("With --dry-run (Simulate) it only shows what it would do.")
    return text


def warnings_for(cmd, used):
    """Careful notes for a command and the options it carries."""
    longs = {o.long for o, _v in used}
    out = []
    if cmd.name == "dist-upgrade":
        out.append(_("A distribution upgrade can change hundreds of packages at once. The Updates page checks the "
                     "space and the problems first."))
    if cmd.name == "remove" and "--no-clean-deps" not in longs:
        out.append(_("Programs that need what you remove are removed with it, without asking (-n)."))
    notes = {
        "--allow-unsigned-rpm": _("Packages without a signature are installed without asking."),
        "--force-resolution": _("The solver may remove packages to find a solution, without asking."),
        "--no-gpgcheck": _("Nothing from this repository is checked against a signature."),
        "--gpgcheck-allow-unsigned": _("What comes from this repository is not fully checked against a signature."),
        "--gpgcheck-allow-unsigned-repo": _("The list of packages of this repository is not checked against a "
                                            "signature."),
        "--gpgcheck-allow-unsigned-package": _("The packages of this repository are not checked against a "
                                               "signature."),
        "--replacefiles": _("Files of other installed packages may be overwritten."),
        "--allow-vendor-change": _("Packages may change to another vendor (for example from Packman to openSUSE)."),
        "--remove-orphaned": _("Installed packages that no repository has any more are removed."),
        "--clean-deps": _("What only these packages needed is removed too."),
        "--force": _("Packages are installed again even when they are already installed, older, or from another "
                     "vendor or architecture."),
        "--all": _("It applies to all of them."),
    }
    for flag, text in notes.items():
        if flag in longs and (flag != "--all" or cmd.effect == "sources"):
            out.append(text)
    return " ".join(out) or None


class _TypedLine(QLineEdit):
    """The line to type a command, with the lines run before under ↑ and ↓, as in a terminal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = []
        self._pos = 0
        self._draft = ""

    def remember(self, text):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self._pos = len(self.history)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Up and self.history:
            if self._pos == len(self.history):
                self._draft = self.text()
            self._pos = max(0, self._pos - 1)
            self.setText(self.history[self._pos])
            return
        if ev.key() == Qt.Key.Key_Down and self.history:
            self._pos = min(len(self.history), self._pos + 1)
            self.setText(self.history[self._pos] if self._pos < len(self.history) else self._draft)
            return
        super().keyPressEvent(ev)


class _Fold(QToolButton):
    """'▸ Global options (-v, --no-refresh…)': a click shows what is under it, a second one hides it."""

    def __init__(self, text, target, parent=None):
        super().__init__(parent)
        self._text, self._target = text, target
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QToolButton { border: none; padding: 2px 0px; color: palette(placeholder-text); }"
                           "QToolButton:focus { background: rgba(29, 153, 243, 0.18); border-radius: 4px; }")
        self.toggled.connect(self._toggle)
        target.setVisible(False)
        self._toggle(False)

    def _toggle(self, on):
        self._target.setVisible(on)
        self.setText(("▾ " if on else "▸ ") + self._text)


def _plain(text, *, muted=False, small=False, wrap=True, font=None, color=None):
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)  # zypper's help has <TYPE>, names come from the network
    lbl.setWordWrap(wrap)
    if font is not None:
        lbl.setFont(font)
    if small:
        f = lbl.font()
        f.setPointSizeF(f.pointSizeF() * 0.92)
        lbl.setFont(f)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    elif muted:
        lbl.setStyleSheet("color: palette(placeholder-text);")
    return lbl


def _bold(text):
    lbl = _plain(text)
    f = lbl.font()
    f.setBold(True)
    lbl.setFont(f)
    return lbl


class ExpertPage(Page):
    key = "expert"
    title = _("Expert")
    icon_name = "utilities-terminal"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.catalog = None
        self.repos = []
        self.cmd = None
        self.parsed = None  # the form's command, checked (zyppcmds.Parsed), or None
        self.show_all = False
        self.arg_edit = self.repo_combo = None
        self.opt_widgets, self.glob_widgets = [], []

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        # ---------------------------------------------- the commands
        left = QFrame(self)
        left.setObjectName("card")
        left.setFixedWidth(236)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 10, 8, 10)
        self.list = QListWidget(left)
        self.list.setObjectName("expertcmds")
        self.list.setStyleSheet(LIST_STYLE)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self._picked)
        self.list.itemClicked.connect(self._clicked)
        self.list.itemActivated.connect(self._clicked)
        lv.addWidget(self.list)
        outer.addWidget(left)

        right = QVBoxLayout()
        right.setSpacing(12)
        outer.addLayout(right, 1)

        # ---------------------------------------------- "Or type it"
        tc = Card(parent=self)
        tc.lay.setSpacing(6)
        tc.lay.addWidget(_bold(_("Or type it:")))
        row = QHBoxLayout()
        self.line = _TypedLine(self)
        self.line.setFont(mono())
        self.line.setStyleSheet(f"QLineEdit {{ {DARK_STYLE} }}")
        self.line.setPlaceholderText("zypper se vlc")
        self.line.setClearButtonEnabled(True)
        self.line.returnPressed.connect(self.run_typed)  # a change still asks first, and there Enter is "Not yet"
        self.line.textEdited.connect(lambda _t: self._line_note())
        row.addWidget(self.line, 1)
        self.line_run = button(_("Run"), "system-run", self.run_typed, primary=True)
        self.line_run.setAutoDefault(False)
        row.addWidget(self.line_run)
        tc.lay.addLayout(row)
        self.line_info = _plain("", muted=True, small=True)
        tc.lay.addWidget(self.line_info)
        self._line_note()
        right.addWidget(tc)

        # ---------------------------------------------- the form of the command
        fc = Card(parent=self)
        fc.lay.setContentsMargins(4, 6, 4, 6)
        self.form_scroll = QScrollArea(fc)
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.form_scroll.setMinimumHeight(60)  # a low window with the terminal open: the buttons stay in sight
        self.form_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        fc.lay.addWidget(self.form_scroll, 1)
        right.addWidget(fc, 1)
        self._set_form(self._message_form(_("Reading zypper's help…")))

        # ---------------------------------------------- the command it will use
        cc = Card(parent=self)
        cc.lay.setSpacing(6)
        cc.lay.addWidget(_plain(_("The command it will use:"), muted=True))
        self.cmd_view = QLabel(self)
        self.cmd_view.setTextFormat(Qt.TextFormat.PlainText)
        self.cmd_view.setWordWrap(True)
        self.cmd_view.setFont(mono())
        self.cmd_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cmd_view.setStyleSheet(f"QLabel {{ {DARK_STYLE} }}")
        self.cmd_view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        cc.lay.addWidget(self.cmd_view)
        self.cmd_info = _plain("", muted=True, small=True)
        cc.lay.addWidget(self.cmd_info)
        brow = QHBoxLayout()
        self.b_copy = button(_("Copy it"), "edit-copy", self.copy_command)
        self.copied = _plain("", muted=True, small=True, wrap=False)
        self.b_sim = button(_("Simulate"), "media-playback-start", self.simulate)
        self.b_queue = button(_("Put in the queue"), "list-add", self.put_in_queue)
        self.b_run = button(_("Run…"), "system-run", self.run_form, primary=True)
        for b in (self.b_copy, self.b_sim, self.b_queue, self.b_run):
            b.setAutoDefault(False)
        brow.addWidget(self.b_copy)
        brow.addWidget(self.copied)
        brow.addStretch(1)
        brow.addWidget(self.b_sim)
        brow.addWidget(self.b_queue)
        brow.addWidget(self.b_run)
        cc.lay.addLayout(brow)
        right.addWidget(cc)
        self._refresh_command()

    # ------------------------------------------------------------------ loading
    def reload(self):
        """The first time, zypper's commands (from the cache, or zypper's help); after a change (after_change), only
        the repositories: what is written on the form stays, unless zypper itself was updated."""
        if self.catalog is None:
            self.list.clear()
            it = QListWidgetItem(_("Reading zypper's help…"))
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        run_async(zyppcmds.load, on_done=self._loaded)
        run_async(backend.repos, on_done=self._repos_loaded)

    def _loaded(self, catalog, err):
        if err is not None or catalog is None:
            if self.catalog is not None:
                return  # keep the commands already shown
            self.list.clear()
            self._set_form(self._message_form(_("zypper's help could not be read: {error}").format(
                error=err or "?"), color=RED))
            return
        if self.catalog is not None and catalog.version == self.catalog.version:
            return
        name = self.cmd.name if self.cmd else "install"
        self.cmd = None  # the form is made again with the new zypper's options
        self.catalog = catalog
        self._fill_list()
        self.select(name)
        self._line_note()

    def _repos_loaded(self, repos, err):
        if err is not None or not repos:
            return
        self.repos = sorted((r for r in repos if r.get("enabled")),
                            key=lambda r: (r.get("priority", 99), (r.get("name") or r["alias"]).lower()))
        if self.repo_combo is not None:
            keep = self.repo_combo.currentData()
            self._fill_repos(self.repo_combo, keep)

    # ------------------------------------------------------------------ the list
    def _fill_list(self):
        self.list.blockSignals(True)
        current = self.cmd.name if self.cmd else None
        self.list.clear()
        groups = self.catalog.grouped()
        total = sum(len(cmds) for _k, cmds in groups)
        for key, cmds in groups:
            shown = [c for c in cmds if self.show_all or c.common or c.name == current]
            if not shown:
                continue
            hdr = QListWidgetItem(group_title(key).upper())
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            f = hdr.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() * 0.85)
            hdr.setFont(f)
            hdr.setForeground(self.list.palette().brush(QPalette.ColorRole.Text))  # the text colour, not greyed out
            self.list.addItem(hdr)
            for c in shown:
                it = QListWidgetItem(c.title)
                it.setData(Qt.ItemDataRole.UserRole, c.name)
                tip = c.summary or c.name
                if c.mode == "none":
                    tip += "\n" + zyppcmds.reason_text(c)
                it.setToolTip(tip)
                self.list.addItem(it)
        tog = QListWidgetItem(_("▾ Fewer commands") if self.show_all else
                              ngettext("▸ All ({n})", "▸ All ({n})", total).format(n=total))
        tog.setData(Qt.ItemDataRole.UserRole, TOGGLE)
        tog.setFlags(Qt.ItemFlag.ItemIsEnabled)
        tog.setToolTip(_("Every zypper command") if not self.show_all else "")
        self.list.addItem(tog)
        self.list.blockSignals(False)
        if current:
            self._select_row(current)

    def _select_row(self, name):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == name:
                self.list.blockSignals(True)
                self.list.setCurrentItem(it)
                self.list.blockSignals(False)
                return True
        return False

    def _clicked(self, item):
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == TOGGLE:
            self.show_all = not self.show_all
            self._fill_list()

    def _picked(self, cur, _prev=None):
        name = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        if name and name != TOGGLE:
            self.select(name)

    def select(self, name):
        """Show the form of a command (its name or an alias)."""
        if self.catalog is None:
            return False
        cmd = self.catalog.find(name)
        if cmd is None:
            return False
        if not self._select_row(cmd.name):
            self.show_all = True  # a command that is not among the common ones
            self._fill_list()
            self._select_row(cmd.name)
        if cmd is not self.cmd:
            self.cmd = cmd
            self._set_form(self._build_form(cmd))
        self._refresh_command()
        return True

    # ------------------------------------------------------------------ the form
    def _set_form(self, w):
        self.form_scroll.setWidget(w)  # the old form is deleted with it

    def _message_form(self, text, color=None):
        self.cmd = None
        self.arg_edit = self.repo_combo = None
        self.opt_widgets, self.glob_widgets = [], []
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 8, 12, 8)
        v.addWidget(_plain(text, muted=color is None, color=color))
        v.addStretch(1)
        return w

    def _build_form(self, cmd):
        self.arg_edit = self.repo_combo = None
        self.opt_widgets, self.glob_widgets = [], []
        w = QWidget()
        w.setObjectName("expertform")
        w.setStyleSheet("QWidget#expertform { background: transparent; }")
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 6, 12, 10)
        v.setSpacing(6)

        title = heading(cmd.name, 1.35)
        title.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(title)
        summary = cmd.summary or (_("A zypper subcommand (a program of its own).") if cmd.external else "")
        if summary:
            v.addWidget(_plain(summary))
        for u in cmd.usage:
            v.addWidget(_plain(u, muted=True, small=True, font=mono()))
        if cmd.description and cmd.description.rstrip(".") != cmd.summary.rstrip("."):
            v.addWidget(_plain(cmd.description, muted=True, small=True))
        if cmd.mode == "user":
            v.addWidget(_plain("● " + _("It only reads: it runs as you, without the password."), small=True,
                               color=GREEN))
        elif cmd.mode == "root":
            v.addWidget(_plain("● " + _("It changes the system: it asks you first, then the administrator password."),
                               small=True, color=ORANGE))
        else:
            v.addWidget(question.box("warn", zyppcmds.reason_text(cmd)))
        if cmd.external:
            v.addWidget(_plain(_("It is a program of its own: its options are in its manual (man zypper-{name}). "
                                 "Write them in the field below.").format(name=cmd.name), muted=True, small=True))

        # arguments
        if cmd.args:
            v.addSpacing(4)
            v.addWidget(_bold(arg_label(cmd.args_kind)))
            self.arg_edit = QLineEdit()
            self.arg_edit.setFont(mono())
            self.arg_edit.setPlaceholderText(cmd.args)
            self.arg_edit.setClearButtonEnabled(True)
            self.arg_edit.textChanged.connect(self._refresh_command)
            v.addWidget(self.arg_edit)
            hint = cmd.args_help or (_("Several are separated by spaces; quote a name that has spaces.")
                                     if cmd.args.rstrip().endswith("...") else "")
            if hint:
                v.addWidget(_plain(hint, muted=True, small=True))

        # from the repository
        ro = cmd.repo_option()
        if ro is not None:
            v.addSpacing(4)
            v.addWidget(_bold(_("From the repository")))
            self.repo_combo = QComboBox()
            self.repo_combo.setToolTip(f"{ro.label} {ro.value}: {ro.help}")
            self._fill_repos(self.repo_combo, None)
            self.repo_combo.currentIndexChanged.connect(self._refresh_command)
            v.addWidget(self.repo_combo)
            v.addWidget(_plain(f"{ro.cli} — {ro.help}", muted=True, small=True))

        # the command's options, by zypper's sections: the first one open, the others folded. The ones people use
        # most go first (zypper lists --no-recommends among the solver options)
        first = [o for f in PROMOTED if (o := cmd.option(f)) is not None and o is not ro]
        sections = [("Command options", list(first))] if first else []
        for o in cmd.options:
            if o is ro or any(o is x for x in first):
                continue
            if not sections or sections[-1][0] != o.section:
                sections.append((o.section, []))
            sections[-1][1].append(o)
        for i, (name, opts) in enumerate(sections):
            v.addSpacing(4)
            box = QWidget()
            bv = QVBoxLayout(box)
            bv.setContentsMargins(0, 0, 0, 0)
            bv.setSpacing(4)
            for o in opts:
                bv.addWidget(self._option_row(o, self.opt_widgets))
            if i == 0 and name == "Command options":
                v.addWidget(_bold(section_title(name)))
                v.addWidget(box)
            else:
                v.addWidget(_Fold(ngettext("{section} ({n})", "{section} ({n})", len(opts)).format(
                    section=section_title(name), n=len(opts)), box))
                v.addWidget(box)

        # global options, folded
        if not cmd.external and cmd.mode != "none" and self.catalog is not None:
            box = QWidget()
            bv = QVBoxLayout(box)
            bv.setContentsMargins(0, 0, 0, 0)
            bv.setSpacing(4)
            for o in self.catalog.globals:
                if o.meta or o.forbidden:
                    continue
                if o.long == "--non-interactive":
                    bv.addWidget(self._option_row(o, self.glob_widgets, checked=True, enabled=False, extra=_(
                        "Always on here: nobody can answer zypper's questions, so it takes its default answers.")))
                elif cmd.mode == "root" and not o.helper_allows:
                    bv.addWidget(self._option_row(o, self.glob_widgets, enabled=False, extra=_(
                        "Not with a command that changes the system: the helper that runs zypper as administrator "
                        "refuses it.")))
                else:
                    on = cmd.mode == "root" and o.long == "--verbose"  # as the rest of Zypp Desk runs changes
                    bv.addWidget(self._option_row(o, self.glob_widgets, checked=on))
            bv.addWidget(_plain(_("Not offered: the options that point zypper at another root, configuration, cache "
                                  "or repository folder, or that add repositories or trust keys on their own "
                                  "(--root, --config, --plus-repo, --gpg-auto-import-keys…)."), muted=True,
                                small=True))
            v.addSpacing(4)
            v.addWidget(_Fold(_("Global options (-v, --no-refresh…)"), box))
            v.addWidget(box)
        v.addStretch(1)
        return w

    def _option_row(self, o, into, checked=False, enabled=True, extra=""):
        row = QWidget()
        rv = QVBoxLayout(row)
        rv.setContentsMargins(0, 0, 0, 2)
        rv.setSpacing(0)
        if o.takes_value:
            h = QHBoxLayout()
            h.setSpacing(8)
            h.addWidget(_plain(f"{o.label} {o.value}".strip(), wrap=False, font=mono()))
            if o.choices:
                field = QComboBox()
                field.setEditable(True)
                field.addItem("")
                field.addItems(o.choices)
                field.lineEdit().setPlaceholderText(o.value)
                field.currentTextChanged.connect(self._refresh_command)
            else:
                field = QLineEdit()
                field.setPlaceholderText(o.value)
                field.textChanged.connect(self._refresh_command)
            field.setMinimumWidth(160)
            field.setMaximumWidth(340)
            h.addWidget(field, 1)
            h.addStretch(0)
            rv.addLayout(h)
            indent = 0
        else:
            field = QCheckBox(o.label)
            field.setFont(mono())
            field.setChecked(checked)
            field.toggled.connect(self._refresh_command)
            rv.addWidget(field)
            indent = 24
        field.setEnabled(enabled)
        text = o.help + (("\n" + extra) if extra else "")
        if text.strip():
            hl = _plain(text.strip(), muted=True, small=True)
            hl.setContentsMargins(indent, 0, 0, 0)
            rv.addWidget(hl)
        if extra:
            field.setToolTip(extra)
        into.append((o, field))
        return row

    def _fill_repos(self, combo, keep):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(_("(any)"), None)
        for r in self.repos:
            name = r.get("name") or r["alias"]
            combo.addItem(name if name == r["alias"] else f"{name} ({r['alias']})", r["alias"])
        if keep:
            i = combo.findData(keep)
            combo.setCurrentIndex(max(0, i))
        combo.blockSignals(False)

    # ------------------------------------------------------------------ the command of the form
    @staticmethod
    def _value(field):
        if isinstance(field, QCheckBox):
            return True if field.isChecked() else None
        text = field.currentText() if isinstance(field, QComboBox) else field.text()
        text = text.strip()
        return text or None

    def form_state(self):
        """(globals on, options on, arguments, repository) of the form. Raises ValueError for arguments that cannot
        be read (a quote not closed)."""
        globs = [(o, val) for o, f in self.glob_widgets if f.isEnabled() and (val := self._value(f)) is not None]
        opts = [(o, val) for o, f in self.opt_widgets if (val := self._value(f)) is not None]
        args = []
        if self.arg_edit is not None and self.arg_edit.text().strip():
            args = [os.path.expanduser(a) if a.startswith("~/") else a for a in shlex.split(self.arg_edit.text())]
        repo = self.repo_combo.currentData() if self.repo_combo is not None else None
        return globs, opts, args, repo

    def form_args(self, dry_run=False):
        """zypper's arguments for the form, or raises LineError / ValueError."""
        globs, opts, args, repo = self.form_state()
        return zyppcmds.compose(self.cmd, globs, opts, args, repo, dry_run=dry_run)

    def _refresh_command(self, *_a):
        cmd = self.cmd
        self.copied.setText("")
        self.parsed = None
        problem = None
        if cmd is None:
            self.cmd_view.setText("zypper …")
        elif cmd.mode == "none":  # the form says why, in a box under the title
            self.cmd_view.setText("zypper " + cmd.name)
        else:
            try:
                args = self.form_args()
                self.parsed = zyppcmds.analyse(args, self.catalog, add_n=False)
            except ValueError as e:  # LineError too
                problem = str(e)
                self.cmd_view.setText("zypper " + cmd.name + " …")
            else:
                self.cmd_view.setText(self._step(self.parsed).display())
        missing = self.parsed is not None and self._missing_arguments()
        if problem:
            self.cmd_info.setStyleSheet(f"color: {RED};")
            self.cmd_info.setText(problem)
        elif missing:  # zypper would only say "too few arguments" (after the password, for a change)
            self.cmd_info.setStyleSheet(f"color: {ORANGE};")
            self.cmd_info.setText(_("Write first what it works on ({what}).").format(
                what=in_sentence(arg_label(cmd.args_kind))))
        else:
            self.cmd_info.setStyleSheet("color: palette(placeholder-text);")
            self.cmd_info.setText(self._command_note(cmd))
        ok = self.parsed is not None and cmd is not None and cmd.mode != "none" and not missing
        self.b_copy.setEnabled(self.parsed is not None)
        self.b_run.setEnabled(ok)
        self.b_run.setText(_("Run…") if cmd is None or cmd.mode != "user" else _("Run"))
        self.b_run.setToolTip(zyppcmds.reason_text(cmd) if cmd is not None and cmd.mode == "none" else "")
        self.b_sim.setVisible(cmd is not None and cmd.has_dry_run)
        self.b_sim.setEnabled(ok)
        plan, why = self.queue_plan() if ok else (None, None)
        self.b_queue.setEnabled(bool(plan))
        self.b_queue.setToolTip("" if plan else (why or ""))

    def _missing_arguments(self):
        """zypper's usage asks for an argument (<…>) and the field is empty, and no option takes its place ('--all',
        a .repo file with 'addrepo --repo')."""
        cmd = self.cmd
        if cmd is None or not cmd.args_required or cmd.external:
            return False
        used, plain = zyppcmds.split_rest(cmd, self.parsed.rest)
        if plain:
            return False
        instead = {"--all", "--local", "--remote", "--medium-type"} | ({"--repo"} if cmd.name == "addrepo" else set())
        return not any(o.long in instead for o, _v in used)

    @staticmethod
    def _command_note(cmd):
        if cmd is None:
            return ""
        if cmd.mode == "none":
            return _("It cannot run from here.")
        if cmd.external:
            return _("It runs as you, as it is: a program of its own takes no global options.")
        if cmd.mode == "user":
            return _("-n is always added: zypper cannot ask you anything here. It runs as you, without the password.")
        return _("-n is always added: zypper cannot ask you anything here, so it takes its default answers. It runs "
                 "as administrator, after a question.")

    @staticmethod
    def _step(parsed):
        return Step("zypper", parsed.args, root=parsed.root)

    # ------------------------------------------------------------------ the queue
    def queue_plan(self):
        """The queue's changes for the form: ([Action], None), or (None, why it cannot go to the queue)."""
        cmd = self.cmd
        if cmd is None or not cmd.queue_kind:
            return None, _("The queue carries installs, removals, locks, languages and repository changes. Use "
                           "“Run…” for this one.")
        try:
            globs, opts, args, repo = self.form_state()
        except ValueError as e:
            return None, str(e)
        extra = [o.label for o, _v in globs if not (set(o.flags) & QUEUE_IGNORES_GLOBALS)]
        if extra:
            return None, _("The queue runs its own commands: these global options do not go with it: {opts}. Use "
                           "“Run…”.").format(opts=", ".join(extra))
        carried = QUEUE_CARRIES[cmd.name]
        names = {}
        for o, val in opts:
            if (o.long or o.short) not in carried:
                return None, _("The queue cannot carry {option}. Use “Run…”, or leave it out.").format(
                    option=o.cli)
            names[o.long or o.short] = val
        for a, b in OPPOSITES:
            if a in names and b in names:
                return None, _("{a} and {b} go against each other.").format(a=a, b=b)
        if not args:
            return None, _("Write first what it works on ({what}).").format(what=in_sentence(arg_label(cmd.args_kind)))
        k = cmd.queue_kind
        typ = names.get("--type") or "package"
        where = repo or names.get("--from") or names.get("--repo") or ""
        if k in ("install", "remove"):
            out = []
            for n in args:
                if is_file_target(n):
                    return None, _("A downloaded file goes to the queue from the file itself (open it with Zypp "
                                   "Desk), which checks it first. Use “Run…” to install it as it is.")
                t = n if typ == "package" else f"{typ}:{n}"
                if k == "install":
                    if where and typ != "package":
                        return None, _("The queue installs only packages from one repository. Use “Run…”.")
                    if where:
                        t = f"{where}:{t}"
                    out.append(Action("install", t, oldpackage=bool(names.get("--oldpackage"))))
                else:
                    out.append(Action("remove", t, **({"clean_deps": True} if names.get("--clean-deps") else {})))
            return out, None
        if k in ("lock", "unlock"):
            return [Action(k, n, type=typ, repo=names.get("--repo") or "") for n in args], None
        if k in ("locale-add", "locale-remove"):
            return [Action(k, n) for n in args], None
        if k == "repo-remove":
            return [Action(k, n) for n in args], None
        prio = names.get("--priority")
        if prio is not None:
            if not (str(prio).isascii() and str(prio).isdigit() and int(prio) >= 1):
                return None, _("The priority must be a whole number, 1 or more.")
            prio = int(prio)
        if k == "repo-add":
            if len(args) != 2:
                return None, _("The queue adds a repository from its address and an alias (two words). A .repo file "
                               "goes through “Run…” or the Repositories page.")
            url, alias = args
            o = dict(url=url, name=names.get("--name") or alias, autorefresh=bool(names.get("--refresh")))
            if names.get("--disable"):
                o["enabled"] = False
            if names.get("--keep-packages"):
                o["keeppackages"] = True
            if prio is not None:
                o["priority"] = prio
            gpg = ("off" if names.get("--no-gpgcheck") else
                   "allow-unsigned-repo" if names.get("--gpgcheck-allow-unsigned-repo") else
                   "allow-unsigned-package" if names.get("--gpgcheck-allow-unsigned-package") else "default")
            o["gpg"] = gpg
            return [Action("repo-add", alias, **o)], None
        # repo-modify
        o = {}
        if names.get("--enable"):
            o["enabled"] = True
        if names.get("--disable"):
            o["enabled"] = False
        if names.get("--refresh") or names.get("-r"):
            o["autorefresh"] = True
        if names.get("--no-refresh") or names.get("-R"):
            o["autorefresh"] = False
        if names.get("--keep-packages"):
            o["keeppackages"] = True
        if names.get("--no-keep-packages"):
            o["keeppackages"] = False
        if prio is not None:
            o["priority"] = prio
        if names.get("--name"):
            o["name"] = names["--name"]
        if not o:
            return None, _("Choose first what to change.")
        return [Action("repo-modify", n, **dict(o)) for n in args], None

    def put_in_queue(self):
        plan, why = self.queue_plan()
        if not plan:
            self.copied.setText(why or "")
            return
        with self.ctx.queue.batch():
            for a in plan:
                self.ctx.queue.add(a)
        self.copied.setText(ngettext("{n} change in the queue.", "{n} changes in the queue.", len(plan)).format(
            n=len(plan)))

    # ------------------------------------------------------------------ copy, simulate, run
    def copy_command(self):
        if self.parsed is not None:
            QGuiApplication.clipboard().setText(self._step(self.parsed).display())
            self.copied.setText(_("Copied."))

    def simulate(self):
        if self.cmd is None or not self.cmd.has_dry_run:
            return
        try:
            parsed = zyppcmds.analyse(self.form_args(dry_run=True), self.catalog, add_n=False)
        except ValueError as e:
            self.cmd_info.setText(str(e))
            return
        self._run(parsed, _("Simulate: zypper {command}").format(command=self.cmd.name))

    def run_form(self):
        if self.parsed is None or self.cmd is None or self.cmd.mode == "none":
            return
        self._run(self.parsed, _("zypper {command}").format(command=self.cmd.name))

    def run_typed(self):
        text = self.line.text()
        if self.catalog is None:
            return
        try:
            parsed = zyppcmds.parse_line(text, self.catalog)
        except zyppcmds.LineError as e:
            self._line_note(str(e), error=True)
            return
        self.line.remember(text.strip())
        self._line_note(" ".join(parsed.notes))
        name = parsed.command.name if parsed.command is not None else ""
        self._run(parsed, _("zypper {command}").format(command=name).strip())

    def _line_note(self, text="", error=False):
        if not text:
            text = _("All of zypper, with the output in the terminal below. Commands that change the system go "
                     "through the same question and the password; the options that change zypper's root or its "
                     "configuration are not accepted (--root, --config…).")
        self.line_info.setStyleSheet(f"color: {RED};" if error else "color: palette(placeholder-text);")
        self.line_info.setText(text)

    def _run(self, parsed, title):
        """Read-only commands and simulations at once; a change after the question. Always through ctx.run, so the
        output goes to the terminal panel."""
        step = self._step(parsed)
        change = parsed.root and not parsed.dry_run
        if change and not self._ask(parsed, step):
            return False
        on_done = (lambda ok: self._changed()) if change else None
        return self.ctx.run([step], title, on_done=on_done)

    def _changed(self):
        after = getattr(self.ctx, "after_change", None)
        if after is not None:
            after()

    def _ask(self, parsed, step):
        """The question before a change: what it is in zypper's words, what it means, careful notes, the exact
        command and the password. 'Not yet' is the default: Enter never starts it."""
        cmd = parsed.command
        used, plain = zyppcmds.split_rest(cmd, parsed.rest)
        what = cmd.summary or cmd.name
        detail = cmd.description.split("\n\n")[0] if cmd.description else ""
        if detail.rstrip(".") == what.rstrip("."):
            detail = ""
        items = [(EFFECT_ICONS.get(cmd.effect, "utilities-terminal"), what, detail)]
        if plain:
            shown = " ".join(plain[:12]) + (" …" if len(plain) > 12 else "")
            items.append(("document-edit|edit-entry", _("On: {names}").format(names=shown), "",
                          plain if len(plain) > 12 else None))
        return question.confirm(
            self, _("Run “zypper {command}”?").format(command=cmd.name), _("Yes, run it"),
            window_title=_("Expert"), icon_name="utilities-terminal", items=items, means=means_text(cmd),
            help_topic="expert",
            warning=warnings_for(cmd, used), commands_=[step.display()])

    def header_actions(self):
        return []
