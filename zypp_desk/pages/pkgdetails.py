# SPDX-License-Identifier: GPL-3.0-or-later
"""The package card: name, state, actions and the tabs Information, Versions, Dependencies, Files, Changes.

The same card is used by the Packages and the Updates pages. The host page gives it:
  host.updates        {name: update} from 'zypper list-updates'
  host.locked_names   names with a lock
  host.pkg_icon(d)    the icon of a package
and it shows packages as the search returns them (backend.search: name, kind, summary, versions, installed…).
"""

import html
import re
from functools import cmp_to_key

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPlainTextEdit, QTabWidget, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import backend
from ..i18n import _
from ..tasks import run_async
from ..txqueue import Action
from ..ui import button, heading, mono, muted

# zypper info prints these keys in English (it runs with LC_ALL=C); these are their labels.
INFO_LABELS = {"Repository": _("Repository"), "Version": _("Version"), "Arch": _("Architecture"),
               "Vendor": _("Vendor"), "Installed Size": _("Installed size"), "Status": _("Status"),
               "Source package": _("Source package"), "Upstream URL": _("Web site"), "License": _("License")}
DEP_LABELS = {"Provides": _("Provides"), "Requires": _("Requires"), "Recommends": _("Recommends"),
              "Suggests": _("Suggests"), "Conflicts": _("Conflicts"), "Obsoletes": _("Obsoletes"),
              "Supplements": _("Supplements")}


def status_label(text):
    """zypper info's Status field (in English, LC_ALL=C), in the user's language."""
    m = re.match(r"out-of-date \(version (.+) installed\)", text)
    if m:
        return _("an update is available (installed: {version})").format(version=m.group(1))
    return {"up-to-date": _("up to date"), "not installed": _("not installed")}.get(text, text)


class PackageDetails(QWidget):
    def __init__(self, host, ctx, parent=None):
        super().__init__(parent)
        self.host = host
        self.ctx = ctx
        self.current = None
        self.extra_html = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 0, 0, 0)
        self.d_name = heading("", 1.4)
        self.d_summary = muted("")
        self.d_state = QLabel("")
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
        btns = QHBoxLayout()
        self.b_custom = button("", "download", None, primary=True)  # a host's own main action (web downloads)
        self.b_custom.hide()
        self._custom_slot = None
        self.b_custom.clicked.connect(lambda: self._custom_slot and self._custom_slot())
        self.b_install = button(_("Install"), "list-add", lambda: self.queue_current("install"))
        self.b_update = button(_("Update"), "system-software-update", lambda: self.queue_current("install"))
        self.b_remove = button(_("Remove…"), "edit-delete", self.remove_current)
        self.b_keep = button(_("Leave as it is"), "edit-undo", self.unqueue_current)
        self.b_lock = button(_("Lock"), "object-locked", self.lock_current)
        for b in (self.b_custom, self.b_install, self.b_update, self.b_remove, self.b_keep, self.b_lock):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)
        self.tabs = QTabWidget(self)
        self.t_info = QTextBrowser(self)
        self.t_info.setOpenExternalLinks(True)
        self.t_versions = QTreeWidget(self)
        self.t_versions.setHeaderLabels([_("Version"), _("Architecture"), _("Repository"), _("Status")])
        self.t_versions.setRootIsDecorated(False)
        self.t_versions.itemDoubleClicked.connect(self.install_version)
        vers = QWidget(self)
        vl = QVBoxLayout(vers)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.addWidget(self.t_versions)
        vl.addWidget(muted(_("Double-click a version to install exactly that one (it may be older).")))
        self.t_deps = QTextBrowser(self)
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
        self.set_buttons(None)

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

    def set_custom(self, label=None, slot=None):
        """A main button of the host page (for example 'Download the new version'); None hides it."""
        self._custom_slot = slot
        self.b_custom.setText(label or "")
        self.b_custom.setVisible(bool(label))
        self.set_buttons(self.current)

    def set_buttons(self, d):
        state = self.ctx.queue.state_of(d["name"]) if d else None
        installed = bool(d and d["installed"])
        is_pkg = bool(d and d["kind"] == "package")
        custom = self.b_custom.isVisible() or bool(self._custom_slot)
        self.b_install.setVisible(bool(d) and not installed and state is None and d["kind"] != "srcpackage")
        self.b_update.setVisible(bool(d) and self.updatable(d) and state is None and not custom)
        self.b_remove.setVisible(installed and state is None and is_pkg)
        self.b_keep.setVisible(state is not None)
        self.b_lock.setVisible(bool(d) and is_pkg)
        if d:
            locked = d["name"] in self.host.locked_names
            self.b_lock.setText(_("Unlock") if locked else _("Lock"))

    def show_state(self, d):
        text, _color, _ic = self.status_text(d)
        parts = [text]
        if d["installed"]:
            parts.append(_("installed {version} from {repo}").format(version=d["installed"]["edition"],
                                                                     repo=d["installed"]["repo"]))
        up = self.host.updates.get(d["name"]) if self.updatable(d) else None
        if up:
            parts.append(_("{version} available in {repo}").format(version=up["edition"], repo=up["repo"]))
        self._state_base = " · ".join(parts)
        self.d_state.setText(self._state_base + (("\n" + self._sig_text) if getattr(self, "_sig_text", "") else ""))

    def refresh(self):
        """After the queue or the locks changed."""
        if self.current:
            self.set_buttons(self.current)
            self.show_state(self.current)

    def refresh_icon(self):
        if self.current:
            self.d_icon.setPixmap(self.host.pkg_icon(self.current).pixmap(56, 56))

    # ------------------------------------------------------------ showing a package
    def clear(self):
        self.current = None
        self.d_name.setText("")
        self.d_icon.clear()
        self.d_summary.setText("")
        self.d_state.setText("")
        self.t_versions.clear()
        for w in (self.t_info, self.t_deps):
            w.setHtml("")
        for w in (self.t_files, self.t_changes):
            w.setPlainText("")
        self.set_buttons(None)

    def show_simple(self, title, summary, qicon, info_html):
        """Something that is not an RPM package (a Flatpak app, a site that did not answer): only Information."""
        self.clear()
        self.d_name.setText(title)
        self.d_summary.setText(summary)
        if qicon is not None:
            self.d_icon.setPixmap(qicon.pixmap(56, 56))
        self.t_info.setHtml(info_html)
        self.tabs.setCurrentIndex(0)
        for w in (self.t_files, self.t_changes):
            w.setPlainText(_("Only for RPM packages."))

    def load(self, d, extra_html=""):
        """Show a package (a dict as backend.search returns it). extra_html goes on top of Information."""
        self.current = d
        self.extra_html = extra_html
        self._sig_text = ""
        self.d_name.setText(d["name"])
        self.d_icon.setPixmap(self.host.pkg_icon(d).pixmap(56, 56))
        self.d_summary.setText(d["summary"])
        self.show_state(d)
        self.set_buttons(d)
        self.t_versions.clear()
        for v in sorted(d["versions"], key=cmp_to_key(lambda a, b: backend.vercmp(a["edition"], b["edition"])),
                        reverse=True):
            st = {"installed": _("installed"), "not-installed": "", "other-version": ""}.get(v["status"], v["status"])
            vi = QTreeWidgetItem(self.t_versions, [v["edition"], v["arch"], v["repo"], st])
            vi.setData(0, Qt.ItemDataRole.UserRole, v)
        for c in range(4):
            self.t_versions.resizeColumnToContents(c)
        self.t_info.setHtml(extra_html + f"<i>{html.escape(_('Loading…'))}</i>")
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
            run_async(backend.signature_of, name, on_done=lambda r, e, n=name: self._sig_loaded(n, r))
        else:
            self.t_files.setPlainText(_("The file list is shown for installed packages."))
            self.t_changes.setPlainText(_("The changelog is shown for installed packages."))

    def refine(self, d):
        """The full search result of the package shown (versions, summary) arrives later: complete the card
        without asking zypper for the rest again."""
        if not self.current or self.current["name"] != d["name"]:
            return
        self.current = d
        if d.get("summary"):
            self.d_summary.setText(d["summary"])
        self.t_versions.clear()
        for v in sorted(d["versions"], key=cmp_to_key(lambda a, b: backend.vercmp(a["edition"], b["edition"])),
                        reverse=True):
            st = {"installed": _("installed"), "not-installed": "", "other-version": ""}.get(v["status"], v["status"])
            vi = QTreeWidgetItem(self.t_versions, [v["edition"], v["arch"], v["repo"], st])
            vi.setData(0, Qt.ItemDataRole.UserRole, v)
        for c in range(4):
            self.t_versions.resizeColumnToContents(c)
        self.show_state(d)
        self.set_buttons(d)

    def _info_loaded(self, name, info, err):
        if not self.current or self.current["name"] != name:
            return
        if err or not info:
            self.t_info.setHtml(self.extra_html + html.escape(str(err or _("No information."))))
            return
        rows = []
        for key in INFO_LABELS:
            if info.get(key):
                val = html.escape(status_label(info[key]) if key == "Status" else info[key])
                if key == "Upstream URL":
                    val = f'<a href="{val}">{val}</a>'
                rows.append(f"<tr><td style='padding-right:14px;color:gray'>{html.escape(INFO_LABELS[key])}</td>"
                            f"<td>{val}</td></tr>")
        desc = html.escape(info.get("Description", "")).replace("\n", "<br>")
        self.t_info.setHtml(f"{self.extra_html}<table>{''.join(rows)}</table><p>{desc}</p>")

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
        if self.current and self.current["name"] == name and sig:
            self._sig_text = _("Not signed") if sig.startswith("(none)") else _("Signed: {sig}").format(sig=sig)
            self.d_state.setText(getattr(self, "_state_base", "") + "\n" + self._sig_text)

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
        RemoveDialog(self.ctx, d, self.host.pkg_icon(d), d["name"], self).exec()

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
