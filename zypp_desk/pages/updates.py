# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates: your routine (run step by step), what there is to update with the package card next to it,
and a simulation of 'zypper dup' with its options in its own window.

Before a real 'zypper dup' the window asks with the numbers of a simulation:
the usual question; "a big update: shall we do it now?" when it is big or Tumbleweed came out
today; and "update in parts?" when it does not fit on the disk with room to spare (diskspace.py). Before that
question every part is simulated (heaps.review): what needs a decision goes to the full update at the end, and
the parts are made again with their real sizes. The routine asks too, before it runs, when the disk is clearly
too full for a big update downloaded at once."""

import datetime as dt
import html
import inspect
import math
import os
import re
import time

from PySide6.QtCore import QDateTime, QEvent, QLocale, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton, QStyle, QStyledItemDelegate,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import (appicons, backend, diskspace, firmware, flatpak, heaps, question, repostate, routine, websources,
               zyppconf)
from ..i18n import _, ngettext
from ..txqueue import Action
from ..runner import Step
from ..simulation import CATEGORIES, disk_text, human_size, parse_dry_run, summary_lines
from ..style import BLUE, GREEN, ORANGE, PURPLE, RED
from ..tasks import run_async
from ..ui import Card, Page, RoomSplitter, button, heading, icon, mono, muted, tool
from .pkgdetails import PackageDetails, table

FIRMWARE_COLOR = "#16a085"
ORIGIN_COLORS = {"repo": BLUE, "flatpak": PURPLE, "web": ORANGE, "firmware": FIRMWARE_COLOR}
ROW_ROLE = Qt.ItemDataRole.UserRole
# a warning inside the card: a tint of orange, readable in the light and the dark theme
WARN_HTML = ("<table width='100%' cellpadding='7' cellspacing='0' style='margin-top:8px;"
             "background-color:rgba(246,116,0,0.16)'><tr><td>⚠ {text}</td></tr></table>")


def is_checked(value):
    """A check state as PySide hands it back (the enum, or its number)."""
    return getattr(value, "value", value) == Qt.CheckState.Checked.value


# ------------------------------------------------------------------ the question before a real dup
# "A big update": this many packages changed, or this much to download
BIG_CHANGES = 300
BIG_DOWNLOAD = int(1.5 * diskspace.GiB)
# a Tumbleweed version younger than this is "less than a day old": its problems may not be known yet
FRESH_TW_HOURS = 24
# a simulation of the dup is used for the question when it is younger than this and for the same options
SIM_FRESH_S = 15 * 60
# the time it takes is only a rough guess: the speed of the connection is not known
ASSUMED_MBIT = 50
SECONDS_PER_PACKAGE = 0.5
REMIND_HOUR = 9
POWER_SUPPLY = "/sys/class/power_supply"
# well-known programs, to say what an update brings in words ("Firefox, KDE Plasma and the Linux kernel")
FRIENDLY = (("plasma6-workspace", "KDE Plasma"), ("plasma5-workspace", "KDE Plasma"), ("gnome-shell", "GNOME"),
            ("MozillaFirefox", "Firefox"), ("libreoffice", "LibreOffice"), ("MozillaThunderbird", "Thunderbird"),
            ("chromium", "Chromium"), ("gimp", "GIMP"), ("vlc", "VLC"))
KERNEL_RE = re.compile(r"^kernel-(default|longterm|rt|kvmsmall|vanilla|preempt|64kb|pae)$")


def _n(n):
    """A number as the user writes it (2.143 in Catalan)."""
    return QLocale().toString(int(n))


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def power_state(base=POWER_SUPPLY):
    """('mains', battery %), ('battery', battery %) or (None, None) when there is no battery (a desktop) or it
    cannot be read. The batteries of a mouse or a keyboard (scope Device) do not count."""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None, None
    mains, battery, pct = False, False, None
    for name in names:
        d = os.path.join(base, name)
        kind = _read(os.path.join(d, "type"))
        if kind in ("Mains", "USB"):
            mains = mains or _read(os.path.join(d, "online")) == "1"
        elif kind == "Battery" and _read(os.path.join(d, "scope")).lower() != "device":
            battery = True
            cap = _read(os.path.join(d, "capacity"))
            if cap.isdigit() and pct is None:
                pct = int(cap)
            if _read(os.path.join(d, "status")) == "Charging":
                mains = True
    if mains:
        return "mains", pct
    if battery:
        return "battery", pct
    return None, None


def tomorrow_morning(now=None):
    """Unix time of tomorrow at REMIND_HOUR, local time ("Remind me tomorrow")."""
    day = dt.date.fromtimestamp(now or time.time()) + dt.timedelta(days=1)
    return int(dt.datetime.combine(day, dt.time(REMIND_HOUR, 0)).timestamp())


def dup_facts(res, repos=None):
    """What the question needs besides the simulation (in a thread: rpm, statvfs, files): dict(space
    (diskspace.assess, or None), plan (heaps.plan when it does not fit, else None), power (power_state()),
    tw_hours (how old the Tumbleweed version is, or None), snapshots (snapper takes a snapshot before the
    update))."""
    facts = dict(space=None, plan=None, power=(None, None), tw_hours=None, snapshots=False)
    try:
        facts["space"] = diskspace.assess(res)
        facts["snapshots"] = facts["space"]["snapshots"]
        if not facts["space"]["fits"]:
            facts["plan"] = heaps.plan(res, facts["space"])  # in parts that fit, one after another
    except Exception:  # the question still comes, without the disk
        try:
            facts["snapshots"] = diskspace.snapshots_in_use()
        except Exception:
            pass
    try:
        facts["power"] = power_state()
    except Exception:
        pass
    try:
        facts["tw_hours"] = repostate.hours_since(repostate.tumbleweed_published(repos))
    except Exception:
        pass
    return facts


def _changes(res):
    return sum(len(v) for v in res.get("groups", {}).values())


def _download(res):
    try:
        return max(0, int(float(res.get("download") or 0)))
    except (TypeError, ValueError):
        return 0


def has_new_kernel(res):
    g = res.get("groups", {})
    return any(KERNEL_RE.match(it["name"]) for key in ("to-install", "to-upgrade") for it in g.get(key, []))


def highlights(items, limit=4):
    """The well-known programs among these packages, in words."""
    names = {it["name"] for it in items}
    out = []
    for pkg, shown in FRIENDLY:
        if pkg in names and shown not in out:
            out.append(shown)
    if any(KERNEL_RE.match(n) for n in names):
        out.append(_("the Linux kernel"))
    return out[:limit]


def _round_minutes(m):
    m = max(1.0, m)
    return int(math.ceil(m)) if m < 8 else int(5 * math.ceil(m / 5))


def minutes(res):
    """(download, whole update) in minutes: a rough guess at ASSUMED_MBIT and SECONDS_PER_PACKAGE."""
    dl = _download(res) * 8 / (ASSUMED_MBIT * 1e6) / 60
    install = _changes(res) * SECONDS_PER_PACKAGE / 60 + 2  # + the initrd and the boot loader at the end
    return _round_minutes(dl), _round_minutes(dl + install)


def restart_text(res):
    if has_new_kernel(res):
        return _("At the end you will need to restart: there is a new Linux kernel.")
    return _("At the end a restart may be needed.")


def _line(it):
    if it.get("old"):
        return f"{it['name']}  {it['old']} → {it['edition']}"
    return f"{it['name']}  {it['edition']}"


def change_items(res, short=False):
    """The changes of a simulation by kind, as question items (icon, title, detail, names). short: the usual
    question ("Among them Firefox…"); else "Firefox, … and 2,139 more"."""
    g = res.get("groups", {})
    out = []
    up = g.get("to-upgrade", [])
    if up:
        hl = highlights(up)
        rest = len(up) - len(hl)
        if not hl:
            detail = ""
        elif short:
            detail = _("Among them {names}.").format(names=", ".join(hl))
        elif rest > 0:
            detail = ngettext("{names} and {n} more.", "{names} and {n} more.", rest).format(
                names=", ".join(hl), n=_n(rest))
        else:
            detail = ", ".join(hl) + "."
        out.append(("system-software-update", ngettext("Update {n} program", "Update {n} programs", len(up)).format(
            n=_n(len(up))), detail, [_line(it) for it in up]))
    kinds = [
        ("to-install", "list-add", lambda n: ngettext("Install {n} new package", "Install {n} new packages", n),
         _("The update brings them in.")),
        ("to-remove", "list-remove", lambda n: ngettext("Remove {n} old package", "Remove {n} old packages", n),
         _("The update takes them out, usually because newer ones replace them.")),
        ("to-downgrade", "go-down|arrow-down", lambda n: ngettext("Go back to an older version of {n} package",
                                                                  "Go back to an older version of {n} packages", n),
         _("A repository only has an older version of them.")),
        ("to-change-vendor", "view-refresh", lambda n: ngettext("Change where {n} package comes from",
                                                                 "Change where {n} packages come from", n),
         _("They move to another vendor, for example from openSUSE to Packman.")),
        ("to-change-arch", "cpu|computer", lambda n: ngettext("Change the architecture of {n} package",
                                                              "Change the architecture of {n} packages", n), ""),
        ("to-reinstall", "view-refresh", lambda n: ngettext("Reinstall {n} package", "Reinstall {n} packages", n), ""),
    ]
    for key, ic, title, detail in kinds:
        items = g.get(key, [])
        if items:
            out.append((ic, title(len(items)).format(n=_n(len(items))), detail, [_line(it) for it in items]))
    return out


def change_summary(res):
    """Every change of a simulation in one item ("2,143 updates · 37 new packages · 12 to remove"), with all the
    names under '▸ Show them': the question about the room on the disk puts its reason first, and the changes
    must not push it below the fold."""
    g = res.get("groups", {})
    up = g.get("to-upgrade", [])
    counts = []
    if up:
        counts.append(ngettext("{n} update", "{n} updates", len(up)).format(n=_n(len(up))))
    n_in, n_rm = len(g.get("to-install", [])), len(g.get("to-remove", []))
    if n_in:
        counts.append(ngettext("{n} new package", "{n} new packages", n_in).format(n=_n(n_in)))
    if n_rm:
        counts.append(ngettext("{n} package to remove", "{n} packages to remove", n_rm).format(n=_n(n_rm)))
    other = sum(len(g.get(k, [])) for k in ("to-downgrade", "to-change-vendor", "to-change-arch", "to-reinstall"))
    if other:
        counts.append(ngettext("{n} other change", "{n} other changes", other).format(n=_n(other)))
    mark = {"to-install": "+ ", "to-remove": "− "}
    lines = [mark.get(key, "") + _line(it) for key, _label in CATEGORIES for it in g.get(key, [])]
    hl = highlights(up)
    detail = _("Among them {names}.").format(names=", ".join(hl)) if hl else ""
    return question.item("system-software-update", " · ".join(counts), detail, more=lines)


def download_item(res, detail=None):
    size = _download(res)
    if size < diskspace.MiB:
        return ("folder-download", _("Nothing to download"), _("Everything it needs is already downloaded."))
    return ("folder-download", _("Download {size}").format(size=human_size(size)),
            detail or _("From your repositories."))


def _copy(st):
    """A new Step with the same command (a Step keeps its state and output: each run gets its own). The title and
    the short command of the routine band (band_title, band_cmd) go with it."""
    new = Step(st.program, list(st.args), cond=st.cond, root=st.root, lang=st.lang, capture=st.capture,
               strict=st.strict, shown=st.shown)
    for attr in ("band_title", "band_cmd"):
        if hasattr(st, attr):
            setattr(new, attr, getattr(st, attr))
    return new


def _more(n):
    return _("… and {n} more").format(n=_n(n))


def part_steps(plan, opts):
    """The commands of the parts of a heaps.plan(): 'zypper -n -v update --download-in-advance … -- names',
    each one only if the one before worked. On screen the list of names is shortened; the command has them
    all. The routine band shows "Part 1 of 3 · 1,041 packages" and the command without the names."""
    out = []
    n = len(plan["parts"])
    for i, part in enumerate(plan["parts"], 1):
        args = ["-n", "-v", *(opts.get("globals") or ()), *heaps.part_args(part["names"], opts)]
        head = args[:args.index("--") + 1]
        st = Step("zypper", args, shown=heaps.shown(["zypper", *head], part["names"], _more))
        count = len(part["names"])
        st.band_title = ngettext("Part {i} of {n} · {count} package", "Part {i} of {n} · {count} packages",
                                 count).format(i=i, n=n, count=_n(count))
        st.band_cmd = " ".join(["zypper", *head, "…"])
        out.append(st)
    return out


def final_step(now):
    """The full update after the parts: only if every part worked (a routine's ';' before
    its dup must not run the whole download-at-once update after a part failed)."""
    st = _copy(now)
    st.cond = "success"
    st.band_title = _("Finish: update the rest (dup)")
    return st


def moved_text(names):
    """The packages that heaps.review() left to the full update at the end, in a sentence."""
    n = len(names)
    shown = ", ".join(names[:3]) + ("…" if n > 3 else "")
    return ngettext("{names} is left to the full update at the end: its update needs a decision that only a "
                    "full update makes.",
                    "{n} packages are left to the full update at the end ({names}): their update needs a "
                    "decision that only a full update makes, or goes with a package that does.", n).format(
        n=_n(n), names=shown)


def free_steps(free):
    """The cleaning commands for a diskspace.freeable() dict: the package cache only ('zypper clean' without -m,
    -M or -a keeps the repository metadata) and old kernels."""
    out = []
    if "clean" in free["steps"]:
        out.append(Step("zypper", ["-n", "clean"], cond="always"))
    if "purge-kernels" in free["steps"]:
        out.append(Step("zypper", ["-n", "purge-kernels"], cond="always"))
    return out


def free_card(free, after):
    """'First, free up space': what can go, and a button. (widget, button); the button is never the default."""
    parts = []
    if "clean" in free["steps"]:
        parts.append(_("Packages downloaded before: {size}").format(size=human_size(free["cache"]["bytes"])))
    k = free["kernels"]
    if k["items"]:
        n = k["count"] or len(k["items"])
        parts.append(ngettext("Old kernels: {n} ({size})", "Old kernels: {n} ({size})", n).format(
            n=n, size=human_size(k["bytes"])))
    lines = [" · ".join(parts)]
    planned = free["cache"]["planned_bytes"] if "clean" in free["steps"] else 0
    if planned:
        lines.append(_("{size} of those packages are for this update: they will be downloaded again.").format(
            size=human_size(planned)))
    lines.append(after)
    btn = QPushButton(_("Free {size}").format(size=human_size(free["bytes"])))
    btn.setAutoDefault(False)  # Enter never runs a change as root
    btn.setDefault(False)
    btn.setToolTip(_("Frees the space now, with the administrator password; then you are asked again.") + "\n"
                   + "\n".join(st.display() for st in free_steps(free)))
    return question.item("edit-clear|edit-delete", _("First, free up space"), "\n".join(lines), right=btn), btn


def _space_box(title, root, extra, caption, rows, tip=None):
    """A box of reasons with the disk bar and its caption under the title (question.reasons + question.Bar)."""
    box = question.reasons(title, rows, tip)
    lay = box.layout()
    lay.insertWidget(1, question.Bar(root["total"] - root["free"], extra, root["total"]))
    lay.insertWidget(2, question.label(caption, small=True, muted=True))
    return box


def wait_reasons(res, facts, big, tip=True):
    """'Is it worth waiting?': the size and a rough time, how old Tumbleweed is, the charger, and a tip."""
    rows = []
    _dl, total = minutes(res)
    took = ngettext("It will take roughly {n} minute.", "It will take roughly {n} minutes.", total).format(n=total)
    if big:
        rows.append((ORANGE, _("It is a big update"), took + " " + restart_text(res)))
    else:
        rows.append((GREEN, _("It is not a big update"), took + " " + restart_text(res)))
    tw = facts.get("tw_hours")
    if tw is not None:
        if tw < FRESH_TW_HOURS:
            h = max(1, int(tw))
            text = ngettext("It came out {n} hour ago. If it has problems, they are usually known and fixed within "
                            "a day or two.",
                            "It came out {n} hours ago. If it has problems, they are usually known and fixed "
                            "within a day or two.", h).format(n=h)
            if tw < repostate.MIRROR_SETTLE_HOURS:
                text += " " + _("The mirrors may still be copying it, so some downloads can fail for a few hours.")
            rows.append((ORANGE, _("This Tumbleweed version is less than a day old"), text))
        else:
            d = int(tw // 24)
            rows.append((GREEN, ngettext("This Tumbleweed version came out {n} day ago",
                                         "This Tumbleweed version came out {n} days ago", d).format(n=d),
                         _("If it had a serious problem, it would usually be known by now.")))
    kind, pct = facts.get("power") or (None, None)
    if kind == "mains":
        rows.append((GREEN, _("The charger is plugged in"),
                     _("Good: the battery will not run out halfway through the update.")))
    elif kind == "battery":
        rows.append((RED if pct is not None and pct < 30 else ORANGE,
                     _("The computer is on battery ({pct}%)").format(pct=pct) if pct is not None
                     else _("The computer is on battery"),
                     _("Plug in the charger first: if the battery runs out halfway, the system can be left half "
                       "updated.")))
    hint = (_("Tip: if you are not in a hurry, download it now and install it when you finish working. "
              "Installing it later is much quicker.") if tip else None)
    return question.reasons(_("Is it worth waiting?"), rows, hint)


def dup_question(parent, res, steps, facts):
    """The question before a real 'zypper dup'. steps: dict(now, download, parts=[Step…] (the parts and the final
    dup), free=[Step…]): the commands each answer runs; facts: dup_facts() (with the plan that heaps.review()
    checked). Returns a question.Question whose exec() gives 'now', 'download', 'parts', 'free', 'remind',
    'history', 'no' or None; its .kind is 'D' (the usual one), 'G' (big, or a brand-new Tumbleweed) or 'H' (it
    does not fit on the disk with room to spare: in parts, or not at all)."""
    space = facts.get("space")
    download = _download(res)
    big = _changes(res) >= BIG_CHANGES or download >= BIG_DOWNLOAD
    tw = facts.get("tw_hours")
    fresh = tw is not None and tw < FRESH_TW_HOURS
    low = space is not None and not space["fits"]
    free_btn = None
    if low:
        kind = "H"
        root = space["root"]
        hs = human_size
        caption = _("Used {used} · free {free} · this update needs {download} to download and {growth} more once "
                    "installed").format(used=hs(root["used"]), free=hs(root["free"]), download=hs(space["download"]),
                                        growth=hs(space["growth"]))
        if not space["same"]:
            caption += "\n" + _("The downloads go to {folder}, on another disk: {free} free there.").format(
                folder=space["cache"]["mount"], free=hs(space["cache"]["free"]))
        if space["snapshots"]:
            caption += "\n" + _("The snapshot taken before the update keeps the old files, so the new ones take "
                                "their whole size.")
        plan = facts.get("plan") or dict(parts=[], feasible=False, reason="none", need=0, short=0)
        feasible = plan["feasible"] and bool(steps.get("parts"))
        # a small update on a nearly full disk: it fits, only not with the room to spare
        tight = not feasible and bool(space.get("fits_tight")) and steps.get("now") is not None
        rows = [] if tight else [(ORANGE, _("There is not enough room to download it all at once"),
                                  _("If the disk fills up halfway, the system can be left half updated."))]
        if tight:
            need = space["need_all"]
            rows.append((ORANGE, _("The update itself fits"),
                         _("It needs about {need} and there are {free} free, so about {left} would be left: less "
                           "than the {margin} that Zypp Desk keeps free as room to spare.").format(
                             need=hs(need), free=hs(root["free"]), left=hs(max(0, root["free"] - need)),
                             margin=hs(space["margin"]))))
            rows.append((RED, _("The disk is nearly full"),
                         _("Free up space soon, for example by deleting old snapshots in History.")
                         if space["snapshots"] else _("Free up space soon: other programs need room too.")))
        elif feasible:
            n = len(plan["parts"])
            text = _("Each part is downloaded completely before it is installed, so if a download fails nothing of "
                     "that part is installed, and the system stays as the previous part left it. Then a normal "
                     "update finishes what is left.")
            if space["snapshots"]:
                text += " " + _("Each part makes its own pair of snapshots.")
            if plan.get("moved"):
                text += " " + moved_text(plan["moved"])
            rows.append((GREEN, ngettext("It can be done in {n} part", "It can be done in {n} parts", n).format(n=n),
                         text))
        else:
            reason = plan.get("reason")
            if reason == "none":
                text = _("Its changes cannot be split into parts.")
            elif reason == "many":
                text = _("It would need more than {n} parts.").format(n=heaps.MAX_PARTS)
            elif reason == "rounds":
                text = _("zypper kept finding problems in the parts, so Zypp Desk could not find parts that work.")
            else:
                text = _("Even split into parts, at least {short} more free space is needed, counting the {margin} "
                         "that Zypp Desk keeps free as room to spare.").format(
                    short=hs(plan.get("short") or space["short"]), margin=hs(space["margin"]))
            if space["snapshots"]:
                text += " " + _("Free up space first, for example by deleting old snapshots in History.")
            elif not steps.get("free"):
                text += " " + _("Free up space first.")
            rows.append((RED, _("Even in parts there is not enough room"), text))
        extra = [_space_box(_("There is little room on the system disk"), root,
                            space["need_all"] if space["same"] else space["growth"], caption, rows),
                 change_summary(res)]
        cmds = []
        if feasible:
            cmds.append((_("Yes, update in parts:"), [st.display() for st in steps["parts"]]))
        if tight:
            cmds.append((_("Update everything anyway:"), [steps["now"].display()]))
        if steps.get("free"):
            card, free_btn = free_card(space["free"], _("After that it should fit all at once. The update is "
                                                         "simulated again and you are asked again.")
                                       if space["fits_after_free"] else
                                       _("Then the update is simulated again and you are asked again."))
            extra.append(card)
            cmds.append((_("First, free up space:"), [st.display() for st in steps["free"]]))
        buttons = [("no", _("Not yet"), False)]
        if feasible:
            if big or fresh:
                extra.append(wait_reasons(res, facts, big, tip=False))
                buttons.append(("remind", _("Remind me tomorrow"), False))
            buttons.append(("parts", _("Yes, update in parts"), True))
            title = _("Update in parts?")
        else:
            # nothing to say yes to: what can be done is freeing space, or updating anyway
            if space["snapshots"]:
                buttons.append(("history", _("Open History"), False))
            if tight:
                buttons.append(("now", _("Update everything anyway"), False))
            title = (_("Very little room on the disk: update anyway?") if tight else
                     _("There is not enough room for this update"))
        dlg = question.Question(parent, title, icon_name="drive-harddisk|computer", extra=extra,
                                commands_=cmds or None, buttons=buttons, width=740, help_topic="update",
                                password=feasible or tight or bool(steps.get("free")))
    elif big or fresh:
        kind = "G"
        dl_min, _total = minutes(res)
        items = change_items(res) + [download_item(res, ngettext(
            "About {n} minute at {speed} Mbit/s.", "About {n} minutes at {speed} Mbit/s.", dl_min).format(
            n=dl_min, speed=ASSUMED_MBIT))]
        downloaded = download < diskspace.MiB
        # always a plain way out that changes nothing, and Enter takes it
        if downloaded:
            cmds = [steps["now"].display()]
            buttons = [("no", _("Not yet"), False), ("remind", _("Remind me tomorrow"), False),
                       ("now", _("Update everything now"), True)]
        else:
            cmds = [(_("Only download the update:"), [steps["download"].display()]),
                    (_("Update everything now:"), [steps["now"].display()])]
            buttons = [("no", _("Not yet"), False), ("remind", _("Remind me tomorrow"), False),
                       ("download", _("Only download the update"), False), ("now", _("Update everything now"), True)]
        title = _("A big update: shall we do it now?") if big else _("A brand-new Tumbleweed: update now?")
        dlg = question.Question(parent, title, items=items, extra=[wait_reasons(res, facts, big, tip=not downloaded)],
                                commands_=cmds, buttons=buttons, width=760, help_topic="update")
    else:
        kind = "D"
        means = [_("it will take a few minutes and you can keep working."), restart_text(res)]
        if facts.get("snapshots"):
            means.append(_("If something goes wrong, the snapshot from before lets you go back."))
        warning = _("Do not turn the computer off while it updates.")
        if (facts.get("power") or (None,))[0] == "battery":
            warning += " " + _("It is on battery: plug in the charger first.")
        dlg = question.Question(parent, _("Update the whole system now?"),
                                items=change_items(res, short=True) + [download_item(res)], means=" ".join(means),
                                warning=warning, commands_=[steps["now"].display()],
                                buttons=[("no", _("Not yet"), False), ("now", _("Yes, update everything"), True)],
                                width=680, help_topic="update")
    if free_btn is not None:
        free_btn.clicked.connect(lambda: dlg.press("free"))
    dlg.kind = kind
    return dlg


def routine_question(parent, info, usual, parts, cleaning):
    """Before the routine, when the disk is clearly too full for a big update downloaded at once (there is no
    simulation, so the threshold is a conservative guess and the question says so). info: diskspace.
    routine_space(); usual: the steps as saved; parts: the commands that run first when it goes in parts (the
    refresh and the simulation); cleaning: free_steps(). exec() gives 'parts', 'usual', 'free', 'no' or None."""
    root = info["root"]
    hs = human_size
    caption = _("Used {used} · free {free}").format(used=hs(root["used"]), free=hs(root["free"]))
    if not info["same"]:
        caption += "\n" + _("The downloads go to {folder}, on another disk: {free} free there.").format(
            folder=info["cache"]["mount"], free=hs(info["cache"]["free"]))
    rows = [(ORANGE, _("Your routine downloads the whole update first"),
             _("Without a simulation it is not known how big this update is, but a big Tumbleweed update can need "
               "{size} or more at once, and there are {free} free.").format(size=hs(info["threshold"]),
                                                                            free=hs(info["free_now"]))),
            (GREEN, _("This time it can go in parts"),
             _("First the repositories are refreshed. Then the update is simulated and split into parts that fit, "
               "and you are asked before anything is installed. Your saved routine does not change."))]
    tip = _("To know exactly how much room it needs, simulate it first with “Simulate updating everything…”.")
    extra = [_space_box(_("There is little room on the system disk"), root, 0, caption, rows, tip)]
    cmds = [(_("Yes, update in parts:"), list(parts)), (_("Run the routine as usual:"), [st.display() for st in usual])]
    free_btn = None
    if cleaning:
        card, free_btn = free_card(info["free"], _("Then the free space is checked again, and your routine runs."))
        extra.append(card)
        cmds.append((_("First, free up space:"), [st.display() for st in cleaning]))
    dlg = question.Question(parent, _("Little room on the disk: run the routine in parts?"),
                            icon_name="drive-harddisk|computer", extra=extra, commands_=cmds,
                            buttons=[("no", _("Not yet"), False), ("usual", _("Run the routine as usual"), False),
                                     ("parts", _("Yes, update in parts"), True)], width=740, help_topic="update")
    if free_btn is not None:
        free_btn.clicked.connect(lambda: dlg.press("free"))
    return dlg


# ------------------------------------------------------------------ what the buttons under the list act on
# a longer name makes the label too long (about 30 characters in English): the label says "the ticked one"
NAME_MAX = 18


def tick_labels(rows):
    """(simulate, to the queue, only) labels for the ticked rows: "Simulate only digikam", "Put digikam in the
    queue", "Only digikam"; with several, the count ("Simulate the 3 ticked")."""
    n = len(rows)
    if n == 0:
        return _("Simulate the ticked ones"), _("Put the ticked ones in the queue"), _("Only the ticked ones")
    if n == 1 and len(rows[0]["name"]) <= NAME_MAX:
        name = rows[0]["name"]
        return (_("Simulate only {name}").format(name=name), _("Put {name} in the queue").format(name=name),
                _("Only {name}").format(name=name))
    if n == 1:
        return _("Simulate the ticked one"), _("Put the ticked one in the queue"), _("Only the ticked one")
    return (ngettext("Simulate the {n} ticked", "Simulate the {n} ticked", n).format(n=_n(n)),
            ngettext("Put the {n} ticked in the queue", "Put the {n} ticked in the queue", n).format(n=_n(n)),
            ngettext("Only the {n} ticked", "Only the {n} ticked", n).format(n=_n(n)))


def names_text(rows, limit=4):
    """'digikam, showfoto … and 3 more'."""
    names = [r["name"] for r in rows]
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + " " + _more(len(names) - limit)


def whole_question(parent, ticked, beyond, whole, steps, queue_cmds):
    """Before "Update everything" when some rows are ticked and the routine would also update rows you
    unticked: the routine does not look at the ticks. ticked: the ticked rows; beyond: the unticked rows it
    updates too; whole: how many rows it updates; steps: the routine; queue_cmds: the queue's commands for the
    ticked ones. exec() gives 'no' (Enter), 'only' (the ticked ones go to the queue, then the usual Apply
    question), 'all' or None."""
    n_t, n_b = len(ticked), len(beyond)
    _sim, _queue, only = tick_labels(ticked)
    items = [
        question.item("checkbox|emblem-checked|dialog-ok",
                      ngettext("You ticked {n} update", "You ticked {n} updates", n_t).format(n=_n(n_t)),
                      names_text(ticked), more=[r["name"] for r in ticked] if n_t > 4 else None),
        question.item("system-software-update",
                      ngettext("Your routine updates everything: {n} update",
                               "Your routine updates everything: {n} updates", whole).format(n=_n(whole)),
                      ngettext("The {n} you unticked is updated too: {names}.",
                               "The {n} you unticked are updated too: {names}.", n_b).format(
                          n=_n(n_b), names=names_text(beyond)),
                      more=[r["name"] for r in beyond] if n_b > 4 else None),
    ]
    means = _("“Update everything” runs your routine as it is, and the routine does not look at the ticks. To "
              "update only what you ticked, choose “{only}”: it goes to the queue, and you are asked before "
              "anything changes.").format(only=only)
    cmds = [(_("Yes, update everything:"), [st.display() for st in steps])]
    if queue_cmds:
        cmds.append((_("{only}, through the queue:").format(only=only), list(queue_cmds)))
    return question.Question(parent, _("Update the whole system?"), items=items, means=means, commands_=cmds,
                             buttons=[("no", _("Not yet"), False, _("Close this question. Nothing changes.")),
                                      ("only", only, False,
                                       _("Puts {names} in the queue, then asks you as usual before anything "
                                         "changes.").format(names=names_text(ticked))),
                                      ("all", _("Yes, update everything"), True,
                                       _("Runs your routine now, which updates the whole system:") + "\n"
                                       + "\n".join(st.display() for st in steps))],
                             width=680, help_topic="update")


class UpdateDelegate(QStyledItemDelegate):
    """A row of the list: tick, icon, name in bold, 'old → new' below, where it comes from on the right.
    Group titles are small grey capitals."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        row = index.data(ROW_ROLE)
        return QSize(200, 30 if row and row.get("header") else 54)

    @staticmethod
    def check_rect(r):
        return QRect(r.left() + 10, r.center().y() - 8, 16, 16)

    def paint(self, p, option, index):
        row = index.data(ROW_ROLE)
        if not row:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        pal = option.palette
        if row.get("header"):
            f = QFont(option.font)
            f.setPointSizeF(option.font.pointSizeF() * 0.85)
            f.setBold(True)
            f.setCapitalization(QFont.Capitalization.AllUppercase)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            p.setFont(f)
            p.setPen(pal.placeholderText().color())
            p.drawText(r.adjusted(8, 0, 0, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, row["header"])
            p.restore()
            return
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 60))
            p.drawRoundedRect(r, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(39, 174, 96, 22))
            p.drawRoundedRect(r, 10, 10)
        x = r.left()
        if not row.get("error"):
            cr = QRectF(self.check_rect(r))
            if is_checked(index.data(Qt.ItemDataRole.CheckStateRole)):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(GREEN))
                p.drawRoundedRect(cr, 4, 4)
                pen = QPen(QColor("white"), 2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.drawPolyline([QPointF(cr.left() + 4, cr.center().y()), QPointF(cr.left() + 7, cr.bottom() - 4),
                                QPointF(cr.right() - 3.5, cr.top() + 4.5)])
            else:
                p.setPen(QPen(pal.placeholderText().color(), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(cr.adjusted(0.7, 0.7, -0.7, -0.7), 4, 4)
        self.page.row_icon(row).paint(p, QRect(x + 36, r.center().y() - 16, 32, 32))
        tx = x + 78
        # the pill on the right
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        sfm = QFontMetrics(small)
        pill = row["where"] if not row.get("error") else _("could not check")
        pill = sfm.elidedText(pill, Qt.TextElideMode.ElideMiddle, 230)
        pw = sfm.horizontalAdvance(pill) + 18
        color = QColor("#7f8c8d" if row.get("error") else ORIGIN_COLORS.get(row["kind"], BLUE))
        pr = QRect(r.right() - 8 - pw, r.center().y() - 11, pw, 22)
        bg = QColor(color)
        bg.setAlpha(40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(pr, 11, 11)
        p.setPen(color)
        p.setFont(small)
        p.drawText(pr, Qt.AlignmentFlag.AlignCenter, pill)
        # name and versions
        room = pr.left() - tx - 10
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(pal.text().color())
        p.drawText(QRect(tx, r.top() + 6, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(bold).elidedText(row["name"], Qt.TextElideMode.ElideRight, room))
        p.setFont(mono())
        p.setPen(pal.placeholderText().color())
        p.drawText(QRect(tx, r.top() + 26, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(mono()).elidedText(row["version"], Qt.TextElideMode.ElideRight, room))
        p.restore()

    def editorEvent(self, event, model, option, index):
        row = index.data(ROW_ROLE)
        if not row or row.get("header") or row.get("error"):
            return False
        toggle = False
        if event.type() == QEvent.Type.MouseButtonRelease and self.check_rect(option.rect.adjusted(4, 2, -4, -2)) \
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

STATE_STYLE = {
    "pending": ("", "palette(mid)", ""),
    "running": ("…", "palette(highlight)", "rgba(61,174,233,0.14)"),
    "done": ("✓", "#27ae60", "rgba(39,174,96,0.08)"),
    "failed": ("✕", "#da4453", "rgba(218,68,83,0.08)"),
    "skipped": ("–", "palette(mid)", ""),
    # "Stop after this step" was pressed: this step will not run
    "stopping": ("–", ORANGE, "rgba(246,116,0,0.10)"),
}
# "Will stop after this step": the pressed Stop button keeps saying what it set, in orange, until the run ends
STOPPING_CSS = (f"QPushButton:disabled {{ color: {ORANGE}; border: 1px solid {ORANGE}; border-radius: 7px; "
                "background: rgba(246, 116, 0, 0.12); padding: 5px 12px; }")


class StepBox(QFrame):
    def __init__(self, number, step, parent=None):
        super().__init__(parent)
        self.setObjectName("step")
        self.number = number
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 10, 6)
        self.badge = QLabel(str(number), self)
        self.badge.setFixedSize(24, 24)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(1)
        # a part of an update in parts says which part it is, and its command leaves the names to the tooltip and
        # the terminal
        title = QLabel(getattr(step, "band_title", None) or routine.describe(step), self)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        title.setWordWrap(True)  # German titles are long: they wrap instead of widening the window
        cmd = QLabel(getattr(step, "band_cmd", None) or step.display(), self)
        cmd.setTextFormat(Qt.TextFormat.PlainText)
        cmd.setFont(mono())
        cmd.setStyleSheet("color: palette(link);")
        cmd.setToolTip(step.display())
        nb = "\u00a0"  # keep "( ; )" together when the line wraps
        cond = (_("runs even if the previous one failed ( ; )").replace("( ; )", f"({nb};{nb})")
                if step.cond == "always" and number > 1 else
                _("only if the previous one worked ( && )").replace("( && )", f"({nb}&&{nb})")
                if step.cond == "success" else "")
        cmd.setWordWrap(True)
        self.state_lbl = muted(cond, self)
        v.addWidget(title)
        v.addWidget(cmd)
        v.addWidget(self.state_lbl)
        h.addLayout(v, 1)
        self.set_state("pending")

    def set_state(self, state, text=None):
        mark, border, bg = STATE_STYLE.get(state, STATE_STYLE["pending"])
        self.setStyleSheet(f"#step {{ border: 1px solid {border}; border-radius: 5px; background: {bg or 'none'}; }}")
        color = border if state != "pending" else "palette(highlight)"
        self.badge.setText(mark or str(self.number))
        self.badge.setStyleSheet(f"border-radius: 12px; background: {color}; color: white; font-weight: bold;")
        if text:
            self.state_lbl.setText(text)


class UpdatesPage(Page):
    key = "updates"
    title = _("Updates")
    icon_name = "system-software-update"
    # what the list found, each time it fills: {"total", "repo", "flatpak", "web", "firmware", "busy",
    # "pending" (parts still looking), "errors", "flatpak_ids"}; the Home page shows the same numbers
    counted = Signal(object)

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)

        # ------------------------------------------------ routine: one band
        rc = Card(None, self)
        rc.lay.setContentsMargins(16, 10, 16, 10)
        head = QHBoxLayout()
        self.routine_lbl = heading(_("Your routine"), 1.05, self)  # the whole routine is in its tooltip
        head.addWidget(self.routine_lbl)
        head.addStretch(1)
        info = QLabel(self)
        info.setPixmap(icon("help-about", "dialog-information").pixmap(18, 18))
        info.setToolTip(_("Nobody can answer zypper in the middle of a routine, so it runs with -n: it takes "
                          "its default answers (“yes, continue”) and stops without changing anything when you "
                          "would have to choose, for example in a conflict."))
        head.addWidget(info)
        self.edit_btn = button(_("Edit the routine…"), "document-edit", self.edit_routine)
        self.edit_btn.setToolTip(_("Change the steps of your routine, in a window. Nothing runs."))
        self.dup_btn = button(_("Simulate updating everything…"), "system-run", self.open_dup)
        self.dup_btn.setToolTip(_("Opens a window that simulates updating the whole system (zypper dup --dry-run): "
                                  "what would be installed, updated or removed. Nothing changes."))
        self.stop_btn = button(_("Stop after this step"), "media-playback-pause", self.stop_after_step)
        self.stop_btn.setToolTip(_("The step that runs now finishes; the steps after it do not run."))
        self.stop_btn.hide()  # it takes the place of Run while the routine runs
        # the routine updates the whole system, whatever is ticked in the list below: the label says so, and it
        # asks first when rows are unticked (update_everything)
        self.run_btn = button(_("Update everything…"), "system-software-update", self.update_everything,
                              primary=True)
        self._stop_asked = False
        self._in_pre = False  # the refresh before an update in parts: after it, the update is simulated and asked
        for b in (self.edit_btn, self.dup_btn, self.stop_btn, self.run_btn):
            head.addWidget(b)
        rc.lay.addLayout(head)
        self.strip = QHBoxLayout()
        self.strip.setSpacing(8)
        rc.lay.addLayout(self.strip)
        self.result_lbl = QLabel("", self)
        self.result_lbl.setWordWrap(True)
        self.result_lbl.hide()
        rc.lay.addWidget(self.result_lbl)
        lay.addWidget(rc)

        # ------------------------------------------------ below: the list | the package card
        lc = Card(None, self)
        lc.lay.setContentsMargins(12, 10, 12, 10)
        lh = QHBoxLayout()
        lh.addWidget(heading(_("What there is to update"), 1.05, self))
        self.up_count = QLabel("", self)
        self.up_count.setStyleSheet(f"background: {GREEN}; color: white; border-radius: 9px; padding: 1px 8px;"
                                    " font-weight: bold;")
        self.up_count.hide()
        lh.addWidget(self.up_count)
        lh.addStretch(1)
        # tick or untick every row at once: updating only one package meant unticking every other row by hand
        self.mark_all_btn = tool(_("Mark all"), "edit-select-all", lambda: self.set_all_ticked(True))
        self.mark_all_btn.setToolTip(_("Tick every row of the list. Nothing is queued or run."))
        self.unmark_all_btn = tool(_("Unmark all"), "edit-select-none", lambda: self.set_all_ticked(False))
        self.unmark_all_btn.setToolTip(_("Untick every row of the list, to tick only the ones you want. Nothing is "
                                         "queued or run."))
        again = button(_("Look again"), "view-refresh", lambda: self.load_updates())
        again.setToolTip(_("Look again for updates, as of the last refresh of the repositories. On Tumbleweed your "
                           "routine (zypper dup) updates everything at once; here you can pick only some."))
        lh.addWidget(again)
        # "Look again" only reads what the computer already knows; this asks the repositories for what is new
        # (a version just published does not show until the repositories are refreshed)
        fresh = button(_("Refresh the repositories"), "download", self.refresh_and_look)
        fresh.setToolTip(_("Ask every repository for what is new (with the administrator password), then look "
                           "again: zypper -n -v refresh"))
        lh.addWidget(fresh)
        lc.lay.addLayout(lh)
        self.up_lbl = muted(_("Looking…"), self)
        lc.lay.addWidget(self.up_lbl)
        self.up_list = QListWidget(self)
        self.up_list.setFrameShape(QFrame.Shape.NoFrame)
        self.up_list.setItemDelegate(UpdateDelegate(self))
        self.up_list.setMouseTracking(True)
        self.up_list.setStyleSheet("QListWidget { background: transparent; }")
        # the card is filled only when the selection stops moving: every row asks zypper and rpm several things,
        # and going through 95 rows (ticking them off) froze the whole computer
        self._row_timer = QTimer(self)
        self._row_timer.setSingleShot(True)
        self._row_timer.setInterval(300)
        self._row_timer.timeout.connect(lambda: self._show_row_now(self.up_list.currentItem()))
        self.up_list.currentItemChanged.connect(lambda cur, _p: self._row_timer.start())
        self.up_list.itemDoubleClicked.connect(self._up_double)
        # the two buttons under the list name what they act on, as the ticks change
        self.up_list.itemChanged.connect(lambda _it: self._update_tick_buttons())
        lc.lay.addWidget(self.up_list, 1)
        ub = QHBoxLayout()
        ub.addWidget(self.mark_all_btn)  # under the list, next to what is done with the ticked ones
        ub.addWidget(self.unmark_all_btn)
        ub.addStretch(1)
        self.up_sim_btn = button(_("Simulate the ticked ones"), "media-playback-start",
                                 lambda: self.queue_ticked(simulate=True))
        # only to the queue: never the green one (the green one changes the system now)
        self.up_queue_btn = button(_("Put the ticked ones in the queue"), "list-add", lambda: self.queue_ticked())
        # "In the queue ✓" for a moment after a press, then the label that follows the ticks again
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(1600)
        self._flash.timeout.connect(self._update_tick_buttons)
        ub.addWidget(self.up_sim_btn)
        ub.addWidget(self.up_queue_btn)
        lc.lay.addLayout(ub)
        self._total = 0

        dc = Card(None, self)
        dc.lay.setContentsMargins(10, 12, 12, 10)
        self.updates = {}
        self.locked_names = set()
        self.details = PackageDetails(self, ctx, dc)
        dc.lay.addWidget(self.details)
        split = RoomSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(lc)
        split.addWidget(dc)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([620, 440])
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)
        ctx.queue.changed.connect(self.details.refresh)
        ctx.queue.changed.connect(self._update_tick_buttons)  # their tooltips say what the queue holds already

        # ------------------------------------------------ the whole dup, in its own window
        self.dup_dialog = self._build_dup_dialog()

        self.boxes = []
        self.steps = []
        self.show_routine()
        ctx.runner.step_started.connect(self._step_started)
        ctx.runner.step_finished.connect(self._step_finished)
        ctx.runner.finished.connect(self._any_run_finished)

    # ------------------------------------------------ routine
    def show_routine(self):
        text = self.ctx.settings["routine"]
        self.routine_lbl.setToolTip(text)
        self.run_btn.setToolTip(_("Runs your routine, which updates the whole system, not only the ticked rows. "
                                  "It asks first when you unticked some of them.") + "\n" + text)
        try:
            self.steps = routine.parse(text)
        except routine.RoutineError as e:
            self.steps = []
            self._show_steps([])
            self.strip.addWidget(QLabel(str(e), self))
            return
        self._show_steps(self.steps)

    def _show_steps(self, steps, custom=False):
        """The band: one box per step (the saved routine, or this run's steps when it runs in parts: custom)."""
        while self.strip.count():
            w = self.strip.takeAt(0).widget()
            if w:
                w.hide()  # out of sight at once: it is deleted only when the event loop gets to it
                w.deleteLater()
        self.boxes = []
        self._band_custom = custom
        for i, st in enumerate(steps, 1):
            box = StepBox(i, st, self)
            self.boxes.append(box)
            self.strip.addWidget(box, 1)

    def _restore_band(self):
        """After a run in parts (or its question), the band shows the saved routine again:
        what this run did is in the terminal and in the line under the band."""
        if getattr(self, "_band_custom", False) and not getattr(self, "_running", False):
            self.show_routine()

    def edit_routine(self):
        from .routine_editor import RoutineEditorDialog
        dlg = RoutineEditorDialog(self.ctx.settings["routine"], self)
        if dlg.exec():
            self.ctx.settings["routine"] = dlg.text()
            self.ctx.settings.save()
            self.show_routine()
        dlg.deleteLater()

    def _set_running(self, running):
        """The buttons of the routine band while it runs (Stop takes the place of Run) and after. "Will stop after
        this step" goes back to "Stop after this step" when a run starts or ends; _stop_asked (pressed during this
        run) is reset only when the next run starts, so that the end of the run can still read it."""
        self._running = running
        if running:
            self._stop_asked = False
        self.run_btn.setVisible(not running)
        self.stop_btn.setText(_("Stop after this step"))
        self.stop_btn.setStyleSheet("")
        self.stop_btn.setVisible(running)
        self.stop_btn.setEnabled(running)
        self.edit_btn.setEnabled(not running)

    def stop_after_step(self):
        """'Stop after this step': the runner stops after the step that runs now. The button shows it at once (a
        disabled orange "Will stop after this step"), the terminal says what will not run, and so does the band."""
        if not self.ctx.runner.busy and not getattr(self, "_running", False):
            return
        self.ctx.runner.stop_after_current()
        self._stop_asked = True
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText(_("Will stop after this step"))
        self.stop_btn.setStyleSheet(STOPPING_CSS)
        steps = list(getattr(self, "run_steps", None) or [])
        cur = getattr(self.ctx.runner, "index", -1)
        if not isinstance(cur, int) or not 0 <= cur < len(steps):
            cur = getattr(self, "_cur_step", 0)
        rest = [st for st in steps[cur + 1:] if getattr(st, "state", "pending") not in ("done", "failed")]
        if rest:
            first = rest[0].display()
            if len(rest) == 1:
                text = _("When this step ends, Zypp Desk stops: {command} will not run.").format(command=first)
            else:
                more = len(rest) - 1
                text = ngettext("When this step ends, Zypp Desk stops: {command} and {n} more step will not run.",
                                "When this step ends, Zypp Desk stops: {command} and {n} more steps will not run.",
                                more).format(command=first, n=more)
        elif self._in_pre:
            text = _("When this step ends, Zypp Desk stops: the update will not be simulated or run.")
        else:
            text = _("This is the last step: when it ends, nothing else runs.")
        try:
            self.ctx.terminal.view.note(text, "info")
        except AttributeError:  # a window without a terminal (tests)
            pass
        for box in self.boxes[cur + 1:]:
            box.set_state("stopping", _("will not run"))

    def _flow_busy(self):
        """An update is on its way to its question: simulated, and now the free space is measured or the parts
        are checked. Nothing else may start meanwhile: the question would be dropped."""
        return bool(getattr(self, "_dup_asking", False) or getattr(self, "_routine_asking", False))

    # ------------------------------------------------ "Update everything (n)…": the routine, asked first when needed
    def routine_kinds(self):
        """The kinds of rows of the list ('repo', 'flatpak', 'firmware') that the saved routine updates: a zypper
        dup, update or patch that installs updates the packages from the repositories, 'flatpak update' the Flatpak
        apps and 'fwupdmgr update' the firmware. Programs from the web are never updated by the routine."""
        kinds = set()
        for st in self.steps:
            sub = next((a for a in st.args if not a.startswith("-")), "")
            if st.program == "zypper" and sub in ("dup", "dist-upgrade", "up", "update", "patch") \
                    and not routine.is_download_only(st):
                kinds.add("repo")
            elif st.program == "flatpak" and {"update", "upgrade"} & set(st.args):
                kinds.add("flatpak")
            elif st.program == "fwupdmgr" and sub == "update":
                kinds.add("firmware")
        return kinds

    def tick_scope(self):
        """(ticked rows, unticked rows the routine would update anyway): what "Update everything" does beyond
        the ticks."""
        kinds = self.routine_kinds()
        ticked, beyond = [], []
        for row, on in self._tick_rows():
            if on:
                ticked.append(row)
            elif row["kind"] in kinds:
                beyond.append(row)
        return ticked, beyond

    def update_everything(self):
        """The green button of the band. The routine updates the whole system: when some rows are ticked and it
        would also update rows you unticked, it asks first (Enter: "Not yet"), and offers to update only the ticked
        ones through the queue. With every row ticked, or none, it runs as always."""
        if self.ctx.runner.busy or self._flow_busy():
            self.run_routine()  # it says to wait
            return
        self.show_routine()
        ticked, beyond = self.tick_scope()
        if ticked and beyond:
            key = self._ask_whole(ticked, beyond)
            if key == "only":
                self.queue_ticked(apply=True)
                return
            if key != "all":
                self.result_lbl.setText(_("Nothing has been changed."))
                self.result_lbl.show()
                return
        self.run_routine()

    def _ask_whole(self, ticked, beyond):
        """"Update the whole system?": what the routine does beyond the ticks. 'no', 'only', 'all' or None."""
        kinds = self.routine_kinds()
        whole = sum(1 for r in ticked if r["kind"] in kinds) + len(beyond)
        dlg = whole_question(self, ticked, beyond, whole, self.steps,
                             self._queue_preview(self._ticked_actions(ticked)))
        try:
            return dlg.exec()
        finally:
            dlg.deleteLater()

    def run_routine(self):
        if self.ctx.runner.busy or self._flow_busy():
            self.result_lbl.setText(_("Wait until the current command finishes."))
            self.result_lbl.show()
            return
        self.show_routine()
        if not self.steps:
            return
        # The usual routine downloads the whole update first ('dup -d'), so it all must fit at once. There is no
        # simulation here: only when the disk is clearly too full does it ask (statvfs only, at once).
        if routine.split_for_parts(self.steps) is not None:
            try:
                low = diskspace.routine_low()[0]
            except Exception:
                low = False
            if low:
                self._routine_asking = True
                self.result_lbl.setText(_("Looking at the free space…"))
                self.result_lbl.show()
                run_async(diskspace.routine_space, on_done=self._routine_space)
                return
        self._start_routine(self.steps)

    def _start_routine(self, steps):
        self.run_steps = steps
        self.result_lbl.setText("")
        self.result_lbl.hide()
        # before ctx.run: the routine may end inside it (no helper, no password) and _routine_done
        # must find the buttons as they are while it runs
        self._set_running(True)
        if not self.ctx.run(self.run_steps, _("Routine: {text}").format(text=self.ctx.settings["routine"]),
                            on_done=self._routine_done):
            self._set_running(False)

    def _routine_space(self, info, err):
        self._routine_asking = False
        self.result_lbl.hide()
        if self.ctx.runner.busy:
            self.result_lbl.setText(_("Wait until the current command finishes."))
            self.result_lbl.show()
            return
        if err or not info or not info["low"]:
            self._start_routine(self.steps)  # the check is a help: when it cannot tell, the routine runs as always
            return
        split = routine.split_for_parts(self.steps)
        args, _lic = routine.dup_arguments(split["dup"])
        first = [st.display() for st in split["pre"]] + [
            Step("zypper", ["--xmlout", "-n", *args, "--dry-run"], lang="C", capture=True).display()]
        cleaning = free_steps(info["free"])
        dlg = routine_question(self, info, self.steps, first, cleaning)
        key = dlg.exec()
        dlg.deleteLater()
        if key == "usual":
            self._start_routine(self.steps)
        elif key == "parts":
            self._routine_in_parts(split)
        elif key == "free":
            self.ctx.run(cleaning, _("Free up space"), on_done=lambda ok: self._routine_freed(cleaning))
        else:
            self.result_lbl.setText(_("The routine did not run."))
            self.result_lbl.show()

    # ------------------------------------------------ the routine in parts: refresh, then the dup's question
    def _routine_say(self, text):
        self.result_lbl.setText(text)
        self.result_lbl.setVisible(bool(text))

    def _routine_in_parts(self, split):
        """This run only: the refresh steps of the routine, then the same flow as the dup window with the
        routine's own dup (simulate, plan the parts, check them, ask), then the rest of the routine."""
        args, lic = routine.dup_arguments(split["dup"])
        dup = split["dup"]
        job = dict(args=tuple(args), now=_copy(dup),
                   download=Step(dup.program, [*dup.args, "--download-only"], cond=dup.cond, strict=dup.strict),
                   opts=heaps.update_options(args, lic), parent=self, say=self._routine_say, post=split["post"],
                   done=self._routine_done, routine=True, end=self._restore_band,
                   cancel=lambda: self._routine_say(_("The repositories were refreshed; nothing else has been "
                                                      "changed.")))
        job["resimulate"] = lambda: self._routine_simulate(job)
        if not split["pre"]:
            self._routine_simulate(job)
            return
        pre = [_copy(st) for st in split["pre"]]
        self.run_steps = pre
        self._show_steps(pre, custom=True)
        self._routine_say("")
        self._set_running(True)
        self._in_pre = True
        if not self.ctx.run(pre, _("Routine: {text}").format(text=routine.steps_text(pre)),
                            on_done=lambda ok: self._routine_pre_done(job, pre, split["join"], ok)):
            self._in_pre = False
            self._set_running(False)
            self._restore_band()

    def _routine_pre_done(self, job, pre, join, ok):
        # "Stop after this step" pressed on the last refresh step: nothing was left to skip in this run, but the
        # simulation and the update after it must not come
        asked, self._in_pre = self._stop_asked, False
        self._set_running(False)
        if asked or getattr(self.ctx.runner, "stopped", False) or any(st.code in (126, 127) for st in pre):
            self._job_end(job, _("The routine was stopped: the steps after it did not run."))
            return
        if not ok and join == "success":
            self._job_end(job, _("The routine stopped with errors: look at the terminal."))
            return
        QTimer.singleShot(0, lambda: self._routine_simulate(job))

    def _routine_simulate(self, job):
        if self.ctx.runner.busy:
            self._job_end(job, _("Wait until the current command finishes."))
            return
        sim = Step("zypper", ["--xmlout", "-n", *job["args"], "--dry-run"], lang="C", capture=True)
        self._sim_step = sim
        if self.ctx.run([sim], _("Simulation of zypper dup"), on_done=lambda ok: self._routine_simulated(job, sim)):
            self._routine_say(_("Simulating…"))

    def _routine_simulated(self, job, sim):
        if sim.code in (126, 127):
            self._job_end(job, _("Not simulated: the administrator password was not given."))
            return
        res = parse_dry_run(sim.output, source="zypper dup")
        for line in summary_lines(res):
            self.ctx.terminal.view.note(line, "ok")
        if res["problems"]:
            # the same window as in "Simulate the dup…": choose, and the routine goes on
            items = self._solvable(res)
            if items:
                self._routine_say(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:3]))
                QTimer.singleShot(0, lambda: self._resolve(items, job=job))
                return
            self._job_end(job, _("zypper found a problem:") + "\n" + "\n".join(res["problems"][:3]))
            return
        if not _changes(res):
            self._routine_say(_("Nothing to do: the system is up to date."))
            if job["post"]:
                self._run_job(job, [], "")  # the rest of the routine (Flatpak…) still runs
            else:
                self._restore_band()
            return
        self._prepare_question(job, res)

    def _routine_freed(self, cleaning):
        if any(s.code in (126, 127) for s in cleaning):
            self.result_lbl.setText(_("Nothing was freed: the administrator password was not given."))
            self.result_lbl.show()
            return
        self._after_change()  # old kernels may have gone
        QTimer.singleShot(0, self.run_routine)  # it looks at the space again: enough now, and the routine runs

    def _after_change(self):
        """The system changed (the routine, a dup): the pages look again (the main window knows which)."""
        fn = getattr(self.ctx, "after_change", None)
        if callable(fn):
            fn()
        else:
            from .. import tray
            tray.poke("check")
            self.reload()

    def _step_started(self, i):
        self._cur_step = i
        if getattr(self, "_running", False) and i < len(self.boxes):
            self.boxes[i].set_state("running", _("running…"))

    def _step_finished(self, i, code):
        if not getattr(self, "_running", False) or i >= len(self.boxes):
            return
        st = self.run_steps[i]
        if st.state == "skipped" and self._stop_asked:
            self.boxes[i].set_state("skipped", _("did not run: you stopped the routine"))
        elif st.state == "skipped":
            self.boxes[i].set_state("skipped", _("skipped"))
        elif st.state == "done":
            self.boxes[i].set_state("done", _("done (exit code {code})").format(code=code))
        else:
            self.boxes[i].set_state("failed", _("failed (exit code {code})").format(code=code))

    def _routine_done(self, ok):
        self._set_running(False)
        if not ok and getattr(self.ctx.runner, "stopped", False):
            self.result_lbl.setText(_("The routine was stopped: the steps after it did not run."))
        else:
            self.result_lbl.setText(_("Routine finished.") if ok else _("The routine stopped with errors: look "
                                                                         "at the terminal."))
        self.result_lbl.show()
        self._restore_band()
        if ok and self.ctx.settings["after_routine_check_restart"]:
            run_async(backend.processes_using_deleted_files, on_done=self._ps_done)
        self._after_change()  # what it updated is not waiting any more: the list (and the tray) look again

    def _ps_done(self, ps, err):
        if err or not ps:
            return
        if ps["core_updated"]:
            msg = _("Core libraries or services were updated: restarting the computer is recommended.")
        elif ps["processes"]:
            names = sorted({p["command"] for p in ps["processes"]})
            msg = _("No need to restart the computer. These programs still use old files; restart them: "
                    "{names}").format(names=", ".join(names[:12]))
        else:
            msg = _("No need to restart anything.")
        self.result_lbl.setText(self.result_lbl.text() + " " + msg)

    # ------------------------------------------------ what there is to update
    def activated(self):
        if self.loaded:
            self.load_updates()
        super().activated()

    def load_updates(self):
        self._say(_("Looking…"))
        # the last answers stay on screen until the new ones arrive
        if not hasattr(self, "up_found"):
            self.up_found = {"repo": None, "flatpak": None, "web": None, "firmware": None}
        self.up_errors = {}
        self.up_pending = {"repo", "flatpak", "web", "firmware"}
        self.up_round = getattr(self, "up_round", 0) + 1
        rnd = self.up_round
        run_async(backend.list_updates, on_done=lambda res, err: self._up_part(rnd, "repo", res, err))
        if flatpak.available():
            run_async(flatpak.updates, on_done=lambda res, err: self._up_part(rnd, "flatpak", res, err))
        else:
            self.up_found["flatpak"] = []
            self.up_pending.discard("flatpak")
        if firmware.available():
            run_async(firmware.updates, on_done=lambda res, err: self._up_part(rnd, "firmware", res, err))
        else:
            self.up_found["firmware"] = []
            self.up_pending.discard("firmware")
        sources = [x for x in websources.load() if x.get("type") != "manual"]

        def web():
            out = []
            for src in sources:
                try:
                    f = websources.check(src)
                except Exception as e:  # one site that does not answer must not hide the others
                    out.append(dict(name=src["package"], old="", new="", where=str(e), error=True))
                    continue
                if f.get("newer") or (f.get("newer") is None and f.get("changed")):  # = WebSourcesPage.counts_as_new
                    out.append(dict(name=src["package"], old=f.get("installed") or "", new=f.get("version") or "",
                                    where=websources.origin_text(src), changed=f.get("newer") is None))
            return out
        run_async(web, on_done=lambda res, err: self._up_part(rnd, "web", res, err))

    def _say(self, text):
        """The line under the title of the list: only when there is something to say."""
        self.up_lbl.setText(text)
        self.up_lbl.setVisible(bool(text))

    def _web_changed(self):
        if self.loaded:
            self.load_updates()

    def _up_part(self, rnd, part, res, err):
        if rnd != self.up_round:
            return  # an older round answered late
        self.up_pending.discard(part)
        self.up_found[part] = res or []
        if part == "web" and not err:
            n = len([u for u in res or [] if not u.get("error")])
            self.ctx.set_badge("websources", str(n) if n else "")
        if err:
            self.up_errors[part] = str(err)
        self._up_fill()

    def _up_fill(self):
        f = self.up_found
        # what you ticked or unticked, and what is selected, stay as they were when the list fills again
        kept, cur_key = {}, None
        for i in range(self.up_list.count()):
            it = self.up_list.item(i)
            row = it.data(ROW_ROLE)
            if row and not row.get("header"):
                kept[(row["kind"], row["name"])] = it.checkState()
        cur = self.up_list.currentItem()
        if cur and cur.data(ROW_ROLE) and not cur.data(ROW_ROLE).get("header"):
            cur_key = (cur.data(ROW_ROLE)["kind"], cur.data(ROW_ROLE)["name"])
        self.up_list.blockSignals(True)
        self.up_list.clear()
        self.updates = {u["name"]: u for u in (f["repo"] or [])}
        groups = [
            ("repo", _("Packages from the repositories"),
             [dict(kind="repo", name=u["name"], version=f"{u['old']} → {u['edition']}" if u["old"] else u["edition"],
                   where=u["repo"], data=u) for u in (f["repo"] or [])]),
            ("flatpak", _("Flatpak apps"),
             [dict(kind="flatpak", name=u.get("application") or "", version=u.get("version") or u.get("branch") or "",
                   where=u.get("origin") or "", data=u) for u in (f["flatpak"] or [])]),
            ("web", _("Programs from the web"),
             [dict(kind="web", name=u["name"], error=bool(u.get("error")), where=u["where"], data=u,
                   version=(_("could not check") if u.get("error") else
                            f"{u['old']} → {u['new']}" if u["new"] else _("{old} → file changed").format(old=u["old"])))
              for u in (f["web"] or [])]),
            ("firmware", _("Firmware"),
             [dict(kind="firmware", name=u["name"], version=f"{u['version']} → {u['new_version']}",
                   where=" · ".join(x for x in (u.get("release_vendor") or u.get("vendor"), "LVFS") if x), data=u)
              for u in (f.get("firmware") or [])]),
        ]
        total, select = 0, None
        for key, label, rows in groups:
            if not rows:
                continue
            hdr = QListWidgetItem()
            hdr.setData(ROW_ROLE, {"header": label})
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            self.up_list.addItem(hdr)
            for row in rows:
                it = QListWidgetItem()
                it.setData(ROW_ROLE, row)
                # the row elides long names and versions: the tooltip has them whole
                it.setToolTip(f"{row['name']}\n{row['where']}" if row.get("error") else
                              "\n".join(x for x in (row["name"], row["version"], row["where"]) if x))
                if row.get("error"):
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                else:
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(kept.get((key, row["name"]), Qt.CheckState.Checked))
                    total += 1
                self.up_list.addItem(it)
                if (key, row["name"]) == cur_key or select is None:
                    select = it if (key, row["name"]) == cur_key or select is None else select
        self.up_list.blockSignals(False)
        if select is not None and self.up_list.currentItem() is None:
            self.up_list.setCurrentItem(select)
        if not hasattr(self, "_desk_icons"):
            self._desk_icons = {}
        names = sorted({r["name"] for _k, _l, rows in groups for r in rows if r["kind"] in ("repo", "web")}
                       - set(self._desk_icons))
        if names:
            run_async(appicons.desktop_icons, names, on_done=self._icons_loaded)
        busy = bool(self.up_pending)
        self.up_count.setText(str(total))
        self.up_count.setVisible(total > 0)
        counts = {key: sum(1 for r in rows if not r.get("error")) for key, _l, rows in groups}
        if not busy:  # the sidebar shows the count too: "Updates (4)"
            self.ctx.set_badge(self.key, str(total) if total else "")
        self.counted.emit(dict(counts, total=total, busy=busy, pending=sorted(self.up_pending),
                               errors=dict(self.up_errors),
                               flatpak_ids=[u.get("application") for u in (f["flatpak"] or [])]))
        errors = [f"{k}: {v[:80]}" for k, v in self.up_errors.items()]
        if busy:
            text = _("Looking…")
        elif total:
            text = ""  # the green number next to the title says it (a line less: the list keeps its room)
            self.up_count.setToolTip(ngettext("{n} update waiting.", "{n} updates waiting.", total).format(n=total))
        else:
            text = _("Everything is up to date.")
            self.details.show_simple(_("Everything is up to date."), "", icon("checkmark", "dialog-ok"), "")
        if errors:
            text = (text + "  " + _("(could not look: {error})").format(error="; ".join(errors))).strip()
        self._say(text)
        self._total = total
        # "Update everything (97)…": the number of updates found so far, as the green number next to the list
        self.run_btn.setText(_("Update everything ({n})…").format(n=_n(total)) if total else _("Update everything…"))
        self._update_tick_buttons()

    # ------------------------------------------------ the ticks and the buttons under the list
    def _tick_rows(self):
        """(row, ticked) for every row that can be ticked."""
        out = []
        for i in range(self.up_list.count()):
            it = self.up_list.item(i)
            row = it.data(ROW_ROLE)
            if row and not row.get("header") and not row.get("error"):
                out.append((row, is_checked(it.checkState())))
        return out

    @staticmethod
    def _ticked_actions(rows):
        """The queue changes for these rows (the programs from the web are downloaded first: not here)."""
        out = []
        for row in rows:
            if row["kind"] == "repo":
                out.append(Action("install", row["name"]))
            elif row["kind"] == "flatpak":
                out.append(Action("flatpak-update", row["name"]))
            elif row["kind"] == "firmware":
                u = row["data"]
                out.append(Action("firmware", u["id"], name=u["name"], version=u["new_version"]))
        return out

    def _queue_preview(self, actions):
        """The commands the queue would run for these changes alone (the queue's own code and settings), a long
        list of names shortened ('… and 90 more'): for a tooltip or a question."""
        if not actions:
            return []
        from ..txqueue import TxQueue
        q = TxQueue(getattr(self.ctx.queue, "settings", None) or self.ctx.settings)
        with q.batch():
            for a in actions:
                q.add(a)
        out = []
        for st in q.steps():
            args = list(st.args)
            if "--" in args:
                i = args.index("--")
                out.append(heaps.shown([st.program, *args[:i + 1]], args[i + 1:], _more))
            else:
                out.append(st.display())
        return out

    def _update_tick_buttons(self):
        """The buttons under the list say what they act on: the one ticked row by name, or how many."""
        rows = [row for row, on in self._tick_rows() if on]
        sim, queue, _only = tick_labels(rows)
        n = len(rows)
        self.up_sim_btn.setText(sim)
        if not self._flash.isActive():
            self.up_queue_btn.setText(queue)
        self.up_sim_btn.setEnabled(n > 0)
        self.up_queue_btn.setEnabled(n > 0)
        if not n:
            tip = _("Tick one or more rows first.")
            self.up_sim_btn.setToolTip(tip)
            self.up_queue_btn.setToolTip(tip)
            return
        what = names_text(rows)
        web = [r for r in rows if r["kind"] == "web"]
        cmds = self._queue_preview(self._ticked_actions(rows))
        sim_tip = [_("Puts {names} in the queue and simulates the queue (zypper --dry-run): what would be installed, "
                     "updated or removed. Nothing changes.").format(names=what)]
        if web:
            sim_tip.append(_("Programs from the web are downloaded and checked first: zypper can only simulate "
                             "them with the file."))
        if len(self.ctx.queue):
            sim_tip.append(_("What the queue holds already is simulated with them."))
        q_tip = [_("Puts {names} in the queue. Nothing changes until you apply the queue.").format(names=what)]
        if web:
            q_tip.append(_("Programs from the web are downloaded and checked first, then they join the queue."))
        if cmds:
            q_tip.append(_("The queue will run:") + "\n" + "\n".join(cmds))
        self.up_sim_btn.setToolTip("\n".join(sim_tip))
        self.up_queue_btn.setToolTip("\n".join(q_tip))

    # ------------------------------------------------ the package card
    def pkg_icon(self, d):
        name = self._desk_icons.get(d["name"]) if hasattr(self, "_desk_icons") else None
        if name:
            ic = QIcon(name) if name.startswith("/") else QIcon.fromTheme(name)
            if not ic.isNull():
                return ic
        return self.ctx.pages["packages"].pkg_icon(d)

    def _icons_loaded(self, icons, err):
        if icons:
            self._desk_icons.update(icons)
            self.up_list.viewport().update()
            self.details.refresh_icon()

    def row_icon(self, row):
        if row["kind"] == "firmware":
            return icon(*(row["data"].get("icons") or []), "computer", "cpu", "application-x-firmware")
        if row["kind"] == "flatpak":
            ic = QIcon.fromTheme(row["name"])
            if ic.isNull():
                path = appicons.flatpak_icon_file(row["name"])
                ic = QIcon(path) if path else icon("flatpak-discover", "package-x-generic")
            return ic
        return self.pkg_icon({"name": row["name"]})

    def _show_row(self, item):
        """Show this row's card at once (when the program selects it, not the user moving through the list)."""
        self._row_timer.stop()
        self._show_row_now(item)

    def _show_row_now(self, item):
        row = item.data(ROW_ROLE) if item else None
        if not row or row.get("header"):
            return
        self.details.set_custom(None)
        if row.get("error"):
            self._detail_for = None
            self.details.show_simple(row["name"], _("could not check"), self.row_icon(row), html.escape(row["where"]))
            return
        if row["kind"] == "firmware":
            self._show_firmware(row)
            return
        if row["kind"] == "flatpak":
            self._detail_for = None
            u = row["data"]
            e = html.escape
            rows = [(_("New version"), f"<b>{e(u.get('version') or '')}</b>"), (_("Branch"), e(u.get("branch") or "")),
                    (_("Comes from"), e(u.get("origin") or ""))]
            self.details.show_simple(row["name"], _("Flatpak app"), self.row_icon(row), table(rows))
            return
        name = row["name"]
        if getattr(self, "_detail_for", None) == (row["kind"], name) and self.details.current \
                and self.details.current["name"] == name:
            return  # already shown (the list was only filled again)
        self._detail_for = (row["kind"], name)
        u = row["data"]
        # what is known already shows at once; the search (a few seconds) completes the card later
        old = u.get("old") or ""
        quick = {"name": name, "kind": "package", "summary": "", "versions": [], "best": None,
                 "installed": {"edition": old, "arch": u.get("arch", ""), "repo": "", "status": "installed"}
                 if old else None}
        overrides = {}
        if row["kind"] == "web":
            overrides = {"from": u["where"],
                         "new": u["new"] or _("the file changed: download it to see its version")}
            # the card's own main action: download, check, then into the queue (nothing is installed yet)
            self.details.set_custom(_("Download the new version"),
                                    lambda n=name: self.ctx.pages["websources"].get_new_versions([n]),
                                    tip=_("Downloads the new version of {name} from {where} and checks it; then it "
                                          "goes to the queue. Nothing is installed until you apply the "
                                          "queue.").format(name=name, where=u["where"]))
        self.details.load(quick, overrides)
        run_async(backend.search, name, exact=True, on_done=lambda res, err, r=row: self._row_package(r, res, err))

    def _show_firmware(self, row):
        """The firmware card: Information / What changes / All devices."""
        u = row["data"]
        self._detail_for = ("firmware", row["name"])
        esc = html.escape
        rows = [(_("You have"), esc(u["version"])), (_("New version"), f"<b>{esc(u['new_version'])}</b>")]
        if u.get("size"):
            rows.append((_("Size"), esc(human_size(u["size"]))))
        rows.append((_("Comes from"), esc(_("LVFS (fwupd.org), where {vendor} publishes its firmware").format(
            vendor=u.get("release_vendor") or u.get("vendor") or "?"))))
        rows.append((_("Installed"), esc(_("when the computer restarts, before Linux starts") if u.get("needs_reboot")
                                        else _("at once, while you work"))))
        if u.get("urgency") in ("high", "critical"):
            rows.append((_("Urgency"), f"<b style='color:{RED}'>{esc(_('high: install it soon'))}</b>"))
        warn = WARN_HTML.format(text=esc(_("Plug in the charger and do not switch the computer off while it "
                                           "updates.")))
        # the exact command the queue runs (txqueue: firmware), shown whole
        cmd = Step("fwupdmgr", ["update", u["id"], "--assume-yes", "--no-reboot-check", "--no-unreported-check"])
        cmd_html = f"<p style='color:gray;font-family:monospace'>{esc(cmd.display())}</p>"
        tabs = []
        what = u.get("description") or u.get("release_summary") or ""
        if what:
            tabs.append((_("What changes"), "<p>" + "<br>".join(esc(x) for x in what.split("\n") if x.strip()) + "</p>"))
        if getattr(self, "_fw_devices", None):
            items = "".join(f"<li>{esc(d['name'])} <span style='color:gray'>{esc(d['version'])}</span></li>"
                            for d in self._fw_devices)
            tabs.append((_("All devices"), f"<p>{esc(_('Every device fwupd knows'))}</p><ul>{items}</ul>"))
        elif firmware.available():
            tabs.append((_("All devices"), f"<i>{esc(_('Loading…'))}</i>"))
            if not getattr(self, "_fw_devices_asked", False):
                self._fw_devices_asked = True
                run_async(firmware.devices, on_done=lambda res, err, r=row: self._fw_devices_loaded(r, res))
        self.details.show_simple(u["name"], u.get("release_summary") or u.get("vendor") or _("Firmware"),
                                 self.row_icon(row), table(rows) + warn + cmd_html, tabs)
        # it only goes to the queue: after "To the queue:", never green
        self.details.set_custom(_("Update"),
                                lambda: self.ctx.queue.add(Action("firmware", u["id"], name=u["name"],
                                                                  version=u["new_version"])),
                                tip=_("Puts the update of {name} in the queue. Nothing changes until you apply the "
                                      "queue, which runs:").format(name=u["name"]) + "\n" + cmd.display(),
                                queue=True)

    def _fw_devices_loaded(self, row, res):
        self._fw_devices = res or []
        if getattr(self, "_detail_for", None) == ("firmware", row["name"]):
            tab = self.details.tabs.currentIndex()
            self._show_firmware(row)
            self.details.tabs.setCurrentIndex(tab)  # stay on the tab that was open

    def _row_package(self, row, res, err):
        if getattr(self, "_detail_for", None) != (row["kind"], row["name"]):
            return  # another row was selected meanwhile
        d = next((x for x in res or [] if x["name"] == row["name"]), None)
        if d is not None:
            self.details.refine(d)

    def _up_double(self, item):
        row = item.data(ROW_ROLE)
        if row and row.get("kind") == "web":
            self.ctx.go("websources")

    def refresh_and_look(self):
        if self.ctx.runner.busy:
            self.up_lbl.setText(_("Wait until the current command finishes."))
            return
        self.ctx.run([Step("zypper", ["-n", "-v", "refresh"])], _("Refresh every repository"),
                     on_done=lambda ok: self.load_updates())

    def set_all_ticked(self, on):
        """Tick (or untick) every row that can be ticked, in one go: one repaint, no card loaded per row."""
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.up_list.blockSignals(True)
        try:
            for i in range(self.up_list.count()):
                it = self.up_list.item(i)
                row = it.data(ROW_ROLE)
                if row and not row.get("header") and not row.get("error"):
                    it.setCheckState(state)
        finally:
            self.up_list.blockSignals(False)
        self.up_list.viewport().update()
        self._update_tick_buttons()

    def queue_ticked(self, simulate=False, apply=False):
        """The ticked rows go to the queue (the programs from the web once downloaded and checked). simulate: then
        the queue is simulated; apply: then the usual Apply question ("Only digikam" in the question before
        "Update everything")."""
        n, web = 0, []
        with self.ctx.queue.batch():
            for i in range(self.up_list.count()):
                it = self.up_list.item(i)
                row = it.data(ROW_ROLE)
                if not row or row.get("header") or row.get("error") or not is_checked(it.checkState()):
                    continue
                if row["kind"] == "repo":
                    self.ctx.queue.add(Action("install", row["name"]))
                    n += 1
                elif row["kind"] == "flatpak":
                    self.ctx.queue.add(Action("flatpak-update", row["name"]))
                    n += 1
                elif row["kind"] == "firmware":
                    u = row["data"]
                    self.ctx.queue.add(Action("firmware", u["id"], name=u["name"], version=u["new_version"]))
                    n += 1
                else:
                    web.append(row["name"])
        text = ngettext("{n} update added to the queue: simulate it or apply it below.",
                        "{n} updates added to the queue: simulate them or apply them below.", n).format(n=n) if n else ""
        if web:
            text += " " + ngettext("{n} program from the web is being downloaded and checked; it joins the queue "
                                   "when it is ready (look at the terminal).",
                                   "{n} programs from the web are being downloaded and checked one after the "
                                   "other; each joins the queue when it is ready (look at the terminal).",
                                   len(web)).format(n=len(web))
        self._say(text.strip())  # first: the simulation below may write its own message
        if n:  # the button shows it happened; the line under the title says the rest
            self._flash.start()
            self.up_queue_btn.setText(_("In the queue ✓"))
        if simulate or apply:
            if self.ctx.runner.busy:
                self._say(_("Wait until the current command finishes."))
                return
            # it goes on once every program from the web is downloaded, checked and queued
            self.ctx.pages["websources"].get_new_versions(
                web, on_finished=self._simulate_queue if simulate else self._apply_queue)
        elif web:
            self.ctx.pages["websources"].get_new_versions(web)

    def _simulate_queue(self):
        if not self.ctx.queue.actions:
            self._say(_("Nothing reached the queue, so there is nothing to simulate."))
            return
        self.ctx.simulate_queue()

    def _apply_queue(self):
        """The usual Apply question of the window, for what is in the queue now."""
        if not self.ctx.queue.actions:
            self._say(_("Nothing reached the queue, so there is nothing to apply."))
            return
        self.ctx.apply_queue()

    # ------------------------------------------------ dup simulation, in its own window
    def _build_dup_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(_("What would 'zypper dup' do?"))
        dlg.resize(820, 560)
        dl = QVBoxLayout(dlg)
        s = self.ctx.settings
        # the same names as the options of the routine editor
        self._vendor_conf = self._read_vendor_conf()
        self.o_vendor = QCheckBox(_("Allow a change of vendor (--allow-vendor-change)"), dlg)
        self.o_vendor.setToolTip(_("Let a package move to another vendor, for example from openSUSE to Packman. "
                                   "It starts as zypp.conf says (solver.dupAllowVendorChange): the command gets an "
                                   "option only when you choose otherwise."))
        self.o_vendor.setChecked(self._vendor_conf)
        self.o_norec = QCheckBox(_("Without recommended packages (--no-recommends)"), dlg)
        self.o_norec.setToolTip(_("Install only what the packages need, not what they suggest (--no-recommends)."))
        self.o_norec.setChecked(s["dup_no_recommends"])
        self.o_down = QCheckBox(_("Allow downgrades (--allow-downgrade)"), dlg)
        self.o_down.setToolTip(_("Let zypper install an older version when a repository only has that one "
                                 "(--allow-downgrade)."))
        self.o_down.setChecked(s["dup_allow_downgrade"])
        self.o_from = QComboBox(dlg)
        self.o_from.addItem(_("From every enabled repository"), "")
        self.o_from.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.o_from.setMinimumContentsLength(12)
        opts = QGridLayout()
        opts.setHorizontalSpacing(24)
        opts.addWidget(self.o_vendor, 0, 0)
        opts.addWidget(self.o_norec, 0, 1)
        opts.addWidget(self.o_down, 1, 0)
        fr = QHBoxLayout()
        fr.addWidget(QLabel(_("Only from:"), dlg))
        fr.addWidget(self.o_from, 1)
        opts.addLayout(fr, 1, 1)
        for cb in (self.o_norec, self.o_down):
            cb.toggled.connect(self._save_options)
        for cb in (self.o_vendor, self.o_norec, self.o_down):
            cb.toggled.connect(self._show_dup_cmd)
        self.o_from.currentIndexChanged.connect(self._show_dup_cmd)
        dl.addLayout(opts)
        # the exact commands the two buttons run, as they change with the options
        self.dup_cmd = QLabel(dlg)
        self.dup_cmd.setFont(mono())
        self.dup_cmd.setTextFormat(Qt.TextFormat.PlainText)
        self.dup_cmd.setWordWrap(True)
        self.dup_cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dup_cmd.setStyleSheet("color: palette(placeholder-text);")
        dl.addWidget(self.dup_cmd)
        self.sim_lbl = muted(_("It asks for the administrator password. Programs from the web are not in it."), dlg)
        self.sim_lbl.setToolTip(_("zypper needs root even to simulate, and it does not know a program from the web "
                                  "until its file is downloaded: tick those in the list and simulate them with the "
                                  "button under it."))
        dl.addWidget(self.sim_lbl)
        self.sim_tree = QTreeWidget(dlg)
        self.sim_tree.setHeaderLabels([_("Package"), _("Version"), _("Repository")])
        self.sim_tree.setAlternatingRowColors(True)
        dl.addWidget(self.sim_tree, 1)
        bb = QHBoxLayout()
        bb.addStretch(1)
        # Enter simulates (nothing changes); the real dup needs a click, and then its question answers No to Enter.
        # The green one is the one that changes the system (after its question), as everywhere.
        self.dup_run_btn = button(_("Update everything with these options…"), "system-software-update",
                                  lambda: self.run_dup(), primary=True)
        self.dup_run_btn.setAutoDefault(False)
        self.dup_sim_btn = button(_("Simulate updating everything"), "media-playback-start",
                                  lambda: self.simulate_dup())
        self.dup_sim_btn.setDefault(True)
        close_btn = button(_("Close"), "window-close", dlg.close)
        close_btn.setAutoDefault(False)
        close_btn.setToolTip(_("Close this window. Nothing changes."))
        for b in (self.dup_run_btn, self.dup_sim_btn, close_btn):
            bb.addWidget(b)
        dl.addLayout(bb)
        self._show_dup_cmd()
        return dlg

    @staticmethod
    def _read_vendor_conf():
        """solver.dupAllowVendorChange as libzypp reads it now (zypp.conf and its drop-ins)."""
        try:
            return zyppconf.read_effective()["solver.dupAllowVendorChange"]["value"] == "true"
        except Exception:  # an unreadable file: libzypp's own default
            return False

    def open_dup(self):
        if not self.dup_dialog.isVisible():
            # as the routine and a dup in a terminal do: what zypp.conf says, unless you tick otherwise
            self._vendor_conf = self._read_vendor_conf()
            self.o_vendor.setChecked(self._vendor_conf)
            self._show_dup_cmd()
        self.dup_dialog.show()
        self.dup_dialog.raise_()
        self.dup_dialog.activateWindow()

    def reload(self):
        run_async(backend.repos, on_done=self._repos_loaded)
        run_async(backend.locks, on_done=self._locks_loaded)
        self.load_updates()

    def _locks_loaded(self, locks, err):
        if not err:
            self.locked_names = {lk["name"] for lk in locks or []}
            self.details.refresh()

    def _repos_loaded(self, repos, err):
        if err:
            return
        self._repos = repos  # for the question before the dup: how old the Tumbleweed version is
        # "Only from:" keeps what you chose across the reloads, while the repository is
        # still there and enabled
        chosen = self.o_from.currentData()
        self.o_from.blockSignals(True)
        self.o_from.clear()
        self.o_from.addItem(_("From every enabled repository"), "")
        for r in sorted((r for r in repos if r["enabled"]), key=lambda r: r["name"].lower()):
            self.o_from.addItem(r["name"], r["alias"])
        self.o_from.setCurrentIndex(max(0, self.o_from.findData(chosen)) if chosen else 0)
        self.o_from.blockSignals(False)
        self._show_dup_cmd()

    def _save_options(self):
        # the change of vendor is not kept: it starts from zypp.conf each time (see open_dup)
        s = self.ctx.settings
        s["dup_no_recommends"] = self.o_norec.isChecked()
        s["dup_allow_downgrade"] = self.o_down.isChecked()
        s.save()

    def dup_args(self):
        args = ["dist-upgrade"]
        vendor = self.o_vendor.isChecked()
        if vendor != self._vendor_conf:  # the same as zypp.conf: no option, as in the routine
            args.append("--allow-vendor-change" if vendor else "--no-allow-vendor-change")
        if self.o_norec.isChecked():
            args.append("--no-recommends")
        args.append("--allow-downgrade" if self.o_down.isChecked() else "--no-allow-downgrade")
        if self.o_from.currentData():
            args += ["--from", self.o_from.currentData()]
        return args

    def _dup_steps(self, args=None):
        """(simulation, real run): the commands the two buttons run, made in one place so that what the window
        shows is exactly what runs."""
        args = list(self.dup_args() if args is None else args)
        sim = Step("zypper", ["--xmlout", "-n", *args, "--dry-run"], lang="C", capture=True)
        return sim, self._choice_steps(args)["now"]

    def _choice_steps(self, args):
        """The command of each answer of the question before the dup: update now, only download."""
        lic = ["--auto-agree-with-licenses"] if self.ctx.settings["apply_accept_licenses"] else []
        return dict(now=Step("zypper", ["-n", "-v", *args, *lic]),
                    download=Step("zypper", ["-n", "-v", *args, "--download-only", *lic]))

    def _show_dup_cmd(self, *_args):
        sim, run = self._dup_steps()
        self.dup_cmd.setText(_("Simulate: {command}").format(command=sim.display()) + "\n"
                             + _("Run: {command}").format(command=run.display()))
        # the buttons' tooltips carry the exact commands, as the options change
        if hasattr(self, "dup_run_btn"):
            self.dup_run_btn.setToolTip(_("Updates the whole system with the options above. First it simulates the "
                                          "update (unless it was just simulated with these options), then it asks "
                                          "you with the numbers: how big it is and whether it fits on the disk.")
                                        + "\n" + run.display())
            self.dup_sim_btn.setToolTip(_("Shows what updating the whole system with the options above would "
                                          "install, update or remove. Nothing changes.") + "\n" + sim.display())

    def _fresh_sim(self, args):
        """The last simulation of the dup, if it is recent, for these options and nothing has run since."""
        last = getattr(self, "_last_sim", None)
        if last and last[0] == tuple(args) and time.monotonic() - last[1] < SIM_FRESH_S:
            return last[2]
        return None

    def _any_run_finished(self, _ok):
        """Anything that ran (a routine, Apply, maintenance…) may have changed what the dup would do."""
        sim = getattr(self, "_sim_step", None)
        if not any(s is sim for s in self.ctx.runner.steps):
            self._last_sim = None

    def simulate_dup(self, args=None, then=None):
        """Simulate the dup. then(result): called after a simulation that can go ahead (the question)."""
        if self.ctx.runner.busy or (then is None and self._flow_busy()):
            self.sim_lbl.setText(_("Wait until the current command finishes."))
            return
        args = tuple(self.dup_args() if args is None else args)
        st, _run = self._dup_steps(args)
        self._sim_step = st
        if self.ctx.run([st], _("Simulation of zypper dup"), on_done=lambda ok: self._simulated(st, args, then)):
            self.sim_lbl.setText(_("Simulating…"))

    @staticmethod
    def _solvable(res):
        """The problems of a simulation that the problem window can show: those with a solution, or all of them
        when there are many (with -n zypper gives solutions only for the first one; problemgroups makes a
        "leave it as it is" for each)."""
        all_items = res.get("problem_items") or []
        try:
            from ..problemgroups import MANY
        except ImportError:
            MANY = 4
        if len(all_items) >= MANY:
            return all_items
        return [p for p in all_items if any(x.get("possible") for x in p.get("solutions") or [])]

    def _simulated(self, st, args=None, then=None):
        if st.code in (126, 127):
            self.sim_lbl.setText(_("Not simulated: the administrator password was not given."))
            return
        self.sim_tree.clear()
        res = parse_dry_run(st.output, source="zypper dup")
        # kept for the question only when there is something to do ("up to date" may change with a refresh)
        self._last_sim = ((tuple(args), time.monotonic(), res) if args is not None and res["ok"] and _changes(res)
                          else None)
        for line in summary_lines(res):
            self.ctx.terminal.view.note(line, "ok")
        self.sim_tree.clear()
        counts = []
        for key, label in CATEGORIES:
            items = res["groups"].get(key, [])
            if not items:
                continue
            counts.append(f"{label}: {len(items)}")
            top = QTreeWidgetItem(self.sim_tree, [f"{label} ({len(items)})"])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for it in items:
                ver = f"{it['old']} → {it['edition']}" if it["old"] else it["edition"]
                QTreeWidgetItem(top, [it["name"], ver, it["repo"]])
            top.setExpanded(len(items) <= 50)
        for c in range(3):
            self.sim_tree.resizeColumnToContents(c)
        if res["download"]:
            counts.append(_("Download: {size}").format(size=human_size(res["download"])))
        if res["disk"]:
            counts.append(disk_text(res["disk"]))
        items = self._solvable(res)
        if items:
            self.sim_lbl.setText(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:3]))
            QTimer.singleShot(0, lambda: self._resolve(items, args, then))
        elif res["problems"]:
            self.sim_lbl.setText(_("zypper found a problem:") + "\n" + "\n".join(res["problems"][:5]))
        elif not counts:
            self.sim_lbl.setText(_("Nothing to do: the system is up to date."))
        else:
            self.sim_lbl.setText(_("Nothing has been changed.") + "  " + "  ·  ".join(counts))
            if then is not None:
                QTimer.singleShot(0, lambda: then(res))

    def _resolve(self, items, args=None, then=None, job=None):
        """The dup cannot go on: choose a solution; it runs at once (a lock, a removal), then simulate again.
        job: the routine's job when the routine runs in parts (the window opens over the page, and after the
        solutions the routine's dup is simulated again and goes on)."""
        try:
            from .problems import ProblemDialog
        except ImportError:
            return
        if job is None:
            parent, say, again = self.dup_dialog, self.sim_lbl.setText, (lambda: self.simulate_dup(args, then))
        else:
            parent, say, again = self, job["say"], job["resimulate"]
        dlg = ProblemDialog(items, parent, apply_label=_("Do it now and simulate again"),
                            note=_("The dup is only simulated: what you choose here runs at once (with the "
                                   "password), and then the dup is simulated again."), now=True)
        accepted = dlg.exec()
        chosen = dlg.chosen()
        wait = getattr(dlg, "wait_requested", False)
        dlg.deleteLater()
        if wait:
            say(_("Zypp Desk will remind you tomorrow morning. Nothing has been changed."))
            if job is not None:
                job["end"]()
            return
        if accepted:
            self._apply_solutions(chosen, again)
        elif job is not None:
            job["cancel"]()
            job["end"]()

    def _apply_solutions(self, chosen, then):
        """MainWindow.apply_solutions for a dup: the problem window was the question, so it does not ask again
        (confirmed, when the main window knows that parameter)."""
        fn = self.ctx.apply_solutions
        kw = dict(then=then, now=True)
        try:
            if "confirmed" in inspect.signature(fn).parameters:
                kw["confirmed"] = True
        except (TypeError, ValueError):
            pass
        fn(chosen, **kw)

    # ------------------------------------------------ the real dup: first the question, with the numbers
    # A "job" is one real upgrade from its simulation to its run: the dup of the dup window, or the routine's dup
    # when the routine goes in parts. dict(args (the simulation's arguments), now, download (Steps), opts (for
    # the parts: heaps.update_options), parent (of the question), say(text), post (steps after the dup: the rest
    # of the routine), done(ok), resimulate(), cancel(), end() (the band goes back to the saved routine), routine
    # (bool)).
    def run_dup(self):
        if self.ctx.runner.busy:
            self.sim_lbl.setText(_("Wait until the current command finishes."))
            return
        if self._flow_busy():
            return  # the question is on its way
        args = tuple(self.dup_args())
        steps = self._choice_steps(list(args))
        job = dict(args=args, now=steps["now"], download=steps["download"],
                   opts=heaps.update_options(args, self.ctx.settings["apply_accept_licenses"]), parent=self.dup_dialog,
                   say=self.sim_lbl.setText, post=[], done=self._dup_done, resimulate=self.run_dup, end=lambda: None,
                   cancel=lambda: self.sim_lbl.setText(_("Nothing has been changed.")), routine=False)
        res = self._fresh_sim(args)
        if res is not None:
            self._prepare_question(job, res)
            return
        # the question needs the numbers: simulate first (the same password is remembered for the real run)
        self.simulate_dup(args, then=lambda r: self._prepare_question(job, r))

    def _job_end(self, job, text):
        """The job ends here without running the update: nothing more is on its way."""
        self._dup_asking = False
        job["say"](text)
        job["end"]()

    def _prepare_question(self, job, res):
        self._dup_asking = True
        job["say"](_("Looking at the free space…"))
        run_async(dup_facts, res, getattr(self, "_repos", None),
                  on_done=lambda facts, err: self._facts_ready(job, res, facts if not err else {}))

    def _facts_ready(self, job, res, facts):
        plan = facts.get("plan")
        if plan and plan["feasible"]:
            self._check_parts(job, res, facts, plan, {})  # the question comes once every part is checked
            return
        self._ask(job, res, facts)

    def _ask(self, job, res, facts):
        try:
            if self.ctx.runner.busy:
                job["say"](_("Wait until the current command finishes."))
                job["end"]()
                return
            space, plan = facts.get("space"), facts.get("plan")
            cleaning = free_steps(space["free"]) if space else []
            parts = part_steps(plan, job["opts"]) + [final_step(job["now"])] if plan and plan["feasible"] else []
            dlg = dup_question(job["parent"], res, dict(now=job["now"], download=job["download"], parts=parts,
                                                        free=cleaning), facts)
            key = dlg.exec()
            dlg.deleteLater()
        finally:
            self._dup_asking = False
        if key == "now":
            self._run_job(job, [job["now"]], _("zypper dup"))
        elif key == "parts":
            self._run_parts(job, plan)
        elif key == "download":
            self.ctx.run([_copy(job["download"])], _("zypper dup, only download"),
                         on_done=lambda ok: self._downloaded(job, ok))
        elif key == "remind":
            self._remind_tomorrow(job)
        elif key == "free":
            self.ctx.run(cleaning, _("Free up space"), on_done=lambda ok: self._job_freed(job, cleaning))
        elif key == "history":
            job["cancel"]()
            job["end"]()
            if not job["routine"]:
                self.dup_dialog.hide()  # History is in the main window, under the dup window
            self.ctx.go("history")
            win = self.window()
            win.raise_()
            win.activateWindow()
        else:
            job["cancel"]()
            job["end"]()

    def _run_job(self, job, steps, title, parts=0):
        """The chosen steps, then the rest of the routine (when it is the routine's job). parts: how many of the
        steps are parts of an update in parts (for the message when one of them stops)."""
        chain = [_copy(st) for st in list(steps) + list(job["post"])]
        if chain:
            chain[0].cond = "always"  # nothing before it in this run
        job["say"]("")  # "Looking at the free space…" and the like are over: the terminal says what runs
        if job["routine"]:
            title = _("Routine: {text}").format(text=routine.steps_text(chain))
            self.run_steps = chain
            self._show_steps(chain, custom=True)  # the band shows what runs
            self._set_running(True)

        def done(ok):
            job["done"](ok)
            if parts and not ok:
                self._parts_stopped(job, chain[:parts])

        if not self.ctx.run(chain, title, on_done=done) and job["routine"]:
            self._set_running(False)
            self._restore_band()

    def _parts_stopped(self, job, parts):
        """Which part stopped, and that its downloads stay."""
        n = len(parts)
        i = next((k for k, st in enumerate(parts, 1) if st.state == "failed"), None)
        if i is None:
            return  # stopped before the parts, or after them
        text = _("The update in parts stopped at part {i} of {n}: the parts after it and the full update did not "
                 "run. What it downloaded and did not install stays in the package cache, so the next try does "
                 "not download it again.").format(i=i, n=n)
        self.ctx.terminal.view.note(text, "info")
        label = self.result_lbl if job["routine"] else self.sim_lbl
        job["say"]((label.text() + "\n" + text).strip())

    # ------------------------------------------------ in parts: every part is simulated before the question
    def _part_sim_step(self, names, opts):
        args = heaps.part_args(names, opts)
        i = args.index("--")
        glob = list(opts.get("globals") or ())
        return Step("zypper", ["--xmlout", "-n", *glob, *args[:i], "--dry-run", *args[i:]], lang="C", capture=True,
                    cond="always",
                    shown=heaps.shown(["zypper", "--xmlout", "-n", *glob, *args[:i], "--dry-run", "--"], names, _more))

    def _check_parts(self, job, res, facts, p, sims):
        """Simulate every part of plan p that is not simulated yet, all in one run (dry runs: nothing changes, and
        the password given for the simulation of the dup is still remembered). Then heaps.review(): what needs
        a decision goes to the final dup, the plan is made again with the real sizes, and a part that does not
        fit is cut in two; the new parts are simulated in turn. Only then the question."""
        todo = [part for part in p["parts"] if heaps.part_key(part["names"]) not in sims]
        if not todo:
            self._parts_measured(job, res, facts, p, sims, {}, None)
            return
        steps = [self._part_sim_step(part["names"], job["opts"]) for part in todo]
        n = len(steps)
        if self.ctx.run(steps, ngettext("Simulation of {n} part", "Simulation of {n} parts", n).format(n=n),
                        on_done=lambda ok: self._parts_simulated(job, res, facts, p, sims, todo, steps)):
            job["say"](ngettext("Checking the part with a simulation before asking…",
                                "Checking each of the {n} parts with a simulation before asking…", n).format(n=n))
        else:
            self._job_end(job, _("Wait until the current command finishes."))

    def _parts_simulated(self, job, res, facts, p, sims, todo, steps):
        if any(st.code in (126, 127) or st.state == "skipped" for st in steps):
            self._job_end(job, _("Not simulated: the administrator password was not given."))
            return
        new = []
        for part, st in zip(todo, steps):
            r = parse_dry_run(st.output, source="zypper update")
            new.append((heaps.part_key(part["names"]), r))
        run_async(self._measure_parts, new, bool(facts.get("snapshots")),
                  on_done=lambda m, err: self._parts_measured(job, res, facts, p, sims, m, err))

    @staticmethod
    def _measure_parts(new, snapshots):
        """{key: dict(res, download, growth)}: what each simulated part needs (rpm as the user, in a thread)."""
        out = {}
        for key, r in new:
            download, growth = heaps.part_need(r, snapshots) if not r["problems"] else (0, 0)
            out[key] = dict(res=r, download=download, growth=growth)
        return out

    def _parts_measured(self, job, res, facts, p, sims, m, err):
        if err or m is None:
            self._job_end(job, _("Nothing has been changed.") + (f" ({err})" if err else ""))
            return
        sims.update(m)
        try:
            out = heaps.review(p, sims, res, facts["space"])
        except Exception as e:  # the check is a help: without it there is no update in parts
            out = dict(action="stop", moved=[], plan=dict(p, feasible=False, reason="error", error=str(e)))
        if out["moved"]:
            self.ctx.terminal.view.note(
                _("Left to the full update at the end (their update needs a decision): {names}").format(
                    names=", ".join(out["moved"])), "info")
        if out["action"] == "again":
            self._check_parts(job, res, facts, out["plan"], sims)
            return
        if out["action"] == "stop" and out["plan"].get("reason") == "error":
            self._job_end(job, _("zypper could not simulate a part of the update, so nothing has been changed:")
                          + "\n" + str(out["plan"].get("error") or "")[:600])
            return
        self._ask(job, res, dict(facts, plan=out["plan"]))

    def _run_parts(self, job, plan):
        """The parts (each one only if the one before worked), then the full update (also
        only if they all worked), then the rest of the routine."""
        steps = part_steps(plan, job["opts"]) + [final_step(job["now"])]
        n = len(plan["parts"])
        self._run_job(job, steps, ngettext("zypper dup, in {n} part", "zypper dup, in {n} parts", n).format(n=n),
                      parts=n)

    # ------------------------------------------------ the other answers
    def _remind_tomorrow(self, job):
        """"Remind me tomorrow": the tray icon reminds. It is started when it is not running, or nobody would
        remind; update_reminder_reason tells it what to say."""
        when = tomorrow_morning()
        s = self.ctx.settings
        s["update_reminder_at"] = when
        s["update_reminder_reason"] = "big"
        try:
            s.save()
        except OSError as e:
            self._job_end(job, _("Nothing has been changed. The reminder could not be saved: {error}").format(
                error=e))
            return
        from .. import tray
        try:
            reminds = tray.poke("reload") or tray.start_tray()  # a running tray reads the settings again
        except Exception:
            reminds = False
        at = QLocale().toString(QDateTime.fromSecsSinceEpoch(when).time(), QLocale.FormatType.ShortFormat)
        if reminds:
            self._job_end(job, _("Nothing has been changed. You will be reminded tomorrow at {time}.").format(time=at))
        else:
            self._job_end(job, _("Nothing has been changed. The tray icon, which reminds you, could not be started: "
                                 "look at the update again tomorrow."))

    def _downloaded(self, job, ok):
        self._last_sim = None  # what is left to download is not what it was
        if not job["routine"]:
            self.sim_tree.clear()
        self._job_end(job, _("Downloaded. When you are ready, run the update: it will not download it again.")
                      if ok else _("The download stopped with errors: look at the terminal."))

    def _job_freed(self, job, cleaning):
        self._last_sim = None
        if any(st.code in (126, 127) for st in cleaning):
            self._job_end(job, _("Nothing was freed: the administrator password was not given."))
            return
        self._after_change()  # old kernels may have gone
        job["say"](_("Simulating the update again…"))
        QTimer.singleShot(0, job["resimulate"])  # new numbers, and the question again

    def _dup_done(self, ok):
        # the simulation shown is old now
        self._last_sim = None
        self.sim_tree.clear()
        self.sim_lbl.setText(_("zypper dup finished.") if ok else _("zypper dup stopped with errors: look at the "
                                                                    "terminal."))
        self._after_change()
