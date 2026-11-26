# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a binary RPM package that holds no files, in pure Python: no rpmbuild.

Zypp Desk uses it for the bridge packages (bridge.py): an empty noarch package whose only job is to say "this
capability is here" (Provides) and to point at what openSUSE calls it (Requires). rpm-build would pull gcc and
glibc-devel into a computer that must stay light, so the package is written here byte by byte, in the version 4
format that rpmbuild writes (rpm 4.20 was the model: the same tags, types and order):

- the lead (96 bytes: magic, version 3.0, binary, noarch, the name, Linux, "header-style signature");
- the signature header, with its region (RPMTAG_HEADERSIGNATURES): the SHA1 and SHA256 of the main header, the
  size and the MD5 of header plus payload, the size of the payload once uncompressed; padded to 8 bytes;
- the main header, with its region (RPMTAG_HEADERIMMUTABLE): name, version, summary, dependencies… and the
  SHA256 of the payload (compressed and not);
- the payload: an empty cpio archive ("newc", only the TRAILER!!! entry), compressed with gzip.

The package is not signed: it is made on this computer and installed with zypper --allow-unsigned-rpm.
"""

import gzip
import hashlib
import os
import re
import struct
import tempfile
import time

# header data types
_CHAR, _INT8, _INT16, _INT32, _INT64, _STRING, _BIN, _STRING_ARRAY, _I18NSTRING = range(1, 10)

# dependency flags (rpmds.h)
SENSE_ANY = 0
SENSE_LESS = 0x02
SENSE_GREATER = 0x04
SENSE_EQUAL = 0x08
SENSE_RPMLIB = 1 << 24

LEAD_MAGIC = b"\xed\xab\xee\xdb"
HEADER_MAGIC = b"\x8e\xad\xe8\x01\x00\x00\x00\x00"

# tags of the signature header
_SIG_REGION = 62          # RPMTAG_HEADERSIGNATURES
_SIG_SHA1 = 269           # RPMSIGTAG_SHA1 (hex SHA1 of the main header)
_SIG_SHA256 = 273         # RPMSIGTAG_SHA256 (hex SHA256 of the main header)
_SIG_SIZE = 1000          # RPMSIGTAG_SIZE (main header + payload)
_SIG_MD5 = 1004           # RPMSIGTAG_MD5 (MD5 of main header + payload)
_SIG_PAYLOADSIZE = 1007   # RPMSIGTAG_PAYLOADSIZE (uncompressed payload)

# tags of the main header
_REGION = 63              # RPMTAG_HEADERIMMUTABLE
_I18NTABLE = 100
_NAME, _VERSION, _RELEASE = 1000, 1001, 1002
_SUMMARY, _DESCRIPTION, _BUILDTIME, _BUILDHOST = 1004, 1005, 1006, 1007
_SIZE, _VENDOR, _LICENSE, _PACKAGER, _GROUP, _URL = 1009, 1011, 1014, 1015, 1016, 1020
_OS, _ARCH = 1021, 1022
_SOURCERPM = 1044
_PROVIDENAME, _REQUIREFLAGS, _REQUIRENAME, _REQUIREVERSION = 1047, 1048, 1049, 1050
_RPMVERSION = 1064
_PROVIDEFLAGS, _PROVIDEVERSION = 1112, 1113
_PAYLOADFORMAT, _PAYLOADCOMPRESSOR, _PAYLOADFLAGS = 1124, 1125, 1126
_PLATFORM = 1132
_ENCODING = 5062
_PAYLOADDIGEST, _PAYLOADDIGESTALGO, _PAYLOADDIGESTALT = 5092, 5093, 5097
_DIGEST_SHA256 = 8        # PGPHASHALGO_SHA256

# what rpmbuild adds to every package it writes (an empty one too)
RPMLIB_REQUIRES = (
    ("rpmlib(CompressedFileNames)", SENSE_RPMLIB | SENSE_LESS | SENSE_EQUAL, "3.0.4-1"),
    ("rpmlib(FileDigests)", SENSE_RPMLIB | SENSE_LESS | SENSE_EQUAL, "4.6.0-1"),
    ("rpmlib(PayloadFilesHavePrefix)", SENSE_RPMLIB | SENSE_LESS | SENSE_EQUAL, "4.0-1"),
)

# a package name, version or release as rpm accepts them (version and release: no '-' and no spaces)
NAME_RE = re.compile(r"[A-Za-z0-9_+][A-Za-z0-9._+-]*")
VERSION_RE = re.compile(r"[A-Za-z0-9._+~^]+")


class RpmWriteError(ValueError):
    pass


def _rpm_version():
    try:
        import rpm
        return rpm.__version__
    except (ImportError, AttributeError):
        return "4.20.0"


def _check_text(what, text, *, multiline=False):
    if not isinstance(text, str) or "\0" in text or (not multiline and ("\n" in text or "\r" in text)):
        raise RpmWriteError(f"{what}: not a valid text")
    return text


def _check_dep(dep):
    """(name, flags, version) of a dependency, checked: rpm reads a space or a NUL as the end of the name."""
    name, flags, ver = dep
    if not name or any(c.isspace() for c in name) or "\0" in name:
        raise RpmWriteError(f"dependency name not valid: {name!r}")
    ver = ver or ""
    if ver and (any(c.isspace() for c in ver) or "\0" in ver):
        raise RpmWriteError(f"dependency version not valid: {ver!r}")
    if ver and not flags & (SENSE_LESS | SENSE_GREATER | SENSE_EQUAL):
        raise RpmWriteError(f"a version without a comparison: {name} {ver}")
    return name, int(flags), ver


def _encode(typ, value):
    """(bytes, count) of a value of this type."""
    if typ == _STRING:
        return value.encode("utf-8") + b"\0", 1
    if typ in (_STRING_ARRAY, _I18NSTRING):
        items = [value] if isinstance(value, str) else list(value)
        return b"".join(s.encode("utf-8") + b"\0" for s in items), len(items)
    if typ == _INT32:
        items = [value] if isinstance(value, int) else list(value)
        return struct.pack(f">{len(items)}I", *[v & 0xFFFFFFFF for v in items]), len(items)
    if typ == _BIN:
        return bytes(value), len(value)
    raise RpmWriteError(f"unsupported type {typ}")


_ALIGN = {_INT16: 2, _INT32: 4, _INT64: 8}


def header_blob(entries, region_tag):
    """A header (magic, index, data) with its region: entries are (tag, type, value). The index is sorted by
    tag and the data follows the same order, as rpm checks; the region's trailer is the last 16 bytes of the
    data and points back at the whole index (a negative offset)."""
    index, data = [], bytearray()
    for tag, typ, value in sorted(entries, key=lambda e: e[0]):
        if tag <= region_tag:
            raise RpmWriteError(f"tag {tag} comes before the region")
        raw, count = _encode(typ, value)
        pad = (-len(data)) % _ALIGN.get(typ, 1)
        data += b"\0" * pad
        index.append((tag, typ, len(data), count))
        data += raw
    il = len(index) + 1  # the region entry too
    trailer_at = len(data)
    data += struct.pack(">IIiI", region_tag, _BIN, -(il * 16), 16)
    index.insert(0, (region_tag, _BIN, trailer_at, 16))
    out = bytearray(HEADER_MAGIC)
    out += struct.pack(">II", il, len(data))
    for tag, typ, off, count in index:
        out += struct.pack(">IIiI", tag, typ, off, count)
    out += data
    return bytes(out)


def empty_cpio():
    """An empty "newc" cpio archive, as rpm writes it: only the TRAILER!!! entry, padded to 4 bytes."""
    name = b"TRAILER!!!\0"
    fields = [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(name), 0]  # ino mode uid gid nlink mtime size ... namesize check
    head = b"070701" + b"".join(b"%08x" % f for f in fields)
    blob = head + name
    return blob + b"\0" * ((-len(blob)) % 4)


def _lead(nvr):
    name = nvr.encode("utf-8")[:65]
    return (LEAD_MAGIC + bytes([3, 0]) + struct.pack(">hh", 0, 255)  # binary package, noarch (as rpmbuild)
            + name + b"\0" * (66 - len(name)) + struct.pack(">hh", 1, 5)  # Linux, header-style signature
            + b"\0" * 16)


def build(*, name, version, release, summary, description, provides=(), requires=(), license="GPL-3.0-or-later",
          group="System/Packages", vendor="", url="", packager="", buildhost="localhost", buildtime=None):
    """The bytes of an empty noarch binary RPM. provides and requires are (name, flags, version) tuples; the
    package always provides itself (name = version-release) and needs rpm's own features as rpmbuild says."""
    for what, v, rx in (("name", name, NAME_RE), ("version", version, VERSION_RE), ("release", release, VERSION_RE)):
        if not isinstance(v, str) or not rx.fullmatch(v):
            raise RpmWriteError(f"{what} not valid for rpm: {v!r}")
    _check_text("summary", summary)
    _check_text("description", description, multiline=True)
    for what, v in (("license", license), ("group", group), ("vendor", vendor), ("url", url),
                    ("packager", packager), ("buildhost", buildhost)):
        _check_text(what, v)

    evr = f"{version}-{release}"
    prov = [_check_dep(d) for d in provides if d[0] != name] + [(name, SENSE_EQUAL, evr)]
    reqs = [_check_dep(d) for d in requires] + list(RPMLIB_REQUIRES)

    cpio = empty_cpio()
    payload = gzip.compress(cpio, compresslevel=9, mtime=0)

    entries = [
        (_I18NTABLE, _STRING_ARRAY, ["C"]),
        (_NAME, _STRING, name), (_VERSION, _STRING, version), (_RELEASE, _STRING, release),
        (_SUMMARY, _I18NSTRING, summary), (_DESCRIPTION, _I18NSTRING, description),
        (_BUILDTIME, _INT32, int(buildtime if buildtime is not None else time.time())),
        (_BUILDHOST, _STRING, buildhost or "localhost"),
        (_SIZE, _INT32, 0),
        (_LICENSE, _STRING, license), (_GROUP, _I18NSTRING, group),
        (_OS, _STRING, "linux"), (_ARCH, _STRING, "noarch"),
        (_SOURCERPM, _STRING, f"{name}-{evr}.src.rpm"),  # without it rpm takes the package for a source one
        (_PROVIDENAME, _STRING_ARRAY, [d[0] for d in prov]),
        (_PROVIDEFLAGS, _INT32, [d[1] for d in prov]),
        (_PROVIDEVERSION, _STRING_ARRAY, [d[2] for d in prov]),
        (_REQUIRENAME, _STRING_ARRAY, [d[0] for d in reqs]),
        (_REQUIREFLAGS, _INT32, [d[1] for d in reqs]),
        (_REQUIREVERSION, _STRING_ARRAY, [d[2] for d in reqs]),
        (_RPMVERSION, _STRING, _rpm_version()),
        (_PAYLOADFORMAT, _STRING, "cpio"), (_PAYLOADCOMPRESSOR, _STRING, "gzip"), (_PAYLOADFLAGS, _STRING, "9"),
        (_PLATFORM, _STRING, "noarch-suse-linux"),
        (_ENCODING, _STRING, "utf-8"),
        (_PAYLOADDIGEST, _STRING_ARRAY, [hashlib.sha256(payload).hexdigest()]),
        (_PAYLOADDIGESTALGO, _INT32, _DIGEST_SHA256),
        (_PAYLOADDIGESTALT, _STRING_ARRAY, [hashlib.sha256(cpio).hexdigest()]),
    ]
    for tag, v in ((_VENDOR, vendor), (_URL, url), (_PACKAGER, packager)):
        if v:
            entries.append((tag, _STRING, v))
    header = header_blob(entries, _REGION)

    md5 = hashlib.md5(header + payload, usedforsecurity=False).digest()
    sig = header_blob([
        (_SIG_SHA1, _STRING, hashlib.sha1(header, usedforsecurity=False).hexdigest()),
        (_SIG_SHA256, _STRING, hashlib.sha256(header).hexdigest()),
        (_SIG_SIZE, _INT32, len(header) + len(payload)),
        (_SIG_MD5, _BIN, md5),
        (_SIG_PAYLOADSIZE, _INT32, len(cpio)),
    ], _SIG_REGION)
    sig += b"\0" * ((-len(sig)) % 8)
    return _lead(f"{name}-{evr}") + sig + header + payload


def file_name(name, version, release):
    return f"{name}-{version}-{release}.noarch.rpm"


def write(folder, **fields):
    """Write the package into folder, under the name rpm gives it (name-version-release.noarch.rpm), through a
    temporary file (nobody ever sees half a package). Returns the path."""
    data = build(**fields)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, file_name(fields["name"], fields["version"], fields["release"]))
    fd, tmp = tempfile.mkstemp(prefix=".bridge-", suffix=".part", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return path
