# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the application."""

import argparse
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import APP_ID, APP_NAME, VERSION


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="zypp-desk", description=f"{APP_NAME}: every zypper feature in a window")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--page", help="open this section (home, packages, updates, repos, …)")
    parser.add_argument("--search", help="search packages at start")
    parser.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)  # used to build the docs
    args, qt_args = parser.parse_known_args(argv[1:])

    app = QApplication([argv[0], *qt_args])
    app.setApplicationName("zypp-desk")
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setDesktopFileName(APP_ID)
    if QIcon.fromTheme("package-x-generic").isNull():
        QIcon.setThemeName("breeze")
    app.setWindowIcon(QIcon.fromTheme("package-x-generic"))

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
        def shot():
            win.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(6000, shot)
    return app.exec()
