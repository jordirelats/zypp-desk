# SPDX-License-Identifier: GPL-3.0-or-later
"""The queue of pending changes: packages, locks, repositories and locales.

Nothing is changed until the queue is applied. The queue turns itself into the exact zypper
commands it will run, so the window can always show them.
"""

import shlex

from PySide6.QtCore import QObject, Signal

from .i18n import _, ngettext
from .runner import Step

PKG_KINDS = ("install", "remove")


class Action:
    def __init__(self, kind, target, **opts):
        self.kind = kind  # install, remove, lock, unlock, repo-add, repo-modify, repo-remove, locale-add, locale-remove
        self.target = target
        self.opts = opts

    def describe(self):
        k, t = self.kind, self.target
        if k == "install" and self.opts.get("package"):
            return _("Install {name} from the downloaded file {file}").format(name=self.opts["package"], file=t)
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


def installation_label(inst):
    return _("for every user") if inst != "user" else _("only for you")


def _key(action):
    """What an action is about: the package name for 'vlc', 'vlc=3.0' or 'pattern:devel_basis'."""
    return action.opts.get("package") or action.target.split("=")[0]


class TxQueue(QObject):
    changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.actions = []

    # ---------------------------------------------------------- editing
    def add(self, action):
        if action.kind == "repo-modify":
            for a in self.actions:
                if a.kind == "repo-modify" and a.target == action.target:
                    a.opts.update(action.opts)
                    self.changed.emit()
                    return
        opposite = {"install": "remove", "remove": "install", "lock": "unlock", "unlock": "lock",
                    "locale-add": "locale-remove", "locale-remove": "locale-add",
                    "flatpak-install": "flatpak-uninstall", "flatpak-uninstall": "flatpak-install"}.get(action.kind)
        key = _key(action)
        self.actions = [a for a in self.actions if not (_key(a) == key and a.kind in (action.kind, opposite))]
        self.actions.append(action)
        self.changed.emit()

    def discard(self, kind, target):
        n = len(self.actions)
        self.actions = [a for a in self.actions if not (a.kind == kind and a.target == target)]
        if len(self.actions) != n:
            self.changed.emit()

    def discard_action(self, action):
        if action in self.actions:
            self.actions.remove(action)
            self.changed.emit()

    def clear(self):
        if self.actions:
            self.actions = []
            self.changed.emit()

    def state_of(self, name):
        for a in self.actions:
            if a.kind in PKG_KINDS and (a.opts.get("package") or a.target.split("=")[0]) == name:
                return a.kind
        return None

    def flatpak_state(self, app):
        for a in self.actions:
            if a.kind.startswith("flatpak-") and a.target in (app, "*"):
                return a.kind
        return None

    def repo_change(self, alias):
        return next((a for a in self.actions if a.kind.startswith("repo-") and a.target == alias), None)

    def __len__(self):
        return len(self.actions)

    def of(self, *kinds):
        return [a for a in self.actions if a.kind in kinds]

    # ---------------------------------------------------------- commands
    def package_args(self, dry_run=False):
        inst = [a for a in self.of("install")]
        rem = [a.target for a in self.of("remove")]
        if not inst and not rem:
            return None
        if inst:
            args = ["install"]
            if any(a.opts.get("oldpackage") for a in inst):
                args.append("--oldpackage")
            if any(a.opts.get("allow_unsigned") for a in inst):
                args.append("--allow-unsigned-rpm")
            if self.settings.get("apply_no_recommends"):
                args.append("--no-recommends")
            if self.settings.get("apply_accept_licenses"):
                args.append("--auto-agree-with-licenses")
            if dry_run:
                args.append("--dry-run")
            # '!name' removes in the same transaction (see "man zypper", install).
            return args + [a.target for a in inst] + ["!" + r for r in rem]
        args = ["remove"] + (["--dry-run"] if dry_run else [])
        return args + rem

    def steps(self):
        """Every command, in order: repositories first, then locks, packages and languages."""
        out = []
        for a in self.of("repo-add"):
            o = a.opts
            args = ["addrepo", "--name", o.get("name") or a.target]
            if o.get("autorefresh", True):
                args.append("--refresh")
            if o.get("priority") not in (None, 99):
                args += ["--priority", str(o["priority"])]
            gpg = o.get("gpg", "default")
            if gpg == "allow-unsigned-repo":
                args.append("--gpgcheck-allow-unsigned-repo")
            elif gpg == "off":
                args.append("--no-gpgcheck")
            out.append(Step("zypper", ["-n", *args, o["url"], a.target]))
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
            out.append(Step("zypper", ["-n", *args, a.target]))
        rr = [a.target for a in self.of("repo-remove")]
        if rr:
            out.append(Step("zypper", ["-n", "removerepo", *rr]))
        for kind, sub in (("unlock", "removelock"), ("lock", "addlock")):
            by_type = {}
            for a in self.of(kind):
                by_type.setdefault(a.opts.get("type", "package"), []).append(a.target)
            for typ, names in by_type.items():
                out.append(Step("zypper", ["-n", sub, *([] if typ == "package" else ["-t", typ]), *names]))
        pkg = self.package_args()
        if pkg:
            out.append(Step("zypper", ["-n", *pkg]))
        for kind, sub in (("locale-add", "addlocale"), ("locale-remove", "removelocale")):
            codes = [a.target for a in self.of(kind)]
            if codes:
                out.append(Step("zypper", ["-n", sub, *codes]))
        out += self.flatpak_steps()
        for i, s in enumerate(out):
            s.cond = "always" if i == 0 else "success"
        return out

    def flatpak_steps(self):
        out, groups = [], {}
        for a in self.of("flatpak-install"):
            groups.setdefault((a.opts.get("installation", "system"), a.opts.get("remote", "flathub")), []).append(a.target)
        for (inst, remote), apps in groups.items():
            out.append(Step("flatpak", ["install", "--noninteractive", "-y", f"--{inst}", remote, *apps]))
        groups = {}
        for a in self.of("flatpak-uninstall"):
            groups.setdefault((a.opts.get("installation", "system"), bool(a.opts.get("delete_data"))), []).append(a.target)
        for (inst, delete), apps in groups.items():
            out.append(Step("flatpak", ["uninstall", "--noninteractive", "-y", f"--{inst}",
                                        *(["--delete-data"] if delete else []), *apps]))
        ups = [a.target for a in self.of("flatpak-update")]
        if ups:
            out.append(Step("flatpak", ["update", "--noninteractive", "-y", *([] if "*" in ups else ups)]))
        return out

    def simulation_step(self):
        pkg = self.package_args(dry_run=True)
        if not pkg:
            return None
        return Step("zypper", ["--xmlout", "-n", *pkg], lang="C", capture=True)

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
