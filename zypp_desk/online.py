# SPDX-License-Identifier: GPL-3.0-or-later
"""Search for software on line: the openSUSE Build Service, Fedora COPR and Flathub.

Only public services are asked, over https, with the program name typed by the user and nothing
else. Each search returns plain dicts; the window decides what to do with them.

- openSUSE Build Service (OBS): which projects publish a binary of that name built for Tumbleweed.
  The OBS search API needs an account, so the request goes through the public proxy that the
  'opi' tool of openSUSE uses: https://opi-proxy.opensuse.org/?obs_api_link=…&obs_instance=openSUSE
- Fedora COPR: the package search of the COPR web site (https://copr.fedorainfracloud.org/coprs/
  packages/search/?name=…), which finds built packages by exact name; the JSON API only searches
  project names and descriptions, slowly and loosely. The description of each project found comes
  from the JSON API (/api_3/project), which also helps to put real projects before test rebuilds.
- Flathub: https://flathub.org/api/v2/search (POST {"query": …}).

What a package of the Build Service is (its title and description) comes from the public part of the
OBS API, which needs no account: https://api.opensuse.org/public/source/<project>/<package>/_meta.
"""

import hashlib
import html
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import cmp_to_key
from pathlib import Path

from . import keys
from .backend import ENV_C, vercmp
from .i18n import _
from .websources import USER_AGENT, https_open

OBS_PROXY = "https://opi-proxy.opensuse.org/"
OBS_API = "https://api.opensuse.org/search/published/binary/id"
OBS_DOWNLOAD = "https://download.opensuse.org/repositories/"
OBS_PUBLIC = "https://api.opensuse.org/public/source/"
COPR = "https://copr.fedorainfracloud.org"
COPR_DOWNLOAD = "https://download.copr.fedorainfracloud.org/results/"
FLATHUB_SEARCH = "https://flathub.org/api/v2/search"
TIMEOUT = 40
MAX_BYTES = 8 * 1024 * 1024
MAX_KEY = 1024 * 1024
TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")

# how much to trust a result
OFFICIAL, TEAM, PERSONAL, FOREIGN, VERIFIED, UNVERIFIED = (
    "official", "team", "personal", "foreign", "verified", "unverified")

# OBS projects that only rebuild Factory for other processors, or that are work in progress
_PORTS_RE = re.compile(r":(ARM|PowerPC|zSystems|RISCV|LoongArch|Ports)\b|staging|:branches:", re.I)
_DEBUG_RE = re.compile(r"-(debuginfo|debugsource)(-|$)")


class SearchError(Exception):
    pass


def valid_term(term):
    """A package name to look for: letters, digits and . _ + - (it goes into an OBS query)."""
    return bool(TERM_RE.fullmatch(term or ""))


def package_term(term):
    """The package name to look for in the Build Service and COPR: what was typed, with spaces turned into '-'
    ('obs studio' → 'obs-studio', as package names are written), or None when it cannot be a package name."""
    t = "-".join((term or "").split())
    return t if valid_term(t) else None


def plain_description(text, limit=700):
    """A description from the network as plain text to show: the lines of a paragraph joined, paragraphs and
    list items kept, HTML tags and Markdown marks left out (a link keeps its text), at most `limit` characters."""
    text = (text or "").replace("\r", "")
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"(?i)</(p|ul|ol)>|<br\s*/?>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # a Markdown picture
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # a Markdown link: its text
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"(?m)^[ \t]*(#+|>)[ \t]*", "", text)  # a Markdown heading or quote
    text = re.sub(r"(?m)^[ \t]*[-*+][ \t]+", "• ", text)  # a Markdown list item
    paras = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = []
        for ln in (x.strip() for x in block.splitlines()):
            if not ln:
                continue
            if ln.startswith("• ") or not lines:
                lines.append(ln)
            else:
                lines[-1] += " " + ln
        if lines:
            paras.append("\n".join(re.sub(r"[ \t]+", " ", x) for x in lines))
    text = "\n\n".join(paras)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return text


def _get(url, data=None, accept=None, ctype=None, limit=MAX_BYTES):
    if not url.startswith("https://"):
        raise SearchError(_("Only https addresses are allowed."))
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with https_open(req, timeout=TIMEOUT) as r:  # refuses redirects to plain http
            body = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise SearchError(_("The service answered with an error ({code}).").format(code=e.code)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise SearchError(_("Could not reach the service: {reason}").format(reason=reason)) from e
    if len(body) > limit:
        raise SearchError(_("The answer is too big."))
    return body


# What a name from the network may look like before it goes into a command: these services are trusted
# less than the local system, and a name that looks like an option ('--eval=…') must never reach rpm or zypper.
RPM_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,199}")
OBS_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+-]{0,199}")
COPR_OWNER_RE = re.compile(r"@?[A-Za-z0-9_][A-Za-z0-9_.+-]{0,99}")
CHROOT_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")
APP_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}")


def _installed(names):
    """Which of these RPM names are installed."""
    names = sorted(n for n in set(names) if RPM_NAME_RE.fullmatch(n))
    if not names:
        return set()
    p = subprocess.run(["rpm", "-q", "--qf", "%{NAME}\n", "--", *names], capture_output=True, text=True,
                       env=ENV_C)
    return {ln.strip() for ln in p.stdout.splitlines() if ln.strip() in names}


# ------------------------------------------------------------------ openSUSE Build Service

def obs_repo_url(project, repository):
    return OBS_DOWNLOAD + project.replace(":", ":/") + "/" + repository + "/"


def obs_alias(project):
    return re.sub(r"[^A-Za-z0-9._-]", "_", project)[:60]


def _obs_repo_ok(project, repository):
    if project == "openSUSE:Factory":
        return repository in ("standard", "snapshot")
    return repository.endswith("openSUSE_Tumbleweed") or repository.endswith("openSUSE_Factory")


def _obs_repo_rank(repository):
    return {"openSUSE_Tumbleweed": 0, "openSUSE_Factory": 1}.get(repository, 2)


def obs_trust(project):
    if project == "openSUSE:Factory":
        return OFFICIAL
    return PERSONAL if project.startswith("home:") else TEAM


def search_obs(term):
    """Binaries built for Tumbleweed (x86_64 or noarch) whose name contains the term.

    One row per project and package name: dict(name, package, project, repository, version, url, trust,
    installed, exact). Exact name matches come first, then official, team and personal projects.
    """
    if not valid_term(term):
        raise SearchError(_("Type a package name: letters, digits and . _ + - only."))
    xpath = (f"contains-ic(@name, '{term}') and path/project='openSUSE:Factory' "
             "and (@arch='x86_64' or @arch='noarch') and not(contains-ic(@project, ':branches:'))")
    api = OBS_API + "?" + urllib.parse.urlencode({"match": xpath, "limit": 0})
    url = OBS_PROXY + "?" + urllib.parse.urlencode({"obs_api_link": api, "obs_instance": "openSUSE"})
    body = _get(url, accept="application/xml")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise SearchError(_("The openSUSE Build Service sent an answer that cannot be read.")) from e
    best = {}
    for b in root.iter("binary"):
        name, project, repo = b.get("name") or "", b.get("project") or "", b.get("repository") or ""
        if not name or _DEBUG_RE.search(name) or _PORTS_RE.search(project) or not _obs_repo_ok(project, repo):
            continue
        if not (RPM_NAME_RE.fullmatch(name) and OBS_NAME_RE.fullmatch(project) and OBS_NAME_RE.fullmatch(repo)):
            continue  # not a name zypper could be given safely
        version = f"{b.get('version', '')}-{b.get('release', '')}"
        key = (project, name)
        old = best.get(key)
        if old:
            better_repo = _obs_repo_rank(repo) < _obs_repo_rank(old["repository"])
            newer = vercmp(version, old["version"]) > 0
            if not (better_repo or (newer and _obs_repo_rank(repo) == _obs_repo_rank(old["repository"]))):
                continue
        best[key] = {"name": name, "package": b.get("package") or name, "project": project,
                     "repository": repo, "version": version,
                     "url": obs_repo_url(project, repo), "trust": obs_trust(project),
                     "exact": name.lower() == term.lower()}
    rows = list(best.values())
    inst = _installed(r["name"] for r in rows)
    order = {OFFICIAL: 0, TEAM: 1, PERSONAL: 2}
    low = term.lower()
    for r in rows:
        r["installed"] = r["name"] in inst
    # exact name first, then official, team and personal projects, the newest version first in each
    rows.sort(key=cmp_to_key(lambda a, b: vercmp(b["version"], a["version"])))
    rows.sort(key=lambda r: (0 if r["exact"] else 1 if r["name"].lower().startswith(low) else 2,
                             r["name"].lower(), order[r["trust"]]))
    return rows[:200]


def obs_key_url(row):
    return row["url"] + "repodata/repomd.xml.key"


def obs_description(project, package):
    """What a package of the Build Service is: dict(title, description, url) from its public _meta (no account
    needed), or None when it cannot be asked for."""
    package = package.split(":", 1)[0]  # a multibuild flavour ('pkg:flavour') has the sources of 'pkg'
    if not (OBS_NAME_RE.fullmatch(project or "") and RPM_NAME_RE.fullmatch(package or "")):
        return None
    url = OBS_PUBLIC + urllib.parse.quote(project, safe=":") + "/" + urllib.parse.quote(package, safe="") + "/_meta"
    body = _get(url, accept="application/xml", limit=256 * 1024)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    home = (root.findtext("url") or "").strip()
    return {"title": " ".join((root.findtext("title") or "").split())[:200],
            "description": plain_description(root.findtext("description")),
            "url": home if home.startswith(("https://", "http://")) else ""}


def obs_descriptions(keys):
    """{(project, package): obs_description(…) or None} for several packages, four at a time."""
    from concurrent.futures import ThreadPoolExecutor

    def one(key):
        try:
            return key, obs_description(*key)
        except SearchError:
            return key, None

    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, keys))


# ------------------------------------------------------------------ Fedora COPR

class _CoprTable(object):
    """Rows of the result table of the COPR package search page (Project, Build, Chroot, Name, Epoch,
    Version, Release, Arch)."""

    CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
    ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
    TAG_RE = re.compile(r"<[^>]+>")

    @classmethod
    def parse(cls, text):
        out = []
        for row in cls.ROW_RE.findall(text):
            cells = [html.unescape(cls.TAG_RE.sub("", c)).strip() for c in cls.CELL_RE.findall(row)]
            if len(cells) == 8:
                out.append(dict(zip(("project", "build", "chroot", "name", "epoch", "version", "release", "arch"),
                                    cells)))
        return out


def _chroot_rank(chroot):
    """Best chroot for openSUSE first: a Tumbleweed one, then the newest Fedora release, rawhide last."""
    if chroot.startswith("opensuse-tumbleweed"):
        return (0, 0)
    m = re.match(r"fedora-(\d+)-", chroot)
    if m:
        return (1, -int(m.group(1)))
    if chroot.startswith("fedora-rawhide"):
        return (2, 0)
    return (3, 0)


COPR_PAGES = 10  # the search lists every build, ten per page: the first pages are enough to find the projects


def _copr_page(term, arch, n):
    path = "/coprs/packages/search/" + (f"{n}/" if n > 1 else "")
    body = _get(COPR + path + "?" + urllib.parse.urlencode({"name": term, "arch": arch}), accept="text/html")
    text = body.decode("utf-8", "replace")
    rows = _CoprTable.parse(text)
    if not rows and "<th>Chroot</th>" in text and "<td>" in text:
        # a result table is there but its rows no longer have the eight columns this reads
        raise SearchError(_("The COPR web site changed: its results cannot be read."))
    pages = [int(x) for x in re.findall(r"/coprs/packages/search/(\d+)/\?", text)]
    return rows, max(pages or [1])


def _copr_rows(term, arch):
    from concurrent.futures import ThreadPoolExecutor
    rows, last = _copr_page(term, arch, 1)
    more = range(2, min(last, COPR_PAGES) + 1)

    def page(n):
        try:
            return _copr_page(term, arch, n)[0]
        except SearchError:
            return []  # a later page that fails (the site is busy): the projects of the other pages still count

    if more:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for r in ex.map(page, more):
                rows += r
    return rows, last > COPR_PAGES


def _copr_any(term):
    rows, _more = _copr_rows(term, "x86_64")
    if not rows:
        rows, _more = _copr_rows(term, "noarch")
    return rows


COPR_INFO_MAX = 40  # projects whose description is asked for (six at a time); the rest keep None


def copr_project_info(owner, projname):
    """dict(description, homepage) of a COPR project, from its JSON API (no account needed)."""
    query = urllib.parse.urlencode({"ownername": owner, "projectname": projname})
    body = _get(COPR + "/api_3/project?" + query, accept="application/json", limit=1024 * 1024)
    try:
        d = json.loads(body)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    home = d.get("homepage") if isinstance(d.get("homepage"), str) else ""
    return {"description": plain_description(d.get("description") if isinstance(d.get("description"), str) else ""),
            "homepage": home if home.startswith(("https://", "http://")) else ""}


def _copr_infos(pairs):
    from concurrent.futures import ThreadPoolExecutor

    def one(pair):
        try:
            return pair, copr_project_info(*pair)
        except SearchError:
            return pair, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        return {k: v for k, v in ex.map(one, pairs) if v is not None}


def search_copr(term):
    """COPR projects that built a package with exactly this name for x86_64 (or noarch).

    One row per project: dict(name, project, owner, projname, chroot, chroots, version, url, key_url,
    trust, tumbleweed, installed, description, homepage, fedora, stale). Only Fedora and openSUSE chroots
    count; fedora is the Fedora release of the chroot chosen (0 for rawhide and Tumbleweed), and stale says
    that Fedora no longer keeps that release while other projects build for newer ones. COPR
    matches names exactly, small and capital letters too: 'Zed' finds nothing, so it is asked again as 'zed'.
    description is None when it could not be asked for, "" when the project has none (often a test
    build or a rebuild of Fedora's own packages). Projects built for Tumbleweed come first, stale ones
    last, and in between the ones named after the package, then the ones with a description.
    """
    if not valid_term(term):
        raise SearchError(_("Type a package name: letters, digits and . _ + - only."))
    rows = _copr_any(term)
    if not rows and term != term.lower():
        term = term.lower()  # almost every package name is written in small letters
        rows = _copr_any(term)
    by_project = {}
    for r in rows:
        if (r["arch"] not in ("x86_64", "noarch") or "x86_64" not in r["chroot"] or "/" not in r["project"]
                or not r["chroot"].startswith(("fedora-", "opensuse-tumbleweed"))):
            continue
        owner_, _sep, proj_ = r["project"].partition("/")
        if not (RPM_NAME_RE.fullmatch(r["name"]) and COPR_OWNER_RE.fullmatch(owner_) and RPM_NAME_RE.fullmatch(proj_)
                and CHROOT_RE.fullmatch(r["chroot"])):
            continue  # not a name zypper could be given safely
        p = by_project.setdefault(r["project"], {"chroots": {}})
        version = f"{r['version']}-{r['release']}"
        old = p["chroots"].get(r["chroot"])
        if not old or vercmp(version, old) > 0:
            p["chroots"][r["chroot"]] = version
        p["name"] = r["name"]
    out = []
    inst = _installed([term])
    for project, p in by_project.items():
        chroot = sorted(p["chroots"], key=_chroot_rank)[0]
        owner, projname = project.split("/", 1)
        base = COPR_DOWNLOAD + f"{owner}/{projname}/"
        out.append({"name": p["name"], "project": project, "owner": owner, "projname": projname,
                    "chroot": chroot, "chroots": sorted(p["chroots"], key=_chroot_rank),
                    "version": p["chroots"][chroot], "url": base + chroot + "/", "key_url": base + "pubkey.gpg",
                    "page": f"{COPR}/coprs/{'g/' + owner[1:] if owner.startswith('@') else owner}/{projname}/",
                    "trust": FOREIGN, "tumbleweed": chroot.startswith("opensuse-tumbleweed"),
                    "installed": p["name"] in inst, "description": None, "homepage": "",
                    "fedora": _fedora_release(chroot), "stale": False})
    # Fedora keeps its last two releases: a project whose newest build is older was left behind
    newest = max((r["fedora"] for r in out if r["fedora"]), default=0)
    for r in out:
        r["stale"] = bool(r["fedora"]) and r["fedora"] < newest - 2
    low = term.lower()

    def rank(r):
        return (not r["tumbleweed"], r["stale"], low not in r["projname"].lower(), not r["description"],
                _chroot_rank(r["chroot"]), r["project"].lower())

    out.sort(key=rank)
    infos = _copr_infos([(r["owner"], r["projname"]) for r in out[:COPR_INFO_MAX]])
    for r in out:
        info = infos.get((r["owner"], r["projname"]))
        if info:
            r["description"], r["homepage"] = info["description"], info["homepage"]
    out.sort(key=rank)
    return out


def _fedora_release(chroot):
    """fedora-43-x86_64 → 43; 0 for rawhide and other systems."""
    m = re.match(r"fedora-(\d+)-", chroot)
    return int(m.group(1)) if m else 0


def copr_alias(row):
    return "copr-" + re.sub(r"[^A-Za-z0-9._-]", "_", f"{row['owner'].lstrip('@')}-{row['projname']}")[:55]


# ------------------------------------------------------------------ Flathub

def search_flathub(term, installed_ids=()):
    """Apps on Flathub: dict(app_id, name, summary, description, developer, icon_url, verified, installs,
    installed)."""
    term = (term or "").strip()
    if not term or len(term) > 100:
        raise SearchError(_("Type the name of a program."))
    body = _get(FLATHUB_SEARCH, data=json.dumps({"query": term}).encode(), accept="application/json",
                ctype="application/json")
    try:
        hits = json.loads(body).get("hits", [])
    except (ValueError, AttributeError) as e:
        raise SearchError(_("Flathub sent an answer that cannot be read.")) from e
    out = []
    for h in hits:
        app = h.get("app_id") or ""
        if not APP_ID_RE.fullmatch(app) or h.get("type") not in (None, "desktop-application", "console-application"):
            continue
        desc = h.get("description")
        out.append({"app_id": app, "name": h.get("name") or app, "summary": h.get("summary") or "",
                    "description": plain_description(desc if isinstance(desc, str) else ""),
                    "developer": h.get("developer_name") or "", "icon_url": h.get("icon") or "",
                    "verified": bool(h.get("verification_verified")),
                    "installs": int(h.get("installs_last_month") or 0), "installed": app in set(installed_ids),
                    "remote": "flathub", "trust": VERIFIED if h.get("verification_verified") else UNVERIFIED})
    return out


# ------------------------------------------------------------------ files kept on disk

def _cache_dir(sub):
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "zypp-desk" / sub
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def icon_file(url):
    """Download a small icon (Flathub) once; later calls take it from ~/.cache/zypp-desk/icons."""
    if not url.startswith("https://"):
        return None
    name = hashlib.sha1(url.encode()).hexdigest() + (".svg" if url.endswith(".svg") else ".png")
    path = _cache_dir("icons") / name
    if path.is_file() and path.stat().st_size > 0:
        return str(path)
    data = _get(url, limit=512 * 1024)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return str(path)


def fetch_key(url):
    """Download a repository signing key and read it (nothing is imported here, and gpg never touches ~/.gnupg:
    keys.inspect_key and keys.save_for_import use a throwaway gpg home).

    The same rules as the Signing keys page: a file that rpm and gpg could read differently is refused, it must hold
    exactly one key, and the file kept for the queue (~/.cache/zypp-desk/keys/<FPR>.asc) is gpg's own copy of that
    key, which the helper checks again as root before rpm imports it.

    Returns dict(path, fingerprint, uids, name, expired, revoked).
    """
    data = _get(url, limit=MAX_KEY)
    try:
        found = keys.inspect_key(data)
        if len(found) != 1:
            raise SearchError(_("The repository does not publish a single readable signing key."))
        k = found[0]
        path = keys.save_for_import(data, k["fpr"])
    except ValueError as e:
        raise SearchError(str(e)) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise SearchError(_("The key could not be read: {error}").format(error=e)) from e
    return {"path": path, "fingerprint": k["fpr"], "uids": k["uids"], "name": k["name"] or k["email"],
            "expired": k["expired"], "revoked": k["revoked"]}
