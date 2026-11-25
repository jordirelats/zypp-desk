# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the application."""

import argparse
import os
import sys
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


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="zypp-desk", description=f"{APP_NAME}: every zypper feature in a window")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--page", help="open this section (home, packages, updates, repos, …)")
    parser.add_argument("--search", help="search packages at start")
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

    from .mainwindow import MainWindow  # after QApplication exists
    win = MainWindow()
    if args.page and args.page in win.pages:
        win.go(args.page)
    if args.search:
        win.go("packages")
        win.pages["packages"].term.setText(args.search)
        win.pages["packages"].search()
    win.show()
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
