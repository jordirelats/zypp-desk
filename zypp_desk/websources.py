# SPDX-License-Identifier: GPL-3.0-or-later
"""Web sources: packages installed from RPM files downloaded from a web site.

zypper cannot know that RustDesk 1.5.3 exists if the RPM came from a folder. A web source says
where to look and which file to take:

- "github": the latest release of a GitHub project; GitHub publishes the SHA-256 of each file.
- "url": a fixed address that always gives the latest file (Zoom does this). The version is read
  from the address it redirects to, when it has one.
- "page": a download page; the newest link that matches a pattern is taken (Iriun, AutoFirma).
  A .zip with an RPM inside is opened.
- "manual": not followed; only a note (packages made on this computer, pages that cannot be read).

When the version cannot be read from the address, Zypp Desk remembers which file it saw last
(its ETag, or its date and size) and says when it changes; downloading it tells the version.

Downloads go to a private folder (~/.cache/zypp-desk/downloads), are checked (SHA-256 when it is
published, the package name, the rpm signature) and move to the source's folder (usually the folder
that is already a local zypper repository) only when the user accepts them.

The list lives in ~/.config/zypp-desk/web-sources.json.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import VERSION
from .backend import ENV_C, vercmp
from .i18n import _

API = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = f"ZyppDesk/{VERSION}"
TYPES = ("github", "url", "page", "manual")
MAX_PAGE = 4 * 1024 * 1024
MAX_RPM_IN_ZIP = 2 * 1024 * 1024 * 1024
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024
ARCH_RE = re.compile(r"x86[_-]64|amd64|aarch64|arm64|i[3-6]86|noarch", re.I)
VERSION_RE = re.compile(r"\d+(?:[._]\d+)+")


def _path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "zypp-desk" / "web-sources.json"


def load():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("package")]


def save(sources):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def new_source(package, kind, folder="", *, repo="", pattern="", url="", page="", note=""):
    s = {"package": package, "type": kind, "folder": folder}
    if kind == "github":
        s.update(repo=repo, pattern=pattern)
    elif kind == "url":
        s.update(url=url)
    elif kind == "page":
        s.update(page=page, pattern=pattern)
    if note or kind == "manual":
        s["note"] = note
    if kind == "manual" and page:
        s["page"] = page
    return s


def origin_text(source):
    """Where a source looks, in a few words, for the table."""
    t = source.get("type", "github")
    if t == "github":
        return "github.com/" + source.get("repo", "")
    if t == "url":
        return urllib.parse.urlsplit(source.get("url", "")).netloc
    if t == "page":
        return urllib.parse.urlsplit(source.get("page", "")).netloc
    return source.get("note") or _("not followed")


def installed_version(package):
    p = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\n", "--", package], capture_output=True,
                       text=True, env=ENV_C)
    lines = p.stdout.split()
    return lines[-1] if p.returncode == 0 and lines else None


def guess_pattern(package, filename=None):
    """A regular expression for the release file, from the file installed before when we know it."""
    if filename:
        m = re.match(rf"^{re.escape(package)}-[\d.]+(-\d+)?(.*)$", filename)
        if m:
            return rf"^{re.escape(package)}-.*{re.escape(m.group(2))}$"
    return rf"^{re.escape(package)}-.*\.x86_64.*\.rpm$"


def guess_link_pattern(package):
    """A regular expression for the link to take on a download page: the package name followed by anything,
    ending in .rpm or .zip (the page check compares it with the whole address, ignoring case)."""
    return rf"{re.escape(package)}[-_][^/]*\.(rpm|zip)$"


def version_in_url(url):
    """The version written in a download address: in the file name, or else in a folder name.

    'iriunwebcam-2.9.3.rpm' → 2.9.3; '…/prod/7.2.5.6661/zoom_openSUSE_x86_64.rpm' → 7.2.5.6661.
    Architecture names (x86_64…) do not count. None when there is no version.
    """
    parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(url).path.split("/") if p]
    if not parts:
        return None
    name = ARCH_RE.sub("", parts[-1])
    m = VERSION_RE.search(name)
    if m:
        return m.group(0).replace("_", ".")
    for folder in reversed(parts[:-1]):
        if re.fullmatch(r"[vV]?\d+(?:\.\d+)+", folder):
            return folder.lstrip("vV")
    return None


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https addresses: nothing is ever read over plain http.

    Some sites redirect to an http address that also works over https (firmaelectronica.gob.es does):
    the redirect is upgraded to https, as browsers do; if https does not answer there, the request fails.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        if newurl.startswith("http://"):
            newurl = "https://" + newurl[len("http://"):]
        if not newurl.startswith("https://"):
            raise urllib.error.URLError(_("The server sent the download to an address that is not https."))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirects)


def https_open(req, timeout=30):
    """urlopen for https only, redirects included. Used by every network request of Zypp Desk."""
    if not req.full_url.startswith("https://"):
        raise ValueError(_("Only https downloads are allowed."))
    return _opener.open(req, timeout=timeout)


def _request(url, method="GET", accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return https_open(urllib.request.Request(url, method=method, headers=headers))


CHECK_TTL = 600  # seconds: the pages share what was looked at in the last ten minutes
_memo = {}
_http_lock = threading.Lock()


def _http_cache_path():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "zypp-desk" / "github-cache.json"


def _http_cache():
    try:
        return json.loads(_http_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _get_json(url):
    """GitHub's answer, asked with the ETag of the last one: an unchanged answer (304) does not count
    against the 60 questions an hour GitHub allows without an account. When the limit is reached, the
    last answer is used if there is one."""
    with _http_lock:
        entry = _http_cache().get(url)
    if not url.startswith("https://"):
        raise ValueError(_("Only https downloads are allowed."))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    try:
        with https_open(urllib.request.Request(url, headers=headers)) as r:
            data = json.load(r)
            etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and entry:
            return entry["data"]
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            if entry:
                return entry["data"]
            reset = int(e.headers.get("X-RateLimit-Reset") or 0)
            minutes = max(1, int((reset - time.time()) / 60) + 1)
            raise ValueError(_("GitHub answers only 60 questions an hour without an account: try again in {n} "
                               "minutes.").format(n=minutes)) from None
        raise
    if etag:
        with _http_lock:
            cache = _http_cache()
            cache[url] = {"etag": etag, "data": data}
            _save_http_cache(cache)
    return data


def _save_http_cache(cache):
    """Write the cache through a temporary file of its own: the window and the tray icon (two processes) may
    write at the same moment, and neither must lose the file or read half of it. A cache that cannot be
    written only means asking GitHub again next time."""
    path = _http_cache_path()
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".github-cache-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _head(url):
    """Final address after redirects, size, date and a fingerprint of the file there."""
    with _request(url, "HEAD") as r:
        final = r.geturl()
        if not final.startswith("https://"):
            raise ValueError(_("Only https downloads are allowed."))
        size = int(r.headers.get("Content-Length") or 0) or None
        etag = r.headers.get("ETag") or ""
        modified = r.headers.get("Last-Modified") or ""
    try:
        published = parsedate_to_datetime(modified).date().isoformat() if modified else ""
    except (TypeError, ValueError):
        published = ""
    return {"url": final, "size": size, "published": published,
            "fingerprint": etag or (f"{modified}|{size}" if modified else "")}


def _links(page):
    """Absolute https links of a web page (only what is written in the HTML: no JavaScript)."""
    with _request(page) as r:
        base = r.geturl()
        data = r.read(MAX_PAGE + 1)
        charset = r.headers.get_content_charset() or "utf-8"
    if len(data) > MAX_PAGE:
        raise ValueError(_("The page is too big."))
    text = data.decode(charset, errors="replace")
    out = []
    for m in re.finditer(r"""href\s*=\s*["']([^"'<>\s]+)["']""", text, re.I):
        u = urllib.parse.urljoin(base, m.group(1).replace("&amp;", "&"))
        if u.startswith("https://") and u not in out:
            out.append(u)
    return out


def _check_github(source):
    rel = _get_json(API.format(repo=source["repo"]))
    tag = rel.get("tag_name") or ""
    rx = re.compile(source["pattern"])
    asset = next((a for a in rel.get("assets", []) if rx.search(a["name"])), None)
    digest = (asset or {}).get("digest") or ""
    return {
        "tag": tag, "version": re.sub(r"^[vV]", "", tag), "published": (rel.get("published_at") or "")[:10],
        "page": rel.get("html_url", ""),
        "asset": asset["name"] if asset else None, "url": asset["browser_download_url"] if asset else None,
        "size": asset.get("size") if asset else None,
        "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
    }


def _check_url(url, page=""):
    h = _head(url)
    return {"tag": "", "version": version_in_url(h["url"]), "published": h["published"], "page": page or url,
            "asset": Path(urllib.parse.unquote(urllib.parse.urlsplit(h["url"]).path)).name or None,
            "url": h["url"], "size": h["size"], "sha256": None, "fingerprint": h["fingerprint"]}


def _check_page(source):
    rx = re.compile(source["pattern"], re.I)
    host = urllib.parse.urlsplit(source["page"]).hostname or ""
    links = [u for u in _links(source["page"]) if rx.search(urllib.parse.unquote(u))
             and _same_site(urllib.parse.urlsplit(u).hostname or "", host)]
    if not links:
        raise ValueError(_("No link on the page matches {pattern}.").format(pattern=source["pattern"]))
    versioned = [(version_in_url(u), u) for u in links]
    versioned = [(v, u) for v, u in versioned if v]
    if versioned:
        best = versioned[0]
        for v, u in versioned[1:]:
            if vercmp(v, best[0]) > 0:
                best = (v, u)
        link = best[1]
    else:
        link = links[0]  # the first one on the page: pages list the newest first
    return _check_url(link, source["page"])


def check(source, force=False):
    """Look for the latest version. Returns a dict with what was found (never downloads).

    'newer' is True or False when the version is known; when it is not, it is None and 'changed' says
    whether the file is different from the one seen last time. What the web said is kept for ten minutes
    (CHECK_TTL) unless force is true; the installed version is always read again.
    """
    kind = source.get("type", "github")
    key = json.dumps({k: v for k, v in source.items() if k not in ("seen", "folder", "note")}, sort_keys=True)
    memo = _memo.get(key)
    if kind == "manual":
        found = {"manual": True, "version": None, "asset": None, "url": None, "page": source.get("page", "")}
    elif memo and not force and time.monotonic() - memo[0] < CHECK_TTL:
        found = dict(memo[1])
    else:
        if kind == "github":
            found = _check_github(source)
        elif kind == "url":
            found = _check_url(source["url"])
        elif kind == "page":
            found = _check_page(source)
        else:
            raise ValueError(_("Unknown kind of source: {kind}").format(kind=kind))
        _memo[key] = (time.monotonic(), dict(found))
    inst = installed_version(source["package"])
    inst_v = inst.split("-")[0] if inst else None
    found["installed"] = inst
    if found.get("manual"):
        found["newer"] = False
    elif found.get("version"):
        found["newer"] = bool(inst_v and vercmp(found["version"], inst_v) > 0)
    else:
        found["newer"] = None
        fp = found.get("fingerprint")
        found["changed"] = not (fp and fp == source.get("seen"))
    return found


def download(source, found, progress=None):
    """Download the file into a private folder and verify it.

    Returns dict(path, sha256, sha256_checked, signature, version, same). The file stays in
    ~/.cache/zypp-desk/downloads until the user accepts it (see place()); a file that fails a check is
    deleted. A .zip is opened and the package's RPM taken out of it. 'version' is the one inside the
    RPM; 'same' says it is the installed one.
    """
    folder = download_dir()
    name = Path(found["asset"] or "download").name
    final = folder / name
    part = final.with_name(final.name + ".part")
    try:
        return _download(source, found, folder, final, part, progress)
    except BaseException:
        part.unlink(missing_ok=True)  # nothing half downloaded stays behind
        raise


def _download(source, found, folder, final, part, progress):
    h = hashlib.sha256()
    done = 0
    with _request(found["url"]) as r, open(part, "wb") as out:
        total = int(r.headers.get("Content-Length") or found.get("size") or 0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if done > MAX_DOWNLOAD:
                raise ValueError(_("The file is bigger than 2 GiB: it was not downloaded."))
            if progress:
                progress(done, total)
    digest = h.hexdigest()
    if found.get("sha256") and digest != found["sha256"]:
        part.unlink(missing_ok=True)
        raise ValueError(_("The SHA-256 does not match: GitHub publishes {expected}, the file has {got}. "
                           "The file was deleted.").format(expected=found["sha256"], got=digest))
    if zipfile.is_zipfile(part):
        rpm = _rpm_from_zip(part, source["package"], folder)
        part.unlink(missing_ok=True)
        part = rpm
    head = _rpm_header(part)
    if head["name"] != source["package"]:
        part.unlink(missing_ok=True)
        raise ValueError(_("The file contains the package “{found}”, not “{expected}”. It was deleted.")
                         .format(found=head["name"] or "?", expected=source["package"]))
    if source.get("type", "github") != "github" or part.suffix != ".rpm":
        # files like zoom_openSUSE_x86_64.rpm carry no version: name them as rpm does, to keep each version
        final = folder / _safe_name(f"{head['name']}-{head['version']}-{head['release']}.{head['arch']}.rpm")
    part.replace(final)
    inst = installed_version(source["package"])
    sig, signer = signature_of_file(final, with_signer=True)
    return {"path": str(final), "sha256": digest, "sha256_checked": bool(found.get("sha256")),
            "file_sha256": sha256_of(final), "signature": sig, "signer": signer,
            "version": f"{head['version']}-{head['release']}",
            "same": inst == f"{head['version']}-{head['release']}"}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name):
    """A file name made from an RPM header: no folders, nothing odd."""
    return re.sub(r"[^A-Za-z0-9._+~^-]", "_", name).lstrip(".") or "download.rpm"


def _same_site(host, page_host):
    host, page_host = host.lower(), page_host.lower()
    return host == page_host or host.endswith("." + page_host) or page_host.endswith("." + host)


def _rpm_header(path):
    p = subprocess.run(["rpm", "-qp", "--qf", "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}", "--", str(path)],
                       capture_output=True, text=True, env=ENV_C)
    f = p.stdout.strip().split("\t") if p.returncode == 0 else []
    if len(f) != 4:
        return {"name": "", "version": "", "release": "", "arch": ""}
    return dict(zip(("name", "version", "release", "arch"), f))


def _rpm_from_zip(zpath, package, folder):
    """Take the RPM of this package out of a .zip, by file name only (nothing else is extracted)."""
    with zipfile.ZipFile(zpath) as z:
        members = [m for m in z.infolist() if m.filename.lower().endswith(".rpm") and not m.is_dir()]
        for m in members:
            if m.file_size > MAX_RPM_IN_ZIP:
                continue
            target = folder / (Path(m.filename).name + ".part")
            keep = False
            try:
                with z.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 16)
                keep = _rpm_header(target)["name"] == package
            finally:
                if not keep:
                    target.unlink(missing_ok=True)
            if keep:
                return target
    raise ValueError(_("The .zip file has no RPM of {package}.").format(package=package))


def folder_versions(folders):
    """Every RPM file in these folders, with its package, version and what it is to the system:
    'installed' (the version you have), 'older', 'newer' (not installed yet) or 'not-installed'.
    Used to offer to throw away old versions that pile up in the folder of programs from the web."""
    files, seen = [], set()
    for folder in folders:
        d = Path(os.path.expanduser(folder))
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            try:
                real = f.resolve()
                if f.suffix != ".rpm" or not f.is_file() or real in seen:
                    continue
                seen.add(real)
                size = f.stat().st_size
            except OSError:
                continue
            head = _rpm_header(f)
            if head["name"]:
                files.append({"path": str(f), "folder": str(d), "name": head["name"],
                              "version": f"{head['version']}-{head['release']}", "size": size})
    installed = {n: installed_version(n) for n in {x["name"] for x in files}}
    for x in files:
        cur = installed.get(x["name"])
        x["installed"] = cur
        if not cur:
            x["state"] = "not-installed"
        elif x["version"] == cur:
            x["state"] = "installed"
        else:
            x["state"] = "older" if vercmp(x["version"], cur) < 0 else "newer"
    return files


def clean_downloads(max_age=3600):
    """Remove what an earlier run left in the private download folder (a closed window, a crash).
    Files younger than an hour stay: another Zypp Desk window may be downloading them."""
    d = download_dir()
    now = time.time()
    for f in d.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


def download_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "zypp-desk" / "downloads"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def place(path, source):
    """Move an accepted download into the source's folder (often a local zypper repository)."""
    folder = Path(os.path.expanduser(source["folder"]))
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / Path(path).name
    shutil.move(path, final)
    return str(final)


def signature_of_file(path, with_signer=False):
    """'verified', 'nokey:<key id>', 'bad' or 'unsigned' — from rpm -K. With with_signer, also the key id that
    signed it (when rpm verified it), so that each source can remember its key."""
    p = subprocess.run(["rpm", "-Kv", "--", str(path)], capture_output=True, text=True, env=ENV_C)
    out = p.stdout + p.stderr
    ok = re.search(r"Signature, key ID (\w+): OK", out)
    if p.returncode == 0 and ok and "BAD" not in out:
        result, signer = "verified", ok.group(1).lower()
    elif m := re.search(r"Signature, key ID (\w+): NOKEY", out):
        result, signer = "nokey:" + m.group(1), None
    elif "Signature" in out:
        result, signer = "bad", None
    else:
        result, signer = "unsigned", None
    return (result, signer) if with_signer else result
