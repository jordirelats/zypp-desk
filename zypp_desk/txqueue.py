# SPDX-License-Identifier: GPL-3.0-or-later
"""The queue of pending changes: packages, locks, repositories and locales.

Nothing is changed until the queue is applied. The queue turns itself into the exact zypper
commands it will run, so the window can always show them.
"""

import re
import shlex

from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal

from .i18n import _, ngettext
from .runner import Step, rpm_major

PKG_KINDS = ("install", "remove")
# What zypper accepts before a ':' as the kind of a solvable ('pattern:kde'). Anything else before a ':' is the
# alias of a repository ('packman:vlc': vlc from that repository).
SOLVABLE_KINDS = ("package", "patch", "pattern", "product", "srcpackage", "application")
# A repository whose address changes is added again under its alias plus this, then takes the old one's alias.
# The change-address dialog writes its .repo file with the section [<alias>-zdnew].
REPO_URL_TEMP_SUFFIX = "-zdnew"


def is_file_target(target, opts=None):
    """True for the install of a downloaded file (a path or an .rpm address), not of a package from a
    repository."""
    return (target.startswith("/") or target.endswith(".rpm") or "://" in target
            or bool((opts or {}).get("sha256")))


def pkg_ident(action):
    """What an install or a removal is about, as zypper's solver names it: 'vlc' for 'vlc', 'vlc=3.0',
    'packman:vlc' (vlc from that repository) or a downloaded vlc file; 'pattern:kde' for a pattern."""
    t = action.target
    if is_file_target(t, action.opts):
        return action.opts.get("package") or t
    t = re.split(r"[<>=]", t, maxsplit=1)[0].strip()
    head, sep, rest = t.partition(":")
    if sep and head in SOLVABLE_KINDS:
        return rest if head == "package" else t
    if sep and "(" not in t:  # 'alias:name' (an alias may have ':' too); not a capability like 'perl(A::B)'
        return t.rpartition(":")[2]
    return t


class Action:
    def __init__(self, kind, target, **opts):
        self.kind = kind  # install, remove, lock, unlock, repo-add, repo-modify, repo-remove, locale-add, locale-remove
        self.target = target
        self.opts = opts

    def describe(self):
        k, t = self.kind, self.target
        if k == "install" and is_file_target(t, self.opts):
            return _("Install {name} from the downloaded file {file}").format(name=self.opts.get("package") or t,
                                                                             file=t)
        if k == "install":
            return _("Install {name}").format(name=t)
        if k == "remove":
            return _("Remove {name}").format(name=t)
        if k == "lock":
            return _("Lock {name}").format(name=t)
        if k == "unlock":
            return _("Unlock {name}").format(name=t)
        if k == "repo-add":
            return _("Add repository {alias}").format(alias=t)
        if k == "repo-remove":
            return _("Remove repository {alias}").format(alias=t)
        if k == "repo-modify":
            return _("Change repository {alias}: {changes}").format(alias=t, changes=", ".join(
                _repo_change_text(key, val) for key, val in self.opts.items()))
        if k == "locale-add":
            return _("Add language {code}").format(code=t)
        if k == "locale-remove":
            return _("Remove language {code}").format(code=t)
        if k == "flatpak-install":
            return _("Install the Flatpak app {app} from {remote} ({installation})").format(
                app=t, remote=self.opts.get("remote", ""), installation=installation_label(self.opts.get("installation")))
        if k == "flatpak-uninstall":
            return _("Uninstall the Flatpak app {app}").format(app=t)
        if k == "flatpak-update":
            return _("Update every Flatpak app") if t == "*" else _("Update the Flatpak app {app}").format(app=t)
        if k == "key-import":
            return _("Import the signing key of {name} ({keyid})").format(name=self.opts.get("name") or "?",
                                                                         keyid=t[-8:])
        if k == "key-remove":
            return _("Delete the signing key of {name} ({keyid})").format(name=self.opts.get("name") or "?",
                                                                         keyid=t[-8:])
        if k == "zypp-conf":
            st = self.opts.get("settings") or {}
            if not st:
                return _("zypp.conf: back to openSUSE's settings")
            return _("zypp.conf: {changes}").format(changes=", ".join(f"{a} = {b}" for a, b in st.items()))
        if k == "firmware":
            return _("Update the firmware of {name} to {version}").format(name=self.opts.get("name") or t,
                                                                         version=self.opts.get("version") or "?")
        if k == "repo-url":
            return _("Change the address of repository {alias} to {url}").format(alias=t, url=self.opts.get("url"))
        return f"{k} {t}"


def _repo_change_text(key, val):
    if key == "priority":
        return _("priority {n}").format(n=val)
    if key == "name":
        return _("name «{name}»").format(name=val)
    labels = {"enabled": (_("enable"), _("disable")), "autorefresh": (_("refresh automatically"),
              _("do not refresh automatically")), "keeppackages": (_("keep downloaded packages"),
              _("do not keep downloaded packages"))}
    on, off = labels.get(key, (key, "no " + key))
    return on if val else off


def _addrepo_args(o, alias=None):
    """zypper addrepo options for a repository's settings (a new one, or one added again at a new address)."""
    args = ["addrepo", "--name", o.get("name") or alias or ""]
    if o.get("autorefresh", True):
        args.append("--refresh")
    if o.get("enabled") is False:
        args.append("--disable")
    if o.get("keeppackages"):
        args.append("--keep-packages")
    if o.get("priority") not in (None, 99):
        args += ["--priority", str(o["priority"])]
    gpg = o.get("gpg", "default")
    if gpg == "allow-unsigned-repo":
        args.append("--gpgcheck-allow-unsigned-repo")
    elif gpg == "off":
        args.append("--no-gpgcheck")
    return args


def installation_label(inst):
    return _("for every user") if inst != "user" else _("only for you")


def _key(action):
    """What an action is about: the package for 'vlc', 'vlc=3.0' or 'packman:vlc', 'pattern:devel_basis' for that
    pattern, the target of the other kinds."""
    if action.kind in PKG_KINDS:
        return pkg_ident(action)
    return action.opts.get("package") or action.target.split("=")[0]


class TxQueue(QObject):
    changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.actions = []
        self._batch = 0
        self._dirty = False

    @contextmanager
    def batch(self):
        """Several changes, one 'changed' signal at the end (every page redraws once, not once per change)."""
        self._batch += 1
        try:
            yield self
        finally:
            self._batch -= 1
            if self._batch == 0 and self._dirty:
                self._dirty = False
                self.changed.emit()

    def _changed(self):
        if self._batch:
            self._dirty = True
        else:
            self.changed.emit()

    # ---------------------------------------------------------- editing
    def add(self, action):
        if action.kind == "repo-modify":
            for a in self.actions:
                # the repository added again at its new address takes the change with its other settings; one
                # added from a .repo file cannot: the change runs after it, as a change of its own
                if a.kind == "repo-url" and a.target == action.target and not a.opts.get("repo_file"):
                    a.opts.update(action.opts)
                    self._changed()
                    return
            for a in self.actions:
                if a.kind == "repo-modify" and a.target == action.target:
                    a.opts.update(action.opts)
                    self._changed()
                    return
        opposite = {"install": "remove", "remove": "install", "lock": "unlock", "unlock": "lock",
                    "locale-add": "locale-remove", "locale-remove": "locale-add",
                    "flatpak-install": "flatpak-uninstall", "flatpak-uninstall": "flatpak-install",
                    "key-import": "key-remove", "key-remove": "key-import"}.get(action.kind)
        if action.kind == "repo-url" and not action.opts.get("repo_file"):
            opposite = "repo-modify"  # a new address replaces a pending change: its settings carry it
        key = _key(action)
        self.actions = [a for a in self.actions if not (_key(a) == key and a.kind in (action.kind, opposite))]
        self.actions.append(action)
        self._changed()

    def discard(self, kind, target):
        n = len(self.actions)
        self.actions = [a for a in self.actions if not (a.kind == kind and a.target == target)]
        if len(self.actions) != n:
            self._changed()

    def discard_action(self, action):
        if action in self.actions:
            self.actions.remove(action)
            self._changed()

    def clear(self):
        if self.actions:
            self.actions = []
            self._changed()

    def state_of(self, name):
        for a in self.actions:
            if a.kind in PKG_KINDS and name in (a.opts.get("package") or a.target.split("=")[0], pkg_ident(a)):
                return a.kind  # also 'packman:vlc' (queued from Search online) for vlc
        return None

    def flatpak_state(self, app):
        for a in self.actions:
            if a.kind.startswith("flatpak-") and a.target in (app, "*"):
                return a.kind
        return None

    def repo_change(self, alias):
        return next((a for a in self.actions if a.kind.startswith("repo-") and a.target == alias), None)

    def key_state(self, fpr):
        return next((a.kind for a in self.actions if a.kind in ("key-import", "key-remove") and a.target == fpr), None)

    def __len__(self):
        return len(self.actions)

    def of(self, *kinds):
        return [a for a in self.actions if a.kind in kinds]

    # ---------------------------------------------------------- commands
    def not_simulable(self):
        """Installs that name a repository still waiting in the queue ('alias:package'): zypper does not know
        that repository until the queue runs, so a dry run would only say it cannot find them."""
        new_aliases = {a.target for a in self.of("repo-add")}
        return [a for a in self.of("install") if ":" in a.target and a.target.split(":", 1)[0] in new_aliases]

    def package_args(self, dry_run=False):
        """The zypper commands for packages, each one a list of arguments. The dry run (simulation) is the same
        transaction as the real one.

        - Installs and removals go together, as one transaction: 'zypper install -- new '!old''. zypper
          resolves them at once, so a new package that conflicts with one leaving is fine. In this form a
          removal does not take away what depends on the package: zypper looks for something else that
          provides it, and stops with a problem when nothing does. (zypp.conf's solver.cleandepsOnRemove
          applies to these removals: 'install' has no --no-clean-deps.)
        - Removals alone: 'zypper remove --no-clean-deps', which also takes away what depends on them (the
          Remove window shows it), and nothing else even when zypp.conf says to clean dependencies.
        - Removals with "Also remove what only it needed" go first, on their own: only 'zypper remove' has
          --clean-deps.
        '--' before the names: no name is read as an option."""
        skip = self.not_simulable() if dry_run else []
        every_inst = self.of("install")
        inst = [a for a in every_inst if a not in skip]
        rem = [a.target for a in self.of("remove") if not a.opts.get("clean_deps")]
        clean = [a.target for a in self.of("remove") if a.opts.get("clean_deps")]
        dry = ["--dry-run"] if dry_run else []
        out = []
        if clean:
            out.append(["remove", "--clean-deps", *dry, "--", *clean])
        if every_inst:  # also when the dry run leaves the installs out: the real run is an install
            names = [a.target for a in inst] + ["!" + r for r in rem]
            if names:
                args = ["install"]
                if any(a.opts.get("oldpackage") for a in inst):
                    args.append("--oldpackage")
                if any(a.opts.get("allow_unsigned") for a in inst):
                    args.append("--allow-unsigned-rpm")
                if self.settings.get("apply_no_recommends"):
                    args.append("--no-recommends")
                if self.settings.get("apply_accept_licenses"):
                    args.append("--auto-agree-with-licenses")
                out.append(args + dry + ["--"] + names)
        elif rem:
            out.append(["remove", "--no-clean-deps", *dry, "--", *rem])
        return out

    def steps(self):
        """Every command, in order: signing keys and zypp.conf first, then repositories, locks, packages,
        languages, Flatpak, keys to delete and, last, firmware (it may want a restart)."""
        out = []
        for a in self.of("key-import"):
            out.append(Step("rpm-import", [a.opts["path"], a.target]))
        for a in self.of("zypp-conf"):
            st = a.opts.get("settings") or {}
            out.append(Step("zypp-conf", [f"{k}={v}" for k, v in st.items()] if st else ["--reset"]))
        for a in self.of("repo-url"):
            # zypper cannot change an address. The repository is added again first, under a temporary alias,
            # with all its settings: if the new address is refused, the old repository is still there. Then
            # the old one goes and the new one takes its alias. '--' before every alias and address.
            temp = a.target + REPO_URL_TEMP_SUFFIX
            if a.opts.get("repo_file"):  # every setting, also the key (gpgkey=), in a .repo file
                out.append(Step("zypper", ["-n", "addrepo", "--", a.opts["repo_file"]]))
            else:
                out.append(Step("zypper", ["-n", *_addrepo_args(a.opts, a.target), "--", a.opts["url"], temp]))
            out.append(Step("zypper", ["-n", "removerepo", "--", a.target]))
            out.append(Step("zypper", ["-n", "renamerepo", "--", temp, a.target]))
        for a in self.of("repo-add"):
            out.append(Step("zypper", ["-n", *_addrepo_args(a.opts, a.target), "--", a.opts["url"], a.target]))
        for a in self.of("repo-modify"):
            args = ["modifyrepo"]
            o = a.opts
            if "enabled" in o:
                args.append("--enable" if o["enabled"] else "--disable")
            if "autorefresh" in o:
                args.append("--refresh" if o["autorefresh"] else "--no-refresh")
            if "keeppackages" in o:
                args.append("--keep-packages" if o["keeppackages"] else "--no-keep-packages")
            if "priority" in o:
                args += ["--priority", str(o["priority"])]
            if "name" in o:
                args += ["--name", o["name"]]
            out.append(Step("zypper", ["-n", *args, "--", a.target]))
        rr = [a.target for a in self.of("repo-remove")]
        if rr:
            out.append(Step("zypper", ["-n", "removerepo", "--", *rr]))
        for kind, sub in (("unlock", "removelock"), ("lock", "addlock")):
            groups = {}
            for a in self.of(kind):
                groups.setdefault((a.opts.get("type", "package"), a.opts.get("repo", "")), []).append(a.target)
            for (typ, repo), names in groups.items():
                out.append(Step("zypper", ["-n", sub, *([] if typ == "package" else ["-t", typ]),
                                           *(["-r", repo] if repo else []), "--", *names]))
        for pkg in self.package_args():
            out.append(Step("zypper", ["-n", *pkg]))
        for kind, sub in (("locale-add", "addlocale"), ("locale-remove", "removelocale")):
            codes = [a.target for a in self.of(kind)]
            if codes:
                out.append(Step("zypper", ["-n", sub, "--", *codes]))
        out += self.flatpak_steps()
        for a in self.of("key-remove"):
            nvr = a.opts.get("nvr") or ""
            key = nvr[len("gpg-pubkey-"):] if nvr.startswith("gpg-pubkey-") and rpm_major() < 6 else a.target
            out.append(Step("key-remove", [a.target], shown=f"rpmkeys --delete {key}"))
        for a in self.of("firmware"):
            # --no-unreported-check: --assume-yes would otherwise say yes to sending reports of earlier updates
            out.append(Step("fwupdmgr", ["update", a.target, "--assume-yes", "--no-reboot-check",
                                         "--no-unreported-check"]))
        for i, s in enumerate(out):
            s.cond = "always" if i == 0 else "success"
        return out

    def flatpak_steps(self):
        out, groups = [], {}
        for a in self.of("flatpak-install"):
            groups.setdefault((a.opts.get("installation", "system"), a.opts.get("remote", "flathub")), []).append(a.target)
        for (inst, remote), apps in groups.items():
            out.append(Step("flatpak", ["install", "--noninteractive", "-y", f"--{inst}", "--", remote, *apps]))
        groups = {}
        for a in self.of("flatpak-uninstall"):
            groups.setdefault((a.opts.get("installation", "system"), bool(a.opts.get("delete_data"))), []).append(a.target)
        for (inst, delete), apps in groups.items():
            out.append(Step("flatpak", ["uninstall", "--noninteractive", "-y", f"--{inst}",
                                        *(["--delete-data"] if delete else []), "--", *apps]))
        ups = [a.target for a in self.of("flatpak-update")]
        if ups:
            out.append(Step("flatpak", ["update", "--noninteractive", "-y", *([] if "*" in ups else ["--", *ups])]))
        return out

    def simulation_steps(self):
        """Dry runs of the package commands: the same transactions that Apply runs (see package_args)."""
        return [Step("zypper", ["--xmlout", "-n", *pkg], lang="C", capture=True, cond="always")
                for pkg in self.package_args(dry_run=True)]

    def preview(self):
        steps = self.steps()
        return " && ".join(s.display() for s in steps) if steps else ""

    def summary(self):
        n_in, n_rm = len(self.of("install")), len(self.of("remove"))
        n_fp = len([a for a in self.actions if a.kind.startswith("flatpak-")])
        others = len(self.actions) - n_in - n_rm - n_fp
        parts = []
        if n_in:
            parts.append(ngettext("{n} to install", "{n} to install", n_in).format(n=n_in))
        if n_rm:
            parts.append(ngettext("{n} to remove", "{n} to remove", n_rm).format(n=n_rm))
        if n_fp:
            parts.append(ngettext("{n} Flatpak change", "{n} Flatpak changes", n_fp).format(n=n_fp))
        if others:
            parts.append(ngettext("{n} other change", "{n} other changes", others).format(n=others))
        return parts


def quote(args):
    return " ".join(shlex.quote(a) for a in args)
