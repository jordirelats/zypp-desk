# SPDX-License-Identifier: GPL-3.0-or-later
"""The queue of pending changes: packages, locks, repositories and locales.

Nothing is changed until the queue is applied. The queue turns itself into the exact zypper
commands it will run, so the window can always show them.
"""

import configparser
import os
import re
import shlex

from contextlib import contextmanager

from PySide6.QtCore import QLocale, QObject, Signal

from .i18n import _, ngettext, pgettext
from .runner import Step, rpm_major, zypper_subcommand

PKG_KINDS = ("install", "remove")
# This many package installs (or removals, locks…) or more are one line in the Apply question, with the names under
# "Show them", instead of hundreds of rows.
GROUP_FROM = 6
# The queue review shows each change with its own button; from this many of one kind on, they are one row with
# "Show them" and one button (a window of 2,000 rows took seconds to draw).
REVIEW_GROUP_FROM = 30
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
REPOS_D = "/etc/zypp/repos.d"


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


def nice_fpr(fpr):
    """'0FF9 D4D2 6264 C149 3957 0657 FD53 6322 2423 2811': a whole fingerprint in groups of 4 (8 digits are a
    short key ID, easy to fake). Each half never breaks; a line may break between them."""
    fpr = (fpr or "").upper().replace(" ", "")
    groups = [fpr[i:i + 4] for i in range(0, len(fpr), 4)]
    half = (len(groups) + 1) // 2
    return " ".join(groups[:half]) + (" " + " ".join(groups[half:]) if groups[half:] else "")


def quoted(name):
    """A name in the quotes of the language of the window."""
    # TRANSLATORS: quotes around a name inside a sentence (ca «{name}», de „{name}“, fr « {name} »)
    return _("“{name}”").format(name=name)


def language_name(code):
    """'français (fr)' for 'fr': the name a language gives itself, with its code; the code alone when Qt does
    not know it."""
    loc = QLocale(code)
    name = loc.nativeLanguageName() if loc.language() not in (QLocale.Language.C, QLocale.Language.AnyLanguage) \
        else ""
    return f"{name} ({code})" if name and name != code else code


_repo_cache = {"stamp": None, "repos": {}}


def repo_info(alias):
    """dict(name, url) of a repository zypper knows, from its .repo file (readable by everyone); {} when there
    is none. Read again only when a file in repos.d changed."""
    try:
        entries = sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(REPOS_D) if e.name.endswith(".repo"))
    except OSError:
        return {}
    if entries != _repo_cache["stamp"]:
        repos = {}
        for name, _mtime in entries:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(os.path.join(REPOS_D, name), encoding="utf-8")
            except (configparser.Error, OSError, UnicodeDecodeError):
                continue
            for sec in cp.sections():
                repos.setdefault(sec, dict(name=cp[sec].get("name", "").strip() or sec,
                                           url=(cp[sec].get("baseurl", "") or "").strip()))
        _repo_cache.update(stamp=entries, repos=repos)
    return _repo_cache["repos"].get(alias, {})


def repo_name(alias, opts=None):
    """The name a person knows a repository by ('Main Repository (OSS)'), not its alias ('repo-oss')."""
    return (opts or {}).get("name") or repo_info(alias).get("name") or alias


class Action:
    def __init__(self, kind, target, **opts):
        self.kind = kind  # install, remove, lock, unlock, repo-add, repo-modify, repo-remove, locale-add, locale-remove
        self.target = target
        self.opts = opts

    def describe(self):
        """This change in one line: the title of words()."""
        return self.words()["title"]

    def words(self, actions=(), installed=None, in_transaction=None):
        """This change in words, for someone who does not know zypper: dict(icon, title, detail, more, package).
        actions: the whole queue (a removal works differently with installs beside it; an install from a
        repository that waits in the queue names it). installed: the names that are installed (an install of one
        of them is an update). in_transaction: True when removals run as 'install -- !name' (None: as the queue
        runs them). icon is a theme icon name ('a|b': the first one the theme has); package: the package whose
        own icon the window may show instead.

        Every title has the context "change": the same English words are also buttons
        ("Remove {name}"), and Catalan says a change in a list with the infinitive ("Treure {name}")."""
        k, t, o = self.kind, self.target, self.opts
        if k == "key-import":
            fpr = nice_fpr(t)
            project = key_project(self, actions)
            if project:
                return _w("security-high", pgettext("change", "Trust the key of the project {project}").format(
                    project=project), _("Then the computer accepts the programs that come from this project. "
                                        "Fingerprint {end}.").format(end=fpr))
            repo = _key_repo(self, actions)
            if repo is not None:
                title = pgettext("change", "Trust the key of the repository “{name}”").format(
                    name=repo.opts.get("name") or repo.target)
            else:
                title = pgettext("change", "Trust the signing key “{name}”").format(name=o.get("name") or t[-8:])
            return _w("security-high", title, _("Then the computer accepts the packages signed with it. Fingerprint "
                                                "{end}.").format(end=fpr))
        if k == "key-remove":
            return _w("security-low", pgettext("change", "Stop trusting the signing key “{name}”").format(
                name=o.get("name") or t[-8:]),
                _("New packages signed with it are refused until you trust it again. Fingerprint {end}.").format(
                    end=nice_fpr(t)))
        if k == "zypp-conf":
            st = o.get("settings") or {}
            detail = ", ".join(f"{a} = {b}" for a, b in st.items()) if st else _("Back to openSUSE's settings.")
            return _w("configure", pgettext("change", "Change zypper's settings (zypp.conf)"), detail)
        if k == "repo-url":
            name = o.get("name") or t
            detail = (_("New address from the file {file}.").format(file=o["repo_file"]) if o.get("repo_file")
                      else _("New address: {url}").format(url=o.get("url") or ""))
            return _w("folder-cloud|folder-network", pgettext(
                "change", "Change the address of the repository “{name}”").format(name=name), detail)
        if k == "repo-add":
            url = o.get("url") or ""
            local = url.startswith(("dir:", "file:", "/"))
            detail = (_("A folder on this computer where zypper looks for programs.") if local else
                      _("A new place to download programs and updates from."))
            gpg = o.get("gpg", "default")
            if gpg == "allow-unsigned-repo":
                detail += " " + _("Its list of packages is not signed; each package is still checked.")
            elif gpg == "allow-unsigned-package":
                detail += " " + _("Its list of packages is checked; the packages themselves are not.")
            elif gpg == "off":
                detail += " " + _("Nothing from it is checked against a signature.")
            return _w("folder" if local else "folder-cloud|folder-network",
                      pgettext("change", "Add the repository “{name}”").format(name=o.get("name") or t), detail)
        if k == "repo-modify":
            return _repo_modify_words(t, o)
        if k == "repo-remove":
            return _w("folder-remove|edit-delete", pgettext("change", "Remove the repository “{name}”").format(
                name=repo_name(t, o)), _("zypper stops looking for programs and updates there. What you installed "
                                         "from it stays installed."))
        if k in ("lock", "unlock"):
            name = t if o.get("type", "package") == "package" else f"{o['type']}:{t}"
            if k == "lock":
                title, detail = pgettext("change", "Lock {name}").format(name=name), _(
                    "zypper will not install, update or remove it until you unlock it.")
            else:
                title, detail = pgettext("change", "Unlock {name}").format(name=name), _(
                    "zypper may install, update or remove it again.")
            if o.get("repo"):
                detail += " " + _("Only in the repository {repo}.").format(repo=o["repo"])
            return _w("object-locked" if k == "lock" else "object-unlocked", title, detail)
        if k == "install":
            return self._install_words(actions, installed)
        if k == "remove":
            name = o.get("package") or pkg_ident(self)
            return _w("package-remove|edit-delete", pgettext("change", "Remove {name}").format(name=name),
                      _removal_detail(o.get("clean_deps"), _removals_in_transaction(actions, in_transaction), 1),
                      package=name)
        if k == "locale-add":
            return _w("preferences-desktop-locale", pgettext("change", "Add the language {language}").format(
                language=language_name(t)), _("zypper installs the translations of your programs into it."))
        if k == "locale-remove":
            return _w("preferences-desktop-locale", pgettext("change", "Remove the language {language}").format(
                language=language_name(t)), _("The translations into it are removed."))
        if k == "flatpak-install":
            return _w("flatpak-discover", pgettext("change", "Install the Flatpak app {app}").format(app=t),
                      _("From {remote}, {installation}.").format(remote=o.get("remote") or "flathub",
                                                                 installation=installation_label(o.get("installation"))))
        if k == "flatpak-uninstall":
            return _w("flatpak-discover", pgettext("change", "Uninstall the Flatpak app {app}").format(app=t),
                      _("Its data in your home folder is deleted too.") if o.get("delete_data") else
                      _("Its data stays in your home folder."))
        if k == "flatpak-update":
            if t == "*":
                return _w("flatpak-discover", pgettext("change", "Update every Flatpak app"),
                          _("And the runtimes they use."))
            return _w("flatpak-discover", pgettext("change", "Update the Flatpak app {app}").format(app=t),
                      _("And the runtime it uses, if it needs a new one."))
        if k == "firmware":
            return _w("cpu|computer", pgettext("change", "Update the firmware of {name} to {version}").format(
                name=o.get("name") or t, version=o.get("version") or "?"),
                _("Keep the computer plugged in. It may need a restart to finish."))
        return _w("system-run", f"{k} {t}")

    def _install_words(self, actions, installed):
        t, o = self.target, self.opts
        if o.get("bridge_for"):
            return _w("application-x-rpm|package-x-generic",
                      pgettext("change", "Install the bridge package for {program}").format(program=o["bridge_for"]),
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
            return _w("application-x-rpm|package-x-generic", pgettext(
                "change", "Install {name} from a downloaded file").format(name=name), " ".join(parts),
                package=o.get("package"))
        name = pkg_ident(self)
        bare = re.split(r"[<>=]", t, maxsplit=1)[0].strip()
        head = bare.partition(":")[0] if ":" in bare else ""
        if head == "pattern":
            return _w("package-x-generic", pgettext("change", "Install the pattern {name}").format(
                name=bare.partition(":")[2]), _("A group of packages that go together."))
        version = t[len(bare):].lstrip("<>= ") if t != bare else ""
        alias = install_alias(self)
        repo = None
        if alias:
            add = next((a for a in actions if a.kind == "repo-add" and a.target == alias), None)
            repo = (add.opts.get("name") or alias) if add else alias
        parts = []
        if version:
            title = pgettext("change", "Install {name} {version}").format(name=name, version=version)
            parts.append(_("This exact version, older than the one you have.") if o.get("oldpackage") else
                         _("This exact version."))
        elif installed is not None and name in installed and not repo:
            # zypper's install (and update) never changes the vendor of a package: a newer
            # version from another vendor (Packman instead of openSUSE…) is left out
            return _w("system-software-update", pgettext("change", "Update {name}").format(name=name),
                      _("To the newest version from the same vendor."), package=name)
        else:
            title = pgettext("change", "Install {name}").format(name=name)
            if not repo:
                parts.append(_("From your repositories: zypper picks the best version."))
        if repo:
            parts.insert(0, _("From the repository “{repo}”.").format(repo=repo))
        return _w("package-install|system-software-install", title, " ".join(parts), package=name)


def _w(icon, title, detail="", more=None, package=None):
    return dict(icon=icon, title=title, detail=detail, more=more, package=package)


def _repo_modify_words(alias, o):
    """A change to a repository, by its name: one title for one change ("Disable the
    repository …"), and each change in a sentence."""
    name = repo_name(alias)
    keys = set(o)
    if keys == {"enabled"}:
        if o["enabled"]:
            return _w("document-edit|configure", pgettext("change", "Enable the repository “{name}”").format(
                name=name), _("zypper looks for programs and updates there again."))
        return _w("document-edit|configure", pgettext("change", "Disable the repository “{name}”").format(
            name=name), _("zypper stops looking for programs and updates there until you enable it again. What you "
                          "installed from it stays installed."))
    if keys == {"priority"}:
        title = pgettext("change", "Change the priority of the repository “{name}”").format(name=name)
    elif keys == {"name"}:
        title = pgettext("change", "Rename the repository “{name}”").format(name=name)
    else:
        title = pgettext("change", "Change the repository “{name}”").format(name=name)
    return _w("document-edit|configure", title, " ".join(_repo_change_sentence(key, val) for key, val in o.items()))


def _repo_change_sentence(key, val):
    if key == "priority":
        return _("Priority {n}: the lower the number, the more zypper prefers it.").format(n=val)
    if key == "name":
        return _("New name: {name}.").format(name=quoted(val))
    if key == "enabled":
        return (_("zypper looks for programs and updates there again.") if val else
                _("zypper stops looking for programs and updates there until you enable it again. What you "
                  "installed from it stays installed."))
    if key == "autorefresh":
        return (_("zypper refreshes it on its own before it looks for updates.") if val else
                _("zypper no longer refreshes it on its own."))
    if key == "keeppackages":
        return (_("The packages downloaded from it are kept.") if val else
                _("The packages downloaded from it are no longer kept."))
    return f"{key} = {val}."


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


def grouped(actions, *, installed=None, in_transaction=None, group_from=GROUP_FROM):
    """The changes in words, in the order given, each with the queued changes it stands for: [(words,
    [Action])]. One item per change, except many package installs (or removals, locks, unlocks, keys to delete,
    Flatpak updates): from group_from of one kind on, they are one item, with the names under "Show them"."""
    counts = {}
    for a in actions:
        g = _group_key(a)
        counts[g] = counts.get(g, 0) + 1
    members = {}
    for a in actions:
        g = _group_key(a)
        if g is not None and counts[g] >= group_from:
            members.setdefault(g, []).append(a)
    out, done = [], set()
    for a in actions:
        g = _group_key(a)
        if g not in members:
            out.append((a.words(actions, installed, in_transaction), [a]))
            continue
        if g in done:
            continue
        done.add(g)
        out.extend(_group_words(g, members[g], actions, installed, in_transaction))
    return out


def describe_actions(actions, *, installed=None, in_transaction=None, group_from=GROUP_FROM):
    """The changes in words for a question, in the order given (see grouped)."""
    return [w for w, _m in grouped(actions, installed=installed, in_transaction=in_transaction,
                                   group_from=group_from)]


def _group_key(a):
    if a.kind == "install" and not is_file_target(a.target, a.opts):
        return "install"
    if a.kind == "remove":
        return "remove-clean" if a.opts.get("clean_deps") else "remove"
    if a.kind in ("lock", "unlock", "key-remove") or (a.kind == "flatpak-update" and a.target != "*"):
        return a.kind
    return None


def _group_words(g, members, actions, installed, in_transaction):
    """[(words, [Action])] for a group of changes of one kind."""
    n = len(members)
    if g == "install":
        ups = [a for a in members if installed is not None and not install_alias(a)
               and a.target == pkg_ident(a) and pkg_ident(a) in installed]
        up_ids = set(map(id, ups))
        news = [a for a in members if id(a) not in up_ids]
        out = []
        if ups:
            # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
            title = ngettext("Update {n} package", "Update {n} packages", len(ups)).format(n=len(ups))
            out.append((_w("system-software-update", title, _("Each one to the newest version from the same vendor."),
                           more=[a.target for a in ups]), ups))
        if news:
            # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
            title = ngettext("Install {n} package", "Install {n} packages", len(news)).format(n=len(news))
            out.append((_w("package-install|system-software-install", title, _("From your repositories."),
                           more=[a.target for a in news]), news))
        return out
    if g in ("remove", "remove-clean"):
        # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
        title = ngettext("Remove {n} package", "Remove {n} packages", n).format(n=n)
        return [(_w("package-remove|edit-delete", title,
                    _removal_detail(g == "remove-clean", _removals_in_transaction(actions, in_transaction), n),
                    more=[a.target for a in members]), members)]
    names = [a.target if a.opts.get("type", "package") == "package" else f"{a.opts['type']}:{a.target}"
             for a in members] if g in ("lock", "unlock") else [a.opts.get("name") or a.target for a in members]
    if g == "lock":
        # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
        title = ngettext("Lock {n} package", "Lock {n} packages", n).format(n=n)
        return [(_w("object-locked", title, _("zypper will not install, update or remove them until you unlock "
                                              "them."), more=names), members)]
    if g == "unlock":
        # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
        title = ngettext("Unlock {n} package", "Unlock {n} packages", n).format(n=n)
        return [(_w("object-unlocked", title, _("zypper may install, update or remove them again."), more=names),
                 members)]
    if g == "key-remove":
        # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
        title = ngettext("Stop trusting {n} signing key", "Stop trusting {n} signing keys", n).format(n=n)
        return [(_w("security-low", title, _("New packages signed with them are refused until you trust them "
                                             "again."), more=names), members)]
    # TRANSLATORS: a change in a list (the Apply question, the queue): ca uses the infinitive here
    title = ngettext("Update {n} Flatpak app", "Update {n} Flatpak apps", n).format(n=n)
    return [(_w("flatpak-discover", title, _("And the runtimes they use, if they need new ones."), more=names),
             members)]


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
# Which signing key belongs to which repository waiting in the queue. Only links that someone made on purpose
# (a key's name is whatever its maker wrote in it, so it never links a key):
# - the key was fetched for that repository, from its own address or its .repo file (opts for_repo=<alias>);
# - the key was fetched from an OBS project's own address (opts project=<project>): it signs that project's
#   repositories, and those of its subprojects that have no key of their own; the repository's address holds the
#   project (…/repositories/home:/jordirelats/openSUSE_Factory/).


def obs_project(url):
    """'home:jordirelats' for …/repositories/home:/jordirelats/openSUSE_Factory/, or None."""
    m = re.search(r"/repositories/(.+)$", url or "")
    if not m:
        return None
    parts = [p for p in m.group(1).split("/") if p]
    if len(parts) < 2:
        return None
    return "".join(parts[:-1]) or None


def _key_repo(key, actions):
    """The queued repo-add this key was fetched for (opts for_repo), or None."""
    alias = key.opts.get("for_repo")
    if not alias:
        return None
    return next((a for a in actions if a.kind == "repo-add" and a.target == alias), None)


def key_project(key, actions=()):
    """The OBS project whose key this is, only when that is known for sure: the import says so (opts project=), or
    the key was fetched for a queued OBS repository (opts for_repo=); else None."""
    if key.opts.get("project"):
        return key.opts["project"]
    repo = _key_repo(key, actions)
    return obs_project(repo.opts.get("url")) if repo is not None else None


def keys_for_repo(repo, keys):
    """The queued key-imports that belong to this queued repo-add (see above); [] when it cannot be told."""
    explicit = [k for k in keys if k.opts.get("for_repo") == repo.target]
    if explicit:
        return explicit
    proj = obs_project(repo.opts.get("url") or "")
    if proj:
        found = [(k, p) for k in keys if (p := k.opts.get("project")) and (proj == p or proj.startswith(p + ":"))]
        if found:
            best = max(len(p) for _k, p in found)  # the project's own key before its parent's
            return [k for k, p in found if len(p) == best]
    return []


class Needs:
    """What needs what in a list of changes, worked out once for all of them (the review window asks it for each
    row; asking needs() per row was quadratic):
    - a repository whose list is checked (gpg 'default' or 'allow-unsigned-package') needs its signing key;
    - an install from a repository that waits in the queue ('alias:package') needs that repository, and its key
      when only the packages are checked (COPR);
    - a change to a repository that waits in the queue needs it to be added first;
    - an install of a downloaded file that says which key signed it (opts keyid/signer) needs that key."""

    def __init__(self, actions):
        self.actions = list(actions)
        keys = [a for a in self.actions if a.kind == "key-import"]
        adds = {}
        for a in self.actions:
            if a.kind == "repo-add":
                adds.setdefault(a.target, a)
        by_end = {}  # the last 8 and the last 16 hex digits of a key → the queued imports of it
        for k in keys:
            fpr = k.target.upper().replace(" ", "")
            for n in (8, 16):
                by_end.setdefault(fpr[-n:], []).append(k)
        repo_keys = {}

        def keys_of(repo):
            if id(repo) not in repo_keys:
                repo_keys[id(repo)] = keys_for_repo(repo, keys)
            return repo_keys[id(repo)]

        self.direct = {}
        for a in self.actions:
            out = []
            if a.kind == "repo-add" and a.opts.get("gpg", "default") in ("default", "allow-unsigned-package"):
                out += keys_of(a)  # its list of packages is checked against its key
            elif a.kind == "repo-modify" and a.target in adds:
                out.append(adds[a.target])
            elif a.kind == "install":
                if is_file_target(a.target, a.opts):
                    # the key whose fingerprint ends with the key ID that signed the file
                    end = (a.opts.get("keyid") or a.opts.get("signer") or "").upper().replace(" ", "")[-16:]
                    if len(end) in (8, 16):
                        out += by_end.get(end, [])
                    elif len(end) > 8:
                        out += [k for k in keys if k.target.upper().replace(" ", "").endswith(end)]
                else:
                    repo = adds.get(install_alias(a))
                    if repo is not None:
                        out.append(repo)
                        if repo.opts.get("gpg", "default") == "allow-unsigned-repo":
                            out += keys_of(repo)
            self.direct[id(a)] = [x for x in dict.fromkeys(out) if x is not a]
        self.users = {}  # id(x) → the changes that need x directly
        for a in self.actions:
            for x in self.direct[id(a)]:
                self.users.setdefault(id(x), []).append(a)

    def needs(self, action):
        return list(self.direct.get(id(action), ()))

    def _walk(self, action, edges):
        seen, out, todo = {id(action)}, [], [action]
        while todo:
            for x in edges.get(id(todo.pop()), ()):
                if id(x) not in seen:
                    seen.add(id(x))
                    out.append(x)
                    todo.append(x)
        return out

    def all_needs(self, action):
        """needs(), and what those need in turn."""
        return self._walk(action, self.direct)

    def needed_by(self, action):
        """The changes that would fail without this one (also through another one)."""
        return self._walk(action, self.users)


def needs(action, actions):
    """The queued changes this one cannot work without (only the direct ones; see Needs)."""
    if not any(a is action for a in actions):
        actions = [*actions, action]
    return Needs(actions).needs(action)


def all_needs(action, actions):
    """needs(), and what those need in turn."""
    if not any(a is action for a in actions):
        actions = [*actions, action]
    return Needs(actions).all_needs(action)


def needed_by(action, actions):
    """The queued changes that would fail without this one (also through another one)."""
    return Needs(actions).needed_by(action)


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
    elif gpg == "allow-unsigned-package":  # gpgcheck=0 with repo_gpgcheck=1: the metadata stays checked
        args.append("--gpgcheck-allow-unsigned-package")
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


OPPOSITE = {"install": "remove", "remove": "install", "lock": "unlock", "unlock": "lock",
            "locale-add": "locale-remove", "locale-remove": "locale-add",
            "flatpak-install": "flatpak-uninstall", "flatpak-uninstall": "flatpak-install",
            "key-import": "key-remove", "key-remove": "key-import"}


class TxQueue(QObject):
    changed = Signal()
    # a change the queue did not take, and why (the removal of a protected app): the window says so
    refused = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._actions = []
        # (kind, what it is about) → the queued changes: add() finds what a change replaces without going
        # through the whole queue (2,457 adds took 5.6 s)
        self._index = {}
        self._index_of = None  # (the list, its length) the index was made for
        self._batch = 0
        self._dirty = False

    @property
    def actions(self):
        """The queued changes, in the order they were added (a list; ordered() gives the order they run in)."""
        return self._actions

    @actions.setter
    def actions(self, value):
        self._actions = list(value)
        self._index_of = None

    def _idx(self):
        """The index, made again when the list was replaced or changed in place since."""
        if self._index_of != (id(self._actions), len(self._actions)):
            idx = {}
            for a in self._actions:
                idx.setdefault((a.kind, _key(a)), []).append(a)
            self._index = idx
            self._index_of = (id(self._actions), len(self._actions))
        return self._index

    def _forget(self, gone):
        """Take these changes out of the list and the index (a new list, as before: a copy someone holds stays)."""
        ids = set(map(id, gone))
        idx = self._idx()
        for a in gone:
            lst = idx.get((a.kind, _key(a)))
            if lst is not None:
                lst[:] = [x for x in lst if x is not a]
                if not lst:
                    del idx[(a.kind, _key(a))]
        self._actions = [a for a in self._actions if id(a) not in ids]
        self._index_of = (id(self._actions), len(self._actions))

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
        from . import protect
        target = protect.action_target(action)
        if target and self.settings is not None and protect.is_protected(self.settings, *target):
            self.refused.emit(_("{name} is protected, so it is not removed. To remove it, first choose “Stop "
                                "protecting {name}” on its card in Applications.").format(name=target[1]))
            return
        idx = self._idx()
        key = _key(action)
        if action.kind == "repo-modify":
            # the repository added again at its new address takes the change with its other settings; one
            # added from a .repo file cannot: the change runs after it, as a change of its own
            for a in idx.get(("repo-url", key), ()):
                if a.target == action.target and not a.opts.get("repo_file"):
                    a.opts.update(action.opts)
                    self._changed()
                    return
            for a in idx.get(("repo-modify", key), ()):
                if a.target == action.target:
                    a.opts.update(action.opts)
                    self._changed()
                    return
        opposite = OPPOSITE.get(action.kind)
        if action.kind == "repo-url" and not action.opts.get("repo_file"):
            opposite = "repo-modify"  # a new address replaces a pending change: its settings carry it
        old = list(idx.get((action.kind, key), ())) + (list(idx.get((opposite, key), ())) if opposite else [])
        if old:
            self._forget(old)
            idx = self._idx()
        self._actions.append(action)
        idx.setdefault((action.kind, key), []).append(action)
        self._index_of = (id(self._actions), len(self._actions))
        self._changed()

    def discard(self, kind, target):
        gone = [a for a in self._actions if a.kind == kind and a.target == target]
        if gone:
            self._forget(gone)
            self._changed()

    def discard_action(self, action):
        self.discard_actions([action])

    def discard_actions(self, actions):
        """Take these changes out (those still in the queue), with one 'changed' signal."""
        ids = set(map(id, actions))
        gone = [a for a in self._actions if id(a) in ids]
        if gone:
            self._forget(gone)
            self._changed()

    def clear(self):
        if self._actions:
            self.actions = []
            self._changed()

    def state_of(self, name):
        for a in self._actions:
            if a.kind in PKG_KINDS and name in (a.opts.get("package") or a.target.split("=")[0], pkg_ident(a)):
                return a.kind  # also 'packman:vlc' (queued from Search online) for vlc
        return None

    def flatpak_state(self, app):
        for a in self._actions:
            if a.kind.startswith("flatpak-") and a.target in (app, "*"):
                return a.kind
        return None

    def repo_change(self, alias):
        return next((a for a in self._actions if a.kind.startswith("repo-") and a.target == alias), None)

    def key_state(self, fpr):
        return next((a.kind for a in self._actions if a.kind in ("key-import", "key-remove") and a.target == fpr),
                    None)

    def __len__(self):
        return len(self._actions)

    def of(self, *kinds):
        return [a for a in self._actions if a.kind in kinds]

    def ordered(self):
        """The changes in the order they run (the order of steps()): keys, repositories, locks, packages
        (removals with --clean-deps first), languages, Flatpak, keys to delete, firmware."""
        by_kind = {}
        for a in self._actions:
            by_kind.setdefault(a.kind, []).append(a)
        out = []
        for k in RUN_ORDER:
            if k == "remove":
                continue
            if k == "install":  # as the package commands run: bridges and unsigned files may have their own
                for _args, acts in self._package_plan():
                    out += acts
            else:
                out += by_kind.get(k, [])
        known = set(map(id, out))
        return out + [a for a in self._actions if id(a) not in known]

    # ---------------------------------------------------------- in words
    def words(self, group_from=GROUP_FROM):
        """The changes in words, in the order they run, for the Apply question (see describe_actions)."""
        acts = self.ordered()
        return describe_actions(acts, installed=installed_names(self.update_candidates(acts)),
                                group_from=group_from)

    @staticmethod
    def update_candidates(acts):
        """The names whose install may be an update (a plain name, from no repository in particular)."""
        return [pkg_ident(a) for a in acts if a.kind == "install" and not is_file_target(a.target, a.opts)
                and not install_alias(a) and a.target == pkg_ident(a)]

    def links(self):
        """What needs what in the queue, worked out once (see Needs)."""
        return Needs(self._actions)

    def needs(self, action):
        return needs(action, self._actions)

    def all_needs(self, action):
        return all_needs(action, self._actions)

    def needed_by(self, action):
        return needed_by(action, self._actions)

    # ---------------------------------------------------------- commands
    def not_simulable(self):
        """Installs that name a repository still waiting in the queue ('alias:package'): zypper does not know
        that repository until the queue runs, so a dry run would only say it cannot find them."""
        new_aliases = {a.target for a in self.of("repo-add")}
        return [a for a in self.of("install") if ":" in a.target and a.target.split(":", 1)[0] in new_aliases]

    def package_args(self, dry_run=False):
        """The zypper commands for packages, each one a list of arguments.

        - Installs and removals go together, as one transaction: 'zypper install -- new '!old''. zypper
          resolves them at once, so a new package that conflicts with one leaving is fine. In this form a
          removal does not take away what depends on the package: zypper looks for something else that
          provides it, and stops with a problem when nothing does. (zypp.conf's solver.cleandepsOnRemove
          applies to these removals: 'install' has no --no-clean-deps.)
        - Removals alone: 'zypper remove --no-clean-deps', which also takes away what depends on them (the
          Remove window shows it), and nothing else even when zypp.conf says to clean dependencies.
        - Removals with "Also remove what only it needed" go first, on their own: only 'zypper remove' has
          --clean-deps.
        - --allow-unsigned-rpm lets every file named in its command in without a signature, so it is only in a
          command whose files were all accepted unsigned: a bridge package (made here,
          it only needs openSUSE packages) is installed on its own first, and files accepted unsigned go on their
          own before the rest when signed files are queued too.
        The dry run (simulation) is one transaction, as before: nothing is installed by it, and the program a
        bridge is for can only be resolved together with its bridge. It installs the same as the real run.
        '--' before the names: no name is read as an option."""
        return [args for args, _acts in self._package_plan(dry_run)]

    def _install_args(self, inst, dry, unsigned):
        args = ["install"]
        if any(a.opts.get("oldpackage") for a in inst):
            args.append("--oldpackage")
        if unsigned:
            args.append("--allow-unsigned-rpm")
        if self.settings.get("apply_no_recommends"):
            args.append("--no-recommends")
        if self.settings.get("apply_accept_licenses"):
            args.append("--auto-agree-with-licenses")
        return args + dry

    def _package_plan(self, dry_run=False):
        """package_args, each command with the queued changes it carries: [(args, [Action])]."""
        skip = set(map(id, self.not_simulable())) if dry_run else set()
        every_inst = self.of("install")
        inst = [a for a in every_inst if id(a) not in skip]
        rem = [a for a in self.of("remove") if not a.opts.get("clean_deps")]
        clean = [a for a in self.of("remove") if a.opts.get("clean_deps")]
        dry = ["--dry-run"] if dry_run else []
        out = []
        if clean:
            out.append((["remove", "--clean-deps", *dry, "--", *(a.target for a in clean)], clean))
        if not every_inst:
            if rem:
                out.append((["remove", "--no-clean-deps", *dry, "--", *(a.target for a in rem)], rem))
            return out
        # also when the dry run leaves the installs out: the real run is an install
        if not dry_run:
            bridges = [a for a in inst if a.opts.get("bridge_for")]
            if bridges:
                out.append((self._install_args(bridges, dry, True) + ["--", *(a.target for a in bridges)], bridges))
                inst = [a for a in inst if not a.opts.get("bridge_for")]
            files = [a for a in inst if is_file_target(a.target, a.opts)]
            unsigned = [a for a in files if a.opts.get("allow_unsigned")]
            if unsigned and len(unsigned) < len(files):
                out.append((self._install_args(unsigned, dry, True) + ["--", *(a.target for a in unsigned)],
                            unsigned))
                gone = set(map(id, unsigned))
                inst = [a for a in inst if id(a) not in gone]
                unsigned = []
            flag = bool(unsigned)
        else:
            flag = any(a.opts.get("allow_unsigned") for a in inst)
        names = [a.target for a in inst] + ["!" + a.target for a in rem]
        if names:
            out.append((self._install_args(inst, dry, flag) + ["--"] + names, inst + rem))
        return out

    def unsigned_files(self):
        """The files the queue installs without a signature (--allow-unsigned-rpm): bridges and files accepted
        as they are."""
        return [a for a in self.of("install") if a.opts.get("allow_unsigned") and is_file_target(a.target, a.opts)]

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
        # A lock on a package this queue removes runs after the removal: before it, the lock would hold the
        # installed package too and zypper would stop the removal (removing kernel-firmware-all and keeping it out).
        removed = {a.target.split(":", 1)[-1].split("=", 1)[0] for a in self.of("remove")}
        later = [a for a in self.of("lock") if a.target in removed and not a.opts.get("repo")]

        def lock_steps(kind, sub, actions):
            groups = {}
            for a in actions:
                groups.setdefault((a.opts.get("type", "package"), a.opts.get("repo", ""), a.opts.get("comment", "")),
                                  []).append(a)
            for (typ, repo, comment), acts in groups.items():
                step(acts, "zypper", ["-n", sub, *([] if typ == "package" else ["-t", typ]),
                                      *(["-r", repo] if repo else []), *(["-m", comment] if comment else []),
                                      "--", *(a.target for a in acts)])

        lock_steps("unlock", "removelock", self.of("unlock"))
        lock_steps("lock", "addlock", [a for a in self.of("lock") if a not in later])
        for pkg, acts in self._package_plan():
            step(acts, "zypper", ["-n", *pkg])
        lock_steps("lock", "addlock", later)
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
        n_fp = len([a for a in self._actions if a.kind.startswith("flatpak-")])
        others = len(self._actions) - n_in - n_rm - n_fp
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
