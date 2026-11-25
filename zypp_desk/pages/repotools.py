# SPDX-License-Identifier: GPL-3.0-or-later
"""Repositories: the priority stepper of the cards, "Suggest an order…" and "Change the address…"."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QRadioButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import repocheck, repoprio
from ..i18n import _, ngettext
from ..style import GREEN, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import button, heading, mono, muted

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
            r, p = ch[0]
            more = len(ch) - 1
            self.cmd.setText(f"zypper modifyrepo --priority {p} {r['alias']}" + (" … " + ngettext(
                "(and {n} more)", "(and {n} more)", more).format(n=more) if more else ""))
        else:
            self.cmd.setText(_("Nothing to change."))


def _how_it_works(parent):
    from ..ui import info_box
    info_box(parent, _("How the priority works"), HOW_IT_WORKS())


class ChangeUrlDialog(QDialog):
    """A repository at a new address: written by hand, or found again among what its OBS project publishes."""

    def __init__(self, repo, health, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle(_("Change the address of “{name}”").format(name=repo["name"]))
        self.resize(760, 600)
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
            state.setStyleSheet("background: #fdf1e6; color: #7a4300; border-radius: 8px; padding: 8px;")
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
        er.addWidget(self.b_check)
        lay.addLayout(er)
        self.check_lbl = QLabel(self)
        self.check_lbl.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.check_lbl)

        lay.addWidget(QLabel("<b>" + _("Find it again") + "</b>", self))
        self.found_lbl = muted(_("Looking at what this repository's project publishes…"), self)
        lay.addWidget(self.found_lbl)
        self.cands = QVBoxLayout()
        self.group = QButtonGroup(self)
        lay.addLayout(self.cands)
        lay.addWidget(muted(_("The name, the priority, the refresh and the signature checks stay. If the project "
                              "changed its name, “Search online…” finds it by the packages you have from it."), self))
        lay.addStretch(1)
        self.cmd = QLabel(self)
        self.cmd.setFont(mono())
        self.cmd.setWordWrap(True)
        self.cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.cmd.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(self.cmd)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(button(_("Cancel"), None, self.reject))
        self.ok = button(_("Add to the queue"), "list-add", self.accept, primary=True)
        btns.addWidget(self.ok)
        lay.addLayout(btns)
        self._edited()
        if repo.get("service"):
            self.edit.setEnabled(False)
            self.found_lbl.setText(_("This repository belongs to the service {service}, which sets its address: "
                                     "change the service instead.").format(service=repo["service"]))
            self.ok.setEnabled(False)
        else:
            run_async(repoprio.candidates, repo["url"], on_done=self._found)

    def url(self):
        return self.edit.text().strip()

    def _edited(self, *_a):
        u = self.url()
        changed = bool(u) and u != self.repo["url"] and ("://" in u or u.startswith("dir:"))
        self.ok.setEnabled(changed and not self.repo.get("service"))
        a = self.repo["alias"]
        self.cmd.setText(f"zypper removerepo {a} && zypper addrepo … {u} {a}" if changed else "")
        self.check_lbl.setText("")
        for b in self.group.buttons():
            b.blockSignals(True)
            b.setChecked(b.property("url") == u)
            b.blockSignals(False)

    def _check(self):
        u = self.url()
        self.check_lbl.setText(_("Checking…"))
        self.check_lbl.setStyleSheet("")
        run_async(repocheck.check_repo, {"url": u, "type": self.repo.get("type") or "rpm-md"},
                  on_done=lambda res, err, u=u: self._checked(u, res, err))

    def _checked(self, u, res, err):
        if u != self.url():
            return
        ok = bool(res and res["state"] == "ok")
        self.check_lbl.setText(("✓ " + _("It answers.")) if ok else ("✗ " + (res["text"] if res else str(err))))
        self.check_lbl.setStyleSheet(f"color: {GREEN if ok else RED};")

    def _found(self, cands, err):
        if err:
            self.found_lbl.setText(_("Could not look: {error}").format(error=err))
            return
        if not cands:
            self.found_lbl.setText(_("Nothing else found here. Write the address yourself, or use “Search online…”."))
            return
        self.found_lbl.setText(_("What this project publishes now:"))
        for c in cands:
            row = QFrame(self)
            row.setObjectName("card")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            rb = QRadioButton(row)
            rb.setProperty("url", c["url"])
            self.group.addButton(rb)
            rb.toggled.connect(lambda on, u=c["url"]: on and self.edit.setText(u))
            h.addWidget(rb)
            tv = QVBoxLayout()
            tv.setSpacing(0)
            path = QLabel(c["folder"] or c["url"], row)
            path.setFont(mono())
            path.setTextFormat(Qt.TextFormat.PlainText)
            tv.addWidget(path)
            tv.addWidget(muted(c["why"], row))
            h.addLayout(tv, 1)
            st = QLabel("…", row)
            h.addWidget(st)
            if c["mine"]:
                h.addWidget(pill(_("recommended"), GREEN))
            self.cands.addWidget(row)
            if c["mine"]:
                run_async(repocheck.check_repo, {"url": c["url"], "type": "rpm-md"},
                          on_done=lambda res, err, st=st: self._cand_checked(st, res))
            else:
                st.setText("")
        first = next((b for b in self.group.buttons()), None)
        if first is not None and cands[0]["mine"]:
            first.setChecked(True)

    def _cand_checked(self, lbl, res):
        ok = bool(res and res["state"] == "ok")
        lbl.setText(("✓ " + _("answers")) if ok else ("✗ " + (res["text"] if res else "")))
        lbl.setStyleSheet(f"color: {GREEN if ok else RED};")

    def action(self, pending_opts=None):
        r = self.repo
        gpg = "off" if not r.get("gpgcheck") else ("allow-unsigned-repo" if r.get("pkg_gpgcheck")
                                                   and not r.get("repo_gpgcheck") else "default")
        opts = dict(url=self.url(), name=r["name"], priority=r["priority"], autorefresh=r["autorefresh"],
                    enabled=r["enabled"], keeppackages=r.get("keeppackages", False), gpg=gpg)
        opts.update(pending_opts or {})  # what was already waiting in the queue for this repository
        opts["url"] = self.url()
        return Action("repo-url", r["alias"], **opts)
