"""Twitter/X media lister — web UI on top of gallery-dl.

Paste a username or profile URL, scan the account's media timeline,
and get images and videos listed separately, ready to download.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, Response, stream_with_context

import publish
import store
import xcookies
from admin import bp as admin_bp
from fetch import MEDIA_HEADERS, is_allowed_media_url

# Local .env (gitignored) — supplies MYAIMODELMANAGER_API_KEY when the user
# hasn't typed a key into the admin page. Loaded before anything reads it.
publish.load_env()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB uploads (videos)
app.register_blueprint(admin_bp)
store.init_db()

VIDEO_EXTS = {"mp4", "m4v", "mov"}
MAX_SCAN_LIMIT = 1000

# --- public site / branding -------------------------------------------
# SITE_URL is the origin this app is served from once it is online; it is
# what canonical + Open Graph URLs are built from. Left unset (local dev)
# we fall back to whatever host the request came in on.
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

BRAND_NAME = "MyAIModelManager"
BRAND_URL = os.environ.get("BRAND_URL", "https://myaimodelmanager.com").rstrip("/")
BRAND_TAGLINE = (
    "MyAIModelManager is an AI character platform — upload characters and "
    "their media, then talk to them."
)


@app.context_processor
def inject_brand():
    return {
        "site_url": SITE_URL or request.url_root.rstrip("/"),
        "brand_name": BRAND_NAME,
        "brand_url": BRAND_URL,
        "brand_tagline": BRAND_TAGLINE,
    }


@app.context_processor
def inject_static_v():
    """Append the file's mtime to static URLs so edits ship immediately
    instead of living behind the browser's cached copy."""
    def static_v(filename):
        path = os.path.join(app.root_path, "static", filename)
        try:
            v = int(os.stat(path).st_mtime)
        except OSError:
            v = 0
        return f"{url_for('static', filename=filename)}?v={v}"
    return {"static_v": static_v}


@app.get("/robots.txt")
def robots_txt():
    """Let crawlers have the landing page; keep the private dashboards out."""
    root = SITE_URL or request.url_root.rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /$\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /download\n"
        "Disallow: /zip\n"
        "Disallow: /media/\n"
        f"\nSitemap: {root}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    root = SITE_URL or request.url_root.rstrip("/")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{root}/</loc><changefreq>weekly</changefreq>"
        "<priority>1.0</priority></url>\n"
        "</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


def parse_username(text):
    """Accept '@name', 'name', or a twitter.com/x.com profile URL."""
    text = text.strip()
    if not text:
        return None
    if "://" in text or text.startswith("www."):
        host = urlparse(text if "://" in text else "https://" + text)
        parts = [p for p in host.path.split("/") if p]
        if not parts:
            return None
        name = parts[0]
    else:
        name = text.lstrip("@").split("/")[0]
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,20}", name):
        return None
    return name


def gallery_dl_version():
    try:
        return metadata.version("gallery-dl")
    except metadata.PackageNotFoundError:
        return ""


# Everything gallery-dl can shout at us, mapped to something a user can act
# on. First match wins, so anything ambiguous sits below the diagnosis it
# would otherwise steal — a proxy's own "403 Forbidden" is not X refusing a
# login, and a protected account is not a missing one.
SCAN_ERRORS = [
    (r"proxyerror|tunnel connection|max retries|connection (refused|reset|error)"
     r"|name resolution|failed to resolve|temporary failure",
     "network",
     "This server could not reach X at all. That is a connection problem on "
     "the machine running the scan, not your cookies."),
    (r"suspended",
     "suspended",
     "That account is suspended, so X serves nothing for it."),
    (r"protected|private profile|not authorized to view",
     "protected",
     "That account is protected. Only cookies from an account it has "
     "approved as a follower can see its media."),
    (r"\b404\b|notfound|not found|does not exist|no user matches",
     "no_account",
     "No such account. Check the handle — X hands names back out after a "
     "rename, and a deleted account looks the same as a typo."),
    (r"\b401\b|unauthorized|could not authenticate",
     "auth",
     "X rejected the login cookies (401). They have expired — copy a fresh "
     "auth_token and ct0 from x.com and paste them in again."),
    (r"\b429\b|rate.?limit|too many requests",
     "rate_limited",
     "X is rate-limiting this session (429). Wait about 15 minutes, or scan "
     "with a lower limit."),
    (r"login required|authorization.?error|requires? (a )?log",
     "auth",
     "gallery-dl says this timeline needs a login. Paste your cookies under "
     "'Login cookies' — auth_token and ct0 at minimum."),
    (r"\b403\b|forbidden",
     "auth",
     "X refused the request (403). Usually a ct0 that no longer matches "
     "auth_token, or an account X has locked. 'Check cookies' says which."),
    (r"timed out|timeout",
     "network",
     "The connection to X timed out part-way through. Try again, or scan "
     "with a lower limit."),
    (r"unsupported url|no suitable extractor",
     "internal",
     "gallery-dl did not recognise the timeline URL."),
    (r"keyerror|traceback|unable to|unexpected|nonetype|json ?decode",
     "outdated",
     "gallery-dl could not read X's response, which is what happens when X "
     "changes its API and the installed gallery-dl predates the change. "
     "Updating it (pip install -U gallery-dl) usually fixes this."),
]


def run_gallery_dl(cookie_path, username, span, previews=True, path="/media",
                   timeout=600):
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--dump-json",
        "--range", span,
        "--retries", "3",
        "-o", f"previews={'true' if previews else 'false'}",
        # Reposts are why we walk two timelines: X's UserMedia endpoint
        # serves an account's own media only — the "from @handle" rows the
        # media tab shows come from the posts timeline instead. "original"
        # rather than true because gallery-dl's true mode transforms the
        # retweet wrapper, whose legacy has no id_str, and dies with a
        # KeyError on the first repost; original mode swaps in the original
        # tweet and survives. The app's URL dedup folds repeats.
        "-o", "retweets=original",
        "--cookies", cookie_path,
        f"https://twitter.com/{username}{path}",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def read_entries(stdout):
    """gallery-dl's --dump-json output, or (None) when it isn't JSON."""
    if not (stdout or "").strip():
        return []
    try:
        entries = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return entries if isinstance(entries, list) else None


def scan_errors(entries):
    """Pull gallery-dl's own error records out of its --dump-json output.

    They arrive as [-1, {"error": …, "message": …}] entries mixed in with the
    files, not on stderr — which is why a failed scan used to come back
    looking like an account with nothing on it.
    """
    out = []
    for entry in entries:
        if not (isinstance(entry, list) and entry and entry[0] == -1):
            continue
        payload = entry[-1] if isinstance(entry[-1], dict) else {}
        text = " ".join(
            str(payload.get(k, "")).strip() for k in ("error", "message")
        ).strip()
        if text:
            out.append(text)
    return out


def diagnose_scan(text, returncode):
    """Turn what gallery-dl reported into one sentence worth showing a user."""
    text = (text or "").lower()
    for pattern, code, message in SCAN_ERRORS:
        if re.search(pattern, text):
            return code, message
    if returncode:
        return "failed", f"gallery-dl exited with status {returncode}."
    return "", ""


def classify_entry(entry):
    """gallery-dl --dump-json yields [3, url, metadata] per file."""
    if not (isinstance(entry, list) and len(entry) == 3 and entry[0] == 3):
        return None
    url, meta = entry[1], entry[2] or {}
    ext = (meta.get("extension") or "").lower()
    if not ext or not isinstance(url, str):
        return None
    kind = "video" if ext in VIDEO_EXTS else "image"
    # gallery-dl emits a still frame right after each video (previews=true);
    # it is a poster for that video, not a gallery item of its own.
    if (meta.get("type") or "").lower() == "preview":
        kind = "preview"
    author = meta.get("author") or meta.get("user") or {}
    nick = author.get("nick") or author.get("name") or ""
    tweet_id = meta.get("tweet_id") or meta.get("id")
    # A repost carries the original tweet's id in retweet_id and its author in
    # `author`; gallery-dl also prefixes the text "RT @handle: ", which the
    # reposted_from badge says better, so the prefix goes.
    reposted_from = ""
    text = (meta.get("content") or "").strip()
    if meta.get("retweet_id"):
        prefix = re.match(r"^RT @(\w+): ?", text)
        # `author` is the original author on a repost; when no author object
        # came through, the prefix gallery-dl added to the caption still is.
        reposted_from = author.get("name") or (prefix.group(1) if prefix else "")
        text = re.sub(r"^RT @\w+: ?", "", text, count=1)
    elif author.get("name") and (meta.get("user") or {}).get("name"):
        # The posts-timeline pass: X can deliver a repost as the original
        # tweet outright, with no retweet marker anywhere — the tell is an
        # author who is not the account being scanned (`user` is that one).
        if author["name"].lower() != meta["user"]["name"].lower():
            reposted_from = author["name"]
    filename = meta.get("filename") or f"media_{tweet_id or 'unknown'}"
    filename = re.sub(r"[^\w.\-]", "_", f"{filename}.{ext}")
    date = meta.get("date") or ""
    return {
        "kind": kind,
        "url": url,
        "filename": filename,
        "date": str(date)[:10],
        "text": text,
        "user": nick,
        "reposted_from": reposted_from,
        "tweet_url": f"https://x.com/i/status/{tweet_id}" if tweet_id else "",
        "tweet_id": str(tweet_id or ""),
        "poster": "",
        "duration": _as_seconds(meta.get("duration")),
    }


def _as_seconds(value):
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return 0


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/scan")
def scan():
    data = request.get_json(force=True, silent=True) or {}
    username = parse_username(data.get("username") or "")
    if not username:
        return jsonify({"error": "Invalid username or URL."}), 400
    try:
        limit = max(1, min(int(data.get("limit") or 200), MAX_SCAN_LIMIT))
    except (TypeError, ValueError):
        limit = 200

    raw_cookies = data.get("cookies") or ""
    pairs = xcookies.parse(raw_cookies)
    # Bad cookies are the failure mode here, and a scan takes a minute to
    # discover what one regex can say up front — so say it up front.
    problem = xcookies.problems(pairs, bool(raw_cookies.strip()))
    if problem:
        return jsonify(problem), 400

    cookie_path = xcookies.write_file(pairs)
    # Previews are extra files, so ask for headroom above the user's limit and
    # trim back to it once the still frames have been folded into their videos.
    span = f"1-{min(limit * 2, MAX_SCAN_LIMIT * 2)}"
    try:
        proc = run_gallery_dl(cookie_path, username, span)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Scan timed out. Try a lower limit."}), 504

    # Second pass down the posts timeline, the only one that carries the
    # account's reposts. Best effort: a slow or failing pass must never cost
    # the media-tab results, it just leaves the reposts out.
    posts = None
    try:
        posts = run_gallery_dl(cookie_path, username, span, path="/tweets",
                               timeout=300)
    except subprocess.TimeoutExpired:
        posts = None
    finally:
        Path(cookie_path).unlink(missing_ok=True)

    posts_entries = []
    if posts is not None:
        posts_entries = read_entries(posts.stdout) or []

    entries = read_entries(proc.stdout)
    unreadable = entries is None
    entries = (entries or []) + posts_entries
    reported = scan_errors(entries)

    # gallery-dl's complaints are the only witness to a scan that dies part
    # way through — its stderr never reaches the browser when some items
    # made it out, so keep a tail in the server log for exactly that case.
    _items, _seen = [], set()
    for _e in entries:
        _i = classify_entry(_e)
        if _i and _i["url"] not in _seen:
            _seen.add(_i["url"])
            _items.append(_i)
    _kinds = {}
    for _i in _items:
        _kinds[_i["kind"]] = _kinds.get(_i["kind"], 0) + 1
    _reposts = sum(1 for _i in _items if _i["reposted_from"])
    _bylines = {}
    for _i in _items:
        _bylines[_i["user"]] = _bylines.get(_i["user"], 0) + 1
    _top = sorted(_bylines.items(), key=lambda kv: -kv[1])[:5]
    print(
        f"[scan] @{username} rc={proc.returncode} entries={len(entries)} "
        f"posts_entries={len(posts_entries)} "
        f"posts_rc={getattr(posts, 'returncode', None)} "
        f"posts_err={' | '.join((getattr(posts, 'stderr', '') or '').strip().splitlines()[-3:])} "
        f"items={len(_items)} kinds={_kinds} reposts={_reposts} "
        f"bylines={_top} "
        f"stderr_tail={' | '.join((proc.stderr or '').strip().splitlines()[-6:])}",
        file=sys.stderr,
    )

    items, seen = [], set()
    for entry in entries:
        item = classify_entry(entry)
        if item and item["url"] not in seen:
            seen.add(item["url"])
            items.append(item)

    # Each tweet's previews arrive in the same order as its videos.
    previews = {}
    for item in items:
        if item["kind"] == "preview":
            previews.setdefault(item["tweet_id"], []).append(item["url"])
    for item in items:
        if item["kind"] == "video":
            queue = previews.get(item["tweet_id"])
            if queue:
                item["poster"] = queue.pop(0)
    # The media pass fills the list on its own, so interleave the posts pass's
    # reposts before the limit is applied — tweet ids are snowflakes, which
    # makes numeric order chronological — or they could never make the cut.
    items = [i for i in items if i["kind"] != "preview"]
    items.sort(key=lambda i: int(i["tweet_id"] or 0), reverse=True)
    items = items[:limit]

    if not items:
        noise = reported + (proc.stderr or "").strip().splitlines()[-4:]
        code, hint = diagnose_scan(
            "\n".join(reported) + "\n" + (proc.stderr or ""), proc.returncode
        )
        if not hint and unreadable:
            code, hint = "internal", (
                "gallery-dl returned something this app could not read. Its "
                "output format may have changed — updating it "
                "(pip install -U gallery-dl) is the usual fix."
            )
        if not hint and entries:
            code, hint = "empty", (
                f"@{username} has no images or videos on its media tab — "
                "the scan reached the account and it is empty."
            )
        if not hint:
            code, hint = "auth", (
                f"Nothing came back for @{username}. X serves an empty "
                "timeline rather than an error when a session is stale, so "
                "check the cookies first — then confirm the handle is right."
            )
        return jsonify({
            "images": [], "videos": [],
            "warning": hint,
            "code": code,
            "detail": " | ".join(n for n in noise if n.strip()),
        })

    images = [i for i in items if i["kind"] == "image"]
    videos = [i for i in items if i["kind"] == "video"]
    return jsonify({
        "images": images,
        "videos": videos,
        "username": username,
        "note": f"Scanned @{username} (up to {limit} media items).",
    })


def probe_timeline(pairs, username):
    """Prove the cookies by pulling one item down the same path a scan uses.

    The authority on whether a login works is the thing that uses it. When
    X's own API won't answer, this asks gallery-dl for a single item and
    reports what came back, so the check can never contradict a scan.
    """
    cookie_path = xcookies.write_file(pairs)
    try:
        proc = run_gallery_dl(cookie_path, username, "1-1", previews=False, timeout=180)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "code": "inconclusive",
            "message": "The test scan took too long to answer. Try a real scan.",
        }
    finally:
        Path(cookie_path).unlink(missing_ok=True)

    entries = read_entries(proc.stdout)
    reported = scan_errors(entries or [])
    if entries and any(classify_entry(e) for e in entries):
        return {
            "ok": True,
            "code": "ok",
            "via": "scan",
            "message": (
                f"These cookies work — a test scan of @{username} pulled media "
                "back with them. (X's own status endpoint would not answer, so "
                "this went down the same path a real scan does.)"
            ),
        }
    code, hint = diagnose_scan(
        "\n".join(reported) + "\n" + (proc.stderr or ""), proc.returncode
    )
    return {
        "ok": False,
        "code": code or "inconclusive",
        "via": "scan",
        "message": hint or (
            f"A test scan of @{username} came back empty, which does not by "
            "itself mean the cookies are bad — that account may simply have "
            "no media."
        ),
        "detail": " | ".join(reported)[:400],
    }


@app.post("/api/cookie-check")
def cookie_check():
    """Say whether these cookies are a live X session, before a scan is spent.

    A scan that comes back empty cannot tell an expired login apart from a
    locked account or a handle that no longer exists. One call to X can —
    and when X declines to answer, a one-item scan can.
    """
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("cookies") or ""
    pairs = xcookies.parse(raw)
    names = sorted(pairs)

    problem = xcookies.problems(pairs, bool(raw.strip()))
    if problem:
        return jsonify({
            "ok": False,
            "code": problem["code"],
            "message": problem["error"],
            "names": names,
        })

    result = xcookies.check_session(pairs)
    username = parse_username(data.get("username") or "")
    if result.get("code") == "inconclusive" and username:
        api_detail = result.get("detail", "")
        result = probe_timeline(pairs, username)
        # Keep why the direct check bowed out; it is the useful half of a
        # report that something upstream, not the user, is the problem.
        result["detail"] = " | ".join(
            d for d in (result.get("detail"), api_detail) if d
        )
    return jsonify({
        **result,
        "names": names,
        "warnings": xcookies.shape_warnings(pairs),
        "gallery_dl": gallery_dl_version(),
    })


def _check_media_url(url):
    return is_allowed_media_url(url)


# --- browser extension handoff ----------------------------------------
# The Chrome extension posts the profile being viewed plus the signed-in
# session's cookies, and the dashboard tab picks the pair up and scans with
# it — no DevTools, no pasting. Like everywhere else in this app, cookies
# never reach disk or logs: the handoff lives in memory only, is handed out
# once, and expires on its own. That store is per-process, so the Procfile
# runs a single gunicorn worker (threads carry the concurrency) — with more
# than one, the extension's POST and the dashboard's GET can land on
# different workers and the handoff is lost.
_HANDOFF_TTL = 15 * 60
_handoff = {}

# Whether an extension lives in this browser at all: both its liveness ping
# and a handoff prove one is installed, and the dashboard uses that to decide
# whether "fill my cookies" should say "click the sidebar item" or walk the
# user through installing it.
_EXTENSION_SEEN_TTL = 24 * 3600
_extension = {"last_seen": 0.0}


def _note_extension():
    _extension["last_seen"] = time.time()


def extension_seen():
    return time.time() - _extension["last_seen"] <= _EXTENSION_SEEN_TTL


@app.get("/api/ping")
def ping():
    """Liveness probe the extension uses to show app status on x.com."""
    _note_extension()
    return jsonify({"ok": True, "app": "twitter-media-app"})


@app.post("/api/extension/handoff")
def extension_handoff():
    data = request.get_json(force=True, silent=True) or {}
    username = parse_username(data.get("username") or "")
    if not username:
        return jsonify({
            "error": "No usable profile handle in the request.",
        }), 400

    raw_cookies = data.get("cookies") or ""
    pairs = xcookies.parse(raw_cookies)
    problem = xcookies.problems(pairs, bool(raw_cookies.strip()))
    if problem:
        return jsonify(problem), 400

    _note_extension()
    _handoff.clear()
    _handoff.update(
        username=username,
        cookies=raw_cookies,
        at=time.time(),
    )
    return jsonify({"ok": True, "username": username})


@app.get("/api/extension/handoff")
def extension_handoff_take():
    """One-shot: the dashboard reads a pending handoff, which consumes it."""
    if _handoff and time.time() - _handoff["at"] <= _HANDOFF_TTL:
        payload = dict(_handoff)
        payload["extension_seen"] = extension_seen()
        _handoff.clear()
        return jsonify({"pending": True, **payload})
    _handoff.clear()
    return jsonify({"pending": False, "extension_seen": extension_seen()})


# twimg often answers with a generic octet-stream, which stops <video> from
# even trying; fall back to the extension so the browser knows what it got.
CONTENT_TYPES = {
    "mp4": "video/mp4",
    "m4v": "video/x-m4v",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "m3u8": "application/vnd.apple.mpegurl",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}

VAGUE_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def _content_type(url, upstream_type):
    declared = (upstream_type or "").split(";")[0].strip().lower()
    if declared not in VAGUE_TYPES:
        return declared
    ext = Path(urlparse(url).path).suffix.lstrip(".").lower()
    return CONTENT_TYPES.get(ext, declared or "application/octet-stream")


@app.route("/api/stream", methods=["GET", "HEAD"])
def stream_media():
    """Same-origin, range-aware proxy so <video> can preview a remote file.

    Pointing a <video> straight at video.twimg.com fails often enough — the CDN
    turns away hits that do not look like they came from a tweet — that the
    preview flashes and dies. Going through the server means the fetch carries
    the headers twimg expects, and passing Range through in both directions
    keeps seeking (and Safari, which refuses to play without it) working.
    """
    url = request.args.get("url", "")
    if not _check_media_url(url):
        return jsonify({"error": "URL not allowed"}), 400

    headers = dict(MEDIA_HEADERS)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:
        upstream = requests.get(url, headers=headers, stream=True, timeout=60)
        upstream.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({"error": f"Fetch failed: {exc}"}), 502

    out = {
        "Content-Type": _content_type(url, upstream.headers.get("Content-Type")),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": "inline",
    }
    for header in ("Content-Length", "Content-Range"):
        if upstream.headers.get(header):
            out[header] = upstream.headers[header]

    # 206 when twimg honoured the Range, 200 when it sent the whole file.
    status = upstream.status_code
    if request.method == "HEAD":
        upstream.close()
        return Response(status=status, headers=out)

    def gen():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(gen()), status=status, headers=out)


@app.get("/api/download")
def download():
    url = request.args.get("url", "")
    name = re.sub(r"[^\w.\-]", "_", request.args.get("name", "media"))
    if not _check_media_url(url):
        return jsonify({"error": "URL not allowed"}), 400
    try:
        r = requests.get(url, headers=MEDIA_HEADERS, stream=True, timeout=60)
        r.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({"error": f"Fetch failed: {exc}"}), 502

    def gen():
        try:
            for chunk in r.iter_content(64 * 1024):
                yield chunk
        finally:
            r.close()

    return Response(
        stream_with_context(gen()),
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
        },
    )


@app.post("/api/zip")
def zip_all():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    label = re.sub(r"[^\w.\-]", "_", str(data.get("label") or "media"))
    if not items or len(items) > 200:
        return jsonify({"error": "Need 1-200 items."}), 400

    spooled = tempfile.SpooledTemporaryFile(max_size=128 * 1024 * 1024)
    with zipfile.ZipFile(spooled, "w", zipfile.ZIP_DEFLATED) as zf:
        used = set()
        for it in items:
            url = it.get("url", "")
            if not _check_media_url(url):
                continue
            name = re.sub(r"[^\w.\-]", "_", it.get("name") or "media")
            while name in used:
                name = "_" + name
            used.add(name)
            try:
                r = requests.get(url, headers=MEDIA_HEADERS, timeout=60)
                r.raise_for_status()
                zf.writestr(name, r.content)
            except requests.RequestException:
                continue
    spooled.seek(0)

    def gen():
        try:
            while True:
                chunk = spooled.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            spooled.close()

    return Response(
        stream_with_context(gen()),
        headers={
            "Content-Disposition": f'attachment; filename="{label}.zip"',
            "Content-Type": "application/zip",
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, threaded=True)
