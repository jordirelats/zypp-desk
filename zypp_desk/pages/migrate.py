# SPDX-License-Identifier: GPL-3.0-or-later
"""Move to another computer: the window that saves this computer's software in a file (export) and the one that
reads such a file on another computer and puts it in the queue (import). The work is in migrate.py.

open_export(ctx, parent) and open_import(ctx, parent, path=None) are what the rest of the window calls.

Both windows follow the pattern of question.py (Enter never starts anything; every text is plain text: names
come from a file). The import changes nothing by itself: "Put it in the queue" adds the changes to the queue in
one batch, and they run only when the queue is applied. The routine, the protected apps and the programs from the
web are settings of Zypp Desk, not changes in the queue: they change only after a question of their own.
"""

import os

import shiboken6
from PySide6.QtCore import QLocale, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from .. import keys as keyring
from .. import migrate, question, txqueue, ui
from ..i18n import _, ngettext, pgettext
from ..tasks import run_async
from ..txqueue import Action, pkg_ident

# the queue can add a Flatpak remote only once txqueue knows the change "flatpak-remote-add"
REMOTE_ADD = "flatpak-remote-add" in txqueue.RUN_ORDER
FILE_FILTER = "Zypp Desk (*.zdesk)"
SETTING_FOLDER = "migrate_folder"  # the folder of the last .zdesk saved or opened


def _num(n):
    return QLocale().toString(int(n))


def _start_folder(settings):
    folder = (settings or {}).get(SETTING_FOLDER) or ""
    if not folder or not os.path.isdir(folder):
        folder = (QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
                  or os.path.expanduser("~"))
    return folder


def _remember_folder(ctx, path):
    try:
        ctx.settings[SETTING_FOLDER] = os.path.dirname(os.path.abspath(path))
        ctx.settings.save()
    except (OSError, AttributeError):
        pass


def _clear(layout):
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif it.layout() is not None:
            _clear(it.layout())


def _cap(text):
    return text[:1].upper() + text[1:] if text else text


# ------------------------------------------------------------------ export

class ExportDialog(question.Question):
    """“Move to another computer”: what goes in the file (ticked), with how much of each, and Save the file…"""

    def __init__(self, ctx, parent=None):
        rows_box = QWidget()
        rows = QVBoxLayout(rows_box)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        status = question.label(_("Looking at what this computer has… It takes a few seconds."), muted=True)
        checks, counts = {}, {}
        for key, title, detail in (
                ("repos", _("Repositories and keys"), _("With their priorities and the signing keys they use.")),
                ("packages", _("The packages you chose"), _("Not those installed as dependencies: zypper brings "
                                                            "them back.")),
                ("flatpak", _("Flatpak apps"), _("With their remotes.")),
                ("web", _("Programs from the web"), _("Where they are published and the folder you keep them in.")),
                ("settings", _("Locks, languages, the routine and the zypp.conf settings"),
                 _("And the apps you protected.")),
                ("todo", _("A list of what to do by hand"), _("AppImages, web apps, programs with their own "
                                                              "installer: they cannot be installed by themselves."))):
            line = QWidget()
            v = QVBoxLayout(line)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            h = QHBoxLayout()
            h.setSpacing(8)
            cb = QCheckBox(title)
            f = cb.font()
            f.setBold(True)
            cb.setFont(f)
            cb.setChecked(True)
            cb.setEnabled(False)  # until the counts are known
            h.addWidget(cb)
            n = question.label("…", small=True, muted=True, wrap=False)
            h.addWidget(n)
            h.addStretch(1)
            v.addLayout(h)
            d = question.label(detail, small=True, muted=True)
            d.setContentsMargins(26, 0, 0, 0)
            v.addWidget(d)
            rows.addWidget(line)
            checks[key], counts[key] = cb, n
        super().__init__(
            parent, _("Move to another computer"), icon_name="document-export|document-save-as",
            lead=_("Save in a file what this computer has. On the other computer, open it with Zypp Desk: it puts "
                   "everything in the queue, so that you review it before applying it."),
            extra=[status, rows_box],
            means=_("the file holds no passwords and none of your documents: only the list of your software and "
                    "where it comes from."),
            password=False, buttons=[("cancel", _("Cancel"), False), ("save", _("Save everything in a file…"), True)],
            width=720, help_topic="move")
        self.ctx = ctx
        self.data = None
        self.saved_path = None
        self.status, self.checks, self.counts = status, checks, counts
        self.buttons["save"].setEnabled(False)
        self.buttons["save"].setToolTip(_("Choose where to save the file (.zdesk) with what is ticked. Nothing on "
                                          "this computer changes."))
        self.buttons["cancel"].setToolTip(_("Close this window without saving anything"))
        for cb in checks.values():
            cb.toggled.connect(self._ticked)
        run_async(migrate.collect, dict(getattr(ctx, "settings", None) or {}), on_done=self._collected)

    def _collected(self, data, err):
        if not shiboken6.isValid(self):
            return  # the window was closed meanwhile
        if err is not None or not data:
            self.status.setText(_("Zypp Desk could not read what this computer has: {error}").format(
                error=str(err) or type(err).__name__))
            return
        self.data = data
        c = data.get("counts", {})
        self.counts["repos"].setText(ngettext("{n} enabled", "{n} enabled", c.get("repos", 0)).format(
            n=_num(c.get("repos", 0))) + " · " + ngettext("{n} key", "{n} keys", c.get("keys", 0)).format(
            n=_num(c.get("keys", 0))))
        for key in ("packages", "flatpak", "web", "todo"):
            self.counts[key].setText(_num(c.get(key, 0)))
        parts = []
        if c.get("locks"):
            parts.append(ngettext("{n} lock", "{n} locks", c["locks"]).format(n=_num(c["locks"])))
        if c.get("locales"):
            parts.append(ngettext("{n} language", "{n} languages", c["locales"]).format(n=_num(c["locales"])))
        if data.get("routine"):
            parts.append(_("the routine"))
        if c.get("zypp_conf"):
            parts.append(ngettext("{n} zypp.conf setting", "{n} zypp.conf settings", c["zypp_conf"]).format(
                n=_num(c["zypp_conf"])))
        if c.get("protected"):
            parts.append(ngettext("{n} protected app", "{n} protected apps", c["protected"]).format(
                n=_num(c["protected"])))
        self.counts["settings"].setText(", ".join(parts) or _("nothing"))
        for key, cb in self.checks.items():
            cb.setEnabled(True)
        self.status.setText(_("Untick what should not go in the file."))
        self._ticked()

    def _ticked(self, *_a):
        n = len(self.parts())
        self.buttons["save"].setEnabled(self.data is not None and n > 0)
        self.buttons["save"].setText(_("Save everything in a file…") if n == len(self.checks) else ngettext(
            "Save the ticked one in a file…", "Save the {n} ticked in a file…", n).format(n=n))

    def parts(self):
        return [k for k, cb in self.checks.items() if cb.isChecked()]

    def _ask_path(self, suggested):
        path, _f = QFileDialog.getSaveFileName(self, _("Save the file"), suggested, FILE_FILTER)
        return path

    def _press(self, key):
        if key != "save":
            super()._press(key)
            return
        if self.data is None or not self.parts():
            return
        suggested = os.path.join(_start_folder(self.ctx.settings), migrate.default_name(self.data.get("computer")))
        path = self._ask_path(suggested)
        if not path:
            return  # the window stays: nothing was saved
        if not path.endswith(migrate.EXTENSION):
            path += migrate.EXTENSION
        try:
            migrate.write(path, self.data, self.parts())
        except OSError as e:
            ui.error_box(self, _("Save the file"), _("The file could not be saved: {reason}").format(
                reason=e.strerror or e))
            return
        _remember_folder(self.ctx, path)
        self.saved_path = path
        ui.info_box(self, _("Move to another computer"), _(
            "Saved in {file}.\n\nCopy it to the other computer and open it there with Zypp Desk (Settings → "
            "Move to another computer, or open the file).").format(file=path))
        super()._press("save")


def open_export(ctx, parent=None):
    """The export window. Returns the path of the file saved, or None."""
    dlg = ExportDialog(ctx, parent)
    try:
        dlg.exec()
        return dlg.saved_path
    finally:
        dlg.deleteLater()


# ------------------------------------------------------------------ choosing what goes in

class ChooseDialog(QDialog):
    """A list to tick and untick, with a filter: which repositories, packages or apps go in the queue."""

    def __init__(self, parent, title, rows, chosen):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 620)
        v = QVBoxLayout(self)
        v.addWidget(question.label(title, bold=True))
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText(_("Filter…"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        v.addWidget(self.filter)
        self.list = QListWidget(self)
        self.list.setUniformItemSizes(True)
        for key, text, detail in rows:
            it = QListWidgetItem(f"{text}  —  {detail}" if detail else text)
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if key in chosen else Qt.CheckState.Unchecked)
            if detail:
                it.setToolTip(detail)
            self.list.addItem(it)
        self.list.itemChanged.connect(self._count)
        v.addWidget(self.list, 1)
        h = QHBoxLayout()
        self.b_tick = QPushButton(_("Tick all"), self)
        self.b_untick = QPushButton(_("Untick all"), self)
        for b, state in ((self.b_tick, Qt.CheckState.Checked), (self.b_untick, Qt.CheckState.Unchecked)):
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, s=state: self._set_all(s))
            h.addWidget(b)
        self.count = question.label("", small=True, muted=True)
        h.addWidget(self.count, 1)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        for b in bb.buttons():
            b.setAutoDefault(False)  # Enter in the filter does not close the window
        self.b_ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.b_ok.setToolTip(_("Back to the list of changes with only the ticked ones. Nothing goes to the queue yet."))
        bb.button(QDialogButtonBox.StandardButton.Cancel).setToolTip(_("Back without changing what was chosen"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._count()

    def _filter(self, text):
        t = text.strip().casefold()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().casefold())
        self._count()

    def _set_all(self, state):
        """Tick or untick the rows shown (all of them, or those the filter leaves)."""
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.isHidden():
                it.setCheckState(state)
        self.list.blockSignals(False)
        self._count()

    def _count(self, *_a):
        n = sum(1 for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked)
        self.count.setText(_("{n} of {total} ticked").format(n=_num(n), total=_num(self.list.count())))
        # Tick all and Untick all act on the rows the filter leaves: they say how many
        shown = sum(1 for i in range(self.list.count()) if not self.list.item(i).isHidden())
        if shown == self.list.count():
            self.b_tick.setText(_("Tick all"))
            self.b_untick.setText(_("Untick all"))
        else:
            self.b_tick.setText(ngettext("Tick the {n} shown", "Tick the {n} shown", shown).format(n=_num(shown)))
            self.b_untick.setText(ngettext("Untick the {n} shown", "Untick the {n} shown", shown).format(
                n=_num(shown)))
        self.b_tick.setToolTip(_("Tick every row the filter shows"))
        self.b_untick.setToolTip(_("Untick every row the filter shows"))
        self.b_ok.setText(ngettext("Keep the ticked one", "Keep the {n} ticked", n).format(n=_num(n)) if n else
                          _("Keep none"))

    def chosen(self):
        return {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked}


# ------------------------------------------------------------------ import

class ImportDialog(question.Question):
    """“Import the software from …”: what the file adds to this computer, in a few rows like the queue's, and
    the list of what to do by hand. "Put it in the queue" adds it all in one batch; nothing runs until Apply."""

    def __init__(self, ctx, path, data, parent=None):
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(question.label(_("Comparing the file with this computer… It takes a few seconds."),
                                     muted=True))
        computer = migrate.text(data.get("computer"), 40) or _("another computer")
        super().__init__(
            parent, _("Import the software from {computer}").format(computer=computer),
            icon_name="document-import|document-open",
            lead=_("From the file “{file}”. Nothing changes until you apply the queue.").format(
                file=migrate.text(os.path.basename(path), 120)),
            extra=[body], password=False,
            buttons=[("cancel", _("Cancel"), False), ("queue", _("Put it in the queue"), False)],
            width=740, help_topic="move")
        self.ctx, self.path, self.data = ctx, path, data
        self.body, self.body_lay = body, lay
        self.plan = None
        self.queued_actions = []
        self.sel_repos, self.sel_pkgs, self.sel_fp = set(), set(), set()
        self.buttons["queue"].setEnabled(False)
        self.buttons["cancel"].setToolTip(_("Close this window: nothing goes to the queue"))
        q = ctx.queue
        queued = {"aliases": [a.target for a in q.of("repo-add")],
                  "urls": [a.opts.get("url") or "" for a in q.of("repo-add")],
                  "packages": [pkg_ident(a) for a in q.of("install")],
                  "flatpaks": [a.target.split("//")[0] for a in q.of("flatpak-install")]}
        run_async(migrate.prepare, data, dict(getattr(ctx, "settings", None) or {}), queued, remote_add=REMOTE_ADD,
                  on_done=self._planned)

    # ---------------------------------------------------------- the plan
    def _planned(self, plan, err):
        if not shiboken6.isValid(self):
            return
        if err is not None or plan is None:
            _clear(self.body_lay)
            self.body_lay.addWidget(question.box("error", _("Zypp Desk could not compare the file with this "
                                                            "computer: {error}").format(error=str(err) or
                                                                                        type(err).__name__)))
            return
        if plan["routine"]:  # only zypper, flatpak and fwupdmgr: a routine that runs anything else is left out
            from .. import routine
            try:
                routine.parse(plan["routine"])
            except routine.RoutineError as e:
                plan["refused"].append((_("Routine"), plan["routine"], str(e)))
                plan["routine"] = ""
        self.plan = plan
        self.sel_repos = {r["alias"] for r in plan["repos"]} | {r["alias"] for r in plan["repos_enable"]}
        self.sel_pkgs = {p["name"] for p in plan["packages"] if not p.get("for_flatpak")}
        self.sel_fp = {f["id"] for f in plan["flatpak"]}
        self._fill()

    def _chosen_packages(self):
        """The chosen packages that can still be installed: those of a repository left out cannot."""
        p = self.plan
        new = {r["alias"] for r in p["repos"]}
        out = [x for x in p["packages"] if (x["name"] in self.sel_pkgs or (x.get("for_flatpak") and self.sel_fp))
               and (not x["repo"] or x["repo"] not in new or x["repo"] in self.sel_repos)]
        return out

    def _fill(self):
        p = self.plan
        lay = self.body_lay
        _clear(lay)
        for w in p["warnings"]:
            lay.addWidget(question.box("warn", "⚠ " + w))
        repos = [r for r in p["repos"] if r["alias"] in self.sel_repos]
        enable = [r for r in p["repos_enable"] if r["alias"] in self.sel_repos]
        keys_new = [k for k in p["keys"] if any(a in self.sel_repos for a in k["repos"])]
        pkgs = self._chosen_packages()
        fps = [f for f in p["flatpak"] if f["id"] in self.sel_fp]
        total = 0

        # repositories
        if p["repos"] or p["repos_enable"]:
            n = len(repos)
            title = ngettext("Add {n} repository", "Add {n} repositories", n).format(n=_num(n))
            detail = ngettext("With its key ({k} new) and priority.", "With their keys ({k} new) and priorities.",
                              n).format(k=_num(len(keys_new))) if n else _("None chosen.")
            if enable:
                detail += " " + ngettext("And enable {n} that is here but disabled.",
                                         "And enable {n} that are here but disabled.", len(enable)).format(
                    n=_num(len(enable)))
            more = []
            for r in repos:
                more.append(f"{r['name']} — {r['url'] if not r['local'] else _('a folder on this computer')}")
                for k in keys_new:
                    if r["alias"] in k["repos"]:
                        more.append("    " + _("key “{name}”, fingerprint {fpr}").format(
                            name=k["name"], fpr=txqueue.nice_fpr(k["fpr"])))
            more += [_("Enable “{name}”").format(name=r["name"]) for r in enable]
            lay.addWidget(question.item("folder-cloud|folder-network", title, detail, more or None,
                                        right=self._choose_button(
                                            _("Choose the repositories…"),
                                            _("Tick or untick the repositories that go in the queue"),
                                            self.choose_repos)))
            total += n + len(enable)

        # packages
        if p["packages"]:
            n = len(pkgs)
            # the programs this computer does not get from its own repositories first (a browser, a media player…)
            titled = [x["title"] for x in sorted((x for x in pkgs if x["title"]),
                                                 key=lambda x: (not x.get("extra_repo"), x["title"].casefold()))]
            names = titled[:4] or [x["name"] for x in pkgs[:4]]
            rest = n - len(names)
            detail = (ngettext("{names} and {n} more", "{names} and {n} more", rest).format(
                names=", ".join(names), n=_num(rest)) if rest > 0 else
                      ", ".join(names)) if n else _("None chosen.")
            if any(x["repo"] for x in pkgs):
                detail += "\n" + _("Some come from a repository added now: zypper reads it first, when you apply "
                                   "the queue (Simulate leaves them out).")
            more = sorted(x["name"] + (f"  ({x['title']})" if x["title"] else "") for x in pkgs)
            lay.addWidget(question.item("package-install|system-software-install", ngettext(
                "Install {n} package", "Install {n} packages", n).format(n=_num(n)), detail, more or None,
                right=self._choose_button(_("Choose the packages…"), _("Tick or untick the packages that go in the "
                                                                        "queue"), self.choose_packages)))
            total += n
        skipped = []
        if p["packages_missing"]:
            skipped.append(ngettext("{n} package is not in the repositories of this computer",
                                    "{n} packages are not in the repositories of this computer",
                                    len(p["packages_missing"])).format(n=_num(len(p["packages_missing"]))))
        if p["packages_locked"]:
            skipped.append(ngettext("{n} package is locked here", "{n} packages are locked here",
                                    len(p["packages_locked"])).format(n=_num(len(p["packages_locked"]))))
        if skipped:
            lay.addWidget(question.item("dialog-information|help-about", _("Left out: {list}").format(
                list="; ".join(skipped)), _("Zypp Desk does not put them in the queue: zypper would stop the whole "
                                            "install for one of them."),
                sorted(p["packages_missing"] + [x + "  " + _("(locked)") for x in p["packages_locked"]])))

        # Flatpak
        eol = [t for t in p["todo"] if t["kind"] == "flatpak-eol"]
        if p["flatpak"] or eol:
            n = len(fps)
            remotes = sorted({f.get("remote_title") or f["remote"] for f in fps})
            detail = _("From {remotes}.").format(remotes=", ".join(remotes)) if fps else ""
            for t in eol:
                detail += (" " if detail else "") + _("{name} is no longer on {remote}: install it from the copy "
                                                      "you saved.").format(name=t["title"], remote=t.get("remote"))
            if p["flatpak_remotes"]:
                detail += " " + ngettext("The remote {names} is added first.", "The remotes {names} are added first.",
                                         len(p["flatpak_remotes"])).format(
                    names=", ".join(r["name"] for r in p["flatpak_remotes"]))
            lay.addWidget(question.item("flatpak-discover|package-x-generic", ngettext(
                "Install {n} Flatpak app", "Install {n} Flatpak apps", n).format(n=_num(n)), detail,
                [f"{f['name']} ({f['id']})" for f in fps] or None,
                right=self._choose_button(_("Choose the apps…"), _("Tick or untick the Flatpak apps that go in the "
                                                                    "queue"), self.choose_flatpaks)
                if p["flatpak"] else None))
            total += n

        # locks, languages, routine, zypp.conf, protected apps
        parts, more = [], []
        if p["locks"]:
            parts.append(ngettext("{n} lock", "{n} locks", len(p["locks"])).format(n=_num(len(p["locks"]))))
            more += [_("Lock {name}").format(name=lk["target"]) + (f"  ({lk['repo']})" if lk["repo"] else "")
                     for lk in p["locks"]]
        if p["locales"]:
            parts.append(_("the language {code}").format(code=p["locales"][0]) if len(p["locales"]) == 1 else
                         ngettext("{n} language", "{n} languages", len(p["locales"])).format(
                             n=_num(len(p["locales"]))))
            more += [_("Add the language {code}").format(code=c) for c in p["locales"]]
        if p["routine"]:
            parts.append(_("your routine"))
            more.append(_("Routine: {text}").format(text=p["routine"]))
        if p["zypp_conf"]:
            parts.append(_("zypper's settings"))
            more += [f"zypp.conf: {k} = {new}" for k, _now, new in p["zypp_conf"]["changes"]]
        if p["protected"]:
            parts.append(ngettext("{n} protected app", "{n} protected apps", len(p["protected"])).format(
                n=_num(len(p["protected"]))))
            more += [_("Protect {name}").format(name=x.partition(":")[2]) for x in p["protected"]]
        if parts:
            asked = p["routine"] or p["zypp_conf"] or p["protected"]
            lay.addWidget(question.item("object-locked|preferences-system", _cap(", ".join(parts)),
                                        _("Your routine, zypper's settings and the protected apps change only if you "
                                          "say so.") if asked else "", more))
            total += len(p["locks"]) + len(p["locales"]) + bool(asked)

        # programs from the web
        if p["web_sources"]:
            n = len(p["web_sources"])
            followed = [s for s in p["web_sources"] if s["type"] != "manual"]
            folders = sorted({s["folder"] for s in followed if s.get("folder")})
            detail = _("Zypp Desk looks for their new versions: download and install them in Programs from the "
                       "web.")
            if folders:
                detail += " " + _("They are kept in {folders}.").format(folders=", ".join(folders))
            lay.addWidget(question.item("applications-internet|internet-web-browser", ngettext(
                "Follow {n} program from the web", "Follow {n} programs from the web", n).format(n=_num(n)), detail,
                [s["package"] for s in p["web_sources"]]))
            total += n

        if total == 0:
            lay.addWidget(question.box("ok", _("This computer already has everything in the file that can go in "
                                               "the queue.")))
        have = []
        if p["repos_have"]:
            have.append(ngettext("{n} repository", "{n} repositories", p["repos_have"]).format(
                n=_num(p["repos_have"])))
        if p["packages_have"]:
            have.append(ngettext("{n} package", "{n} packages", p["packages_have"]).format(
                n=_num(p["packages_have"])))
        if p["flatpak_have"]:
            have.append(ngettext("{n} Flatpak app", "{n} Flatpak apps", p["flatpak_have"]).format(
                n=_num(p["flatpak_have"])))
        if have:
            lay.addWidget(question.label(_("Already on this computer: {list}.").format(list=", ".join(have)),
                                         small=True, muted=True))

        # by hand
        todo = [t for t in p["todo"] if t["kind"] != "flatpak-eol"] + eol
        if todo:
            lay.addWidget(self._by_hand(todo))
        if p["refused"]:
            lay.addWidget(question.item("dialog-warning", ngettext(
                "Left out: {n} thing Zypp Desk could not check in the file",
                "Left out: {n} things Zypp Desk could not check in the file", len(p["refused"])).format(
                n=_num(len(p["refused"]))), _("They are not in the queue."),
                [f"{what}: {name} — {why}" for what, name, why in p["refused"]]))
        if repos or keys_new:
            lay.addWidget(question.box("warn", "⚠ " + _(
                "Open only a file you made yourself, or one from someone you trust: its repositories and keys let "
                "zypper install programs from them as administrator. Check the list before you apply the queue.")))
        self._label_queue(total)
        if self.isVisible():
            self._fit()

    def _label_queue(self, total):
        """“Put the 312 changes in the queue”, with “…” when the question about the settings comes first."""
        b = self.buttons["queue"]
        b.setEnabled(total > 0)
        asks = bool(self._settings_rows())
        if total > 0:
            text = ngettext("Put {n} change in the queue", "Put the {n} changes in the queue", total).format(
                n=_num(total))
        else:
            text = _("Put it in the queue")
        b.setText(text + ("…" if asks else ""))
        tip = _("Adds the chosen changes to the queue in one go. Nothing changes until you apply the queue.")
        if asks:
            tip += " " + _("First it asks about the routine, zypper's settings, the protected apps and the programs "
                           "from the web: those settings change as soon as you answer.")
        b.setToolTip(tip)

    def _choose_button(self, text, tip, slot):
        b = QPushButton(text)
        b.setAutoDefault(False)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _by_hand(self, todo):
        """“By hand (N)”: a summary line, “Show it all” and “Copy the list”."""
        f = QFrame()
        f.setObjectName("byhand")
        f.setStyleSheet("QFrame#byhand { background: palette(base); border: 1px solid rgba(127, 140, 141, 0.35); "
                        "border-radius: 10px; }")
        v = QVBoxLayout(f)
        v.setContentsMargins(12, 9, 12, 9)
        v.setSpacing(3)
        v.addWidget(question.label(_("By hand ({n})").format(n=_num(len(todo))), bold=True))
        groups = []
        inst = [t["title"] for t in todo if t["kind"] == "installer"]
        if inst:
            groups.append(_("{names} (own installer)").format(names=", ".join(inst[:3]) + (" …" if len(inst) > 3
                                                                                           else "")))
        imgs = [t["title"] for t in todo if t["kind"] == "appimage"]
        if imgs:
            groups.append(_("{names} (AppImage)").format(names=", ".join(imgs[:3]) + (" …" if len(imgs) > 3 else "")))
        web = [t for t in todo if t["kind"] == "webapp"]
        if web:
            browsers = sorted({t.get("browser") or "?" for t in web})
            groups.append(ngettext("{n} web app of {browsers}", "{n} web apps of {browsers}", len(web)).format(
                n=_num(len(web)), browsers=", ".join(browsers)))
        others = [t["title"] for t in todo if t["kind"] not in ("installer", "appimage", "webapp")]
        if others:
            groups.append(", ".join(others[:3]) + (" …" if len(others) > 3 else ""))
        v.addWidget(question.label(" · ".join(groups), small=True, muted=True))
        lines = [f"• {t['title']} — {t['detail']}" for t in todo]
        full = question.label("\n".join(lines), small=True, selectable=True)
        full.setVisible(False)
        h = QHBoxLayout()
        more = question.More(_("▸ Show the whole list"), _("▾ Hide the list"), full)
        more.setToolTip(_("Show or hide what to do by hand, one by one"))
        h.addWidget(more)
        h.addStretch(1)
        copy = QPushButton(ui.icon("edit-copy"), _("Copy the list"))
        copy.setAutoDefault(False)
        copy.setFlat(True)
        copy.setToolTip(_("Copy the list of what to do by hand to the clipboard, to keep it somewhere"))

        def copied():
            QGuiApplication.clipboard().setText("\n".join(lines))
            copy.setText(_("Copied ✓"))
            QTimer.singleShot(2500, lambda: shiboken6.isValid(copy) and copy.setText(_("Copy the list")))
        copy.clicked.connect(copied)
        h.addWidget(copy)
        v.addLayout(h)
        v.addWidget(full)
        return f

    # ---------------------------------------------------------- Choose…
    def _choose(self, title, rows, chosen):
        """The chosen keys, or None when the window is cancelled (tests replace it)."""
        dlg = ChooseDialog(self, title, rows, chosen)
        try:
            return dlg.chosen() if dlg.exec() else None
        finally:
            dlg.deleteLater()

    def choose_repos(self):
        p = self.plan
        rows = [(r["alias"], r["name"], r["url"] if not r["local"] else _("a folder on this computer"))
                for r in p["repos"]] + [(r["alias"], r["name"], _("here, disabled: enable it"))
                                        for r in p["repos_enable"]]
        got = self._choose(_("Which repositories?"), rows, self.sel_repos)
        if got is not None:
            self.sel_repos = got
            self._fill()

    def choose_packages(self):
        p = self.plan
        rows = sorted(((x["name"], x["name"], x["title"]) for x in p["packages"] if not x.get("for_flatpak")),
                      key=lambda r: r[0].casefold())
        got = self._choose(_("Which packages?"), rows, self.sel_pkgs)
        if got is not None:
            self.sel_pkgs = got
            self._fill()

    def choose_flatpaks(self):
        rows = [(f["id"], f["name"], f["id"]) for f in self.plan["flatpak"]]
        got = self._choose(_("Which Flatpak apps?"), rows, self.sel_fp)
        if got is not None:
            self.sel_fp = got
            self._fill()

    # ---------------------------------------------------------- Put it in the queue
    def _press(self, key):
        if key == "queue":
            if self.plan is None or not self._put_in_queue():
                return  # nothing was added: the window stays
        super()._press(key)

    def _ask_settings(self, rows):
        """The question before the settings change: rows are (key, title, detail, more). Returns the ticked keys,
        set() for “Leave them as they are”, or None when the window is closed (tests replace it)."""
        boxes, extra = {}, []
        for key, title, detail, more in rows:
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)
            cb = QCheckBox(title)
            f = cb.font()
            f.setBold(True)
            cb.setFont(f)
            cb.setChecked(True)
            v.addWidget(cb)
            if detail:
                d = question.label(detail, small=True, muted=True)
                d.setContentsMargins(26, 0, 0, 0)
                v.addWidget(d)
            if more:
                m = question.label("\n".join(more), small=True, selectable=True, font=ui.mono())
                m.setContentsMargins(26, 0, 0, 0)
                v.addWidget(m)
            boxes[key] = cb
            extra.append(w)
        dlg = question.Question(
            self, _("Also change these settings?"), icon_name="configure|preferences-system",
            lead=_("They are settings of Zypp Desk and of zypper, not packages. Untick what should stay as it is "
                   "on this computer."), extra=extra,
            means=_("the routine, the protected apps and the programs from the web change as soon as you press the "
                    "button; zypper's settings go in the queue with the rest."),
            password=False, buttons=[("keep", _("Leave them as they are"), False),
                                     ("change", _("Change the ticked ones"), True)], width=660)
        dlg.buttons["keep"].setToolTip(_("Nothing of these changes; the rest still goes to the queue."))

        def label_change(*_a):
            n = sum(cb.isChecked() for cb in boxes.values())
            b = dlg.buttons["change"]
            if n == len(boxes) == 1:
                b.setText(_("Change it"))
            elif n == len(boxes):
                b.setText(ngettext("Change all {n}", "Change all {n}", n).format(n=n))
            elif n:
                b.setText(ngettext("Change the ticked one", "Change the {n} ticked", n).format(n=n))
            else:
                b.setText(_("Change the ticked ones"))
            b.setEnabled(n > 0)
            b.setToolTip(_("The routine, the protected apps and the programs from the web change at once; zypper's "
                           "settings go to the queue with the rest."))
        for cb in boxes.values():
            cb.toggled.connect(label_change)
        label_change()
        try:
            answer = dlg.exec()
        finally:
            dlg.deleteLater()
        if answer is None:
            return None
        return {k for k, cb in boxes.items() if cb.isChecked()} if answer == "change" else set()

    def _settings_rows(self):
        p = self.plan
        rows = []
        if p["routine"]:  # checked in _planned
            now = self.ctx.settings.get("routine") or ""
            rows.append(("routine", _("Use the routine of the other computer"), "",
                         [_("Now: {text}").format(text=now), _("New: {text}").format(text=p["routine"])]))
        if p["zypp_conf"]:
            rows.append(("zypp_conf", pgettext("change", "Change zypper's settings (zypp.conf)"),
                         _("In the queue: written when you apply it."),
                         [f"{k}: {now or '-'} → {new}" for k, now, new in p["zypp_conf"]["changes"]]))
        if p["protected"]:
            n = len(p["protected"])
            rows.append(("protected", ngettext("Protect {n} app", "Protect {n} apps", n).format(n=_num(n)),
                         _("Zypp Desk never removes a protected app until you stop protecting it."),
                         [x.partition(":")[2] for x in p["protected"]]))
        if p["web_sources"]:
            n = len(p["web_sources"])
            folders = sorted({s["folder"] for s in p["web_sources"] if s.get("folder")})
            rows.append(("web", ngettext("Follow {n} program from the web", "Follow {n} programs from the web",
                                         n).format(n=_num(n)),
                         _("They download into {folders} (Zypp Desk makes the folder when it needs it).").format(
                             folders=", ".join(folders)) if folders else "",
                         [s["package"] for s in p["web_sources"]]))
        return rows

    def _put_in_queue(self):
        """Every chosen change in the queue in one batch, after the question about the settings. True when it was
        done (the window closes)."""
        p = self.plan
        rows = self._settings_rows()
        change = set()
        if rows:
            change = self._ask_settings(rows)
            if change is None:
                return False
        new_aliases = {r["alias"] for r in p["repos"]}
        repos = [r for r in p["repos"] if r["alias"] in self.sel_repos]
        enable = [r for r in p["repos_enable"] if r["alias"] in self.sel_repos]
        chosen = {r["alias"] for r in repos}
        pkgs = self._chosen_packages()
        fps = [f for f in p["flatpak"] if f["id"] in self.sel_fp]
        remotes = [r for r in p["flatpak_remotes"] if any(f["remote"] == r["name"] for f in fps)]
        acts = []
        try:
            for k in p["keys"]:
                users = [a for a in k["repos"] if a in chosen]
                if users:  # the queue review links a key to one repository (for_repo): the first one chosen
                    path = keyring.save_for_import(k["data"].encode("ascii"), k["fpr"])
                    acts.append(Action("key-import", k["fpr"], path=path, name=k["name"], for_repo=users[0]))
        except (OSError, ValueError) as e:
            ui.error_box(self, _("Put it in the queue"), _("A signing key of the file could not be used, so nothing "
                                                           "was put in the queue: {error}").format(error=e))
            return False
        if "zypp_conf" in change and p["zypp_conf"]:
            acts.append(Action("zypp-conf", "zypp.conf", settings=dict(p["zypp_conf"]["settings"])))
        for r in repos:
            acts.append(Action("repo-add", r["alias"], **r["opts"]))
        for r in enable:
            acts.append(Action("repo-modify", r["alias"], enabled=True))
        for lk in p["locks"]:
            if lk["repo"] and lk["repo"] in new_aliases and lk["repo"] not in chosen:
                continue
            opts = {"type": lk["type"]} if lk["type"] != "package" else {}
            if lk["repo"]:
                opts["repo"] = lk["repo"]
            acts.append(Action("lock", lk["target"], **opts))
        for x in pkgs:
            acts.append(Action("install", x["target"]))
        for c in p["locales"]:
            acts.append(Action("locale-add", c))
        for r in remotes:  # only when txqueue knows "flatpak-remote-add" (REMOTE_ADD)
            opts = {"url": r.get("flatpakrepo") or r["url"], "installation": r["installation"], "title": r["title"]}
            if not r.get("flatpakrepo") and r.get("key"):
                opts["gpg_key_data"] = r["key"]
            acts.append(Action("flatpak-remote-add", r["name"], **opts))
        for f in fps:
            acts.append(Action("flatpak-install", f["target"], remote=f["remote"], installation=f["installation"]))
        queue = self.ctx.queue
        with queue.batch():
            for a in acts:
                queue.add(a)
        self.queued_actions = acts
        self._change_settings(change)
        later = len(p["packages_later"]) + len(p["locks_later"])
        if later:
            ui.info_box(self, _("Put it in the queue"), ngettext(
                "{n} change waits for repositories zypper has not read yet. After you apply the queue, open the "
                "file again: then Zypp Desk adds it.",
                "{n} changes wait for repositories zypper has not read yet. After you apply the queue, open the "
                "file again: then Zypp Desk adds them.", later).format(n=_num(later)))
        return True

    def _change_settings(self, change):
        """The routine, the protected apps and the programs from the web, when the question said so."""
        p, ctx = self.plan, self.ctx
        settings = ctx.settings
        changed = False
        if "routine" in change and p["routine"]:
            settings["routine"] = p["routine"]
            changed = True
        if "protected" in change and p["protected"]:
            settings["protected_apps"] = sorted(set(settings.get("protected_apps") or []) | set(p["protected"]))
            changed = True
        if changed:
            settings.save()
        pages = getattr(ctx, "pages", {}) or {}
        if "routine" in change and p["routine"] and hasattr(pages.get("updates"), "show_routine"):
            pages["updates"].show_routine()
        if "web" in change and p["web_sources"]:
            from .. import websources
            now = websources.load()
            have = {s.get("package") for s in now}
            websources.save(now + [s for s in p["web_sources"] if s["package"] not in have])
            page = pages.get("websources")
            if page is not None:
                page.sources = websources.load()  # the page keeps the list: it must not save the old one later
                if getattr(page, "loaded", False):
                    page.reload()
                if hasattr(page, "sources_changed"):
                    page.sources_changed.emit()


def open_import(ctx, parent=None, path=None):
    """Open a .zdesk file (asked for when path is None), check it, compare it with this computer and show what
    it would put in the queue. True when the changes went to the queue."""
    if path is None:
        path, _f = QFileDialog.getOpenFileName(parent, _("Open a file of another computer"),
                                               _start_folder(getattr(ctx, "settings", None)),
                                               FILE_FILTER + ";;" + _("All files (*)"))
        if not path:
            return False
    try:
        data = migrate.load(path)
    except migrate.MigrateError as e:
        ui.error_box(parent, _("Import the software of another computer"), str(e))
        return False
    _remember_folder(ctx, path)
    dlg = ImportDialog(ctx, path, data, parent)
    try:
        return dlg.exec() == "queue"
    finally:
        dlg.deleteLater()
