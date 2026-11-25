# SPDX-License-Identifier: GPL-3.0-or-later
"""A downloaded .rpm file: what it is, whether it can be installed here, and who made it.

read_rpm(path) reads the file with rpm as the normal user, without the network and without changing anything:
name, version, who made it, the version installed now, the signature (as the programs from the web check it,
websources.signature_of_file) and what it needs. Files that must not be installed come back with 'refuse' set
to the reason, in words for people: not an RPM package, a source package, made for another processor, damaged,
a signature that does not verify, unreadable.

obs_repo(info) says which repository of the openSUSE Build Service the file comes from, when it does;
check_obs(obs, keyid) asks that repository (network, https) whether it exists for Tumbleweed and whether the
key it publishes is the one that signed the file.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

from . import online, websources
from .backend import ENV_C, vercmp
from .i18n import _

RPM_MAGIC = b"\xed\xab\xee\xdb"  # the first bytes of every .rpm file (its "lead")
TIMEOUT = 15
OBS_RE = re.compile(r"obs://build\.opensuse\.org/([A-Za-z0-9_][A-Za-z0-9_.:+-]{0,199})(?:/([A-Za-z0-9_.+-]+))?")
# projects of openSUSE itself: their packages are already in the official repositories
OFFICIAL_RE = re.compile(r"openSUSE:(Factory|Tumbleweed)(:.*)?")

DAMAGED = _("The file is damaged, or it was changed after it was made: its checksums or its signature do not "
            "match. Do not install it; download it again from the official site.")

SENSE_LESS, SENSE_GREATER, SENSE_EQUAL = 0x02, 0x04, 0x08
SENSE_RPMLIB = 1 << 24

# One line per field, 'key<TAB>value', the value written as JSON by rpm itself (:json), so that any text in the
# header (tabs, new lines, quotes) comes back intact. Requirements and provides: one line each.
_QF = "".join(f"{key}\t%|{tag}?{{%{{{tag}:json}}}}:{{null}}|\\n" for key, tag in (
    ("name", "NAME"), ("version", "VERSION"), ("release", "RELEASE"), ("arch", "ARCH"), ("summary", "SUMMARY"),
    ("description", "DESCRIPTION"), ("url", "URL"), ("vendor", "VENDOR"), ("packager", "PACKAGER"),
    ("license", "LICENSE"), ("distribution", "DISTRIBUTION"), ("disturl", "DISTURL"), ("sourcerpm", "SOURCERPM"),
    ("buildhost", "BUILDHOST"))) + (
    "epoch\t%|EPOCH?{%{EPOCH}}:{null}|\\n"
    "size\t%|LONGSIZE?{%{LONGSIZE}}:{0}|\\n"
    "buildtime\t%|BUILDTIME?{%{BUILDTIME}}:{0}|\\n"
    "srcpkg\t%|SOURCEPACKAGE?{1}:{0}|\\n"
    "[req\t%{REQUIRENAME:json}\t%{REQUIREFLAGS}\t%{REQUIREVERSION:json}\\n]"
    "[prov\t%{PROVIDENAME:json}\\n]")
_QF = _QF.replace("\\n", "\n")
# the signature lines, asked apart: these tags change between rpm versions, and a tag rpm does not know makes the
# whole query fail (then only the short key id from 'rpm -K' is shown)
_QF_SIG = "%{RSAHEADER:pgpsig}\n%{DSAHEADER:pgpsig}\n%{SIGPGP:pgpsig}\n%{SIGGPG:pgpsig}\n"


def machine_arch():
    return os.uname().machine


def evr(info):
    """'epoch:version-release' (no epoch when there is none), the way rpm compares versions."""
    e = info.get("epoch")
    return (f"{e}:" if e not in (None, "", "0", 0) else "") + f"{info.get('version', '')}-{info.get('release', '')}"


def _json(value):
    try:
        v = json.loads(value)
    except ValueError:
        return value
    return v


def _rpm(args):
    return subprocess.run(["rpm", *args], capture_output=True, text=True, env=ENV_C, errors="replace")


def _text(raw: bytes):
    """A header field as text. rpm gives the bytes as they are in the package; most are UTF-8, but older packages
    (AutoFirma, Configurador FNMT) were written in Latin-1 / Windows-1252: 'Aplicación', not 'Aplicaci�n'."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _header(path):
    """The fields of the file's header (no signature or digest check here: read_rpm does those itself)."""
    # bytes, decoded field by field (_text): one package can mix UTF-8 and Latin-1 fields
    p = subprocess.run(["rpm", "-qp", "--nosignature", "--nodigest", "--qf", _QF, "--", path], capture_output=True,
                       env=ENV_C)
    if p.returncode != 0 or not p.stdout.startswith(b"name\t"):
        return None
    h = {"requires": [], "provides": [], "sigs": []}
    for raw in p.stdout.split(b"\n"):
        if not raw:
            continue
        key, _sep, val = _text(raw).partition("\t")
        if key == "req":
            f = val.split("\t")
            if len(f) == 3:
                try:
                    flags = int(f[1])
                except ValueError:
                    flags = 0
                h["requires"].append((_json(f[0]) or "", flags, _json(f[2]) or ""))
        elif key == "prov":
            h["provides"].append(_json(val) or "")
        elif key in ("epoch", "size", "buildtime", "srcpkg"):
            v = _json(val)
            h[key] = v if isinstance(v, int) else (None if key == "epoch" else 0)
        elif key not in h:  # only the first package of the file
            v = _json(val)
            h[key] = v if isinstance(v, str) else ""
        else:
            break
    p = _rpm(["-qp", "--nosignature", "--nodigest", "--qf", _QF_SIG, "--", path])
    if p.returncode == 0:
        h["sigs"] = [ln for ln in p.stdout.splitlines() if ln.strip() and ln.strip() != "(none)"]
    return h


def _op(flags):
    return {SENSE_LESS: "<", SENSE_GREATER: ">", SENSE_EQUAL: "=", SENSE_LESS | SENSE_EQUAL: "<=",
            SENSE_GREATER | SENSE_EQUAL: ">="}.get(flags & (SENSE_LESS | SENSE_GREATER | SENSE_EQUAL), "")


def _without_release(ed):
    epoch, sep, rest = ed.rpartition(":")
    return epoch + sep + rest.split("-")[0]


def _satisfies(pflags, pver, rflags, rver):
    """Whether a provide (flags, version) meets a versioned requirement, as rpm decides it (for the usual '='
    provides; anything rarer is left to zypper and counted as met)."""
    rop = rflags & (SENSE_LESS | SENSE_GREATER | SENSE_EQUAL)
    pop = pflags & (SENSE_LESS | SENSE_GREATER | SENSE_EQUAL)
    if not rver or not rop or not pver or pop != SENSE_EQUAL:
        return True
    if "-" not in rver.rpartition(":")[2]:  # the requirement names no release: compare the versions only
        pver = _without_release(pver)
    c = vercmp(pver, rver)
    return bool((c < 0 and rop & SENSE_LESS) or (c == 0 and rop & SENSE_EQUAL) or (c > 0 and rop & SENSE_GREATER))


def requirements(head):
    """What the package needs, without rpm's own features (rpmlib(…)), file paths and what it brings itself.

    Returns (provided, missing): provided = {installed package: [capabilities]}; missing = [capability text]
    that no installed package gives: zypper will look for them in the repositories.
    """
    own = set(head["provides"])
    reqs, seen = [], set()
    for name, flags, ver in head["requires"]:
        if not name or name.startswith("/") or name.startswith("rpmlib(") or flags & SENSE_RPMLIB or name in own:
            continue
        key = (name, flags & 0x0E, ver)
        if key not in seen:
            seen.add(key)
            reqs.append((name, flags, ver))
    if not reqs:
        return {}, []
    names = sorted({n for n, _f, _v in reqs})
    p = _rpm(["-q", "--whatprovides", "--qf", "[%{=NAME}\t%{PROVIDENAME}\t%{PROVIDEFLAGS}\t%{PROVIDEVERSION}\n]",
              "--", *names])
    offers = {}
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 4 and f[1] in names:
            try:
                pflags = int(f[2])
            except ValueError:
                pflags = 0
            offers.setdefault(f[1], []).append((f[0], pflags, f[3]))
    provided, missing = {}, []
    for name, flags, ver in reqs:
        who = [pkg for pkg, pflags, pver in offers.get(name, []) if _satisfies(pflags, pver, flags, ver)]
        if who:
            provided.setdefault(sorted(who)[0], []).append(name)
        else:
            op = _op(flags)
            text = f"{name} {op} {ver}" if op and ver else name
            if text not in missing:
                missing.append(text)
    return provided, missing


def installed_versions(name):
    """Every installed edition of a package name ([] when it is not installed)."""
    p = _rpm(["-q", "--qf", "%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\t%{ARCH}\n", "--", name])
    out = []
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 2 and p.returncode == 0:
            out.append(f[0])
    return out


def key_name(keyid):
    """Who a key that rpm trusts belongs to, from its gpg-pubkey package ('' when unknown)."""
    if not keyid:
        return ""
    p = _rpm(["-q", "gpg-pubkey", "--qf", "%{VERSION}\t%{PACKAGER}\n"])
    for line in p.stdout.splitlines():
        ver, _sep, who = line.partition("\t")
        if ver and ver.lower()[-8:] == keyid.lower()[-8:]:
            return who.strip()
    return ""


def _long_keyid(sigs):
    for s in sigs:
        m = re.search(r"Key ID ([0-9a-fA-F]{8,40})", s)
        if m:
            return m.group(1).upper()
    return ""


def _stamp(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def read_rpm(path):
    """Everything about a downloaded .rpm file (nothing changes, no network). Never raises for a bad file:
    the dict has 'refuse' set to the reason instead.

    Keys: path, file_name, file_size, sha256, refuse (None or text), name, epoch, version, release, evr, arch,
    summary, description, url, vendor, packager, license, distribution, disturl, size (installed), buildtime,
    built_for (the OBS repository it was built for, or ''), installed (editions), installed_evr (the newest),
    compare ('not-installed', 'newer', 'same', 'older'), signature ('verified', 'nokey:<id>', 'unsigned', 'bad'),
    keyid (upper case; the long id from the header when there is one), signer_name, digests_ok,
    provided ({package: [capabilities]}), missing ([capabilities]).
    """
    path = os.path.abspath(path)
    info = {"path": path, "file_name": os.path.basename(path), "file_size": 0, "sha256": "", "refuse": None,
            "name": "", "epoch": None, "version": "", "release": "", "evr": "", "arch": "", "summary": "",
            "description": "", "url": "", "vendor": "", "packager": "", "license": "", "distribution": "",
            "disturl": "", "size": 0, "buildtime": 0, "built_for": "", "installed": [], "installed_evr": None,
            "compare": "not-installed", "signature": "", "keyid": "", "signer_name": "", "digests_ok": None,
            "provided": {}, "missing": [], "source": False, "refuse_kind": None}
    low = path.lower()
    if not os.path.isfile(path):
        exists = os.path.exists(path)
        info["refuse"] = _("This is not a file.") if exists else _("There is no such file.")
        info["refuse_kind"] = "not-file" if exists else "missing"
        return info
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        info["file_size"] = os.path.getsize(path)
    except OSError as e:
        info["refuse"] = _("The file cannot be read: {error}").format(error=e.strerror or e)
        info["refuse_kind"] = "unreadable"
        return info
    if magic != RPM_MAGIC:
        # never hand a file that is not an rpm to rpm -qp: it reads a text file as a list of other files to open
        info["refuse"] = _("This is not an RPM package. Zypp Desk can only install .rpm files made for openSUSE "
                           "(or for a compatible system).")
        info["refuse_kind"] = "not-rpm"
        return info
    if low.endswith(".src.rpm") or low.endswith(".nosrc.rpm"):
        info["source"] = True
    try:
        before = _stamp(path)
        info["sha256"] = websources.sha256_of(path)
    except OSError as e:
        info["refuse"] = _("The file cannot be read: {error}").format(error=e.strerror or e)
        info["refuse_kind"] = "unreadable"
        return info
    head = _header(path)
    if head is None:
        info["refuse"] = _("rpm cannot read this file: it is damaged, or it is not a real RPM package.")
        info["refuse_kind"] = "damaged"
        return info
    for k in ("name", "version", "release", "arch", "summary", "description", "url", "vendor", "packager",
              "license", "distribution", "disturl", "epoch", "size", "buildtime"):
        info[k] = head.get(k) if head.get(k) is not None else info[k]
    info["evr"] = evr(info)
    if head.get("srcpkg") or not head.get("sourcerpm"):
        info["source"] = True
    m = OBS_RE.match(info["disturl"] or "")
    if m and m.group(2):
        project, repo = m.group(1), m.group(2)
        if repo in ("standard", "snapshot", "images"):  # openSUSE's own projects: the project says the system
            repo = "openSUSE_Tumbleweed" if OFFICIAL_RE.fullmatch(project) else project
        info["built_for"] = repo

    # signature: the same check as the programs from the web, plus the checksums of the whole file
    sig, signer = websources.signature_of_file(path, with_signer=True)
    info["signature"] = sig
    info["keyid"] = _long_keyid(head["sigs"]) or (signer or (sig[6:] if sig.startswith("nokey:") else "")).upper()
    if sig == "verified":
        info["signer_name"] = key_name(signer or info["keyid"])
    info["digests_ok"] = _rpm(["-K", "--nosignature", "--", path]).returncode == 0

    inst = installed_versions(info["name"]) if info["name"] else []
    info["installed"] = inst
    if inst:
        newest = inst[0]
        for v in inst[1:]:
            if vercmp(v, newest) > 0:
                newest = v
        info["installed_evr"] = newest
        c = vercmp(info["evr"], newest)
        info["compare"] = "newer" if c > 0 else "older" if c < 0 else "same"
    info["provided"], info["missing"] = requirements(head)

    try:
        changed = _stamp(path) != before
    except OSError:
        changed = True
    arch = machine_arch()
    if changed:
        info["refuse"] = _("The file changed while it was being read. Wait until the download finishes, then open "
                           "it again.")
        info["refuse_kind"] = "changed"
    elif info["source"]:
        info["refuse"] = _("This is a source package (.src.rpm): it holds the source code to build the program, not "
                           "the program itself, so it cannot be installed. Look for the file whose name ends in "
                           "{arch}.rpm or noarch.rpm.").format(arch=arch)
        info["refuse_kind"] = "source"
    elif info["arch"] not in (arch, "noarch"):
        info["refuse"] = _("This package is made for computers whose processor is {arch}. The processor of this "
                           "computer is {machine}, so it would not work here. Look for the file whose name ends in "
                           "{machine}.rpm or noarch.rpm.").format(arch=info["arch"] or "?", machine=arch)
        info["refuse_kind"] = "arch"
    elif not info["digests_ok"] or sig == "bad":
        info["refuse"] = DAMAGED
        info["refuse_kind"] = "damaged"
    return info


def obs_repo(info):
    """The openSUSE Build Service repository a file comes from (from its DISTURL or its vendor), as
    dict(project, url, key_url, alias, name, built_for), or None. Files of openSUSE itself (openSUSE:Factory)
    give None: their packages are already in the official repositories."""
    for text in (info.get("disturl"), info.get("vendor")):
        m = OBS_RE.match(text or "")
        if m:
            project = m.group(1).rstrip(":")
            break
    else:
        return None
    if OFFICIAL_RE.fullmatch(project) or not online.OBS_NAME_RE.fullmatch(project):
        return None
    url = online.obs_repo_url(project, "openSUSE_Tumbleweed")
    return {"project": project, "url": url, "key_url": url + "repodata/repomd.xml.key",
            "alias": online.obs_alias(project), "name": project, "built_for": info.get("built_for", "")}


def check_obs(obs, keyid=""):
    """Network: is there a Tumbleweed repository for this project, and, when keyid is given (the file is signed
    with a key rpm does not know), is the key the project publishes the one that signed the file?

    Returns dict(exists: True/False/None (unknown), error, fingerprint, uids, key_matches: True/False/None,
    key_error). The key is only read (online.fetch_key keeps gpg's copy of it in the cache), never imported.
    """
    out = {"exists": None, "error": None, "fingerprint": None, "uids": [], "key_matches": None, "key_error": None}
    req = urllib.request.Request(obs["url"] + "repodata/repomd.xml", method="HEAD",
                                 headers={"User-Agent": websources.USER_AGENT})
    try:
        with websources.https_open(req, timeout=TIMEOUT) as r:
            out["exists"] = 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 410):
            out["exists"] = False
        else:
            out["error"] = _("The server answered with an error ({code}).").format(code=e.code)
        return out
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        out["error"] = str(getattr(e, "reason", e))
        return out
    if out["exists"] and keyid:
        try:
            k = online.fetch_key(obs["key_url"])
        except (online.SearchError, OSError) as e:
            out["key_error"] = str(e)
        else:
            out["fingerprint"] = k["fingerprint"].upper()
            out["uids"] = k["uids"]
            out["key_matches"] = out["fingerprint"].endswith(keyid.upper())
    return out


def signature_now(path):
    """The signature check again (after a key was imported): ('verified'|'nokey:…'|'unsigned'|'bad', signer
    name)."""
    sig, signer = websources.signature_of_file(path, with_signer=True)
    return sig, (key_name(signer) if sig == "verified" else "")
