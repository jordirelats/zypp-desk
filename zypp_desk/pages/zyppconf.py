# SPDX-License-Identifier: GPL-3.0-or-later
"""The card "How zypper works" of the Settings page: a few zypp.conf settings, in plain words.

It shows what libzypp uses now and where each value comes from (zyppconf.py does the reading and merging).
Changes go to the queue as one 'zypp-conf' action holding the whole content of Zypp Desk's drop-in
(/etc/zypp/zypp.conf.d/50-zypp-desk.conf); the root helper writes it when the queue is applied.
"""

import os

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import zyppconf
from ..i18n import _
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Card, ask, button, error_box, heading, mono, muted

GREY = "#7f8c8d"


def _texts():
    """Title and one short line for each setting (a function: the strings are translated when called)."""
    return {
        "solver.onlyRequires": (_("Install recommended packages too"),
                                _("They are not essential, but they complete the program.")),
        "multiversion.kernels": (_("Kernels to keep"),
                                 _("So you can start with the previous one if the new one fails.")),
        "download.max_concurrent_connections": (_("Downloads at once"), _("Connections per file.")),
        "download.max_download_speed": (_("Maximum download speed"), _("So updates do not fill your connection.")),
        "solver.dupAllowVendorChange": (_("Allow changing vendor when updating"),
                                        _("For example, moving VLC from openSUSE to VideoLAN.")),
        "solver.cleandepsOnRemove": (_("Always remove what is no longer needed"),
                                     _("Better to choose it each time, with the Remove button: the zypp.conf manual "
                                       "warns that turning it on for everything can damage the system.")),
    }


def _tooltips():
    return {
        "solver.onlyRequires": _("Recommended packages add optional parts: plugins, codecs, translations, "
                                 "documentation. Off is like always adding --no-recommends."),
        "multiversion.kernels": _("After a kernel update, the old kernels are removed (zypper purge-kernels): "
                                  "these are the ones that stay."),
        "download.max_concurrent_connections": _("zypper downloads a file from several mirrors at once. "
                                                 "Fewer connections are gentler with a slow network."),
        "download.max_download_speed": _("A limit leaves room for a video call or other downloads while zypper "
                                         "is working."),
        "solver.dupAllowVendorChange": _("Off is safer: an update (dup) keeps every package from the vendor it "
                                         "came from, so packages from VideoLAN or Packman are not replaced. "
                                         "For one update only, there is zypper's option --allow-vendor-change."),
        "solver.cleandepsOnRemove": _("When on, removing a package also removes the packages that only it needed "
                                      "(zypper remove --clean-deps), even when nobody is watching, for example "
                                      "in automatic updates."),
    }


def kernel_label(value):
    labels = {
        "latest,running": _("The newest and the one you use"),
        "latest,latest-1,running": _("The newest, the one before and the one you use"),
        "latest,latest-1,latest-2,running": _("The three newest and the one you use"),
        "oldest,latest,running": _("The oldest, the newest and the one you use"),
    }
    if value in labels:
        return labels[value]
    if not value.strip():
        return _("All of them (none is removed)")
    return _("Your own list: {value}").format(value=value)


def speed_label(value):
    if value == "0":
        return _("No limit")
    if value.isdigit() and int(value) % 1000000 == 0:
        return _("{n} MB/s").format(n=int(value) // 1000000)
    if value.isdigit():
        return _("{n} bytes per second").format(n=value)
    return _("Other: {value}").format(value=value)


def value_label(key, value):
    if key == "multiversion.kernels":
        return kernel_label(value)
    if key == "download.max_download_speed":
        return speed_label(value)
    spec = zyppconf.SPECS[key]
    if value in spec.choices:
        return value
    return _("Other: {value}").format(value=value)


def _rgba(color, alpha):
    c = QColor(color)
    return "rgba({}, {}, {}, {})".format(c.red(), c.green(), c.blue(), alpha)


class Pill(QLabel):
    """A small rounded tag: where a value comes from, or "experts only"."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        f = self.font()
        f.setPointSizeF(f.pointSizeF() * 0.88)
        self.setFont(f)

    def show_as(self, text, color, tip=""):
        self.setText(text)
        self.setToolTip(tip)
        self.setStyleSheet("QLabel {{ color: {c}; background: {bg}; border-radius: 7px; padding: 2px 8px; }}"
                           .format(c=color, bg=_rgba(color, 0.14)))
        self.show()


class Switch(QCheckBox):
    """A QCheckBox drawn as an on/off switch (keeps QCheckBox's signals and accessibility)."""

    W, H = 42, 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # the focus ring only shows when moving with the keyboard
        self.setFixedSize(self.W, self.H)

    def sizeHint(self):
        return QSize(self.W, self.H)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(1.5, 2.5, self.W - 3, self.H - 5)
        on = self.isChecked()
        color = QColor(GREEN) if on else QColor(127, 140, 141, 120)
        if not self.isEnabled():
            color.setAlpha(60)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        r = track.height() / 2
        p.drawRoundedRect(track, r, r)
        d = track.height() - 4
        x = track.right() - 2 - d if on else track.left() + 2
        p.setBrush(QColor(255, 255, 255, 255 if self.isEnabled() else 170))
        p.drawEllipse(QRectF(x, track.top() + 2, d, d))
        if self.hasFocus():
            p.setPen(QPen(self.palette().highlight().color(), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-1, -1, 1, 1), r + 1, r + 1)
        p.end()


class SettingRow(QWidget):
    """One setting: title and pills, a short line, 'key = value' and the control on the right."""

    edited = Signal()

    def __init__(self, spec, parent=None):
        super().__init__(parent)
        self.spec = spec
        title, line = _texts()[spec.key]
        tip = _tooltips()[spec.key]
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 9, 4, 9)
        h.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(title, self)
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setToolTip(tip)
        top.addWidget(self.title)
        self.pill = Pill(self)
        top.addWidget(self.pill)
        self.expert = Pill(self)
        self.expert.setVisible(spec.expert)
        if spec.expert:
            self.expert.show_as(_("experts only"), RED, _("The zypp.conf manual marks this setting "
                                                         "«Expert Only». Leave it off unless you know why."))
        top.addWidget(self.expert)
        top.addStretch(1)
        text.addLayout(top)
        self.line = muted(line, self)
        self.line.setToolTip(tip)
        text.addWidget(self.line)
        self.keyline = QLabel(self)
        self.keyline.setTextFormat(Qt.TextFormat.PlainText)
        self.keyline.setFont(mono())
        self.keyline.setStyleSheet("color: palette(placeholder-text);")
        self.keyline.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.keyline)
        self.note = QLabel(self)  # "set in a file read after ours"
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {ORANGE};")
        self.note.hide()
        text.addWidget(self.note)
        h.addLayout(text, 1)
        if spec.kind == "bool":
            self.control = Switch(self)
            self.control.toggled.connect(self._changed)
        else:
            self.control = QComboBox(self)
            self.control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            self.control.setMinimumWidth(150)
            self.control.currentIndexChanged.connect(self._changed)
        self.control.setAccessibleName(title)
        self.control.setToolTip(tip)
        h.addWidget(self.control, 0, Qt.AlignmentFlag.AlignVCenter)
        self._setting = False

    # ---------------------------------------------------------- the value of the control
    def value(self):
        """What the control says, in zypp.conf's words ("true"/"false" for switches)."""
        if self.spec.key == "solver.onlyRequires":  # shown inverted: on = recommended packages = false
            return "false" if self.control.isChecked() else "true"
        if self.spec.kind == "bool":
            return "true" if self.control.isChecked() else "false"
        return self.control.currentData()

    def set_value(self, value):
        self._setting = True
        try:
            if self.spec.key == "solver.onlyRequires":
                self.control.setChecked(value != "true")
            elif self.spec.kind == "bool":
                self.control.setChecked(value == "true")
            else:
                self._fill(value)
        finally:
            self._setting = False
        self.keyline.setText("{} = {}".format(self.spec.key, value))

    def _fill(self, value, extra=()):
        """The allowed values, plus the current one and openSUSE's if they are something else."""
        combo = self.control
        combo.clear()
        values = list(self.spec.choices)
        for v in (*extra, value):
            if v is not None and v not in values:
                values.append(v)
        for v in values:
            combo.addItem(value_label(self.spec.key, v), v)
            combo.setItemData(combo.count() - 1, "{} = {}".format(self.spec.key, v), Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(values.index(value))

    def offer(self, values):
        """Keep values that are not in the list (the current file's, openSUSE's) selectable."""
        if self.spec.kind == "bool":
            return
        current = self.value()
        self._setting = True
        try:
            self._fill(current, values)
        finally:
            self._setting = False
        self.keyline.setText("{} = {}".format(self.spec.key, current))

    def _changed(self, *_args):
        self.keyline.setText("{} = {}".format(self.spec.key, self.value()))
        if not self._setting:
            self.edited.emit()

    # ---------------------------------------------------------- where it comes from
    def show_state(self, info, disk, pending):
        """info: what applies (with the queued drop-in, if any); disk: what applies now; pending: a
        'zypp-conf' action is in the queue."""
        value = self.value()
        locked = info["later"]
        self.control.setEnabled(not locked)
        if locked:
            self.note.setText(_("Set in {file}, which zypper reads after Zypp Desk's file: change it there.")
                              .format(file=info["file"]))
        self.note.setVisible(locked)
        if value != info["value"]:
            self.pill.show_as(_("changed"), ORANGE, _("Not saved yet: add the changes to the queue."))
        elif pending and info["value"] != disk["value"]:
            self.pill.show_as(_("in the queue"), BLUE, _("It is written when you apply the queue. Now: {value}")
                              .format(value="{} = {}".format(self.spec.key, disk["value"])))
        else:
            src, path = info["source"], info["file"]
            if src == "default" and self.spec.expert:
                self.pill.hide()  # the red pill says enough
            elif src == "default":
                self.pill.show_as(_("default"), GREY, _("libzypp's built-in value (man 5 zypp.conf)."))
            elif src == "openSUSE":
                self.pill.show_as(_("from openSUSE"), GREY, _("Set by openSUSE in {file}.").format(file=path))
            elif src == "yours":
                self.pill.show_as(_("yours"), GREEN, _("Set with Zypp Desk in {file}.").format(file=path))
            else:
                self.pill.show_as(_("in {file}").format(file=os.path.basename(path)), PURPLE,
                                  _("Set in {file}.").format(file=path))


def _separator(parent=None):
    f = QFrame(parent)
    f.setFixedHeight(1)
    f.setStyleSheet("background: rgba(127, 140, 141, 0.25); border: none;")
    return f


class ZyppConfCard(Card):
    """The card "How zypper works": recommended packages, kernels, downloads and two expert switches."""

    MAX_WIDTH = 800

    def __init__(self, ctx, parent=None, etc=zyppconf.ETC, usr=zyppconf.USR):
        super().__init__(None, parent)
        self.ctx = ctx
        self.etc, self.usr = etc, usr
        self.snap = None
        self._gen = 0
        self._pending_seen = None
        self.setMaximumWidth(self.MAX_WIDTH)  # short lines; _fit_height() keeps the height right
        self.lay.addWidget(heading(_("How zypper works"), 1.15, self))
        self.lay.addWidget(muted(_("This applies to everything: Zypp Desk, zypper in a terminal and Myrlyn."), self))
        self.status = muted(_("Reading the configuration…"), self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)  # it may show an error message
        self.lay.addWidget(self.status)
        self.problems = QLabel(self)
        self.problems.setTextFormat(Qt.TextFormat.PlainText)
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"QLabel {{ color: {RED}; border-left: 4px solid {RED}; padding: 6px 10px; "
                                    f"background: {_rgba(RED, 0.08)}; border-radius: 6px; }}")
        self.problems.hide()
        self.lay.addWidget(self.problems)
        self.rows_box = QWidget(self)
        rows = QVBoxLayout(self.rows_box)
        rows.setContentsMargins(0, 2, 0, 0)
        rows.setSpacing(0)
        self.rows = {}
        for i, spec in enumerate(zyppconf.SETTINGS):
            r = SettingRow(spec, self.rows_box)
            r.edited.connect(self._edited)
            self.rows[spec.key] = r
            rows.addWidget(r)
            rows.addWidget(_separator(self.rows_box))
        self.rows_box.hide()
        self.lay.addWidget(self.rows_box)
        # the changes waiting in the queue
        self.pending_box = QFrame(self)
        self.pending_box.setObjectName("zcpending")
        self.pending_box.setStyleSheet(f"QFrame#zcpending {{ background: {_rgba(BLUE, 0.08)}; "
                                       f"border: 1px solid {_rgba(BLUE, 0.35)}; border-radius: 8px; }}")
        pb = QHBoxLayout(self.pending_box)
        pb.setContentsMargins(10, 6, 6, 6)
        self.pending_text = QLabel(self.pending_box)
        self.pending_text.setWordWrap(True)
        pb.addWidget(self.pending_text, 1)
        pb.addWidget(button(_("Take out of the queue"), "edit-undo", self._discard, parent=self.pending_box), 0)
        self.pending_box.hide()
        self.lay.addWidget(self.pending_box)
        foot = muted(_("Saved in {dropin}. openSUSE's file ({vendor}) is not touched.").format(
            dropin=zyppconf.DROPIN, vendor=zyppconf.USR + "/" + zyppconf.STEM), self)
        f = foot.font()
        f.setPointSizeF(f.pointSizeF() * 0.92)
        foot.setFont(f)
        self.lay.addSpacing(6)
        self.lay.addWidget(foot)
        btns = QHBoxLayout()
        btns.setContentsMargins(0, 6, 0, 0)
        btns.addStretch(1)
        self.defaults_btn = button(_("Back to the defaults"), "edit-reset", self._defaults, parent=self)
        self.add_btn = button(_("Add the changes to the queue"), "list-add", self._add, primary=True, parent=self)
        btns.addWidget(self.defaults_btn)
        btns.addWidget(self.add_btn)
        self.lay.addLayout(btns)
        self.defaults_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        ctx.queue.changed.connect(self._queue_changed)
        self.reload()

    # ---------------------------------------------------------- size
    def sizeHint(self):
        s = super().sizeHint()
        return QSize(max(s.width(), self.MAX_WIDTH), s.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def event(self, event):
        done = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:  # a row grew or shrank, a note appeared…
            self._fit_height()
        return done

    def _fit_height(self):
        """A box layout asks the card's height for the layout's whole width, not for the narrower width
        the card really gets (it has a maximum width), and the wrapped lines would then be cut. The
        minimum height says the truth."""
        lay = self.layout()
        h = lay.totalHeightForWidth(self.width()) if lay is not None and self.width() > 0 else -1
        if h > 0 and h != self.minimumHeight():
            self.setMinimumHeight(h)

    # ---------------------------------------------------------- reading
    def reload(self):
        """Read the files again (in a thread; they are small, but the rule is the rule)."""
        self._gen += 1
        gen = self._gen
        run_async(zyppconf.Snapshot, self.etc, self.usr, on_done=lambda snap, err: self._loaded(gen, snap, err))

    def _loaded(self, gen, snap, err):
        if gen != self._gen:
            return
        if err is not None:
            self.status.setText(_("Could not read the configuration: {error}").format(error=str(err)))
            self.status.show()
            return
        self.snap = snap
        self.status.hide()
        self.rows_box.show()
        probs = snap.problems()
        if probs:
            lines = []
            for path, n, text in probs:
                if n is None:
                    lines.append(_("Could not read {file}: {error}. The values here may not be the ones zypper "
                                   "uses.").format(file=path, error=text))
                else:
                    lines.append(_("{file}, line {n}: «{text}» is not «key = value». zypper stops with an error "
                                   "until it is fixed.").format(file=path, n=n, text=text))
            self.problems.setText("\n".join(lines))
        self.problems.setVisible(bool(probs))
        self._pending_seen = self._pending_settings()
        self._reset_controls()

    # ---------------------------------------------------------- state
    def _pending(self):
        acts = self.ctx.queue.of("zypp-conf")
        return acts[-1] if acts else None

    def _pending_settings(self):
        a = self._pending()
        return dict(a.opts.get("settings") or {}) if a else None

    def _shown(self):
        """What the controls start from: the queued drop-in if there is one, otherwise the disk."""
        pending = self._pending_settings()
        return self.snap.effective(ours=pending) if pending is not None else self.snap.effective()

    def _reset_controls(self):
        if not self.snap:
            return
        shown, disk, base = self._shown(), self.snap.effective(), self.snap.effective(ours=None)
        for key, row in self.rows.items():
            row.set_value(shown[key]["value"])
            row.offer([disk[key]["value"], base[key]["value"]])
        self._refresh()

    def _wanted(self):
        return {key: row.value() for key, row in self.rows.items()}

    def _refresh(self):
        if not self.snap:
            return
        pending = self._pending_settings()
        shown, disk = self._shown(), self.snap.effective()
        dirty = False
        for key, row in self.rows.items():
            row.show_state(shown[key], disk[key], pending is not None)
            dirty |= row.value() != shown[key]["value"]
        self.add_btn.setEnabled(dirty)
        # nothing to do when the deletion is already queued, or when there is no file and nothing queued
        self.defaults_btn.setEnabled(dirty or bool(pending) or (pending is None and self.snap.ours_exists))
        if pending is not None:
            self.pending_text.setText(
                _("Zypp Desk's file will be deleted when you apply the queue.") if not pending else
                _("These values are in the queue: they are written when you apply it."))
        self.pending_box.setVisible(pending is not None)

    def _edited(self):
        self._refresh()

    def _queue_changed(self):
        now = self._pending_settings()
        if now == self._pending_seen:
            return
        was, self._pending_seen = self._pending_seen, now
        if was is not None and now is None:
            self.reload()  # applied (the file changed) or taken out of the queue: read the disk again
        else:
            self._reset_controls()

    # ---------------------------------------------------------- actions
    def _queue(self, settings):
        """Put the drop-in's new content in the queue, or nothing if it is what the disk has already."""
        on_disk = {k: zyppconf.normalize(k, v) for k, v in self.snap.ours_raw.items()}
        if (settings or self.snap.ours_exists) and settings != on_disk:
            self.ctx.queue.add(Action("zypp-conf", "zypp.conf", settings=dict(settings)))
        elif self._pending() is not None:
            self.ctx.queue.discard("zypp-conf", "zypp.conf")

    def _add(self, *_args):
        if not self.snap:
            return
        wanted = self._wanted()
        settings = self.snap.desired(wanted)
        bad = [k for k, v in settings.items() if not zyppconf.validate(k, v)]
        if bad:  # a value from a hand-made file that Zypp Desk does not write: leave that row as it was
            error_box(self, _("Not added"), _("Zypp Desk does not write these values: {list}. Choose one of the "
                                              "list.").format(list=", ".join("{} = {}".format(k, settings[k])
                                                                             for k in bad)))
            return
        self._queue(settings)
        self._refresh()

    def _defaults(self, *_args):
        if not self.snap:
            return
        if self.snap.ours_exists:
            if not ask(self, _("Back to the defaults"),
                       _("Zypp Desk's file ({file}) will be deleted when you apply the queue, and the values of "
                         "openSUSE and of libzypp apply again.").format(file=zyppconf.DROPIN)):
                return
            self._queue({})
        elif self._pending() is not None:
            self.ctx.queue.discard("zypp-conf", "zypp.conf")
        self._reset_controls()

    def _discard(self, *_args):
        self.ctx.queue.discard("zypp-conf", "zypp.conf")
