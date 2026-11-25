# SPDX-License-Identifier: GPL-3.0-or-later
"""Packages: search with every zypper option, the package card, and adding changes to the queue."""

import html
import re
from functools import cmp_to_key

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QSplitter, QTabWidget, QTextBrowser,
                               QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import appicons, backend
from ..style import BLUE, GREEN, RED, dot
from PySide6.QtGui import QIcon
from ..i18n import _, ngettext
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Chip, Page, button, heading, icon, mono, muted

KIND_LABELS = [("package", _("Packages")), ("pattern", _("Patterns")), ("patch", _("Patches")),
               ("product", _("Products")), ("srcpackage", _("Source packages"))]
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


STATUS_LABELS = [("all", _("Any status")), ("installed", _("Installed")), ("not-installed", _("Not installed"))]


class PackagesPage(Page):
    key = "packages"
    title = _("Packages")
    icon_name = "package-x-generic"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.results = []
        self.current = None
        self.search_id = 0
        self.locked_names = set()
        self.updates = {}
        self.desktop = {}
        self._icons = {}

        self.term = QLineEdit(self)
        self.term.setPlaceholderText(_("Search packages, for example vlc"))
        self.term.setClearButtonEnabled(True)
        self.term.returnPressed.connect(self.search)
        self.kind = QComboBox(self)
        for k, label in KIND_LABELS:
            self.kind.addItem(label, k)
        self.status = QComboBox(self)
        for k, label in STATUS_LABELS:
            self.status.addItem(label, k)
        self.repo = QComboBox(self)
        self.repo.addItem(_("All repositories"), "")
        search_btn = button(_("Search"), "edit-find", self.search)
        self.online_btn = button(_("Search online…"), "internet-services", self.search_online)
        self.online_btn.setToolTip(_("Look for it in the openSUSE Build Service, in Fedora COPR and on Flathub"))

        top = QHBoxLayout()
        top.addWidget(self.term, 1)
        for w in (self.kind, self.status, self.repo, search_btn, self.online_btn):
            top.addWidget(w)

        self.chips = {
            "descriptions": Chip(_("Summary and description")),
            "provides": Chip(_("Provides")),
            "requires": Chip(_("Requires")),
            "recommends": Chip(_("Recommends")),
            "files": Chip(_("Files")),
            "exact": Chip(_("Exact match")),
            "case": Chip(_("Case-sensitive")),
        }
        for key, tip in (("descriptions", _("Also look in the summary and the description, not only the name")),
                         ("provides", _("Packages that provide this name (a library, a command, a capability)")),
                         ("requires", _("Packages that need this name")),
                         ("recommends", _("Packages that recommend this name")),
                         ("files", _("Packages that contain a file with this name or path")),
                         ("exact", _("Only the exact name, not names that contain it")),
                         ("case", _("Tell capitals and small letters apart"))):
            self.chips[key].setToolTip(tip)
        chips = QHBoxLayout()
        chips.addWidget(muted(_("Search in:"), wrap=False))
        for c in self.chips.values():
            chips.addWidget(c)
        chips.addStretch(1)
        lists = QHBoxLayout()
        lists.addWidget(muted(_("Lists:"), wrap=False))
        tips = {"orphaned": _("Orphaned packages (installed, but in no repository)"),
                "unneeded": _("Unneeded packages (nothing requires them any more)"),
                "recommended": _("Recommended packages that are not installed"),
                "suggested": _("Suggested packages that are not installed")}
        for key, label in (("orphaned", _("Orphaned")), ("unneeded", _("Unneeded")),
                           ("recommended", _("Recommended")), ("suggested", _("Suggested"))):
            b = Chip(label)
            b.setToolTip(tips[key])
            b.setCheckable(False)
            b.clicked.connect(lambda _c=False, k=key: self.search(special=k))
            lists.addWidget(b)
        lists.addStretch(1)

        self.info = muted("", self, wrap=False)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([_("Status"), _("Name"), _("Summary"), _("Version"), _("Repository")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(22, 22))
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self.show_details(cur))
        self.tree.setColumnWidth(0, 125)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 250)
        self.tree.setColumnWidth(3, 150)

        self.details = self._build_details()
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.tree)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(chips)
        lay.addLayout(lists)
        lay.addWidget(self.info)
        lay.addWidget(split, 1)

        # the queue changes often (and in bursts): repaint once, later, and only if this page is in front
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.update_statuses)
        ctx.queue.changed.connect(self._queue_changed)
        # details are asked for only when the selection stops moving (arrow keys would start dozens of queries)
        self._details_timer = QTimer(self)
        self._details_timer.setSingleShot(True)
        self._details_timer.setInterval(200)
        self._details_timer.timeout.connect(lambda: self._load_details(self.tree.currentItem()))
        self._statuses_stale = False
        self._dots = {}

    # ------------------------------------------------------------ details panel
    def _build_details(self):
        w = QWidget(self)
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 0, 0, 0)
        self.d_name = heading("", 1.4)
        self.d_summary = muted("")
        self.d_state = QLabel("")
        self.d_state.setWordWrap(True)
        self.d_icon = QLabel(w)
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
        self.b_install = button(_("Install"), "list-add", lambda: self.queue_current("install"))
        self.b_update = button(_("Update"), "system-software-update", lambda: self.queue_current("install"))
        self.b_remove = button(_("Remove"), "edit-delete", lambda: self.queue_current("remove"))
        self.b_keep = button(_("Leave as it is"), "edit-undo", self.unqueue_current)
        self.b_lock = button(_("Lock"), "object-locked", self.lock_current)
        for b in (self.b_install, self.b_update, self.b_remove, self.b_keep, self.b_lock):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)
        self.tabs = QTabWidget(w)
        self.t_info = QTextBrowser(w)
        self.t_info.setOpenExternalLinks(True)
        self.t_versions = QTreeWidget(w)
        self.t_versions.setHeaderLabels([_("Version"), _("Architecture"), _("Repository"), _("Status")])
        self.t_versions.setRootIsDecorated(False)
        self.t_versions.itemDoubleClicked.connect(self.install_version)
        vers = QWidget(w)
        vl = QVBoxLayout(vers)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.addWidget(self.t_versions)
        vl.addWidget(muted(_("Double-click a version to install exactly that one (it may be older).")))
        self.t_deps = QTextBrowser(w)
        self.t_files = QPlainTextEdit(w)
        self.t_files.setReadOnly(True)
        self.t_files.setFont(mono())
        self.t_changes = QPlainTextEdit(w)
        self.t_changes.setReadOnly(True)
        self.t_changes.setFont(mono())
        self.tabs.addTab(self.t_info, _("Information"))
        self.tabs.addTab(vers, _("Versions"))
        self.tabs.addTab(self.t_deps, _("Dependencies"))
        self.tabs.addTab(self.t_files, _("Files"))
        self.tabs.addTab(self.t_changes, _("Changes"))
        v.addWidget(self.tabs, 1)
        self._set_buttons(None)
        return w

    def _set_buttons(self, d):
        state = self.ctx.queue.state_of(d["name"]) if d else None
        installed = bool(d and d["installed"])
        is_pkg = bool(d and d["kind"] == "package")
        self.b_install.setVisible(bool(d) and not installed and state is None and d["kind"] != "srcpackage")
        self.b_update.setVisible(bool(d) and self._updatable(d) and state is None)
        self.b_remove.setVisible(installed and state is None and is_pkg)
        self.b_keep.setVisible(state is not None)
        self.b_lock.setVisible(bool(d) and is_pkg)
        if d:
            locked = d["name"] in self.locked_names
            self.b_lock.setText(_("Unlock") if locked else _("Lock"))

    # ------------------------------------------------------------ data
    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)
        run_async(backend.locks, on_done=self._locks_loaded)
        run_async(backend.list_updates, on_done=self._updates_loaded)

    def _updates_loaded(self, updates, err):
        if err:
            return
        self.updates = {u["name"]: u for u in updates}
        self.update_statuses()

    def _updatable(self, d):
        # Qt hands back a copy of the dict stored in an item, so this is computed, never stored.
        return bool(d and d["installed"] and d["name"] in self.updates)

    def _repos_loaded(self, repos, err):
        if err:
            return
        cur = self.repo.currentData()
        self.repo.blockSignals(True)
        self.repo.clear()
        self.repo.addItem(_("All repositories"), "")
        for r in sorted((r for r in repos if r["enabled"]), key=lambda r: r["name"].lower()):
            self.repo.addItem(r["name"], r["alias"])
        i = self.repo.findData(cur)
        self.repo.setCurrentIndex(max(i, 0))
        self.repo.blockSignals(False)

    def _locks_loaded(self, locks, err):
        if not err:
            self.locked_names = {lk["name"] for lk in locks}
            self.update_statuses()

    def search(self, special=None):
        term = self.term.text().strip()
        if not term and not special:
            self.term.setFocus()
            return
        self.search_id += 1
        sid = self.search_id
        opts = {k: c.isChecked() for k, c in self.chips.items()}
        self.info.setText(_("Searching…"))
        self.tree.clear()
        run_async(backend.search, term, kind=self.kind.currentData(), status=self.status.currentData(),
                  repo=self.repo.currentData() or None, special=special, **opts,
                  on_done=lambda res, err: self._searched(sid, res, err, special))

    def search_online(self):
        try:
            from .findrepo import FindRepoDialog
        except ImportError:
            return
        FindRepoDialog(self.ctx, term=self.term.text().strip(), parent=self).exec()

    def _searched(self, sid, results, err, special):
        if sid != self.search_id:
            return  # an older search finished late
        if err:
            self.info.setText(_("The search failed: {error}").format(error=str(err)[:200]))
            return
        self.results = results
        what = {"orphaned": _("Orphaned packages (installed, but in no repository)"),
                "unneeded": _("Unneeded packages (nothing requires them any more)"),
                "recommended": _("Recommended packages that are not installed"),
                "suggested": _("Suggested packages that are not installed")}.get(special)
        n = len(results)
        self.info.setText((what + " · " if what else "") + ngettext("{n} found", "{n} found", n).format(n=n))
        term = self.term.text().strip().lower()
        # the package with exactly the name you typed first ("vlc" before "libextractor-plugins-vlc")
        results = sorted(results, key=lambda d: d["name"].lower() != term)
        missing = n == 0 and not special
        if missing:
            self.info.setText(_("Not found in your repositories. “Search online…” looks for it in the openSUSE Build "
                                "Service, in Fedora COPR and on Flathub."))
        self.online_btn.setProperty("primary", missing)
        self.online_btn.style().unpolish(self.online_btn)
        self.online_btn.style().polish(self.online_btn)
        self.tree.setSortingEnabled(False)
        for d in results:
            ref = d["installed"] or d["best"] or (d["versions"][0] if d["versions"] else None)
            it = QTreeWidgetItem(self.tree, ["", d["name"], d["summary"], ref["edition"] if ref else "",
                                             ref["repo"] if ref else ""])
            it.setData(0, Qt.ItemDataRole.UserRole, d)
            it.setIcon(1, self.pkg_icon(d))
            if len(d["versions"]) > 1:
                it.setToolTip(3, _("{n} versions available").format(n=len(d["versions"])))
            self._paint_status(it, d)
        # keep the order of the search (the exact name first); clicking a header sorts as usual
        self.tree.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.tree.setSortingEnabled(True)
        if n:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._clear_details()
        names = [d["name"] for d in results if d["installed"] and d["name"] not in self.desktop][:400]
        if names:
            run_async(appicons.desktop_icons, names, on_done=self._desktop_icons_loaded)

    def _desktop_icons_loaded(self, icons, err):
        if icons:
            self.desktop.update(icons)
            self.refresh_icons()

    def pkg_icon(self, d):
        name = d["name"]
        if name in self._icons:
            return self._icons[name]
        ic = QIcon()
        found = self.ctx.icon_catalog.get(name)
        if found and found[0] == "file":
            ic = QIcon(found[1])
        elif found and found[0] == "theme":
            ic = QIcon.fromTheme(found[1])
        if ic.isNull() and name in self.desktop:
            ic = QIcon.fromTheme(self.desktop[name])
        if ic.isNull():
            generic = "application-x-sharedlib" if name.startswith("lib") else "package-x-generic"
            return icon(generic, "package-x-generic")  # not cached: a better icon may appear later
        self._icons[name] = ic
        return ic

    def refresh_icons(self):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setIcon(1, self.pkg_icon(it.data(0, Qt.ItemDataRole.UserRole)))
        if self.current:
            self.d_icon.setPixmap(self.pkg_icon(self.current).pixmap(56, 56))

    def _queue_changed(self):
        if self.isVisible():
            self._status_timer.start(0)
        else:
            self._statuses_stale = True

    def activated(self):
        if self._statuses_stale:
            self._statuses_stale = False
            self._status_timer.start(0)
        super().activated()

    def update_statuses(self):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            self._paint_status(it, it.data(0, Qt.ItemDataRole.UserRole))
        if self.current:
            self._set_buttons(self.current)
            self._show_state(self.current)

    def _status_text(self, d):
        q = self.ctx.queue.state_of(d["name"])
        if q == "install":
            return (_("Will be updated") if d["installed"] else _("Will be installed")), "#27ae60", "list-add"
        if q == "remove":
            return _("Will be removed"), "#da4453", "list-remove"
        if d["name"] in self.locked_names:
            return _("Locked"), None, "object-locked"
        if self._updatable(d):
            return _("Update available"), "#1d99f3", "update-low"
        if d["installed"]:
            return _("Installed"), None, "checkmark"
        return _("Not installed"), None, ""

    def _paint_status(self, it, d):
        text, color, ic = self._status_text(d)
        it.setText(0, text)
        dots = {"checkmark": GREEN, "update-low": BLUE, "list-add": GREEN, "list-remove": RED}
        if ic in dots:
            if dots[ic] not in self._dots:
                self._dots[dots[ic]] = dot(dots[ic])
            it.setIcon(0, self._dots[dots[ic]])
        else:
            it.setIcon(0, icon(ic) if ic else QIcon())
        brush = QBrush(QColor(color)) if color else QBrush()
        it.setForeground(0, brush)
        f = it.font(0)
        f.setBold(bool(color and color != "#1d99f3"))
        it.setFont(0, f)

    # ------------------------------------------------------------ details
    def _clear_details(self):
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

    def show_details(self, item):
        if not item:
            return
        self._details_timer.start()  # the queries wait until the selection stops moving

    def _load_details(self, item):
        if not item:
            return
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if not d:
            return
        self.current = d
        self.d_name.setText(d["name"])
        self.d_icon.setPixmap(self.pkg_icon(d).pixmap(56, 56))
        self.d_summary.setText(d["summary"])
        self._show_state(d)
        self._set_buttons(d)
        self.t_versions.clear()
        for v in sorted(d["versions"], key=cmp_to_key(lambda a, b: backend.vercmp(a["edition"], b["edition"])),
                        reverse=True):
            st = {"installed": _("installed"), "not-installed": "", "other-version": ""}.get(v["status"], v["status"])
            vi = QTreeWidgetItem(self.t_versions, [v["edition"], v["arch"], v["repo"], st])
            vi.setData(0, Qt.ItemDataRole.UserRole, v)
        for c in range(4):
            self.t_versions.resizeColumnToContents(c)
        self.t_info.setHtml(f"<i>{html.escape(_('Loading…'))}</i>")
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

    def _show_state(self, d):
        text, _color, _ic = self._status_text(d)
        parts = [text]
        if d["installed"]:
            parts.append(_("installed {version} from {repo}").format(version=d["installed"]["edition"],
                                                                     repo=d["installed"]["repo"]))
        up = self.updates.get(d["name"]) if self._updatable(d) else None
        if up:
            parts.append(_("{version} available in {repo}").format(version=up["edition"], repo=up["repo"]))
        self.d_state.setText(" · ".join(parts))

    def _info_loaded(self, name, info, err):
        if not self.current or self.current["name"] != name:
            return
        if err or not info:
            self.t_info.setPlainText(str(err or _("No information.")))
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
        self.t_info.setHtml(f"<table>{''.join(rows)}</table><p>{desc}</p>")

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
            extra = _("Not signed") if sig.startswith("(none)") else _("Signed: {sig}").format(sig=sig)
            self.d_state.setText(self.d_state.text() + "\n" + extra)

    # ------------------------------------------------------------ queue
    def queue_current(self, kind):
        d = self.current
        if not d:
            return
        # patterns, patches and products are installed as "pattern:name"
        target = d["name"] if d["kind"] == "package" else f"{d['kind']}:{d['name']}"
        self.ctx.queue.add(Action(kind, target, package=d["name"]))

    def unqueue_current(self):
        if self.current:
            for kind in ("install", "remove"):
                for a in list(self.ctx.queue.of(kind)):
                    if (a.opts.get("package") or a.target.split("=")[0]) == self.current["name"]:
                        self.ctx.queue.discard_action(a)

    def lock_current(self):
        if not self.current:
            return
        name = self.current["name"]
        if name in self.locked_names:
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
