# SPDX-License-Identifier: GPL-3.0-or-later
"""The icon in the system tray: it tells you when there are updates (Discover did this before).

'zypp-desk --tray' starts it (at login, from ~/.config/autostart). It is a small process of its own that only
reads, as the user, never changing anything:
- 'zypper list-updates' on the lists that are already on disk: the system timer zypp-desk-refresh refreshes them
  as root, without a password, and never installs anything;
- 'flatpak remote-ls --updates', the programs from the web (Web sources) and fwupd (firmware);
- 'zypper needs-rebooting'.
Its buttons start the window ('zypp-desk --page updates', '--run-routine'); an open window takes the request.

One tray per user: a lock file and a local socket in $XDG_RUNTIME_DIR. The window talks to it through the
socket: poke("check") after the routine, poke("reload") when the notification settings change.
"""

import datetime
import fcntl
import html
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import (QCoreApplication, QEvent, QFileSystemWatcher, QLocale, QMetaObject, QObject, QPoint,
                            QProcess, QProcessEnvironment, QRect, QSize, Qt, QTimer, Q_ARG, Q_RETURN_ARG, Signal, Slot,
                            SLOT)
from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QMenu, QSizePolicy,
                               QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

from . import APP_ID, APP_NAME, backend, flatpak, websources
from .i18n import _, ngettext
from .style import BLUE, GREEN, GREEN_DARK, ORANGE, PURPLE
from .tasks import run_async
from .ui import button, icon, muted

FIRST_CHECK_S = 120          # the first look, a while after login (the desktop is busy starting)
TICK_MS = 60_000             # how often the tray asks itself whether a check is due (wall clock: survives sleep)
RETRY_AFTER_ERROR_S = 1800   # a part that failed (no network yet after waking up) is asked again sooner
WATCH_SETTLE_MS = 60_000     # after the zypp cache or the rpm database changes, wait until it is quiet…
WATCH_MIN_GAP_S = 600        # …and look at most once every ten minutes for that reason
INTERVALS = (1, 3, 6, 12, 24)
SOLV_DIR = "/var/cache/zypp/solv"  # rewritten when the repositories are refreshed (by the timer or anyone)
STALE_S = 24 * 3600          # lists of the repositories older than this are shown in orange (the timer runs every 6 h)
RPMDB_FILES = ("/usr/lib/sysimage/rpm/rpmdb.sqlite", "/usr/lib/sysimage/rpm/Packages.db",
               "/var/lib/rpm/rpmdb.sqlite", "/var/lib/rpm/Packages.db")  # written when packages change
AUTOSTART_NAME = f"{APP_ID}.tray.desktop"
NAMES_IN_NOTIFICATION = 3

NOTIFY_SERVICE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def _log(text):
    print(f"zypp-desk tray: {text}", file=sys.stderr, flush=True)


# ============================================================ one tray per user, and talking to it

def socket_name():
    from .app import socket_path
    return socket_path("tray")


def _lock_path():
    return socket_name() + ".lock"


_lock_fd = None


def _take_lock():
    """Hold an exclusive lock for the life of the process: a second tray started at the same moment gives up."""
    global _lock_fd
    try:
        fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        _log(f"cannot open the lock file: {e}")
        return True  # no lock possible: still better to show the icon
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd
    return True


def poke(command="ping", timeout_ms=400):
    """Send a word to the running tray (ping, check, reload, show, quit). True if a tray took it."""
    sock = QLocalSocket()
    sock.connectToServer(socket_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(command.encode("ascii", "ignore") + b"\n")
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


def tray_running():
    return poke("ping")


# ============================================================ starting the window (or another tray)

def main_command(args):
    """(program, arguments, extra environment) that starts Zypp Desk: the zypp-desk launcher when it is on
    PATH (the package, or install.sh's link in ~/.local/bin), else this source tree."""
    exe = shutil.which("zypp-desk")
    if exe:
        return exe, list(args), {}
    src = str(Path(__file__).resolve().parent.parent)
    old = os.environ.get("PYTHONPATH")
    return sys.executable, ["-P", "-m", "zypp_desk", *args], {"PYTHONPATH": src + (":" + old if old else "")}


def start_detached(program, args, extra_env=None):
    """Start a program that lives on its own (closing the tray does not close it)."""
    proc = QProcess()
    proc.setProgram(program)
    proc.setArguments(list(args))
    env = QProcessEnvironment.systemEnvironment()
    for k, v in (extra_env or {}).items():
        env.insert(k, v)
    proc.setProcessEnvironment(env)
    proc.setStandardInputFile(QProcess.nullDevice())
    r = proc.startDetached()  # (ok, pid) in older PySide6, only ok in 6.12
    ok = bool(r[0]) if isinstance(r, tuple) else bool(r)
    if not ok:
        _log(f"could not start {program}")
    return ok


def open_main(args=(), token=None):
    """Open the window (an open one takes the request). 'token' lets Wayland bring it to the front."""
    program, a, env = main_command(args)
    if token:
        env["XDG_ACTIVATION_TOKEN"] = token
    return start_detached(program, a, env)


def start_tray():
    program, a, env = main_command(["--tray"])
    return start_detached(program, a, env)


# ============================================================ autostart at login

def autostart_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / AUTOSTART_NAME


def _desktop_quote(arg):
    """One argument of a desktop entry's Exec key (quoting rules of the Desktop Entry Specification)."""
    if re.fullmatch(r"[A-Za-z0-9_@+=:,./-]+", arg):
        return arg
    quoted = re.sub(r'(["`$\\])', r"\\\1", arg)
    # the string escape (\\ → \) is applied before the quoting: every backslash is written twice
    return '"' + quoted.replace("\\", "\\\\").replace("%", "%%") + '"'


def _desktop_string(value):
    """A desktop entry string value (TryExec, Comment): backslashes and line breaks escaped."""
    return value.replace("\\", "\\\\").replace("\n", " ").replace("\r", " ").replace("\t", " ")


def autostart_exec():
    """(Exec, TryExec). TryExec makes the desktop skip the entry quietly while that program is not there."""
    if shutil.which("zypp-desk"):
        return "zypp-desk --tray", "zypp-desk"  # the package or install.sh: follows whichever is installed
    src = Path(__file__).resolve().parent.parent
    launcher = src / "zypp-desk"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return _desktop_quote(str(launcher)) + " --tray", str(launcher)
    return (" ".join(_desktop_quote(a) for a in ("env", f"PYTHONPATH={src}", sys.executable, "-P", "-m", "zypp_desk",
                                                  "--tray")), sys.executable)


def autostart_text():
    """The entry in ~/.config/autostart (visible in Plasma's Autostart settings, where it can also be removed)."""
    exec_line, try_exec = autostart_exec()
    comment = _desktop_string(_("Tells you when there are updates"))
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Comment={comment}\n"
            f"TryExec={_desktop_string(try_exec)}\n"
            f"Exec={exec_line}\n"
            f"Icon={APP_ID}\n"
            "Terminal=false\n"
            "StartupNotify=false\n"
            "X-KDE-autostart-phase=2\n")


def system_autostart_path():
    """The entry the package installs for everyone (/etc/xdg/autostart), or None (install.sh, the source tree)."""
    for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":"):
        if d:
            p = Path(d) / "autostart" / AUTOSTART_NAME
            if p.is_file():
                return p
    return None


def write_autostart():
    """Start the tray at login. With the package's entry for everyone, it is enough to take away a user entry
    that switched it off (the user entry, same name, wins over the system one)."""
    if system_autostart_path():
        try:
            autostart_path().unlink()
        except FileNotFoundError:
            pass
        return
    p = autostart_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(autostart_text(), encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(p)


def remove_autostart():
    """Do not start the tray at login. With the package's entry for everyone, a user entry with Hidden=true
    switches it off for this user only (Desktop Application Autostart Specification)."""
    if system_autostart_path():
        p = autostart_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(autostart_text() + "Hidden=true\n", encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(p)
        return
    try:
        autostart_path().unlink()
    except FileNotFoundError:
        pass


def autostart_enabled():
    """True if the tray starts at login: the file is there and not switched off (Hidden, or GNOME's own key)."""
    try:
        text = autostart_path().read_text(encoding="utf-8", errors="replace")
    except OSError:  # no entry of this user: the package's one for everyone, if there is one
        sysp = system_autostart_path()
        if sysp is None:
            return False
        try:
            text = sysp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    return not re.search(r"^\s*(Hidden\s*=\s*true|X-GNOME-Autostart-enabled\s*=\s*false)\s*$", text, re.M | re.I)


# ============================================================ what there is to update (in a thread)

def web_updates():
    """Programs from the web with something new, counted like the Updates page does (a newer version, or a
    changed file when the version cannot be read). What the web said is kept ten minutes; GitHub is asked with
    the ETag of its last answer (websources.check, never forced)."""
    sources = [s for s in websources.load() if s.get("type", "github") != "manual"]
    out, failed = [], []
    for src in sources:
        try:
            f = websources.check(src)
        except Exception as e:  # one site that does not answer must not hide the others
            failed.append(f"{src.get('package')}: {e}")
            continue
        if f.get("newer") or (f.get("newer") is None and f.get("changed")):
            out.append(dict(name=src["package"], old=f.get("installed") or "", new=f.get("version") or ""))
    for line in failed:
        _log(f"web source: {line}")
    if sources and len(failed) == len(sources):
        raise RuntimeError(failed[0])
    return out


def _repo_updates():
    return [dict(name=u["name"], old=u.get("old", ""), new=u.get("edition", ""), repo=u.get("repo", ""))
            for u in backend.list_updates()]


def _flatpak_updates():
    if not flatpak.available():
        return []
    return [dict(name=u.get("application", ""), new=u.get("version", ""), origin=u.get("origin", ""))
            for u in flatpak.updates()]


def _firmware_updates():
    """A list, or None when fwupd is not there."""
    try:
        from . import firmware
    except ImportError:
        return None
    if not firmware.available():
        return None
    return [dict(name=d.get("name", "?"), old=d.get("version", ""), new=d.get("new_version", ""))
            for d in firmware.updates()]


def _needs_reboot():
    return backend.run_c(["needs-rebooting"], timeout=120).returncode == 102


def empty_result():
    return {"time": time.time(), "repo": None, "flatpak": None, "web": None, "firmware": None,
            "firmware_state": "off", "reboot": None, "refreshed": None, "errors": {}}


def gather(settings):
    """Look everywhere at once. One part that fails is noted in 'errors' and the others still count."""
    result = empty_result()
    jobs = {"repo": _repo_updates, "flatpak": _flatpak_updates, "web": web_updates, "reboot": _needs_reboot}
    if settings.get("notify_firmware", True):
        jobs["firmware"] = _firmware_updates
        result["firmware_state"] = "ok"
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="tray-check") as ex:
        futures = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, fut in futures.items():
            try:
                result[k] = fut.result()
            except Exception as e:
                result["errors"][k] = str(e).strip() or type(e).__name__
                _log(f"{k}: {result['errors'][k]}")
    if "firmware" in jobs:
        if "firmware" in result["errors"]:
            result["firmware_state"] = "error"
        elif result["firmware"] is None:
            result["firmware_state"] = "missing"
    try:
        result["refreshed"] = backend.repos_refreshed_at()  # the lists are only as new as their last refresh
    except Exception as e:
        _log(f"refreshed: {e}")
    result["time"] = time.time()
    return result


def counts(result):
    """How many in each part (None: not known) and the total that the icon and the notification show."""
    c = {}
    for k in ("repo", "flatpak", "web", "firmware"):
        v = (result or {}).get(k)
        c[k] = len(v) if isinstance(v, list) else None
    c["total"] = sum(v for k, v in c.items() if v)
    return c


# ============================================================ the notification: when and what

def should_notify(state, total, today, enabled=True):
    """At most one notification a day: the first check of a day that finds something tells it. A number that
    grows later the same day waits for the next day (the icon shows it at once)."""
    if not enabled or total <= 0:
        return False
    return state.get("day") != today


def _names(items, limit=NAMES_IN_NOTIFICATION):
    names = [i.get("name") or "?" for i in items]
    text = ", ".join(names[:limit])
    return text + ("…" if len(names) > limit else "")


def notification_text(result):
    """(summary, body) in plain text."""
    c = counts(result)
    n = c["total"]
    summary = ngettext("There is {n} update", "There are {n} updates", n).format(n=n)
    parts = [(k, result.get(k)) for k in ("repo", "flatpak", "web", "firmware") if c[k]]
    lines = []
    if len(parts) == 1 and parts[0][0] == "repo":
        lines.append(_("All from the repositories: {names}").format(names=_names(parts[0][1])))
    else:
        labels = {"repo": _("Repositories: {names}"), "flatpak": _("Flatpak: {names}"), "web": _("Web: {names}"),
                  "firmware": _("Firmware: {names}")}
        lines += [labels[k].format(names=_names(items)) for k, items in parts]
    if result.get("reboot"):
        lines.append(_("The computer must restart to finish an earlier update."))
    return summary, "\n".join(lines)


def reminder_due(settings, now=None):
    """The time of the reminder when it is due (settings["update_reminder_at"] is set and past), else 0.

    "Wait and try again tomorrow" (pages/problems.py) sets it when an upgrade has many problems that usually go
    away by themselves, such as a repository that is a day or two late after a new Tumbleweed."""
    try:
        at = int(float(settings.get("update_reminder_at") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0
    return at if at > 0 and (now or time.time()) >= at else 0


def reminder_text():
    """(summary, body) of the reminder, in plain text."""
    return (_("Time to try the update again"),
            _("You asked Zypp Desk to remind you. Simulate the update again: the problems that fix themselves may "
              "be gone by now."))


def _state_path():
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "zypp-desk" / "tray.json"


def load_state():
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state):
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        _log(f"cannot save {p}: {e}")


def notify_icon_name():
    """The icon for the notification server: by name when the theme has it, else the file of the source tree."""
    if QIcon.hasThemeIcon(APP_ID):
        return APP_ID
    f = Path(__file__).resolve().parent.parent / "data" / "icons" / f"{APP_ID}.svg"
    return str(f) if f.exists() else "system-software-update"


class DBusNotifier(QObject):
    """Desktop notifications through org.freedesktop.Notifications, with buttons.

    The call goes through a QDBusInterface (its method types come from the server's own description), because
    PySide6 sends a Python int as int32 and the server wants uint32 for 'replaces_id'. Plasma describes Notify
    fully; with a server that does not, send() returns None and the tray uses its own balloon instead.
    Plasma sends ActionInvoked only to the program that showed the notification: this process.
    """

    action = Signal(str, str)  # action key, activation token ("" when none)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bus = QDBusConnection.sessionBus()
        self._ids = set()
        self._tokens = {}
        if self._bus.isConnected():
            for name, slot in (("ActionInvoked", "_on_action(QDBusMessage)"),
                               ("NotificationClosed", "_on_closed(QDBusMessage)"),
                               ("ActivationToken", "_on_token(QDBusMessage)")):
                self._bus.connect(NOTIFY_SERVICE, NOTIFY_PATH, NOTIFY_SERVICE, name, self, SLOT(slot))

    def send(self, summary, body, actions, icon_name, hints=None, timeout=-1):
        """Show a notification. actions: [(key, label), …]. Returns its id, or None if it could not be sent."""
        if not self._bus.isConnected():
            return None
        iface = QDBusInterface(NOTIFY_SERVICE, NOTIFY_PATH, NOTIFY_SERVICE, self._bus)
        if not iface.isValid():
            return None
        iface.setTimeout(5000)
        caps = []
        reply = iface.call("GetCapabilities")
        if reply.arguments() and isinstance(reply.arguments()[0], (list, tuple)):
            caps = list(reply.arguments()[0])
        if "body-markup" in caps:  # names come from repositories and web sites: never markup
            body = html.escape(body, quote=False)
        flat = []
        if "actions" in caps:
            for key, label in actions:
                flat += [key, label]
        try:
            nid = QMetaObject.invokeMethod(
                iface, "Notify", Qt.ConnectionType.DirectConnection, Q_RETURN_ARG("uint"),
                Q_ARG(str, APP_NAME), Q_ARG("uint", 0), Q_ARG(str, icon_name), Q_ARG(str, summary), Q_ARG(str, body),
                Q_ARG("QStringList", flat), Q_ARG("QVariantMap", dict(hints or {})), Q_ARG(int, timeout))
        except (RuntimeError, TypeError, ValueError) as e:
            _log(f"notification not sent: {e}")
            return None
        if not nid or iface.lastError().isValid():
            _log(f"notification not sent: {iface.lastError().message()}")
            return None
        self._ids.add(int(nid))
        return int(nid)

    @Slot(QDBusMessage)
    def _on_action(self, msg):
        args = msg.arguments()
        if len(args) >= 2 and args[0] in self._ids:
            self.action.emit(str(args[1]), self._tokens.pop(args[0], ""))

    @Slot(QDBusMessage)
    def _on_token(self, msg):
        args = msg.arguments()
        if len(args) >= 2 and args[0] in self._ids:
            self._tokens[args[0]] = str(args[1])

    @Slot(QDBusMessage)
    def _on_closed(self, msg):
        args = msg.arguments()
        if args:
            self._ids.discard(args[0])
            self._tokens.pop(args[0], None)


# ============================================================ how it looks

def badge_icon(base, n, sizes=(16, 22, 24, 32, 48, 64)):
    """The app icon with the number of updates in a small orange badge (top right), as on the panel."""
    if n <= 0:
        return base
    text = str(n) if n < 100 else "99+"
    out = QIcon()
    for s in sizes:
        pm = QPixmap(s, s)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base.paint(p, QRect(0, 0, s, s))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(max(7, round(s * 0.40)))
        fm = QFontMetrics(f)
        h = max(9, round(s * 0.52))
        w = min(s, max(h, fm.horizontalAdvance(text) + round(h * 0.5)))
        r = QRect(s - w, 0, w, h)
        p.setPen(QColor(255, 255, 255, 200))
        p.setBrush(QColor(ORANGE))
        p.drawRoundedRect(r.adjusted(0, 0, -1, -1), h / 2, h / 2)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        out.addPixmap(pm)
    return out


def when_text(ts, now=None):
    if not ts:
        return _("Not checked yet")
    now = now or time.time()
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.fromtimestamp(now)
    t = QLocale().toString(d.time(), QLocale.FormatType.ShortFormat)
    if d.date() == today:
        return _("Checked today at {time}").format(time=t)
    if d.date() == today - datetime.timedelta(days=1):
        return _("Checked yesterday at {time}").format(time=t)
    return _("Checked on {date} at {time}").format(date=QLocale().toString(d.date(), QLocale.FormatType.ShortFormat),
                                                   time=t)


def refreshed_text(ts, now=None):
    """(text, stale) about when the repositories were last refreshed, or ("", False) when it is not known.
    The count of updates is only as new as these lists: "Checked today" must not hide that they are old."""
    if not ts:
        return "", False
    now = now or time.time()
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.fromtimestamp(now)
    t = QLocale().toString(d.time(), QLocale.FormatType.ShortFormat)
    if d.date() >= today:
        text = _("Repositories refreshed today at {time}").format(time=t)
    elif d.date() == today - datetime.timedelta(days=1):
        text = _("Repositories refreshed yesterday at {time}").format(time=t)
    else:
        n = (today - d.date()).days
        text = ngettext("Repositories refreshed {n} day ago", "Repositories refreshed {n} days ago", n).format(n=n)
    return text, now - ts > STALE_S


def headline(result, checking=False):
    """(big text, colour) for the popup and the first line of the tooltip: the same words in both."""
    c = counts(result)
    if result is None:
        return (_("Looking…") if checking else _("Not checked yet")), None
    if c["total"]:
        return ngettext("{n} update", "{n} updates", c["total"]).format(n=c["total"]), GREEN
    if result.get("errors", {}).get("repo") or result.get("errors", {}).get("all"):
        return _("Could not check"), ORANGE
    return _("Everything is up to date"), GREEN


def _part_name(k):
    return {"repo": _("the repositories"), "flatpak": _("Flatpak"), "web": _("the web"),
            "reboot": _("whether a restart is needed"), "all": _("everything")}.get(k, k)


def pill_text(kind, n):
    if n is None:
        return {"repo": _("Repositories: ?"), "flatpak": _("Flatpak: ?"), "web": _("Web: ?"),
                "firmware": _("Firmware: ?")}[kind]
    return {"repo": ngettext("{n} from the repositories", "{n} from the repositories", n),
            "flatpak": ngettext("{n} Flatpak", "{n} Flatpak", n),
            "web": ngettext("{n} from the web", "{n} from the web", n),
            "firmware": ngettext("{n} firmware", "{n} firmware", n)}[kind].format(n=n)


def _rgba(color, alpha):
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


class FlowLayout(QLayout):
    """Widgets in a row that goes on in the next line when it is full (the pills, in any language)."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
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
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _place(self, rect, move):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            if it.widget() is not None and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            hint = it.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line = rect.x(), y + line + self._gap, 0
            if move:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - rect.y()


class _ClickLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


class Popup(QWidget):
    """What a click on the icon shows: when it last looked, how many updates and where from, and the buttons."""

    run_routine = Signal()
    open_app = Signal()
    open_updates = Signal()
    check_now = Signal()
    open_settings = Signal()

    def __init__(self, parent=None):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self._was_active = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        card = QFrame(self)
        card.setObjectName("card")
        outer.addWidget(card)
        lay = self.card_box = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        logo = QLabel(card)
        from .app import app_icon
        logo.setPixmap(app_icon().pixmap(32, 32))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel(APP_NAME, card)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        names.addWidget(title)
        self.when = muted("", card)
        names.addWidget(self.when)
        self.fresh = muted("", card)
        self.fresh.hide()
        names.addWidget(self.fresh)
        head.addLayout(names, 1)
        gear = QToolButton(card)
        gear.setIcon(icon("configure", "settings-configure", "preferences-system"))
        if gear.icon().isNull():
            gear.setText("⚙")
        gear.setAutoRaise(True)
        gear.setToolTip(_("Settings"))
        gear.setAccessibleName(_("Settings"))
        gear.clicked.connect(lambda: self._emit(self.open_settings))
        head.addWidget(gear, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        self.big = _ClickLabel(card)
        f = self.big.font()
        f.setBold(True)
        f.setPixelSize(26)
        self.big.setFont(f)
        self.big.setWordWrap(True)
        self.big.setCursor(Qt.CursorShape.PointingHandCursor)
        self.big.setToolTip(_("Open Updates"))
        self.big.clicked.connect(lambda: self._emit(self.open_updates))
        lay.addWidget(self.big)

        pills = self.pill_box = FlowLayout(spacing=6)
        self.pills = {}
        for kind, color in (("repo", BLUE), ("flatpak", PURPLE), ("web", ORANGE), ("firmware", GREEN_DARK)):
            lbl = QLabel(card)
            f = lbl.font()
            f.setPointSizeF(f.pointSizeF() * 0.9)
            lbl.setFont(f)
            lbl.setStyleSheet(f"background: {_rgba(color, 0.14)}; color: {color}; border-radius: 7px; "
                              "padding: 2px 8px;")
            lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            pills.addWidget(lbl)
            self.pills[kind] = lbl
        lay.addLayout(pills)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        grid.addWidget(muted(_("Firmware"), card, wrap=False), 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(muted(_("Restart needed"), card, wrap=False), 1, 0, Qt.AlignmentFlag.AlignTop)
        self.fw = QLabel(card)
        self.reboot = QLabel(card)
        for lbl in (self.fw, self.reboot):
            lbl.setTextFormat(Qt.TextFormat.PlainText)  # firmware names come from fwupd and LVFS
            lbl.setWordWrap(True)
        grid.addWidget(self.fw, 0, 1)
        grid.addWidget(self.reboot, 1, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        self.errors = muted("", card)
        self.errors.setTextFormat(Qt.TextFormat.PlainText)
        self.errors.hide()
        lay.addWidget(self.errors)

        self.run_btn = button(_("Run the routine"), "view-refresh", lambda: self._emit(self.run_routine),
                              primary=True, parent=card)
        self.run_btn.setMinimumHeight(38)
        lay.addWidget(self.run_btn)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.open_btn = button(_("Open Zypp Desk"), None, lambda: self._emit(self.open_app), parent=card)
        self.check_btn = button(_("Check now"), None, lambda: self._emit(self.check_now), parent=card)
        row.addWidget(self.open_btn, 1)
        row.addWidget(self.check_btn, 1)
        lay.addLayout(row)
        self.setFixedWidth(372)
        self.set_state(None, False)

    def _emit(self, signal):
        self.hide()
        signal.emit()

    def set_state(self, result, checking):
        c = counts(result)
        if checking:
            self.when.setText(_("Looking for updates…"))
        elif result is None:
            self.when.setText(_("It looks for updates in a moment."))
        else:
            self.when.setText(when_text(result.get("time")))
        self.check_btn.setEnabled(not checking)
        known = result is not None
        text, color = headline(result, checking)
        self.big.setText(text)
        self.big.setStyleSheet(f"color: {color};" if color else "color: palette(placeholder-text);")
        fresh, stale = refreshed_text((result or {}).get("refreshed"))
        self.fresh.setText(fresh)
        self.fresh.setStyleSheet(f"color: {ORANGE};" if stale else "color: palette(placeholder-text);")
        self.fresh.setVisible(bool(fresh))
        # the big button runs the routine only when there is something to update: not while nothing is known
        # yet, not when everything is up to date and not when the check failed (then: open Zypp Desk, check again)
        self.run_btn.setVisible(known and c["total"] > 0)
        for kind, lbl in self.pills.items():
            n = c[kind]
            lbl.setText(pill_text(kind, n))
            lbl.setVisible(known and (kind != "firmware" or bool(n)))
        self.fw.setText(self._firmware_text(result) if known else "—")
        reboot = (result or {}).get("reboot")
        self.reboot.setText("—" if reboot is None else (_("yes, to finish an earlier update") if reboot else _("no")))
        self.reboot.setStyleSheet(f"color: {ORANGE}; font-weight: 600;" if reboot else "")
        failed = [k for k in ("repo", "flatpak", "web", "reboot", "all") if k in (result or {}).get("errors", {})]
        self.errors.setText(_("Could not check: {parts}.").format(parts=", ".join(_part_name(k) for k in failed)))
        self.errors.setVisible(bool(failed))
        if self.isVisible():
            self._fit()

    def _fit(self):
        """Exactly as tall as what it shows now (it may have shown more lines before). Only measured while
        shown: a hidden window does not hear that its lines changed."""
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        for box in (self.pill_box, self.card_box, self.layout()):
            box.invalidate()
        lay = self.layout()
        lay.activate()
        h = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        self.setFixedHeight(max(h, lay.totalMinimumSize().height()))

    @staticmethod
    def _firmware_text(result):
        st = result.get("firmware_state")
        if st == "off":
            return _("not checked (turned off in the settings)")
        if st == "missing":
            return _("fwupd is not installed")
        if st == "error":
            return _("could not check")
        items = result.get("firmware") or []
        if not items:
            return _("nothing new")
        first = ", ".join(f"{i['name']} {i.get('old') or '?'} → {i.get('new') or '?'}" for i in items[:2])
        return ngettext("{n} update: {what}", "{n} updates: {what}", len(items)).format(
            n=len(items), what=first + ("…" if len(items) > 2 else ""))

    def show_near(self, anchor):
        """Next to the tray icon. On Wayland a program cannot place its windows: there the desktop does it."""
        self._was_active = False
        self.show()
        self._fit()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        if screen is not None and not QGuiApplication.platformName().startswith("wayland"):
            area = screen.availableGeometry()
            w, h = self.width(), self.height()
            x = min(max(anchor.x() - w + 24, area.left() + 4), area.right() - w - 4)
            y = anchor.y() - h - 8 if anchor.y() - h - 8 >= area.top() else anchor.y() + 16
            y = min(max(y, area.top() + 4), area.bottom() - h - 4)
            self.move(x, y)
        self.raise_()
        self.activateWindow()

    def event(self, ev):
        if ev.type() == QEvent.Type.WindowActivate:
            self._was_active = True
        elif ev.type() == QEvent.Type.WindowDeactivate and self._was_active:
            self.hide()  # a click anywhere else closes it, like a menu
        return super().event(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(ev)


# ============================================================ the tray itself

class Tray(QObject):
    def __init__(self, app, settings, parent=None):
        super().__init__(parent)
        self.app = app
        self.settings = settings
        self.last = None
        self.checking = False
        self._started = time.time()
        self._last_start = None
        self._watch_pending = False
        self._icon_tries = 0
        self.server = None
        from .app import app_icon
        self.base_icon = app_icon()

        self.icon = QSystemTrayIcon(self)
        self.icon.setIcon(self.base_icon)
        self.menu = QMenu()
        self.menu.addAction(self.base_icon, _("Open Zypp Desk"), self.open_app)
        self.menu.addAction(icon("view-refresh"), _("Run the routine"), self.run_routine)
        self.check_action = self.menu.addAction(icon("view-refresh"), _("Check now"), self.check_now)
        self.menu.addSeparator()
        self.menu.addAction(icon("configure"), _("Settings…"), self.open_settings)
        self.menu.addSeparator()
        self.menu.addAction(icon("application-exit"), _("Quit"), self.app.quit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self.open_updates)

        self.popup = Popup()
        self.popup.run_routine.connect(self.run_routine)
        self.popup.open_app.connect(self.open_app)
        self.popup.open_updates.connect(self.open_updates)
        self.popup.check_now.connect(self.check_now)
        self.popup.open_settings.connect(self.open_settings)

        self.notifier = DBusNotifier(self)
        self.notifier.action.connect(self._notification_action)

        self.tick = QTimer(self)
        self.tick.setInterval(TICK_MS)
        self.tick.timeout.connect(self._on_tick)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._fs_changed)
        self.watcher.fileChanged.connect(self._fs_changed)
        self.settle = QTimer(self)
        self.settle.setSingleShot(True)
        self.settle.setInterval(WATCH_SETTLE_MS)
        self.settle.timeout.connect(self._watch_due)
        self.gap = QTimer(self)
        self.gap.setSingleShot(True)
        self.gap.timeout.connect(self._watch_due)
        self._refresh_look()

    # ------------------------------------------------ starting
    def start(self, show_icon=True):
        self.tick.start()
        QTimer.singleShot(FIRST_CHECK_S * 1000 + 500, self._on_tick)
        self._sync_watch()
        if show_icon:
            self._show_icon()

    def _show_icon(self):
        """At login the panel may not be ready yet: wait for it a little (5 minutes at most)."""
        if QSystemTrayIcon.isSystemTrayAvailable() or self._icon_tries >= 60:
            self.icon.show()
            return
        self._icon_tries += 1
        QTimer.singleShot(5000, self._show_icon)

    def listen(self):
        name = socket_name()
        QLocalServer.removeServer(name)  # left by a tray that crashed (the lock says none is running)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(name):
            _log(f"cannot listen on {name}: {self.server.errorString()}")
            return False
        self.server.newConnection.connect(self._new_client)
        return True

    def _new_client(self):
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: self._client_read(c))
            conn.disconnected.connect(conn.deleteLater)

    def _client_read(self, conn):
        while conn.canReadLine():
            self.command(bytes(conn.readLine()).decode("ascii", "replace").strip())
        if conn.bytesAvailable() > 1024:  # not one of ours
            conn.abort()

    def command(self, word):
        if word == "check":
            QTimer.singleShot(5000, self.check_now)  # the zypper that just finished may still be closing
        elif word == "reload":
            self._reload_settings()
            self._refresh_look()
            self._check_reminder()
        elif word == "show":
            self._show_popup()
        elif word == "quit":
            self.app.quit()

    # ------------------------------------------------ settings and timing
    def _reload_settings(self):
        """The window may have changed them: read settings.json again."""
        try:
            from .settings import Settings
            self.settings.update(Settings())
        except Exception as e:
            _log(f"cannot read the settings: {e}")

    def interval_s(self):
        try:
            h = int(self.settings.get("notify_interval_hours", 6))
        except (TypeError, ValueError):
            h = 6
        return min(max(h, 1), 168) * 3600

    def next_due(self):
        if self.last is None:
            return self._started + FIRST_CHECK_S
        wait = self.interval_s()
        if self.last.get("errors"):
            wait = min(wait, RETRY_AFTER_ERROR_S)
        return self.last["time"] + wait

    def _on_tick(self):
        self._reload_settings()
        self._check_reminder()
        if not self.checking and time.time() >= self.next_due():
            self.check_now()

    # ------------------------------------------------ watching the zypp cache and the rpm database
    def _watch_paths(self):
        paths = []
        if os.path.isdir(SOLV_DIR):
            paths.append(SOLV_DIR)
            try:
                paths += [e.path for e in os.scandir(SOLV_DIR) if e.is_dir(follow_symlinks=False)]
            except OSError:
                pass
        paths += [f for f in RPMDB_FILES if os.path.isfile(f)]
        return paths

    def _sync_watch(self):
        have = set(self.watcher.directories()) | set(self.watcher.files())
        want = self._watch_paths()
        new = [p for p in want if p not in have]
        if new:
            self.watcher.addPaths(new)
        gone = [p for p in have if p not in want]
        if gone:
            self.watcher.removePaths(gone)

    def _fs_changed(self, _path):
        self.settle.start()  # restarts while zypper or rpm keep writing

    def _watch_due(self):
        self._sync_watch()
        if self.checking:
            self._watch_pending = True
            return
        since = time.time() - (self._last_start or 0)
        if since < WATCH_MIN_GAP_S:
            self.gap.start(int((WATCH_MIN_GAP_S - since) * 1000) + 1000)
            return
        self.check_now()

    # ------------------------------------------------ checking
    def check_now(self):
        if self.checking:
            return
        self.checking = True
        self._last_start = time.time()
        self._reload_settings()
        self.check_action.setEnabled(False)
        self._refresh_look()
        run_async(gather, dict(self.settings), on_done=self._checked)

    def _checked(self, result, err):
        self.checking = False
        self.check_action.setEnabled(True)
        if err is not None or not isinstance(result, dict):
            result = empty_result()
            result["errors"]["all"] = str(err)
            _log(f"check failed: {err}")
        self.last = result
        self._refresh_look()
        self._maybe_notify()
        if self._watch_pending:
            self._watch_pending = False
            self._watch_due()

    # ------------------------------------------------ showing
    def tooltip_text(self):
        if self.checking:
            return _("Looking for updates…")
        if self.last is None:
            return _("It looks for updates in a moment.")
        c = counts(self.last)
        lines = [headline(self.last)[0]]
        lines.append(", ".join(pill_text(k, c[k]) for k in ("repo", "flatpak", "web")))
        if c["firmware"]:
            lines.append(pill_text("firmware", c["firmware"]))
        if self.last.get("reboot"):
            lines.append(_("Restart needed"))
        lines.append(when_text(self.last.get("time")))
        fresh, _stale = refreshed_text(self.last.get("refreshed"))
        if fresh:
            lines.append(fresh)
        return "\n".join(lines)

    def _refresh_look(self):
        n = counts(self.last)["total"] if self.last else 0
        self.icon.setIcon(badge_icon(self.base_icon, n))
        self.icon.setToolTip(f"{APP_NAME}\n{self.tooltip_text()}")
        self.popup.set_state(self.last, self.checking)

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.popup.isVisible():
                self.popup.hide()
            else:
                self._show_popup()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.check_now()

    def _show_popup(self):
        self.popup.set_state(self.last, self.checking)
        geo = self.icon.geometry()
        anchor = geo.center() if geo.isValid() and not geo.isEmpty() else QCursor.pos()
        self.popup.show_near(anchor)

    # ------------------------------------------------ notifying
    def _maybe_notify(self):
        c = counts(self.last)
        state = load_state()
        today = datetime.date.today().isoformat()
        if not should_notify(state, c["total"], today, bool(self.settings.get("notify_enabled", True))):
            return
        summary, body = notification_text(self.last)
        nid = self.notifier.send(summary, body, [("default", ""), ("routine", _("Run the routine")),
                                                 ("updates", _("Open Updates"))],
                                 notify_icon_name(), {"desktop-entry": APP_ID})
        if nid is None:
            if not self.icon.isVisible():
                return  # nowhere to say it: try again at the next check
            self.icon.showMessage(summary, body, self.base_icon, 20000)
        state.update({"day": today, "count": c["total"], "time": time.time()})  # keeps "reminded"
        save_state(state)

    def _check_reminder(self):
        """"Wait and try again tomorrow": once settings["update_reminder_at"] is past, say it (a click opens
        Updates) and set it back to 0. The settings are read from disk each time: the window writes them, and it
        may write the old time back later; the state file remembers which time was already told, so it is told
        only once."""
        try:
            from .settings import Settings
            at = reminder_due(Settings())
        except Exception as e:
            _log(f"cannot read the settings: {e}")
            return
        if not at:
            return
        if load_state().get("reminded") != at:
            summary, body = reminder_text()
            nid = self.notifier.send(summary, body, [("default", ""), ("updates", _("Open Updates"))],
                                     notify_icon_name(), {"desktop-entry": APP_ID})
            if nid is None:
                if not self.icon.isVisible():
                    return  # nowhere to say it yet: try again at the next tick
                self.icon.showMessage(summary, body, self.base_icon, 30000)
            state = load_state()
            state["reminded"] = at
            save_state(state)
        try:
            s = Settings()
            if reminder_due(s) == at:  # a newer reminder set meanwhile stays
                s["update_reminder_at"] = 0
                s.save()
            self.settings["update_reminder_at"] = s.get("update_reminder_at", 0)
        except Exception as e:
            _log(f"cannot save the settings: {e}")

    def _notification_action(self, key, token):
        if key == "routine":
            self.run_routine(token)
        elif key in ("updates", "default"):
            self.open_updates(token)

    # ------------------------------------------------ the buttons
    def open_app(self, token=None):
        open_main([], token if isinstance(token, str) else None)

    def open_updates(self, token=None):
        open_main(["--page", "updates"], token if isinstance(token, str) else None)

    def run_routine(self, token=None):
        open_main(["--run-routine"], token if isinstance(token, str) else None)

    def open_settings(self):
        open_main(["--page", "settings"])


def run(app, settings):
    """The tray's whole life. Returns the exit code (0 at once if this user already has a tray)."""
    if tray_running() or not _take_lock():
        return 0
    app.setQuitOnLastWindowClosed(False)  # the popup closing must not end the program
    tray = Tray(app, settings)
    tray.listen()
    tray.start()
    app._zypp_desk_tray = tray  # keep it alive
    return app.exec()
