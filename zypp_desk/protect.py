# SPDX-License-Identifier: GPL-3.0-or-later
"""Protected applications: an app that is no longer maintained nor distributed (no longer on Flathub, say) can
be protected so that it is not removed by mistake.

A protected app is never removed through Zypp Desk: the queue refuses its removal (TxQueue.add) until it is
unprotected. For a Flatpak app, protecting it can also mask it in flatpak (`flatpak mask`: no updates and no
automatic replacement), and its file can be saved with `flatpak build-bundle`, to install it again when the remote
no longer has it. An AppImage is protected by its path: Zypp Desk does not move it to the trash. Kept in
settings["protected_apps"] as "flatpak:<app id>", "appimage:<path>" or "rpm:<package>".
"""

import os
import subprocess

FLATPAK_REPO = "/var/lib/flatpak/repo"
FLATHUB_RUNTIMES = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def key(kind, ident):
    return f"{kind if kind in ('flatpak', 'appimage') else 'rpm'}:{ident}"


def protected(settings):
    return set(settings.get("protected_apps") or [])


def is_protected(settings, kind, ident):
    return key(kind, ident) in protected(settings)


def set_protected(settings, kind, ident, on):
    keys = protected(settings)
    k = key(kind, ident)
    if on:
        keys.add(k)
    else:
        keys.discard(k)
    settings["protected_apps"] = sorted(keys)
    settings.save()


def action_target(action):
    """(kind, ident) of the app a queued removal is about, or None if it is not a removal."""
    if action.kind == "flatpak-uninstall":
        return "flatpak", action.target
    if action.kind == "remove":
        t = action.opts.get("package") or action.target
        return "rpm", t.split(":", 1)[-1].split("=", 1)[0]
    return None


def masked_flatpaks():
    """The patterns masked in the system installation (`flatpak mask`), or an empty set."""
    try:
        p = subprocess.run(["flatpak", "mask", "--system"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in p.stdout.splitlines()[1:] if line.strip()}


def bundle_step(app_id, path, installation="system"):
    """The command that saves an installed Flatpak app as one file that `flatpak install ./file` installs again
    (its runtime still comes from Flathub). Runs as the user: the system repository can be read."""
    from .runner import Step
    repo = FLATPAK_REPO if installation != "user" else os.path.expanduser("~/.local/share/flatpak/repo")
    return Step("flatpak", ["build-bundle", f"--runtime-repo={FLATHUB_RUNTIMES}", repo, path, app_id, "stable"],
                root=False)
