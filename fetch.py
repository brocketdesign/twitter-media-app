"""Shared helpers for pulling remote media through the server."""

from urllib.parse import urlparse

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

ALLOWED_MEDIA_HOSTS = ("twimg.com",)


def is_allowed_media_url(url):
    host = urlparse(url or "").hostname or ""
    return any(host == d or host.endswith("." + d) for d in ALLOWED_MEDIA_HOSTS)


def fetch_media(url, max_bytes=512 * 1024 * 1024, timeout=60):
    """Download an allowed media URL into memory, bounded by max_bytes."""
    if not is_allowed_media_url(url):
        raise ValueError("URL not allowed")
    resp = requests.get(url, headers=UA, stream=True, timeout=timeout)
    resp.raise_for_status()
    chunks, total = [], 0
    try:
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("File too large")
            chunks.append(chunk)
    finally:
        resp.close()
    return b"".join(chunks)
