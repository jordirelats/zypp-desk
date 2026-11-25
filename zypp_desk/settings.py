# SPDX-License-Identifier: GPL-3.0-or-later
"""Application settings, stored as JSON in ~/.config/zypp-desk/settings.json."""

import json
import os
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
}


def _path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "zypp-desk" / "settings.json"


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            self.update(json.loads(_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
