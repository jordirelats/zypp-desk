# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the application."""

import argparse
import fcntl
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, Qt, QTimer, QTranslator
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import QApplication

from . import APP_ID, APP_NAME, VERSION, tasks


def app_icon():
    """The installed icon, or the one in the source tree when running from it."""
    ic = QIcon.fromTheme(APP_ID)
    if ic.isNull():
        f = Path(__file__).resolve().parent.parent / "data" / "icons" / f"{APP_ID}.svg"
        ic = QIcon(str(f)) if f.exists() else QIcon.fromTheme("package-x-generic")
    return ic


def _own_private_dir(path, create=False):
    """True if path is a folder (not a link) of this user that nobody else can open (mode 0700). With create,
    make it first if it does not exist."""
    if create:
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError:
            return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def _cache_home():
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


def socket_path(kind="window"):
    """The local socket a running window (or tray icon) listens on; its lock file is this plus ".lock".

    It lives in a folder that only this user can open: $XDG_RUNTIME_DIR, or else a private folder that this
    user made in /tmp (another user could have made one first: then it is not used), or else one in
    ~/.cache. A socket path must stay under about 100 bytes."""
    name = f"zypp-desk-{kind}-{os.getuid()}.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    candidates = []
    if os.path.isabs(runtime):
        candidates.append((runtime, False))
    candidates.append((os.path.join(tempfile.gettempdir(), f"zypp-desk-{os.getuid()}"), True))
    cache = os.path.join(_cache_home(), "zypp-desk", "run")
    candidates.append((cache, True))
    for folder, create in candidates:
        path = os.path.join(folder, name)
        if len(path.encode()) < 100 and _own_private_dir(folder, create):
            return path
    # none fits: a path in this user's own cache, even if too long for a socket (listening fails, so there is
    # no single instance, but nothing listens where another user could)
    _own_private_dir(cache, create=True)
    return os.path.join(cache, name)


_window_lock = None


def _take_window_lock():
    """True if this is the first window (it holds the lock until it ends), False if another window holds it,
    None if there is no lock file (then there is no single instance). Two windows started at the same moment
    cannot both win: only one gets the lock."""
    global _window_lock
    try:
        fd = os.open(socket_path() + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _window_lock = fd  # kept open (and locked) for the life of the window
    return True


MAX_REQUEST = 4 * 1024 * 1024  # a request is one line of JSON (paths of the files to open, and a few words)


def _send_to_running(request, wait=0.0):
    """Hand the request to a window that is already open. True if one took it (it answers "ok").

    wait: seconds to keep trying while that window is still starting (it holds the lock but does not
    listen yet)."""
    from PySide6.QtNetwork import QLocalSocket
    deadline = time.monotonic() + wait
    data = json.dumps(request).encode("utf-8") + b"\n"
    while True:
        sock = QLocalSocket()
        sock.connectToServer(socket_path())
        if sock.waitForConnected(400):
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    sock.write(data)
    if not sock.waitForBytesWritten(3000):
        sock.abort()
        return False
    answer = b""
    while b"\n" not in answer and sock.waitForReadyRead(10000):
        answer += bytes(sock.readAll())
    answer += bytes(sock.readAll())
    if not answer:
        # a window of an earlier version reads the request and closes without answering
        return sock.state() == QLocalSocket.LocalSocketState.UnconnectedState
    sock.disconnectFromServer()
    return answer.startswith(b"ok")


def _listen(win):
    """Answer later starts (a second click on the launcher, a file opened from Dolphin, the tray icon). Only
    the window that holds the lock listens, so a socket left behind can be removed: nobody else uses it."""
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer(win)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    path = socket_path()
    QLocalServer.removeServer(path)  # left behind by a window that crashed
    if not server.listen(path):
        print(f"zypp-desk: cannot listen on {path}: {server.errorString()}", file=sys.stderr)

    def accept():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            buf = bytearray()

            def read(c=conn, b=buf):
                b.extend(bytes(c.readAll()))
                if len(b) > MAX_REQUEST:
                    c.write(b"too-big\n")
                    c.disconnectFromServer()
                    b.clear()
                    return
                if not b.endswith(b"\n"):
                    return
                try:
                    req = json.loads(b.decode("utf-8"))
                except ValueError:
                    req = None
                b.clear()
                c.write(b"ok\n" if isinstance(req, dict) else b"bad\n")
                c.flush()
                c.disconnectFromServer()
                if isinstance(req, dict):
                    win.handle_request(req)
            conn.readyRead.connect(read)
            conn.disconnected.connect(conn.deleteLater)
    server.newConnection.connect(accept)
    return server


def opened_dir():
    """Copies of files that would vanish once the program that gave them to us ends."""
    d = os.path.join(_cache_home(), "zypp-desk", "opened")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _stable_path(path):
    """The path to hand over for a file given on the command line. A file outside the home folder (a temporary
    file, a USB stick) or a copy that KIO made of a remote file (~/.cache/kioexec) may be gone once the program
    that opened us ends, or before the queue installs it: it is copied to ~/.cache/zypp-desk/opened/ first.
    Other files are handed over as they are."""
    if path.startswith("file://"):
        from PySide6.QtCore import QUrl
        path = QUrl(path).toLocalFile() or path
    p = os.path.abspath(os.path.expanduser(path))
    real = os.path.realpath(p)
    home = os.path.realpath(os.path.expanduser("~"))
    temporary = [os.path.realpath(os.path.join(c, "kioexec")) for c in (_cache_home(), os.path.join(home, ".cache"))]
    if real.startswith(home + os.sep) and not any(real.startswith(t + os.sep) for t in temporary):
        return p
    if not os.path.isfile(real):
        return p  # the window says what is wrong with it
    try:
        dest = os.path.join(tempfile.mkdtemp(dir=opened_dir()), os.path.basename(p))
        shutil.copy2(real, dest)
    except OSError as e:
        print(f"zypp-desk: cannot copy {p}: {e}", file=sys.stderr)
        return p
    return dest


def _clean_opened(max_age=3 * 24 * 3600):
    """Copies from earlier runs (the queue of a window that ended is gone with it)."""
    try:
        d = opened_dir()
        now = time.time()
        for entry in os.scandir(d):
            if entry.is_dir(follow_symlinks=False) and now - entry.stat(follow_symlinks=False).st_mtime > max_age:
                shutil.rmtree(entry.path, ignore_errors=True)
    except OSError:
        pass


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="zypp-desk", description=f"{APP_NAME}: every zypper feature in a window")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--page", help="open this section (home, packages, updates, repos, …)")
    parser.add_argument("--search", help="search packages at start")
    parser.add_argument("--run-routine", action="store_true", help="open Updates and run the update routine")
    parser.add_argument("--tray", action="store_true", help="only the icon in the system tray, which tells you "
                                                            "about updates")
    parser.add_argument("files", nargs="*", help="downloaded .rpm files to look at and install")
    parser.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)  # used to build the docs
    parser.add_argument("--screenshot-delay", type=float, default=6, help=argparse.SUPPRESS)
    args, qt_args = parser.parse_known_args(argv[1:])

    app = QApplication([argv[0], *qt_args])
    app.setApplicationName("zypp-desk")
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setDesktopFileName(APP_ID)
    # Qt's own words (Yes, No, Cancel in dialogs) in the user's language, also outside Plasma
    qt_tr = QTranslator(app)
    if qt_tr.load(QLocale(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt_tr)
    if QIcon.fromTheme("package-x-generic").isNull():
        QIcon.setThemeName("breeze")
    app.setWindowIcon(app_icon())
    from . import style
    style.apply(app)

    if args.tray:
        from . import tray
        from .settings import Settings
        rc = tray.run(app, Settings())
        tasks.shutting_down()
        os._exit(rc)

    request = {"page": args.page, "search": args.search, "run_routine": args.run_routine,
               "files": [_stable_path(f) for f in args.files],
               # from the tray's notification: lets the window that is already open come to the front (Wayland)
               "token": os.environ.get("XDG_ACTIVATION_TOKEN")}
    listen = False
    if not args.screenshot:  # one window per user: a later start hands its request to the first one
        first = _take_window_lock()
        if first is False:
            # another window holds the lock: it may still be starting, so keep trying for a while
            if _send_to_running(request, wait=20):
                os._exit(0)
            print("zypp-desk: the window that is already open does not answer: opening another one",
                  file=sys.stderr)
        elif first:
            if _send_to_running(request):  # a window of an earlier version, which has no lock
                os._exit(0)
            listen = True
            _clean_opened()

    from .mainwindow import MainWindow  # after QApplication exists
    win = MainWindow()
    server = _listen(win) if listen else None  # noqa: F841 (kept alive by its parent, the window)
    win.show()
    win.handle_request(request)
    if not args.screenshot:  # the tray icon that tells about updates (see pages/notifysettings.py)
        from .pages.notifysettings import ensure_tray
        QTimer.singleShot(3000, lambda: ensure_tray(win.settings))
    if args.screenshot:
        QCursor.setPos(-1000, -1000)  # no hover highlight in the picture
        win.sidebar.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, False)

        def shot():
            win.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(int(args.screenshot_delay * 1000), shot)
    rc = app.exec()
    # Leave at once: background threads may still be waiting for the network, and a normal teardown of Qt and
    # Python with them alive can crash. Settings are saved as they change, so nothing is lost.
    tasks.shutting_down()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
