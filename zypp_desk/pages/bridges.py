# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge packages in the window: the question before making one, and the list in Programs from the web.

ask_bridge() is the question before making a bridge: the program, what it asks for by its Fedora
names and what openSUSE calls each thing, the bridge Zypp Desk will make, and the commands. It only puts things in
the queue, so it says so and asks for no password; what else there is to know about the
file (not signed, older than yours) goes in the same question, as a warning, instead of a question of its own.
queue_bridge() writes the bridge (bridge.build_bridge, as the normal user) and puts it in the queue next to
the program. The queue installs both (TxQueue.package_args); the bridge with --allow-unsigned-rpm: it is made
here, nobody signs it.

BridgesBox is the "Bridge packages" list of the Programs from the web page: each bridge with its program, the
names it translates and whether it is still needed (bridge.check_bridges), and "Remove it".
"""

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import bridge, websources
from ..i18n import _, ngettext
from ..question import ask_change, box, item, label
from ..runner import Step
from ..style import BLUE, GREEN, ORANGE
from ..tasks import run_async
from ..txqueue import Action, TxQueue
from ..ui import button, heading, icon, mono, muted

GREY = "#7f8c8d"
RPM_ICON = "application-x-rpm|package-x-generic"


def home_short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def queue_buttons(yes):
    """The buttons of a question whose answer only adds to the queue: "Not yet" (Enter) and `yes`, which is never the
    green one, since green is only for what changes the computer now."""
    return [("no", _("Not yet"), False), ("yes", yes, False)]


def system_words(plan):
    """(the system the program was made for, how that system is called in "its … names")."""
    kind, full = plan["system"]
    if kind == "fedora":
        return full, "Fedora"
    if kind == "rhel":
        return full, "Red Hat"
    return "", ""


def _title(plan):
    kind = plan["system"][0]
    if kind == "fedora":
        return _("This program is for Fedora")
    if kind == "rhel":
        return _("This program is for Red Hat Enterprise Linux")
    return _("This program was made for another system")


def _targets_text(m):
    """'✓ filesystem, fontconfig' (you have them) or '+ libfoo2 (from repo-oss)' (zypper installs it)."""
    parts = []
    for t in m["targets"]:
        if t.get("installed", True):
            parts.append(t["name"])
        else:
            parts.append(_("{name} (zypper installs it from the repository {repo})").format(
                name=t["name"], repo=t.get("repo") or "?"))
    every = all(t.get("installed", True) for t in m["targets"])
    return ("✓ " if every else "+ ") + ", ".join(parts), GREEN if every else BLUE


def mapping_table(mappings, system_short):
    """The table of the question: what it asks for by its Fedora name, what openSUSE calls it, and why."""
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(6)
    heads = [_("Asks for ({system} name)").format(system=system_short) if system_short else _("Asks for"),
             _("In openSUSE"), _("Why")]
    for c, h in enumerate(heads):
        g.addWidget(label(h, bold=True, small=True, muted=True), 0, c)
    for r, m in enumerate(mappings, 1):
        g.addWidget(label(m["text"], small=True, selectable=True, font=mono()), r, 0, Qt.AlignmentFlag.AlignTop)
        text, color = _targets_text(m)
        g.addWidget(label(text, small=True, color=color, selectable=True), r, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(label(m["why"], small=True, muted=True), r, 2, Qt.AlignmentFlag.AlignTop)
    g.setColumnStretch(0, 11)
    g.setColumnStretch(1, 8)
    g.setColumnStretch(2, 16)
    return w


def _bridge_box(plan, folder):
    """The box that says what the bridge is, its name and where it goes."""
    f = QFrame()
    f.setObjectName("qbridge")
    f.setStyleSheet("QFrame#qbridge { background: palette(base); border: 1px solid rgba(127, 140, 141, 0.35); "
                    "border-radius: 10px; }")
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 9, 12, 10)
    v.setSpacing(4)
    v.addWidget(label(_("A “bridge package”"), bold=True))
    maps = list(plan["kept"]) + list(plan["mapped"])
    if len(maps) == 1:
        m = maps[0]
        what = _("Zypp Desk makes a small, empty package that tells zypper: “{fedora} is already here: it is "
                 "{opensuse}”. Then the program installs properly, and updates will not complain later.").format(
            fedora=m["name"], opensuse=", ".join(t["name"] for t in m["targets"]))
    else:
        what = _("Zypp Desk makes a small, empty package that tells zypper that these names are already here, "
                 "under their openSUSE names. Then the program installs properly, and updates will not complain "
                 "later.")
    v.addWidget(label(what, small=True, muted=True))
    old = plan.get("bridge")
    if old:
        v.addWidget(label(_("Your bridge {name} (release {old}) does not cover what this version asks for, so Zypp "
                            "Desk makes release {new}, which covers all of it.").format(
            name=old["name"], old=old["release"], new=plan["release"]), small=True))
    v.addWidget(label(_("It will be called {name} and saved in the folder of programs from the web ({folder}). "
                        "Programs from the web → Bridge packages lists it and says when it is no longer needed.")
                      .format(name=plan["bridge_name"], folder=folder or "?"), small=True))
    return f


def bridge_actions(plan, program_action, folder, built=None):
    """The queue actions of the bridge and the program (in this order). folder: the folder of programs from the
    web, where Apply moves the bridge (a local zypper repository: zypper keeps seeing it, so it is never an
    orphan). built: what build_bridge returned; without it (the question) a planned file, with no checksum."""
    path = built["path"] if built else bridge.planned_file(plan, str(websources.download_dir()))
    b = Action("install", path, package=plan["bridge_name"], allow_unsigned=True,
               sha256=(built or {}).get("sha256") or "planned", move_to=folder or None, bridge_for=plan["program"])
    return [b, program_action]


def commands_for(plan, program_action, settings):
    """The commands of the question: the bridge written here (no command runs for it), then zypper's (as the queue
    will run them: the bridge may be installed on its own first)."""
    path = bridge.planned_file(plan, str(websources.download_dir()))
    q = TxQueue(settings)
    q.actions = bridge_actions(plan, program_action, "")
    home = os.path.expanduser("~") + "/"
    # shorter, and the same for the shell
    zyp = [Step("zypper", ["-n", *args]).display().replace(" " + home, " ~/") for args in q.package_args()]
    return [_("Zypp Desk writes {file} (as you, without rpmbuild)").format(file=home_short(path)), *zyp]


def ask_bridge(parent, plan, info, program_action, settings, folder, warnings=()):
    """The question before making a bridge. True when the answer is "Create the bridge and add both to the
    queue". warnings: what else to know about the file (not signed, older than yours), said here instead of in a
    question of its own."""
    system, short = system_words(plan)
    maps = list(plan["mapped"]) + list(plan["kept"])
    n = len(plan["mapped"])
    made_for = _("made for {system}").format(system=system) if system else _("made for another system")
    head = item(RPM_ICON, "{name} {version} ({made_for})".format(name=info.get("name", ""),
                                                                  version=info.get("version", ""), made_for=made_for),
                info.get("summary") or "")
    if short:
        lead = ngettext("It asks for {n} thing by its {system} name. openSUSE has the same thing under another "
                        "name:", "It asks for {n} things by their {system} names. openSUSE has the same things "
                        "under other names:", n).format(n=n, system=short)
    else:
        lead = ngettext("It asks for {n} thing by a name openSUSE does not use. openSUSE has the same thing under "
                        "another name:", "It asks for {n} things by names openSUSE does not use. openSUSE has the "
                        "same things under other names:", n).format(n=n)
    extra = [head, box("warn", lead), mapping_table(maps, short), _bridge_box(plan, folder)]
    if plan["available"]:
        extra.append(box("info", ngettext("It also needs {list}, which your repositories have: zypper installs it "
                                          "with the program.",
                                          "It also needs {list}, which your repositories have: zypper installs them "
                                          "with the program.", len(plan["available"])).format(
            list=", ".join(plan["available"]))))
    answer = ask_change(
        parent, _title(plan), window_title=_("A program for another system"), icon_name=RPM_ICON, extra=extra,
        means=_("both packages go to the queue together. Nothing is installed now: zypper installs them when you "
                "apply the queue."),
        warning="\n\n".join(warnings) or None,
        commands_=commands_for(plan, program_action, settings), password=False, width=780, help_topic="web",
        buttons=queue_buttons(_("Create the bridge and add both to the queue")))
    return answer == "yes"


def ask_unmapped(parent, plan, info, warnings=()):
    """A program from the web that asks for things openSUSE has under no name Zypp Desk knows: say which, and
    let you decide whether it goes to the queue anyway (zypper will say it cannot install it). No bridge.
    warnings: what else to know about the file (not signed…)."""
    rows = "\n".join("• {req}: {why}".format(req=r, why=why) for r, why in plan["unmapped"])
    system = system_words(plan)[0]
    made_for = _("made for {system}").format(system=system) if system else _("made for another system")
    head = item(RPM_ICON, "{name} {version} ({made_for})".format(name=info.get("name", ""),
                                                                  version=info.get("version", ""), made_for=made_for),
                info.get("summary") or "")
    answer = ask_change(
        parent, _("openSUSE does not have everything this program asks for"), icon_name=RPM_ICON,
        extra=[head, box("error", rows, ngettext("It asks for {n} thing that openSUSE does not have:",
                                                  "It asks for {n} things that openSUSE does not have:",
                                                  len(plan["unmapped"])).format(n=len(plan["unmapped"])))],
        means=_("zypper will not be able to install it as it is. A bridge package cannot help here: openSUSE has "
                "these things under no name Zypp Desk knows. Look for a version of the program made for openSUSE."),
        warning="\n\n".join(warnings) or None, password=False, width=720, help_topic="web",
        buttons=queue_buttons(_("Add it to the queue anyway")))
    return answer == "yes"


def queue_bridge(ctx, plan, program_action, folder):
    """Write the bridge into the private download folder (Apply moves it to folder, the folder of programs from
    the web) and queue the bridge and the program together. Returns the bridge's file, or raises OSError /
    rpmwrite.RpmWriteError."""
    built = bridge.build_bridge(plan, str(websources.download_dir()), web_folder=folder)
    with ctx.queue.batch():
        for a in bridge_actions(plan, program_action, folder, built):
            ctx.queue.add(a)
    return built["path"]


# ------------------------------------------------------------------ the list in Programs from the web

STATE = {
    "needed": (GREEN, lambda b: _("{program} needs it.").format(program=b["program"])),
    "orphan": (ORANGE, lambda b: _("It can go: {program} is not installed any more.").format(program=b["program"])),
    "unused": (ORANGE, lambda b: _("It can go: {program} no longer asks for these names.").format(
        program=b["program"])),
    "replaced": (ORANGE, lambda b: _("It can go: openSUSE now has these names itself ({list}).").format(
        list=", ".join(sorted({x["package"] for v in b["real"].values() for x in v if x["installed"]})))),
    "replaceable": (ORANGE, lambda b: _("It can go: openSUSE now has these names itself. Removing it installs "
                                        "{list} instead.").format(list=", ".join(b["install"]))),
}


class BridgesBox(QFrame):
    """The bridge packages installed now, whether each one is still needed, and "Remove it"."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)  # the list above gets the rest
        self.bridges = []
        self._round = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(heading(_("Bridge packages"), 1.05))
        self.state = muted("", self)
        top.addWidget(self.state, 1)
        v.addLayout(top)
        v.addWidget(muted(_("Zypp Desk made them so that programs made for Fedora could be installed: each one tells "
                            "zypper that a Fedora name is met by what openSUSE calls it. Zypp Desk checks them each "
                            "time you open this page."), self))
        self.rows = QWidget(self)
        self.rows_lay = QVBoxLayout(self.rows)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(6)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows)
        self.scroll.viewport().setBackgroundRole(QPalette.ColorRole.Base)  # the card's colour
        v.addWidget(self.scroll)
        self.hide()
        queue = getattr(ctx, "queue", None)
        if queue is not None:  # "Remove it" becomes "In the queue", and back
            queue.changed.connect(self._queue_changed)

    def _queue_changed(self):
        if self.bridges:
            self._fill()

    def load(self):
        """Look at the installed bridges (rpm, at once) and whether each is still needed (rpm and one zypper
        search, in a thread). The box stays hidden when there is none."""
        self._round += 1
        rnd = self._round

        def look():
            found = bridge.installed_bridges()
            return found, (bridge.check_bridges(found) if found else [])
        self.state.setText(_("checking…"))
        run_async(look, on_done=lambda res, err: self._loaded(rnd, res, err))

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._fit)

    def _loaded(self, rnd, res, err):
        if rnd != self._round:
            return
        if err or res is None:
            self.state.setText(_("could not check: {error}").format(error=err))
            return
        _found, checked = res
        self.bridges = checked
        self.state.setText("")
        self._fill()

    def _fill(self):
        while self.rows_lay.count():
            it = self.rows_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for b in self.bridges:
            self.rows_lay.addWidget(self._row(b))
        self.rows_lay.addStretch(1)
        self.setVisible(bool(self.bridges))
        QTimer.singleShot(0, self._fit)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _fit(self):
        """As tall as three bridges at this width; more scroll."""
        width = self.scroll.viewport().width()
        if not self.isVisible() or width < 200:  # not laid out yet: it would measure every line as one word wide
            return
        heights = []
        for i in range(min(3, self.rows_lay.count())):
            w = self.rows_lay.itemAt(i).widget()
            if w is None:
                continue
            lay = w.layout()
            heights.append(lay.heightForWidth(width) if lay.hasHeightForWidth() else w.sizeHint().height())
        if heights:
            self.scroll.setFixedHeight(sum(heights) + 6 * (len(heights) - 1) + 8)

    def _row(self, b):
        f = QFrame()
        f.setObjectName("bridgerow")
        f.setStyleSheet("QFrame#bridgerow { border: 1px solid rgba(127, 140, 141, 0.30); border-radius: 10px; }")
        h = QHBoxLayout(f)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(12)
        pic = QLabel()
        pic.setPixmap(icon(*RPM_ICON.split("|")).pixmap(28, 28))
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        v.addWidget(label(_("{name} {version}, for {program}").format(name=b["name"], version=b["evr"],
                                                                     program=b["program"]), bold=True))
        lines = ["{fedora} → {opensuse}".format(fedora=n, opensuse=", ".join(b["maps"].get(n) or ["?"]))
                 for n, _f, _v in b["provides"]]
        v.addWidget(label("\n".join(lines) or "—", small=True, selectable=True, font=mono()))
        state = b.get("state", "needed")
        color, text = STATE.get(state, (GREY, lambda _b: ""))
        st = QHBoxLayout()
        st.setSpacing(6)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color};")
        st.addWidget(dot)
        st.addWidget(label(text(b), small=True), 1)
        v.addLayout(st)
        if state == "needed" and len(b.get("asked", {})) < len(b["provides"]):
            v.addWidget(label(_("Some of its names are no longer asked for: when you install a new version of "
                                "{program}, Zypp Desk makes a smaller bridge.").format(program=b["program"]),
                              small=True, muted=True))
        h.addLayout(v, 1)
        queued = self.ctx.queue.state_of(b["name"]) == "remove"
        # it only puts the removal in the queue, and says so; a bridge still needed asks first ("…")
        if queued:
            text = _("In the queue ✓")
            tip = _("Its removal waits in the queue: apply the queue to remove it.")
        elif state == "needed":
            text = _("Put its removal in the queue…")
            tip = _("{program} still needs it: Zypp Desk asks first, because removing the bridge removes {program} "
                    "too. Nothing is removed until you apply the queue.").format(program=b["program"])
        else:
            text = _("Put its removal in the queue")
            tip = _("zypper removes {name} when you apply the queue.").format(name=b["name"])
            if state == "replaceable":
                tip += " " + _("It installs {list} instead.").format(list=", ".join(b.get("install", [])))
        rm = button(text, "list-remove", lambda _c=False, b=b: self.remove(b))
        rm.setToolTip(tip)
        rm.setEnabled(not queued)
        h.addWidget(rm, 0, Qt.AlignmentFlag.AlignVCenter)
        return f

    def remove(self, b):
        """Queue the removal of a bridge. One that is still needed takes its program with it: ask first."""
        state = b.get("state", "needed")
        actions = [Action("install", p) for p in b.get("install", [])] if state == "replaceable" else []
        actions.append(Action("remove", b["name"], package=b["name"]))
        if state == "needed":
            q = TxQueue(self.ctx.settings)
            q.actions = list(actions)
            cmds = [Step("zypper", ["-n", *a]).display() for a in q.package_args()]
            ok = ask_change(
                self, _("Remove the bridge of {program}?").format(program=b["program"]), icon_name=RPM_ICON,
                items=[(RPM_ICON, b["name"], _("Tells zypper that {program} has what it asks for by its Fedora "
                                               "names.").format(program=b["program"]))],
                warning=_("{program} still needs it: removing the bridge removes {program} too.").format(
                    program=b["program"]),
                means=_("both removals go to the queue. Nothing is removed until you apply it."),
                commands_=cmds, password=False, help_topic="web",
                buttons=queue_buttons(_("Put both removals in the queue"))) == "yes"
            if not ok:
                return
        with self.ctx.queue.batch():
            for a in actions:
                self.ctx.queue.add(a)
        self._fill()
