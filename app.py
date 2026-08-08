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
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, Response, stream_with_context

import publish
import store
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


def build_cookie_file(raw):
    """Turn user input into a Netscape cookie file.

    Accepts a full Netscape cookies.txt export, a JSON cookie export
    (array of {name, value, domain, path, secure, expirationDate, ...}
    objects, as produced by browser extensions like "Get cookies.txt
    LOCALLY" or EditThisCookie), or a header-style string like
    'auth_token=...; ct0=...'.
    """
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{") or raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list) and parsed and all(
            isinstance(c, dict) and c.get("name") and "value" in c
            for c in parsed
        ):
            lines = ["# Netscape HTTP Cookie File"]
            for c in parsed:
                domain = c.get("domain") or ".x.com"
                include_subdomains = "FALSE" if c.get("hostOnly") else "TRUE"
                path = c.get("path") or "/"
                secure = "TRUE" if c.get("secure") else "FALSE"
                try:
                    expires = int(float(c.get("expirationDate") or 0))
                except (TypeError, ValueError):
                    expires = 0
                if c.get("session") or expires <= 0:
                    expires = 2000000000
                lines.append(
                    f"{domain}\t{include_subdomains}\t{path}\t{secure}"
                    f"\t{expires}\t{c['name']}\t{c['value']}"
                )
            content = "\n".join(lines) + "\n"
            return _write_cookie_file(content)
        # fall through: treat as header-style string
    if "\t" in raw and ("twitter.com" in raw or "x.com" in raw):
        content = raw
    else:
        pairs = {}
        for chunk in raw.replace("\n", ";").split(";"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                pairs[k.strip()] = v.strip()
        if not pairs:
            return None
        lines = ["# Netscape HTTP Cookie File"]
        for domain in (".twitter.com", ".x.com"):
            for k, v in pairs.items():
                lines.append(f"{domain}\tTRUE\t/\tTRUE\t2000000000\t{k}\t{v}")
        content = "\n".join(lines) + "\n"
    return _write_cookie_file(content)


def _write_cookie_file(content):
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="twcookies_", delete=False
    )
    tmp.write(content)
    tmp.close()
    return tmp.name


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
    filename = meta.get("filename") or f"media_{tweet_id or 'unknown'}"
    filename = re.sub(r"[^\w.\-]", "_", f"{filename}.{ext}")
    date = meta.get("date") or ""
    return {
        "kind": kind,
        "url": url,
        "filename": filename,
        "date": str(date)[:10],
        "text": (meta.get("content") or "").strip(),
        "user": nick,
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

    cookie_path = build_cookie_file(data.get("cookies") or "")
    timeline_url = f"https://twitter.com/{username}/media"

    # Previews are extra files, so ask for headroom above the user's limit and
    # trim back to it once the still frames have been folded into their videos.
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--dump-json",
        "--range", f"1-{min(limit * 2, MAX_SCAN_LIMIT * 2)}",
        "--retries", "3",
        "-o", "previews=true",
    ]
    if cookie_path:
        cmd += ["--cookies", cookie_path]
    cmd.append(timeline_url)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Scan timed out. Try a lower limit."}), 504
    finally:
        if cookie_path:
            Path(cookie_path).unlink(missing_ok=True)

    entries = []
    if proc.stdout.strip():
        try:
            entries = json.loads(proc.stdout)
        except json.JSONDecodeError:
            entries = []

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
    items = [i for i in items if i["kind"] != "preview"][:limit]

    if not items:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
        hint = (
            "No media found. X/Twitter requires a logged-in session: "
            "expand 'Login cookies' and paste your cookies "
            "(at minimum auth_token and ct0 from x.com)."
        )
        return jsonify({
            "images": [], "videos": [],
            "warning": hint,
            "detail": " | ".join(stderr_tail),
        })

    images = [i for i in items if i["kind"] == "image"]
    videos = [i for i in items if i["kind"] == "video"]
    return jsonify({
        "images": images,
        "videos": videos,
        "username": username,
        "note": f"Scanned @{username} (up to {limit} media items).",
    })


def _check_media_url(url):
    return is_allowed_media_url(url)


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
