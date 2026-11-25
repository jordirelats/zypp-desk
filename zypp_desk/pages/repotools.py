# SPDX-License-Identifier: GPL-3.0-or-later
"""Repositories: the priority stepper of the cards, "Suggest an order…" and "Change the address…"."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QRadioButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import repocheck, repoprio
from ..i18n import _, ngettext
from ..style import GREEN, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import button, error_box, heading, mono, muted, plain

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
        if self._answer[1] != "ok":
            box = QMessageBox(QMessageBox.Icon.Warning, _("Change the address…"),
                              _("The new address does not answer ({why}).").format(why=self._answer[2]),
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            box.setInformativeText(_("Until it answers, zypper cannot read this repository, and refreshing it fails. "
                                     "Add the new address to the queue anyway?"))
            box.setDefaultButton(QMessageBox.StandardButton.No)
            answer = plain(box).exec()
            box.deleteLater()
            if answer != QMessageBox.StandardButton.Yes:
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
