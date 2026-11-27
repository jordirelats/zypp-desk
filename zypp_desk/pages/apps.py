# SPDX-License-Identifier: GPL-3.0-or-later
"""Applications: what is installed and where it comes from, and programs to discover by theme.

Two tabs:
- My applications: every installed program (RPM and Flatpak) with its origin: a repository, Flatpak, or
  a file downloaded from the web; the web apps of the browsers; and the AppImages (appimages.py). Filters by
  origin, updates waiting, and the actions of each program.
- Discover: the catalogues of the openSUSE repositories and of Flathub, by theme (the freedesktop
  categories), with installing through the queue.
"""

import html
import os
import re
import shlex
from pathlib import Path

from PySide6.QtCore import QEvent, QRect, QSize, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QScrollArea, QSizePolicy, QStackedWidget, QStyle,
                               QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget)

from .. import apps, appicons, appimages, webapps
from ..i18n import _, ngettext
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Chip, FlowLayout, Page, button, heading, icon, mono, muted, rich_tip, tool
from .home import Tile
from .markbar import MarkBar, amp, check_rect, is_checked, paint_check, queue_label

MARK_ROLE = Qt.ItemDataRole.UserRole + 7  # True on the rows that can be marked (Discover); QListWidgetItem is
#                                           user-checkable by default, so its flags cannot tell them apart

TEAL = "#16a085"
PINK = "#e93a9a"
SLATE = "#5d6d7e"
YELLOW = "#f39c12"
INDIGO = "#5c6bc0"  # the AppImage pill

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


def host_of(url):
    """'https://github.com/a/b/releases/…' → 'github.com'."""
    from urllib.parse import urlsplit
    return urlsplit(url or "").netloc


def repo_label(name):
    """'repo-oss' → 'openSUSE · oss': openSUSE's own repositories by what people call them."""
    if name.startswith("repo-"):
        return "openSUSE · " + name[5:]
    return name


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
        shift = 0
        if index.data(MARK_ROLE):  # Discover: mark several
            paint_check(p, check_rect(r), is_checked(index.data(Qt.ItemDataRole.CheckStateRole)), option.palette)
            shift = 28
        self.page.app_icon(a).paint(p, QRect(r.left() + 10 + shift, r.top() + (r.height() - 38) // 2, 38, 38))

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
        version = (a.get("version") or "") if a["kind"] != "appimage" else ""  # an AppImage: its file and size
        if version:
            pfm = p.fontMetrics()
            vw = min(pfm.horizontalAdvance(version) + 6, 160)
            p.setPen(option.palette.color(QPalette.ColorRole.PlaceholderText))
            p.drawText(QRect(x - vw, r.top(), vw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       pfm.elidedText(version, Qt.TextElideMode.ElideMiddle, vw))
            x -= vw + 10

        tx = r.left() + 60 + shift
        tw = max(40, x - tx - 6)
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(option.palette.color(QPalette.ColorRole.Text))
        bfm = QFontMetrics(bold)
        p.drawText(QRect(tx, r.top() + 7, tw, bfm.height()), Qt.AlignmentFlag.AlignLeft,
                   bfm.elidedText(a["name"], Qt.TextElideMode.ElideRight, tw))
        line_font = option.font
        if a["kind"] == "appimage":  # the file, as in a terminal: where it is is what tells AppImages apart
            line_font = mono()
            line_font.setPointSizeF(option.font.pointSizeF() * 0.92)
        p.setFont(line_font)
        p.setPen(option.palette.color(QPalette.ColorRole.PlaceholderText))
        fm = QFontMetrics(line_font)
        if a["kind"] == "appimage":
            summary = self.page.appimage_line(a)
        else:
            summary = a.get("summary") or (
                (a.get("host") or _("Web app of {browser}").format(browser=a["origin_name"])) if a["kind"] == "webapp"
                else a["package"])
        p.drawText(QRect(tx, r.top() + 9 + bfm.height(), tw, fm.height()), Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(summary, Qt.TextElideMode.ElideRight, tw))
        p.restore()

    def editorEvent(self, event, model, option, index):
        """A click on the box (or Space on the row) ticks it; a click elsewhere still opens the program's card."""
        if not index.data(MARK_ROLE):
            return False
        toggle = False
        if event.type() == QEvent.Type.MouseButtonRelease and check_rect(option.rect.adjusted(4, 2, -4, -2)) \
                .adjusted(-4, -4, 4, 4).contains(event.position().toPoint()):
            toggle = True
        elif event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            toggle = True
        if toggle:
            on = is_checked(index.data(Qt.ItemDataRole.CheckStateRole))
            model.setData(index, Qt.CheckState.Unchecked if on else Qt.CheckState.Checked,
                          Qt.ItemDataRole.CheckStateRole)
            return True
        return False


class ShortHeading(QLabel):
    """A heading that gives way with "…" when the row is narrow, instead of making the page wider than the
    window (the title of a theme beside its source chips and filter)."""

    def __init__(self, scale, parent=None):
        super().__init__(parent)
        f = self.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * scale)
        self.setFont(f)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self.updateGeometry()
        self._elide()

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(min(self.sizeHint().width(), self.fontMetrics().horizontalAdvance("M" * 4)),
                     super().minimumSizeHint().height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def _elide(self):
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, self.width() - 2))
        if shown != self.text():
            self.setText(shown)


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
        self.info.setTextFormat(Qt.TextFormat.RichText)  # the rows: every value is escaped (html.escape, breakable)
        self.note = muted("", self)
        # names and summaries come from files anyone can write (an AppImage, a .desktop file): never rich text
        for lbl in (self.name, self.summary, self.note):
            lbl.setTextFormat(Qt.TextFormat.PlainText)
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
        grey = self.palette().color(QPalette.ColorRole.PlaceholderText).name()  # light and dark
        self.info.setText("<table width='100%' cellspacing='0' cellpadding='2'>" + "".join(
            f"<tr><td style='color:{grey}; padding-right:10px' valign='top'>{html.escape(k)}</td><td>{v}</td></tr>"
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
        self.webapps = []  # the web apps (PWA) of the browsers (webapps.py)
        self.appimages = []  # the AppImages and the menu entries of AppImages that are gone (appimages.py)
        self._ai_found = {}  # path → what "Look for a new version" found (appimages.check)
        self._ai_errors = {}  # path → why it could not look
        self._ai_busy = {}  # path → "checking" or "downloading"
        self._ai_select = None  # the path to select once the AppImages are read again
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
        self.b_mine.setToolTip(_("Every program you have, and where it comes from"))
        self.b_discover.setToolTip(_("Programs to install, by theme, from the openSUSE repositories and Flathub"))
        for i, b in enumerate((self.b_mine, self.b_discover)):
            b.setCheckable(True)
            b.setStyleSheet(SEGMENT_STYLE)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.seg_group.addButton(b, i)
            seg.addWidget(b)
        self.b_mine.setChecked(True)
        seg.addStretch(1)
        # the filter of My applications sits beside the switch, where there is room: on the row of the chips it
        # made the page wider than a 1100 px window in Catalan and German
        self.mine_search = QLineEdit(self)
        self.mine_search.setPlaceholderText(_("Filter by name…"))
        self.mine_search.setClearButtonEnabled(True)
        self.mine_search.setMinimumWidth(180)
        self.mine_search.setMaximumWidth(320)
        self.mine_search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.mine_search.textChanged.connect(lambda _t: self._fill_mine())
        seg.addWidget(self.mine_search, 1)
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
        tips = {"all": _("Show every program you have"),
                "repo": _("Show only the programs installed from a repository"),
                "flatpak": _("Show only the Flatpak apps"),
                "web": _("Show only the programs installed from files downloaded from the web"),
                "webapp": _("Show only the web apps of your browsers"),
                "appimage": _("Show only the AppImages"),
                "updates": _("Show only the programs with a new version waiting")}
        for key in ("all", "repo", "flatpak", "web", "webapp", "appimage", "updates"):
            c = Chip("", key == "all", w)
            c.setToolTip(tips[key])
            self.filter_group.addButton(c)
            c.toggled.connect(lambda on, k=key: on and self._fill_mine())
            self.chips[key] = c
            top.addWidget(c)
        top.addStretch(1)
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
        back = button(_("Back to all themes"), "go-previous", self.back_to_themes)
        back.setToolTip(_("Show the themes again"))
        head.addWidget(back)
        self.cat_icon = QLabel(list_page)
        self.cat_title = ShortHeading(1.25, list_page)
        head.addSpacing(8)
        head.addWidget(self.cat_icon)
        head.addWidget(self.cat_title, 1)
        self.src_group = QButtonGroup(self)
        self.src_chips = {}
        src_tips = {"all": _("Show programs from every source"),
                    "rpm": _("Show only programs from the openSUSE repositories"),
                    "flatpak": _("Show only Flatpak apps from Flathub")}
        for key, label in (("all", _("Every source")), ("rpm", "openSUSE"), ("flatpak", "Flathub")):
            c = Chip(label, key == "all", list_page)
            c.setToolTip(src_tips[key])
            self.src_group.addButton(c)
            c.toggled.connect(lambda on: on and self._fill_discover())
            self.src_chips[key] = c
            head.addWidget(c)
        self.cat_search = QLineEdit(list_page)
        self.cat_search.setPlaceholderText(_("Filter by name…"))
        self.cat_search.setClearButtonEnabled(True)
        self.cat_search.setMinimumWidth(160)
        self.cat_search.setMaximumWidth(260)
        self.cat_search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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
        # mark several programs and install them at once; the marks survive a new filter
        self.disc_marked = {}  # (kind, package) -> the catalogue entry
        self.disc_markbar = MarkBar(self._disc_mark_action, self._disc_unmark_all, list_page, to_queue=True)
        self.disc_list.itemChanged.connect(self._disc_item_changed)
        left = QWidget(list_page)
        lv2 = QVBoxLayout(left)
        lv2.setContentsMargins(0, 0, 0, 0)
        lv2.setSpacing(6)
        lv2.addWidget(self.disc_list, 1)
        lv2.addWidget(self.disc_markbar)
        body.addWidget(left, 1)
        body.addWidget(self.disc_details)
        lv.addLayout(body, 1)
        self.discover.addWidget(list_page)
        return self.discover

    def _switch(self, i):
        self.tabs.setCurrentIndex(i)
        self.mine_search.setVisible(i == 0)
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
        run_async(webapps.scan, on_done=self._webapps_loaded)
        self._scan_appimages()
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

    def _webapps_loaded(self, res, err):
        self.webapps = res or []
        self._chip_texts()
        self._fill_mine()

    def _mine(self):
        return self.installed + self.webapps + self.appimages

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
        if ic.isNull() and a["kind"] == "webapp" and name:
            # the browser writes the icon of each web app into the user's icon folder
            base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                                "icons", "hicolor")
            for size in ("256x256", "128x128", "64x64", "48x48", "32x32"):
                f = os.path.join(base, size, "apps", f"{name}.png")
                if os.path.isfile(f):
                    ic = QIcon(f)
                    break
        if ic.isNull() and a["kind"] == "flatpak":
            # the icon the installed app exported (an app that left Flathub's catalogue has none there)
            for base in ("/var/lib/flatpak/exports/share/icons/hicolor",
                         os.path.expanduser("~/.local/share/flatpak/exports/share/icons/hicolor")):
                for size in ("scalable", "256x256", "128x128", "64x64", "48x48"):
                    for ext in ("svg", "png"):
                        f = os.path.join(base, size, "apps", f"{a['package']}.{ext}")
                        if os.path.isfile(f):
                            ic = QIcon(f)
                            break
                    if not ic.isNull():
                        break
                if not ic.isNull():
                    break
        if ic.isNull():
            ic = icon("application-x-executable", "package-x-generic")
        self._icons[key] = ic
        return ic

    def update_of(self, a):
        """The new version waiting for an installed program, or None."""
        if a["kind"] == "webapp":
            return None  # the browser updates its web apps by itself
        if a["kind"] == "appimage":  # known only after "Look for a new version": never asked by itself
            f = self._ai_found.get(a["path"]) or {}
            if f.get("newer") or (f.get("newer") is None and f and not f.get("same")):
                return f.get("version") or f.get("asset")
            return None
        if a["kind"] == "flatpak":
            return self.updates["flatpak"].get(a["package"]) or None
        if a["origin"] == "web":
            f = getattr(self.ctx.pages.get("websources"), "found", {}).get(a["package"]) or {}
            if f.get("newer"):
                return f.get("version")
            return self.updates.get("web", {}).get(a["package"])
        return self.updates["rpm"].get(a["package"])

    def queue_state(self, a):
        if a["kind"] == "appimage":
            return None  # AppImages never go through the queue: they are the user's files
        if a["kind"] == "flatpak":
            return self.ctx.queue.flatpak_state(a["package"])
        return self.ctx.queue.state_of(a["package"])

    def is_installed(self, a):
        return a["package"] in (self.flatpak_ids if a["kind"] == "flatpak" else self.rpm_names)

    def origin_badge(self, a):
        if a["kind"] == "webapp":
            return (_("orphan"), ORANGE) if a["orphan"] else (a["origin_name"], a["color"])
        if a["kind"] == "appimage":
            return (_("orphan"), ORANGE) if a["orphan"] else ("AppImage", INDIGO)
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
        if not a.get("catalogue") and self.is_protected(a):
            out.append((_("protected"), PURPLE, True))
        text, color = self.origin_badge(a)
        out.append((text if len(text) <= 34 else text[:32] + "…", color, False))
        return out

    # ------------------------------------------------------------ the buttons of a card (botons/NORMES.md)
    def _named(self, named, plain, name, **kw):
        """A label with the program's name when it fits the card ('Open Signal'), else without it ('Open it'):
        the name is right above, in the card's title."""
        text = named.format(name=amp(name), **kw)
        f = QFont(self.mine_details.font())
        f.setBold(True)  # the green button is bold: measure as if every one were
        if QFontMetrics(f).horizontalAdvance(text.replace("&&", "&")) <= 228:
            return text
        return plain.format(**kw)

    @staticmethod
    def _btn(obj, text, icon_name, slot, tip, primary=False):
        b = button(text, icon_name, slot, primary=primary)
        b.setObjectName(obj)  # tests and other pages find a button by this name, whatever its label says
        b.setToolTip(tip)
        return b

    @staticmethod
    def _queue_cmd(a, kind):
        """The command a queued change of this program becomes, for a tooltip."""
        if a["kind"] == "flatpak":
            inst = a.get("installation") or "system"
            return {"update": f"flatpak update {a['package']}",
                    "remove": f"flatpak uninstall --{inst} {a['package']}",
                    "install": f"flatpak install --system {a.get('remote') or 'flathub'} {a['package']}"}[kind]
        return {"update": f"zypper install {a['package']}", "remove": f"zypper remove {a['package']}",
                "install": f"zypper install {a['package']}"}[kind]

    def _protect_btn(self, a, guarded):
        """Protect (asks first) or stop protecting (at once): only a mark in Zypp Desk, never in the queue."""
        if guarded:
            return self._btn("app_protect", self._named(_("Stop protecting {name}"), _("Stop protecting it"),
                                                        a["name"]),
                             "security-low", lambda: self.toggle_protect(a),
                             _("Zypp Desk may remove it again. Nothing on the system changes, and nothing goes to "
                               "the queue."))
        return self._btn("app_protect", self._named(_("Protect {name}…"), _("Protect it…"), a["name"]),
                         "security-high", lambda: self.toggle_protect(a),
                         _("Asks first, then marks it so that Zypp Desk never removes it, from any page. Nothing on "
                           "the system changes, and nothing goes to the queue."))

    # ------------------------------------------------------------ web apps of the browsers (webapps.py)
    def _show_webapp(self, a):
        rows = [(_("Browser"), html.escape(a["origin_name"] + (f" · {a['profile']}" if a["profile"] else "")))]
        if a["host"]:
            rows.append((_("Web"), html.escape(a["host"])))
        if a["actions"]:
            rows.append((_("In the menu"), html.escape(", ".join(a["actions"]))))
        rows.append((_("Updates"), html.escape(_("done by the browser, by itself"))))
        if a["orphan"]:
            note = _("Its browser is no longer installed ({program}): the entry in the menu does not work any more "
                     "and can be removed.").format(program=a["program"] or "?")
        else:
            note = _("Web apps are installed and updated by their browser. Zypp Desk shows them all together, from "
                     "every browser, opens them and lets the browser remove them.")
        self.mine_details.show_app(self.app_icon(a), a["name"], a["summary"] or "", rows, note)
        btns = []
        name, browser = a["name"], a["origin_name"]
        if a["orphan"]:
            btns.append(self._btn("web_menu_remove", self._named(_("Remove {name} from the menu…"),
                                                                 _("Remove it from the menu…"), name),
                                  "edit-delete", lambda: self._remove_orphan(a),
                                  _("Asks first, then moves its menu entry and icons to the trash at once: its "
                                    "browser is gone. Web apps do not go through the queue.")))
        else:
            words = webapps.open_command(a)
            btns.append(self._btn("web_open", self._named(_("Open {name}"), _("Open it"), name),
                                  "media-playback-start", lambda: self._start(webapps.open_command(a)),
                                  _("Opens it in {browser} now, as its menu entry does ({command}).").format(
                                      browser=browser, command=" ".join(shlex.quote(w) for w in words) or "?"),
                                  primary=True))
            if a["manage"]:
                btns.append(self._btn("web_remove", self._named(_("Remove {name}…"), _("Remove it…"), name),
                                      "edit-delete", lambda: self._remove_webapp(a),
                                      _("Asks first, then opens the page of apps of {browser}, where the browser "
                                        "removes it. Web apps do not go through the queue.").format(browser=browser)))
                btns.append(self._btn("web_manage", _("Manage web apps in {browser}").format(browser=amp(browser)),
                                      "internet-web-browser", lambda: self._start(webapps.manage_command(a)),
                                      _("Opens the page of apps of {browser} ({page}), where it installs, removes "
                                        "and sets up its web apps.").format(browser=browser, page=a["manage"])))
        self.mine_details.set_buttons(btns)

    def _start(self, words):
        from PySide6.QtCore import QProcess
        if words:
            QProcess.startDetached(words[0], words[1:])

    def _remove_webapp(self, a):
        from .. import question
        if question.confirm(self, _("Remove {name}?").format(name=a["name"]),
                            _("Open {browser}").format(browser=a["origin_name"]), icon_name="internet-web-browser",
                            items=[("internet-web-browser", _("{browser} removes it").format(browser=a["origin_name"]),
                                    _("Its page of apps ({page}) opens: there, right-click {name} and choose to "
                                      "remove it.").format(page=a["manage"], name=a["name"]))],
                            means=_("only the browser can remove a web app for good: if only the menu entry went, the "
                                    "browser would write it again."),
                            commands_=[" ".join(shlex.quote(w) for w in webapps.manage_command(a))],
                            password=False, help_topic="programs"):
            self._start(webapps.manage_command(a))

    def _remove_orphan(self, a):
        from PySide6.QtCore import QFile
        from .. import question
        files = webapps.leftovers(a)
        if not question.confirm(self, _("Remove {name} from the menu?").format(name=a["name"]),
                                _("Yes, move them to the trash"), icon_name="edit-delete",
                                items=[("edit-delete", _("Move its menu entry and icons to the trash"),
                                        ngettext("{n} file", "{n} files", len(files)).format(n=len(files)),
                                        files)],
                                means=_("its browser is gone, so the entry no longer opens anything. The trash keeps "
                                        "the files until you empty it."), password=False, help_topic="programs"):
            return
        for f in files:
            QFile.moveToTrash(f)
        run_async(webapps.scan, on_done=self._webapps_loaded)

    # ------------------------------------------------------------ AppImages (appimages.py)
    def header_actions(self):
        b = tool(_("Add an AppImage…"), "list-add", self.add_appimage)
        b.setToolTip(_("Choose an AppImage file: Zypp Desk asks, then puts it in your AppImage folder and in the menu "
                       "at once. AppImages are your own files: this does not go through the queue."))
        return [b]

    def _scan_appimages(self, select=None):
        if select:
            self._ai_select = select
        download = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or None
        run_async(appimages.scan, self.ctx.settings.get(appimages.SETTING_FOLDERS) or [], download,
                  on_done=self._appimages_loaded)

    def _appimages_loaded(self, res, err):
        if err:
            return  # the other programs are still listed; an AppImage that cannot be read is simply not shown
        self.appimages = res or []
        paths = {a["path"] for a in self.appimages}
        for d in (self._ai_found, self._ai_errors):
            for p in [p for p in d if p not in paths]:
                d.pop(p, None)
        select, self._ai_select = self._ai_select, None
        if select:
            key = next((k for k, c in self.chips.items() if c.isChecked()), "all")
            if key not in ("all", "appimage"):
                self.chips["appimage"].setChecked(True)  # fills the list
        self._chip_texts()
        self._fill_mine()
        if select:
            for i in range(self.mine_list.count()):
                a = self.mine_list.item(i).data(Qt.ItemDataRole.UserRole)
                if a and a["kind"] == "appimage" and a["path"] == select:
                    self.mine_list.setCurrentRow(i)
                    self.mine_list.scrollToItem(self.mine_list.item(i))
                    break

    def appimage_line(self, a):
        """The grey line of a row: '~/AppImages/app.AppImage · 138 MB · terminal tool'."""
        from ..simulation import human_size
        parts = [appimages.display_path(a["path"])]
        if a["orphan"]:
            parts.append(_("the file is gone"))
        else:
            parts.append(human_size(a["size"]))
            if a["terminal"]:
                parts.append(_("terminal tool"))
        return " · ".join(parts)

    def _ai_source(self, a):
        """Where new versions come from: what the file says, else what the user said (settings)."""
        src = a.get("source")
        if src and src["kind"] != "unknown":
            return src
        said = (self.ctx.settings.get(appimages.SETTING_SOURCES) or {}).get(a["path"])
        return appimages.github_source(said) if said else src

    def _refresh_ai(self, path):
        """Show again the card (and the row) of this AppImage if it is the one selected."""
        self._chip_texts()
        self.mine_list.viewport().update()
        cur = self.mine_list.currentItem()
        a = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        if a and a["kind"] == "appimage" and a["path"] == path and self.tabs.currentIndex() == 0:
            self._show_appimage(a)

    def _show_appimage(self, a):
        from ..simulation import human_size
        path = a["path"]
        src = self._ai_source(a)
        found = self._ai_found.get(path) or {}
        busy = self._ai_busy.get(path)
        new = self.update_of(a)
        guarded = self.is_protected(a)
        rows = [(_("File"), breakable(appimages.display_path(path)))]
        if a["orphan"]:
            rows.append((_("In the menu"), html.escape(_("yes, but the file is gone"))))
        else:
            rows += [(_("Size"), html.escape(human_size(a["size"]))),
                     (_("Version"), html.escape(a["version"] or _("the file does not say"))),
                     (_("In the menu"), html.escape(_("yes") if a["menu"] else _("no")))]
            if not src:
                where = _("the file does not say")
            elif src["kind"] == "unknown":
                where = _("the file says it in a way Zypp Desk does not know")
            elif src.get("user"):
                where = _("{place}, as you said").format(place=appimages.source_text(src))
            else:
                where = _("{place}, as the file says").format(place=appimages.source_text(src))
            rows.append((_("New versions from"), breakable(where)))
            if new:
                rows.append((_("New version"), f"<b style='color:{GREEN}'>{html.escape(new)}</b>"))

        notes = []
        if a["orphan"]:
            notes.append(_("The file this menu entry runs is gone: the entry no longer opens anything and can be "
                           "removed."))
        else:
            notes.append(_("An AppImage is a whole program in one file. Zypp Desk puts it in the menu, looks for a "
                           "new version and can protect it or remove it."))
            if not a["read"]:
                notes.append(_("Install unsquashfs (the package squashfs) to see its name, version and icon: Zypp "
                               "Desk reads them without running the program.") if a["why"] == "notool" else
                             _("Zypp Desk cannot read inside this kind of AppImage without running it, so it shows "
                               "what the menu entry or the file name says."))
            if not a["writable"]:
                notes.append(_("It is in a folder only the administrator can change: Zypp Desk can open it, but not "
                               "update it or remove it."))
            if a["backup"]:
                notes.append(_("The previous version is kept as {file} until the new one has opened once from "
                               "here.").format(file=Path(a["backup"]).name))
            if busy == "checking":
                notes.append(_("Looking for a new version…"))
            elif busy == "downloading":
                notes.append(_("Downloading the new version…"))
            elif path in self._ai_errors:
                notes.append(_("Could not look for a new version: {error}").format(error=self._ai_errors[path]))
            elif found.get("same"):
                notes.append(_("You have the newest version: the SHA-256 GitHub publishes is the one of your file."))
            elif found and found.get("newer") is False:
                notes.append(_("You have the newest version ({version}).").format(
                    version=found.get("version") or found.get("asset")))
            elif found and found.get("newer") is None:
                notes.append(_("The newest file on GitHub ({file}) is not the one you have, and its version cannot "
                               "be compared.").format(file=found.get("asset")))
            if guarded:
                notes.append(_("Protected: Zypp Desk does not remove it until you stop protecting it."))
        self.mine_details.show_app(self.app_icon(a), a["name"], a["summary"] or "AppImage", rows, "\n\n".join(notes))

        # every action here acts at once (after its question, when it has one): an AppImage is the user's own
        # file and never goes through the queue. The tooltips say so.
        name = a["name"]
        at_once = _("AppImages are your own files: this does not go through the queue.")
        btns = []
        if a["orphan"]:
            btns.append(self._btn("ai_menu_remove", self._named(_("Remove {name} from the menu…"),
                                                                _("Remove it from the menu…"), name),
                                  "edit-delete", lambda: self._ai_menu_remove(a),
                                  _("Asks first, then moves its menu entry and icons to the trash at once: the file "
                                    "it runs is gone.") + " " + at_once))
            self.mine_details.set_buttons(btns)
            return
        get_new = bool(new) and a["writable"] and not busy
        ask = not os.access(path, os.X_OK)  # "Let it run?" comes first
        btns.append(self._btn("ai_open", self._named(_("Open {name}…") if ask else _("Open {name}"),
                                                     _("Open it…") if ask else _("Open it"), name),
                              "media-playback-start", lambda: self._ai_open(a),
                              (_("Asks first to make the file executable, then starts the program.") if ask else
                               _("Starts the program now.")) + " " + at_once, primary=not get_new))
        if get_new:
            version = found.get("version")
            btns.append(self._btn("ai_get_new", self._named(_("Get {name} {version}…"), _("Get the new version…"),
                                                            name, version=version) if version
                                  else _("Get the new version…"),
                                  "download", lambda: self._ai_get_new(a),
                                  _("Asks first, then downloads {file} from {place} and checks it; it asks again "
                                    "before the new file takes the place of yours.").format(
                                      file=found.get("asset") or "?", place=host_of(found.get("url")) or "?")
                                  + " " + at_once, primary=True))
        if busy == "checking":
            look_text, look_tip = _("Looking…"), _("Looking for a new version…")
        elif src and src["kind"] == "github":
            look_text = _("Look for a new version")
            look_tip = _("Asks GitHub now whether {project} has a newer file than yours. Nothing is downloaded.") \
                .format(project=src.get("repo") or "?")
        elif src and src["kind"] in ("zsync", "page"):
            look_text = _("Look for a new version…")
            look_tip = _("Opens in your browser the page where its publisher puts new versions.")
        else:
            look_text = _("Look for a new version…")
            look_tip = _("The file does not say where new versions are published: Zypp Desk asks you for its "
                         "GitHub project first, then looks there. Nothing is downloaded.")
        look = self._btn("ai_look", look_text, "view-refresh", lambda: self._ai_look(a), look_tip)
        look.setEnabled(not busy)
        btns.append(look)
        if src and src.get("user"):
            btns.append(self._btn("ai_source", _("Change where to look…"), "document-edit",
                                  lambda: self._ai_ask_source(a),
                                  _("Write again the GitHub project where new versions of {name} are "
                                    "published.").format(name=name)))
        if a["backup"] and a["writable"]:
            btns.append(self._btn("ai_restore", _("Back to the previous version…"), "edit-undo",
                                  lambda: self._ai_restore(a),
                                  _("Asks first, then puts back the version you had ({file}); the new one goes to "
                                    "the trash.").format(file=Path(a["backup"]).name) + " " + at_once))
        if a["menu"]:
            btns.append(self._btn("ai_menu_remove", self._named(_("Remove {name} from the menu…"),
                                                                _("Remove it from the menu…"), name),
                                  "list-remove", lambda: self._ai_menu_remove(a),
                                  _("Asks first, then moves its menu entry and icons to the trash at once. The "
                                    "program stays where it is and still opens from here.") + " " + at_once))
        else:
            btns.append(self._btn("ai_menu_add", self._named(_("Add {name} to the menu"), _("Add it to the menu"),
                                                             name),
                                  "list-add", lambda: self._ai_menu_add(a),
                                  _("Writes a menu entry with its name and icon now. Zypp Desk does not run the "
                                    "program.") + " " + at_once))
        btns.append(self._protect_btn(a, guarded))
        rm = self._btn("ai_remove", self._named(_("Remove {name}…"), _("Remove it…"), name), "edit-delete",
                       lambda: self._ai_remove(a),
                       _("Asks first, then moves the program, its menu entry and its icons to the trash at once. "
                         "Its settings in your home folder stay.") + " " + at_once)
        if guarded:
            rm.setEnabled(False)
            rm.setToolTip(_("It is protected: stop protecting it first."))
        elif not a["writable"]:
            rm.setEnabled(False)
            rm.setToolTip(_("Only the administrator can change the folder it is in."))
        btns.append(rm)
        self.mine_details.set_buttons(btns)

    @staticmethod
    def _trash(path):
        """Move a file to the trash (never deleted for good). True when it went."""
        from PySide6.QtCore import QFile
        res = QFile.moveToTrash(path)
        return bool(res[0] if isinstance(res, tuple) else res)

    def _ai_open(self, a):
        from PySide6.QtCore import QProcess
        from .. import question
        from ..ui import error_box
        if os.geteuid() == 0:
            error_box(self, _("Open {name}").format(name=a["name"]),
                      _("Zypp Desk does not run AppImages as the administrator."))
            return
        if not os.access(a["path"], os.X_OK):
            if not question.confirm(
                    self, _("Let {name} run?").format(name=a["name"]), _("Yes, make it executable"),
                    icon_name="system-run",
                    items=[("system-run", _("Make the file executable"), appimages.display_path(a["path"]))],
                    means=_("an AppImage is a program: it runs with your permissions, like any program you start. "
                            "Open only AppImages from publishers you trust."), password=False, help_topic="programs"):
                return
            try:
                appimages.make_executable(a["path"])
            except OSError as e:
                error_box(self, _("Open {name}").format(name=a["name"]), str(e))
                return
        words = appimages.open_command(a)
        ok, pid = QProcess.startDetached(words[0], words[1:], a["folder"])
        if not ok:
            error_box(self, _("Open {name}").format(name=a["name"]), _("It could not be started."))
            return
        if a["backup"]:  # still running a few seconds later: the new version works, the old one can go
            QTimer.singleShot(8000, self, lambda p=a["path"], b=a["backup"], pid=pid: self._ai_opened(p, b, pid))

    def _ai_opened(self, path, backup, pid):
        if appimages.process_alive(pid) and os.path.isfile(backup) and os.path.isfile(path):
            self._trash(backup)
            self._scan_appimages()

    def _ai_ask_source(self, a):
        """Ask where new versions are published (most AppImages do not say it themselves). Returns the source."""
        from PySide6.QtWidgets import QInputDialog
        from ..ui import error_box
        said = (self.ctx.settings.get(appimages.SETTING_SOURCES) or {}).get(a["path"]) or ""
        text, ok = QInputDialog.getText(
            self, _("Where is {name} published?").format(name=a["name"]),
            _("The file does not say where its new versions are. If they are published on GitHub, write the "
              "address of the project, for example https://github.com/owner/project:"),
            QLineEdit.EchoMode.Normal, f"https://github.com/{said}" if said else "https://github.com/")
        if not ok:
            return None
        src = appimages.github_source(text)
        if not src:
            error_box(self, _("Where is {name} published?").format(name=a["name"]),
                      _("That is not the address of a project on GitHub."))
            return None
        sources = dict(self.ctx.settings.get(appimages.SETTING_SOURCES) or {})
        sources[a["path"]] = src["repo"]
        self.ctx.settings[appimages.SETTING_SOURCES] = sources
        self.ctx.settings.save()
        self._ai_found.pop(a["path"], None)
        self._ai_errors.pop(a["path"], None)
        self._refresh_ai(a["path"])
        return src

    def _ai_look(self, a):
        """Look for a new version: only when asked (GitHub answers 60 questions an hour without an account)."""
        from ..ui import info_box
        src = self._ai_source(a)
        if not src or src["kind"] == "unknown":
            src = self._ai_ask_source(a)
            if not src:
                return
        if src["kind"] in ("zsync", "page"):
            # no GitHub: the place where the publisher puts the files, in the browser
            url = src["url"].rsplit("/", 1)[0] + "/" if src["kind"] == "zsync" else src["url"]
            if not QDesktopServices.openUrl(QUrl(url)):
                info_box(self, _("Look for a new version"), url)
            return
        path = a["path"]
        self._ai_busy[path] = "checking"
        self._ai_errors.pop(path, None)
        self._refresh_ai(path)
        run_async(appimages.check, a, src, on_done=lambda res, err, p=path: self._ai_checked(p, res, err))

    def _ai_checked(self, path, res, err):
        self._ai_busy.pop(path, None)
        if err:
            self._ai_errors[path] = str(err)
            self._ai_found.pop(path, None)
        else:
            self._ai_found[path] = res
        self._refresh_ai(path)

    def _ai_get_new(self, a):
        from .. import question
        from ..simulation import human_size
        f = self._ai_found.get(a["path"])
        if not f:
            return
        new_name = Path(a["path"]).name + ".new"
        size = human_size(f["size"]) if f.get("size") else ""
        if not question.confirm(
                self, _("Download {name} {version}?").format(name=a["name"], version=f.get("version") or ""),
                _("Yes, download it"), icon_name="download",
                items=[("download", _("Download {file}").format(file=f["asset"]),
                        (_("{size}, from {place}") if size else _("From {place}")).format(
                            size=size, place=host_of(f["url"]))),
                       ("security-high", _("Check it"),
                        _("The SHA-256 that GitHub publishes is compared with the file's.") if f.get("sha256") else
                        _("GitHub publishes no SHA-256 for this file: Zypp Desk only checks that it is an "
                          "AppImage."))],
                means=_("nothing changes yet: the new file is saved beside the old one as {file}, and Zypp Desk "
                        "asks before it takes the old one's place.").format(file=new_name),
                password=False, help_topic="programs"):
            return
        path = a["path"]
        self._ai_busy[path] = "downloading"
        self._refresh_ai(path)
        run_async(appimages.download_new, a, f, on_done=lambda res, err, app=a: self._ai_downloaded(app, res, err))

    def _ai_downloaded(self, a, res, err):
        from .. import question
        from ..ui import error_box
        path = a["path"]
        self._ai_busy.pop(path, None)
        if err:
            self._refresh_ai(path)
            error_box(self, _("Get the new version"), str(err))
            return
        old_name = Path(path).name + ".old"
        key = question.ask_change(
            self, _("Use {name} {version} now?").format(name=a["name"], version=res.get("version") or ""),
            icon_name="system-software-update",
            items=[("download", _("The new version is downloaded and checked"),
                    _("SHA-256 checked with GitHub's") if res["sha256_checked"] else
                    _("GitHub publishes no SHA-256 for it: it is an AppImage, nothing more could be checked")),
                   ("system-software-update", _("It takes the place of the one you have"),
                    _("Same name and folder: the menu entries keep working.")),
                   ("document-save", _("The old one is kept as {file}").format(file=old_name),
                    _("It goes to the trash once the new one has opened from here; until then you can go back."))],
            means=_("the program changes only when it is opened again; if it is open now, close it first."),
            buttons=[("no", _("Not now: throw it away"), False,
                      _("The downloaded file goes to the trash; yours stays as it is.")),
                     ("yes", _("Yes, replace it"), True,
                      _("The new file takes the place of yours now; the old one is kept as {file}.").format(
                          file=old_name))],
            password=False, help_topic="programs")
        if key != "yes":
            self._trash(res["path"])
            self._refresh_ai(path)
            return
        backup = path + ".old"
        if os.path.lexists(backup) and not self._trash(backup):
            error_box(self, _("Get the new version"), _("{file} could not be moved to the trash.").format(
                file=Path(backup).name))
            return
        try:
            appimages.replace_with_new(path, res["path"])
        except OSError as e:
            error_box(self, _("Get the new version"), str(e))
            return
        self._ai_found.pop(path, None)
        if a["menu_ours"]:  # the entry Zypp Desk wrote says the version: write it again
            run_async(lambda: appimages.write_menu_entry(appimages.describe(path)),
                      on_done=lambda _r, _e: self._scan_appimages(select=path))
        else:
            self._scan_appimages(select=path)

    def _ai_restore(self, a):
        from .. import question
        from ..ui import error_box
        if not question.confirm(
                self, _("Back to the previous version of {name}?").format(name=a["name"]),
                _("Yes, go back"), icon_name="edit-undo",
                items=[("edit-delete", _("The new version goes to the trash"), appimages.display_path(a["path"])),
                       ("edit-undo", _("The previous one takes its place"), Path(a["backup"]).name)],
                means=_("the program is the one you had before the update. Close it first if it is open."),
                password=False, help_topic="programs"):
            return
        if not self._trash(a["path"]):
            error_box(self, _("Back to the previous version"), _("{file} could not be moved to the trash.").format(
                file=Path(a["path"]).name))
            return
        try:
            appimages.restore_backup(a["path"])
        except OSError as e:
            error_box(self, _("Back to the previous version"), str(e))
        self._ai_found.pop(a["path"], None)
        self._scan_appimages(select=a["path"])

    def _ai_menu_add(self, a):
        from ..ui import error_box

        def done(_res, err):
            if err:
                error_box(self, _("Add it to the menu"), str(err))
            self._scan_appimages(select=a["path"])
        run_async(appimages.write_menu_entry, a, on_done=done)

    def _ai_menu_remove(self, a):
        from .. import question
        files = appimages.menu_files(a)
        if not files:
            return
        if not question.confirm(self, _("Remove {name} from the menu?").format(name=a["name"]),
                                _("Yes, move them to the trash"), icon_name="edit-delete",
                                items=[("edit-delete", _("Move its menu entry and icons to the trash"),
                                        ngettext("{n} file", "{n} files", len(files)).format(n=len(files)),
                                        [appimages.display_path(f) for f in files])],
                                means=(_("its file is gone, so the entry no longer opens anything. The trash keeps "
                                         "the files until you empty it.") if a["orphan"] else
                                       _("the program stays where it is and still opens from Zypp Desk; only the "
                                         "menu no longer shows it. The trash keeps the files until you empty it.")),
                                password=False, help_topic="programs"):
            return
        for f in files:
            self._trash(f)
        self._scan_appimages(select=None if a["orphan"] else a["path"])

    def _ai_remove(self, a):
        from .. import question
        from ..ui import error_box
        if self.is_protected(a):
            return
        files = appimages.remove_files(a)
        extra = [f for f in files if f != a["path"]]  # the old version, the menu entries, their icons
        items = [("edit-delete", _("Move the program to the trash"), appimages.display_path(a["path"]))]
        if extra:
            items.append(("edit-delete", _("And its menu entry and icons"),
                          ngettext("{n} file", "{n} files", len(extra)).format(n=len(extra)),
                          [appimages.display_path(f) for f in extra]))
        if not question.confirm(self, _("Remove {name}?").format(name=a["name"]), _("Yes, move it to the trash"),
                                icon_name="edit-delete", items=items,
                                means=_("the program goes away from this computer; its settings and data in your "
                                        "home folder stay. The trash keeps the files until you empty it."),
                                password=False, help_topic="programs"):
            return
        failed = [f for f in files if not self._trash(f)]
        if failed:
            error_box(self, _("Remove {name}").format(name=a["name"]),
                      _("These files could not be moved to the trash:") + "\n" + "\n".join(failed))
        sources = dict(self.ctx.settings.get(appimages.SETTING_SOURCES) or {})
        if sources.pop(a["path"], None) is not None:
            self.ctx.settings[appimages.SETTING_SOURCES] = sources
            self.ctx.settings.save()
        self._scan_appimages()

    def add_appimage(self):
        """The header's "Add an AppImage…": pick a file, check that it is one, put it in the AppImage folder (moved
        or copied) and in the menu."""
        from PySide6.QtWidgets import QFileDialog
        from .. import protect, question
        from ..ui import error_box, info_box
        start = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or \
            os.path.expanduser("~")
        path, _f = QFileDialog.getOpenFileName(self, _("Add an AppImage"), start,
                                               _("AppImages") + " (*.AppImage *.appimage);;" + _("Every file") + " (*)")
        if not path:
            return
        if not appimages.appimage_type(path):
            error_box(self, _("Add an AppImage"), _("{file} is not an AppImage.").format(file=Path(path).name))
            return
        real = os.path.realpath(path)
        known = next((x for x in self.appimages if not x["orphan"] and os.path.realpath(x["path"]) == real), None)
        if known and known["menu"]:
            info_box(self, _("Add an AppImage"),
                     _("{name} is already here and in the menu.").format(name=known["name"]))
            self._scan_appimages(select=known["path"])
            return
        home = appimages.home_folder(self.ctx.settings)
        download = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or None
        kept = {os.path.realpath(f) for f, depth in
                appimages.folders(self.ctx.settings.get(appimages.SETTING_FOLDERS) or [], download)} - \
            {os.path.realpath(download or appimages.xdg_download_dir())}
        name = Path(path).name
        if os.path.realpath(os.path.dirname(path)) in kept | {os.path.realpath(home)}:
            # already where AppImages are kept: only the menu
            if not question.confirm(self, _("Add {file} to the menu?").format(file=name), _("Yes, add it"),
                                    icon_name="list-add",
                                    items=[("list-add", _("Put it in the menu"), appimages.display_path(path))],
                                    means=_("Zypp Desk does not run it now; you open it when you want. An AppImage "
                                            "runs with your permissions: add only AppImages from publishers you "
                                            "trust."), password=False, help_topic="programs"):
                return
            folder, move = os.path.dirname(path), True
        else:
            target = os.path.join(home, name)
            conflict = os.path.lexists(target)
            if conflict and protect.is_protected(self.ctx.settings, "appimage", target):
                error_box(self, _("Add an AppImage"), _("There is already a protected AppImage named {file} in "
                                                         "{folder}.").format(file=name,
                                                                             folder=appimages.display_path(home)))
                return
            key = question.ask_change(
                self, _("Add {file}?").format(file=name), icon_name="list-add",
                items=[("folder", _("Put it in {folder}").format(folder=appimages.display_path(home)),
                        _("The folder for AppImages: Zypp Desk looks for them there.")),
                       ("system-run", _("Make it executable"), _("So that it can be opened.")),
                       ("application-menu|list-add", _("Put it in the menu"), _("With its own name and icon."))],
                warning=(_("There is already a file named {file} in {folder}: it goes to the trash.").format(
                    file=name, folder=appimages.display_path(home)) if conflict else None),
                means=_("Zypp Desk does not run it now; you open it when you want. An AppImage runs with your "
                        "permissions: add only AppImages from publishers you trust."),
                buttons=[("no", _("Not yet"), False, _("Nothing is copied or moved.")),
                         ("copy", _("Copy it"), False,
                          _("A copy goes to {folder} now; the file stays where it is too.").format(
                              folder=appimages.display_path(home))),
                         ("move", _("Move it"), True,
                          _("The file moves to {folder} now.").format(folder=appimages.display_path(home)))],
                password=False, help_topic="programs")
            if key not in ("copy", "move"):
                return
            if conflict and not self._trash(target):
                error_box(self, _("Add an AppImage"), _("{file} could not be moved to the trash.").format(file=name))
                return
            folder, move = home, key == "move"

        def done(res, err):
            if err:
                error_box(self, _("Add an AppImage"), str(err))
                self._scan_appimages()
                return
            self._scan_appimages(select=res["path"])
        run_async(appimages.adopt, path, folder, move, on_done=done)

    # ------------------------------------------------------------ protected apps (protect.py)
    def is_protected(self, a):
        from .. import protect
        return protect.is_protected(self.ctx.settings, a["kind"], a["package"])

    def toggle_protect(self, a):
        from .. import protect, question
        on = not self.is_protected(a)
        if on:
            ok = question.confirm(
                self, _("Protect {name}?").format(name=a["name"]), _("Yes, protect it"),
                icon_name="security-high",
                items=[("security-high", _("Never remove it by mistake"),
                        _("Zypp Desk refuses to remove it, from any page, until you stop protecting it."))],
                means=_("nothing changes now: it is only a mark in Zypp Desk. Updates still come, unless it is "
                        "masked in Flatpak."), password=False, help_topic="programs")
            if not ok:
                return
        protect.set_protected(self.ctx.settings, a["kind"], a["package"], on)
        self.mine_list.viewport().update()
        self._show_installed(self.mine_list.currentItem())

    def save_bundle(self, a):
        """A copy of an installed Flatpak app as one file: it can be installed again when the remote no longer
        has it (an app marked "no longer maintained or distributed")."""
        from PySide6.QtWidgets import QFileDialog
        from .. import protect
        # the folder used last time, else the user's Documents (never a folder of one particular computer)
        from PySide6.QtCore import QStandardPaths
        folder = self.ctx.settings.get("bundle_folder") or ""
        if not folder or not os.path.isdir(folder):
            folder = (QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
                      or os.path.expanduser("~"))
        name = f"{a['package']}-{a.get('version') or 'stable'}.flatpak"
        path, _f = QFileDialog.getSaveFileName(self, _("Save a copy of {name}").format(name=a["name"]),
                                               os.path.join(folder, name), "Flatpak (*.flatpak)")
        if not path:
            return
        self.ctx.settings["bundle_folder"] = os.path.dirname(path)
        self.ctx.settings.save()
        self.ctx.run([protect.bundle_step(a["package"], path, a.get("installation") or "system")],
                     _("Save a copy of {name}").format(name=a["name"]))

    def _queue_changed(self):
        self.mine_list.viewport().update()
        self.disc_list.viewport().update()
        if self.tabs.currentIndex() == 0:
            self._show_installed(self.mine_list.currentItem())
        else:
            self._show_catalogue(self.disc_list.currentItem())

    # ------------------------------------------------------------ my applications
    def _origin_count(self, key):
        mine = self._mine()
        if key == "all":
            return len(mine)
        if key == "updates":
            return sum(1 for a in mine if self.update_of(a))
        return sum(1 for a in mine if a["origin"] == key)

    def _chip_texts(self):
        labels = {"all": _("All"), "repo": _("Repositories"), "flatpak": _("Flatpak"), "web": _("From the web"),
                  "webapp": _("Web apps"), "appimage": "AppImage", "updates": _("With updates")}
        for key, c in self.chips.items():
            c.setText(f"{labels[key]}  {self._origin_count(key)}" if self.installed else labels[key])

    def _fill_mine(self):
        key = next((k for k, c in self.chips.items() if c.isChecked()), "all")
        term = self.mine_search.text().strip().lower()
        cur = self.mine_list.currentItem()
        cur_id = cur.data(Qt.ItemDataRole.UserRole)["id"] if cur else None
        self.mine_list.clear()
        for a in self._mine():
            if key == "updates" and not self.update_of(a):
                continue
            if key not in ("all", "updates") and a["origin"] != key:
                continue
            if not matches(a, term):
                continue
            it = QListWidgetItem(self.mine_list)
            it.setData(Qt.ItemDataRole.UserRole, a)
            it.setToolTip(rich_tip(a["name"], a.get("summary") or "", "", [a["package"]]))
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
        if a["kind"] == "webapp":
            self._show_webapp(a)
            return
        if a["kind"] == "appimage":
            self._show_appimage(a)
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
        if a["kind"] != "webapp" and self.is_protected(a):
            note = (note + "\n\n" if note else "") + _("Protected: Zypp Desk does not remove it until you stop "
                                                       "protecting it.")
        if state:
            note = (note + "\n\n" if note else "") + _("In the queue: {change}.").format(change=self._state_text(state))
        self.mine_details.show_app(self.app_icon(a), a["name"], a["summary"] or "", rows, note)

        # Updating and removing only add to the queue: "To the queue:" says so in front of them, and none is green
        # (nothing on this card changes the system at once)
        name = a["name"]
        nothing_yet = _("Nothing changes until you apply the queue.")
        queued = []
        if new and a["origin"] == "web":
            queued.append(self._btn(
                "app_get_new", self._named(_("Get {name} {version}…"), _("Get the new version…"), name, version=new),
                "download", lambda _c=False, pkg=a["package"]: self.ctx.pages["websources"].get_new_versions([pkg]),
                _("Downloads {name} {version} from {site} and checks it, then puts it in the queue (the checks may ask "
                  "questions).").format(name=name, version=new, site=a["origin_name"] or "?") + " " + nothing_yet))
        elif new and not state:
            queued.append(self._btn(
                "app_update", self._named(_("Update {name}"), _("Update it"), name), "system-software-update",
                lambda: self._queue_update(a),
                _("Puts the update of {name} to {version} in the queue ({command}).").format(
                    name=name, version=new, command=self._queue_cmd(a, "update")) + " " + nothing_yet))
        guarded = self.is_protected(a)
        rest = []
        if state:
            rest.append(self._btn("app_drop", self._named(_("Drop {name} from the queue"), _("Drop it from the queue"),
                                                          name),
                                  "edit-undo", lambda: self._drop(a),
                                  _("Takes the change of {name} out of the queue ({change}). Nothing on the system "
                                    "changes.").format(name=name, change=self._state_text(state))))
        else:
            rm = self._btn("app_remove", self._named(_("Remove {name}") if a["kind"] == "rpm" else _("Uninstall {name}"),
                                                     _("Remove it") if a["kind"] == "rpm" else _("Uninstall it"), name),
                           "edit-delete", lambda: self._queue_remove(a),
                           _("Puts the removal of {name} in the queue ({command}).").format(
                               name=name, command=self._queue_cmd(a, "remove")) + " " + nothing_yet)
            if guarded:
                rm.setEnabled(False)
                rm.setToolTip(_("It is protected: stop protecting it first."))
            queued.append(rm)
        rest.append(self._protect_btn(a, guarded))
        if a["kind"] == "flatpak":
            rest.append(self._btn("app_save_copy", self._named(_("Save a copy of {name}…"), _("Save a copy…"), name),
                                  "document-save", lambda: self.save_bundle(a),
                                  _("Asks where, then writes the whole app to one .flatpak file at once (flatpak "
                                    "build-bundle), so that it can be installed again even if its remote drops it. "
                                    "It does not go through the queue.")))
        if a["origin"] == "web":
            rest.append(self._btn("app_websources", _("Open Web sources"), "internet-web-browser",
                                  lambda: self.ctx.go("websources"),
                                  _("Go to Web sources, where Zypp Desk looks for new versions of the programs from "
                                    "the web.")))
        if a["kind"] == "flatpak":
            rest.append(self._btn("app_show_flatpak", self._named(_("Show {name} in Flatpak"), _("Show it in Flatpak"),
                                                                  name),
                                  "flatpak-discover", lambda: self._show_in_flatpak(a),
                                  _("Go to the Flatpak page with {name} selected: its size, runtime and "
                                    "permissions.").format(name=name)))
        else:
            rest.append(self._btn("app_show_packages", self._named(_("Show {name} in Packages"),
                                                                   _("Show it in Packages"), name),
                                  "package-x-generic", lambda: self._show_in_packages(a),
                                  _("Go to Packages and search for {package}: its versions, dependencies and "
                                    "files.").format(package=a["package"])))
        self.mine_details.set_buttons(([queue_label()] + queued if queued else []) + rest)

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

    def _show_in_flatpak(self, a):
        self.ctx.go("flatpak")
        page = self.ctx.pages.get("flatpak")
        if page is not None and hasattr(page, "select_app"):
            page.select_app(a["package"])

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
            self.cat_title.set_full_text(label)
            self.cat_icon.setPixmap(icon(icon_name, "package-x-generic").pixmap(28, 28))
        else:
            self.cat_title.set_full_text(_("Search in every theme"))
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
        first.setToolTip(_("Show every program of this theme"))
        chips = [first]
        for s in sorted(top, key=lambda s: sub_label(s).lower()):
            c = Chip(sub_label(s), False, self)
            c.setProperty("sub", s)
            c.setToolTip(_("Show only: {theme}").format(theme=sub_label(s)))
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
            it.setToolTip(rich_tip(a["name"], a.get("summary") or "", "", [a["package"]]))
            if not self.is_installed(a):  # only what you can install can be marked
                it.setData(MARK_ROLE, True)
                it.setCheckState(Qt.CheckState.Checked if (a["kind"], a["package"]) in self.disc_marked
                                 else Qt.CheckState.Unchecked)
            n += 1
        self.disc_count.setText(ngettext("{n} program", "{n} programs", n).format(n=n))
        if n:
            self.disc_list.setCurrentRow(0)
        else:
            self.disc_details.clear(_("Nothing found. Try another word, or every source."))

    # ------------------------------------------------ marking several in Discover
    def _disc_item_changed(self, item):
        a = item.data(Qt.ItemDataRole.UserRole)
        if not a or not item.data(MARK_ROLE):
            return
        key = (a["kind"], a["package"])
        if is_checked(item.checkState()):
            self.disc_marked[key] = a
        else:
            self.disc_marked.pop(key, None)
        self._disc_update_markbar()

    def _disc_update_markbar(self):
        marked = [a for a in self.disc_marked.values() if not self.is_installed(a)]
        n = len(marked)
        names = ", ".join(a["name"] for a in marked[:4]) + ("…" if n > 4 else "")
        if n == 1 and len(marked[0]["name"]) <= 24:
            label = _("Install {name}").format(name=amp(marked[0]["name"]))
        else:
            label = ngettext("Install the ticked one", "Install the {n} ticked", n).format(n=n)
        rpms = [a["package"] for a in marked if a["kind"] == "rpm"]
        fps = [a["package"] for a in marked if a["kind"] != "rpm"]
        cmds = []
        for words, pkgs in (("zypper install", rpms), ("flatpak install --system", fps)):
            if pkgs:
                cmds.append(" ".join([words] + pkgs[:3]) + (" …" if len(pkgs) > 3 else ""))
        tip = _("Puts {names} in the queue to be installed ({command}).").format(
            names=names, command="; ".join(cmds)) + " " + _("Nothing changes until you apply the queue.")
        # only adds to the queue: never green, "To the queue:" in front (the bar was made with to_queue=True)
        self.disc_markbar.set_state(n, ngettext("{n} marked", "{n} marked", n).format(n=n), names,
                                    [("install", label, n > 0, False, tip)] if n else [])

    def _disc_unmark_all(self):
        self.disc_marked.clear()
        self.disc_list.blockSignals(True)
        try:
            for i in range(self.disc_list.count()):
                it = self.disc_list.item(i)
                if it.data(MARK_ROLE):
                    it.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self.disc_list.blockSignals(False)
        self.disc_list.viewport().update()
        self._disc_update_markbar()

    def _disc_mark_action(self, key):
        if key != "install":
            return
        with self.ctx.queue.batch():
            for a in self.disc_marked.values():
                if self.is_installed(a) or self.queue_state(a):
                    continue
                if a["kind"] == "rpm":
                    self.ctx.queue.add(Action("install", a["package"]))
                else:
                    self.ctx.queue.add(Action("flatpak-install", a["package"], remote=a["remote"],
                                              installation="system"))
        self._disc_unmark_all()
        self._show_catalogue(self.disc_list.currentItem())

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
        # installing and removing only add to the queue: "To the queue:" in front, never green
        name = a["name"]
        nothing_yet = _("Nothing changes until you apply the queue.")
        btns = []
        if state:
            btns.append(self._btn("disc_drop", self._named(_("Drop {name} from the queue"), _("Drop it from the queue"),
                                                           name),
                                  "edit-undo", lambda: self._drop(a),
                                  _("Takes the change of {name} out of the queue ({change}). Nothing on the system "
                                    "changes.").format(name=name, change=self._state_text(state))))
        elif installed:
            inst = next((x for x in self.installed if x["package"] == a["package"] and x["kind"] == a["kind"]), None)
            target = inst or dict(a, installation="system")
            btns += [queue_label(), self._btn(
                "disc_remove", self._named(_("Remove {name}") if a["kind"] == "rpm" else _("Uninstall {name}"),
                                           _("Remove it") if a["kind"] == "rpm" else _("Uninstall it"), name),
                "edit-delete", lambda: self._queue_remove(target),
                _("Puts the removal of {name} in the queue ({command}).").format(
                    name=name, command=self._queue_cmd(target, "remove")) + " " + nothing_yet)]
        else:
            if a["kind"] == "rpm":
                def add():
                    self.ctx.queue.add(Action("install", a["package"]))
            else:
                def add():
                    self.ctx.queue.add(Action("flatpak-install", a["package"], remote=a["remote"],
                                              installation="system"))
            btns += [queue_label(), self._btn(
                "disc_install", self._named(_("Install {name}"), _("Install it"), name), "list-add", add,
                _("Puts {name} in the queue to be installed ({command}).").format(
                    name=name, command=self._queue_cmd(a, "install")) + " " + nothing_yet)]
        if a["kind"] == "rpm":
            btns.append(self._btn("disc_show_packages", self._named(_("Show {name} in Packages"),
                                                                    _("Show it in Packages"), name),
                                  "package-x-generic", lambda: self._show_in_packages(a),
                                  _("Go to Packages and search for {package}: its versions, dependencies and "
                                    "files.").format(package=a["package"])))
        self.disc_details.set_buttons(btns)
