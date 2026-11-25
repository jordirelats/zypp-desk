# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the application."""

import argparse
import json
import os
import sys
import tempfile
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


def socket_path(kind="window"):
    """The local socket a running window (or tray icon) listens on: in the user's runtime folder, which nobody
    else can open."""
    name = f"zypp-desk-{kind}-{os.getuid()}.sock"
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), name)
    # a socket path longer than about 100 bytes cannot be used: then the shared temporary folder (the socket
    # itself is still only for this user)
    return path if len(path.encode()) < 100 else os.path.join(tempfile.gettempdir(), name)


def _send_to_running(request):
    """Hand the request to a window that is already open. True if one took it."""
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(socket_path())
    if not sock.waitForConnected(400):
        return False
    sock.write(json.dumps(request).encode("utf-8") + b"\n")
    ok = sock.waitForBytesWritten(1500)
    sock.disconnectFromServer()
    return ok


def _listen(win):
    """Answer later starts (a second click on the launcher, a file opened from Dolphin, the tray icon)."""
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer(win)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    path = socket_path()
    if not server.listen(path):
        QLocalServer.removeServer(path)  # left behind by a window that crashed
        server.listen(path)

    def accept():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            buf = bytearray()

            def read(c=conn, b=buf):
                b.extend(bytes(c.readAll()))
                if b.endswith(b"\n") and len(b) < 65536:
                    try:
                        req = json.loads(b.decode("utf-8"))
                    except ValueError:
                        req = None
                    c.disconnectFromServer()
                    if isinstance(req, dict):
                        win.handle_request(req)
            conn.readyRead.connect(read)
            conn.disconnected.connect(conn.deleteLater)
    server.newConnection.connect(accept)
    return server


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
               "files": [os.path.abspath(f) for f in args.files],
               # from the tray's notification: lets the window that is already open come to the front (Wayland)
               "token": os.environ.get("XDG_ACTIVATION_TOKEN")}
    if not args.screenshot and _send_to_running(request):
        os._exit(0)  # the window that is already open does it

    from .mainwindow import MainWindow  # after QApplication exists
    win = MainWindow()
    server = _listen(win)  # noqa: F841 (kept alive by its parent, the window)
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
