#
# spec file for package zypp-desk
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

%define app_id io.github.jordirelats.ZyppDesk

Name:           zypp-desk
Version:        0.2.0
Release:        0
Summary:        Every zypper feature in a window
License:        GPL-3.0-or-later
URL:            https://github.com/jordirelats/zypp-desk
Source0:        %{name}-%{version}.tar.gz
Source1:        %{name}-rpmlintrc
BuildArch:      noarch
BuildRequires:  gettext-tools
Requires:       pkexec
Requires:       polkit
Requires:       python3
Requires:       python3-pyside6
Requires:       python3-rpm
Requires:       zypper
Recommends:     flatpak
Recommends:     gpg2

%description
Zypp Desk is a graphical front end for zypper, the package manager of
openSUSE, that keeps the whole power of zypper: search with every option,
install and remove packages, upgrade the distribution, manage repositories,
locks, languages, Flatpak apps, history and maintenance.

Every change goes to a queue that shows the exact zypper command, can be
simulated with --dry-run and is applied at once. A terminal panel shows
zypper's own output while it works. Update routines are written the way
they are typed in a terminal and run step by step.

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

# launcher
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'EOF'
#!/bin/sh
PYTHONPATH=%{_datadir}/%{name}${PYTHONPATH:+:$PYTHONPATH} exec python3 -m zypp_desk "$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/%{name}

# the root helper and its polkit action
install -D -m 0755 helper/%{name}-helper %{buildroot}%{_libexecdir}/%{name}/%{name}-helper
install -d %{buildroot}%{_datadir}/polkit-1/actions
sed 's|@HELPER@|%{_libexecdir}/%{name}/%{name}-helper|' data/%{app_id}.policy.in \
  > %{buildroot}%{_datadir}/polkit-1/actions/%{app_id}.policy

# menu entry and icon
install -d %{buildroot}%{_datadir}/applications
sed 's|@EXEC@|%{name}|' data/%{app_id}.desktop.in > %{buildroot}%{_datadir}/applications/%{app_id}.desktop
install -D -m 0644 data/icons/%{app_id}.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg

# translations
for mo in locale/*/LC_MESSAGES/%{name}.mo; do
  lang=$(echo "$mo" | cut -d/ -f2)
  install -D -m 0644 "$mo" %{buildroot}%{_datadir}/locale/$lang/LC_MESSAGES/%{name}.mo
done
%find_lang %{name}

%files -f %{name}.lang
%license LICENSE
%doc README.md TRANSLATING.md
%{_bindir}/%{name}
%{_datadir}/%{name}
%dir %{_libexecdir}/%{name}
%{_libexecdir}/%{name}/%{name}-helper
%{_datadir}/polkit-1/actions/%{app_id}.policy
%{_datadir}/applications/%{app_id}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg

%changelog
