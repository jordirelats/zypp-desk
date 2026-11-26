# SPDX-License-Identifier: GPL-3.0-or-later
"""Application settings, stored as JSON in ~/.config/zypp-desk/settings.json."""

import copy
import json
import os
import tempfile
from pathlib import Path

from .routine import DEFAULT_ROUTINE

DEFAULTS = {
    "routine": DEFAULT_ROUTINE,
    "show_terminal": True,
    "fold_repeated_lines": True,
    "apply_no_recommends": False,
    "apply_accept_licenses": False,
    "dup_no_recommends": False,
    "dup_allow_downgrade": True,
    "after_routine_check_restart": True,
    "web_folder": "",
    # the tray icon (tray.py) and its notifications
    "notify_enabled": True,
    "notify_interval_hours": 6,
    "notify_firmware": True,
    "tray_autostart": True,
    "tray_autostart_done": False,
    # "Remind me tomorrow" before a big upgrade or one with many problems: a Unix time, 0 = none. The tray
    # icon (tray.py) notifies once it is past, then sets it back to 0.
    "update_reminder_at": 0,
    # why: "big" (a big update, Remind me tomorrow) or "problems" (an update with problems): the reminder's words
    "update_reminder_reason": "",
    # the locks a "leave these as they are" made in the problem window ({spec, type, repo, added, tw}): Locks says so
    "temp_locks": [],
    # 2 once the tray's autostart entry was moved to the package's /etc/xdg/autostart one (tray.migrate_autostart)
    "autostart_scheme": 0,
    # apps never removed through Zypp Desk until unprotected: "flatpak:<app id>" or "rpm:<package>" (protect.py)
    "protected_apps": [],
    # repositories for Fedora (/etc/yum.repos.d) whose banner was answered "Not now": "<file>#<section>"
    "yum_dismissed": [],
}


def _path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "zypp-desk" / "settings.json"


class Settings(dict):
    def __init__(self):
        super().__init__(copy.deepcopy(DEFAULTS))  # lists in DEFAULTS are never shared
        try:
            self.update(json.loads(_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # a temp name of its own: the window and the tray may save at the same moment (code review 0.5, #7)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self, indent=2, ensure_ascii=False))
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
