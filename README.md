# Zypp Desk

**Every zypper feature, in a window.** Zypp Desk is a graphical front end for
[zypper](https://github.com/openSUSE/zypper), the package manager of openSUSE, for people who
want the full power of zypper without giving up a desktop application: search, install and
remove packages, upgrade the distribution, manage repositories, locks, languages, history and
maintenance — and always see the exact zypper command and its real output.

> **Status: 0.4, early development.** Searching, package details, the change queue with
> simulation and solutions to dependency problems, routines (written or built step by step), your
> applications by origin, discovering programs by theme, Flatpak apps, firmware updates, programs
> downloaded from the web, repositories with a health check and priorities, signing keys,
> `zypp.conf`, a tray icon that tells you about updates, searching the openSUSE Build Service,
> Fedora COPR and Flathub, locks, services, languages, the history with snapper snapshots and
> maintenance work, and warnings are explained. Expect rough edges.

## Ideas behind it

- **Nothing is hidden.** Every change shows the exact `zypper` command it will run, and a
  terminal panel shows zypper's own output as it happens, coloured like Konsole, with repeated
  lines folded and a filter for warnings and errors.
- **A queue of changes.** Packages, locks, repositories and languages go to a queue. The queue
  can be simulated (`--dry-run`) and then applied at once, with one password.
- **Routines.** Write your update routine the way you type it, for example
  `zypper -v ref ; zypper -v dup -d -l && zypper -v dup ; flatpak update -y`, and run it with one
  click, step by step.
- **Your applications, wherever they come from.** One list of the programs you have, from the
  repositories, from Flatpak or from files downloaded from the web, with what can be updated;
  and the openSUSE and Flathub catalogues to discover programs by theme.
- **Flatpak too.** Installed apps, search, updates and remotes, through the same queue.
- **Programs from the web.** Follow programs you install from RPM files: from GitHub releases, a
  fixed download address or a download page (a .zip with the RPM inside works too). Zypp Desk
  finds new versions, downloads them over https only, checks the SHA-256 when it is published,
  the package name and the signature, and queues them.
- **Repositories that work.** Enabled ones first, a check that tells which ones no longer answer,
  a list or a cards view, the Flatpak remotes next to them, and a search in the openSUSE Build
  Service, Fedora COPR and Flathub for what your repositories do not have — with a plain word on
  how far each source can be trusted.
- **Told when there is something new.** Without Discover nobody tells you about updates: a tray
  icon counts them (repositories, Flatpak, the web, firmware) and a notification says so once a
  day. A system timer refreshes the repositories as root; it never installs anything.
- **Undo, with snapper.** The history shows the snapshots before and after each change, compares
  their files and undoes a change.
- **Problems in plain words.** When a simulation finds a dependency problem, zypper's solutions
  are explained and you choose one; it goes to the queue and the queue is simulated again.
- **Keys and settings.** Which signing key signs which repository and which of your packages,
  delete the ones nobody uses, import new ones after checking the fingerprint; and the settings
  of `zypp.conf`, written to a drop-in file of its own.
- **Warnings explained.** After every run, each message is shown with how serious it is, what it
  means in plain words and what to do.
- **Native.** Qt 6 through PySide6, so it follows the Plasma theme (Breeze) and the system icons.

## How it gets root

The window runs as your user. Commands that change the system go through `pkexec` and a small
helper, [`helper/zypp-desk-helper`](helper/zypp-desk-helper), which only runs `zypper` with a
fixed list of subcommands and refuses options that point zypper at another root, configuration
or cache directory. The polkit action uses `auth_admin_keep`, so the password is asked once and
remembered for a few minutes.

## Requirements

- openSUSE (Tumbleweed is the main target) with `zypper`
- Python 3.11 or newer, `python3-pyside6` (on Tumbleweed: `python313-pyside6`), `python3-rpm`
- polkit with `pkexec`, and a polkit agent (Plasma and GNOME have one)
- `gettext-tools` to build the translations

## Running from the source tree

```bash
./install.sh      # installs the helper and the polkit action (asks for the root password once)
./zypp-desk
```

`./uninstall.sh` removes what `install.sh` installed.

## Building the RPM

```bash
tools/build-rpm.sh   # builds from the committed tree into build/rpmbuild/RPMS/noarch/
```

The spec file is `packaging/zypp-desk.spec`.

## Translations

Source strings are in English. Translations live in `po/`: Catalan, German and French are
complete. To add a language, see [TRANSLATING.md](TRANSLATING.md).

## License

Zypp Desk is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version. See [LICENSE](LICENSE).

Copyright © 2026 The Zypp Desk authors.

"openSUSE" and "zypper" are names of the openSUSE project. Zypp Desk is an independent project
and is not affiliated with or endorsed by openSUSE or SUSE.
