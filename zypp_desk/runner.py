# SPDX-License-Identifier: GPL-3.0-or-later
"""Run commands that change the system, one after another, and stream their output.

zypper commands that need root go through pkexec and the helper (helper/zypp-desk-helper),
which only accepts zypper with a fixed list of subcommands. polkit's "auth_admin_keep" means the
password is asked once and remembered for a few minutes. Other commands (flatpak, read-only
zypper) run as the user.
"""

import codecs
import os
import shlex

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from . import HELPER_PATHS
from .i18n import _, user_lang

SUCCESS = {0, 100, 101, 102, 103, 106, 107}
PKEXEC_CANCELLED = {126, 127}

# zypper subcommands that work without root (they only read).
USER_SUBCOMMANDS = {"ps", "needs-rebooting", "lu", "list-updates", "lp", "list-patches", "se", "search",
                    "if", "info", "lr", "repos", "ll", "locks", "licenses", "versioncmp", "vcmp",
                    "targetos", "tos", "system-architecture", "patch-check", "pchk"}


# Commands of the helper other than zypper (they always run as root), and how each one is shown.
ROOT_VERBS = {
    "rpm-import": "rpm --import",
    "history": "cat /var/log/zypp/history",
    "key-remove": "rpmkeys --delete",
    "snapper-list": "snapper list",
    "snapper-status": "snapper status",
    "snapper-undo": "snapper undochange",
    "snapper-rollback": "snapper rollback",
    "zypp-conf": "zypp.conf",
    "download-ahead": "download-ahead",
}
# Programs that run as the user (fwupd asks for the password itself, through polkit).
USER_PROGRAMS = {"flatpak", "fwupdmgr"}


_RPM_MAJOR = None


def rpm_major():
    """rpm's major version (4 or 6): rpm 6 names keys by fingerprint, rpm 4 by the gpg-pubkey version-release."""
    global _RPM_MAJOR
    if _RPM_MAJOR is None:
        import re
        import subprocess
        try:
            out = subprocess.run(["rpm", "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"(\d+)\.", out)
            _RPM_MAJOR = int(m.group(1)) if m else 4
        except (OSError, subprocess.SubprocessError):
            _RPM_MAJOR = 4
    return _RPM_MAJOR


def helper_path():
    return next((p for p in HELPER_PATHS if os.path.exists(p)), None)


def zypper_subcommand(args):
    """First argument that is not a global option (skips values of options that take one)."""
    takes_value = {"-c", "--config", "--userdata", "-s", "--table-style", "-p", "--plus-repo",
                   "--plus-content", "--releasever", "-R", "--root", "--installroot", "-D", "--reposd-dir",
                   "-C", "--cache-dir", "--raw-cache-dir", "--solv-cache-dir", "--pkg-cache-dir", "--servicesd-dir"}
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in takes_value:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return ""


class Step:
    """One command. 'cond' is 'always' (like ';') or 'success' (like '&&')."""

    def __init__(self, program, args, *, cond="success", root=None, lang=None, capture=False, label=None,
                 strict=False, shown=None):
        self.program = program  # "zypper", a USER_PROGRAMS entry or a ROOT_VERBS entry
        self.args = list(args)
        self.cond = cond
        # strict: only exit code 0 counts as success, as in a shell ('&&' in routines). Otherwise zypper's
        # informational codes (100-107: updates available, restart needed, a repository skipped…) also do.
        self.strict = strict
        if root is None:
            root = program in ROOT_VERBS or (
                program == "zypper" and zypper_subcommand(self.args) not in USER_SUBCOMMANDS)
        self.root = root
        self.lang = lang or user_lang()
        self.capture = capture
        self.shown = shown  # what the command looks like, when the helper turns the arguments into another one
        self.label = label or self.display()
        self.state = "pending"  # pending, running, done, failed, skipped
        self.code = None
        self._chunks = []

    @property
    def output(self):
        return "".join(self._chunks)

    @output.setter
    def output(self, text):
        self._chunks = [text] if text else []

    def display(self):
        if self.shown:
            return self.shown
        a = self.args
        if self.program in ("snapper-status", "snapper-undo") and len(a) == 2:
            return f"{ROOT_VERBS[self.program]} {a[0]}..{a[1]}"
        if self.program == "zypp-conf":
            if a == ["--reset"]:
                return "rm /etc/zypp/zypp.conf.d/50-zypp-desk.conf"
            return "/etc/zypp/zypp.conf.d/50-zypp-desk.conf ← " + " ".join(shlex.quote(x) for x in a)
        if self.program == "download-ahead":
            flag = "/etc/zypp-desk/download-ahead"
            return f"touch {flag}" if a == ["on"] else f"rm {flag}"
        prog = ROOT_VERBS.get(self.program, self.program)
        return " ".join([prog, *(shlex.quote(x) for x in a)])


class Runner(QObject):
    step_started = Signal(int)
    step_finished = Signal(int, int)
    output = Signal(str)
    note = Signal(str, str)  # text, kind (info, error, prompt)
    finished = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.steps = []
        self.proc = None
        self.index = -1
        self.last_ok = True
        self.stop_requested = False
        self._decoder = None

    @property
    def busy(self):
        return self.proc is not None

    def run(self, steps):
        if self.busy:
            return False
        self.steps = list(steps)
        self.index = -1
        self.last_ok = True
        self.stop_requested = False
        self._next()
        return True

    def stop_after_current(self):
        self.stop_requested = True

    def _next(self):
        self.index += 1
        while self.index < len(self.steps):
            st = self.steps[self.index]
            if self.stop_requested:
                st.state = "skipped"
            elif st.cond == "success" and not self.last_ok:
                st.state = "skipped"
                self.note.emit(_("Skipped (the previous step failed): {cmd}").format(cmd=st.display()), "info")
            else:
                self._start(st)
                return
            self.step_finished.emit(self.index, -1)
            self.index += 1
        ok = all(s.state in ("done", "skipped") for s in self.steps) and any(s.state == "done" for s in self.steps)
        ok = ok and not any(s.state == "failed" for s in self.steps)
        self.finished.emit(ok)

    def _start(self, st):
        st.state = "running"
        st.output = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # Nobody can type into the child. zypper reads its answers from the terminal, and without one it gives
        # up ("cannot read input", exit 4): every zypper step that can ask a question must carry -n (the queue
        # and the routines add it). The null device only makes sure nothing waits for ever.
        self.proc.setStandardInputFile(QProcess.nullDevice())
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._error)
        self.step_started.emit(self.index)
        self.note.emit(("# " if st.root else "$ ") + st.display(), "prompt")
        if st.root:
            helper = helper_path()
            if not helper:
                self.note.emit(_("The helper that runs zypper as root is not installed. Run install.sh first."),
                               "error")
                self.proc = None
                st.state, st.code, self.last_ok = "failed", -1, False
                self.step_finished.emit(self.index, -1)
                self._next()
                return
            if st.program != "zypper" and st.program not in ROOT_VERBS:
                raise ValueError(f"not a helper command: {st.program}")
            verb = st.program
            self.proc.start("/usr/bin/pkexec", [helper, "--lang", st.lang, verb, *st.args])  # not a pkexec earlier in PATH
        else:
            env = QProcessEnvironment.systemEnvironment()
            env.insert("LANG", st.lang)
            env.insert("LC_ALL", st.lang)
            env.insert("COLUMNS", "120")
            self.proc.setProcessEnvironment(env)
            self.proc.start(st.program, st.args)

    def _read(self):
        data = bytes(self.proc.readAllStandardOutput())
        text = self._decoder.decode(data)
        st = self.steps[self.index]
        st._chunks.append(text)
        if not st.capture:
            self.output.emit(text)

    def _error(self, err):
        if err == QProcess.ProcessError.FailedToStart:
            st = self.steps[self.index]
            self.note.emit(_("Could not start {prog}.").format(prog=st.program), "error")
            self.proc.deleteLater()
            self.proc = None
            st.state, st.code, self.last_ok = "failed", -1, False
            self.step_finished.emit(self.index, -1)
            self._next()

    def _done(self, code, status):
        if self.proc is None:
            return
        self._read()
        st = self.steps[self.index]
        tail = self._decoder.decode(b"", final=True)
        if tail:
            st._chunks.append(tail)
            if not st.capture:
                self.output.emit(tail)
        if status == QProcess.ExitStatus.CrashExit:
            code = -1
        st.code = code
        if st.program == "zypper" and not st.strict:
            ok = code in SUCCESS
        elif st.program == "fwupdmgr" and not st.strict:
            ok = code in (0, 2)  # 2: nothing to do (already up to date)
        else:
            ok = code == 0
        if st.root and code in PKEXEC_CANCELLED:
            self.note.emit(_("Authentication was cancelled or refused: nothing was changed by this step."), "error")
            ok = False
            self.stop_requested = True  # do not ask for the password again for the next steps
        st.state = "done" if ok else "failed"
        self.last_ok = ok
        if not ok and code not in PKEXEC_CANCELLED:
            if st.program == "zypper":
                self.note.emit(_("Exit code {code}: {meaning}").format(code=code, meaning=exit_meaning(code)), "error")
            else:  # the meanings above are zypper's; other programs (rpm-import, flatpak) print their own reason
                self.note.emit(_("The command ended with exit code {code}.").format(code=code), "error")
        self.proc.deleteLater()
        self.proc = None
        self.step_finished.emit(self.index, code)
        self._next()


def exit_meaning(code):
    meanings = {
        1: _("unexpected error"),
        2: _("wrong syntax"),
        3: _("invalid arguments"),
        4: _("problem reported by libzypp (dependencies, signatures…)"),
        5: _("root privileges are required"),
        6: _("no repositories are defined"),
        7: _("the package manager is locked by another program (for example PackageKit)"),
        8: _("commit error: some packages could not be installed or removed"),
        100: _("updates are available"),
        101: _("security updates are available"),
        102: _("the computer must be restarted"),
        103: _("zypper itself was updated: run the command again"),
        104: _("nothing matched"),
        105: _("interrupted"),
        106: _("some repositories could not be refreshed"),
        107: _("an RPM scriptlet failed"),
    }
    return meanings.get(code, _("see 'man zypper', EXIT CODES"))
