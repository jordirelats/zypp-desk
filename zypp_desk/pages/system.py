# SPDX-License-Identifier: GPL-3.0-or-later
"""Locks, services, languages, maintenance and settings (History is in history.py)."""

import html
import os
import re
import shutil
import subprocess
from functools import cmp_to_key
from pathlib import Path

from PySide6.QtCore import QLocale, QPoint, Qt
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import APP_ID, backend, diskspace, question, repostate, zyppconf
from ..i18n import _, ngettext, pgettext
from ..runner import Step
from ..tasks import run_async
from ..ui import Card, Page, button, error_box, heading, mono, muted, tool


def kind_label(kind):
    return {"package": _("package"), "pattern": _("pattern"), "patch": _("patch"), "product": _("product"),
            "srcpackage": _("source package")}.get(kind, kind)


def ask_clean_locks(parent, step):
    """The question before 'zypper cleanlocks' (Locks and Maintenance)."""
    return question.confirm(
        parent, _("Clean the useless locks?"), _("Yes, clean them"), window_title=_("Clean useless locks"),
        icon_name="object-locked",
        items=[("object-locked", _("Remove the locks that match no package"),
                _("Neither an installed package nor one in your repositories."))],
        means=_("nothing changes in what zypper may install or update now. A lock for a package that is only "
                "missing for a while (its repository is disabled) goes too."),
        commands_=[step.display()], help_topic="problems")


def simple_tree(headers, parent):
    t = QTreeWidget(parent)
    t.setHeaderLabels(headers)
    t.setRootIsDecorated(False)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(True)
    return t


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
        self.repos = []

    def header_actions(self):
        return [tool(_("Refresh services"), "view-refresh",
                     lambda: self.ctx.run([Step("zypper", ["-n", "-v", "refresh-services"])], _("Refresh services"),
                                          on_done=lambda ok: self.ctx.after_change())),
                tool(_("Enable / disable"), "system-run", self.toggle)]

    def reload(self):
        run_async(self._load_all, on_done=self._loaded)

    @staticmethod
    def _load_all():
        items = backend.services()
        try:
            repos = backend.repos()  # to say what a service's repositories are (Tumbleweed's own?)
        except Exception:  # the services are shown anyway
            repos = []
        return items, repos

    def _loaded(self, res, err):
        if err:
            error_box(self, self.title, str(err))
            return
        items, self.repos = res
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
        if self._ask_toggle(s, st):
            self.ctx.run([st], _("Change a service"), on_done=lambda ok: self.ctx.after_change())

    def _ask_toggle(self, s, st):
        """The question before switching a service on or off. zypper disables a service's repositories with it,
        and enabling it again puts them back as they were at its last refresh."""
        name = s["name"]
        mine = [r for r in self.repos if r["alias"] in s["repos"]]
        if s["enabled"]:
            n = sum(1 for r in mine if r.get("enabled")) if mine else len(s["repos"])
            detail = _("A service is a list of repositories its provider keeps up to date.")
            if n:
                detail += " " + ngettext("Its repository is disabled with it.", "Its {n} repositories are disabled "
                                         "with it.", n).format(n=n)
            tw = any(repostate.is_tumbleweed(r) for r in mine if r.get("enabled")) or (
                not mine and any(repostate.is_tumbleweed({"alias": a}) for a in s["repos"]))
            means = _("you get no programs or updates from its repositories until you enable it again.")
            if tw:
                means += " " + _("It is normally left enabled.")
            return question.confirm(
                self, _("Disable the service “{name}”?").format(name=name), _("Yes, disable it"),
                window_title=_("Change a service"), icon_name="network-server",
                items=[("network-server", pgettext("change", "Stop using the service “{name}”").format(name=name),
                        detail)],
                means=means, warning=_("Without this service, Tumbleweed is not updated.") if tw else None,
                commands_=[st.display()], help_topic="repos")
        return question.confirm(
            self, _("Enable the service “{name}”?").format(name=name), _("Yes, enable it"),
            window_title=_("Change a service"), icon_name="network-server",
            items=[("network-server", pgettext("change", "Use the service “{name}” again").format(name=name),
                    _("Its repositories are enabled again as they were before."))],
            means=_("zypper refreshes the service and looks for programs and updates in its repositories again."),
            commands_=[st.display()], help_topic="repos")


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


def keep_by_rule(rule, editions, running=None):
    """The kernels zypp.conf's multiversion.kernels keeps ('latest,latest-1,running'), of these editions (sorted
    oldest first); None when the rule has a part this cannot read (an exact version)."""
    tokens = [t.strip() for t in (rule or "").split(",") if t.strip()]
    if not tokens or not editions:
        return None
    keep = set()
    for t in tokens:
        if t == "latest":
            keep.add(editions[-1])
        elif t == "oldest":
            keep.add(editions[0])
        elif t == "running":
            if running:
                keep.add(running)
        elif (m := re.fullmatch(r"latest-(\d+)", t)):
            i = len(editions) - 1 - int(m[1])
            if i >= 0:
                keep.add(editions[i])
        elif (m := re.fullmatch(r"oldest\+(\d+)", t)):
            i = int(m[1])
            if i < len(editions):
                keep.add(editions[i])
        else:
            return None
    return keep


def kernel_plan():
    """What 'zypper purge-kernels' would take away, from rpm's database and zypp.conf (as the user, no root):
    dict(installed, running, keep, remove, size) for kernel-default, or None when it cannot be told. keep and
    remove are None when the rule cannot be read. Only kernel-default is counted: the parts that belong to a
    kernel (devel, modules) go with it and are not in the size."""
    rule = (zyppconf.read_effective().get("multiversion.kernels") or {}).get("value") or ""
    p = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE} %{SIZE}[ %{PROVIDENAME}=%{PROVIDEVERSION}]\n",
                        "kernel-default"], capture_output=True, text=True, timeout=60, env=backend.ENV_C,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    kernels = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        unames = {x.split("=", 1)[1] for x in parts[2:] if x.startswith("kernel-uname-r=")}
        kernels.append((parts[0], int(parts[1]), unames))
    if not kernels:
        return None
    kernels.sort(key=cmp_to_key(lambda a, b: backend.vercmp(a[0], b[0])))
    editions = [k[0] for k in kernels]
    release = os.uname().release
    running = next((k[0] for k in kernels if release in k[2]), None)
    keep = keep_by_rule(rule, editions, running)
    remove = None if keep is None else [e for e in editions if e not in keep]
    size = sum(k[1] for k in kernels if remove and k[0] in remove)
    return dict(installed=editions, running=running, keep=keep, remove=remove, size=size, rule=rule)


def zypp_cache_sizes(root="/var/cache/zypp"):
    """dict(packages, metadata) in bytes: what 'zypper clean --all' frees (the folders are readable by everyone)."""
    def du(path):
        total = 0
        for base, _dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                try:
                    total += os.lstat(os.path.join(base, f)).st_blocks * 512
                except OSError:
                    pass
        return total
    return dict(packages=du(os.path.join(root, "packages")),
                metadata=du(os.path.join(root, "raw")) + du(os.path.join(root, "solv")))


def nice_bytes(n):
    return QLocale().formattedDataSize(int(n), 1 if n >= 1 << 30 else 0)


class MaintenancePage(Page):
    key = "maintenance"
    title = _("Maintenance")
    icon_name = "tools"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        from .. import health
        health.set_size_format(nice_bytes)  # sizes in the findings' sentences, in the user's language
        self._health_round = 0
        self._health_checked = False
        self._health_busy = False
        self._health = None  # the last result of health.run_all
        self._room_names = []
        self._check_buttons = []
        inner = QWidget(self)
        page_lay = QVBoxLayout(inner)
        page_lay.setContentsMargins(14, 10, 14, 10)
        page_lay.setSpacing(8)
        intro = muted(_("Zypp Desk looks at the software of this computer and tells you what is worth a look, from "
                        "the most to the least important."), inner)
        page_lay.addWidget(intro)
        # the findings, one row each (health_rows), then what takes most room
        self.health_box = QVBoxLayout()
        self.health_box.setSpacing(8)
        page_lay.addLayout(self.health_box)
        page_lay.addSpacing(10)
        page_lay.addWidget(heading(_("Maintenance tasks"), 1.05, inner))
        grid = QGridLayout()
        page_lay.addLayout(grid)
        page_lay.addStretch(1)
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
                                 lambda _c=False, k=key, a=run, t=title: self._run(k, a, t)))
            row.addStretch(1)
            card.lay.addLayout(row)
            grid.addWidget(card, i // 2, i % 2)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)

    def _run(self, key, args, title):
        """The commands that change the system ask first, in words; the ones that only
        read (ps, needs-rebooting, licenses) run at once."""
        st = Step("zypper", ["-v", *(["-n"] if args[0] != "-n" else []), *args])
        if key == "purge":  # real numbers first: rpm's database, zypp.conf and snapper, read in the background
            def asked(res, err):
                plan, snapshots = res if not err and res else (None, False)
                self._ask_then_run(st, title, self._purge_question(st, plan, snapshots))
            run_async(lambda: (kernel_plan(), diskspace.snapshots_in_use()), on_done=asked)
            return
        if key == "clean":
            run_clean_cache(self, st, title)
            return
        if key == "cleanlocks":
            if ask_clean_locks(self, st):
                self._start(st, title)
            return
        self._ask_then_run(st, title, self._question(key, st))

    def _ask_then_run(self, st, title, q):
        if q is None or question.confirm(self, **{"help_topic": "problems", **q}):
            self._start(st, title)

    def _start(self, st, title):
        # the page in front, the others and the tray look again afterwards (old kernels gone, new packages…)
        self.ctx.run([st], title, on_done=lambda ok: self.ctx.after_change())

    @staticmethod
    def _question(key, st):
        """The question for a maintenance command, as question.confirm's arguments; None for one that only reads."""
        cmds = [st.display()]
        if key == "verify":
            return dict(title=_("Check and fix the dependencies?"), yes=_("Yes, check them"), icon_name="tools",
                        items=[("tools", _("Check that every installed package has what it needs"),
                                _("If something is missing or in conflict, zypper installs or removes packages to "
                                  "fix it, without asking again."))],
                        means=_("most of the time there is nothing to fix. “Simulate” shows first what it would "
                                "change."), commands_=cmds)
        if key == "inr":
            return dict(title=_("Install the new recommended packages?"), yes=_("Yes, install them"),
                        icon_name="tools",
                        items=[("package-install|system-software-install",
                                _("Install what your programs recommend now"),
                                _("Only what they started to recommend after you installed them: translations, "
                                  "plugins…"))],
                        means=_("“Simulate” shows first which packages and how much space they take."),
                        commands_=cmds)
        return None

    @staticmethod
    def _purge_question(st, plan, snapshots=False):
        """snapshots: snapper keeps a snapshot of / before each change, and with it the old kernels' files."""
        cmds = [st.display()]
        base = dict(title=_("Remove the old kernels?"), icon_name="tools", commands_=cmds)
        means = _("the computer starts the same. The boot menu will have fewer versions to choose from.")
        if not plan or plan["remove"] is None:
            rule = (plan or {}).get("rule")
            return dict(base, yes=_("Yes, remove them"),
                        items=[("computer", _("Remove the old Linux kernels"),
                                _("The ones zypp.conf does not keep: {rule}.").format(rule=rule) if rule else
                                _("The ones zypp.conf does not keep (multiversion.kernels)."))],
                        means=means)
        kept = sorted(plan["keep"], key=plan["installed"].index, reverse=True)
        kept_text = ", ".join(_("{version} (in use)").format(version=e) if e == plan["running"] else e for e in kept)
        n = len(plan["remove"])
        if not n:
            # nothing to do: say so, and leave running it anyway as a plain choice
            total = len(plan["installed"])
            return dict(base, title=_("No old kernel to remove"), password=False, yes=_("Run it anyway"),
                        buttons=[("no", _("Close"), False), ("yes", _("Run it anyway"), False)],
                        items=[("computer", ngettext("zypp.conf keeps your kernel", "zypp.conf keeps all your {n} "
                                                     "kernels", total).format(n=total), kept_text)],
                        means=_("nothing needs to be done. If you run it anyway, it only cleans up what is left of "
                                "kernels that are gone, and it asks for the administrator password."))
        items = [("computer", ngettext("Remove {n} old Linux kernel", "Remove {n} old Linux kernels",
                                       n).format(n=n),
                  _("These stay: {list}.").format(list=kept_text), list(plan["remove"]))]
        if snapshots:
            # the snapshots keep every file a change removes
            items.append(("drive-harddisk", _("No space comes back yet"), _(
                "The snapshots of the system keep the old kernels' files. About {size} comes back when the "
                "snapshots from before this change are deleted: snapper deletes old ones by itself, or you can "
                "in History.").format(size=nice_bytes(plan["size"]))))
        else:
            items.append(("drive-harddisk", _("Free about {size}").format(size=nice_bytes(plan["size"])),
                          _("On the system partition.")))
        return dict(base, yes=_("Yes, remove them"), items=items, means=means)

    @staticmethod
    def _clean_question(st, sizes):
        return clean_cache_question(st, sizes)  # Repositories → More asks the same

    # ------------------------------------------------------------ health check (health.py)
    def header_actions(self):
        b = button(_("Check again"), "view-refresh", lambda: self.check_health(refresh=True))
        b.setToolTip(_("Look at the computer again: what was installed, removed or changed since."))
        self._check_buttons = [b]
        self._busy_buttons(self._health_busy)
        return [b]

    def _busy_buttons(self, busy):
        for b in self._check_buttons:
            try:
                b.setEnabled(not busy)
                b.setText(_("Checking…") if busy else _("Check again"))
            except RuntimeError:  # the button of a header that is gone
                pass

    def reload(self):
        # the first look reuses what another page may have read this session; after a change, everything again
        self.check_health(refresh=self._health_checked)
        self._health_checked = True

    def check_health(self, refresh=True):
        from .. import health
        self._health_round += 1
        rnd = self._health_round
        self._health_busy = True
        self._busy_buttons(True)
        if not self.health_box.count():  # the first time: say that it is looking, where the rows will be
            self.health_box.addWidget(health_row(None, _("Looking at the computer…"),
                                                 _("It takes a few seconds: packages, repositories, keys, firmware "
                                                   "and Flatpak apps."), parent=self))
        run_async(health.run_all, self.ctx.settings, refresh,
                  on_done=lambda res, err, r=rnd: self._health_done(r, res, err))

    def _health_done(self, rnd, res, err):
        if rnd != self._health_round:
            return  # an older look, overtaken by a newer one
        self._health_busy = False
        self._busy_buttons(False)
        self._health = res
        clear_layout(self.health_box)
        if err or not res:
            self.health_box.addWidget(health_row("low", _("Zypp Desk could not look at the computer"),
                                                 str(err or ""), parent=self))
            return
        names = health_check_names()
        for f in res["findings"]:
            self.health_box.addWidget(health_row(
                f["severity"], f["title"], f["text"], f["details"],
                (f["button"], lambda _c=False, f=f: self._health_act(f)) if f.get("action") and f.get("button")
                else None, parent=self))
        if res["clean"]:
            self.health_box.addWidget(health_row(
                "ok", _("Nothing to look at in: {checks}").format(
                    checks=", ".join(names.get(k, k) for k in res["clean"])),
                _("Checked just now."), parent=self))
        if res["errors"]:
            self.health_box.addWidget(health_row(
                "low", _("Could not check: {checks}").format(checks=", ".join(names.get(k, k) for k in res["errors"])),
                _("Check again later; if it keeps failing, the details say why."),
                [f"{names.get(k, k)}: {v}" for k, v in res["errors"].items()], parent=self))
        if res.get("room"):
            self.health_box.addWidget(self._room_card(res["room"]))

    def _room_card(self, room):
        """What takes most room: the biggest packages, Flatpak, the downloaded packages, old kernels. Each name is a
        link to the place where it can be removed; "Show all" opens the longer list."""
        card = Card(_("What takes most room"), self)
        self._room_names = [name for name, _label, _size in room["packages"]]
        pkgs = [(f"pkg:{i}", label if label == name else f"{label} ({name})", size)
                for i, (name, label, size) in enumerate(room["packages"])]
        short = [(f"pkg:{i}", label, size) for i, (_name, label, size) in enumerate(room["packages"][:4])]
        extra = [(f"act:{it['action']}", it["label"], it["bytes"]) for it in room["items"]]

        def link(href, label, size=None):
            text = f'<a href="{href}" {PLAIN_LINK}>{html.escape(label)}</a>'
            return text if size is None else f"{text} {html.escape(nice_bytes(size))}"
        more, less = _("▸ Show all"), _("▾ Show less")
        line = QLabel(card)
        line.setTextFormat(Qt.TextFormat.RichText)
        line.setWordWrap(True)
        full = QLabel(card)
        full.setTextFormat(Qt.TextFormat.RichText)
        full.setWordWrap(True)
        full.setText("<br>".join(link(*e) for e in pkgs) + ("<br><br>" + "<br>".join(link(*e) for e in extra)
                                                             if extra else ""))
        full.setVisible(False)

        def draw():
            line.setText(" · ".join(link(*e) for e in short + extra) + " · "
                         + link("more", less if full.isVisible() else more))

        def clicked(href):
            if href == "more":
                full.setVisible(not full.isVisible())
                draw()
                return
            self._room_act(href)
        draw()
        for lbl in (line, full):
            lbl.linkActivated.connect(clicked)
            card.lay.addWidget(lbl)
        return card

    def _room_act(self, href):
        kind, _sep, what = href.partition(":")
        if kind == "pkg" and what.isdigit() and int(what) < len(self._room_names):
            self._show_package(self._room_names[int(what)])
        elif href == "act:flatpak":
            self.ctx.go("flatpak")
        elif href == "act:clean":
            run_clean_cache(self)
        elif href == "act:purge":
            self._maintenance("purge")

    def _maintenance(self, key):
        """Run one of the maintenance commands above, with its question."""
        for k, title, _desc, _sim, run in MAINTENANCE:
            if k == key:
                self._run(k, run, title)
                return

    def _show_package(self, name):
        page = self.ctx.pages.get("packages")
        self.ctx.go("packages")
        if page is not None and hasattr(page, "term"):
            page.term.setText(name)
            page.search()

    def _show_app(self, name):
        page = self.ctx.pages.get("apps")
        self.ctx.go("apps")
        if page is None:
            return
        if hasattr(page, "_switch") and getattr(page, "b_mine", None) is not None:
            page.b_mine.setChecked(True)
            page._switch(0)
        if getattr(page, "mine_search", None) is not None:
            page.mine_search.setText(name)

    def _health_act(self, f):
        """A finding's button: it opens the right place, or asks and puts the changes in the queue."""
        action, data = f.get("action"), f.get("data") or {}
        if action == "firmware":
            if "drivers" in getattr(self.ctx, "pages", {}):
                self.ctx.go("drivers")  # the page of the hardware shows it with the other drivers
            else:
                self._ask_firmware(data["plan"])
        elif action in ("show-app", "duplicates"):
            self._show_app(data.get("name") or (data.get("names") or [""])[0])
        elif action == "vendor":
            self._ask_vendor(data.get("packages") or [])
        elif action == "repos":
            self.ctx.go("repos")
        elif action == "keys":
            self.ctx.go("keys")
        elif action == "orphans":
            page = self.ctx.pages.get("packages")
            self.ctx.go("packages")
            if page is not None and hasattr(page, "search"):
                page.search(special="orphaned")
        elif action == "verify":
            self._maintenance("verify")

    def _ask_firmware(self, plan):
        from .. import health
        from ..txqueue import Action
        changes = health.firmware_changes(plan)
        if not question.confirm(self, **firmware_question(plan, changes)):
            return
        with self.ctx.queue.batch():
            for kind, target, opts in changes:
                self.ctx.queue.add(Action(kind, target, **opts))

    def _ask_vendor(self, packages):
        from ..txqueue import Action
        q = vendor_question(packages)
        if q is None:
            info_text = _("Your repositories only have older versions of them: leave them as they are for now.")
            question.confirm(self, _("Packages from another vendor"), _("Close"), password=False,
                             buttons=[("no", _("Close"), True)],
                             items=[("package-x-generic", _("Nothing to switch"), info_text,
                                     [vendor_line(p) for p in packages])])
            return
        if not question.confirm(self, **q):
            return
        with self.ctx.queue.batch():
            for p in packages:
                if not p.get("downgrade"):
                    # the same program version with a lower release (rebuilt by another vendor) needs --oldpackage
                    opts = {"oldpackage": True} if p.get("older") else {}
                    self.ctx.queue.add(Action("install", f"{p['alias']}:{p['name']}", **opts))


def clean_cache_question(st, sizes):
    """The question before 'zypper clean --all', as question.confirm's arguments (Maintenance, and Repositories →
    More: one question for the same command). sizes: zypp_cache_sizes(), or None."""
    items = [("package-x-generic|application-x-rpm", _("Delete the downloaded packages"),
              _("The packages zypper keeps: those of repositories set to keep them, and those downloaded ahead for "
                "the next update.")),
             ("view-refresh", _("Delete the lists of packages of every repository"),
              _("zypper downloads them again the next time it refreshes."))]
    if sizes and (sizes["packages"] or sizes["metadata"]):
        parts = []
        if sizes["packages"]:
            parts.append(_("Downloaded packages: {size}.").format(size=nice_bytes(sizes["packages"])))
        if sizes["metadata"]:
            parts.append(_("Repositories' information: {size}.").format(size=nice_bytes(sizes["metadata"])))
        items.append(("drive-harddisk", _("Free about {size}").format(
            size=nice_bytes(sizes["packages"] + sizes["metadata"])), " ".join(parts)))
    return dict(title=_("Clean the cache?"), yes=_("Yes, clean it"), icon_name="edit-clear",
                window_title=_("Clean the cache"), items=items,
                means=_("nothing installed changes. The next refresh downloads every repository's information "
                        "again, which takes a while."), commands_=[st.display()], password=st.root,
                help_topic="problems")


def run_clean_cache(page, st=None, title=None):
    """Measure the cache (in the background), ask, and run 'zypper clean --all' (Maintenance, Repositories)."""
    st = st or Step("zypper", ["-v", "-n", "clean", "--all"])
    title = title or _("Clean the cache")

    def ask(sizes, err):
        if question.confirm(page, **clean_cache_question(st, None if err else sizes)):
            page.ctx.run([st], title, on_done=lambda ok: page.ctx.after_change())
    run_async(zypp_cache_sizes, on_done=ask)


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
    COLUMN = 800  # one width for every card, short lines

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
        jump = QLabel(band)  # links to the other cards, which are further down ("Terminal · Notifications ▸")
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
        col.addWidget(self._move_card(column))
        col.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(column, 1)
        lay.addStretch(0)
        for cb, key in ((self.no_rec, "apply_no_recommends"), (self.lic, "apply_accept_licenses"),
                        (self.fold, "fold_repeated_lines"), (self.after, "after_routine_check_restart")):
            cb.toggled.connect(lambda on, k=key: self._set(k, on))
        self._show_band_cmd()

    def _move_card(self, parent):
        """Move to another computer: this computer's software saved in a file, which Zypp Desk opens on the other
        one and puts in the queue (pages/migrate.py)."""
        from .migrate import open_export, open_import
        card = Card(_("Move to another computer"), parent)
        card.lay.addWidget(muted(_("Save in a file the repositories, programs and settings of this computer. On the "
                                   "other computer, open the file with Zypp Desk: everything goes to the queue, for "
                                   "you to look at before applying it. The file has no passwords or documents.")))
        row = QHBoxLayout()
        row.addWidget(button(_("Save this computer's software…"), "document-export",
                             lambda: open_export(self.ctx, self)))
        row.addWidget(button(_("Import it from a file…"), "document-import", lambda: open_import(self.ctx, self)))
        row.addStretch(1)
        card.lay.addLayout(row)
        return card

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


# ============================================================ health check rows (Maintenance)
HEALTH_GREY = "#7f8c8d"
PLAIN_LINK = 'style="text-decoration: none;"'  # links in a sentence, without the underline


def health_check_names():
    from .. import health
    return health.check_names()


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def health_row(severity, title, text="", details=(), action=None, parent=None):
    """One finding: a coloured dot (its importance), the title, one plain sentence, "▸ Details" when there are
    some, and a button on the right. action: (text, slot) or None. Every text is plain text (package and
    repository names come from outside)."""
    from PySide6.QtWidgets import QFrame
    from ..style import GREEN, ORANGE, RED
    row = QFrame(parent)
    row.setObjectName("card")
    row.setFrameShape(QFrame.Shape.StyledPanel)
    h = QHBoxLayout(row)
    h.setContentsMargins(14, 10, 12, 10)
    h.setSpacing(12)
    dot = QLabel(row)
    dot.setFixedSize(10, 10)
    color = {"high": RED, "medium": ORANGE, "low": HEALTH_GREY, "ok": GREEN}.get(severity)
    dot.setStyleSheet(f"background: {color}; border-radius: 5px;" if color else "background: transparent;")
    dot_box = QVBoxLayout()
    dot_box.setContentsMargins(0, 5, 0, 0)
    dot_box.addWidget(dot)
    dot_box.addStretch(1)
    h.addLayout(dot_box)
    v = QVBoxLayout()
    v.setSpacing(2)
    t = QLabel(row)
    t.setTextFormat(Qt.TextFormat.PlainText)
    t.setWordWrap(True)
    t.setText(title)
    f = t.font()
    f.setBold(True)
    t.setFont(f)
    v.addWidget(t)
    if text:
        m = muted("", row)
        m.setTextFormat(Qt.TextFormat.PlainText)
        m.setText(text)
        v.addWidget(m)
    if details:
        lines = list(details)
        body = muted("", row)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setText("\n".join(lines[:400] + (["…"] if len(lines) > 400 else [])))
        body.setVisible(False)
        toggle = QLabel(row)
        toggle.setTextFormat(Qt.TextFormat.RichText)
        show, hide = _("▸ Details"), _("▾ Hide the details")

        def flip(_href, body=body, toggle=toggle):
            body.setVisible(not body.isVisible())
            toggle.setText(f'<a href="x" {PLAIN_LINK}>{html.escape(hide if body.isVisible() else show)}</a>')
        toggle.setText(f'<a href="x" {PLAIN_LINK}>{html.escape(show)}</a>')
        toggle.linkActivated.connect(flip)
        v.addWidget(toggle)
        v.addWidget(body)
    h.addLayout(v, 1)
    if action:
        label, slot = action
        b = button(label, None, slot, parent=row)
        # beside the title and its sentence, also when the details are open
        h.addWidget(b, 0, Qt.AlignmentFlag.AlignTop if details else Qt.AlignmentFlag.AlignVCenter)
    return row


def firmware_question(plan, changes):
    """The question before the firmware this computer does not use goes into the queue (question.confirm's
    arguments). changes: health.firmware_changes(plan)."""
    from .. import health
    what = health.firmware_for()
    sizes = plan.get("sizes") or {}
    remove = plan["remove"]
    n = len(remove)
    lines = [f"{name} ({nice_bytes(sizes.get(name, 0))})" + (f": {what[name]}" if name in what else "")
             for name, _why in remove]
    items = [("package-remove|edit-delete",
              ngettext("Remove {n} firmware package", "Remove {n} firmware packages", n).format(n=n),
              _("No device of this computer uses them. Frees about {size}.").format(
                  size=nice_bytes(plan["size_freed"])), lines)]
    locks = [target for kind, target, _opts in changes if kind == "lock"]
    if locks:
        items.append(("object-locked", _("Lock {name}").format(name=locks[0]), _(
            "The kernel recommends it: without the lock, the next kernel update would install it again, and all the "
            "firmware with it. The lock appears in Locks, where you can remove it.")))
    kept = list(plan["keep"]) + list(plan["unsure"])
    items.append(("dialog-ok-apply|emblem-ok",
                  ngettext("Keep {n} firmware package", "Keep {n} firmware packages", len(kept)).format(n=len(kept)),
                  _("What the devices of this computer use, and what a dock or an adapter may need."),
                  [f"{name}: {why}" for name, why in kept]))
    warning = None
    if plan.get("lock") and not locks:
        warning = _("Zypp Desk could not tell which repository has kernel-firmware-all, so it is not locked: the "
                    "next kernel update may install all the firmware again. Lock it in Locks after applying.")
    return dict(title=_("Keep only the firmware this computer needs?"), yes=_("Put it in the queue"),
                icon_name="cpu|computer", window_title=_("Firmware"),
                lead=_("Nothing changes yet: the queue shows the exact zypper commands, and Simulate shows what they "
                       "would do."),
                items=items, warning=warning, password=False, help_topic="drivers",
                means=_("the devices of this computer keep working. A device you plug in later may need firmware "
                        "that is gone: each firmware package says which devices it is for, so “Install new "
                        "recommended packages” (zypper inr) puts the right one back once the device is plugged in."))


def vendor_line(p):
    line = _("{name}: {vendor} → “{repo}” {version}").format(
        name=p["name"], vendor=p.get("vendor") or _("no vendor"), repo=p["repo"], version=p["cand_edition"])
    return line + ("  " + _("(an older version)") if p.get("downgrade") else "")


def vendor_question(packages):
    """The question before packages left from another vendor go back to the vendor of their repository
    (question.confirm's arguments), or None when no package can switch (only older versions)."""
    switch = [p for p in packages if not p.get("downgrade")]
    if not switch:
        return None
    n = len(switch)
    repos = sorted({p["repo"] for p in switch})
    items = [("package-install|system-software-install",
              ngettext("Install {n} package again from its repository",
                       "Install {n} packages again from their repositories", n).format(n=n),
              ngettext("From {repos}. From then on zypper updates it from there.",
                       "From {repos}. From then on zypper updates them from there.", n).format(repos=", ".join(repos)),
              [vendor_line(p) for p in switch])]
    left = [p for p in packages if p.get("downgrade")]
    if left:
        items.append(("package-x-generic",
                      ngettext("Leave {n} package as it is", "Leave {n} packages as they are", len(left)).format(
                          n=len(left)),
                      _("Your repositories only have older versions of them."), [vendor_line(p) for p in left]))
    return dict(title=_("Switch these packages to their repository's vendor?"), yes=_("Put it in the queue"),
                icon_name="system-software-update", items=items, password=False, help_topic="health",
                lead=_("Nothing changes yet: Simulate shows what else would change with them."),
                means=_("the programs stay; only the place their updates come from changes. The version of the "
                        "repository may be built differently (with other codecs, for example)."))
