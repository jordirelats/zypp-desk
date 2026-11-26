# SPDX-License-Identifier: GPL-3.0-or-later
"""Repositories: the priority stepper of the cards, "Suggest an order…", "Change the address…", and the banner of a
repository left for Fedora (yumrepos.py) with its question."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QRadioButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import repocheck, repoprio, yumrepos
from ..i18n import _, ngettext, pgettext
from ..question import confirm
from ..question import label as plain_label
from ..style import BLUE, GREEN, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action, TxQueue
from ..ui import button, error_box, heading, icon, mono, muted

REPO_ICON = "folder-cloud|repository|folder-download"

HOW_IT_WORKS = lambda: _(  # noqa: E731
    "Every repository has a priority, a number from 1 to 200. When a package is in more than one repository, "
    "zypper takes it from the one with the lowest number, even if another one has a newer version. With the same "
    "number, the newest version wins, wherever it comes from.\n\n"
    "99 is the default. A lower number (90) makes a repository win: useful for the codecs of VideoLAN or Packman. "
    "A higher number (110, 120) makes it give way: useful for personal repositories, so that they only bring what "
    "openSUSE does not have.\n\n"
    "zypper also keeps each package with the vendor it came from: a priority alone does not move a program from "
    "openSUSE to another vendor (that needs “Allow a change of vendor”).")


def tint(color, alpha=0.14):
    """rgba() of a #rrggbb colour (Qt reads #rrggbbaa as #aarrggbb, so no alpha in hex)."""
    c = color.lstrip("#")
    return f"rgba({int(c[0:2], 16)}, {int(c[2:4], 16)}, {int(c[4:6], 16)}, {alpha})"


def pill(text, color):
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setStyleSheet(f"color: {color}; background: {tint(color)}; border-radius: 9px; padding: 1px 8px;")
    return lbl


class PriorityStepper(QWidget):
    """− 99 + with the kind of repository the number means under it. It waits a moment after the last click
    before it says the new number, so five clicks are one change in the queue."""

    changed = Signal(int)

    def __init__(self, value, parent=None):
        super().__init__(parent)
        self.value = int(value)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        box = QFrame(self)
        box.setObjectName("stepper")
        box.setStyleSheet("QFrame#stepper { border: 1px solid rgba(127, 140, 141, 0.55); border-radius: 8px; }")
        h = QHBoxLayout(box)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(0)
        self.minus = QToolButton(box)
        self.minus.setText("−")
        self.minus.setAutoRaise(True)
        self.minus.setAutoRepeat(True)
        self.plus = QToolButton(box)
        self.plus.setText("+")
        self.plus.setAutoRaise(True)
        self.plus.setAutoRepeat(True)
        self.num = QLabel(box)
        f = self.num.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.1)
        self.num.setFont(f)
        self.num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.num.setMinimumWidth(34)
        h.addWidget(self.minus)
        h.addWidget(self.num)
        h.addWidget(self.plus)
        v.addWidget(box)
        self.tier = QLabel(self)
        self.tier.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tf = self.tier.font()
        tf.setPointSizeF(tf.pointSizeF() * 0.85)
        self.tier.setFont(tf)
        v.addWidget(self.tier)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(800)
        self.timer.timeout.connect(lambda: self.changed.emit(self.value))
        self.minus.clicked.connect(lambda: self._step(-1))
        self.plus.clicked.connect(lambda: self._step(1))
        self.setToolTip(_("Priority: the lowest number wins."))
        self._show()

    def _step(self, d):
        self.value = max(1, min(200, self.value + d))
        self._show()
        self.timer.start()

    def _show(self):
        self.num.setText(str(self.value))
        t = repoprio.tier_of(self.value) if self.value != 99 else None
        if self.value == 99:
            self.tier.setText(_("default"))
            self.tier.setStyleSheet("color: palette(placeholder-text);")
        elif t:
            self.tier.setText(t[0])  # as written: German nouns keep their capital
            self.tier.setStyleSheet(f"color: {t[1]};")
        else:
            self.tier.setText(_("wins") if self.value < 99 else _("gives way"))
            self.tier.setStyleSheet("color: palette(placeholder-text);")


class ProposeOrderDialog(QDialog):
    """A priority for every enabled repository, by kind; each kind can be left out."""

    def __init__(self, repos, current, parent=None):
        """repos: dicts from backend.repos(); current(r) -> the priority it has (with the queue)."""
        super().__init__(parent)
        self.setWindowTitle(_("Suggest an order of priority"))
        self.resize(760, 720)
        self.repos = [r for r in repos if r["enabled"] and not r.get("service")]
        self.current = current
        lay = QVBoxLayout(self)
        lay.addWidget(heading(_("Suggest an order of priority"), 1.3, self))
        lay.addWidget(muted(ngettext(
            "For your {n} enabled repository. Untick a kind to leave it as it is; afterwards you can change any "
            "number on its card.",
            "For your {n} enabled repositories. Untick a kind to leave it as it is; afterwards you can change any "
            "number on its card.", len(self.repos)).format(n=len(self.repos)), self))
        groups = {}
        for r in self.repos:
            groups.setdefault(repoprio.proposal(r), []).append(r)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        self.checks = {}
        for prio, name, color, why in repoprio.tiers():
            items = sorted(groups.get(prio, []), key=lambda r: r["name"].lower())
            if not items:
                continue
            row = QFrame(inner)
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 8, 4, 8)
            cb = QCheckBox(row)
            changes = [r for r in items if int(current(r)) != prio]
            cb.setChecked(bool(changes))
            cb.setEnabled(bool(changes))
            cb.toggled.connect(self._update)
            self.checks[prio] = (cb, items)
            h.addWidget(cb, 0, Qt.AlignmentFlag.AlignTop)
            num = QLabel(str(prio), row)
            f = num.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() * 1.6)
            num.setFont(f)
            num.setStyleSheet(f"color: {color};")
            num.setMinimumWidth(50)
            num.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            h.addWidget(num)
            tv = QVBoxLayout()
            tv.setSpacing(2)
            th = QHBoxLayout()
            title = QLabel(name, row)
            tf = title.font()
            tf.setBold(True)
            title.setFont(tf)
            th.addWidget(title)
            th.addWidget(pill(ngettext("{n} change", "{n} changes", len(changes)).format(n=len(changes))
                              if changes else _("already there"), color if changes else "#7f8c8d"))
            th.addStretch(1)
            tv.addLayout(th)
            tv.addWidget(muted(why, row))
            names = QLabel(", ".join(r["name"].split(" (")[0] for r in items), row)
            names.setTextFormat(Qt.TextFormat.PlainText)
            names.setWordWrap(True)
            tv.addWidget(names)
            h.addLayout(tv, 1)
            row.setStyleSheet("QFrame { border-bottom: 1px solid rgba(127, 140, 141, 0.30); }"
                              "QLabel, QCheckBox { border: none; }")
            iv.addWidget(row)
        iv.addStretch(1)
        sc = QScrollArea(self)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setWidget(inner)
        lay.addWidget(sc, 1)
        note = muted(_("Changing it installs or removes nothing now. It decides where each package will come from "
                       "in the next installs and updates."), self)
        lay.addWidget(note)
        self.cmd = QLabel(self)
        self.cmd.setFont(mono())
        self.cmd.setWordWrap(True)
        self.cmd.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(self.cmd)
        btns = QHBoxLayout()
        b_help = button(_("How it works"), "help-about", lambda: _how_it_works(self))
        btns.addWidget(b_help)
        btns.addStretch(1)
        btns.addWidget(button(_("Cancel"), None, self.reject))
        self.ok = button(_("Add to the queue"), "list-add", self.accept, primary=True)
        btns.addWidget(self.ok)
        lay.addLayout(btns)
        self._update()

    def chosen(self):
        """[(repo, new priority)] for the ticked kinds."""
        out = []
        for prio, (cb, items) in self.checks.items():
            if cb.isChecked():
                out += [(r, prio) for r in items if int(self.current(r)) != prio]
        return out

    def _update(self):
        ch = self.chosen()
        self.ok.setEnabled(bool(ch))
        if ch:
            from ..txqueue import TxQueue
            r, p = ch[0]
            more = len(ch) - 1
            q = TxQueue({})
            q.actions = [Action("repo-modify", r["alias"], priority=p)]
            self.cmd.setText(q.steps()[0].display() + (" … " + ngettext(
                "(and {n} more)", "(and {n} more)", more).format(n=more) if more else ""))
        else:
            self.cmd.setText(_("Nothing to change."))


def _how_it_works(parent):
    from ..ui import info_box
    info_box(parent, _("How the priority works"), HOW_IT_WORKS())


class ChangeUrlDialog(QDialog):
    """A repository at a new address: written by hand, or found again among what its OBS project publishes.

    The address must look right (repoprio.url_problem), and it is checked: “Add to the queue” waits until the check
    has answered, and an address that does not answer goes to the queue only after a question. zypper cannot
    change an address: the queue adds the repository again from a .repo file with every setting of the old one
    (repoprio.new_address_file), under <alias>-zdnew, removes the old one and gives the new one its alias. A change
    that already waits in the queue for this repository (enabled, priority…) stays there and runs after it.
    """

    def __init__(self, repo, health, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.repo_file = None  # written when the change goes to the queue
        self._answer = None  # (url, "ok" / "fail", text) of the last check of the address
        self.setWindowTitle(_("Change the address of “{name}”").format(name=repo["name"]))
        self.resize(760, 640)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        title = heading(_("Change the address of “{name}”").format(name=repo["name"]), 1.3, self)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        lay.addWidget(title)
        failing = bool(health and health.get("state") == "fail")
        state = QLabel(self)
        state.setTextFormat(Qt.TextFormat.PlainText)
        state.setWordWrap(True)
        if failing:
            state.setText(_("The current address does not answer: {url} ({why}).").format(url=repo["url"],
                                                                                          why=health["text"]))
            # a tint, not a fixed colour: readable in light and dark themes
            state.setStyleSheet(f"background: {tint(ORANGE, 0.14)}; border-radius: 8px; padding: 8px;")
        else:
            state.setText(_("Current address: {url}").format(url=repo["url"]))
            state.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(state)

        lay.addWidget(QLabel("<b>" + _("Address") + "</b> " + _("(you can write it yourself)"), self))
        er = QHBoxLayout()
        self.edit = QLineEdit(repo["url"], self)
        self.edit.setFont(mono())
        self.edit.textChanged.connect(self._edited)
        er.addWidget(self.edit, 1)
        self.b_check = button(_("Check it"), "network-connect", self._check)
        self.b_check.setAutoDefault(False)
        er.addWidget(self.b_check)
        lay.addLayout(er)
        self.check_lbl = QLabel(self)
        self.check_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.check_lbl.setWordWrap(True)
        lay.addWidget(self.check_lbl)
        self.timer = QTimer(self)  # the address is checked a moment after the last key press
        self.timer.setSingleShot(True)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self._check)

        lay.addWidget(QLabel("<b>" + _("Find it again") + "</b>", self))
        self.found_lbl = muted(_("Looking at what this repository's project publishes…"), self)
        self.found_lbl.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.found_lbl)
        # the folders of the project, in a scroll area: each row keeps its height, however many there are
        inner = QWidget()
        self.cands = QVBoxLayout(inner)
        self.cands.setContentsMargins(0, 0, 0, 0)
        self.cands.setSpacing(6)
        self.cands.addStretch(1)
        self.cand_area = QScrollArea(self)
        self.cand_area.setWidgetResizable(True)
        self.cand_area.setFrameShape(QFrame.Shape.NoFrame)
        self.cand_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cand_area.setWidget(inner)
        self.cand_area.hide()
        lay.addWidget(self.cand_area, 1)
        self.group = QButtonGroup(self)
        self.keep_lbl = muted(_("Every setting of the repository stays: the name, whether it is enabled, the priority, "
                                "the refresh, the signature checks and its key. If the project changed its name, "
                                "“Search online…” finds it by the packages you have from it."), self)
        lay.addWidget(self.keep_lbl)
        self.spacer = QWidget(self)  # takes the free room while there is no list of folders
        lay.addWidget(self.spacer, 1)
        self.cmd_title = muted(_("Commands that will run, in this order:"), self)
        lay.addWidget(self.cmd_title)
        from .localrpm import _CommandView  # the exact command, wrapped anywhere (paths have no spaces)
        self.cmd = _CommandView("", self)
        lay.addWidget(self.cmd)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = button(_("Cancel"), None, self.reject)
        cancel.setAutoDefault(False)
        btns.addWidget(cancel)
        self.ok = button(_("Add to the queue"), "list-add", self._accept, primary=True)
        btns.addWidget(self.ok)
        lay.addLayout(btns)
        self._edited()
        if repo.get("service"):
            self.edit.setEnabled(False)
            self.b_check.setEnabled(False)
            self.found_lbl.setText(_("This repository belongs to the service {service}, which sets its address: "
                                     "change the service instead.").format(service=repo["service"]))
            self.ok.setEnabled(False)
        else:
            run_async(repoprio.candidates, repo["url"], on_done=self._found)

    def url(self):
        return self.edit.text().strip()

    def _changed(self, u):
        return bool(u) and u != self.repo["url"] and not self.repo.get("service")

    def _edited(self, *_a):
        u = self.url()
        problem = repoprio.url_problem(self.edit.text()) if u else ""
        changed = self._changed(u) and not problem
        self.cmd.setPlainText(self._commands(u) if changed else "")
        self.cmd_title.setVisible(changed)
        self.cmd.setVisible(changed)
        self.timer.stop()
        if problem and u != self.repo["url"]:
            self._show_check("✗ " + problem, RED)
        elif changed and self._answer and self._answer[0] == u:
            self._show_answer()
        elif changed:
            self._show_check(_("Checking…"), None)
            self.timer.start()
        else:
            self._show_check("", None)
        self._update_ok()
        for b in self.group.buttons():
            b.blockSignals(True)
            b.setChecked(b.property("url") == u)
            b.blockSignals(False)

    def _commands(self, u):
        """The exact commands, as the queue will run them (one line each)."""
        from ..txqueue import TxQueue
        q = TxQueue({})
        q.actions = [self._make_action(u, str(repoprio.new_address_path(self.repo["alias"])))]
        return "\n".join(st.display() for st in q.steps())

    def _update_ok(self):
        u = self.url()
        ready = (self._changed(u) and not repoprio.url_problem(self.edit.text()) and bool(self._answer)
                 and self._answer[0] == u)
        self.ok.setEnabled(ready)
        self.ok.setToolTip("" if ready or not self._changed(u) else _("Wait until the address has been checked."))

    def _show_check(self, text, color):
        self.check_lbl.setText(text)
        self.check_lbl.setStyleSheet(f"color: {color};" if color else "color: palette(placeholder-text);")
        self.check_lbl.setVisible(bool(text))

    def _show_answer(self):
        _u, state, text = self._answer
        self._show_check(("✓ " + _("It answers.")) if state == "ok" else ("✗ " + text), GREEN if state == "ok" else RED)

    def _check(self):
        u = self.url()
        if not self._changed(u) or repoprio.url_problem(self.edit.text()):
            return
        self.timer.stop()
        self._show_check(_("Checking…"), None)
        run_async(repocheck.check_repo, {"url": u, "type": self.repo.get("type") or "rpm-md"},
                  on_done=lambda res, err, u=u: self._checked(u, res, err))

    def _checked(self, u, res, err):
        ok = bool(res and res["state"] == "ok")
        text = res["text"] if res else str(err)
        if res and res["state"] == "skip":
            text = _("it cannot be checked from here ({why})").format(why=res["text"])
        self._answer = (u, "ok" if ok else "fail", text)
        if u != self.url():
            return
        self._show_answer()
        self._update_ok()

    def _accept(self):
        u = self.url()
        if not self.ok.isEnabled() or not self._answer or self._answer[0] != u:
            return
        if self._answer[1] != "ok" and not confirm(
                self, _("The new address does not answer"), _("Add it to the queue anyway"),
                window_title=_("Change the address…"), icon_name="network-disconnect|dialog-warning",
                items=[(REPO_ICON, pgettext("change", "Change the address of the repository “{name}”").format(
                    name=self.repo["name"]), _("New address: {url}").format(url=u))],
                warning=_("The new address does not answer ({why}).").format(why=self._answer[2]) + " " + _(
                    "Until it answers, zypper cannot read this repository, and refreshing it fails."),
                means=_("it only goes to the queue: nothing changes until you apply it."),
                password=False, help_topic="repos"):
            return
        try:
            self.repo_file = repoprio.new_address_file(self.repo, u)
        except (OSError, ValueError) as e:
            error_box(self, _("Change the address…"), str(e))
            return
        self.accept()

    def _found(self, cands, err):
        if err:
            self.found_lbl.setText(_("Could not look: {error}").format(error=err))
            return
        if not cands:
            self.found_lbl.setText(_("Nothing else found here. Write the address yourself, or use “Search online…”."))
            return
        self.found_lbl.setText(_("What this project publishes now:"))
        for c in cands:
            row = QFrame()
            row.setObjectName("card")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            rb = QRadioButton(row)
            rb.setProperty("url", c["url"])
            self.group.addButton(rb)
            rb.toggled.connect(lambda on, u=c["url"]: on and self.edit.setText(u))
            h.addWidget(rb, 0, Qt.AlignmentFlag.AlignVCenter)
            tv = QVBoxLayout()
            tv.setSpacing(2)
            path = QLabel(c["folder"] or c["url"], row)  # from the project's web page: plain text
            path.setFont(mono())
            path.setTextFormat(Qt.TextFormat.PlainText)
            tv.addWidget(path)
            why = muted(c["why"], row)
            why.setTextFormat(Qt.TextFormat.PlainText)
            tv.addWidget(why)
            h.addLayout(tv, 1)
            st = QLabel("…", row)
            st.setTextFormat(Qt.TextFormat.PlainText)
            h.addWidget(st)
            if c["mine"]:
                h.addWidget(pill(_("recommended"), GREEN))
            self.cands.insertWidget(self.cands.count() - 1, row)
            if c["mine"]:
                run_async(repocheck.check_repo, {"url": c["url"], "type": "rpm-md"},
                          on_done=lambda res, err, st=st: self._cand_checked(st, res))
            else:
                st.setText("")
        self.spacer.hide()
        self.cand_area.show()
        # as tall as the rows (up to five), so they are never squeezed; more rows scroll
        inner = self.cand_area.widget()
        inner.adjustSize()
        rows = [self.cands.itemAt(i).widget() for i in range(self.cands.count() - 1)]
        want = sum(r.sizeHint().height() for r in rows[:5]) + self.cands.spacing() * (min(len(rows), 5) - 1) + 4
        self.cand_area.setMinimumHeight(min(want, 320))
        QTimer.singleShot(0, self._grow)
        first = next((b for b in self.group.buttons()), None)
        if first is not None and cands[0]["mine"]:
            first.setChecked(True)

    def _grow(self):
        lay = self.layout()
        lay.activate()
        need = lay.totalMinimumSize().height()
        if need > self.height():
            self.resize(self.width(), need)

    def _cand_checked(self, lbl, res):
        ok = bool(res and res["state"] == "ok")
        lbl.setText(("✓ " + _("answers")) if ok else ("✗ " + (res["text"] if res else "")))
        lbl.setStyleSheet(f"color: {GREEN if ok else RED};")

    def _make_action(self, url, repo_file):
        r = self.repo
        return Action("repo-url", r["alias"], url=url, repo_file=repo_file, name=r["name"], priority=r["priority"],
                      autorefresh=r["autorefresh"], enabled=r["enabled"], keeppackages=r.get("keeppackages", False))

    def action(self, pending_opts=None):
        """The change for the queue. The .repo file holds the repository's own settings: a change that already
        waits in the queue for it (pending_opts, a repo-modify) stays in the queue and runs after it, so it is not
        put in the file."""
        if not self.repo_file:
            self.repo_file = repoprio.new_address_file(self.repo, self.url())
        return self._make_action(self.url(), self.repo_file)


# ------------------------------------------------------------------ a repository left for Fedora

def yum_who(entry):
    """What to call a repository for Fedora: the program that left it (ChatGPT), or its own name."""
    return entry.get("program") or entry.get("name") or entry.get("section") or "?"


def yum_action(entry, used):
    """The repo-add for the queue: an alias from the section, free among used; the name the file gives it; the
    address with $basearch replaced; refreshed automatically; the signature checks the file asks for; and the
    priority of its kind (100 for a company's program)."""
    alias = yumrepos.alias_for(entry["section"], used)
    prio = repoprio.proposal({"alias": alias, "url": entry["url"], "name": entry["name"], "type": "rpm-md"})
    return Action("repo-add", alias, url=entry["url"], name=entry["name"] or alias, priority=prio, autorefresh=True,
                  gpg=entry["gpg"])


def commands_of(actions):
    """The exact commands of these actions, in the order the queue runs them."""
    q = TxQueue({})
    q.actions = list(actions)
    return [st.display() for st in q.steps()]


class YumBanner(QFrame):
    """A repository for Fedora that zypper does not see: who left it, where it is, why it matters, the command that
    adds it (small), and “Do not add it” / “Add it to zypper”. “Do not add it” only hides the banner: Repositories →
    More → “Show the hidden repositories for Fedora” brings it back. Every text is plain: names come from files."""

    add = Signal(object)
    dismiss = Signal(object)

    def __init__(self, entry, command, busy=False, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("yumbanner")
        self.setStyleSheet(f"QFrame#yumbanner {{ background: palette(base); border: 1px solid {BLUE}; "
                           "border-radius: 10px; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 12, 10)
        h.setSpacing(14)
        pic = QLabel(self)
        pic.setPixmap(icon("folder-cloud", "repository", "folder-download").pixmap(36, 36))
        pic.setFixedSize(36, 36)
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignVCenter)
        v = QVBoxLayout()
        v.setSpacing(2)
        who = yum_who(entry)
        if entry.get("package"):
            title = _("The program “{program}” left a repository for Fedora").format(program=who)
            text = _("It is in {path}, where zypper does not look: unless it is added to zypper, {program} will never "
                     "update.").format(path=entry["file"], program=who)
        else:
            title = _("There is a repository for Fedora that zypper does not see: “{name}”").format(name=who)
            text = _("It is in {path}, where zypper does not look: unless it is added to zypper, what it brings will "
                     "never update.").format(path=entry["file"])
        t = plain_label(title, bold=True)
        t.setToolTip("\n".join(x for x in (entry.get("name"), entry.get("url"),
                                           _("Package: {name}").format(name=entry["package"])
                                           if entry.get("package") else "") if x))
        v.addWidget(t)
        v.addWidget(plain_label(text, muted=True))
        from .localrpm import _CommandView  # the exact command, wrapped anywhere (addresses have no spaces)
        self.cmd = _CommandView(command, self)
        v.addWidget(self.cmd)
        h.addLayout(v, 1)
        self.b_later = button(_("Do not add it"), None, lambda: self.dismiss.emit(self.entry))
        self.b_later.setToolTip(_("Hide this message. Nothing is changed. Repositories → More → “Show the hidden "
                                  "repositories for Fedora” shows it again."))
        self.b_add = button(_("Add it to zypper"), None, lambda: self.add.emit(self.entry), primary=True)
        for b in (self.b_later, self.b_add):
            b.setAutoDefault(False)
            h.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_busy(busy)

    def set_busy(self, busy):
        self.b_add.setEnabled(not busy)
        self.b_later.setEnabled(not busy)
        self.b_add.setText(_("Checking its key…") if busy else _("Add it to zypper"))


def yum_question(entry, prep, action, key_path):
    """The question before a repository for Fedora goes to the queue together with its signing keys: keyword
    arguments for question.confirm (title and yes included). None when there is nothing to ask: no key to trust
    and nothing to warn about (then the banner said it all).

    prep: yumrepos.prepare_keys(); key_path(fpr): where the key will be kept for the helper (for the command)."""
    from .. import keys as keyring
    who = yum_who(entry)
    warnings = []
    if entry.get("plain_http"):  # security review 0.5, #2: said first, it matters most
        if entry["gpg"] == "allow-unsigned-repo":
            warnings.append(_("Its address is not encrypted (http, and https does not answer there), and its list of "
                              "packages is not signed: anybody on the same network (a café's Wi-Fi) could keep its "
                              "updates from you, or give you older versions with their known holes. Each package is "
                              "still checked against its signature."))
        else:
            warnings.append(_("Its address is not encrypted (http, and https does not answer there). Its list of "
                              "packages is signed, so nobody can change what is in it, but anybody on the same network "
                              "could keep its updates from you."))
    if entry["gpg"] == "off":
        warnings.append(_("The file says its packages are not signed: zypper will not be able to check that they "
                          "come from {who}.").format(who=who))
    elif entry["gpg"] == "allow-unsigned-package":
        warnings.append(_("The file says its packages are not signed. zypper still checks the list of packages, which "
                          "is signed and holds a checksum of each package."))
    if entry["gpg"] != "off" and not entry["gpgkeys"]:
        warnings.append(_("The file names no signing key. The first time zypper reads the repository, it will ask "
                          "whether to trust the key it finds there."))
    for source, problem in prep["problems"]:
        warnings.append(_("Its signing key could not be used ({source}): {problem} The first time zypper reads the "
                          "repository, it will ask about the key.").format(source=source, problem=problem))
    if not prep["import"] and not warnings:
        return None
    items = []
    for k in prep["import"]:
        if k.get("owner"):
            where = _("It comes with the package {package}: {file}").format(package=k["owner"], file=k["path"])
        elif k.get("path"):
            where = _("From the file {file}").format(file=k["path"])
        else:
            where = _("Downloaded from {url}").format(url=k["source"])
        items.append(("security-high|object-locked|dialog-password",
                      pgettext("change", "Trust the signing key “{name}”").format(
                          name=k["name"] or k["email"] or k["fpr"][-8:]),
                      _("Fingerprint {fpr}").format(fpr=keyring.nice_fpr(k["fpr"])) + "\n" + where))
    detail = _("A new place to download {who} and its updates from: {url}").format(who=who, url=entry["url"])
    if entry.get("https_from"):
        detail += "\n" + _("The file says {url}; Zypp Desk uses the same address with https, which answers.").format(
            url=entry["https_from"])
    items.append((REPO_ICON, pgettext("change", "Add the repository “{name}”").format(
        name=entry["name"] or action.target), detail))
    if prep["import"]:
        note = _("A key lets rpm install as root anything signed with it.")
        if all(k.get("owner") and k["owner"] == entry.get("package") for k in prep["import"]):
            note += " " + _("This one came with {who}, which you already installed.").format(who=who)
        warnings.insert(0, note)
    imports = [Action("key-import", k["fpr"], path=key_path(k["fpr"]), for_repo=action.target,
                      name=k["name"] or k["email"] or k["fpr"][-8:]) for k in prep["import"]]
    n = len(imports) + 1
    return {
        "title": _("Add the repository of {who} to zypper?").format(who=who),
        "yes": _("Add to the queue"),
        "icon_name": "folder-cloud|repository|folder-download",
        "lead": ngettext("Zypp Desk will put this in the queue:", "Zypp Desk will put this in the queue, in this "
                         "order:", n),
        "items": items,
        "means": _("{who} will update with the rest of the system. Nothing changes now: it happens when you apply "
                   "the queue.").format(who=who),
        "warning": "\n\n".join(warnings),
        "commands_": commands_of(imports + [action]),
        "password": False,  # it only goes to the queue
        "help_topic": "repos",
    }


def personal_project_question(parent, project, url, install=None):
    """The question before the repository of a personal project of the openSUSE Build Service (home:…) goes to
    the queue (it replaces an old Yes/No box: visual review 0.5, #15). install: the package that goes with it."""
    items = [(REPO_ICON, pgettext("change", "Add the repository “{name}”").format(name=project),
              _("A new place to download programs and updates from: {url}").format(url=url))]
    if install:
        items.append(("application-x-rpm|package-x-generic", pgettext("change", "Install {name}").format(name=install),
                      _("From this repository.")))
    return confirm(
        parent, _("Add the personal project {project}?").format(project=project), _("Add it to the queue"),
        window_title=_("Personal project"), icon_name="im-user|user-identity|dialog-warning", items=items,
        warning=_("{project} belongs to one person and nobody else reviews it. It can bring anything, and it can "
                  "stop being updated at any time. Add it only if you trust this person, or if nothing better "
                  "exists.").format(project=project),
        means=_("it only goes to the queue: nothing changes until you apply it."),
        password=False, help_topic="repos")
