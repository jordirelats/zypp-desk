#
# spec file for package zypp-desk
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

%define app_id io.github.jordirelats.ZyppDesk
# runner.HELPER_PATHS looks here first; %%{_libexecdir} is /usr/lib on older distributions
%define helperdir %{_prefix}/libexec/%{name}

Name:           zypp-desk
Version:        0.5.8
Release:        0
Summary:        Every zypper feature in a window
License:        GPL-3.0-or-later
URL:            https://github.com/jordirelats/zypp-desk
# git archive of the released commit (tools/build-rpm.sh; on OBS the obs_scm service makes it): the releases
# are not tagged on GitHub, so there is no download address
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  AppStream
BuildRequires:  desktop-file-utils
BuildRequires:  gettext-tools
BuildRequires:  hicolor-icon-theme
BuildRequires:  pkgconfig(systemd)
BuildRequires:  python3 >= 3.11
Requires:       gpg2
Requires:       pkexec
Requires:       polkit
Requires:       python3 >= 3.11
Requires:       python3-pyside6
Requires:       python3-rpm
# the health check reads zypper's package index directly (else one slower zypper search)
Recommends:     python3-solv
Requires:       zypper
Recommends:     flatpak
Recommends:     fwupd
Recommends:     xdg-utils
# reads the name, version and icon inside an AppImage without running it (-cat, -max-depth)
Recommends:     squashfs >= 4.6
Suggests:       snapper
Recommends:     kf6-breeze-icons
%{?systemd_ordering}

%description
Zypp Desk is a graphical front end for zypper, the package manager of
openSUSE, that keeps the whole power of zypper: search with every option,
install and remove packages, upgrade the distribution, manage repositories,
locks, languages, Flatpak apps, history and maintenance.

Every change goes to a queue that shows the exact zypper command, can be
simulated with --dry-run and is applied at once. A terminal panel shows
zypper's own output while it works. Update routines are written the way
they are typed in a terminal, or built step by step, and run step by step.
Programs installed from files downloaded from the web can be followed for
new versions. A tray icon tells about updates, firmware included (fwupd),
and a system timer refreshes the repositories; it never installs anything.
The history shows the snapper snapshots of each change and can undo it.

%prep
%autosetup -p1

%build
for po in po/*.po; do
  lang=$(basename "$po" .po)
  mkdir -p "locale/$lang/LC_MESSAGES"
  msgfmt --check -o "locale/$lang/LC_MESSAGES/%{name}.mo" "$po"
done

%install
# the Python package, private to the application
install -d %{buildroot}%{_datadir}/%{name}
cp -a zypp_desk %{buildroot}%{_datadir}/%{name}/
find %{buildroot}%{_datadir}/%{name} -name '__pycache__' -prune -exec rm -rf {} +

# launcher: the system Python, without the user's site-packages (a venv earlier in PATH must not win)
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'EOF'
#!/bin/sh
PYTHONPATH=%{_datadir}/%{name}${PYTHONPATH:+:$PYTHONPATH} exec /usr/bin/python3 -P -s -m zypp_desk "$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/%{name}

# the root helper and its polkit action
install -D -m 0755 helper/%{name}-helper %{buildroot}%{helperdir}/%{name}-helper
install -d %{buildroot}%{_datadir}/polkit-1/actions
sed 's|@HELPER@|%{helperdir}/%{name}-helper|' data/%{app_id}.policy.in \
  > %{buildroot}%{_datadir}/polkit-1/actions/%{app_id}.policy

# the system timer that refreshes the repositories as root (never installs anything), on by default (preset)
install -D -m 0755 helper/%{name}-refresh %{buildroot}%{helperdir}/%{name}-refresh
install -d %{buildroot}%{_unitdir} %{buildroot}%{_presetdir}
sed 's|@REFRESH@|%{helperdir}/%{name}-refresh|' data/%{name}-refresh.service \
  > %{buildroot}%{_unitdir}/%{name}-refresh.service
install -m 0644 data/%{name}-refresh.timer %{buildroot}%{_unitdir}/%{name}-refresh.timer
# "Look for new updates" in the tray: the same refresh at once, without downloading ahead; a polkit rule lets a user
# at the computer start exactly this unit without a password (it never installs anything)
sed 's|@REFRESH@|%{helperdir}/%{name}-refresh|' data/%{name}-check.service \
  > %{buildroot}%{_unitdir}/%{name}-check.service
install -D -m 0644 data/%{app_id}.rules %{buildroot}%{_datadir}/polkit-1/rules.d/50-%{app_id}.rules
echo "enable %{name}-refresh.timer" > %{buildroot}%{_presetdir}/90-%{name}.preset
# the flag the helper writes to download updates ahead: owned (not installed) so that removing the package removes it
install -d %{buildroot}%{_sysconfdir}/%{name}
touch %{buildroot}%{_sysconfdir}/%{name}/download-ahead

# menu entry, icon and AppStream metadata
install -d %{buildroot}%{_datadir}/applications
sed 's|@EXEC@|%{name}|' data/%{app_id}.desktop.in > %{buildroot}%{_datadir}/applications/%{app_id}.desktop
# the tray icon that tells about updates starts at login for everyone (turned off per user in Settings, which
# writes a Hidden=true entry in ~/.config/autostart, the standard way to switch off a system autostart entry)
install -d %{buildroot}%{_sysconfdir}/xdg/autostart
sed 's|@EXEC@|%{name}|' data/%{app_id}.tray.desktop.in > %{buildroot}%{_sysconfdir}/xdg/autostart/%{app_id}.tray.desktop
install -D -m 0644 data/icons/%{app_id}.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
install -D -m 0644 data/%{app_id}.metainfo.xml %{buildroot}%{_datadir}/metainfo/%{app_id}.metainfo.xml
install -D -m 0644 data/%{app_id}.mime.xml %{buildroot}%{_datadir}/mime/packages/%{app_id}.xml

# translations
for mo in locale/*/LC_MESSAGES/%{name}.mo; do
  lang=$(echo "$mo" | cut -d/ -f2)
  install -D -m 0644 "$mo" %{buildroot}%{_datadir}/locale/$lang/LC_MESSAGES/%{name}.mo
done
%find_lang %{name}

%pre
%service_add_pre %{name}-refresh.service %{name}-refresh.timer %{name}-check.service

%post
%service_add_post %{name}-refresh.service %{name}-refresh.timer %{name}-check.service
# A new installation: the preset has turned the timer on, but it would only start at the next boot. Start it now
# (it waits for the zypp lock of this installation; systemctl does nothing in a chroot or an image build).
if [ "$1" -eq 1 ] && [ -d /run/systemd/system ] && systemctl -q is-enabled %{name}-refresh.timer 2>/dev/null; then
  systemctl start %{name}-refresh.timer >/dev/null 2>&1 || :
fi

%preun
%service_del_preun %{name}-refresh.service %{name}-refresh.timer %{name}-check.service

%postun
# no restart on upgrade: a restarted oneshot would run zypper while the upgrade still holds the zypp lock
%service_del_postun_without_restart %{name}-refresh.service %{name}-refresh.timer %{name}-check.service

%check
# a version forgotten in one place: the window would warn that the helper is from another version
grep -qx 'VERSION = "%{version}"' zypp_desk/__init__.py
grep -qx 'VERSION = "%{version}"' helper/%{name}-helper
grep -q '<release version="%{version}"' data/%{app_id}.metainfo.xml
# every translation of the help has the same topics as the English one
for f in zypp_desk/help/*.html; do
  test "$(grep -o 'topic: [a-z-]*' "$f")" = "$(grep -o 'topic: [a-z-]*' zypp_desk/help/en.html)"
done
desktop-file-validate %{buildroot}%{_datadir}/applications/%{app_id}.desktop
desktop-file-validate %{buildroot}%{_sysconfdir}/xdg/autostart/%{app_id}.tray.desktop
appstreamcli validate --no-net %{buildroot}%{_datadir}/metainfo/%{app_id}.metainfo.xml
python3 -m py_compile helper/%{name}-helper $(find zypp_desk -name '*.py')
sh -n helper/%{name}-refresh
find . -name '__pycache__' -prune -exec rm -rf {} +

%files -f %{name}.lang
%license LICENSE
%doc README.md TRANSLATING.md
%{_bindir}/%{name}
%{_datadir}/%{name}
%dir %{helperdir}
%{helperdir}/%{name}-helper
%{helperdir}/%{name}-refresh
%{_unitdir}/%{name}-refresh.service
%{_unitdir}/%{name}-refresh.timer
%{_unitdir}/%{name}-check.service
%{_presetdir}/90-%{name}.preset
%dir %{_sysconfdir}/%{name}
%ghost %{_sysconfdir}/%{name}/download-ahead
%{_datadir}/polkit-1/actions/%{app_id}.policy
%{_datadir}/polkit-1/rules.d/50-%{app_id}.rules
%{_datadir}/applications/%{app_id}.desktop
%config(noreplace) %{_sysconfdir}/xdg/autostart/%{app_id}.tray.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
%{_datadir}/metainfo/%{app_id}.metainfo.xml
%{_datadir}/mime/packages/%{app_id}.xml

%changelog
