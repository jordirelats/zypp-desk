# SPDX-License-Identifier: GPL-3.0-or-later
"""The package card: name, state, actions and the tabs Information, Versions, Dependencies, Files, Changes.

The same card is used by the Packages and the Updates pages. The host page gives it:
  host.updates        {name: update} from 'zypper list-updates'
  host.locked_names   names with a lock
  host.pkg_icon(d)    the icon of a package
and it shows packages as the search returns them (backend.search: name, kind, summary, versions, installed…).
What is not an RPM package (a Flatpak app, a firmware, a site that did not answer) goes through show_simple():
Information and the host's own tabs, without the RPM ones.
"""

import html
import re
import subprocess
from functools import cmp_to_key

from PySide6.QtCore import QLocale, Qt, QTimer
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPlainTextEdit, QStackedWidget, QTabWidget, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend
from ..i18n import _
from ..style import GREEN, ORANGE
from ..tasks import run_async
from ..txqueue import Action, TxQueue
from ..ui import FlowLayout, button, heading, mono, muted

DEP_LABELS = {"Provides": _("Provides"), "Requires": _("Requires"), "Recommends": _("Recommends"),
              "Suggests": _("Suggests"), "Conflicts": _("Conflicts"), "Obsoletes": _("Obsoletes"),
              "Supplements": _("Supplements")}
# what zypper calls the installed packages that no repository carries
NO_REPO = ("(System Packages)", "@System")
LABEL_CSS = "padding-right:14px;padding-bottom:3px;color:gray;vertical-align:top"
RPM_TABS = 4  # Versions, Dependencies, Files, Changes (after Information)


def repo_text(repo):
    """A repository as the card shows it: zypper's '(System Packages)' means no repository has that version."""
    if not repo or repo in NO_REPO:
        return _("not in any repository")
    return repo


def size_text(text):
    """zypper's size ('73.9 MiB', always with a dot) in the user's own format ('73,9 MiB')."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?)i?B\s*", text or "")
    if not m:
        return text or ""
    try:
        value = float(m.group(1))
    except ValueError:
        return text
    n = int(value * 1024 ** {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[m.group(2)])
    return QLocale().formattedDataSize(n, 1)


def paragraphs(text):
    """zypper info cuts the description at 80 columns: join the lines of each paragraph again (a line that
    starts a list item starts a new paragraph)."""
    out, cur = [], []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        if cur and re.match(r"([-*•+]|\d+[.)])\s", s):
            out.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return out


def signature_state(name):
    """(state, key ID) of an installed package; state is 'checked' (signed with a key in rpm's keyring, so rpm
    checked it when it was installed), 'unsigned', 'unknown-key' or '' (unknown). Runs rpm: call it in a thread."""
    # rpm -qi as text that may not be UTF-8 (some packages use Latin-1): only the Signature line matters
    p = subprocess.run(["rpm", "-qi", "--", name], capture_output=True, text=True, errors="replace",
                       env=backend.ENV_C)
    line = next((x.split(":", 1)[1].strip() for x in p.stdout.splitlines() if x.startswith("Signature")), "")
    if not line:
        return "", ""
    if line.startswith("(none)"):
        return "unsigned", ""
    m = re.search(r"Key ID ([0-9a-fA-F]+)", line)
    if not m:
        return "", ""
    keyid = m.group(1).lower()
    # rpm 4 names keys gpg-pubkey-<short key ID>, rpm 6 gpg-pubkey-<fingerprint>: both end with the key ID
    keys = backend.rpm_query(["-q", "gpg-pubkey", "--qf", "%{VERSION}\n"]).lower().split()
    known = any(k.endswith(keyid[-8:]) for k in keys)
    return ("checked" if known else "unknown-key"), keyid


def link_or_text(url):
    """A web address as a link only when it is http or https (never smb:, file: or a program's own scheme)."""
    e = html.escape(url or "", quote=True)
    if re.match(r"https?://[^\s]+$", url or "", re.I):
        return f'<a href="{e}">{e}</a>'
    return e


def table(rows):
    """(label, value html) rows as the card's two-column table; the labels are escaped here."""
    return "<table cellspacing='0'>" + "".join(
        f"<tr><td style='{LABEL_CSS}'>{html.escape(k)}</td><td style='padding-bottom:3px'>{v}</td></tr>"
        for k, v in rows if v) + "</table>"


def _browser(parent):
    b = QTextBrowser(parent)
    b.setOpenExternalLinks(True)
    # break lines between words only: "org.kde.Platform" or a package name is never cut in two
    b.setWordWrapMode(QTextOption.WrapMode.WordWrap)
    return b


class PackageDetails(QWidget):
    def __init__(self, host, ctx, parent=None):
        super().__init__(parent)
        self.host = host
        self.ctx = ctx
        self.current = None
        self.overrides = {}
        self._info = None
        self._sig = None
        self._extra = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self)
        outer.addWidget(self.stack)
        # nothing chosen yet: a hint instead of an empty card
        self.hint = muted("", self)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_page = QWidget(self)
        hl = QVBoxLayout(hint_page)
        hl.setContentsMargins(40, 0, 40, 0)  # short lines in the middle of the card
        hl.addStretch(1)
        hl.addWidget(self.hint)
        hl.addStretch(2)
        self.stack.addWidget(hint_page)

        content = QWidget(self)
        v = QVBoxLayout(content)
        v.setContentsMargins(8, 0, 0, 0)
        self.d_name = heading("", 1.4)
        self.d_name.setTextFormat(Qt.TextFormat.PlainText)
        self.d_name.setWordWrap(True)
        self.d_summary = muted("")
        self.d_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.d_state = QLabel("")
        self.d_state.setTextFormat(Qt.TextFormat.PlainText)
        self.d_state.setWordWrap(True)
        self.d_icon = QLabel(self)
        self.d_icon.setFixedSize(60, 60)
        top = QHBoxLayout()
        top.addWidget(self.d_icon, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.addWidget(self.d_name)
        names.addWidget(self.d_summary)
        names.addWidget(self.d_state)
        top.addLayout(names, 1)
        v.addLayout(top)
        # the buttons wrap onto a second line when the card is narrow (German, a 1100 pixel window)
        btn_box = QWidget(self)
        btns = FlowLayout(btn_box)
        self.b_custom = button("", "download", None, primary=True)  # a host's own main action (web downloads)
        self.b_custom.hide()
        self._custom_slot = None
        self.b_custom.clicked.connect(lambda: self._custom_slot and self._custom_slot())
        # Every button after this label only changes the queue: nothing happens to the computer until the queue
        # is applied. They are never green (the green ones change the system now).
        self.to_queue = QLabel(_("To the queue:"), self)
        self.to_queue.setToolTip(_("These buttons only change the queue. Nothing happens to the computer until you "
                                   "apply the queue."))
        self.to_queue.setStyleSheet(f"color: {ORANGE}; font-weight: bold;")
        self.to_queue.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        # a host's own button that only queues (the firmware's Update), after the label
        self.b_qcustom = button("", "system-software-update", None)
        self.b_qcustom.hide()
        self.b_qcustom.clicked.connect(self._qcustom_clicked)
        self._custom_queue = False
        self._flash = QTimer(self)  # "In the queue ✓" for a moment on the host's queue button
        self._flash.setSingleShot(True)
        self._flash.setInterval(1600)
        self._flash.timeout.connect(lambda: self.b_qcustom.setText(self._custom_label))
        self._custom_label = ""
        self.b_install = button(_("Install"), "list-add", lambda: self.queue_current("install"))
        self.b_update = button(_("Update"), "system-software-update", lambda: self.queue_current("install"))
        self.b_remove = button(_("Remove…"), "edit-delete", self.remove_current)
        self.b_keep = button(_("Leave as it is"), "edit-undo", self.unqueue_current)
        self.b_lock = button(_("Lock"), "object-locked", self.lock_current)
        self.to_queue.setMinimumHeight(self.b_update.sizeHint().height())  # in line with the buttons
        for b in (self.b_custom, self.to_queue, self.b_qcustom, self.b_install, self.b_update, self.b_remove,
                  self.b_keep, self.b_lock):
            btns.addWidget(b)
        self._queue_btns = (self.b_qcustom, self.b_install, self.b_update, self.b_remove, self.b_keep, self.b_lock)
        v.addWidget(btn_box)
        self.tabs = QTabWidget(self)
        self.tabs.setUsesScrollButtons(True)
        # it may get small (a 1100×700 window with the terminal open): the card must not make its page scroll
        self.tabs.setMinimumHeight(80)
        self.t_info = _browser(self)
        self.t_versions = QTreeWidget(self)
        self.t_versions.setHeaderLabels([_("Version"), _("Architecture"), _("Repository"), _("Status")])
        self.t_versions.setRootIsDecorated(False)
        self.t_versions.itemDoubleClicked.connect(self.install_version)
        vers = QWidget(self)
        vl = QVBoxLayout(vers)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.addWidget(self.t_versions)
        vl.addWidget(muted(_("Double-click a version to install exactly that one (it may be older).")))
        self.t_deps = _browser(self)
        self.t_files = QPlainTextEdit(self)
        self.t_files.setReadOnly(True)
        self.t_files.setFont(mono())
        self.t_changes = QPlainTextEdit(self)
        self.t_changes.setReadOnly(True)
        self.t_changes.setFont(mono())
        self.tabs.addTab(self.t_info, _("Information"))
        self.tabs.addTab(vers, _("Versions"))
        self.tabs.addTab(self.t_deps, _("Dependencies"))
        self.tabs.addTab(self.t_files, _("Files"))
        self.tabs.addTab(self.t_changes, _("Changes"))
        v.addWidget(self.tabs, 1)
        self.stack.addWidget(content)
        self.set_buttons(None)
        self.clear()

    # ------------------------------------------------------------ state
    def updatable(self, d):
        # Qt hands back a copy of the dict stored in an item, so this is computed, never stored.
        return bool(d and d["installed"] and d["name"] in self.host.updates)

    def status_text(self, d):
        q = self.ctx.queue.state_of(d["name"])
        if q == "install":
            return (_("Will be updated") if d["installed"] else _("Will be installed")), "#27ae60", "list-add"
        if q == "remove":
            return _("Will be removed"), "#da4453", "list-remove"
        if d["name"] in self.host.locked_names:
            return _("Locked"), None, "object-locked"
        if self.updatable(d):
            return _("Update available"), "#1d99f3", "update-low"
        if d["installed"]:
            return _("Installed"), None, "checkmark"
        return _("Not installed"), None, ""

    def set_custom(self, label=None, slot=None, tip=None, queue=False):
        """A main button of the host page (for example 'Download the new version'); None hides it. queue=True: a
        button that only puts something in the queue (the firmware's 'Update'): it goes after "To the queue:" and
        is never green. tip: its tooltip (what it does, on what, and the command)."""
        self._custom_slot = slot
        self._custom_queue = bool(queue and label)
        self._custom_label = label or ""
        self._flash.stop()
        main, other = (self.b_qcustom, self.b_custom) if self._custom_queue else (self.b_custom, self.b_qcustom)
        main.setText(label or "")
        main.setToolTip(tip or "")
        main.setVisible(bool(label))
        other.setText("")
        other.setToolTip("")
        other.hide()
        self.set_buttons(self.current)

    def _qcustom_clicked(self):
        if self._custom_slot:
            self._custom_slot()
            # the card of a firmware shows no state: the button says it happened
            self.b_qcustom.setText(_("In the queue ✓"))
            self._flash.start()

    def _queue_commands(self, action):
        """The command the queue runs for this change alone (the queue's own code and the user's settings)."""
        try:
            q = TxQueue(getattr(self.ctx.queue, "settings", None) or {})
            q.add(action)
            return "\n".join(st.display() for st in q.steps())
        except Exception:  # only a tooltip
            return ""

    def _tips(self, d):
        """What each button does to this package, and the exact command the queue will run."""
        name = d["name"]
        target = name if d["kind"] == "package" else f"{d['kind']}:{name}"
        after = _("Nothing changes until you apply the queue, which runs:")

        def tip(text, action):
            cmd = self._queue_commands(action)
            return text + ("\n" + after + "\n" + cmd if cmd else "")

        self.b_install.setToolTip(tip(_("Puts {name} in the queue, to be installed.").format(name=name),
                                      Action("install", target, package=name)))
        self.b_update.setToolTip(tip(_("Puts {name} in the queue, to be updated.").format(name=name),
                                     Action("install", target, package=name)))
        self.b_remove.setToolTip(_("Asks first whether what only {name} needed goes too, then puts the removal in "
                                   "the queue (zypper remove, with --clean-deps if you choose it).").format(name=name))
        self.b_keep.setToolTip(_("Takes {name} out of the queue: nothing will be done to it.").format(name=name))
        if name in self.host.locked_names:
            self.b_lock.setToolTip(tip(_("Puts the unlocking of {name} in the queue: zypper may then install, "
                                         "update or remove it again.").format(name=name), Action("unlock", name)))
        else:
            self.b_lock.setToolTip(tip(_("Puts a lock on {name} in the queue: zypper will then leave it as it is "
                                         "(no install, update or removal).").format(name=name), Action("lock", name)))

    def set_buttons(self, d):
        state = self.ctx.queue.state_of(d["name"]) if d else None
        installed = bool(d and d["installed"])
        is_pkg = bool(d and d["kind"] == "package")
        custom = (self.b_custom.isVisibleTo(self) or self.b_qcustom.isVisibleTo(self) or bool(self._custom_slot))
        self.b_install.setVisible(bool(d) and not installed and state is None and d["kind"] != "srcpackage")
        self.b_update.setVisible(bool(d) and self.updatable(d) and state is None and not custom)
        self.b_remove.setVisible(installed and state is None and is_pkg)
        self.b_keep.setVisible(state is not None)
        self.b_lock.setVisible(bool(d) and is_pkg)
        if d:
            locked = d["name"] in self.host.locked_names
            self.b_lock.setText(_("Unlock") if locked else _("Lock"))
            self._tips(d)
        # "To the queue:" only in front of buttons that are there
        self.to_queue.setVisible(any(not b.isHidden() for b in self._queue_btns))

    def show_state(self, d):
        """One short word under the name (the versions and where they come from are in the table)."""
        text, color, _ic = self.status_text(d)
        self.d_state.setText(text)
        self.d_state.setStyleSheet(f"color: {color}; font-weight: 600;" if color else "")

    def refresh(self):
        """After the queue or the locks changed."""
        if self.current:
            self.set_buttons(self.current)
            self.show_state(self.current)
            self._render_info()

    def refresh_icon(self):
        if self.current:
            self.d_icon.setPixmap(self.host.pkg_icon(self.current).pixmap(56, 56))

    # ------------------------------------------------------------ the tabs
    def _set_tabs(self, rpm, extra=()):
        """The RPM tabs only for RPM packages; extra = [(label, html)] after Information."""
        for i in range(1, 1 + RPM_TABS):
            self.tabs.setTabVisible(i, rpm)
        while len(self._extra) < len(extra):
            b = _browser(self)
            self._extra.append(b)
            self.tabs.addTab(b, "")
        for i, b in enumerate(self._extra):
            idx = self.tabs.indexOf(b)
            if i < len(extra):
                label, body = extra[i]
                self.tabs.setTabText(idx, label)
                b.setHtml(body)
                self.tabs.setTabVisible(idx, True)
            else:
                b.setHtml("")
                self.tabs.setTabVisible(idx, False)
        # a single tab needs no tab bar
        self.tabs.tabBar().setVisible(rpm or bool(extra))

    # ------------------------------------------------------------ showing a package
    def _reset(self):
        self.current = None
        self.overrides = {}
        self._info = None
        self._sig = None
        self.d_name.setText("")
        self.d_icon.clear()
        self.d_summary.setText("")
        self.d_state.setText("")
        self.d_state.setStyleSheet("")
        self.t_versions.clear()
        for w in (self.t_info, self.t_deps):
            w.setHtml("")
        for w in (self.t_files, self.t_changes):
            w.setPlainText("")
        self.set_buttons(None)

    def clear(self, hint=None):
        """Nothing to show: a hint in the middle of the card."""
        self._reset()
        self.hint.setText(hint or _("Choose a package in the list to see what it is, its versions and what it "
                                    "needs."))
        self.stack.setCurrentIndex(0)

    def show_simple(self, title, summary, qicon, info_html, tabs=()):
        """Something that is not an RPM package (a Flatpak app, a firmware, a site that did not answer):
        Information, plus the host's own tabs [(label, html)]."""
        self._reset()
        self.stack.setCurrentIndex(1)
        self.d_name.setText(title)
        self.d_summary.setText(summary)
        if qicon is not None:
            self.d_icon.setPixmap(qicon.pixmap(56, 56))
        self.t_info.setHtml(info_html)
        self._set_tabs(False, tabs)
        self.tabs.setCurrentIndex(0)

    def load(self, d, overrides=None):
        """Show a package (a dict as backend.search returns it). overrides: what the host knows better than
        zypper, shown in the table: {"new": new version, "from": where it comes from}."""
        self.stack.setCurrentIndex(1)
        self._set_tabs(True)
        self.current = d
        self.overrides = dict(overrides or {})
        self._info = None
        self._sig = None
        self.d_name.setText(d["name"])
        self.d_icon.setPixmap(self.host.pkg_icon(d).pixmap(56, 56))
        self.d_summary.setText(d["summary"])
        self.show_state(d)
        self.set_buttons(d)
        self._fill_versions(d)
        self._render_info()
        self.t_deps.setHtml("")
        self.t_files.setPlainText("")
        self.t_changes.setPlainText("")
        name, kind = d["name"], d["kind"]
        run_async(backend.info, name, kind, on_done=lambda r, e, n=name: self._info_loaded(n, r, e))
        run_async(backend.info_deps, name, kind, on_done=lambda r, e, n=name: self._deps_loaded(n, r, e))
        if d["installed"] and kind == "package":
            run_async(backend.files_of, name, on_done=lambda r, e, n=name: self._text_loaded(n, self.t_files, r))
            run_async(backend.changelog_of, name,
                      on_done=lambda r, e, n=name: self._text_loaded(n, self.t_changes, r))
            run_async(signature_state, name, on_done=lambda r, e, n=name: self._sig_loaded(n, r))
        else:
            self.t_files.setPlainText(_("The file list is shown for installed packages."))
            self.t_changes.setPlainText(_("The changelog is shown for installed packages."))

    def _fill_versions(self, d):
        self.t_versions.clear()
        for v in sorted(d["versions"], key=cmp_to_key(lambda a, b: backend.vercmp(a["edition"], b["edition"])),
                        reverse=True):
            st = {"installed": _("installed"), "not-installed": "", "other-version": ""}.get(v["status"], v["status"])
            vi = QTreeWidgetItem(self.t_versions, [v["edition"], v["arch"], repo_text(v["repo"]), st])
            vi.setData(0, Qt.ItemDataRole.UserRole, v)
        for c in range(4):
            self.t_versions.resizeColumnToContents(c)

    def refine(self, d):
        """The full search result of the package shown (versions, summary) arrives later: complete the card
        without asking zypper for the rest again."""
        if not self.current or self.current["name"] != d["name"]:
            return
        self.current = d
        if d.get("summary"):
            self.d_summary.setText(d["summary"])
        self._fill_versions(d)
        self.show_state(d)
        self.set_buttons(d)
        self._render_info()

    def _render_info(self):
        """The Information tab: a clean table, then the description."""
        d = self.current
        if not d:
            return
        info = self._info if isinstance(self._info, dict) else {}
        o = self.overrides
        e = html.escape
        inst = d.get("installed")
        up = self.host.updates.get(d["name"]) if self.updatable(d) else None
        best = d.get("best") or {}
        rows = []
        if inst:
            rows.append((_("You have"), e(inst["edition"])))
        new = o.get("new") or (up["edition"] if up else "")
        if new:
            rows.append((_("New version"), f"<b>{e(new)}</b>"))
        elif not inst:
            rows.append((_("Version"), e(info.get("Version") or best.get("edition", ""))))
        if o.get("from"):
            src = o["from"]
        elif up:
            src = repo_text(up["repo"])
        elif inst:
            src = repo_text(inst.get("repo") or info.get("Repository"))
        else:
            src = info.get("Repository") or best.get("repo", "")
        rows.append((_("Comes from"), e(src)))
        rows.append((_("Vendor"), e(info.get("Vendor", ""))))
        if info.get("Installed Size"):
            rows.append((_("Size on disk"), e(size_text(info["Installed Size"]))))
        rows.append((_("License"), e(info.get("License", ""))))
        if self._sig:
            state, keyid = self._sig
            if state == "checked":
                sig = (f"<span style='color:{GREEN}'>●&nbsp;{e(_('checked'))}</span> "
                       f"<span style='color:gray'>· {e(_('key ID {id}').format(id=keyid[-8:]))}</span>")
            elif state == "unsigned":
                sig = f"<span style='color:{ORANGE}'>●&nbsp;{e(_('not signed'))}</span>"
            else:
                sig = (f"<span style='color:{ORANGE}'>●&nbsp;"
                       f"{e(_('signed with a key rpm does not know ({key})').format(key=keyid[-8:]))}</span>")
            rows.append((_("Signature"), sig))
        if info.get("Upstream URL"):
            rows.append((_("Web site"), link_or_text(info["Upstream URL"])))
        body = table(rows)
        if self._info is None:
            body += f"<p><i>{e(_('Loading…'))}</i></p>"
        elif isinstance(self._info, str):  # the error zypper gave
            body += f"<p>{e(self._info)}</p>"
        else:
            body += "".join(f"<p>{e(p)}</p>" for p in paragraphs(info.get("Description", "")))
        bar = self.t_info.verticalScrollBar().value()
        self.t_info.setHtml(body)
        self.t_info.verticalScrollBar().setValue(bar)  # a late answer must not jump to the top

    def _info_loaded(self, name, info, err):
        if not self.current or self.current["name"] != name:
            return
        self._info = info if (info and not err) else str(err or _("No information."))
        self._render_info()

    def _deps_loaded(self, name, info, err):
        if not self.current or self.current["name"] != name or err or not info:
            return
        out = []
        for key, items in info.get("deps", {}).items():
            if not items:
                continue
            out.append(f"<h4>{html.escape(DEP_LABELS.get(key, key))} ({len(items)})</h4><ul>"
                       + "".join(f"<li><code>{html.escape(x)}</code></li>" for x in items[:400]) + "</ul>")
        self.t_deps.setHtml("".join(out) or html.escape(_("No dependencies listed.")))

    def _text_loaded(self, name, widget, text):
        if self.current and self.current["name"] == name:
            widget.setPlainText(text or "")

    def _sig_loaded(self, name, sig):
        if self.current and self.current["name"] == name and sig and sig[0]:
            self._sig = sig
            self._render_info()

    # ------------------------------------------------------------ queue
    def queue_current(self, kind):
        d = self.current
        if not d:
            return
        # patterns, patches and products are installed as "pattern:name"
        target = d["name"] if d["kind"] == "package" else f"{d['kind']}:{d['name']}"
        self.ctx.queue.add(Action(kind, target, package=d["name"]))

    def remove_current(self):
        """Remove asks first whether what only this package needed goes too (zypper remove --clean-deps)."""
        d = self.current
        if not d:
            return
        if d["kind"] != "package":
            self.queue_current("remove")
            return
        from .removedlg import RemoveDialog
        dlg = RemoveDialog(self.ctx, d, self.host.pkg_icon(d), d["name"], self)
        dlg.exec()
        dlg.deleteLater()

    def unqueue_current(self):
        if self.current:
            with self.ctx.queue.batch():
                for kind in ("install", "remove"):
                    for a in list(self.ctx.queue.of(kind)):
                        if (a.opts.get("package") or a.target.split("=")[0]) == self.current["name"]:
                            self.ctx.queue.discard_action(a)

    def lock_current(self):
        if not self.current:
            return
        name = self.current["name"]
        if name in self.host.locked_names:
            self.ctx.queue.add(Action("unlock", name))
        else:
            self.ctx.queue.add(Action("lock", name))

    def install_version(self, item, _col):
        if not self.current:
            return
        v = item.data(0, Qt.ItemDataRole.UserRole)
        if v["status"] == "installed":
            return
        target = f"{self.current['name']}={v['edition']}"
        old = bool(self.current["installed"] and backend.vercmp(v["edition"], self.current["installed"]["edition"]) < 0)
        self.ctx.queue.add(Action("install", target, oldpackage=old, repo=v["repo"], package=self.current["name"]))
