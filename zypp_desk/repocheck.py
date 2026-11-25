# SPDX-License-Identifier: GPL-3.0-or-later
"""Is a repository still there? Quick checks done as the user, without zypper and without root.

- http(s) and ftp repositories: ask the server for the file every repository of that type has
  (repodata/repomd.xml for rpm-md, content for yast2/susetags), with a HEAD request, or a GET of the
  first bytes when the server refuses HEAD.
- dir: and plaindir repositories: the folder exists and has packages in it.
- Flatpak remotes: the remote's 'config' or 'summary' file.

Every function returns a dict: state ("ok", "fail" or "skip"), text (a few words for the list) and
detail (what was asked and what came back, for the tooltip). Nothing here changes anything.
"""

import platform
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import VERSION
from .i18n import _, ngettext

TIMEOUT = 15
USER_AGENT = f"zypp-desk/{VERSION} (+https://github.com/jordirelats/zypp-desk)"


def _result(state, text, detail=""):
    return {"state": state, "text": text, "detail": detail}


def _releasever():
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def expand(url):
    """zypper already expands $basearch and $releasever in what it lists; this is only a safety net."""
    arch = platform.machine() or "x86_64"
    for var, val in (("${basearch}", arch), ("$basearch", arch), ("${arch}", arch), ("$arch", arch),
                     ("${releasever}", _releasever()), ("$releasever", _releasever())):
        url = url.replace(var, val)
    return url


def _join(base, rel):
    """base + '/' + rel, keeping a query string (some repositories carry a token in it) at the end."""
    parts = urllib.parse.urlsplit(base)
    path = parts.path if not rel else parts.path.rstrip("/") + "/" + rel
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _http_reason(code):
    if code == 404 or code == 410:
        return _("not found ({code})").format(code=code)
    if code in (401, 403):
        return _("access denied ({code})").format(code=code)
    if code >= 500:
        return _("server error ({code})").format(code=code)
    return _("the server says {code}").format(code=code)


def probe(url):
    """One request: HEAD, or GET of the first bytes if the server does not accept HEAD."""
    headers = {"User-Agent": USER_AGENT}
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method,
                                     headers=dict(headers, Range="bytes=0-1023") if method == "GET" else headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if method == "GET":
                    r.read(1024)
                return _result("ok", _("works"), f"{url}\n→ {r.status} ({r.geturl()})")
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (400, 403, 405, 406, 501):
                continue  # some servers only refuse HEAD: try a small GET before deciding
            return _result("fail", _http_reason(e.code), f"{url}\n→ {e.code} {e.reason}")
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return _result("fail", _("no answer in {s} s").format(s=TIMEOUT), url)
            if isinstance(reason, ssl.SSLError):
                return _result("fail", _("certificate problem"), f"{url}\n→ {reason}")
            if isinstance(reason, socket.gaierror):
                return _result("fail", _("the server does not exist"), f"{url}\n→ {reason}")
            return _result("fail", _("does not answer"), f"{url}\n→ {reason}")
        except (socket.timeout, TimeoutError):
            return _result("fail", _("no answer in {s} s").format(s=TIMEOUT), url)
        except (OSError, ValueError) as e:
            return _result("fail", _("does not answer"), f"{url}\n→ {e}")
    return _result("fail", _("does not answer"), url)


def _check_folder(path, plain):
    p = Path(path)
    if not p.is_dir():
        return _result("fail", _("the folder does not exist"), str(p))
    if not plain and (p / "repodata" / "repomd.xml").is_file():
        return _result("ok", _("works"), str(p))
    n = 0
    try:
        for _f in p.rglob("*.rpm"):
            n += 1
            if n >= 9999:
                break
    except OSError as e:
        return _result("fail", _("the folder cannot be read"), f"{p}\n→ {e}")
    if not n:
        return _result("fail", _("the folder has no packages"), str(p))
    return _result("ok", ngettext("works · {n} package", "works · {n} packages", n).format(n=n), str(p))


def check_repo(repo):
    """repo: a dict from backend.repos() (url, type)."""
    url = expand((repo.get("url") or "").strip())
    if not url:
        return _result("skip", _("no address"), _("It uses a mirror list: not checked."))
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    kind = (repo.get("type") or "").lower()
    if scheme in ("dir", "file") or url.startswith("/"):
        return _check_folder(urllib.parse.unquote(parts.path if scheme else url), kind == "plaindir")
    if scheme not in ("http", "https", "ftp"):
        return _result("skip", _("not checked"), _("Addresses of the kind “{scheme}” are not checked.")
                       .format(scheme=scheme))
    if kind == "rpm-md":
        tries = ["repodata/repomd.xml"]
    elif kind in ("yast2", "susetags"):
        tries = ["content", "repodata/repomd.xml"]
    elif kind == "plaindir":
        tries = [""]
    else:  # never refreshed (type N/A): try what it would most likely be
        tries = ["repodata/repomd.xml", "content", ""]
    res = None
    for rel in tries:
        res = probe(_join(url, rel))
        if res["state"] == "ok":
            return res
    return res


def check_flatpak_remote(remote):
    """remote: a dict from flatpak.remotes() (url)."""
    url = (remote.get("url") or "").strip()
    if not url:
        return _result("skip", _("no address"), "")
    if url.startswith("oci+"):
        return _result("skip", _("not checked"), _("Addresses of the kind “{scheme}” are not checked.")
                       .format(scheme="oci"))
    if url.startswith("file://"):
        folder = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
        if (folder / "config").is_file():
            return _result("ok", _("works"), str(folder))
        return _result("fail", _("the folder does not exist") if not folder.is_dir()
                       else _("the folder has no Flatpak repository"), str(folder))
    res = probe(_join(url, "config"))
    if res["state"] == "ok":
        return res
    alt = probe(_join(url, "summary"))
    return alt if alt["state"] == "ok" else res
