# SPDX-License-Identifier: GPL-3.0-or-later
"""Warnings explained for people who know nothing about zypper, with how serious they are.

Each rule matches a message in English or in Catalan (zypper writes in the user's language). A
line that looks like a warning or an error but matches no rule is still listed, as "unknown".
To add a rule: a regular expression, a severity, a title, what it means and what to do.
"""

import re

from .i18n import _

NONE, INFO, WARN, SERIOUS, UNKNOWN = "none", "info", "warn", "serious", "unknown"
SEVERITY_ORDER = [SERIOUS, WARN, UNKNOWN, INFO, NONE]


def severity_label(sev):
    return {
        NONE: _("No importance"),
        INFO: _("Just so you know"),
        WARN: _("Pay attention"),
        SERIOUS: _("Serious"),
        UNKNOWN: _("Unknown message"),
    }[sev]


def severity_color(sev):
    return {NONE: "#7f8c8d", INFO: "#1d99f3", WARN: "#f67400", SERIOUS: "#da4453", UNKNOWN: "#9b59b6"}[sev]


def _rules():
    return [
        (r"egrep: warning: egrep is obsolescent|fgrep: warning: fgrep is obsolescent", NONE,
         _("An install script uses an old command"),
         _("The small program that runs while a package installs uses 'egrep', an old name for a tool. "
           "It still works; the message only says that the name will disappear some day."),
         _("Nothing. It is the package maker's business.")),
        (r"rmdir: failed to remove .*(No such file|No existeix|no existeix)", NONE,
         _("An install script tried to delete a folder that was not there"),
         _("While updating, the package tried to tidy up a folder that did not exist. Nothing went wrong."),
         _("Nothing.")),
        (r"(Package header is not signed|capçalera del paquet no està signada|is not signed|no està signat)", WARN,
         _("The package is not signed"),
         _("A signature proves who made a package and that nobody changed it. This one has none, so the "
           "system cannot check where it comes from. If you downloaded it from the official web site and the "
           "checksum matched, it is usually fine."),
         _("Install only packages from places you trust.")),
        (r"(NOKEY|unknown key|clau desconeguda|Signature verification failed|ha fallat la verificació de la "
         r"signatura|signatures? .*NOT OK|SIGNATURES NO ÉS CORRECTE)", SERIOUS,
         _("A signature could not be verified"),
         _("The package or the repository is signed with a key your system does not trust, or the signature "
           "does not match. It can be a key that has not been imported yet, but it can also mean the file was "
           "changed."),
         _("Do not go on until you know why. Import the vendor's key only after checking its fingerprint on "
           "the official web site.")),
        (r"(key .* has expired|[Kk]ey .*expired|clau .*ha caducat|caducada)", SERIOUS,
         _("A signing key has expired"),
         _("The key that guarantees a repository is out of date, so nothing from that repository can be "
           "installed or updated until the key is renewed."),
         _("Usually the vendor publishes a renewed key: import it (Repositories → Import a signing key).")),
        (r"(running programs which still use files|programes en execució que encara usen fitxers)", INFO,
         _("Some open programs still use the old version"),
         _("Programs that were open during the update keep using the old files until they are closed. "
           "Nothing is broken."),
         _("Close and reopen them when it suits you. Restart the computer only if Zypp Desk says so.")),
        (r"(about to do a distribution upgrade with all enabled repositories|"
         r"punt de fer una actualització de la distribució)", INFO,
         _("Reminder before a distribution upgrade"),
         _("zypper always says this before 'dup': it will use every enabled repository, so they should all be "
           "meant for your version of the system."),
         _("Nothing, if your repositories are the usual ones.")),
        (r"(excluded from file conflict check|excloure .* de la comprovació de conflictes)", INFO,
         _("Some packages were not checked for file conflicts"),
         _("In a simulation the packages are not downloaded, so zypper cannot look inside them. The real "
           "installation does check them."),
         _("Nothing.")),
        (r"(System management is locked|gestió del sistema està bloquejada)", WARN,
         _("Another program is using the package manager"),
         _("Only one program can install software at a time. Often it is Discover or PackageKit checking for "
           "updates in the background."),
         _("Wait a minute and try again, or close Discover.")),
        (r"(Problem retrieving|Download \(curl\) error|Error en baixar|could not be refreshed|"
         r"no s'ha pogut refrescar|No s'han pogut refrescar|Repository .* is invalid|Timeout exceeded|"
         r"Could not resolve host)", WARN,
         _("A repository did not answer"),
         _("The server of a repository did not respond, or there is no Internet connection. zypper uses what "
           "it already knew about that repository."),
         _("Try again later. If it happens every time, that repository may no longer exist: disable it in "
           "Repositories.")),
        (r"(nothing provides|res no proporciona|conflicts with|entra en conflicte amb|Problem: |Problema: )",
         SERIOUS,
         _("A dependency problem"),
         _("What you asked for needs something that is missing, or clashes with something you have. zypper "
           "stops so it does not break anything."),
         _("Look at the simulation: change the queue (lock or remove the package that clashes) and simulate "
           "again.")),
        (r"(scriptlet failed|%post.*failed|ha fallat un script|warning: %(post|pre|postun|preun))", WARN,
         _("A package's install script failed"),
         _("The package was installed, but the small program that sets it up afterwards reported an error. "
           "Sometimes it does not matter; sometimes something stays unconfigured."),
         _("If the program does not work well, reinstall that package.")),
        (r"(Reboot is suggested|reboot is required|Cal reiniciar|reinici suggerit|needs to be rebooted)", WARN,
         _("Restart the computer"),
         _("An update changed something basic (for example the kernel or the system libraries) that only "
           "takes effect after a restart."),
         _("Restart when it suits you.")),
        (r"(Not running as root you are limited|Sense executar com a root)", NONE,
         _("Checked without administrator rights"),
         _("This check ran as your user, so it could only look at your own programs."),
         _("Nothing.")),
        (r"(end-of-life|end of life|(?-i:\bEOL\b))", WARN,
         _("Something no longer gets updates"),
         _("A Flatpak runtime or app has reached its end of life: it will not get security fixes any more."),
         _("Update the app; if it keeps using it, look for a newer version or an alternative.")),
        (r"(File conflicts happen|conflictes entre fitxers .* (detectat|trobat)|file .* conflicts with file)",
         SERIOUS,
         _("Two packages want the same file"),
         _("Two packages contain a file with the same name. Installing both could overwrite one with the "
           "other."),
         _("Usually one of them comes from an extra repository: choose one of the two.")),
    ]


_COMPILED = None


def severity_of_line(line):
    """The severity of a known message, or None (used to colour the terminal like the explanation)."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = [(re.compile(rx, re.I), sev) for rx, sev, *_rest in _rules()]
    for rx, sev in _COMPILED:
        if rx.search(line):
            return sev
    return None


# A line counts as an unknown warning only when it looks like one ("warning: …", "Error: …",
# "… failed"), not when a package name contains the word (libgpg-error0, perl-Error).
_WARN_RE = re.compile(r"(^|\s)(warning|warnung|avís|aviso|attention|error|erreur|fehler|err|problem|problema)\s*[:!]"
                      r"|\bfailed\b|\bha fallat\b", re.I)
_SKIP_RE = re.compile(r"^(\(\d+/\d+\)|Retrieving|Obtenint|Precarregant|Preloading|Installing|Instal·lant|Removing|"
                      r"Suprimint|Comprovant|Checking|Loading|Carregant)")


def explain(text):
    """Group the messages of a run. Returns a list of dicts, most serious first."""
    rules = [(re.compile(rx, re.I), sev, title, what, todo) for rx, sev, title, what, todo in _rules()]
    found, unknown = {}, {}
    last = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("⋯"):
            # the terminal folded repeated lines: "⋯ the same line 24 more times"
            m = re.search(r"(\d+)", s)
            if last is not None and m:
                last["count"] += int(m.group(1))
            continue
        last = None
        if not s or s.startswith(("#", "$")):
            continue
        for rx, sev, title, what, todo in rules:
            if rx.search(s):
                d = found.setdefault(title, dict(severity=sev, title=title, what=what, todo=todo, count=0, example=s))
                d["count"] += 1
                last = d
                break
        else:
            if _WARN_RE.search(s) and not s.startswith("CommitResult") and not _SKIP_RE.match(s):
                d = unknown.setdefault(s[:160], dict(
                    severity=UNKNOWN, title=_("A message Zypp Desk does not know yet"),
                    what=_("zypper (or a package) wrote this and Zypp Desk has no explanation for it. It is "
                           "not necessarily bad: many messages are only informative."),
                    todo=_("If something does not work afterwards, search for the exact text on the Internet."),
                    count=0, example=s))
                d["count"] += 1
                last = d
    items = list(found.values()) + list(unknown.values())[:8]
    items.sort(key=lambda d: SEVERITY_ORDER.index(d["severity"]))
    return items
