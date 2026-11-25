# Zypp Desk

**Every zypper feature, in a window.** Zypp Desk is a graphical front end for
[zypper](https://github.com/openSUSE/zypper), the package manager of openSUSE, for people who
want the full power of zypper without giving up a desktop application: search, install and
remove packages, upgrade the distribution, manage repositories, locks, languages, history and
maintenance — and always see the exact zypper command and its real output.

> **Status: 0.2, early development.** Searching, package details, the change queue with
> simulation, routines, Flatpak apps, programs downloaded from GitHub, repositories, locks,
> services, languages, history and maintenance work, and warnings are explained. Still missing:
> choosing among zypper's proposed solutions to a conflict inside the window, and editing
> `zypp.conf`. Expect rough edges.

## Ideas behind it

- **Nothing is hidden.** Every change shows the exact `zypper` command it will run, and a
  terminal panel shows zypper's own output as it happens, coloured like Konsole, with repeated
  lines folded and a filter for warnings and errors.
- **A queue of changes.** Packages, locks, repositories and languages go to a queue. The queue
  can be simulated (`--dry-run`) and then applied at once, with one password.
- **Routines.** Write your update routine the way you type it, for example
  `zypper -v ref ; zypper -v dup -d -l && zypper -v dup ; flatpak update -y`, and run it with one
  click, step by step.
- **Flatpak too.** Installed apps, search, updates and remotes, through the same queue.
- **Programs from the web.** Follow programs you install from RPM files published on GitHub:
  Zypp Desk finds new releases, downloads them, checks the SHA-256 and the signature, and
  queues them.
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
