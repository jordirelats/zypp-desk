# SPDX-License-Identifier: GPL-3.0-or-later
"""Home: a friendly start. A big search field and tiles that say what there is to do."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from .. import backend, flatpak, langcheck, websources
from ..i18n import _, ngettext
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Page, button, heading, icon, muted


class Tile(QFrame):
    clicked = Signal()

    def __init__(self, icon_name, title, color, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # Tab reaches it; Enter or Space opens it
        self.color = color
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 14, 16, 14)
        h.setSpacing(14)
        badge = QLabel(self)
        badge.setFixedSize(52, 52)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        badge.setStyleSheet(f"background: rgba({r}, {g}, {b}, 0.16); border-radius: 14px;")
        badge.setPixmap(icon(icon_name, "package-x-generic").pixmap(32, 32))
        h.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        self.title = QLabel(title, self)
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)  # long words in German and French: the tile wraps, the window stays
        self.big = QLabel("…", self)
        fb = self.big.font()
        fb.setPointSizeF(fb.pointSizeF() * 1.45)
        fb.setBold(True)
        self.big.setFont(fb)
        self.big.setWordWrap(True)
        self.big.setStyleSheet(f"color: {color};")
        self.small = muted("", self)
        v.addWidget(self.title)
        v.addWidget(self.big)
        v.addWidget(self.small)
        self.extra = QHBoxLayout()
        v.addLayout(self.extra)
        v.addStretch(1)
        h.addLayout(v, 1)

    def set(self, big, small="", color=None, small_color=None):
        self.big.setText(big)
        self.small.setText(small)
        if color:
            self.big.setStyleSheet(f"color: {color};")
        # a warning in the small line shows in its colour; otherwise the usual grey
        self.small.setStyleSheet(f"color: {small_color}; font-weight: 600;" if small_color
                                 else "color: palette(placeholder-text);")

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(ev)


class HomePage(Page):
    key = "home"
    title = _("Home")
    icon_name = "go-home"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        # room enough for a 1100×700 window with the terminal open
        lay.setContentsMargins(22, 16, 22, 12)
        lay.setSpacing(12)
        hello = heading(_("Hello! What would you like to do?"), 1.7)
        lay.addWidget(hello)
        self.product = muted("", self)
        lay.addWidget(self.product)
        # the language of the computer missing from zypper's languages: its translations never arrive (langcheck)
        self.lang_note = QFrame(self)
        self.lang_note.setObjectName("langnote")
        self.lang_note.setStyleSheet("QFrame#langnote { background: rgba(29, 153, 243, 0.08); border: 1px solid "
                                     "rgba(29, 153, 243, 0.35); border-radius: 10px; }")
        ln = QHBoxLayout(self.lang_note)
        ln.setContentsMargins(14, 10, 14, 10)
        ln.setSpacing(12)
        pic = QLabel(self.lang_note)
        pic.setPixmap(icon("preferences-desktop-locale").pixmap(32, 32))
        ln.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        self.lang_text = QLabel(self.lang_note)
        self.lang_text.setWordWrap(True)
        self.lang_text.setTextFormat(Qt.TextFormat.PlainText)
        ln.addWidget(self.lang_text, 1)
        self.lang_btn = button("", "list-add", self._add_language, primary=True)
        ln.addWidget(self.lang_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lang_note.hide()
        self._lang_code = None
        lay.addWidget(self.lang_note)
        ctx.queue.changed.connect(self._lang_refresh)

        self.search = QLineEdit(self)
        self.search.setObjectName("bigsearch")
        self.search.setPlaceholderText(_("Search programs to install, for example krita or vlc…"))
        self.search.addAction(icon("edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.returnPressed.connect(self._search)
        srow = QHBoxLayout()
        srow.addWidget(self.search, 1)
        online = button(_("Search online…"), "internet-services", self._search_online)
        online.setMinimumHeight(40)
        online.setToolTip(_("Look for it in the openSUSE Build Service, in Fedora COPR and on Flathub"))
        srow.addWidget(online)
        lay.addLayout(srow)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.t_updates = Tile("system-software-update", _("System updates"), GREEN)
        self.t_updates.clicked.connect(lambda: ctx.go("updates"))
        run = button(_("Run my routine"), "media-playback-start",
                     lambda: ctx.go("updates", run_routine=True), primary=True)
        self.t_updates.extra.addWidget(run)
        self.t_updates.extra.addStretch(1)
        self.t_flatpak = Tile("flatpak-discover", _("Flatpak apps"), BLUE)
        self.t_flatpak.clicked.connect(lambda: ctx.go("flatpak"))
        self.t_web = Tile("internet-web-browser", _("Programs from the web"), ORANGE)
        self.t_web.clicked.connect(lambda: ctx.go("websources"))
        self.t_restart = Tile("system-reboot", _("Restart"), PURPLE)
        self.t_restart.clicked.connect(lambda: ctx.go("maintenance"))
        self.t_repos = Tile("repository", _("Repositories"), BLUE)
        self.t_repos.clicked.connect(lambda: ctx.go("repos"))
        self.t_maint = Tile("tools", _("Maintenance"), GREEN)
        self.t_maint.clicked.connect(lambda: ctx.go("maintenance"))
        self.t_maint.set(_("Keep it tidy"), _("Old kernels, cache, broken dependencies…"))
        tiles = [self.t_updates, self.t_flatpak, self.t_web, self.t_restart, self.t_repos, self.t_maint]
        for i, t in enumerate(tiles):
            grid.addWidget(t, i // 3, i % 3)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        lay.addLayout(grid)
        lay.addStretch(1)
        lay.addWidget(muted(_("Every change waits in the queue at the bottom until you apply it. You can always see "
                              "the exact zypper command and what zypper answers.")))

    # ------------------------------------------------ the language of the computer
    def _lang_name(self, code):
        from PySide6.QtCore import QLocale
        name = QLocale(code).nativeLanguageName() or code
        return name[:1].upper() + name[1:]

    def _lang_checked(self, code, err):
        self._lang_code = code if not err else None
        self._lang_refresh()

    def _lang_refresh(self):
        code = self._lang_code
        if not code:
            self.lang_note.hide()
            return
        name = self._lang_name(code)
        queued = any(a.target == code for a in self.ctx.queue.of("locale-add"))
        if queued:
            self.lang_text.setText(_("{language} is in the queue: apply it to install the translations of your "
                                     "programs.").format(language=name))
            self.lang_btn.hide()
        else:
            self.lang_text.setText(_("This computer is in {language}, but zypper was never asked for it, so the "
                                     "translations of your programs into {language} are not installed and parts of "
                                     "them show in English.").format(language=name))
            self.lang_btn.setText(_("Add {language}").format(language=name))
            self.lang_btn.show()
        self.lang_note.show()

    def _add_language(self):
        if self._lang_code:
            self.ctx.queue.add(Action("locale-add", self._lang_code))

    def _search_online(self):
        try:
            from .findrepo import FindRepoDialog
        except ImportError:
            return
        dlg = FindRepoDialog(self.ctx, term=self.search.text().strip(), parent=self)
        dlg.exec()
        dlg.deleteLater()

    def _search(self):
        text = self.search.text().strip()
        if not text:
            return
        page = self.ctx.pages["packages"]
        self.ctx.go("packages")
        page.term.setText(text)
        page.search()

    # ------------------------------------------------------------ data
    def reload(self):
        run_async(backend.system_info, on_done=self._info)
        run_async(langcheck.missing_system_language, on_done=self._lang_checked)
        # the numbers of updates are the ones of the Updates page (and of its entry in the sidebar): it counts,
        # this page shows them
        updates = self.ctx.pages["updates"]
        if not getattr(self, "_listening", False):
            self._listening = True
            updates.counted.connect(self._counted)
        self._fp_apps = None
        self._fp_updates = None
        updates.load_updates()
        if flatpak.available():
            def fp():
                apps = flatpak.installed()
                return apps, flatpak.eol_apps(apps[:40])
            run_async(fp, on_done=self._flatpak)
        else:
            self.t_flatpak.set(_("Not installed"), _("Install Flatpak from here"))
        sources = [s for s in websources.load() if s.get("type") != "manual"]
        if sources:
            run_async(lambda: [self._check_one(s) for s in sources], on_done=self._web)
        else:
            self.t_web.set(_("None yet"), _("Follow programs you download from GitHub"))

    def _info(self, info, err):
        if err:
            return
        self.product.setText(_("{product} · {arch}").format(product=info["product"] or "openSUSE", arch=info["arch"]))
        if info["needs_reboot"]:
            self.t_restart.set(_("Needed"), _("An update asks to restart the computer"), RED)
        else:
            self.t_restart.set(_("Not needed"), _("Nothing asks to restart the computer"), PURPLE)
        self.t_repos.set(ngettext("{n} enabled", "{n} enabled", info["repos_enabled"]).format(n=info["repos_enabled"]),
                         ngettext("of {total} · {locks} lock", "of {total} · {locks} locks", info["locks"])
                         .format(total=info["repos_total"], locks=info["locks"]))

    def _counted(self, c):
        """What the Updates page found: the same total as its list and its entry in the sidebar."""
        if "flatpak" not in c.get("pending", ()):
            self._fp_updates = c.get("flatpak", 0)
            self._render_flatpak()
        if c.get("busy"):
            return  # the total shows once every part has answered
        n = c.get("total", 0)
        since = _("Since the last refresh. Your routine refreshes first.")
        self.t_updates.setToolTip(since)
        if c.get("errors") and not n:
            self.t_updates.set("?", "; ".join(str(v) for v in c["errors"].values())[:80])
            return
        if not n:
            self.t_updates.set(_("Up to date"), since, GREEN)
            return
        parts = [text.format(n=c[key]) for key, text in (
            ("repo", ngettext("{n} package", "{n} packages", c.get("repo", 0))),
            ("flatpak", ngettext("{n} Flatpak app", "{n} Flatpak apps", c.get("flatpak", 0))),
            ("web", ngettext("{n} program from the web", "{n} programs from the web", c.get("web", 0))),
            ("firmware", ngettext("{n} firmware", "{n} firmware", c.get("firmware", 0)))) if c.get(key)]
        self.t_updates.set(ngettext("{n} update", "{n} updates", n).format(n=n), " · ".join(parts), GREEN)

    def _flatpak(self, res, err):
        if err or not res:
            return
        self._fp_apps = res
        self._render_flatpak()

    def _render_flatpak(self):
        if not getattr(self, "_fp_apps", None) or not flatpak.available():
            return
        apps, eol = self._fp_apps
        n_up = self._fp_updates
        if eol:
            # an app that is no longer maintained gets no security fixes: say it here, not only in Flatpak
            names = ", ".join(a["name"] for a in apps if a["application"] in eol)
            self.t_flatpak.set(ngettext("{n} app", "{n} apps", len(apps)).format(n=len(apps)),
                               ngettext("{n} is no longer maintained: {names}", "{n} are no longer maintained: {names}",
                                        len(eol)).format(n=len(eol), names=names)[:90], BLUE, small_color=ORANGE)
            return
        self.t_flatpak.set(ngettext("{n} app", "{n} apps", len(apps)).format(n=len(apps)),
                           _("Looking…") if n_up is None else
                           ngettext("{n} update waiting", "{n} updates waiting", n_up).format(n=n_up) if n_up
                           else _("All up to date"))

    @staticmethod
    def _check_one(source):
        try:
            found = websources.check(source)
        except Exception:  # a site that does not answer must not hide the others
            found = {}
        found["package"] = source["package"]
        return found

    def _web(self, res, err):
        if err or res is None:
            self.t_web.set("?", str(err or "")[:80])
            return
        web = self.ctx.pages["websources"]
        web.set_badge_from(res)  # the sidebar shows it before Web sources is opened
        newer = [r for r in res if web.counts_as_new(r)]
        if newer:
            self.t_web.set(ngettext("{n} new version", "{n} new versions", len(newer)).format(n=len(newer)),
                           ", ".join(r["package"] for r in newer)[:80], ORANGE)
        else:  # green, as the other tiles say "up to date"; orange is for new versions (visual review 0.5, #20)
            self.t_web.set(_("All up to date"),
                           ngettext("{n} program followed", "{n} programs followed", len(res)).format(n=len(res)),
                           GREEN)
