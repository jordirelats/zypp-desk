# SPDX-License-Identifier: GPL-3.0-or-later
"""Drivers: one card per part of the computer (graphics, Wi-Fi and Bluetooth, network, sound, webcams, printers) with
the driver it uses, whether it is free, and a button when something better or missing can be installed (NVIDIA's
driver, Broadcom's wl, firmware). At the end, "Not needed for this computer": what is installed for hardware this
computer does not have, by groups, to remove in one go.

Everything is read in a thread (drivers.collect); every button only puts changes in the queue, after a question that
says what it is, where it comes from, whether it is proprietary, what happens with Secure Boot, and the commands.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from .. import drivers
from ..i18n import _, ngettext, pgettext
from ..question import GREY, More, ask_change, box, label
from ..simulation import human_size
from ..style import BLUE, GREEN, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action, TxQueue, nice_fpr
from ..ui import Page, button, error_box, heading, icon, mono, muted

STATUS = {  # status → (pill text, colour)
    "ok": (lambda: _("All good"), GREEN),
    "better": (lambda: _("There is a better driver"), ORANGE),
    "optional": (lambda: _("Optional"), BLUE),
    "firmware": (lambda: _("Firmware missing"), ORANGE),
    "missing": (lambda: _("Can be better"), BLUE),
    "nodriver": (lambda: _("No driver"), RED),
    "info": (lambda: _("Good to know"), GREY),
}
SHOWN_ROWS = 5  # the biggest packages of a group; the rest under "▸ N more"


def _tint(color, alpha=0.14):
    c = color.lstrip("#")
    return f"rgba({int(c[0:2], 16)}, {int(c[2:4], 16)}, {int(c[4:6], 16)}, {alpha})"


def pill(text, color):
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setStyleSheet(f"color: {color}; background: {_tint(color)}; border-radius: 9px; padding: 1px 9px;")
    return lbl


def _frame(name, soft=False):
    f = QFrame()
    f.setObjectName(name)
    bg = "transparent" if soft else "palette(base)"
    f.setStyleSheet(f"QFrame#{name} {{ background: {bg}; border: 1px solid rgba(127, 140, 141, 0.35); "
                    f"border-radius: 12px; }}")
    return f


def _icon_label(names, size=40):
    pic = QLabel()
    pic.setPixmap(icon(*names.split("|"), "cpu", "computer").pixmap(size, size))
    pic.setFixedSize(size, size)
    return pic


class DeviceCard(QFrame):
    """A part of the computer: icon, what it is, the driver, a pill, and a button when there is a proposal."""

    def __init__(self, card, on_click, parent=None):
        super().__init__(parent)
        self.card = card
        self.setObjectName("drvcard")
        self.setStyleSheet("QFrame#drvcard { background: palette(base); border: 1px solid rgba(127, 140, 141, "
                           "0.35); border-radius: 12px; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 11, 14, 11)
        h.setSpacing(14)
        h.addWidget(_icon_label(card["icon"]), 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        v.addWidget(label(card["title"], bold=True))
        self.detail = label(card["detail"], small=True, muted=True)
        v.addWidget(self.detail)
        h.addLayout(v, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        text, color = STATUS.get(card["status"], STATUS["info"])
        self.pill = pill(text(), color)
        right.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignRight)
        self.button = None
        if card.get("proposal"):
            self.button = QPushButton(self._button_text())
            self.button.setAutoDefault(False)
            self.button.clicked.connect(lambda: on_click(self.card))
            right.addWidget(self.button, 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch(1)
        h.addLayout(right)

    def _button_text(self):
        p = self.card["proposal"]
        if p["kind"] == "nvidia":
            return _("Install NVIDIA's driver…")
        if p["kind"] == "broadcom":
            return _("Install Broadcom's driver…")
        names = p.get("packages") or []
        if len(names) == 1:
            return _("Install {name}…").format(name=names[0])
        return _("Install them…")

    def set_queued(self, queued):
        if self.button is None:
            return
        if queued:
            self.pill.setText(_("In the queue"))
            self.pill.setStyleSheet(f"color: {GREEN}; background: {_tint(GREEN)}; border-radius: 9px; "
                                    "padding: 1px 9px;")
            self.button.setText(_("In the queue"))
            self.button.setEnabled(False)
        else:
            text, color = STATUS.get(self.card["status"], STATUS["info"])
            self.pill.setText(text())
            self.pill.setStyleSheet(f"color: {color}; background: {_tint(color)}; border-radius: 9px; "
                                    "padding: 1px 9px;")
            self.button.setText(self._button_text())
            self.button.setEnabled(True)


class GroupCard(QFrame):
    """A group of packages that are not needed: a tick, the title, a line, the size, and (for groups of several)
    the biggest packages with why, and "▸ N more"."""

    def __init__(self, group, what_for, on_change, parent=None):
        super().__init__(parent)
        self.group = group
        self.setObjectName("drvgroup")
        self.setStyleSheet("QFrame#drvgroup { background: palette(base); border: 1px solid rgba(127, 140, 141, "
                           "0.35); border-radius: 12px; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 11, 14, 11)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.check = QCheckBox()
        self.check.setChecked(group["ticked"])
        self.check.setAccessibleName(group["title"])
        self.check.toggled.connect(lambda _on: on_change())
        top.addWidget(self.check, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        self.title = label(group["title"], bold=True)
        tv.addWidget(self.title)
        tv.addWidget(label(group["detail"], small=True, muted=True))
        top.addLayout(tv, 1)
        top.addWidget(label(human_size(group["size"]), bold=True, wrap=False), 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)
        items = group["items"]
        if len(items) < 2:
            return
        biggest = sorted(items, key=lambda i: -i["size"])
        shown = biggest[:SHOWN_ROWS]
        extra = [i for i in items if i["package"] == drivers.FIRMWARE_ALL and i not in shown]
        shown = shown + extra
        rest = [i for i in biggest if i not in shown]
        v.addWidget(self._rows(shown, what_for))
        if rest:
            names = [i["package"].replace("kernel-firmware-", "") for i in rest]
            more_rows = self._rows(rest, what_for)
            more_rows.setVisible(False)
            text = ngettext("▸ {n} more: {names}", "▸ {n} more: {names}", len(rest)).format(
                n=len(rest), names=", ".join(names[:8]) + ("…" if len(names) > 8 else ""))
            v.addWidget(More(text, _("▾ Hide them"), more_rows))
            v.addWidget(more_rows)

    @staticmethod
    def _rows(items, what_for):
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(30, 0, 0, 0)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(3)
        for r, it in enumerate(items):
            name = label(it["package"], small=True, wrap=False, selectable=True, font=mono())
            g.addWidget(name, r, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(label(what_for(it), small=True, muted=True), r, 1)
            g.addWidget(label(human_size(it["size"]), small=True, muted=True, wrap=False), r, 2,
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        g.setColumnStretch(1, 1)
        return w

    def packages(self):
        return [i["package"] for i in self.group["items"]] if self.check.isChecked() else []


class DriversPage(Page):
    key = "drivers"
    title = _("Drivers")
    icon_name = "cpu"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.hw = None
        self._seen = None
        self._round = 0
        self.device_cards = []
        self.group_cards = []
        self.items = []
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 10, 16, 16)
        v.setSpacing(10)
        v.addWidget(muted(_("The hardware of this computer and the driver each part uses. When a proprietary one works "
                            "better (above all NVIDIA's, and Broadcom's for some Wi-Fi cards), Zypp Desk tells you "
                            "what it is, where it comes from and what happens with Secure Boot, and puts it in the "
                            "queue.")))
        self.status = muted(_("Looking at the hardware…"))
        self.status.setTextFormat(Qt.TextFormat.PlainText)  # an error can hold anything
        v.addWidget(self.status)
        self.devices_box = QVBoxLayout()
        self.devices_box.setSpacing(10)
        v.addLayout(self.devices_box)
        self.notice = label("", small=True)
        self.notice.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
        self.notice.hide()
        v.addWidget(self.notice)

        # ---- not needed for this computer
        self.section = QWidget()
        sv = QVBoxLayout(self.section)
        sv.setContentsMargins(0, 12, 0, 0)
        sv.setSpacing(10)
        sv.addWidget(heading(_("Not needed for this computer"), 1.3))
        sv.addWidget(muted(_("Software for hardware this computer does not have. Zypp Desk only lists what it is sure "
                             "nothing here uses.")))
        self.groups_box = QVBoxLayout()
        self.groups_box.setSpacing(10)
        sv.addLayout(self.groups_box)
        self.stays = _frame("drvstays", soft=True)
        st = QVBoxLayout(self.stays)
        st.setContentsMargins(14, 10, 14, 10)
        st.setSpacing(2)
        st.addWidget(label(_("Stays"), bold=True))
        self.stays_text = label("", small=True, muted=True)
        st.addWidget(self.stays_text)
        sv.addWidget(self.stays)
        foot = QHBoxLayout()
        self.total = label("", small=True, muted=True)
        foot.addWidget(self.total, 1)
        self.b_remove = button(_("Remove the packages…"), primary=True, slot=lambda: self.remove_ticked())
        self.b_remove.setAutoDefault(False)
        foot.addWidget(self.b_remove, 0, Qt.AlignmentFlag.AlignVCenter)
        sv.addLayout(foot)
        self.section.hide()
        v.addWidget(self.section)
        v.addStretch(1)

        self.b_again = button(_("Look again"), "view-refresh", lambda: self.reload())
        ctx.queue.changed.connect(self._queue_changed)

    def header_actions(self):
        return [self.b_again]

    def activated(self):
        # a device plugged in or out since the last look (a dock, a USB adapter): look again, so that what it needs
        # shows here (sysfs only: a few milliseconds)
        if self.loaded and self.hw is not None and drivers.device_aliases() != self._seen:
            self.loaded = False
        super().activated()

    # ------------------------------------------------------------ loading
    def reload(self):
        self._round += 1
        rnd = self._round
        self.status.setText(_("Looking at the hardware…"))
        self.status.show()
        self.b_again.setEnabled(False)
        settings = getattr(self.ctx, "settings", None)
        run_async(self._read, settings, on_done=lambda res, err: self._loaded(rnd, res, err))

    @staticmethod
    def _read(settings):
        hw = drivers.collect(settings)
        return hw, drivers.cards(hw), drivers.unneeded(hw)

    def _loaded(self, rnd, res, err):
        if rnd != self._round:
            return  # an older look answered late
        self.b_again.setEnabled(True)
        if err:
            self.status.setText(_("The hardware could not be read: {error}").format(error=err))
            return
        self.show_result(*res)

    def show_result(self, hw, cards, items):
        """Fill the page (also from tests, with hardware of their own)."""
        self.hw, self.items = hw, items
        self._seen = hw.get("aliases")
        self.status.hide()
        for w in self.device_cards + self.group_cards:
            w.setParent(None)
            w.deleteLater()
        self.device_cards, self.group_cards = [], []
        for c in cards:
            card = DeviceCard(c, self.propose)
            self.devices_box.addWidget(card)
            self.device_cards.append(card)
        if not cards:
            self.status.setText(_("Zypp Desk found no graphics, network, sound or printer to show."))
            self.status.show()
        what = self._firmware_words()
        for g in drivers.groups(items, hw):
            gc = GroupCard(g, lambda it, w=what: self._why(it, w), self._update_total)
            self.groups_box.addWidget(gc)
            self.group_cards.append(gc)
        lines = drivers.stays(hw)
        self.stays_text.setText(" ".join(lines))
        self.stays.setVisible(bool(lines))
        self.section.setVisible(bool(items))
        self._queue_changed()

    def _firmware_words(self):
        try:
            from .. import health
            return health.firmware_for()
        except Exception:
            return {}

    def _why(self, it, what):
        """A row's reason: short for firmware ('Mellanox server network cards: none here')."""
        if it["package"] == drivers.FIRMWARE_ALL:
            plan = (self.hw or {}).get("fw_plan") or {}
            if plan.get("lock"):
                return _("The package that brings all the others; it stays locked.")
            return it["reason"]
        if it["group"] == "firmware" and it["package"] in what:
            return _("{what}: none here").format(what=what[it["package"]])
        return it["reason"]

    # ------------------------------------------------------------ the queue
    def _queued_removals(self):
        return {a.target for a in self.ctx.queue.of("remove")}

    def _queue_changed(self):
        q = self.ctx.queue
        for card in self.device_cards:
            p = card.card.get("proposal")
            if not p:
                continue
            installs = [a for a in p["actions"] if a[0] == "install"]
            card.set_queued(bool(installs) and all(q.state_of(o.get("package") or t) == "install"
                                                   for _k, t, o in installs))
        gone = self._queued_removals()
        for gc in self.group_cards:
            names = [i["package"] for i in gc.group["items"]]
            all_queued = all(n in gone for n in names)
            gc.check.setEnabled(not all_queued)
            gc.title.setText(gc.group["title"] + ("  —  " + _("in the queue") if all_queued else ""))
        self._update_total()

    def ticked(self):
        gone = self._queued_removals()
        return [n for gc in self.group_cards if gc.check.isEnabled() for n in gc.packages() if n not in gone]

    def _update_total(self):
        names = self.ticked()
        size = sum(i["size"] for i in self.items if i["package"] in names)
        n = len(names)
        if not n:
            self.total.setText(_("Nothing is ticked."))
            self.b_remove.setText(_("Remove the packages…"))
            self.b_remove.setEnabled(False)
            return
        self.total.setText(ngettext("Ticked: {n} package, {size}. If you plug in a device that needs it, you will see "
                                    "it on this page.",
                                    "Ticked: {n} packages, {size}. If you plug in a device that needs one of them, "
                                    "you will see it on this page.", n).format(n=n, size=human_size(size)))
        self.b_remove.setText(ngettext("Remove the {n} package…", "Remove the {n} packages…", n).format(n=n)
                              if n else _("Remove the packages…"))
        self.b_remove.setEnabled(n > 0)

    def _commands(self, actions):
        """The exact commands these changes will run, as the queue makes them."""
        q = TxQueue(getattr(self.ctx.queue, "settings", None) or {})
        for a in actions:
            q.add(a)
        return [s.display() for s in q.steps()]

    def _queue(self, actions, notice):
        with self.ctx.queue.batch():
            for a in actions:
                self.ctx.queue.add(a)
        self.notice.setText(notice)
        self.notice.show()

    # ------------------------------------------------------------ removing what is not needed
    def remove_ticked(self):
        names = self.ticked()
        if not names or self.hw is None:
            return
        changes = drivers.removal_changes(names, self.hw)
        actions = [Action(k, t, **o) for k, t, o in changes]
        removes = [a for a in actions if a.kind == "remove"]
        locks = [a for a in actions if a.kind == "lock"]
        by_name = {i["package"]: i for i in self.items}
        size = sum(by_name[n]["size"] for n in names if n in by_name)
        fw = [n for n in names if by_name.get(n, {}).get("group") == "firmware" and n != drivers.FIRMWARE_ALL]
        others = [n for n in names if n not in fw and n != drivers.FIRMWARE_ALL]
        parts = []
        if fw:
            parts.append(ngettext("{n} of firmware", "{n} of firmware", len(fw)).format(n=len(fw)))
        parts += others
        n = len(removes)
        items = [("user-trash|edit-delete", ngettext("Remove {n} package this computer doesn't use",
                                                     "Remove {n} packages this computer doesn't use", n).format(n=n),
                  drivers.join(parts, 5) + ". " + _("Frees {size}.").format(size=human_size(size)),
                  [a.target for a in removes])]
        for lk in locks:
            items.append(("object-locked", pgettext("change", "Lock {name}").format(name=lk.target),
                          ngettext("Otherwise every new kernel would bring it back, and with it the {n} firmware "
                                   "package.",
                                   "Otherwise every new kernel would bring it back, and with it the {n} firmware "
                                   "packages.", len(fw)).format(n=len(fw))))
        means = _("the computer works the same. If one day you connect a device that needs one of these packages, "
                  "this page tells you, and you can put it back with one click.")
        if locks:
            means += " " + _("The lock can be removed in Locks.")
        means += " " + _("Everything goes to the queue: nothing changes until you apply it.")
        answer = ask_change(
            self, _("Remove what this computer doesn't need?"), window_title=_("Not needed for this computer"),
            icon_name="cpu|computer", lead=_("Zypp Desk will do this, in this order:"), items=items,
            means=means, commands_=self._commands(actions),
            help_topic="drivers", buttons=[("no", _("Not yet"), False), ("simulate", _("Look first (simulate)"), False),
                     ("yes", _("Yes, remove them"), True)])
        if answer not in ("yes", "simulate"):
            return
        self._queue(actions, ngettext("Added to the queue: remove {n} package.", "Added to the queue: remove {n} "
                                      "packages.", n).format(n=n))
        if answer == "simulate" and hasattr(self.ctx, "simulate_queue"):
            self.ctx.simulate_queue()

    # ------------------------------------------------------------ proposals
    def propose(self, card):
        p = card.get("proposal")
        if not p:
            return
        if p["kind"] == "nvidia":
            self._nvidia(card)
        elif p["kind"] == "broadcom":
            self._broadcom(card)
        else:
            self._install(card)

    @staticmethod
    def _secure_boot_box(signed_by_opensuse, sb):
        if signed_by_opensuse:
            if sb:
                return box("ok", _("Secure Boot is on, and openSUSE signs this kernel part: it starts as it is, with "
                                   "nothing to enroll."))
            if sb is False:
                return box("info", _("Secure Boot is off on this computer. If you turn it on one day, this driver "
                                     "still starts: openSUSE signs it."))
            return box("info", _("openSUSE signs this kernel part, so it also starts with Secure Boot on."))
        if sb:
            return box("warn", _("Secure Boot is on. This driver is signed with a key made on this computer, so at the "
                                 "next start a blue screen (MokManager) asks to enroll it: choose “Enroll MOK”, then "
                                 "“Continue” and “Yes”, and type the root password (the keyboard there is English, "
                                 "US). If you skip it, the computer starts with nouveau, and you can enroll the key "
                                 "at a later start."), title=_("Secure Boot: one step at the next start"))
        if sb is False:
            return box("info", _("Secure Boot is off on this computer, so nothing else is needed at start. If you turn "
                                 "it on one day, you will have to enroll the key this driver is signed with."))
        return box("info", _("If Secure Boot is on, at the next start a blue screen (MokManager) asks to enroll the "
                             "key this driver is signed with: choose “Enroll MOK”, “Continue”, “Yes”, and type the "
                             "root password."))

    def _nvidia(self, card):
        plan = card["proposal"]["plan"]
        key = plan.get("key")
        card_widget = next((c for c in self.device_cards if c.card is card), None)
        if key:
            if card_widget and card_widget.button:
                card_widget.button.setEnabled(False)
                card_widget.button.setText(_("Getting NVIDIA's key…"))
            run_async(self._fetch_key, key["url"], on_done=lambda res, err: self._nvidia_ask(card, res, err))
        else:
            self._nvidia_ask(card, None, None)

    @staticmethod
    def _fetch_key(url):
        from .. import keys, online
        k = online.fetch_key(url)  # read with a throwaway gpg home; gpg's own copy kept for the queue
        k["known"] = k["fingerprint"].upper() in {f.upper() for f in keys.installed_keys()}
        return k

    def _nvidia_ask(self, card, k, err):
        self._queue_changed()  # the button back as it was
        p = card["proposal"]
        plan = p["plan"]
        actions = []
        if plan.get("key"):
            if err:
                error_box(self, _("NVIDIA's driver"), _("NVIDIA's signing key could not be read: {error}").format(
                    error=err))
                return
            if (k["fingerprint"] or "").upper().replace(" ", "") != drivers.NVIDIA_KEY_FPR:
                error_box(self, _("NVIDIA's driver"), _(
                    "NVIDIA's repository publishes a signing key that is not the one Zypp Desk knows ({fpr}). Zypp "
                    "Desk does not add it: follow openSUSE's guide (SDB:NVIDIA drivers) instead.").format(
                    fpr=nice_fpr(k["fingerprint"])))
                return
        for kind, target, opts in plan["actions"]:
            if kind == "key-import":
                if k.get("known"):
                    continue
                opts = dict(opts, path=k["path"])
            actions.append(Action(kind, target, **opts))
        series, flavor = plan["series"], plan["flavor"]
        names = [n for n, _r in plan["packages"]]
        items = []
        if any(a.kind == "key-import" for a in actions):
            items.append(("security-high|object-locked", pgettext("change", "Trust NVIDIA's signing key"),
                          _("Fingerprint {fpr}. NVIDIA signs its repository for openSUSE with it.").format(
                              fpr=nice_fpr(drivers.NVIDIA_KEY_FPR))))
        repo = plan["repo"]
        if repo["state"] == "new":
            items.append(("folder-cloud|folder-network", pgettext("change", "Add the repository “{name}”").format(
                name=repo["name"]), _("{url}: NVIDIA's own repository for openSUSE, the one openSUSE's guide "
                                      "uses.").format(url=repo["url"])))
        elif repo["state"] == "disabled":
            items.append(("folder-cloud|folder-network", pgettext("change", "Enable the repository “{name}”").format(
                name=repo["name"]), _("NVIDIA's repository is already here, but disabled.")))
        if not plan["compiled"]:
            kmp = next(n for n, from_nvidia in plan["packages"] if not from_nvidia)
            detail = _("The kernel part ({kmp}) is open source, from openSUSE's main repository and signed by "
                       "openSUSE. The rest (graphics libraries, video decoding, CUDA, settings) is proprietary and "
                       "comes from NVIDIA.").format(kmp=kmp)
        else:
            detail = _("Everything comes from NVIDIA and is proprietary. The kernel part is built on this computer "
                       "when it is installed: that brings gcc, make and the kernel's development files (a few hundred "
                       "MB).")
        items.append(("video-display|package-install", _("Install NVIDIA's driver, series {series}").format(
            series=series), detail, names))
        means = _("after the next restart, the {card} uses NVIDIA's driver instead of nouveau: faster, with video "
                  "decoding in the card and CUDA. NVIDIA's part is proprietary: nobody outside NVIDIA can check or fix "
                  "its code, and NVIDIA's licence applies.").format(card=p.get("model") or p["device"])
        if plan.get("legacy"):
            means += " " + _("This old series only works in an X11 session, not in Wayland.")
        means += " " + _("It goes to the queue: nothing changes until you apply it.")
        warning = (_("NVIDIA no longer updates series {series}: it gets no security fixes, and a future kernel may "
                     "not work with it. nouveau, which you have now, keeps working.").format(series=series)
                   if plan.get("legacy") else None)
        answer = ask_change(
            self, _("Install NVIDIA's driver for the {card}?").format(card=p.get("model") or p["device"]),
            window_title=_("NVIDIA's driver"), icon_name="video-display", lead=_("Zypp Desk will put this in the "
                                                                                 "queue, in this order:"),
            items=items, extra=[self._secure_boot_box(not plan["compiled"], p.get("secure_boot"))], means=means,
            warning=warning, commands_=self._commands(actions),
            help_topic="drivers", buttons=[("no", _("Not yet"), False), ("yes", _("Add to the queue"), True)])
        if answer != "yes":
            return
        self._queue(actions, _("Added to the queue: NVIDIA's driver, series {series} ({flavor} kernel).").format(
            series=series, flavor=flavor))

    def _broadcom(self, card):
        p = card["proposal"]
        actions = [Action(k, t, **o) for k, t, o in p["actions"]]
        items = []
        if p.get("repo"):
            items.append(("folder-cloud|folder-network", pgettext("change", "Enable the repository “{name}”").format(
                name=p["repo"]["name"]), _("openSUSE's non-free repository: programs openSUSE may share but not as "
                                           "free software.")))
        items.append(("network-wireless", _("Install Broadcom's driver wl"),
                      _("From openSUSE's non-free repository (repo-non-oss): Broadcom's licence does not allow it "
                        "to be shipped as free software."), p["packages"]))
        means = _("after the next restart, the {card} ({chip}) works with Broadcom's driver wl. It switches off the "
                  "free drivers b43, bcma, brcmfmac, brcmsmac and ssb, which no other device of this computer uses. "
                  "It goes to the queue: nothing changes until you apply it.").format(card=p["device"], chip=p["chip"])
        answer = ask_change(
            self, _("Install Broadcom's Wi-Fi driver?"), window_title=_("Broadcom's driver"),
            icon_name="network-wireless", lead=_("Zypp Desk will put this in the queue, in this order:"), items=items,
            extra=[self._secure_boot_box(True, p.get("secure_boot"))], means=means,
            commands_=self._commands(actions),
            help_topic="drivers", buttons=[("no", _("Not yet"), False), ("yes", _("Add to the queue"), True)])
        if answer != "yes":
            return
        self._queue(actions, _("Added to the queue: Broadcom's driver wl."))

    def _install(self, card):
        p = card["proposal"]
        names = p["packages"]
        actions = [Action(k, t, **o) for k, t, o in p["actions"]]
        device = p.get("device") or ""
        if ": " in device:  # a card's title, "Wi-Fi: Intel …": the device is after the colon
            device = device.split(": ", 1)[1]
        items = []
        for n in names:
            fw = "firmware" in n or n.startswith("ucode-")
            if device:
                why = (_("Firmware for {device}: its driver loads it when it starts.") if fw
                       else _("For {device}.")).format(device=device)
            else:  # firmware the kernel asked for, without a device to name
                why = (_("Firmware a driver asked for: it loads it when it starts.") if fw
                       else _("For a device of this computer."))
            items.append(("package-install|system-software-install", pgettext("change", "Install {name}").format(
                name=n), why))
        answer = ask_change(
            self, _("Install {name}?").format(name=names[0]) if len(names) == 1 else ngettext(
                "Install these {n} packages?", "Install these {n} packages?", len(names)).format(n=len(names)),
            window_title=_("Drivers"), icon_name="cpu|computer",
            lead=ngettext("Zypp Desk will put this in the queue:", "Zypp Desk will put this in the queue, in this "
                          "order:", len(items)), items=items,
            means=_("after the next restart (or when you plug the device in again), its driver finds what it needs. "
                    "It comes from your repositories. It goes to the queue: nothing changes until you apply it."),
            commands_=self._commands(actions),
            help_topic="drivers", buttons=[("no", _("Not yet"), False), ("yes", _("Add to the queue"), True)])
        if answer != "yes":
            return
        self._queue(actions, ngettext("Added to the queue: install {names}.", "Added to the queue: install {names}.",
                                      len(names)).format(names=drivers.join(names)))
