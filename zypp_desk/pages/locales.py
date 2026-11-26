# SPDX-License-Identifier: GPL-3.0-or-later
"""Languages: every language zypper knows, the ones you have, and what adding one would bring.

zypper installs the translation packages (-lang, dictionaries, l10n) of the programs you have into the "requested"
languages (/var/lib/zypp/RequestedLocales). The list on the left is 'zypper locales -a'; the card on the right
tells what one language means for this computer ('zypper locales -p', asked only for the selected language, once the
selection stops moving). Ticking and unticking only queue 'zypper addlocale' / 'removelocale':
nothing runs until the queue is applied.
"""

import gettext
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QEvent, QLocale, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPalette, QPen
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QListWidget,
                               QListWidgetItem, QSizePolicy, QStyle, QStyledItemDelegate, QToolButton, QToolTip,
                               QVBoxLayout, QWidget)

from .. import backend
from ..i18n import _, ngettext, pgettext
from ..runner import Step
from ..style import GREEN, GREEN_DARK, ORANGE, RED
from ..tasks import run_async
from ..txqueue import Action
from ..ui import Card, Page, RoomSplitter, button, heading, hline, icon, muted
from .history import INFO_BOX, command_label, plain_label
from .markbar import amp, check_rect, paint_check, queue_label

LOCALE_CONF = Path("/etc/locale.conf")
ENGLISH = "en"  # the programs are written in it: always there, it needs no translation packages
CODE_ROLE = Qt.ItemDataRole.UserRole
# libzypp's Locale::fallback(): a variant also brings its language (ca_ES → ca), except Brazilian Portuguese,
# which falls back to English
NO_BASE_FALLBACK = {"pt_BR"}
# dictionaries and spell checkers among the packages of a language
SPELL = re.compile(r"^(myspell|hunspell|aspell|ispell|hyphen)-")
# "Translations for package gimp", "Documentation for texlive-babel-spanish", "Locale support for cnf"
FOR_PACKAGE = re.compile(r"(?:Translations|Documentation|Locale support|Locale Data|Common translations) for "
                         r"(?:package )?(\S+)")
SUFFIXES = ("-lang", "-translations-common", "-translations-other", "-locale", "-doc")
NOTE_GREY, NOTE_GREEN, NOTE_RED, NOTE_ORANGE = "grey", "green", "red", "orange"
FILTER_STYLE = ("QToolButton { border: 1px solid rgba(127, 140, 141, 0.45); border-radius: 7px; padding: 5px 12px; }"
                "QToolButton:hover { border-color: rgba(39, 174, 96, 0.8); }"
                f"QToolButton:checked {{ background: {GREEN}; color: white; border-color: {GREEN_DARK};"
                " font-weight: 600; }")

_UNKNOWN_LANGUAGES = (QLocale.Language.C, QLocale.Language.AnyLanguage)
_ANY_TERRITORY = getattr(QLocale.Country, "AnyTerritory", None) or QLocale.Country.AnyCountry


# ------------------------------------------------------------------ zypper and the system (worker threads)

def all_locales():
    """Every language zypper knows ('zypper locales -a': no XML output, its table is read): a list of
    dict(code, name, requested), with zypper's English name."""
    p = backend.run_c(["--no-refresh", "locales", "-a"], check=True)
    out = []
    for line in p.stdout.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 3 and cols[0] and cols[0] != "Code" and not set(cols[0]) <= {"-", "+"}:
            out.append(dict(code=cols[0], name=cols[1], requested=backend.locale_requested(cols[2])))
    return out


def packages_for(codes):
    """The packages that bring these languages ('zypper locales -p'): {name: dict(installed, summary, code)}.
    zypper lists every package of the repositories, also those for programs you do not have."""
    p = backend.run_c(["--no-refresh", "locales", "-p", "--", *codes], check=True)
    out = {}
    code = codes[0]
    for line in p.stdout.splitlines():
        m = re.search(r"\(locale '([^']+)', requested:", line)
        if m:
            code = m.group(1)
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) == 3 and cols[1] and cols[1] != "Name":
            # i, i+: installed; v: another version of it is installed
            out.setdefault(cols[1], dict(installed=cols[0][:1] in ("i", "v"), summary=cols[2], code=code))
    return out


def installed_names():
    return set(backend.rpm_query(["-qa", "--qf", "%{NAME}\n"]).split())


def system_language():
    """The language of the computer: LANG of /etc/locale.conf (else of the environment) without the encoding,
    'ca_ES' for 'ca_ES.UTF-8'. '' for C and POSIX."""
    lang = ""
    try:
        for line in LOCALE_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "LANG":
                lang = value.strip().strip("\"'")
    except OSError:
        pass
    lang = (lang or os.environ.get("LANG", "")).split(".")[0].split("@")[0]
    return "" if lang in ("", "C", "POSIX") else lang


def programs_of(name, summary, code):
    """The packages a translation package is for, as far as its name and summary tell: 'gimp' for gimp-lang,
    'libreoffice' for libreoffice-l10n-es, 'man-pages' (or 'man') for man-pages-es. A translation package comes
    with a language only for the programs you have (its Supplements: '(gimp and namespace:language(es))'); reading
    the Supplements of a thousand packages takes zypper 13 s, the names give the same count (checked with ca and es)."""
    out = set()
    m = FOR_PACKAGE.match(summary)
    if m:
        out.add(m.group(1))
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            out.add(name[:-len(suffix)])
    m = re.match(r"(.+)-" + re.escape(code) + r"(?:[_@-][A-Za-z_@-]*)?$", name)
    if m:
        parts = m.group(1).split("-")
        out.update("-".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


def what_it_brings(packages, installed):
    """dict(have=[names installed], bring=[names for programs you have, not installed], dicts=[(name, installed)])."""
    have, bring, dicts = [], [], []
    for name, d in sorted(packages.items()):
        if SPELL.match(name):
            dicts.append((name, d["installed"]))
        if d["installed"]:
            have.append(name)
        elif programs_of(name, d["summary"], d["code"]) & installed:
            bring.append(name)
    return dict(have=have, bring=bring, dicts=dicts)


# ------------------------------------------------------------------ names

def fold(text):
    """For searching and sorting: no capitals, no accents ('espanol' finds 'Español')."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def split_code(code):
    """'ca_ES_valencia' → ('ca', '', 'ES', ['valencia']); 'zh_Hant' → ('zh', 'Hant', '', [])."""
    parts = [p for p in re.split(r"[_@.-]", code) if p]
    lang, script, territory, rest = (parts[0].lower() if parts else code), "", "", []
    for p in parts[1:]:
        if not script and not territory and len(p) == 4 and p.isalpha():
            script = p
        elif not territory and ((len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit())):
            territory = p.upper()
        else:
            rest.append(p)
    return lang, script, territory, rest


def capitalized(name):
    return name[:1].upper() + name[1:] if name[:1].islower() else name


_plain_names = {}


def plain_native_name(language):
    """The name a language gives itself without a country: Qt says 'español de España' for es and 'American
    English' for en, so take the name most of its countries use ('español', 'English')."""
    if language not in _plain_names:
        names = Counter(loc.nativeLanguageName() for loc in
                        QLocale.matchingLocales(language, QLocale.Script.AnyScript, _ANY_TERRITORY))
        names.pop("", None)
        top = max(names.values(), default=0)
        _plain_names[language] = min((n for n, c in names.items() if c == top), key=len, default="")
    return _plain_names[language]


def native_name(code):
    """'Español', 'Español (México)', 'Català (Espanya, valencia)', 'Srpski (Srbija)' for sr_Latn_RS; '' when Qt
    does not know the language."""
    lang, script, territory, rest = split_code(code)
    loc = QLocale(f"{lang}_{script}" if script else lang)
    if loc.language() in _UNKNOWN_LANGUAGES:
        return ""
    name = loc.nativeLanguageName() if script else plain_native_name(loc.language())
    if not name:
        return ""
    extra = []
    if territory:
        t = QLocale(f"{lang}_{territory}")
        if t.language() == loc.language() and QLocale.territoryToCode(t.territory()) == territory:
            extra.append(t.nativeTerritoryName())
        else:
            extra.append(territory)
    extra += rest
    name = capitalized(name)
    return f"{name} ({', '.join(extra)})" if extra else name


_zypp = None


def zypp_text(text):
    """zypper's names of languages and countries come from libzypp, which has them translated (domain 'zypp'):
    the name in the language of the window, without asking zypper again."""
    global _zypp
    if _zypp is None:
        _zypp = gettext.translation("zypp", fallback=True)
    return _zypp.gettext(text)


def window_name(code, english):
    """zypper's English name ('Spanish (Mexico)') in the language of the window ('Espanyol (Mèxic)'); '' when zypper
    does not know the language either."""
    if not english or english.startswith("Unknown language"):
        return ""
    lang_part, country = english, ""
    _l, _s, territory, rest = split_code(code)
    if (territory or rest) and english.endswith(")") and " (" in english:
        i = english.rfind(" (")
        lang_part, country = english[:i], english[i + 2:-1]
    name = zypp_text(lang_part)
    if country and not country.startswith("Unknown country"):
        return f"{name} ({zypp_text(country)})"
    return name


def base_of(code):
    return split_code(code)[0]


# ------------------------------------------------------------------ the list

def paint_locked(p, rect):
    """A grey box with a tick: there, and it cannot be unticked."""
    cr = QRectF(rect)
    p.save()
    p.setRenderHint(p.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(127, 140, 141, 110))
    p.drawRoundedRect(cr, 4, 4)
    pen = QPen(QColor("white"), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPointF(cr.left() + 4, cr.center().y()), QPointF(cr.left() + 7, cr.bottom() - 4),
                    QPointF(cr.right() - 3.5, cr.top() + 4.5)])
    p.restore()


class LanguageDelegate(QStyledItemDelegate):
    """A row: the box, the name in bold and the code in grey, and a short grey note on the right."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(200, max(34, option.fontMetrics.height() + 18))

    def paint(self, p, option, index):
        code = index.data(CODE_ROLE)
        if code is None or code not in self.page.entries:
            return super().paint(p, option, index)
        st = self.page.row_state(code)
        e = self.page.entries[code]
        pal = option.palette
        r = option.rect
        p.save()
        if option.state & QStyle.StateFlag.State_Selected:
            p.fillRect(r, QColor(39, 174, 96, 40))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.fillRect(r, QColor(39, 174, 96, 16))
        p.setPen(QColor(127, 140, 141, 55))
        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
        box = check_rect(r, 12)
        if st["locked"]:
            paint_locked(p, box)
        else:
            paint_check(p, box, st["checked"], pal)

        x = box.right() + 12
        right = r.right() - 12
        note = st["note"]
        if note:
            small = QFont(option.font)
            small.setPointSizeF(option.font.pointSizeF() * 0.9)
            sfm = QFontMetrics(small)
            nw = min(sfm.horizontalAdvance(note) + 2, max(60, (right - x) // 2))
            p.setFont(small)
            p.setPen(self._note_color(st["note_color"], pal))
            p.drawText(QRect(right - nw, r.top(), nw, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       sfm.elidedText(note, Qt.TextElideMode.ElideRight, nw))
            right -= nw + 12

        room = max(20, right - x)
        bold = QFont(option.font)
        bold.setBold(True)
        bfm = QFontMetrics(bold)
        name = bfm.elidedText(e["native"], Qt.TextElideMode.ElideRight, room)
        p.setFont(bold)
        p.setPen(pal.color(QPalette.ColorRole.Text))
        p.drawText(QRect(x, r.top(), room, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   name)
        if e["native"] != code:
            used = bfm.horizontalAdvance(name) + 6
            fm = QFontMetrics(option.font)
            p.setFont(option.font)
            p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            if room - used > 20:
                p.drawText(QRect(x + used, r.top(), room - used, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fm.elidedText(f"({code})", Qt.TextElideMode.ElideRight, room - used))
        p.restore()

    @staticmethod
    def _note_color(kind, pal):
        dark = pal.color(QPalette.ColorRole.Base).lightness() <= 128
        if kind in (NOTE_GREEN, NOTE_RED, NOTE_ORANGE):
            c = QColor({NOTE_GREEN: GREEN, NOTE_RED: RED, NOTE_ORANGE: ORANGE}[kind])
            return c.lighter(125) if dark else c.darker(125)
        return pal.color(QPalette.ColorRole.PlaceholderText)

    def helpEvent(self, event, view, option, index):
        """The whole row in a tooltip: in a narrow window the note is shortened with "…"."""
        code = index.data(CODE_ROLE)
        if event.type() == QEvent.Type.ToolTip and code in self.page.entries:
            e = self.page.entries[code]
            note = self.page.row_state(code)["note"]
            text = e["native"] + ("" if e["native"] == code else f" ({code})")
            QToolTip.showText(event.globalPos(), f"{text}\n{note}" if note else text, view)
            return True
        return super().helpEvent(event, view, option, index)

    def editorEvent(self, event, model, option, index):
        """A click on the box (or Space on the row) ticks or unticks it; a click elsewhere only shows its card."""
        code = index.data(CODE_ROLE)
        if code is None:
            return False
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton \
                and check_rect(option.rect, 12).adjusted(-6, -6, 6, 6).contains(event.position().toPoint()):
            self.page.toggle(code)
            return True
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            self.page.toggle(code)
            return True
        return False


# ------------------------------------------------------------------ the page

class LocalesPage(Page):
    key = "locales"
    title = _("Languages")
    icon_name = "preferences-desktop-locale"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.entries = {}        # code → dict(code, english, requested, native, local, base, sort, search)
        self.order = []          # every code, in the order of the names
        self.requested = set()
        self.system = ""         # the code of the language of the computer, as zypper knows it ('ca_ES')
        self.system_row = ""     # the row that stands for it: the code requested for it ('ca' or 'ca_ES')
        self.installed = None    # names of the installed packages (None: not read yet)
        self.details = {}        # (codes…) → (packages, error)
        self._asking = set()
        self._gen = 0            # a reload drops what the earlier one still hands back
        self._shown = []         # the codes in the list now
        self.current = ""
        self.mode = "have"
        self.load_error = ""

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(muted(_("The languages you want the translations of your programs in. Tick or untick them; the "
                              "changes go to the queue.")))

        # ------------------------------------------------ left: the languages
        left = Card(None, self)
        left.lay.setContentsMargins(0, 0, 0, 6)
        left.lay.setSpacing(0)
        bar = QWidget(left)
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(12, 10, 12, 10)
        bh.setSpacing(8)
        self.search = QLineEdit(bar)
        self.search.setPlaceholderText(_("Search for a language…"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setMinimumWidth(140)
        self.search.textEdited.connect(self._typed)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        bh.addWidget(self.search, 1)
        self.filters = QButtonGroup(self)
        self.filters.setExclusive(True)
        self.b_have = self._filter_button(_("The ones you have"), "have", bar)
        self.b_all = self._filter_button(pgettext("languages", "All"), "all", bar)
        self.b_have.setToolTip(_("Shows the languages zypper installs translations for, and the changes in the "
                                 "queue."))
        self.b_all.setToolTip(_("Shows every language zypper knows, to add one."))
        self.b_have.setChecked(True)
        bh.addWidget(self.b_have)
        bh.addWidget(self.b_all)
        left.lay.addWidget(bar)
        left.lay.addWidget(hline())

        self.list = QListWidget(left)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(LanguageDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setStyleSheet("QListWidget { background: transparent; } QListWidget::item { border: none; }")
        self.list.currentItemChanged.connect(lambda cur, _p: self._select(cur))
        left.lay.addWidget(self.list, 1)
        self.list_note = muted("", left)
        self.list_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_note.setContentsMargins(16, 16, 16, 16)
        self.list_note.hide()
        left.lay.addWidget(self.list_note, 1)  # in the place of the list while there is nothing to show
        self.more_hint = muted(_("To add another language, search for it or show them all."), left)
        self.more_hint.setContentsMargins(12, 8, 12, 2)
        self.more_hint.hide()
        left.lay.addWidget(self.more_hint)

        # ------------------------------------------------ right: the card
        right = Card(None, self)
        right.lay.setContentsMargins(18, 16, 18, 16)
        right.lay.setSpacing(10)
        right.setMinimumWidth(300)
        self.c_title = heading("", 1.4, right)
        self.c_title.setTextFormat(Qt.TextFormat.PlainText)
        self.c_title.setWordWrap(True)
        self.c_sub = muted("", right)
        self.c_sub.setTextFormat(Qt.TextFormat.PlainText)
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(self.c_title)
        names.addWidget(self.c_sub)
        right.lay.addLayout(names)
        self.grid_box = QWidget(right)
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 4, 0, 4)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(1, 1)
        self._kv_labels = []
        right.lay.addWidget(self.grid_box)
        self.c_means = plain_label("", right)
        self.c_means.setStyleSheet(INFO_BOX)
        right.lay.addWidget(self.c_means)
        brow = QHBoxLayout()
        # adding or removing a language only puts it in the queue: "To the queue:" says so, and it is never green
        self.c_queue = queue_label(right)
        brow.addWidget(self.c_queue)
        self.c_button = button("", "list-add", self._card_button)
        brow.addWidget(self.c_button)
        brow.addStretch(1)
        right.lay.addLayout(brow)
        self.c_command = command_label("", right)
        right.lay.addWidget(self.c_command)
        right.lay.addStretch(1)
        self.c_placeholder = muted(_("Choose a language on the left."), right)
        right.lay.insertWidget(0, self.c_placeholder)

        split = RoomSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 11)
        split.setStretchFactor(1, 10)
        split.setSizes([550, 500])  # the card is almost as wide as the list
        lay.addWidget(split, 1)

        self._search_timer = QTimer(self)  # refilling 500 rows on every key is wasted work: wait for a pause
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._fill)
        # 'zypper locales -p' only when the selection stops moving (the arrow keys would start one per row)
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(300)
        self._detail_timer.timeout.connect(self._ask_details)
        ctx.queue.changed.connect(self._queue_changed)
        self._set_list_note(_("Loading the languages…"))
        self._show_card()

    def _filter_button(self, text, mode, parent):
        b = QToolButton(parent)
        b.setText(text)
        b.setCheckable(True)
        b.setStyleSheet(FILTER_STYLE)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.toggled.connect(lambda on, m=mode: on and self._set_mode(m))
        self.filters.addButton(b)
        return b

    # ------------------------------------------------------------ data
    def reload(self):
        self._gen += 1
        gen = self._gen
        self.details.clear()
        self._asking.clear()
        self.installed = None
        run_async(all_locales, on_done=lambda res, err, g=gen: self._loaded(res, err, g))
        run_async(installed_names, on_done=lambda res, err, g=gen: self._installed_loaded(res, err, g))

    def _loaded(self, items, err, gen):
        if gen != self._gen:
            return
        if err:
            self.load_error = str(err)
            self.entries, self.order = {}, []
            self._set_list_note(_("zypper could not list the languages:") + "\n" + self.load_error)
            self._show_card()
            return
        self.load_error = ""
        self.requested = {d["code"] for d in items if d["requested"]}
        self.entries = {}
        for d in items:
            self._add_entry(d["code"], d["name"], d["requested"])
        system = system_language()
        if system and system not in self.entries:
            system = base_of(system) if base_of(system) in self.entries else system
        self.system = system
        if system and system not in self.entries:
            self._add_entry(system, "", False)
        # the row that stands for the language of the computer: the one requested for it (ca_ES, or only ca)
        self.system_row = system
        if system and system not in self.requested and base_of(system) in self.requested:
            self.system_row = base_of(system)
        for a in self.ctx.queue.of("locale-add", "locale-remove"):  # also codes zypper does not list
            if a.target not in self.entries:
                self._add_entry(a.target, "", False)
        self._sort()
        self.b_all.setText(_("All ({n})").format(n=len(self.entries)))
        if not self.current or self.current not in self.entries:
            self.current = self.system_row or ""
        self._fill(force=True)

    def _add_entry(self, code, english, requested):
        native = native_name(code)
        local = window_name(code, english)
        shown = native or local or english or code
        if shown.startswith("Unknown language"):
            shown = code
        search = " ".join(fold(x) for x in (shown, local, english, code) if x)
        self.entries[code] = dict(code=code, english=english, requested=requested, native=shown, local=local,
                                  base=base_of(code), sort=fold(shown), search=search,
                                  words=set(re.split(r"[\W_]+", search)) - {""})

    def _sort(self):
        self.order = sorted(self.entries, key=lambda c: (self.entries[c]["sort"], c.lower()))

    def _installed_loaded(self, names, err, gen):
        if gen != self._gen:
            return
        self.installed = names if not err else set()
        self._refresh_card()

    def _refresh_card(self):
        """The card again for the row selected now (none: the placeholder)."""
        self._show_card(self.current if self.list.currentItem() is not None else "")

    # ------------------------------------------------------------ states
    def _pending(self):
        return {a.target: a.kind for a in self.ctx.queue.of("locale-add", "locale-remove")}

    def locked(self, code):
        """English and the language of the computer (once requested) cannot be unticked."""
        return code == ENGLISH or (code == self.system_row and code in self.requested)

    def row_state(self, code, pending=None):
        pending = self._pending() if pending is None else pending
        kind = pending.get(code)
        requested = code in self.requested
        checked = (requested and kind != "locale-remove") or kind == "locale-add" or code == ENGLISH
        note, color = "", NOTE_GREY
        if kind == "locale-add":
            note, color = _("will be added"), NOTE_GREEN
        elif kind == "locale-remove":
            note, color = _("will be removed"), NOTE_RED
        elif code == self.system_row and self.system:
            if requested:
                note = _("the language of the computer")
            else:
                note, color = _("the language of the computer · not added yet"), NOTE_ORANGE
        elif code == ENGLISH:
            note = _("always there")
        elif requested:
            note = _("added by you")
        else:
            via = self._comes_with(code)
            if via:
                note = _("comes with {code}").format(code=via)
        return dict(checked=checked, locked=self.locked(code), note=note, note_color=color)

    def _comes_with(self, code):
        """The requested variant that already brings this language (ca_ES brings ca), or ''."""
        if "_" in code or "@" in code or "-" in code:
            return ""
        return next((r for r in sorted(self.requested) if r != code and base_of(r) == code
                     and r not in NO_BASE_FALLBACK), "")

    # ------------------------------------------------------------ the list
    def _typed(self, text):
        # what you have is a few rows: a search looks in every language
        if text.strip() and self.mode == "have":
            self.b_all.setChecked(True)

    def _set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "have" and self.search.text():
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self._fill()

    def _visible(self):
        if not self.entries:
            return []
        term = fold(self.search.text().strip())
        if term:
            # the code itself first, then its variants, then names that start with it ('es' finds Español before
            # English (Seychelles))
            ranked = []
            for i, c in enumerate(self.order):
                e = self.entries[c]
                if term not in e["search"]:
                    continue
                rank = 0 if c.lower() == term else 1 if e["base"] == term else \
                    2 if any(w.startswith(term) for w in e["words"]) else 3
                ranked.append((rank, i, c))
            return [c for _r, _i, c in sorted(ranked)]
        if self.mode == "all":
            return list(self.order)
        have = set(self.requested) | set(self._pending())
        if ENGLISH in self.entries:
            have.add(ENGLISH)
        first = [c for c in (self.system_row, ENGLISH) if c and c in self.entries]
        return list(dict.fromkeys(first + [c for c in self.order if c in have]))

    def _fill(self, force=False):
        codes = self._visible()
        self.more_hint.setVisible(bool(self.entries) and self.mode == "have" and not self.search.text().strip())
        if not force and codes == self._shown:
            self.list.viewport().update()
            return
        self._shown = codes
        self.list.blockSignals(True)
        self.list.clear()
        select = None
        for code in codes:
            e = self.entries[code]
            it = QListWidgetItem(f"{e['native']} ({code})" if e["native"] != code else code)
            it.setData(CODE_ROLE, code)
            self.list.addItem(it)
            if code == self.current:
                select = it
        if select is None and codes and not self.search.text().strip():
            select = self.list.item(0)
        if select is not None:
            self.list.setCurrentItem(select)
            self.list.scrollToItem(select)
        self.list.blockSignals(False)
        if self.entries and not codes:
            self._set_list_note(_("No language matches “{text}”.").format(text=self.search.text().strip()))
        elif self.entries:
            self._set_list_note("")
        self._select(select)

    def _set_list_note(self, text):
        self.list_note.setText(text)
        self.list_note.setVisible(bool(text))
        self.list.setVisible(not text)

    def _select(self, item):
        code = item.data(CODE_ROLE) if item is not None else ""
        if code:
            self.current = code
        self._show_card(code)
        if code:
            self._detail_timer.start()

    def _queue_changed(self):
        if not self.entries:
            return
        new = [a.target for a in self.ctx.queue.of("locale-add", "locale-remove") if a.target not in self.entries]
        for code in new:
            self._add_entry(code, "", False)
        if new:
            self._sort()
        self._fill()
        self._refresh_card()

    # ------------------------------------------------------------ ticking
    def toggle(self, code):
        """Tick or untick a language: the change goes to the queue (or comes out of it); nothing runs now."""
        if code not in self.entries or self.locked(code):
            return
        kind = self._pending().get(code)
        if kind:
            self.ctx.queue.discard(kind, code)
        else:
            self.ctx.queue.add(Action("locale-remove" if code in self.requested else "locale-add", code))

    def _card_button(self):
        if self.current:
            self.toggle(self.current)

    @staticmethod
    def command(kind, code):
        """The command the queue will run for this change (TxQueue.steps)."""
        sub = "addlocale" if kind == "locale-add" else "removelocale"
        return Step("zypper", ["-n", sub, "--", code]).display()

    # ------------------------------------------------------------ the card
    def _codes_for(self, code):
        """The languages a request for this code brings: a variant brings its language too (ca_ES: ca)."""
        base = base_of(code)
        if base != code and base in self.entries and code not in NO_BASE_FALLBACK:
            return (code, base)
        return (code,)

    def _ask_details(self):
        code = self.current
        if not code or code == ENGLISH or code not in self.entries:
            return
        codes = self._codes_for(code)
        if codes in self.details or codes in self._asking:
            return
        self._asking.add(codes)
        gen = self._gen
        run_async(packages_for, list(codes),
                  on_done=lambda res, err, c=codes, g=gen: self._details_loaded(c, res, err, g))

    def _details_loaded(self, codes, packages, err, gen):
        if gen != self._gen:
            return
        self._asking.discard(codes)
        self.details[codes] = (packages or {}, str(err) if err else "")
        if self.current and self._codes_for(self.current) == codes:
            self._show_card(self.current)

    def _kv(self, rows):
        """Name: value lines (the labels are reused while browsing)."""
        while len(self._kv_labels) < len(rows):
            i = len(self._kv_labels)
            key = muted("", self.grid_box, wrap=False)
            key.setTextFormat(Qt.TextFormat.PlainText)
            value = plain_label("", self.grid_box)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.grid.addWidget(key, i, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, i, 1)
            self._kv_labels.append((key, value))
        for i, (key, value) in enumerate(self._kv_labels):
            if i < len(rows):
                key.setText(rows[i][0])
                value.setText(rows[i][1])
                value.setToolTip(rows[i][2] if len(rows[i]) > 2 else "")
            key.setVisible(i < len(rows))
            value.setVisible(i < len(rows))
        self.grid_box.setVisible(bool(rows))

    def _show_card(self, code=""):
        e = self.entries.get(code) if code else None
        card = (self.c_title, self.c_sub, self.c_means, self.c_button, self.c_command, self.c_queue)
        if e is None:
            for w in card:
                w.hide()
            self._kv([])
            self.c_placeholder.setText(_("Choose a language on the left.") if self.entries or not self.load_error
                                       else "")
            self.c_placeholder.setVisible(bool(self.c_placeholder.text()))
            return
        self.c_placeholder.hide()
        pending = self._pending()
        kind = pending.get(code)
        requested = code in self.requested
        locked = self.locked(code)
        is_system = bool(self.system) and code == self.system_row

        self.c_title.setText(e["native"])
        sub = code
        if e["local"] and fold(e["local"]) != fold(e["native"]):
            sub = f"{e['local']} · {code}"
        self.c_sub.setText(sub)

        rows = [(_("Now"), self._now_text(code, requested, kind))]
        if code != ENGLISH:
            rows += self._package_rows(code, requested)
        variants = [c for c in self.order if c != code and base_of(c) == code]
        base = e["base"]
        if variants:
            shown = ", ".join(variants[:4]) + ("…" if len(variants) > 4 else "")
            rows.append((_("Variants"), _("{codes} ({n})").format(codes=shown, n=len(variants)), ", ".join(variants)))
        elif base != code and base in self.entries:
            rows.append((_("Variant of"), f"{self.entries[base]['native']} ({base})"))
        self._kv(rows)

        self.c_means.setText(self._means_text(code, requested, kind, is_system))
        self.c_means.show()
        if locked:
            self.c_button.hide()
            self.c_command.hide()
            self.c_queue.hide()
        else:
            name = amp(e["native"]) if len(e["native"]) <= 22 else ""
            if kind:  # it acts on the queue itself: no "To the queue:" in front
                self.c_button.setText(_("Take {language} out of the queue").format(language=name) if name else
                                      _("Take it out of the queue"))
                self.c_button.setIcon(icon("edit-undo"))
                self.c_button.setToolTip(_("Nothing changes for this language: the change comes out of the queue, "
                                           "now."))
                cmd_kind = kind
            elif requested:
                self.c_button.setText(pgettext("language", "Remove {language}").format(language=name) if name else
                                      pgettext("language", "Remove it"))
                self.c_button.setIcon(icon("list-remove"))
                self.c_button.setToolTip(_("Puts its removal in the queue: zypper no longer installs its "
                                           "translations once you apply it.") + "\n"
                                         + self.command("locale-remove", code))
                cmd_kind = "locale-remove"
            else:
                self.c_button.setText(pgettext("language", "Add {language}").format(language=name) if name else
                                      pgettext("language", "Add it"))
                self.c_button.setIcon(icon("list-add"))
                self.c_button.setToolTip(_("Puts it in the queue: zypper installs its translations when you apply "
                                           "it.") + "\n" + self.command("locale-add", code))
                cmd_kind = "locale-add"
            self.c_button.setProperty("primary", False)  # it only changes the queue: never the green button
            self.c_button.style().unpolish(self.c_button)
            self.c_button.style().polish(self.c_button)
            self.c_button.show()
            self.c_queue.setVisible(not kind)
            self.c_command.setText(self.command(cmd_kind, code))
            self.c_command.show()
        self.c_title.show()
        self.c_sub.show()

    @staticmethod
    def _now_text(code, requested, kind):
        if code == ENGLISH:
            return _("always there: the programs are written in English")
        if kind == "locale-add":
            return _("you don't have it; it is in the queue to be added")
        if kind == "locale-remove":
            return _("you have it; it is in the queue to be removed")
        return pgettext("language", "you have it") if requested else _("you don't have it")

    def _package_rows(self, code, requested):
        codes = self._codes_for(code)
        found = self.details.get(codes)
        label = _("Translations for your programs")
        if found is None or self.installed is None:
            return [(label, _("counting…"))]
        packages, err = found
        if err:
            return [(label, _("zypper could not tell: {error}").format(error=err.splitlines()[0][:200]))]
        w = what_it_brings(packages, self.installed)
        have, bring = len(w["have"]), len(w["bring"])
        parts = []
        if requested:
            parts.append(ngettext("{n} package installed", "{n} packages installed", have).format(n=have))
            if bring:
                parts.append(ngettext("{n} more not installed yet", "{n} more not installed yet", bring)
                             .format(n=bring))
        elif bring:
            parts.append(ngettext("{n} package (-lang) to install", "{n} packages (-lang) to install", bring)
                         .format(n=bring))
            if have:
                parts.append(ngettext("{n} you already have", "{n} you already have", have).format(n=have))
        elif have:
            parts.append(_("nothing more to install"))
            parts.append(ngettext("{n} you already have", "{n} you already have", have).format(n=have))
        else:
            parts.append(_("none for the programs you have"))
        tip = ""
        if bring:
            tip = "\n".join(w["bring"][:25]) + ("\n…" if bring > 25 else "")
        rows = [(label, " · ".join(parts), tip)]
        if w["dicts"]:
            names = ", ".join(_("{name} (installed)").format(name=n) if inst else n for n, inst in w["dicts"][:3])
            more = len(w["dicts"]) - 3
            if more > 0:
                names = ngettext("{names} and {n} more", "{names} and {n} more", more).format(names=names, n=more)
            rows.append((_("Dictionary and spell checker"), names, "\n".join(n for n, _i in w["dicts"])))
        return rows

    def _means_text(self, code, requested, kind, is_system):
        if code == ENGLISH:
            return _("The programs are written in English: it is always there and needs no translation packages.")
        window = _("The language of the window does not change: that is chosen in Plasma's settings.")
        if kind == "locale-add":
            text = _("When the queue is applied, zypper installs the translations of the programs you have into it, "
                     "and from then on those of the programs you install.") + " " + window
        elif is_system and requested:
            text = _("It is the language of the computer: zypper installs the translations into it for the programs "
                     "you have and for those you install. It cannot be unticked here.") + " " + window
        elif is_system:
            text = _("It is the language of the computer, but zypper has not been asked for it: the translations "
                     "into it do not come by themselves with the programs you install. Tick it to add them.")
        elif requested and kind != "locale-remove":
            text = _("zypper installs the translations into it for the programs you have and for those you install. "
                     "If you untick it, they are removed; the programs stay, in your other languages.")
        elif requested:
            text = _("When the queue is applied, the translations into it are removed; the programs stay, in your "
                     "other languages.")
        else:
            text = _("If you tick it, zypper installs the translations of the programs you have into it, and from now "
                     "on those of the programs you install.") + " " + window
        base = base_of(code)
        if base != code and base in self.entries and code not in NO_BASE_FALLBACK and not requested:
            text += " " + _("It also brings the translations into {language}.").format(
                language=f"{self.entries[base]['native']} ({base})")
        via = self._comes_with(code)
        if via and not requested:
            text += " " + _("Its translations already come with {code}, which you have.").format(code=via)
        return text
