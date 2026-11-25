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

    def __init__(self, program, args, *, cond="success", root=None, lang=None, capture=False, label=None):
        self.program = program  # "zypper", "flatpak", "rpm-import", "history"
        self.args = list(args)
        self.cond = cond
        if root is None:
            root = program in ("rpm-import", "history") or (
                program == "zypper" and zypper_subcommand(self.args) not in USER_SUBCOMMANDS)
        self.root = root
        self.lang = lang or user_lang()
        self.capture = capture
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
        prog = {"rpm-import": "rpm --import", "history": "cat /var/log/zypp/history"}.get(self.program, self.program)
        return " ".join([prog, *(shlex.quote(a) for a in self.args)])


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
        # Nobody types into the child: a question gets end-of-input and zypper takes its default answer
        # (the same as -n), instead of waiting for ever.
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
            verb = {"zypper": "zypper", "rpm-import": "rpm-import", "history": "history"}[st.program]
            self.proc.start("pkexec", [helper, "--lang", st.lang, verb, *st.args])
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
        ok = code in SUCCESS if st.program == "zypper" else code == 0
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
        104: _("nothing matched"),
        105: _("interrupted"),
        106: _("some repositories could not be refreshed"),
        107: _("an RPM scriptlet failed"),
    }
    return meanings.get(code, _("see 'man zypper', EXIT CODES"))
