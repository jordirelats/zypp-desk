# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Notifications: the tray icon, how often it looks, firmware, downloading ahead, and the system
timer that refreshes the repositories (zypp-desk-refresh.timer)."""

import datetime
import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QLocale, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QAbstractButton, QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import tray
from ..i18n import _, ngettext
from ..runner import Step
from ..style import GREEN, ORANGE, dot
from ..tasks import run_async
from ..ui import Card, ask, error_box, mono, muted

AHEAD_FLAG = Path("/etc/zypp-desk/download-ahead")  # read by zypp-desk-refresh, written by the helper
TIMER = "zypp-desk-refresh.timer"
TIMER_COMMANDS = ("zypper --non-interactive --quiet refresh",
                  "zypper --non-interactive --quiet dist-upgrade --download-only --auto-agree-with-licenses",
                  "fwupdmgr refresh --assume-yes")


class Switch(QAbstractButton):
    """An on/off switch like Plasma's (a pill with a knob): green when on."""

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(QSize(38, 22))

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 2, 36, 20)
        pal = self.palette()
        if self.isChecked():
            track = QColor(GREEN)
        else:
            track = pal.color(QPalette.ColorRole.PlaceholderText)
            track.setAlpha(110)
        if not self.isEnabled():
            track.setAlpha(60)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, 10, 10)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(pal.color(QPalette.ColorRole.Highlight))
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 11, 11)
        x = r.right() - 18 if self.isChecked() else r.left() + 2
        knob = QRectF(x, r.top() + 2, 16, 16)
        p.setPen(QColor(0, 0, 0, 40))
        p.setBrush(QColor("white"))
        p.drawEllipse(knob)
        p.end()


class _ClickLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


def timer_state():
    """The system timer, read as the user (read-only): dict(installed, enabled, active, last), or None when there
    is no systemctl."""
    if not shutil.which("systemctl"):
        return None
    p = subprocess.run(["systemctl", "show", "--timestamp=unix",
                        "--property=LoadState,UnitFileState,ActiveState,LastTriggerUSec", "--", TIMER],
                       capture_output=True, text=True, timeout=15, env=dict(os.environ, LC_ALL="C"),
                       stdin=subprocess.DEVNULL)
    d = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    last = d.get("LastTriggerUSec", "")
    return dict(installed=d.get("LoadState") == "loaded",
                enabled=d.get("UnitFileState") in ("enabled", "enabled-runtime", "linked", "linked-runtime"),
                active=d.get("ActiveState") == "active",
                last=int(last[1:]) if last.startswith("@") and last[1:].isdigit() and int(last[1:]) > 0 else None)


def _when(ts):
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    t = QLocale().toString(d.time(), QLocale.FormatType.ShortFormat)
    if d.date() == today:
        return _("today at {time}").format(time=t)
    if d.date() == today - datetime.timedelta(days=1):
        return _("yesterday at {time}").format(time=t)
    return _("on {date} at {time}").format(date=QLocale().toString(d.date(), QLocale.FormatType.ShortFormat), time=t)


def ensure_tray(settings):
    """Call when the window starts. The first time (tray_autostart on, no autostart file yet), put the tray in
    the autostart folder; then start the tray if it should run at login and none is running."""
    if not settings.get("tray_autostart", True):
        return
    if not settings.get("tray_autostart_done"):
        if not tray.autostart_path().exists():
            try:
                tray.write_autostart()
            except OSError:
                return
        settings["tray_autostart_done"] = True
        settings.save()
    # a file the user took away (Plasma's Autostart settings) is respected: no tray then
    if tray.autostart_enabled() and not tray.tray_running():
        tray.start_tray()


class NotifyCard(Card):
    """The settings of the tray icon and its notifications (mockup funcions-v1-01-safata, ③)."""

    def __init__(self, ctx, parent=None):
        super().__init__(_("Notifications"), parent)
        self.ctx = ctx
        s = ctx.settings
        self.lay.setSpacing(6)
        self.setMaximumWidth(600)  # short lines, as in the mockup: the choice next to its words

        self.sw_notify, row = self._switch_row(_("Tell me when there are updates"), s.get("notify_enabled", True))
        self.lay.addWidget(row)

        every = QWidget(self)
        h = QHBoxLayout(every)
        h.setContentsMargins(self.sw_notify.sizeHint().width() + 10, 0, 0, 0)
        h.addWidget(QLabel(_("Check every"), every))
        h.addStretch(1)
        self.interval = QComboBox(every)
        try:
            cur = int(s.get("notify_interval_hours", 6))
        except (TypeError, ValueError):
            cur = 6
        for hours in tray.INTERVALS:
            self.interval.addItem(ngettext("{n} hour", "{n} hours", hours).format(n=hours), hours)
        nearest = min(tray.INTERVALS, key=lambda x: abs(x - cur))
        self.interval.setCurrentIndex(tray.INTERVALS.index(nearest))
        h.addWidget(self.interval)
        self.lay.addWidget(every)

        self.sw_tray, row = self._switch_row(_("Put the icon in the tray when the computer starts"),
                                             tray.autostart_enabled())
        self.lay.addWidget(row)
        self.sw_fw, row = self._switch_row(_("Tell me about firmware too"), s.get("notify_firmware", True))
        self.lay.addWidget(row)
        self.sw_ahead, row = self._switch_row(_("Download them ahead, without installing"), AHEAD_FLAG.exists(),
                                              _("“Run the routine” will go faster."))
        self.lay.addWidget(row)

        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(127, 140, 141, 0.35);")
        self.lay.addSpacing(4)
        self.lay.addWidget(line)
        self.lay.addWidget(muted(_("To know what is new, the repositories must be refreshed. A system timer does "
                                   "it as root, without asking for a password. It only refreshes: it never "
                                   "installs anything by itself."), self))
        state_row = QHBoxLayout()
        state_row.setSpacing(6)
        self.timer_dot = QLabel(self)
        self.timer_dot.hide()
        state_row.addWidget(self.timer_dot, 0, Qt.AlignmentFlag.AlignTop)
        self.timer_lbl = muted("", self)
        state_row.addWidget(self.timer_lbl, 1)
        self.lay.addLayout(state_row)
        self.enable_cmd = self._mono("sudo systemctl enable --now " + TIMER)
        self.enable_cmd.hide()
        self.lay.addWidget(self.enable_cmd)
        self.lay.addSpacing(2)
        self.lay.addWidget(muted(_("What the timer runs:"), self))
        self.cmd = self._mono("")
        self.lay.addWidget(self.cmd)
        self._show_commands()

        self.sw_notify.toggled.connect(lambda on: self._set("notify_enabled", on))
        self.sw_fw.toggled.connect(lambda on: self._set("notify_firmware", on))
        self.interval.currentIndexChanged.connect(
            lambda i: self._set("notify_interval_hours", self.interval.itemData(i)))
        self.sw_tray.toggled.connect(self._tray_toggled)
        self.sw_ahead.toggled.connect(self._ahead_toggled)
        self.reload()

    def _mono(self, text):
        lbl = QLabel(text, self)
        lbl.setFont(mono())
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet("color: palette(placeholder-text);")
        return lbl

    def _switch_row(self, text, checked, sub=None):
        w = QWidget(self)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 3, 0, 3)
        h.setSpacing(10)
        sw = Switch(bool(checked), w)
        sw.setAccessibleName(text)
        h.addWidget(sw, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        lbl = _ClickLabel(text, w)
        lbl.setWordWrap(True)
        lbl.clicked.connect(sw.click)
        col.addWidget(lbl)
        if sub:
            col.addWidget(muted(sub, w))
        h.addLayout(col, 1)
        return sw, w

    @staticmethod
    def _quietly(sw, on):
        sw.blockSignals(True)
        sw.setChecked(on)
        sw.blockSignals(False)
        sw.update()

    def reload(self):
        """Read again what lives outside the settings: the autostart file, the flag, the timer."""
        self._quietly(self.sw_tray, tray.autostart_enabled())
        self._quietly(self.sw_ahead, AHEAD_FLAG.exists())
        self._show_commands()
        run_async(timer_state, on_done=self._timer_read)

    # ------------------------------------------------ the plain settings
    def _set(self, key, value):
        self.ctx.settings[key] = value
        self.ctx.settings.save()
        tray.poke("reload")  # the tray reads them again now (it would anyway before its next check)

    def _tray_toggled(self, on):
        self.ctx.settings["tray_autostart"] = on
        self.ctx.settings["tray_autostart_done"] = True  # a choice made here is never undone by ensure_tray
        self.ctx.settings.save()
        try:
            if on:
                tray.write_autostart()
            else:
                tray.remove_autostart()
        except OSError as e:
            error_box(self, _("Notifications"), str(e))
            self._quietly(self.sw_tray, tray.autostart_enabled())
            return
        if on:
            if not tray.tray_running():
                tray.start_tray()
        else:
            tray.poke("quit")  # no icon now either: that is what turning it off means

    # ------------------------------------------------ download ahead (root, through the helper)
    def _ahead_toggled(self, on):
        if on:
            title = _("Download the updates ahead?")
            text = _("The system timer will also download the packages of the next upgrade, without installing "
                     "them. They wait in zypper's cache (/var/cache/zypp/packages) until “Run the routine” "
                     "installs them: a big upgrade can take a few gigabytes. On a metered connection (a phone "
                     "hotspot) nothing is downloaded ahead.\n\nIt asks for the administrator password once.")
        else:
            title = _("Stop downloading the updates ahead?")
            text = _("What was already downloaded stays until the next upgrade uses it.\n\nIt asks for the "
                     "administrator password once.")
        if not ask(self, title, text):
            self._quietly(self.sw_ahead, AHEAD_FLAG.exists())
            return
        started = self.ctx.run([Step("download-ahead", ["on" if on else "off"], root=True)],
                               _("Download updates ahead"), on_done=lambda _ok: self._ahead_done())
        if started is False:
            self._quietly(self.sw_ahead, AHEAD_FLAG.exists())

    def _ahead_done(self):
        self._quietly(self.sw_ahead, AHEAD_FLAG.exists())
        self._show_commands()

    # ------------------------------------------------ the timer
    def _show_commands(self):
        cmds = [TIMER_COMMANDS[0]]
        if AHEAD_FLAG.exists():
            cmds.append(TIMER_COMMANDS[1])
        cmds.append(TIMER_COMMANDS[2])
        self.cmd.setText("\n".join(cmds))

    def _timer_read(self, st, err):
        self.enable_cmd.hide()
        if err is not None or st is None:
            self.timer_lbl.hide()
            self.timer_dot.hide()
            return
        if not st["installed"]:
            color = ORANGE
            text = _("The timer is not installed on this computer yet: install.sh (or the zypp-desk package) "
                     "sets it up.")
        elif st["enabled"] or st["active"]:
            color = GREEN
            text = _("The timer is on.")
            if st["last"]:
                text += " " + _("It last ran {when}.").format(when=_when(st["last"]))
        else:
            color = ORANGE
            text = _("The timer is installed but turned off. To turn it on, run this in a terminal:")
            self.enable_cmd.show()
        self.timer_dot.setPixmap(dot(color).pixmap(16, 16))
        self.timer_dot.show()
        self.timer_lbl.setText(text)
        self.timer_lbl.show()


__all__ = ["NotifyCard", "Switch", "ensure_tray", "timer_state"]
