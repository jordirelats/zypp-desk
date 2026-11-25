# SPDX-License-Identifier: GPL-3.0-or-later
"""Home: a friendly start. A big search field and tiles that say what there is to do."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from .. import backend, flatpak, websources
from ..i18n import _, ngettext
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..ui import Page, button, heading, icon, muted


class Tile(QFrame):
    clicked = Signal()

    def __init__(self, icon_name, title, color, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.big = QLabel("…", self)
        fb = self.big.font()
        fb.setPointSizeF(fb.pointSizeF() * 1.45)
        fb.setBold(True)
        self.big.setFont(fb)
        self.big.setStyleSheet(f"color: {color};")
        self.small = muted("", self)
        v.addWidget(self.title)
        v.addWidget(self.big)
        v.addWidget(self.small)
        self.extra = QHBoxLayout()
        v.addLayout(self.extra)
        v.addStretch(1)
        h.addLayout(v, 1)

    def set(self, big, small="", color=None):
        self.big.setText(big)
        self.small.setText(small)
        if color:
            self.big.setStyleSheet(f"color: {color};")

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


class HomePage(Page):
    key = "home"
    title = _("Home")
    icon_name = "go-home"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)
        hello = heading(_("Hello! What would you like to do?"), 1.7)
        lay.addWidget(hello)
        self.product = muted("", self)
        lay.addWidget(self.product)

        self.search = QLineEdit(self)
        self.search.setObjectName("bigsearch")
        self.search.setPlaceholderText(_("Search programs to install, for example krita or vlc…"))
        self.search.addAction(icon("edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.returnPressed.connect(self._search)
        lay.addWidget(self.search)

        grid = QGridLayout()
        grid.setSpacing(14)
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
        run_async(backend.list_updates, on_done=self._updates)
        if flatpak.available():
            run_async(lambda: (flatpak.installed(), flatpak.updates()), on_done=self._flatpak)
        else:
            self.t_flatpak.set(_("Not installed"), _("Install Flatpak from here"))
        sources = websources.load()
        if sources:
            run_async(lambda: [websources.check(s) for s in sources], on_done=self._web)
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
        self.t_repos.set(_("{n} enabled").format(n=info["repos_enabled"]),
                         ngettext("of {total} · {locks} lock", "of {total} · {locks} locks", info["locks"])
                         .format(total=info["repos_total"], locks=info["locks"]))

    def _updates(self, ups, err):
        if err:
            self.t_updates.set("?", str(err)[:80])
            return
        n = len(ups)
        if n:
            self.t_updates.set(ngettext("{n} update", "{n} updates", n).format(n=n),
                               _("Since the last refresh. Your routine refreshes first."), GREEN)
        else:
            self.t_updates.set(_("Up to date"), _("Since the last refresh. Your routine refreshes first."), GREEN)

    def _flatpak(self, res, err):
        if err or not res:
            return
        apps, ups = res
        n_up = len([u for u in ups if u["application"] in {a["application"] for a in apps}])
        self.t_flatpak.set(ngettext("{n} app", "{n} apps", len(apps)).format(n=len(apps)),
                           ngettext("{n} update waiting", "{n} updates waiting", n_up).format(n=n_up) if n_up
                           else _("All up to date"))

    def _web(self, res, err):
        if err or res is None:
            self.t_web.set("?", str(err or "")[:80])
            return
        newer = [r for r in res if r.get("newer")]
        if newer:
            self.t_web.set(ngettext("{n} new version", "{n} new versions", len(newer)).format(n=len(newer)),
                           ", ".join(r["asset"] or "" for r in newer)[:80], ORANGE)
        else:
            self.t_web.set(_("All up to date"),
                           ngettext("{n} program followed", "{n} programs followed", len(res)).format(n=len(res)))
