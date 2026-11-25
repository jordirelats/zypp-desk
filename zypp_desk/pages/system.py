# SPDX-License-Identifier: GPL-3.0-or-later
"""Locks, services, languages, maintenance and settings (History is in history.py)."""

import html
import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QScrollArea, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import APP_ID, backend
from ..i18n import _
from ..runner import Step, zypper_subcommand
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Card, Page, ask, button, error_box, heading, mono, muted, tool


def kind_label(kind):
    return {"package": _("package"), "pattern": _("pattern"), "patch": _("patch"), "product": _("product"),
            "srcpackage": _("source package")}.get(kind, kind)


def simple_tree(headers, parent):
    t = QTreeWidget(parent)
    t.setHeaderLabels(headers)
    t.setRootIsDecorated(False)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(True)
    return t


# ============================================================ locks
class LocksPage(Page):
    key = "locks"
    title = _("Locks")
    icon_name = "object-locked"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Name"), _("Type"), _("Repository"), _("Pending")], self)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("A locked package is never installed, removed or updated by any zypper command "
                              "until you unlock it.")))
        lay.addWidget(self.tree, 1)
        self.items = []
        ctx.queue.changed.connect(self._fill)
        # 'zypper removelock' ends with 0 also when it found no such lock: that is a failure here
        runner = getattr(ctx, "runner", None)
        if runner is not None and hasattr(runner, "step_finished"):
            runner.step_finished.connect(self._step_finished)

    def header_actions(self):
        return [tool(_("Lock…"), "list-add", self.add), tool(_("Unlock"), "list-remove", self.remove),
                tool(_("Clean useless locks"), "edit-clear",
                     lambda: self.ctx.run([Step("zypper", ["cleanlocks"])], _("Clean useless locks"),
                                          on_done=lambda ok: self.ctx.after_change()))]

    def reload(self):
        run_async(backend.locks, on_done=self._loaded)

    def _loaded(self, items, err):
        if err:
            error_box(self, self.title, str(err))
            return
        self.items = items
        self._fill()

    @staticmethod
    def _same(action, lock):
        """True if a queued unlock is for this lock (the same text, kind and repository)."""
        def norm(text):
            return "".join(text.split())
        return (norm(action.target) == norm(lock["spec"]) and action.opts.get("type", "package") == lock["type"]
                and action.opts.get("repo", "") == lock["repo"])

    def _fill(self):
        self.tree.clear()
        specs = set()
        for lk in self.items:
            specs.add((lk["spec"], lk["type"], lk["repo"]))
            pend = _("will be unlocked") if any(self._same(a, lk) for a in self.ctx.queue.of("unlock")) else ""
            # the name with its version range: "tool < 2.0" (zypper finds the lock again only with it)
            it = QTreeWidgetItem(self.tree, [lk["spec"], kind_label(lk["type"]), lk["repo"], pend])
            it.setData(0, Qt.ItemDataRole.UserRole, lk)
            if lk["range"]:
                it.setToolTip(0, _("Locked only in these versions: {range}").format(range=lk["range"]))
        for a in self.ctx.queue.of("lock"):
            if (a.target, a.opts.get("type", "package"), a.opts.get("repo", "")) not in specs:
                QTreeWidgetItem(self.tree, [a.target, kind_label(a.opts.get("type", "package")),
                                            a.opts.get("repo", ""), _("will be locked")])
        self.tree.resizeColumnToContents(0)

    def add(self):
        name, ok = QInputDialog.getText(self, _("Lock"), _("Name of the package (wildcards like vlc-* work):"))
        if ok and name.strip():
            self.ctx.queue.add(Action("lock", name.strip()))

    def remove(self):
        it = self.tree.currentItem()
        lk = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        if lk:
            # zypper finds a lock only as it is written: with its version range ("tool < 2.0"), its kind (-t)
            # and, for a lock limited to a repository, that repository (-r)
            self.ctx.queue.add(Action("unlock", lk["spec"], type=lk["type"] or "package", repo=lk["repo"] or ""))

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


# ============================================================ services
class ServicesPage(Page):
    key = "services"
    title = _("Services")
    icon_name = "network-server"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Name"), _("Enabled"), _("Auto-refresh"), _("Type"), _("Repositories"),
                                 _("Address")], self)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("A service is a list of repositories kept up to date by its provider (openSUSE's "
                              "own repositories come from one).")))
        lay.addWidget(self.tree, 1)

    def header_actions(self):
        return [tool(_("Refresh services"), "view-refresh",
                     lambda: self.ctx.run([Step("zypper", ["-n", "-v", "refresh-services"])], _("Refresh services"),
                                          on_done=lambda ok: self.ctx.after_change())),
                tool(_("Enable / disable"), "system-run", self.toggle)]

    def reload(self):
        run_async(backend.services, on_done=self._loaded)

    def _loaded(self, items, err):
        if err:
            error_box(self, self.title, str(err))
            return
        self.tree.clear()
        for s in items:
            it = QTreeWidgetItem(self.tree, [s["name"], _("yes") if s["enabled"] else _("no"),
                                             _("yes") if s["autorefresh"] else _("no"), s["type"],
                                             str(len(s["repos"])), s["url"]])
            it.setData(0, Qt.ItemDataRole.UserRole, s)
        for c in range(5):
            self.tree.resizeColumnToContents(c)

    def toggle(self):
        it = self.tree.currentItem()
        if not it:
            return
        s = it.data(0, Qt.ItemDataRole.UserRole)
        flag = "--disable" if s["enabled"] else "--enable"
        st = Step("zypper", ["modifyservice", flag, "--", s["alias"]])  # '--': an alias is never read as an option
        if ask(self, self.title, _("Run '{command}'?").format(command=st.display()), default_no=True):
            self.ctx.run([st], _("Change a service"), on_done=lambda ok: self.ctx.after_change())


# ============================================================ languages
class LocalesPage(Page):
    key = "locales"
    title = _("Languages")
    icon_name = "preferences-desktop-locale"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.tree = simple_tree([_("Code"), _("Language"), _("Pending")], self)
        lay = QVBoxLayout(self)
        lay.addWidget(muted(_("Requested languages make zypper install the translation packages (-lang) of the "
                              "programs you have.")))
        lay.addWidget(self.tree, 1)
        self.items = []
        ctx.queue.changed.connect(self._fill)

    def header_actions(self):
        return [tool(_("Add…"), "list-add", self.add), tool(_("Remove"), "list-remove", self.remove)]

    def reload(self):
        run_async(backend.locales, on_done=self._loaded)

    def _loaded(self, items, err):
        if not err:
            self.items = items
            self._fill()

    def _fill(self):
        self.tree.clear()
        for lc in self.items:
            pend = _("will be removed") if any(a.target == lc["code"] for a in self.ctx.queue.of("locale-remove")) else ""
            QTreeWidgetItem(self.tree, [lc["code"], lc["name"], pend])
        for a in self.ctx.queue.of("locale-add"):
            QTreeWidgetItem(self.tree, [a.target, "", _("will be added")])
        if not self.items and not self.ctx.queue.of("locale-add"):
            empty = QTreeWidgetItem(self.tree, [_("No language has been requested.")])
            empty.setFirstColumnSpanned(True)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)

    def add(self):
        code, ok = QInputDialog.getText(self, _("Add a language"), _("Language code, for example ca or ca_ES:"))
        if ok and code.strip():
            self.ctx.queue.add(Action("locale-add", code.strip()))

    def remove(self):
        it = self.tree.currentItem()
        if it and it.text(0):
            self.ctx.queue.add(Action("locale-remove", it.text(0)))


# ============================================================ maintenance
MAINTENANCE = [
    ("verify", _("Check dependencies"),
     _("Looks for installed packages whose dependencies are broken and proposes how to fix them."),
     ["verify", "--dry-run"], ["-n", "verify"]),
    ("inr", _("Install new recommended packages"),
     _("Installs packages recommended by what you have, added since it was installed."),
     ["install-new-recommends", "--dry-run"], ["-n", "install-new-recommends"]),
    ("purge", _("Remove old kernels"),
     _("Keeps the kernels set in zypp.conf (multiversion.kernels; on Tumbleweed /usr/etc/zypp/zypp.conf and "
       "/etc/zypp/zypp.conf.d) and removes the rest."),
     ["purge-kernels", "--dry-run"], ["-n", "purge-kernels"]),
    ("clean", _("Clean the cache"),
     _("Deletes downloaded packages and cached metadata. They are downloaded again when needed."),
     None, ["clean", "--all"]),
    ("cleanlocks", _("Clean useless locks"),
     _("Removes locks that no longer match any package."), None, ["cleanlocks"]),
    ("ps", _("Programs using deleted files"),
     _("Programs that still use libraries replaced by an update and should be restarted."), None, ["ps", "-s"]),
    ("reboot", _("Is a restart needed?"),
     _("Asks zypper whether an update requires restarting the computer."), None, ["needs-rebooting"]),
    ("licenses", _("Licenses report"),
     _("Lists the licenses of the installed packages."), None, ["licenses"]),
]


class MaintenancePage(Page):
    key = "maintenance"
    title = _("Maintenance")
    icon_name = "tools"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        inner = QWidget(self)
        grid = QGridLayout(inner)
        for i, (key, title, desc, sim, run) in enumerate(MAINTENANCE):
            card = Card(title, inner)
            card.lay.addWidget(muted(desc))
            cmd = muted("zypper " + " ".join(run))
            cmd.setStyleSheet("color: palette(link); font-family: monospace;")
            card.lay.addWidget(cmd)
            row = QHBoxLayout()
            if sim:
                row.addWidget(button(_("Simulate"), "media-playback-start",
                                     lambda _c=False, a=sim, t=title: self.ctx.run([Step("zypper", ["-n", "-v", *a])], t)))
            row.addWidget(button(_("Run"), "system-run",
                                 lambda _c=False, a=run, t=title, s=sim: self._run(a, t, s)))
            row.addStretch(1)
            card.lay.addLayout(row)
            grid.addWidget(card, i // 2, i % 2)
        grid.setRowStretch(len(MAINTENANCE) // 2 + 1, 1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)

    def _run(self, args, title, sim):
        if sim and not ask(self, title, _("Run it for real? Use Simulate first to see what it would do."),
                                    default_no=True):
            return
        # the page in front, the others and the tray look again afterwards (old kernels gone, new packages…)
        self.ctx.run([Step("zypper", ["-v", *(["-n"] if args[0] != "-n" else []), *args])], title,
                     on_done=lambda ok: self.ctx.after_change())


# ============================================================ settings
RPM_MIME = "application/x-rpm"
DESKTOP_ID = f"{APP_ID}.desktop"


def _desktop_file(desktop_id):
    """The menu entry with this id in the folders where the desktop looks for them, or None."""
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    dirs = [home] + [d for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    for d in dirs:
        f = Path(d) / "applications" / desktop_id
        if f.is_file():
            return f
    return None


def _desktop_name(desktop_id):
    """The name a menu entry shows, in the user's language when it has one ('Ark'), or the id itself."""
    f = _desktop_file(desktop_id)
    if f is None:
        return desktop_id
    lang = (os.environ.get("LANGUAGE") or os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
            or os.environ.get("LANG") or "").split(":")[0].split(".")[0]
    wanted = [f"Name[{lang}]", f"Name[{lang.split('_')[0]}]", "Name"] if lang else ["Name"]
    found, section = {}, None
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
                continue
            if section == "[Desktop Entry]" and "=" in line:
                k, v = line.split("=", 1)
                found.setdefault(k.strip(), v.strip())
    except OSError:
        return desktop_id
    return next((found[k] for k in wanted if found.get(k)), desktop_id)


def rpm_opener():
    """Which program opens .rpm files now (xdg-mime, as the user): dict(available, current, name, ours)."""
    xdg = shutil.which("xdg-mime")
    current = ""
    if xdg:
        p = subprocess.run([xdg, "query", "default", RPM_MIME], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        current = (p.stdout.strip().splitlines() or [""])[0].strip()
    return dict(available=bool(xdg), current=current, name=_desktop_name(current) if current else "",
                ours=_desktop_file(DESKTOP_ID) is not None)


def set_rpm_opener():
    """Make Zypp Desk open .rpm files, for this user only (xdg-mime writes ~/.config/mimeapps.list)."""
    p = subprocess.run(["xdg-mime", "default", DESKTOP_ID, RPM_MIME], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or _("xdg-mime ended with exit code {code}.").format(
            code=p.returncode))
    return rpm_opener()


class RpmFilesCard(Card):
    """Which program opens an .rpm file you double-click (Ark, until you choose Zypp Desk here)."""

    def __init__(self, parent=None):
        super().__init__(_(".rpm files"), parent)
        self.state = muted(_("Looking which program opens them…"), self)
        self.state.setTextFormat(Qt.TextFormat.PlainText)  # a program's name comes from its menu entry
        self.lay.addWidget(self.state)
        row = QHBoxLayout()
        self.btn = button(_("Open .rpm files with Zypp Desk"), "document-open", self._set, parent=self)
        self.btn.setEnabled(False)
        row.addWidget(self.btn)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.cmd = QLabel(f"xdg-mime default {DESKTOP_ID} {RPM_MIME}", self)
        self.cmd.setFont(mono())
        self.cmd.setWordWrap(True)
        self.cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cmd.setStyleSheet("color: palette(placeholder-text);")
        self.lay.addWidget(self.cmd)

    def reload(self):
        run_async(rpm_opener, on_done=self._shown)

    def _set(self):
        self.btn.setEnabled(False)
        run_async(set_rpm_opener, on_done=self._done)

    def _done(self, st, err):
        if err is not None:
            error_box(self, _(".rpm files"), _("The default could not be changed: {error}").format(error=err))
            self.reload()
            return
        self._shown(st, None)

    def _shown(self, st, err):
        if err is not None or st is None:
            self.state.setText(_("Could not find out which program opens them: {error}").format(error=err))
            self.btn.setEnabled(False)
            return
        if not st["available"]:
            self.state.setText(_("xdg-mime (the xdg-utils package) is not installed, so the program that opens "
                                 "them cannot be chosen from here."))
            self.btn.setEnabled(False)
            return
        if st["current"] == DESKTOP_ID:
            text = _("A double click on an .rpm file opens it with Zypp Desk: it shows what is inside and can "
                     "install it.")
        elif st["current"]:
            text = _("A double click on an .rpm file opens it with {program}. With Zypp Desk, you see what is "
                     "inside, who signed it, and you can install it.").format(program=st["name"])
        else:
            text = _("No program is chosen to open .rpm files. With Zypp Desk, you see what is inside, who signed "
                     "it, and you can install it.")
        if not st["ours"]:
            text += "\n" + _("Zypp Desk's menu entry is not installed (install.sh or the zypp-desk package adds it), "
                             "so it cannot be chosen yet.")
        self.state.setText(text)
        self.btn.setEnabled(st["ours"] and st["current"] != DESKTOP_ID)


class SettingsPage(Page):
    key = "settings"
    title = _("Settings")
    icon_name = "configure"
    COLUMN = 800  # one width for every card (mockup funcions-v1-09), short lines

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        s = ctx.settings
        column = QWidget(self)
        column.setMaximumWidth(self.COLUMN)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        # ------------------------------------------------ the band: when applying the queue
        band = Card(None, column)
        band.lay.setContentsMargins(16, 10, 16, 10)
        band.lay.setSpacing(4)
        from ..tray import FlowLayout
        flow = FlowLayout(spacing=14)
        self.no_rec = QCheckBox(_("Without recommended packages"), band)
        self.no_rec.setChecked(s["apply_no_recommends"])
        self.no_rec.setToolTip(_("Recommended packages add optional parts (plugins, codecs, translations). "
                                 "Ticked, the queue installs only what is needed: --no-recommends."))
        self.lic = QCheckBox(_("Accept licenses without asking"), band)
        self.lic.setChecked(s["apply_accept_licenses"])
        self.lic.setToolTip(_("Changes are applied without questions (-n), after you have seen the simulation. "
                              "If a license must be accepted and this option is off, zypper stops and nothing "
                              "is installed."))
        title = heading(_("When applying the queue"), 1.0, band)
        title.setMinimumHeight(self.no_rec.sizeHint().height())
        title.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        for w in (title, self.no_rec, self.lic):
            flow.addWidget(w)
        band.lay.addLayout(flow)
        bottom = QHBoxLayout()
        self.band_cmd = QLabel(band)  # the exact options the queue adds
        self.band_cmd.setFont(mono())
        self.band_cmd.setWordWrap(True)
        self.band_cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.band_cmd.setStyleSheet("color: palette(placeholder-text);")
        bottom.addWidget(self.band_cmd, 1)
        jump = QLabel(band)  # the other cards are further down (mockup: "Terminal · Notifications ▸")
        jump.setTextFormat(Qt.TextFormat.RichText)
        jump.setText(" · ".join(f'<a href="{key}">{html.escape(text)}</a>' for key, text in
                                (("notify", _("Notifications")), ("terminal", _("Terminal")))) + " ▸")
        jump.linkActivated.connect(self._jump)
        bottom.addWidget(jump, 0, Qt.AlignmentFlag.AlignBottom)
        band.lay.addLayout(bottom)
        col.addWidget(band)

        # ------------------------------------------------ the cards
        from .zyppconf import ZyppConfCard
        self.zconf = ZyppConfCard(ctx, column)
        col.addWidget(self.zconf)
        from .notifysettings import NotifyCard
        self.notify = NotifyCard(ctx, column)
        col.addWidget(self.notify)
        self.terminal_card = c2 = Card(_("Terminal"), column)
        self.fold = QCheckBox(_("Fold a line repeated many times in a row into one"), c2)
        self.fold.setChecked(s["fold_repeated_lines"])
        self.after = QCheckBox(_("After the routine, check what must be restarted (zypper ps)"), c2)
        self.after.setChecked(s["after_routine_check_restart"])
        c2.lay.addWidget(self.fold)
        c2.lay.addWidget(self.after)
        col.addWidget(c2)
        self.rpm = RpmFilesCard(column)
        col.addWidget(self.rpm)
        col.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(column, 1)
        lay.addStretch(0)
        for cb, key in ((self.no_rec, "apply_no_recommends"), (self.lic, "apply_accept_licenses"),
                        (self.fold, "fold_repeated_lines"), (self.after, "after_routine_check_restart")):
            cb.toggled.connect(lambda on, k=key: self._set(k, on))
        self._show_band_cmd()

    def reload(self):
        self.notify.reload()
        self.zconf.reload()
        self.rpm.reload()

    def _show_band_cmd(self):
        opts = (["--no-recommends"] if self.no_rec.isChecked() else []) + \
               (["--auto-agree-with-licenses"] if self.lic.isChecked() else [])
        self.band_cmd.setText(" ".join(["zypper", "-n", "install", *opts, "--", "…"]))

    def _jump(self, key):
        """Scroll the page so that the card is at the top (the band's links)."""
        target = {"notify": self.notify, "terminal": self.terminal_card}.get(key)
        sa = self.parentWidget()
        while sa is not None and not isinstance(sa, QScrollArea):
            sa = sa.parentWidget()
        if target is None or sa is None or sa.widget() is None:
            return
        sa.verticalScrollBar().setValue(target.mapTo(sa.widget(), QPoint(0, 0)).y() - 8)

    def _set(self, key, value):
        self.ctx.settings[key] = value
        self.ctx.settings.save()
        if key in ("apply_no_recommends", "apply_accept_licenses"):
            self._show_band_cmd()
        if key == "fold_repeated_lines":
            self.ctx.terminal.view.fold = value
