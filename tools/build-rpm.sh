#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the RPM from the committed tree (git HEAD) into ./build/rpmbuild.
# Usage: tools/build-rpm.sh   → build/rpmbuild/RPMS/noarch/zypp-desk-<version>-0.noarch.rpm
set -eu
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Warning: there are uncommitted changes; the RPM is built from the last commit (HEAD) without them." >&2
fi
VERSION=$(sed -n 's/^Version: *//p' packaging/zypp-desk.spec)
TOP="$PWD/build/rpmbuild"
mkdir -p "$TOP/SOURCES" "$TOP/SPECS"
git archive --format=tar.gz --prefix="zypp-desk-$VERSION/" -o "$TOP/SOURCES/zypp-desk-$VERSION.tar.gz" HEAD
cp packaging/zypp-desk-rpmlintrc "$TOP/SOURCES/"
cp packaging/zypp-desk.spec "$TOP/SPECS/"
rpmbuild --define "_topdir $TOP" -ba "$TOP/SPECS/zypp-desk.spec"
ls -la "$TOP/RPMS/noarch/"
