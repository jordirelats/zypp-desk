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
from .runner import Step, rpm_major, zypper_subcommand

PKG_KINDS = ("install", "remove")
# This many package installs (or removals, locks…) or more are one line in the Apply question, with the names under
# "Show them", instead of hundreds of rows.
GROUP_FROM = 6
# The order the queue runs in (TxQueue.steps); the review window numbers the changes in this order.
RUN_ORDER = ("key-import", "zypp-conf", "repo-url", "repo-add", "repo-modify", "repo-remove", "unlock", "lock",
             "install", "remove", "locale-add", "locale-remove", "flatpak-install", "flatpak-uninstall",
             "flatpak-update", "key-remove", "firmware")
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

    def words(self, actions=(), installed=None, in_transaction=None):
        """This change in words, for someone who does not know zypper: dict(icon, title, detail, more, package).
        actions: the whole queue (a removal works differently with installs beside it; an install from a
        repository that waits in the queue names it). installed: the names that are installed (an install of one
        of them is an update). in_transaction: True when removals run as 'install -- !name' (None: as the queue
        runs them). icon is a theme icon name ('a|b': the first one the theme has); package: the package whose
        own icon the window may show instead."""
        k, t, o = self.kind, self.target, self.opts
        if k == "key-import":
            project = _obs_key_project(self)
            end = _fpr_end(t)
            if project:
                return _w("security-high", _("Trust the key of the project {project}").format(project=project),
                          _("Then the computer accepts the programs that come from this project. Fingerprint "
                            "{end}.").format(end=end))
            return _w("security-high", _("Trust the signing key «{name}»").format(name=o.get("name") or t[-8:]),
                      _("Then the computer accepts the packages signed with it. Fingerprint {end}.").format(end=end))
        if k == "key-remove":
            return _w("security-low", _("Stop trusting the signing key «{name}»").format(name=o.get("name") or t[-8:]),
                      _("New packages signed with it are refused until you trust it again. Fingerprint {end}.").format(
                          end=_fpr_end(t)))
        if k == "zypp-conf":
            st = o.get("settings") or {}
            detail = ", ".join(f"{a} = {b}" for a, b in st.items()) if st else _("Back to openSUSE's settings.")
            return _w("configure", _("Change zypper's settings (zypp.conf)"), detail)
        if k == "repo-url":
            name = o.get("name") or t
            detail = (_("New address from the file {file}.").format(file=o["repo_file"]) if o.get("repo_file")
                      else _("New address: {url}").format(url=o.get("url") or ""))
            return _w("folder-cloud|folder-network", _("Change the address of the repository «{name}»").format(
                name=name), detail)
        if k == "repo-add":
            url = o.get("url") or ""
            local = url.startswith(("dir:", "file:", "/"))
            detail = (_("A folder on this computer where zypper looks for programs.") if local else
                      _("A new place to download programs and updates from."))
            gpg = o.get("gpg", "default")
            if gpg == "allow-unsigned-repo":
                detail += " " + _("Its list of packages is not signed; each package is still checked.")
            elif gpg == "off":
                detail += " " + _("Nothing from it is checked against a signature.")
            return _w("folder" if local else "folder-cloud|folder-network",
                      _("Add the repository «{name}»").format(name=o.get("name") or t), detail)
        if k == "repo-modify":
            changes = ", ".join(_repo_change_text(key, val) for key, val in o.items())
            return _w("document-edit|configure", _("Change the repository «{alias}»").format(alias=t),
                      changes[:1].upper() + changes[1:] + ("." if changes else ""))
        if k == "repo-remove":
            return _w("folder-remove|edit-delete", _("Remove the repository «{alias}»").format(alias=t),
                      _("zypper stops looking for programs and updates there. What you installed from it stays "
                        "installed."))
        if k in ("lock", "unlock"):
            name = t if o.get("type", "package") == "package" else f"{o['type']}:{t}"
            if k == "lock":
                title, detail = _("Lock {name}").format(name=name), _("zypper will not install, update or remove "
                                                                       "it until you unlock it.")
            else:
                title, detail = _("Unlock {name}").format(name=name), _("zypper may install, update or remove it "
                                                                         "again.")
            if o.get("repo"):
                detail += " " + _("Only in the repository {repo}.").format(repo=o["repo"])
            return _w("object-locked" if k == "lock" else "object-unlocked", title, detail)
        if k == "install":
            return self._install_words(actions, installed)
        if k == "remove":
            name = o.get("package") or pkg_ident(self)
            return _w("package-remove|edit-delete", _("Remove {name}").format(name=name),
                      _removal_detail(o.get("clean_deps"), _removals_in_transaction(actions, in_transaction), 1),
                      package=name)
        if k == "locale-add":
            return _w("preferences-desktop-locale", _("Add the language {code}").format(code=t),
                      _("zypper installs the translations of your programs into it."))
        if k == "locale-remove":
            return _w("preferences-desktop-locale", _("Remove the language {code}").format(code=t),
                      _("The translations into it are removed."))
        if k == "flatpak-install":
            return _w("flatpak-discover", _("Install the Flatpak app {app}").format(app=t),
                      _("From {remote}, {installation}.").format(remote=o.get("remote") or "flathub",
                                                                 installation=installation_label(o.get("installation"))))
        if k == "flatpak-uninstall":
            return _w("flatpak-discover", _("Uninstall the Flatpak app {app}").format(app=t),
                      _("Its data in your home folder is deleted too.") if o.get("delete_data") else
                      _("Its data stays in your home folder."))
        if k == "flatpak-update":
            if t == "*":
                return _w("flatpak-discover", _("Update every Flatpak app"), _("And the runtimes they use."))
            return _w("flatpak-discover", _("Update the Flatpak app {app}").format(app=t),
                      _("And the runtime it uses, if it needs a new one."))
        if k == "firmware":
            return _w("cpu|computer", _("Update the firmware of {name} to {version}").format(
                name=o.get("name") or t, version=o.get("version") or "?"),
                _("Keep the computer plugged in. It may need a restart to finish."))
        return _w("system-run", self.describe())

    def _install_words(self, actions, installed):
        t, o = self.target, self.opts
        if o.get("bridge_for"):
            return _w("application-x-rpm|package-x-generic",
                      _("Install the bridge package for {program}").format(program=o["bridge_for"]),
                      _("Made by Zypp Desk on this computer: it tells zypper that what {program} asks for by its "
                        "Fedora names is here. It is not signed, because it was made here.").format(
                          program=o["bridge_for"]), package=o.get("package"))
        if is_file_target(t, o):
            name = o.get("package") or t
            parts = [_("The file {file}.").format(file=t.rsplit("/", 1)[-1])]
            if o.get("allow_unsigned"):
                parts.append(_("It is not signed: you accepted it as it is."))
            if o.get("oldpackage"):
                parts.append(_("It is older than the one you have."))
            return _w("application-x-rpm|package-x-generic", _("Install {name} from a downloaded file").format(
                name=name), " ".join(parts), package=o.get("package"))
        name = pkg_ident(self)
        bare = re.split(r"[<>=]", t, maxsplit=1)[0].strip()
        head = bare.partition(":")[0] if ":" in bare else ""
        if head == "pattern":
            return _w("package-x-generic", _("Install the pattern {name}").format(name=bare.partition(":")[2]),
                      _("A group of packages that go together."))
        version = t[len(bare):].lstrip("<>= ") if t != bare else ""
        alias = install_alias(self)
        repo = None
        if alias:
            add = next((a for a in actions if a.kind == "repo-add" and a.target == alias), None)
            repo = (add.opts.get("name") or alias) if add else alias
        parts = []
        if version:
            title = _("Install {name} {version}").format(name=name, version=version)
            parts.append(_("This exact version, older than the one you have.") if o.get("oldpackage") else
                         _("This exact version."))
        elif installed is not None and name in installed and not repo:
            return _w("system-software-update", _("Update {name}").format(name=name),
                      _("To the newest version your repositories have."), package=name)
        else:
            title = _("Install {name}").format(name=name)
            if not repo:
                parts.append(_("From your repositories: zypper picks the best version."))
        if repo:
            parts.insert(0, _("From the repository «{repo}».").format(repo=repo))
        return _w("package-install|system-software-install", title, " ".join(parts), package=name)


def _w(icon, title, detail="", more=None, package=None):
    return dict(icon=icon, title=title, detail=detail, more=more, package=package)


def _fpr_end(fpr):
    """'…2423 2811': the end of a fingerprint, never split between two lines."""
    end = (fpr or "")[-8:].upper()
    return f"…\u2060{end[:4]}\u00a0{end[4:]}"


def _removals_in_transaction(actions, forced=None):
    """True when the queue's removals run inside the install ('install -- !name': only if nothing needs them)."""
    if forced is not None:
        return forced
    return any(a.kind == "install" for a in actions)


def _removal_detail(clean_deps, in_transaction, n):
    if clean_deps:
        return ngettext("Also what only it needed. Programs that need it go too.",
                        "Also what only they needed. Programs that need them go too.", n)
    if in_transaction:
        return ngettext("Only if nothing installed still needs it: otherwise zypper stops and changes nothing.",
                        "Only if nothing installed still needs them: otherwise zypper stops and changes nothing.", n)
    return ngettext("Programs that need it are removed with it.", "Programs that need them are removed with them.", n)


def install_alias(action):
    """The repository an install names ('packman:vlc', or an alias option), or None."""
    o = action.opts
    if o.get("alias"):
        return o["alias"]
    if is_file_target(action.target, o):
        return None
    t = re.split(r"[<>=]", action.target, maxsplit=1)[0].strip()
    head, sep, _rest = t.partition(":")
    if sep and head not in SOLVABLE_KINDS and "(" not in t:
        return t.rpartition(":")[0]
    return o.get("repo") or None


def installed_names(names):
    """The names among these that are installed (rpm's database, read as the user). Empty if it cannot be read."""
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return set()
    try:
        import rpm
    except ImportError:
        return set()
    out = set()
    try:
        ts = rpm.TransactionSet()
        for n in names:
            if ts.dbMatch("name", n).count():
                out.add(n)
        ts.closeDB()
    except Exception:  # rpm.error, a locked database…: no names, every install reads as "Install"
        return set()
    return out


def describe_actions(actions, *, installed=None, in_transaction=None, group_from=GROUP_FROM):
    """The changes in words for a question, in the order given: one item per change, except many package
    installs (or removals, locks, unlocks, keys to delete, Flatpak updates), which are one item each, with the
    names under "Show them"."""
    counts = {}
    for a in actions:
        counts[_group_key(a)] = counts.get(_group_key(a), 0) + 1
    out, done = [], set()
    for a in actions:
        g = _group_key(a)
        if g is None or counts[g] < group_from:
            out.append(a.words(actions, installed, in_transaction))
            continue
        if g in done:
            continue
        done.add(g)
        members = [b for b in actions if _group_key(b) == g]
        out.extend(_group_words(g, members, actions, installed, in_transaction))
    return out


def _group_key(a):
    if a.kind == "install" and not is_file_target(a.target, a.opts):
        return "install"
    if a.kind == "remove":
        return "remove-clean" if a.opts.get("clean_deps") else "remove"
    if a.kind in ("lock", "unlock", "key-remove") or (a.kind == "flatpak-update" and a.target != "*"):
        return a.kind
    return None


def _group_words(g, members, actions, installed, in_transaction):
    n = len(members)
    if g == "install":
        ups = [a for a in members if installed is not None and not install_alias(a)
               and a.target == pkg_ident(a) and pkg_ident(a) in installed]
        news = [a for a in members if a not in ups]
        out = []
        if ups:
            out.append(_w("system-software-update", ngettext("Update {n} package", "Update {n} packages",
                                                             len(ups)).format(n=len(ups)),
                          _("To the newest version your repositories have."), more=[a.target for a in ups]))
        if news:
            out.append(_w("package-install|system-software-install", ngettext(
                "Install {n} package", "Install {n} packages", len(news)).format(n=len(news)),
                _("From your repositories."), more=[a.target for a in news]))
        return out
    if g in ("remove", "remove-clean"):
        return [_w("package-remove|edit-delete", ngettext("Remove {n} package", "Remove {n} packages", n).format(n=n),
                   _removal_detail(g == "remove-clean", _removals_in_transaction(actions, in_transaction), n),
                   more=[a.target for a in members])]
    names = [a.target if a.opts.get("type", "package") == "package" else f"{a.opts['type']}:{a.target}"
             for a in members] if g in ("lock", "unlock") else [a.opts.get("name") or a.target for a in members]
    if g == "lock":
        return [_w("object-locked", ngettext("Lock {n} package", "Lock {n} packages", n).format(n=n),
                   _("zypper will not install, update or remove them until you unlock them."), more=names)]
    if g == "unlock":
        return [_w("object-unlocked", ngettext("Unlock {n} package", "Unlock {n} packages", n).format(n=n),
                   _("zypper may install, update or remove them again."), more=names)]
    if g == "key-remove":
        return [_w("security-low", ngettext("Stop trusting {n} signing key", "Stop trusting {n} signing keys",
                                            n).format(n=n),
                   _("New packages signed with them are refused until you trust them again."), more=names)]
    return [_w("flatpak-discover", ngettext("Update {n} Flatpak app", "Update {n} Flatpak apps", n).format(n=n),
               _("And the runtimes they use, if they need new ones."), more=names)]


def step_actions(step):
    """The changes a solution's command makes ('zypper addlock/removelock/install'), as Actions, so that they
    can be told in words like the queue (pages/problems.solution_plan makes these commands)."""
    a = step.args
    sub = zypper_subcommand(a)
    names = a[a.index("--") + 1:] if "--" in a else []
    typ = a[a.index("-t") + 1] if "-t" in a and a.index("-t") + 1 < len(a) else "package"
    repo = a[a.index("-r") + 1] if "-r" in a and a.index("-r") + 1 < len(a) else ""
    if sub in ("addlock", "al"):
        return [Action("lock", n, type=typ, repo=repo) for n in names]
    if sub in ("removelock", "rl"):
        return [Action("unlock", n, type=typ, repo=repo) for n in names]
    if sub in ("install", "in"):
        old = "--oldpackage" in a
        return [Action("remove", n[1:]) if n.startswith("!") else Action("install", n, oldpackage=old)
                for n in names]
    return []


# ------------------------------------------------------------------ what needs what
# Which signing key belongs to which repository waiting in the queue. Only links that can be told for sure:
# - the key-import says so (opts for_repo=<alias>);
# - OBS: the key OBS made for a project is named "<project> OBS Project" (<project>@build.opensuse.org), and a
#   subproject without its own key is signed with its parent's; the repository's address holds the project
#   (…/repositories/home:/jordirelats/openSUSE_Factory/);
# - Fedora COPR: the key is named "<owner>_<project> (None)", the address holds …/results/<owner>/<project>/.
_OBS_KEY = re.compile(r"^(\S+?)(?: OBS Project|@build\.opensuse\.org)$")
_COPR_URL = re.compile(r"/results/([^/]+)/([^/]+)/")


def obs_project(url):
    """'home:jordirelats' for …/repositories/home:/jordirelats/openSUSE_Factory/, or None."""
    m = re.search(r"/repositories/(.+)$", url or "")
    if not m:
        return None
    parts = [p for p in m.group(1).split("/") if p]
    if len(parts) < 2:
        return None
    return "".join(parts[:-1]) or None


def _obs_key_project(key):
    m = _OBS_KEY.match((key.opts.get("name") or "").strip())
    return m.group(1) if m else None


def keys_for_repo(repo, keys):
    """The queued key-imports that belong to this queued repo-add (see above); [] when it cannot be told."""
    explicit = [k for k in keys if k.opts.get("for_repo") == repo.target]
    if explicit:
        return explicit
    url = repo.opts.get("url") or ""
    proj = obs_project(url)
    if proj:
        found = [(k, p) for k in keys if (p := _obs_key_project(k)) and (proj == p or proj.startswith(p + ":"))]
        if found:
            best = max(len(p) for _k, p in found)  # the project's own key before its parent's
            return [k for k, p in found if len(p) == best]
        return []
    m = _COPR_URL.search(url)
    if m and "copr" in url:
        owner, project = m.group(1), m.group(2)
        want = {f"{owner}_{project}", f"{owner.lstrip('@')}_{project}"}
        return [k for k in keys if (k.opts.get("name") or "").split(" (")[0].strip() in want]
    return []


def needs(action, actions):
    """The queued changes this one cannot work without (only the direct ones):
    - a repository whose list is checked (gpg 'default') needs its signing key;
    - an install from a repository that waits in the queue ('alias:package') needs that repository, and its key
      when only the packages are checked (COPR);
    - a change to a repository that waits in the queue needs it to be added first;
    - an install of a downloaded file that says which key signed it (opts keyid/signer) needs that key."""
    keys = [a for a in actions if a.kind == "key-import"]
    adds = {a.target: a for a in actions if a.kind == "repo-add"}
    out = []
    if action.kind == "repo-add" and action.opts.get("gpg", "default") == "default":
        out += keys_for_repo(action, keys)
    elif action.kind == "repo-modify" and action.target in adds:
        out.append(adds[action.target])
    elif action.kind == "install":
        if is_file_target(action.target, action.opts):
            kid = (action.opts.get("keyid") or action.opts.get("signer") or "").upper().replace(" ", "")
            if len(kid) >= 8:
                out += [k for k in keys if k.target.upper().endswith(kid[-16:])]
        else:
            repo = adds.get(install_alias(action))
            if repo is not None:
                out.append(repo)
                if repo.opts.get("gpg", "default") == "allow-unsigned-repo":
                    out += keys_for_repo(repo, keys)
    return [a for a in dict.fromkeys(out) if a is not action]


def all_needs(action, actions):
    """needs(), and what those need in turn."""
    seen, todo = [], [action]
    while todo:
        for n in needs(todo.pop(), actions):
            if n is not action and n not in seen:
                seen.append(n)
                todo.append(n)
    return seen


def needed_by(action, actions):
    """The queued changes that would fail without this one (also through another one)."""
    return [b for b in actions if b is not action and action in all_needs(b, actions)]


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

    def ordered(self):
        """The changes in the order they run (the order of steps()): keys, repositories, locks, packages
        (removals with --clean-deps first), languages, Flatpak, keys to delete, firmware."""
        out = []
        for k in RUN_ORDER:
            if k == "remove":
                continue
            if k == "install":
                out += [a for a in self.of("remove") if a.opts.get("clean_deps")]
                out += self.of("install")
                out += [a for a in self.of("remove") if not a.opts.get("clean_deps")]
            else:
                out += self.of(k)
        known = set(map(id, out))
        return out + [a for a in self.actions if id(a) not in known]

    # ---------------------------------------------------------- in words
    def words(self, group_from=GROUP_FROM):
        """The changes in words, in the order they run, for the Apply question (see describe_actions)."""
        acts = self.ordered()
        names = [pkg_ident(a) for a in acts if a.kind == "install" and not is_file_target(a.target, a.opts)
                 and not install_alias(a) and a.target == pkg_ident(a)]
        return describe_actions(acts, installed=installed_names(names), group_from=group_from)

    def needs(self, action):
        return needs(action, self.actions)

    def all_needs(self, action):
        return all_needs(action, self.actions)

    def needed_by(self, action):
        return needed_by(action, self.actions)

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
        return [args for args, _acts in self._package_plan(dry_run)]

    def _package_plan(self, dry_run=False):
        """package_args, each command with the queued changes it carries: [(args, [Action])]."""
        skip = self.not_simulable() if dry_run else []
        every_inst = self.of("install")
        inst = [a for a in every_inst if a not in skip]
        rem = [a for a in self.of("remove") if not a.opts.get("clean_deps")]
        clean = [a for a in self.of("remove") if a.opts.get("clean_deps")]
        dry = ["--dry-run"] if dry_run else []
        out = []
        if clean:
            out.append((["remove", "--clean-deps", *dry, "--", *(a.target for a in clean)], clean))
        if every_inst:  # also when the dry run leaves the installs out: the real run is an install
            names = [a.target for a in inst] + ["!" + a.target for a in rem]
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
                out.append((args + dry + ["--"] + names, inst + rem))
        elif rem:
            out.append((["remove", "--no-clean-deps", *dry, "--", *(a.target for a in rem)], rem))
        return out

    def steps(self):
        """Every command, in order: signing keys and zypp.conf first, then repositories, locks, packages,
        languages, Flatpak, keys to delete and, last, firmware (it may want a restart).

        Each Step carries .actions: the queued changes it makes (a change that needs several commands, like a
        new address, is done when all of them are)."""
        out = []

        def step(acts, *args, **kw):
            st = Step(*args, **kw)
            st.actions = list(acts)
            out.append(st)

        for a in self.of("key-import"):
            step([a], "rpm-import", [a.opts["path"], a.target])
        for a in self.of("zypp-conf"):
            st = a.opts.get("settings") or {}
            step([a], "zypp-conf", [f"{k}={v}" for k, v in st.items()] if st else ["--reset"])
        for a in self.of("repo-url"):
            # zypper cannot change an address. The repository is added again first, under a temporary alias,
            # with all its settings: if the new address is refused, the old repository is still there. Then
            # the old one goes and the new one takes its alias. '--' before every alias and address.
            temp = a.target + REPO_URL_TEMP_SUFFIX
            if a.opts.get("repo_file"):  # every setting, also the key (gpgkey=), in a .repo file
                step([a], "zypper", ["-n", "addrepo", "--", a.opts["repo_file"]])
            else:
                step([a], "zypper", ["-n", *_addrepo_args(a.opts, a.target), "--", a.opts["url"], temp])
            step([a], "zypper", ["-n", "removerepo", "--", a.target])
            step([a], "zypper", ["-n", "renamerepo", "--", temp, a.target])
        for a in self.of("repo-add"):
            step([a], "zypper", ["-n", *_addrepo_args(a.opts, a.target), "--", a.opts["url"], a.target])
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
            step([a], "zypper", ["-n", *args, "--", a.target])
        rr = self.of("repo-remove")
        if rr:
            step(rr, "zypper", ["-n", "removerepo", "--", *(a.target for a in rr)])
        for kind, sub in (("unlock", "removelock"), ("lock", "addlock")):
            groups = {}
            for a in self.of(kind):
                groups.setdefault((a.opts.get("type", "package"), a.opts.get("repo", "")), []).append(a)
            for (typ, repo), acts in groups.items():
                step(acts, "zypper", ["-n", sub, *([] if typ == "package" else ["-t", typ]),
                                      *(["-r", repo] if repo else []), "--", *(a.target for a in acts)])
        for pkg, acts in self._package_plan():
            step(acts, "zypper", ["-n", *pkg])
        for kind, sub in (("locale-add", "addlocale"), ("locale-remove", "removelocale")):
            acts = self.of(kind)
            if acts:
                step(acts, "zypper", ["-n", sub, "--", *(a.target for a in acts)])
        out += self.flatpak_steps()
        for a in self.of("key-remove"):
            nvr = a.opts.get("nvr") or ""
            key = nvr[len("gpg-pubkey-"):] if nvr.startswith("gpg-pubkey-") and rpm_major() < 6 else a.target
            step([a], "key-remove", [a.target], shown=f"rpmkeys --delete {key}")
        for a in self.of("firmware"):
            # --no-unreported-check: --assume-yes would otherwise say yes to sending reports of earlier updates
            step([a], "fwupdmgr", ["update", a.target, "--assume-yes", "--no-reboot-check", "--no-unreported-check"])
        for i, s in enumerate(out):
            s.cond = "always" if i == 0 else "success"
        return out

    def flatpak_steps(self):
        out, groups = [], {}

        def step(acts, args):
            st = Step("flatpak", args)
            st.actions = list(acts)
            out.append(st)

        for a in self.of("flatpak-install"):
            groups.setdefault((a.opts.get("installation", "system"), a.opts.get("remote", "flathub")), []).append(a)
        for (inst, remote), acts in groups.items():
            step(acts, ["install", "--noninteractive", "-y", f"--{inst}", "--", remote, *(a.target for a in acts)])
        groups = {}
        for a in self.of("flatpak-uninstall"):
            groups.setdefault((a.opts.get("installation", "system"), bool(a.opts.get("delete_data"))), []).append(a)
        for (inst, delete), acts in groups.items():
            step(acts, ["uninstall", "--noninteractive", "-y", f"--{inst}", *(["--delete-data"] if delete else []),
                        "--", *(a.target for a in acts)])
        ups = self.of("flatpak-update")
        if ups:
            targets = [a.target for a in ups]
            step(ups, ["update", "--noninteractive", "-y", *([] if "*" in targets else ["--", *targets])])
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
