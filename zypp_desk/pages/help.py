# SPDX-License-Identifier: GPL-3.0-or-later
"""Help and About: a place for the help, in several languages, and one for the credits.

The help is one HTML file per language in zypp_desk/help/ (en.html, ca.html): each topic starts with a comment
'<!-- topic: id | Title -->'. The window shows the file of your language, or the English one. Links 'page:<key>'
open that section of the window; web links open in the browser.
"""

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

from .. import APP_ID, APP_NAME, VERSION
from ..i18n import _
from ..style import GREEN
from ..ui import Card, Page, RoomSplitter, button, heading, icon, muted

HELP_DIR = Path(__file__).resolve().parent.parent / "help"
HOME = "https://github.com/jordirelats/zypp-desk"
_TOPIC = re.compile(r"<!--\s*topic:\s*([\w-]+)\s*\|\s*(.*?)\s*-->")
STYLE = (f"h2 {{ color: {GREEN}; margin-top: 4px; }} h3 {{ margin-top: 14px; }} "
         "p, li { line-height: 135%; } code { font-family: monospace; } a { color: #2980b9; }")


def help_language():
    """The two letters of the language the help is shown in: the user's, if there is a help file for it."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in (os.environ.get(var) or "").split(":"):
            code = part.split("_")[0].split(".")[0].split("@")[0].lower()
            if code in ("c", "posix", ""):
                continue
            if (HELP_DIR / f"{code}.html").is_file():
                return code
            if var == "LANGUAGE":
                continue  # the next language of the list
            return "en"
    return "en"


def topics(lang=None):
    """[(id, title, html)] of the help file of that language."""
    lang = lang or help_language()
    try:
        text = (HELP_DIR / f"{lang}.html").read_text(encoding="utf-8")
    except OSError:
        if lang != "en":
            return topics("en")
        return []
    marks = list(_TOPIC.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), m.group(2), text[m.end():end].strip()))
    return out


class _Browser(QTextBrowser):
    """Help text: 'page:<key>' links open that section; web links open in the browser."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDefaultStyleSheet(STYLE)
        self.document().setDocumentMargin(18)
        # short lines, also in a wide window: long lines are hard to read
        self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        self._fit_width()
        self.anchorClicked.connect(self._open)

    def _fit_width(self):
        """At most about 88 characters a line, and never wider than the view (no sideways scrolling)."""
        room = self.viewport().width() - 2 * int(self.document().documentMargin()) - 4
        self.setLineWrapColumnOrWidth(max(200, min(self.fontMetrics().averageCharWidth() * 88, room)))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_width()

    def _open(self, url):
        s = url.toString()
        if s.startswith("page:"):
            key = s[5:]
            if key in self.page.ctx.pages:
                self.page.ctx.go(key)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)


class HelpPage(Page):
    key = "help"
    title = _("Help")
    icon_name = "help-contents"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        split = RoomSplitter(Qt.Orientation.Horizontal, self)
        self.list = QListWidget(split)
        self.list.setMaximumWidth(300)
        self.list.setMinimumWidth(200)
        self.list.setObjectName("helptopics")
        self.view = _Browser(self, split)
        split.addWidget(self.list)
        split.addWidget(self.view)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)
        self._html = {}
        for tid, title, html in topics():
            it = QListWidgetItem(title)
            it.setData(Qt.ItemDataRole.UserRole, tid)
            it.setSizeHint(it.sizeHint().expandedTo(self.list.fontMetrics().size(0, "Ag") * 2))
            self.list.addItem(it)
            self._html[tid] = html
        self.list.currentItemChanged.connect(self._show)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.view.setPlainText(_("The help files are missing."))

    def show_topic(self, tid):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == tid:
                self.list.setCurrentRow(i)
                return

    def _show(self, cur, _prev=None):
        if cur is not None:
            self.view.setHtml(self._html.get(cur.data(Qt.ItemDataRole.UserRole), ""))
            self.view.verticalScrollBar().setValue(0)


def _license_path():
    for p in (Path("/usr/share/licenses/zypp-desk/LICENSE"),
              Path(__file__).resolve().parent.parent.parent / "LICENSE"):
        if p.is_file():
            return p
    return None


def _credit(title, text):
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 4, 0, 4)
    v.setSpacing(1)
    t = QLabel(title)
    f = t.font()
    f.setBold(True)
    t.setFont(f)
    v.addWidget(t)
    d = muted(text)
    d.setTextFormat(Qt.TextFormat.PlainText)
    v.addWidget(d)
    return w


class AboutPage(Page):
    key = "about"
    title = _("About")
    icon_name = "help-about"
    COLUMN = 760

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        column = QWidget(self)
        column.setMaximumWidth(self.COLUMN)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        outer.addWidget(column, 1)
        outer.addStretch(0)

        # who and what
        head = Card()
        h = QHBoxLayout()
        h.setSpacing(18)
        pic = QLabel()
        pic.setPixmap(icon(APP_ID, "system-software-install").pixmap(72, 72))
        h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        hv = QVBoxLayout()
        hv.setSpacing(3)
        hv.addWidget(heading(f"{APP_NAME} {VERSION}", 1.5))
        hv.addWidget(QLabel(_("Every zypper feature in a window")))
        by = muted(_("Made by Jordi Relats, with the help of Claude, an AI assistant by Anthropic."))
        hv.addWidget(by)
        lic = muted(_("Free software: you can use, study, share and change it under the GNU General Public "
                      "License, version 3 or later. It comes with no warranty."))
        hv.addWidget(lic)
        links = QHBoxLayout()
        links.setSpacing(8)
        # each says what it opens: the two web pages in the browser, the license in a window ("…")
        b = button(_("Open the web site"), "internet-web-browser", lambda: QDesktopServices.openUrl(QUrl(HOME)))
        b.setToolTip(_("Opens Zypp Desk's page in your web browser: {url}").format(url=HOME))
        links.addWidget(b)
        b = button(_("Report a problem on GitHub"), "tools-report-bug",
                   lambda: QDesktopServices.openUrl(QUrl(HOME + "/issues")))
        b.setToolTip(_("Opens the list of problems of Zypp Desk in your web browser, to add yours: {url}").format(
            url=HOME + "/issues"))
        links.addWidget(b)
        b = button(_("Read the license…"), "text-x-generic", self.show_license)
        b.setToolTip(_("Shows the GNU General Public License in a window."))
        links.addWidget(b)
        links.addStretch(1)
        hv.addSpacing(6)
        hv.addLayout(links)
        h.addLayout(hv, 1)
        head.lay.addLayout(h)
        col.addWidget(head)

        # credits
        cred = Card(_("Credits"))
        for title, text in (
            (_("Translations"), _("Catalan, German and French. To add your language, see TRANSLATING.md on the "
                                  "web site.")),
            (_("zypper and libzypp"), _("The package manager of openSUSE, which does all the real work. Zypp Desk "
                                        "only runs it and shows what it says.")),
            (_("RPM"), _("The package format, its database and its signatures.")),
            (_("Qt and Qt for Python (PySide6)"), _("The window, its widgets and the Breeze style of KDE "
                                                    "Plasma.")),
            (_("Flatpak, fwupd and snapper"), _("Flatpak apps, firmware updates, and the snapshots to go back "
                                                "after an update.")),
            (_("polkit"), _("Asks for the administrator password and lets the small helper run zypper as root.")),
            (_("AppStream, KDE Breeze icons"), _("The catalogue of programs, their icons and the icons of the "
                                                 "window.")),
            (_("Open Build Service, Fedora COPR and Flathub"), _("Where Search online looks for programs, and "
                                                                 "where Zypp Desk itself is built.")),
            (_("openSUSE"), _("Tumbleweed, and the people who make it every day.")),
        ):
            cred.lay.addWidget(_credit(title, text))
        col.addWidget(cred)
        col.addStretch(1)

    def show_license(self):
        p = _license_path()
        dlg = QDialog(self)
        dlg.setWindowTitle(_("License"))
        dlg.resize(720, 620)
        v = QVBoxLayout(dlg)
        view = QTextBrowser(dlg)
        view.setOpenExternalLinks(True)
        try:
            view.setPlainText(p.read_text(encoding="utf-8") if p else "")
        except OSError:
            view.setPlainText("")
        if not view.toPlainText():
            view.setPlainText(_("The license file was not found. Zypp Desk is under the GNU General Public License, "
                                "version 3 or later: https://www.gnu.org/licenses/gpl-3.0.html"))
        v.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.exec()
        dlg.deleteLater()
