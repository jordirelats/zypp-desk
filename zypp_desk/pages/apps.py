# SPDX-License-Identifier: GPL-3.0-or-later
"""Applications: what is installed and where it comes from, and programs to discover by theme.

Two tabs:
- My applications: every installed program (RPM and Flatpak) with its origin: a repository, Flatpak, or
  a file downloaded from the web. Filters by origin, updates waiting, and the actions of each program.
- Discover: the catalogues of the openSUSE repositories and of Flathub, by theme (the freedesktop
  categories), with installing through the queue.
"""

import html
import re
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit,
                               QListWidget, QListWidgetItem, QScrollArea, QStackedWidget, QStyle,
                               QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget)

from .. import apps, appicons
from ..i18n import _, ngettext
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Chip, Page, button, heading, icon, muted
from .home import Tile

TEAL = "#16a085"
PINK = "#e93a9a"
SLATE = "#5d6d7e"
YELLOW = "#f39c12"

# freedesktop main categories: label, icon, colour
CATEGORIES = {
    "AudioVideo": (_("Music and video"), "applications-multimedia", ORANGE),
    "Graphics": (_("Graphics and photos"), "applications-graphics", PINK),
    "Office": (_("Office"), "applications-office", GREEN),
    "Network": (_("Internet"), "applications-internet", BLUE),
    "Game": (_("Games"), "applications-games", RED),
    "Education": (_("Education"), "applications-education", PURPLE),
    "Science": (_("Science"), "applications-science", TEAL),
    "Development": (_("Programming"), "applications-development", SLATE),
    "Utility": (_("Utilities"), "applications-utilities", YELLOW),
    "System": (_("System"), "applications-system", SLATE),
    "Settings": (_("Settings"), "preferences-system", BLUE),
}

# the commonest additional categories, as people say them
SUBCATEGORIES = {
    "Audio": _("Audio"), "Video": _("Video"), "Player": _("Players"), "Recorder": _("Recording"),
    "AudioVideoEditing": _("Audio and video editing"), "Music": _("Music"), "Midi": _("MIDI"), "Mixer": _("Mixers"),
    "TV": _("TV"), "Sequencer": _("Sequencers"),
    "2DGraphics": _("2D graphics"), "3DGraphics": _("3D graphics"), "RasterGraphics": _("Painting and photo editing"),
    "VectorGraphics": _("Vector drawing"), "Photography": _("Photography"), "Scanning": _("Scanning"),
    "Viewer": _("Viewers"), "Publishing": _("Publishing"), "OCR": _("Text recognition"),
    "WebBrowser": _("Web browsers"), "Email": _("Email"), "InstantMessaging": _("Messaging"), "Chat": _("Chat"),
    "FileTransfer": _("File transfer"), "P2P": _("Peer to peer"), "RemoteAccess": _("Remote access"),
    "News": _("News"), "Feed": _("Feeds"), "Telephony": _("Telephony"), "VideoConference": _("Video calls"),
    "WordProcessor": _("Word processors"), "Spreadsheet": _("Spreadsheets"), "Presentation": _("Presentations"),
    "Calendar": _("Calendars"), "ContactManagement": _("Contacts"), "Finance": _("Finance"),
    "Database": _("Databases"), "Dictionary": _("Dictionaries"), "ProjectManagement": _("Project management"),
    "IDE": _("Development environments"), "Debugger": _("Debuggers"), "RevisionControl": _("Version control"),
    "WebDevelopment": _("Web development"), "TextEditor": _("Text editors"), "Translation": _("Translation"),
    "ActionGame": _("Action"), "AdventureGame": _("Adventure"), "ArcadeGame": _("Arcade"),
    "BoardGame": _("Board games"), "BlocksGame": _("Blocks"), "CardGame": _("Card games"), "KidsGame": _("For children"),
    "LogicGame": _("Logic"), "RolePlaying": _("Role playing"), "Shooter": _("Shooters"),
    "Simulation": _("Simulation"), "SportsGame": _("Sports"), "StrategyGame": _("Strategy"),
    "Emulator": _("Emulators"),
    "Math": _("Mathematics"), "Astronomy": _("Astronomy"), "Chemistry": _("Chemistry"), "Physics": _("Physics"),
    "Biology": _("Biology"), "Geography": _("Geography"), "Languages": _("Languages"),
    "Electronics": _("Electronics"), "Engineering": _("Engineering"), "MedicalSoftware": _("Medicine"),
    "DataVisualization": _("Data visualization"),
    "TerminalEmulator": _("Terminals"), "FileManager": _("File managers"), "Monitor": _("Monitoring"),
    "Security": _("Security"), "FileTools": _("File tools"), "Archiving": _("Archives"),
    "Compression": _("Compression"), "Calculator": _("Calculators"), "Clock": _("Clocks"),
    "Accessibility": _("Accessibility"), "Maps": _("Maps"), "DesktopSettings": _("Desktop"),
    "HardwareSettings": _("Hardware"), "Printing": _("Printing"), "PackageManager": _("Package managers"),
}

# the additional categories that make sense inside each theme (freedesktop's "related categories")
RELATED = {
    "AudioVideo": ("Audio", "Video", "Player", "Recorder", "AudioVideoEditing", "Music", "Midi", "Mixer", "TV",
                   "Sequencer"),
    "Graphics": ("2DGraphics", "3DGraphics", "RasterGraphics", "VectorGraphics", "Photography", "Scanning", "OCR",
                 "Viewer", "Publishing"),
    "Network": ("WebBrowser", "Email", "InstantMessaging", "Chat", "FileTransfer", "P2P", "RemoteAccess", "News",
                "Feed", "Telephony", "VideoConference"),
    "Office": ("WordProcessor", "Spreadsheet", "Presentation", "Calendar", "ContactManagement", "Finance",
               "Database", "Dictionary", "ProjectManagement", "Viewer", "Publishing"),
    "Game": ("ActionGame", "AdventureGame", "ArcadeGame", "BoardGame", "BlocksGame", "CardGame", "KidsGame",
             "LogicGame", "RolePlaying", "Shooter", "Simulation", "SportsGame", "StrategyGame", "Emulator"),
    "Education": ("Math", "Astronomy", "Chemistry", "Physics", "Biology", "Geography", "Languages", "Electronics",
                  "Engineering", "MedicalSoftware", "DataVisualization"),
    "Science": ("Math", "Astronomy", "Chemistry", "Physics", "Biology", "Geography", "Electronics", "Engineering",
                "MedicalSoftware", "DataVisualization"),
    "Development": ("IDE", "Debugger", "RevisionControl", "WebDevelopment", "TextEditor", "Translation", "Database"),
    "System": ("TerminalEmulator", "FileManager", "Monitor", "Security", "Emulator", "PackageManager", "FileTools"),
    "Utility": ("TextEditor", "Archiving", "Compression", "Calculator", "Clock", "Accessibility", "FileTools", "Maps"),
    "Settings": ("DesktopSettings", "HardwareSettings", "Printing", "Security", "Accessibility"),
}

SEGMENT_STYLE = (
    "QToolButton { border: 1px solid rgba(127, 140, 141, 0.45); padding: 6px 18px; font-weight: 600; }"
    "QToolButton:checked { background: #27ae60; color: white; border-color: #1c9450; }"
    "QToolButton#segleft { border-top-left-radius: 15px; border-bottom-left-radius: 15px; }"
    "QToolButton#segright { border-top-right-radius: 15px; border-bottom-right-radius: 15px; border-left: none; }")


def sub_label(token):
    return SUBCATEGORIES.get(token) or re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", token)


def matches(a, term):
    """A program matches a search: anywhere in its name or package, at the start of a word in its summary."""
    if not term:
        return True
    if term in a["name"].lower() or term in a["package"].lower():
        return True
    return re.search(r"\b" + re.escape(term), (a.get("summary") or "").lower()) is not None


def breakable(text):
    """Escaped for rich text, with places to break long IDs (com.github.Something.Long) at dots."""
    return html.escape(text).replace(".", ".&#8203;").replace("/", "/&#8203;")


def repo_label(name):
    """'repo-oss' → 'openSUSE · oss': openSUSE's own repositories by what people call them."""
    if name.startswith("repo-"):
        return "openSUSE · " + name[5:]
    return name


class FlowLayout(QLayout):
    """Widgets in a row that wraps onto the next line when there is no room (the theme chips)."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _arrange(self, rect, apply):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._spacing, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y()


# ------------------------------------------------------------------ list drawing

class AppDelegate(QStyledItemDelegate):
    """A row: icon, name in bold and summary below, coloured badges on the right."""

    HEIGHT = 58

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(200, self.HEIGHT)

    def paint(self, p, option, index):
        a = index.data(Qt.ItemDataRole.UserRole)
        if not a:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 60))
            p.drawRoundedRect(r, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 24))
            p.drawRoundedRect(r, 10, 10)
        self.page.app_icon(a).paint(p, QRect(r.left() + 10, r.top() + (r.height() - 38) // 2, 38, 38))

        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        sfm = QFontMetrics(small)
        x = r.right() - 8
        p.setFont(small)
        for text, color, filled in reversed(self.page.badges(a)):
            w = sfm.horizontalAdvance(text) + 18
            br = QRect(x - w, r.center().y() - 11, w, 22)
            c = QColor(color)
            if filled:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(br, 11, 11)
                p.setPen(QColor("white"))
            else:
                bg = QColor(c)
                bg.setAlpha(40)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(br, 11, 11)
                p.setPen(c.darker(115) if option.palette.color(QPalette.ColorRole.Base).lightness() > 128
                         else c.lighter(130))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, text)
            x -= w + 6
        version = a.get("version") or ""
        if version:
            pfm = p.fontMetrics()
            vw = min(pfm.horizontalAdvance(version) + 6, 160)
            p.setPen(option.palette.color(QPalette.ColorRole.PlaceholderText))
            p.drawText(QRect(x - vw, r.top(), vw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       pfm.elidedText(version, Qt.TextElideMode.ElideMiddle, vw))
            x -= vw + 10

        tx = r.left() + 60
        tw = max(40, x - tx - 6)
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(option.palette.color(QPalette.ColorRole.Text))
        bfm = QFontMetrics(bold)
        p.drawText(QRect(tx, r.top() + 7, tw, bfm.height()), Qt.AlignmentFlag.AlignLeft,
                   bfm.elidedText(a["name"], Qt.TextElideMode.ElideRight, tw))
        p.setFont(option.font)
        p.setPen(option.palette.color(QPalette.ColorRole.PlaceholderText))
        fm = QFontMetrics(option.font)
        summary = a.get("summary") or a["package"]
        p.drawText(QRect(tx, r.top() + 9 + bfm.height(), tw, fm.height()), Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(summary, Qt.TextElideMode.ElideRight, tw))
        p.restore()


def app_list(page):
    lst = QListWidget(page)
    lst.setItemDelegate(AppDelegate(page))
    lst.setMouseTracking(True)
    lst.setFrameShape(QFrame.Shape.NoFrame)
    lst.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
    lst.setUniformItemSizes(True)
    lst.setStyleSheet("QListWidget { background: palette(base); border-radius: 12px; }"
                      "QListWidget::item { border: none; }")
    return lst


class Details(QFrame):
    """The card on the right: the selected program and what can be done with it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedWidth(330)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        self.icon = QLabel(self)
        self.icon.setFixedSize(64, 64)
        self.name = heading("", 1.3, self)
        self.name.setWordWrap(True)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.info = QLabel(self)
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.note = muted("", self)
        self.buttons = QVBoxLayout()
        self.buttons.setSpacing(6)
        lay.addWidget(self.icon)
        lay.addWidget(self.name)
        lay.addWidget(self.summary)
        lay.addSpacing(4)
        lay.addWidget(self.info)
        lay.addWidget(self.note)
        lay.addSpacing(4)
        lay.addLayout(self.buttons)
        lay.addStretch(1)
        self.clear(_("Choose a program from the list."))

    def clear(self, text=""):
        self.icon.clear()
        self.name.setText("")
        self.summary.setText(text)
        self.info.setText("")
        self.note.setText("")
        self.set_buttons([])

    def set_buttons(self, buttons):
        while self.buttons.count():
            w = self.buttons.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        for b in buttons:
            self.buttons.addWidget(b)

    def show_app(self, ic, name, summary, rows, note=""):
        self.icon.setPixmap(ic.pixmap(64, 64))
        self.name.setText(name)
        self.summary.setText(summary)
        self.info.setText("<table width='100%' cellspacing='0' cellpadding='2'>" + "".join(
            f"<tr><td style='color:gray; padding-right:10px' valign='top'>{html.escape(k)}</td><td>{v}</td></tr>"
            for k, v in rows if v) + "</table>")
        self.note.setText(note)


# ------------------------------------------------------------------ the page

class AppsPage(Page):
    key = "apps"
    title = _("Applications")
    icon_name = "applications-all"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.installed = []
        self.updates = {"rpm": {}, "flatpak": {}}
        self.catalogue = []
        self.rpm_names, self.flatpak_ids = set(), set()
        self.catalogue_loading = False
        self.stale = False
        self._icons = {}
        self.category = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 6)
        lay.setSpacing(10)

        # segmented switch between the two tabs
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.seg_group = QButtonGroup(self)
        self.b_mine = QToolButton(self)
        self.b_mine.setObjectName("segleft")
        self.b_mine.setText(_("My applications"))
        self.b_discover = QToolButton(self)
        self.b_discover.setObjectName("segright")
        self.b_discover.setText(_("Discover by theme"))
        for i, b in enumerate((self.b_mine, self.b_discover)):
            b.setCheckable(True)
            b.setStyleSheet(SEGMENT_STYLE)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.seg_group.addButton(b, i)
            seg.addWidget(b)
        self.b_mine.setChecked(True)
        seg.addStretch(1)
        lay.addLayout(seg)

        self.tabs = QStackedWidget(self)
        self.tabs.addWidget(self._build_mine())
        self.tabs.addWidget(self._build_discover())
        lay.addWidget(self.tabs, 1)
        self.seg_group.idClicked.connect(self._switch)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._discover_search)

        ctx.queue.changed.connect(self._queue_changed)
        # after a run the main window reloads the page in front and marks the others to load again

    # ------------------------------------------------------------ building
    def _build_mine(self):
        w = QWidget(self)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.chips = {}
        for key in ("all", "repo", "flatpak", "web", "updates"):
            c = Chip("", key == "all", w)
            self.filter_group.addButton(c)
            c.toggled.connect(lambda on, k=key: on and self._fill_mine())
            self.chips[key] = c
            top.addWidget(c)
        top.addStretch(1)
        self.mine_search = QLineEdit(w)
        self.mine_search.setPlaceholderText(_("Filter by name…"))
        self.mine_search.setClearButtonEnabled(True)
        self.mine_search.setMinimumWidth(240)
        self.mine_search.textChanged.connect(lambda _t: self._fill_mine())
        top.addWidget(self.mine_search)
        v.addLayout(top)
        self._chip_texts()
        self.mine_hint = muted(_("Looking at what is installed…"), w)
        v.addWidget(self.mine_hint)
        body = QHBoxLayout()
        body.setSpacing(12)
        self.mine_list = app_list(self)
        self.mine_list.currentItemChanged.connect(lambda cur, _p: self._show_installed(cur))
        self.mine_details = Details(w)
        body.addWidget(self.mine_list, 1)
        body.addWidget(self.mine_details)
        v.addLayout(body, 1)
        return w

    def _build_discover(self):
        self.discover = QStackedWidget(self)
        # the themes
        grid_page = QWidget(self)
        gv = QVBoxLayout(grid_page)
        gv.setContentsMargins(0, 0, 0, 0)
        self.disc_search = QLineEdit(grid_page)
        self.disc_search.setObjectName("bigsearch")
        self.disc_search.setPlaceholderText(_("Search every program, for example a photo editor…"))
        self.disc_search.addAction(icon("edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.disc_search.setClearButtonEnabled(True)
        self.disc_search.textChanged.connect(lambda _t: self._search_timer.start())
        gv.addWidget(self.disc_search)
        self.disc_hint = muted("", grid_page)
        gv.addWidget(self.disc_hint)
        scroll = QScrollArea(grid_page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(scroll)
        self.grid = QGridLayout(inner)
        self.grid.setSpacing(12)
        self.grid.setContentsMargins(0, 4, 0, 4)
        self.tiles = {}
        for i, (cat, (label, icon_name, color)) in enumerate(CATEGORIES.items()):
            t = Tile(icon_name, label, color, inner)
            t.set("…", "")
            t.small.setWordWrap(True)
            t.clicked.connect(lambda c=cat: self.open_category(c))
            self.grid.addWidget(t, i // 4, i % 4)
            self.tiles[cat] = t
        for c in range(4):
            self.grid.setColumnStretch(c, 1)
        self.grid.setRowStretch(3, 1)
        scroll.setWidget(inner)
        gv.addWidget(scroll, 1)
        self.discover.addWidget(grid_page)

        # the programs of one theme (or of a search)
        list_page = QWidget(self)
        lv = QVBoxLayout(list_page)
        lv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(button(_("All themes"), "go-previous", self.back_to_themes))
        self.cat_icon = QLabel(list_page)
        self.cat_title = heading("", 1.25, list_page)
        head.addSpacing(8)
        head.addWidget(self.cat_icon)
        head.addWidget(self.cat_title)
        head.addStretch(1)
        self.src_group = QButtonGroup(self)
        self.src_chips = {}
        for key, label in (("all", _("Every source")), ("rpm", "openSUSE"), ("flatpak", "Flathub")):
            c = Chip(label, key == "all", list_page)
            self.src_group.addButton(c)
            c.toggled.connect(lambda on: on and self._fill_discover())
            self.src_chips[key] = c
            head.addWidget(c)
        self.cat_search = QLineEdit(list_page)
        self.cat_search.setPlaceholderText(_("Filter by name…"))
        self.cat_search.setClearButtonEnabled(True)
        self.cat_search.setMinimumWidth(220)
        self._cat_timer = QTimer(self)  # wait for the typing to pause: refilling thousands of rows per key is slow
        self._cat_timer.setSingleShot(True)
        self._cat_timer.setInterval(250)
        self._cat_timer.timeout.connect(self._fill_discover)
        self.cat_search.textChanged.connect(lambda _t: self._cat_timer.start())
        head.addWidget(self.cat_search)
        lv.addLayout(head)
        self.sub_box = QWidget(list_page)
        self.sub_row = FlowLayout(self.sub_box)
        self.sub_group = QButtonGroup(self)
        lv.addWidget(self.sub_box)
        self.disc_count = muted("", list_page)
        lv.addWidget(self.disc_count)
        body = QHBoxLayout()
        body.setSpacing(12)
        self.disc_list = app_list(self)
        self.disc_list.currentItemChanged.connect(lambda cur, _p: self._show_catalogue(cur))
        self.disc_details = Details(list_page)
        body.addWidget(self.disc_list, 1)
        body.addWidget(self.disc_details)
        lv.addLayout(body, 1)
        self.discover.addWidget(list_page)
        return self.discover

    def _switch(self, i):
        self.tabs.setCurrentIndex(i)
        if i == 1:
            self._load_catalogue()

    def show_discover(self):
        self.b_discover.setChecked(True)
        self._switch(1)

    # ------------------------------------------------------------ loading
    def reload(self):
        self.stale = False
        self.mine_hint.setText(_("Looking at what is installed…"))
        self.mine_hint.show()
        if not self.installed and self.mine_list.count() == 0:
            # the first look takes a while: say so where the list will be, not only in a small grey line
            wait = QListWidgetItem(icon("view-refresh"), "   " + _("Looking at what is installed… it takes a few "
                                                                  "seconds the first time."))
            wait.setFlags(Qt.ItemFlag.NoItemFlags)
            self.mine_list.addItem(wait)
        run_async(apps.installed, on_done=self._installed_loaded)
        run_async(apps.updates, on_done=self._updates_loaded)
        run_async(apps.web_updates, on_done=self._web_updates_loaded)
        if self.catalogue:
            run_async(apps.installed_names, on_done=self._names_loaded)

    def activated(self):
        if self.stale and self.loaded:
            self.reload()
        super().activated()

    def _installed_loaded(self, res, err):
        if err:
            self.mine_hint.setText(_("Could not read the installed programs: {error}").format(error=err))
            return
        self.installed = res or []
        self.mine_hint.hide()
        self._chip_texts()
        self._fill_mine()

    def _updates_loaded(self, res, err):
        if res:
            self.updates.update(res)
        self._chip_texts()
        self.mine_list.viewport().update()
        self._show_installed(self.mine_list.currentItem())

    def _web_updates_loaded(self, res, err):
        if res is not None:
            self.updates["web"] = res
            self._chip_texts()
            self.mine_list.viewport().update()
            self._show_installed(self.mine_list.currentItem())

    def _load_catalogue(self):
        if self.catalogue or self.catalogue_loading:
            return
        self.catalogue_loading = True
        self.disc_hint.setText(_("Reading the catalogues of the repositories and of Flathub…"))
        run_async(apps.catalogue, on_done=self._catalogue_loaded)
        run_async(apps.installed_names, on_done=self._names_loaded)

    def _catalogue_loaded(self, res, err):
        self.catalogue_loading = False
        if err:
            self.disc_hint.setText(_("Could not read the catalogues: {error}").format(error=err))
            return
        self.catalogue = res or []
        for a in self.catalogue:
            a["catalogue"] = True
        n_rpm = sum(1 for a in self.catalogue if a["kind"] == "rpm")
        self.disc_hint.setText(ngettext("{n} program to discover", "{n} programs to discover", len(self.catalogue))
                               .format(n=len(self.catalogue)) + " · " +
                               _("{rpm} from the openSUSE repositories, {fp} from Flathub").format(
                                   rpm=n_rpm, fp=len(self.catalogue) - n_rpm))
        self._fill_tiles()
        if self.discover.currentIndex() == 1:
            self._fill_discover()

    def _names_loaded(self, res, err):
        if res:
            self.rpm_names, self.flatpak_ids = res
            self._fill_tiles()
            self.disc_list.viewport().update()
            self._show_catalogue(self.disc_list.currentItem())

    # ------------------------------------------------------------ helpers
    def app_icon(self, a):
        key = (a["kind"], a.get("icon") or "", a["package"])
        ic = self._icons.get(key)
        if ic is not None:
            return ic
        name = a.get("icon") or ""
        ic = QIcon()
        if name.startswith("/"):
            if Path(name).is_file():
                ic = QIcon(name)
        elif name:
            ic = QIcon.fromTheme(name)
        if ic.isNull() and a["kind"] == "rpm":
            found = self.ctx.icon_catalog.get(a["package"])
            if found and found[0] == "file":
                ic = QIcon(found[1])
            elif found:
                ic = QIcon.fromTheme(found[1])
        if ic.isNull() and a["kind"] == "flatpak":
            f = appicons.flatpak_icon_file(a["package"])
            if f:
                ic = QIcon(f)
        if ic.isNull():
            ic = icon("application-x-executable", "package-x-generic")
        self._icons[key] = ic
        return ic

    def update_of(self, a):
        """The new version waiting for an installed program, or None."""
        if a["kind"] == "flatpak":
            return self.updates["flatpak"].get(a["package"]) or None
        if a["origin"] == "web":
            f = getattr(self.ctx.pages.get("websources"), "found", {}).get(a["package"]) or {}
            if f.get("newer"):
                return f.get("version")
            return self.updates.get("web", {}).get(a["package"])
        return self.updates["rpm"].get(a["package"])

    def queue_state(self, a):
        if a["kind"] == "flatpak":
            return self.ctx.queue.flatpak_state(a["package"])
        return self.ctx.queue.state_of(a["package"])

    def is_installed(self, a):
        return a["package"] in (self.flatpak_ids if a["kind"] == "flatpak" else self.rpm_names)

    def origin_badge(self, a):
        if a.get("catalogue"):
            return ("openSUSE", GREEN) if a["kind"] == "rpm" else (a["origin_name"], PURPLE)
        if a["origin"] == "flatpak":
            return (_("Flatpak · {remote}").format(remote=a["origin_name"].capitalize()
                                                   if a["origin_name"] == "flathub" else a["origin_name"]), PURPLE)
        if a["origin"] == "web":
            if a["web_kind"] in ("github", "url", "page") and a["origin_name"]:
                return _("Web · {site}").format(site=a["origin_name"]), ORANGE
            return _("Local file"), ORANGE
        return (repo_label(a["origin_name"]), BLUE)

    def badges(self, a):
        out = []
        state = self.queue_state(a)
        if state:
            out.append((_("in the queue"), BLUE, True))
        if a.get("catalogue"):
            if self.is_installed(a):
                out.append((_("installed"), GREEN, False))
        elif self.update_of(a):
            out.append((_("update"), GREEN, True))
        text, color = self.origin_badge(a)
        out.append((text if len(text) <= 34 else text[:32] + "…", color, False))
        return out

    def _queue_changed(self):
        self.mine_list.viewport().update()
        self.disc_list.viewport().update()
        if self.tabs.currentIndex() == 0:
            self._show_installed(self.mine_list.currentItem())
        else:
            self._show_catalogue(self.disc_list.currentItem())

    # ------------------------------------------------------------ my applications
    def _origin_count(self, key):
        if key == "all":
            return len(self.installed)
        if key == "updates":
            return sum(1 for a in self.installed if self.update_of(a))
        return sum(1 for a in self.installed if a["origin"] == key)

    def _chip_texts(self):
        labels = {"all": _("All"), "repo": _("Repositories"), "flatpak": _("Flatpak"), "web": _("From the web"),
                  "updates": _("With updates")}
        for key, c in self.chips.items():
            c.setText(f"{labels[key]}  {self._origin_count(key)}" if self.installed else labels[key])

    def _fill_mine(self):
        key = next((k for k, c in self.chips.items() if c.isChecked()), "all")
        term = self.mine_search.text().strip().lower()
        cur = self.mine_list.currentItem()
        cur_id = cur.data(Qt.ItemDataRole.UserRole)["id"] if cur else None
        self.mine_list.clear()
        for a in self.installed:
            if key == "updates" and not self.update_of(a):
                continue
            if key not in ("all", "updates") and a["origin"] != key:
                continue
            if not matches(a, term):
                continue
            it = QListWidgetItem(self.mine_list)
            it.setData(Qt.ItemDataRole.UserRole, a)
            it.setToolTip(a["package"])
            if a["id"] == cur_id:
                self.mine_list.setCurrentItem(it)
        if self.mine_list.count() and self.mine_list.currentItem() is None:
            self.mine_list.setCurrentRow(0)
        if not self.mine_list.count():
            self.mine_details.clear(_("Nothing here.") if self.installed else "")

    def _show_installed(self, item):
        a = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not a:
            return
        new = self.update_of(a)
        state = self.queue_state(a)
        origin_text, _c = self.origin_badge(a)
        if a["kind"] == "flatpak":
            rows = [(_("Origin"), html.escape(_("Flatpak, from the remote {remote}").format(remote=a["origin_name"]))),
                    (_("App ID"), breakable(a["package"])),
                    (_("Installation"), html.escape(_("for every user") if a["installation"] != "user"
                                                    else _("only for you")))]
        elif a["origin"] == "web":
            rows = [(_("Origin"), html.escape(_("a file downloaded from the web, installed from a local folder"))),
                    (_("Package"), html.escape(a["package"]))]
            if a["web_kind"] in ("github", "url", "page") and a["origin_name"]:
                rows.append((_("New versions from"), breakable(a["origin_name"])))
        else:
            rows = [(_("Origin"), html.escape(_("the repository {name}").format(name=a["origin_name"]))),
                    (_("Package"), html.escape(a["package"]))]
        rows.append((_("Version"), html.escape(a["version"] or "")))
        if new:
            rows.append((_("New version"), f"<b style='color:{GREEN}'>{html.escape(new)}</b>"))
        note = ""
        if a["origin"] == "web" and a["web_kind"] == "manual":
            note = a["web_note"] or _("Not followed in Web sources.")
        elif a["origin"] == "web" and not a["web_kind"]:
            note = _("Nobody looks for new versions of this program. In Web sources you can say where it is "
                     "published.")
        elif a["origin"] == "web":
            note = _("Web sources looks for new versions of this program.")
        if state:
            note = (note + "\n\n" if note else "") + _("In the queue: {change}.").format(change=self._state_text(state))
        self.mine_details.show_app(self.app_icon(a), a["name"], a["summary"] or "", rows, note)

        btns = []
        if new and a["origin"] == "web":
            btns.append(button(_("Get the new version…"), "download",
                               lambda _c=False, pkg=a["package"]: self.ctx.pages["websources"].get_new_versions([pkg]),
                               primary=True))
        elif new and not state:
            btns.append(button(_("Update"), "system-software-update", lambda: self._queue_update(a), primary=True))
        if state:
            btns.append(button(_("Drop from the queue"), "edit-undo", lambda: self._drop(a)))
        else:
            btns.append(button(_("Remove") if a["kind"] == "rpm" else _("Uninstall"), "edit-delete",
                               lambda: self._queue_remove(a)))
        if a["origin"] == "web":
            btns.append(button(_("Open Web sources"), "internet-web-browser", lambda: self.ctx.go("websources")))
        if a["kind"] == "flatpak":
            btns.append(button(_("Show in Flatpak"), "flatpak-discover", lambda: self.ctx.go("flatpak")))
        else:
            btns.append(button(_("Show in Packages"), "package-x-generic", lambda: self._show_in_packages(a)))
        self.mine_details.set_buttons(btns)

    @staticmethod
    def _state_text(state):
        return {"install": _("install or update"), "remove": _("remove"), "flatpak-install": _("install"),
                "flatpak-uninstall": _("uninstall"), "flatpak-update": _("update")}.get(state, state)

    def _queue_update(self, a):
        if a["kind"] == "flatpak":
            self.ctx.queue.add(Action("flatpak-update", a["package"]))
        else:
            self.ctx.queue.add(Action("install", a["package"]))

    def _queue_remove(self, a):
        if a["kind"] == "flatpak":
            self.ctx.queue.add(Action("flatpak-uninstall", a["package"], installation=a.get("installation") or "system",
                                      delete_data=False))
        else:
            self.ctx.queue.add(Action("remove", a["package"]))

    def _drop(self, a):
        """Take this program's install, update or removal out of the queue (locks and the rest stay)."""
        q = self.ctx.queue
        with q.batch():
            for act in list(q.actions):
                if a["kind"] == "flatpak":
                    # "update every Flatpak app" (target *) also covers this one
                    same = act.kind in ("flatpak-install", "flatpak-uninstall", "flatpak-update") and \
                        act.target in (a["package"], "*")
                else:
                    name = act.opts.get("package") or act.target.split("=")[0]
                    same = act.kind in ("install", "remove") and (name == a["package"] or
                                                                   name.endswith(":" + a["package"]))
                if same:
                    q.discard_action(act)

    def _show_in_packages(self, a):
        page = self.ctx.pages["packages"]
        self.ctx.go("packages")
        page.term.setText(a["package"])
        page.search()

    # ------------------------------------------------------------ discover
    def _fill_tiles(self):
        if not self.catalogue:
            return
        by_cat = {c: [] for c in CATEGORIES}
        for a in self.catalogue:
            for m in apps.main_categories(a):
                by_cat[m].append(a)
        for cat, items in by_cat.items():
            t = self.tiles[cat]
            mine = [a["name"] for a in items if self.is_installed(a)]
            examples = mine[:3] if mine else [a["name"] for a in items if a["kind"] == "rpm"][:3]
            small = (_("You have: {names}") if mine else _("For example: {names}")).format(
                names=", ".join(examples)) if examples else ""
            t.set(ngettext("{n} program", "{n} programs", len(items)).format(n=len(items)), small)

    def open_category(self, cat):
        self.category = cat
        self.disc_search.blockSignals(True)
        self.disc_search.clear()
        self.disc_search.blockSignals(False)
        self._open_list(cat)

    def _discover_search(self):
        text = self.disc_search.text().strip()
        if len(text) < 2:
            return
        self.category = None
        self._open_list(None, text)
        self.disc_search.blockSignals(True)
        self.disc_search.clear()
        self.disc_search.blockSignals(False)

    def _open_list(self, cat, text=""):
        if cat:
            label, icon_name, _color = CATEGORIES[cat]
            self.cat_title.setText(label)
            self.cat_icon.setPixmap(icon(icon_name, "package-x-generic").pixmap(28, 28))
        else:
            self.cat_title.setText(_("Search in every theme"))
            self.cat_icon.setPixmap(icon("edit-find").pixmap(28, 28))
        self._build_sub_chips(cat)
        self.cat_search.blockSignals(True)
        self.cat_search.setText(text)
        self.cat_search.blockSignals(False)
        self.discover.setCurrentIndex(1)
        self._fill_discover()
        if text:
            self.cat_search.setFocus()
            self.cat_search.setCursorPosition(len(text))

    def back_to_themes(self):
        self.discover.setCurrentIndex(0)
        self.disc_search.setFocus()

    def _build_sub_chips(self, cat):
        for b in self.sub_group.buttons():
            self.sub_group.removeButton(b)
        while self.sub_row.count():
            w = self.sub_row.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.sub_box.setVisible(bool(cat))
        if not cat:
            return
        related = set(RELATED.get(cat, ()))
        counts = {}
        for a in self.catalogue:
            if cat in a["categories"]:
                for s in a["categories"]:
                    if s in related:
                        counts[s] = counts.get(s, 0) + 1
        top = [s for s in counts if counts[s] >= 2]
        first = Chip(_("All"), True, self)
        first.setProperty("sub", "")
        chips = [first]
        for s in sorted(top, key=lambda s: sub_label(s).lower()):
            c = Chip(sub_label(s), False, self)
            c.setProperty("sub", s)
            chips.append(c)
        for c in chips:
            self.sub_group.addButton(c)
            c.toggled.connect(lambda on: on and self._fill_discover())
            self.sub_row.addWidget(c)

    def _fill_discover(self):
        if not self.catalogue:
            self.disc_count.setText(_("Reading the catalogues of the repositories and of Flathub…"))
            return
        cat = self.category
        checked = self.sub_group.checkedButton()
        sub = checked.property("sub") if checked else ""
        src = next((k for k, c in self.src_chips.items() if c.isChecked()), "all")
        term = self.cat_search.text().strip().lower()
        self.disc_list.clear()
        n = 0
        for a in self.catalogue:
            if cat and cat not in a["categories"]:
                continue
            if sub and sub not in a["categories"]:
                continue
            if src != "all" and a["kind"] != src:
                continue
            if not matches(a, term):
                continue
            it = QListWidgetItem(self.disc_list)
            it.setData(Qt.ItemDataRole.UserRole, a)
            it.setToolTip(a["package"])
            n += 1
        self.disc_count.setText(ngettext("{n} program", "{n} programs", n).format(n=n))
        if n:
            self.disc_list.setCurrentRow(0)
        else:
            self.disc_details.clear(_("Nothing found. Try another word, or every source."))

    def _show_catalogue(self, item):
        a = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not a:
            return
        installed = self.is_installed(a)
        state = self.queue_state(a)
        cats = [CATEGORIES[m][0] for m in apps.main_categories(a)] + \
            [sub_label(s) for s in apps.sub_categories(a)][:4]
        if a["kind"] == "rpm":
            rows = [(_("Comes from"), html.escape(_("the openSUSE repositories"))),
                    (_("Package"), html.escape(a["package"]))]
        else:
            rows = [(_("Comes from"), html.escape(_("Flatpak, from the remote {remote}").format(remote=a["remote"]))),
                    (_("App ID"), breakable(a["package"]))]
        rows.append((_("Themes"), html.escape(", ".join(dict.fromkeys(cats)))))
        rows.append((_("Status"), f"<b style='color:{GREEN}'>{html.escape(_('installed'))}</b>" if installed
                     else html.escape(_("not installed"))))
        note = ""
        if a["kind"] == "flatpak":
            note = _("Flatpak apps run isolated from the system and bring what they need with them; they take more "
                     "space and are updated apart from zypper.")
        if state:
            note = (note + "\n\n" if note else "") + _("In the queue: {change}.").format(change=self._state_text(state))
        self.disc_details.show_app(self.app_icon(a), a["name"], a["summary"] or "", rows, note)
        btns = []
        if state:
            btns.append(button(_("Drop from the queue"), "edit-undo", lambda: self._drop(a)))
        elif installed:
            inst = next((x for x in self.installed if x["package"] == a["package"] and x["kind"] == a["kind"]), None)
            btns.append(button(_("Remove") if a["kind"] == "rpm" else _("Uninstall"), "edit-delete",
                               lambda: self._queue_remove(inst or dict(a, installation="system"))))
        elif a["kind"] == "rpm":
            btns.append(button(_("Install"), "list-add", lambda: self.ctx.queue.add(Action("install", a["package"])),
                               primary=True))
        else:
            btns.append(button(_("Install"), "list-add", lambda: self.ctx.queue.add(
                Action("flatpak-install", a["package"], remote=a["remote"], installation="system")), primary=True))
        if a["kind"] == "rpm":
            btns.append(button(_("Show in Packages"), "package-x-generic", lambda: self._show_in_packages(a)))
        self.disc_details.set_buttons(btns)
