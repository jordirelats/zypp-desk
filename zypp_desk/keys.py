# SPDX-License-Identifier: GPL-3.0-or-later
"""Signing keys: the keys rpm trusts, who uses each one, and reading a key before it is imported.

Everything here only reads. gpg always runs with a temporary --homedir that is deleted at once, so
~/.gnupg is never created or touched. Importing and deleting keys go through the queue and the helper
(key-import, key-remove), which runs as root and checks the fingerprint again.

Where the facts come from:
- the keys: 'rpm -qa gpg-pubkey' (the armored key is the package's description), read with gpg;
- which repository uses a key: the key that signed its metadata, from the signature zypper keeps in
  /var/cache/zypp/raw/<alias>/repodata/repomd.xml.asc (readable by everybody); it can be a subkey;
- how many installed packages a key signed: the signature in each package header ('Key ID …');
- the keys openSUSE ships: /usr/lib/rpm/gnupg/keys/*.asc (package openSUSE-build-key).
"""

import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import backend, websources
from .backend import ENV_C
from .i18n import _

MAX_KEY = 1024 * 1024
TIMEOUT = 20
RAW_CACHE = Path("/var/cache/zypp/raw")
SHIPPED_DIR = Path("/usr/lib/rpm/gnupg/keys")
KEYSERVER = "https://keys.openpgp.org/vks/v1/by-fingerprint/{fpr}"
FPR_RE = re.compile(r"[0-9A-F]{40}|[0-9A-F]{64}")
_SEP = "@@ZYPP-DESK-KEY@@"
_MAX_FILES = 1000  # .rpm files looked at in the folders of programs from the web


# ------------------------------------------------------------------ gpg, without a keyring of its own

def _gpg(args, data=b""):
    """Run gpg on data with a temporary home folder (nothing in ~/.gnupg). Returns (code, stdout bytes)."""
    with tempfile.TemporaryDirectory(prefix="zypp-desk-gpg-") as home:
        p = subprocess.run(["gpg", "--homedir", home, "--batch", "--no-autostart", "--no-tty", *args],
                           input=data, capture_output=True, env=ENV_C, timeout=60)
    return p.returncode, p.stdout


def _unescape(text):
    """gpg writes ':' and other special bytes of a user ID as \\xHH in the colon format."""
    raw = re.sub(rb"\\x([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]), text.encode("utf-8", "replace"))
    out = raw.decode("utf-8", "replace")
    return re.sub(r"[\x00-\x1f\x7f]+", " ", out).strip()  # no line breaks or control characters in a name


def _epoch(value):
    if not value:
        return None
    if value.isdigit():
        return int(value) or None
    try:  # some gpg versions write ISO 8601 dates (20260115T123456)
        return int(time.mktime(time.strptime(value[:15], "%Y%m%dT%H%M%S")))
    except ValueError:
        return None


def split_uid(uid):
    """'Name (comment) <mail@host>' → ('Name (comment)', 'mail@host')."""
    m = re.match(r"^(.*?)\s*<([^<>]*)>\s*$", uid)
    if m:
        return m.group(1).strip() or m.group(2), m.group(2)
    return uid, ""


def parse_colons(out):
    """The keys in 'gpg --with-colons --show-keys' output, in order.

    Each one: fpr, ids (16-hex long ids of the primary key and its subkeys), fprs (their fingerprints),
    name, email, uids, created, expires, expired, revoked, secret.
    """
    keys, cur, last = [], None, None
    now = time.time()
    for line in out.decode("utf-8", "replace").splitlines():
        f = line.split(":")
        rec = f[0]
        if rec in ("pub", "sec"):
            cur = {"fpr": "", "ids": set(), "fprs": set(), "uids": [], "name": "", "email": "",
                   "created": _epoch(f[5]) if len(f) > 5 else None, "expires": _epoch(f[6]) if len(f) > 6 else None,
                   "validity": f[1] if len(f) > 1 else "", "secret": rec == "sec"}
            if len(f) > 4 and f[4]:
                cur["ids"].add(f[4].upper())
            keys.append(cur)
            last = rec
        elif cur is None:
            continue
        elif rec in ("sub", "ssb"):
            if len(f) > 4 and f[4]:
                cur["ids"].add(f[4].upper())
            last = rec
        elif rec == "fpr" and len(f) > 9:
            fpr = f[9].upper()
            cur["fprs"].add(fpr)
            if last in ("pub", "sec") and not cur["fpr"]:
                cur["fpr"] = fpr
            last = rec
        elif rec == "uid" and len(f) > 9:
            cur["uids"].append(_unescape(f[9]))
    for k in keys:
        if k["uids"]:
            k["name"], k["email"] = split_uid(k["uids"][0])
        k["revoked"] = k["validity"] == "r"
        k["expired"] = k["validity"] == "e" or bool(k["expires"] and k["expires"] < now)
        del k["validity"]
    return [k for k in keys if k["fpr"]]


def inspect_key(data: bytes):
    """Read a key file (armored or binary) without importing it anywhere.

    Returns a list of dicts {fpr, name, email, created, expires, expired, revoked, ids, fprs, uids}, one per
    primary key in the data (an empty list when there is none). A secret key is refused: it must never be
    shared, and rpm only needs the public one.
    """
    if len(data) > MAX_KEY:
        raise ValueError(_("The file is bigger than 1 MiB: it is not a signing key."))
    _code, out = _gpg(["--with-colons", "--fixed-list-mode", "--show-keys"], data)
    keys = parse_colons(out)
    if any(k["secret"] for k in keys):
        raise ValueError(_("This is a secret key. Never share it: rpm only needs the public key."))
    return keys


def armored(data: bytes, fpr):
    """The key as text (rpm --import wants the armored form). Binary keys are converted with a throwaway keyring."""
    if b"-----BEGIN PGP PUBLIC KEY BLOCK-----" in data:
        return data
    with tempfile.TemporaryDirectory(prefix="zypp-desk-gpg-") as home:
        base = ["gpg", "--homedir", home, "--batch", "--no-autostart", "--no-tty"]
        p = subprocess.run([*base, "--import"], input=data, capture_output=True, env=ENV_C, timeout=60)
        if p.returncode == 0:
            p = subprocess.run([*base, "--armor", "--export", "--", fpr], capture_output=True, env=ENV_C, timeout=60)
    if p.returncode != 0 or b"-----BEGIN PGP PUBLIC KEY BLOCK-----" not in p.stdout:
        raise ValueError(_("The key could not be read."))
    return p.stdout


def nice_fpr(fpr, lines=1):
    """'84C365D6…' → '84C3 65D6 …' (groups of 4), on one line or split in two halves."""
    groups = [fpr[i:i + 4] for i in range(0, len(fpr), 4)]
    if lines == 2 and len(groups) > 5:
        half = (len(groups) + 1) // 2
        return " ".join(groups[:half]) + "\n" + " ".join(groups[half:])
    return " ".join(groups)


def normalize_fpr(text):
    """A fingerprint as typed or pasted (spaces, lower case, '0x') → upper-case hex, or '' if it is not one."""
    t = re.sub(r"\s+", "", text or "").upper()
    if t.startswith("0X"):
        t = t[2:]
    return t if FPR_RE.fullmatch(t) else ""


def matches_id(key, kid):
    """Whether a key ID found in a signature (8 or 16 hex digits, or a fingerprint) belongs to this key."""
    kid = kid.upper()
    if len(kid) >= 40:
        return kid in key["fprs"] or kid[-16:] in key["ids"]
    if len(kid) == 16:
        return kid in key["ids"]
    return len(kid) >= 8 and any(i.endswith(kid) for i in key["ids"])


# ------------------------------------------------------------------ the keys rpm has

def _rpm_keys():
    """[(nvr, version, release, installtime, packager, armored text)] from rpm's keyring."""
    fmt = _SEP + r"%{NAME}-%{VERSION}-%{RELEASE}\t%{VERSION}\t%{RELEASE}\t%{INSTALLTIME}\t%{PACKAGER}\n%{DESCRIPTION}\n"
    p = subprocess.run(["rpm", "-qa", "gpg-pubkey", "--qf", fmt], capture_output=True, text=True, env=ENV_C,
                       timeout=120)
    out = []
    for chunk in p.stdout.split(_SEP)[1:]:
        head, _nl, body = chunk.partition("\n")
        f = head.split("\t")
        if len(f) < 5:
            continue
        itime = int(f[3]) if f[3].isdigit() else None
        out.append((f[0], f[1].lower(), f[2].lower(), itime, f[4], body))
    if out:
        return out
    # rpm 6 can keep its keys in files instead of the database: 'rpmkeys --export' gives them all
    try:
        p = subprocess.run(["rpmkeys", "--export"], capture_output=True, text=True, env=ENV_C, timeout=120)
    except OSError:
        return []
    if p.returncode != 0:
        return []
    blocks = re.findall(r"-----BEGIN PGP PUBLIC KEY BLOCK-----.*?-----END PGP PUBLIC KEY BLOCK-----", p.stdout, re.S)
    return [("", "", "", None, "", b) for b in blocks]


def _match_rpm_entry(entry, parsed):
    """The parsed key that is this gpg-pubkey (its version is the short or the full key ID, its release the
    creation time in hex)."""
    _nvr, version, release, _t, _p, _b = entry
    for k in parsed:
        ids = {k["fpr"].lower(), k["fpr"][-8:].lower(), k["fpr"][-16:].lower()}
        created = "%08x" % k["created"] if k["created"] else ""
        if version in ids and (not release or release == created):
            return k
    for k in parsed:  # the release is not always the creation time (rpm 6, keys updated in place)
        if version in {k["fpr"].lower(), k["fpr"][-8:].lower(), k["fpr"][-16:].lower()}:
            return k
    return None


def shipped_fingerprints():
    """Fingerprints of the keys openSUSE ships with the system (they can never be deleted)."""
    files = sorted(SHIPPED_DIR.glob("*.asc")) if SHIPPED_DIR.is_dir() else []
    data = b""
    for f in files:
        try:
            data += f.read_bytes()[:MAX_KEY] + b"\n"
        except OSError:
            continue
    if not data:
        return set()
    _code, out = _gpg(["--with-colons", "--fixed-list-mode", "--show-keys"], data)
    return {k["fpr"] for k in parse_colons(out)}


def _signature_ids(text):
    """Key IDs in rpm's ':pgpsig' text ('RSA/SHA512, <date>, Key ID 35a2f86e29b700a4')."""
    return {m.upper() for m in re.findall(r"Key ID ([0-9a-fA-F]{8,64})", text)}


def package_signers():
    """Counter {key ID: number of installed packages it signed}, or None when rpm cannot tell."""
    try:
        tags = set(subprocess.run(["rpm", "--querytags"], capture_output=True, text=True, env=ENV_C,
                                  timeout=60).stdout.split())
    except (OSError, subprocess.SubprocessError):
        tags = set()
    parts = [f"%{{{t}:pgpsig}}|" for t in ("RSAHEADER", "DSAHEADER") if t in tags or not tags]
    formats = []
    if "OPENPGP" in tags:  # rpm 6: a package can carry several signatures
        formats.append("".join(parts) + r"[%{OPENPGP:pgpsig}|]\n")
    formats.append("".join(parts) + r"\n")
    for fmt in formats:
        try:
            p = subprocess.run(["rpm", "-qa", "--qf", fmt], capture_output=True, text=True, env=ENV_C, timeout=300)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode != 0 or "incorrect format" in p.stderr or not p.stdout.strip():
            continue
        lines = p.stdout.splitlines()
        if not any("Key ID" in ln or "(none)" in ln for ln in lines):
            continue
        counts = Counter()
        for ln in lines:
            for kid in _signature_ids(ln):
                counts[kid] += 1
        return counts
    return None


def _signature_of_metadata(path):
    """Key IDs and fingerprints that signed a repository's metadata (repomd.xml.asc)."""
    try:
        data = path.read_bytes()[:MAX_KEY]
    except OSError:
        return set()
    _code, out = _gpg(["--list-packets"], data)
    text = out.decode("utf-8", "replace")
    ids = {m.upper() for m in re.findall(r"keyid ([0-9A-Fa-f]{16})", text)}
    ids |= {m.upper() for m in re.findall(r"issuer fpr v\d ([0-9A-Fa-f]{40,64})", text)}
    return ids


def repo_signers():
    """[(alias, name, enabled, {key IDs that signed its metadata})] for every repository."""
    out = []
    for r in backend.repos():
        alias = r["alias"]
        if not alias or "/" in alias or alias in (".", ".."):
            continue
        ids = set()
        for rel in ("repodata/repomd.xml.asc", "content.asc"):
            p = RAW_CACHE / alias / rel
            if p.is_file():
                ids |= _signature_of_metadata(p)
        out.append((alias, r["name"], r["enabled"], ids))
    return out


GROUPS = ("repos", "packages", "opensuse", "unused")


def list_keys():
    """Every key rpm trusts, with who uses it. See the module docstring for where each fact comes from.

    Each key: fpr, keyid (last 8 hex digits, upper case), ids, fprs, name, email, created, expires, expired,
    revoked, installed, nvr, repos [(alias, name, enabled)], packages (int, or None when unknown), shipped,
    group ('repos', 'packages', 'opensuse' or 'unused') and remove_command (what the helper will run).
    """
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_sigs = pool.submit(package_signers)  # the slowest (every installed package): it starts first
        f_repos = pool.submit(repo_signers)
        f_shipped = pool.submit(shipped_fingerprints)
        entries = _rpm_keys()
        data = "\n".join(e[5] for e in entries).encode("utf-8", "replace")
        parsed = parse_colons(_gpg(["--with-colons", "--fixed-list-mode", "--show-keys"], data)[1]) if data else []
        keys = []
        for e in entries:
            k = _match_rpm_entry(e, parsed)
            if k is None:  # one key gpg could not read in the batch: read it alone
                found = parse_colons(_gpg(["--with-colons", "--fixed-list-mode", "--show-keys"],
                                          e[5].encode("utf-8", "replace"))[1])
                k = found[0] if len(found) == 1 else None
            if k is None:
                name, email = split_uid(e[4])
                k = {"fpr": "", "ids": set(), "fprs": set(), "uids": [e[4]] if e[4] else [], "name": name,
                     "email": email, "created": None, "expires": None, "expired": False, "revoked": False}
            k = dict(k)
            k["nvr"] = e[0] or ("gpg-pubkey-" + k["fpr"][-8:].lower() if k["fpr"] else "")
            k["installed"] = e[3]
            k["keyid"] = (k["fpr"][-8:] if k["fpr"] else e[1][-8:]).upper()
            if not k["name"]:
                k["name"] = _("(no name in the key)")
            keys.append(k)
        try:
            signers = f_repos.result()
        except (backend.ZypperError, OSError, subprocess.SubprocessError):
            signers = []
        shipped = f_shipped.result()
        try:
            pkg = f_sigs.result()
        except (OSError, subprocess.SubprocessError):
            pkg = None
        major = rpm_major()
    for k in keys:
        k["remove_command"] = remove_command(k, major)
        k["repos"] = [(alias, name, enabled) for alias, name, enabled, ids in signers
                      if any(matches_id(k, i) for i in ids)]
        k["packages"] = None if pkg is None else sum(n for kid, n in pkg.items() if matches_id(k, kid))
        k["shipped"] = bool(k["fpr"]) and k["fpr"] in shipped
        k["group"] = ("repos" if k["repos"] else "packages" if k["packages"] else
                      "opensuse" if k["shipped"] else "unused")
    keys.sort(key=lambda k: (GROUPS.index(k["group"]), k["name"].lower()))
    return keys


# ------------------------------------------------------------------ getting a key to import

def fetch_url(url):
    """Download a key file: https only (redirects too), at most 1 MiB."""
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise ValueError(_("Only https addresses are allowed: over plain http anybody on the way could swap "
                           "the key."))
    req = urllib.request.Request(url, headers={"User-Agent": websources.USER_AGENT})
    try:
        with websources.https_open(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_KEY + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(_("The server answered with an error ({code}).").format(code=e.code)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(_("Could not download it: {reason}").format(reason=getattr(e, "reason", e))) from e
    if len(data) > MAX_KEY:
        raise ValueError(_("The file is bigger than 1 MiB: it is not a signing key."))
    return data


def fetch_fingerprint(fpr):
    """The key with this fingerprint from keys.openpgp.org. The caller must check that it is that key."""
    norm = normalize_fpr(fpr)
    if not norm:
        raise ValueError(_("A fingerprint has 40 hexadecimal digits (0-9, A-F)."))
    try:
        return fetch_url(KEYSERVER.format(fpr=urllib.parse.quote(norm)))
    except ValueError as e:
        if "(404)" in str(e):
            raise ValueError(_("keys.openpgp.org does not have a key with this fingerprint.")) from None
        raise


def web_folders(settings=None, sources=None):
    """The folders where programs downloaded from the web are kept: the one chosen in Zypp Desk, those of the
    web sources and the local folder repositories (plaindir). Only folders that exist."""
    found = []
    if settings is not None:
        found.append(settings.get("web_folder") or "")
    for s in (websources.load() if sources is None else sources):
        found.append(s.get("folder") or "")
    try:
        for r in backend.repos():
            if r.get("type") == "plaindir" and r.get("url", "").startswith("dir:"):
                found.append(urllib.parse.unquote(r["url"][4:]))
    except (backend.ZypperError, OSError, subprocess.SubprocessError):
        pass
    out = []
    for f in found:
        if not f:
            continue
        d = os.path.realpath(os.path.expanduser(f))
        if d not in out and os.path.isdir(d):
            out.append(d)
    return out


def _file_signers(path):
    """Key IDs in an RPM file's header signature (only the header is read: fast even for big files)."""
    p = subprocess.run(["rpm", "-qp", "--nosignature", "--nodigest", "--qf",
                        "%{RSAHEADER:pgpsig}|%{DSAHEADER:pgpsig}", "--", str(path)],
                       capture_output=True, text=True, env=ENV_C, timeout=60)
    return _signature_ids(p.stdout) if p.returncode == 0 else set()


def local_files_signed_by(ids, folders):
    """The .rpm files in these folders signed by one of these key IDs (16 hex digits, or fingerprints)."""
    key = {"ids": {i.upper() for i in ids if len(i) <= 16}, "fprs": {i.upper() for i in ids if len(i) >= 40}}
    files, seen = [], set()
    for folder in folders:
        try:
            entries = sorted(Path(folder).iterdir())
        except OSError:
            continue
        for f in entries:
            try:
                real = f.resolve()
                if f.suffix != ".rpm" or real in seen or not f.is_file():
                    continue
            except OSError:
                continue
            seen.add(real)
            files.append(f)
            if len(files) >= _MAX_FILES:
                break
    with ThreadPoolExecutor(max_workers=6) as pool:
        signers = list(pool.map(_file_signers, files))
    return [str(f) for f, sig in zip(files, signers) if any(matches_id(key, s) for s in sig)]


def key_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "zypp-desk" / "keys"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def save_for_import(data: bytes, fpr):
    """Keep the key in ~/.cache/zypp-desk/keys/<FPR>.asc for the helper, which reads it again as root and
    imports it only if it is exactly the key with this fingerprint. Returns the path."""
    norm = normalize_fpr(fpr)
    if not norm:
        raise ValueError(_("A fingerprint has 40 hexadecimal digits (0-9, A-F)."))
    data = armored(data, norm)
    path = key_dir() / f"{norm}.asc"
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return str(path)


def installed_keys():
    """{fingerprint: key} of the keys rpm already trusts (one rpm and one gpg call: quick enough for a dialog)."""
    entries = _rpm_keys()
    data = "\n".join(e[5] for e in entries).encode("utf-8", "replace")
    if not data:
        return {}
    return {k["fpr"]: k for k in parse_colons(_gpg(["--with-colons", "--fixed-list-mode", "--show-keys"], data)[1])}


def rpm_major():
    p = subprocess.run(["rpm", "--version"], capture_output=True, text=True, env=ENV_C, timeout=30)
    m = re.search(r"(\d+)\.", p.stdout)
    return int(m.group(1)) if m else 4


def remove_command(key, major=4):
    """What the helper runs to delete this key (see key_remove in helper/zypp-desk-helper): rpm 4 names a key
    by the version-release of its gpg-pubkey package, rpm 6 by its fingerprint."""
    nvr = key.get("nvr") or ""
    if major < 6 and nvr.startswith("gpg-pubkey-"):
        return "rpmkeys --delete " + nvr[len("gpg-pubkey-"):]
    return "rpmkeys --delete " + (key.get("fpr") or "?")
