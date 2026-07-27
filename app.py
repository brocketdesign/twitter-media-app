"""Twitter/X media lister — web UI on top of gallery-dl.

Paste a username or profile URL, scan the account's media timeline,
and get images and videos listed separately, ready to download.
"""

import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, Response, stream_with_context

app = Flask(__name__)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

ALLOWED_MEDIA_HOSTS = ("twimg.com",)
VIDEO_EXTS = {"mp4", "m4v", "mov"}
MAX_SCAN_LIMIT = 1000


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

    Accepts either a full Netscape cookies.txt export or a header-style
    string like 'auth_token=...; ct0=...'.
    """
    raw = raw.strip()
    if not raw:
        return None
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
    }


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

    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--dump-json",
        "--range", f"1-{limit}",
        "--retries", "3",
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
        "note": f"Scanned @{username} (up to {limit} media items).",
    })


def _check_media_url(url):
    host = urlparse(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in ALLOWED_MEDIA_HOSTS)


@app.get("/api/download")
def download():
    url = request.args.get("url", "")
    name = re.sub(r"[^\w.\-]", "_", request.args.get("name", "media"))
    if not _check_media_url(url):
        return jsonify({"error": "URL not allowed"}), 400
    try:
        r = requests.get(url, headers=UA, stream=True, timeout=60)
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
                r = requests.get(url, headers=UA, timeout=60)
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
