"""Publish local characters to myaimodelmanager.com.

Talks to the chatlamix external API (`/api/external/*`) with an API key that
never leaves this machine:

  1. a key typed into the admin page, stored in the local SQLite DB, or
  2. `MYAIMODELMANAGER_API_KEY` in a local `.env`.

Both live under gitignored paths (`data/`, `.env`), and the browser is only
ever shown a masked preview — see `mask_key`. Nothing here writes the key
anywhere else.

Media is pushed as raw bytes (base64) rather than by URL, because the files
live on this disk and the remote has no way to reach them. Anything past the
remote's per-file cap falls back to its original CDN URL when we have one.
"""

import base64
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

import store

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

DEFAULT_BASE_URL = "https://myaimodelmanager.com"

SETTING_KEY = "mam_api_key"
SETTING_BASE = "mam_base_url"

# The remote's decoded per-file caps (models/character-profile-media-utils.js).
# Staying under them here turns a guaranteed 400 into a useful local message.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 60 * 1024 * 1024
# create-character carries the portrait inline as a data URL under a 20MB body.
MAX_PORTRAIT_BYTES = 12 * 1024 * 1024

# Character creation runs vision + persona generation and optionally draws a
# character sheet, so it is slow by nature.
CREATE_TIMEOUT = 600
MEDIA_TIMEOUT = 300


class PublishError(Exception):
    """A remote call failed in a way worth showing the user verbatim."""


# --- configuration ----------------------------------------------------


def load_env(path=ENV_PATH):
    """Read a local .env into os.environ without clobbering real env vars.

    Deliberately tiny: a KEY=value reader, no dependency, no export syntax.
    Values may be quoted; `#` comments and blank lines are skipped.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def api_key():
    """The key to use, preferring one the user typed over the .env fallback."""
    return (store.get_setting(SETTING_KEY) or os.environ.get("MYAIMODELMANAGER_API_KEY") or "").strip()


def key_source():
    if store.get_setting(SETTING_KEY):
        return "saved"
    if os.environ.get("MYAIMODELMANAGER_API_KEY"):
        return "env"
    return "none"


def base_url():
    url = (
        store.get_setting(SETTING_BASE)
        or os.environ.get("MYAIMODELMANAGER_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip()
    return url.rstrip("/")


def mask_key(key):
    """`clx_ABCD…WXYZ` — enough to recognise a key, not enough to use it."""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:2] + "…"
    return f"{key[:8]}…{key[-4:]}"


def settings_view():
    """What the browser is allowed to know about the current configuration."""
    key = api_key()
    return {
        "configured": bool(key),
        "key_preview": mask_key(key),
        "key_source": key_source(),
        "base_url": base_url(),
        "env_available": bool(os.environ.get("MYAIMODELMANAGER_API_KEY")),
    }


# --- client -----------------------------------------------------------


class Client:
    """Thin wrapper over the external API. One instance per request is fine."""

    def __init__(self, key=None, base=None):
        self.key = (key or api_key()).strip()
        self.base = (base or base_url()).rstrip("/")
        if not self.key:
            raise PublishError(
                "No API key configured. Add one on the Admin page, or set "
                "MYAIMODELMANAGER_API_KEY in a local .env file."
            )

    def _call(self, method, path, *, json=None, params=None, timeout=60):
        url = f"{self.base}{path}"
        try:
            resp = requests.request(
                method, url,
                headers={"X-API-Key": self.key, "Accept": "application/json"},
                json=json, params=params, timeout=timeout,
            )
        except requests.RequestException as exc:
            raise PublishError(f"Could not reach {self.base}: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.status_code >= 400:
            detail = body.get("error") or body.get("message") or resp.text[:200]
            if resp.status_code == 401:
                detail = "API key rejected. Check the key on the Admin page."
            raise PublishError(f"{method} {path} → {resp.status_code}: {detail}")
        return body

    # -- reads --

    def me(self):
        return self._call("GET", "/api/external/me", timeout=30)

    def characters(self, limit=50):
        return self._call(
            "GET", "/api/external/characters", params={"limit": limit}, timeout=60
        ).get("characters", [])

    def media(self, chat_id, kind="image", limit=200):
        return self._call(
            "GET", f"/api/external/character/{chat_id}/media",
            params={"type": kind, "limit": limit}, timeout=60,
        ).get("items", [])

    # -- writes --

    def create_character(self, payload):
        return self._call(
            "POST", "/api/external/create-character", json=payload, timeout=CREATE_TIMEOUT
        )

    def add_image(self, chat_id, payload):
        return self._call(
            "POST", f"/api/external/character/{chat_id}/add-image",
            json=payload, timeout=MEDIA_TIMEOUT,
        )

    def add_video(self, chat_id, payload):
        return self._call(
            "POST", f"/api/external/character/{chat_id}/add-video",
            json=payload, timeout=MEDIA_TIMEOUT,
        )


# --- payload building -------------------------------------------------


def _is_http_url(url):
    return urlparse(url or "").scheme in ("http", "https")


def data_url(path, fallback_mime="application/octet-stream"):
    """Read a local file into a `data:` URL the remote can decode."""
    mime = mimetypes.guess_type(path.name)[0] or fallback_mime
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def media_payload(item, path, caption_default=""):
    """Build the add-image / add-video body for one stored file.

    Local bytes are preferred — they are what the user actually curated, and
    a Twitter CDN URL may already have rotated. Files past the remote's cap
    fall back to their original source URL, which the remote fetches itself.
    """
    kind = item["kind"]
    limit = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
    size = path.stat().st_size
    prompt = (item.get("caption") or caption_default or "").strip()

    body = {"prompt": prompt[:2000], "nsfw": False, "isPrivate": False}

    if size <= limit:
        payload_key = "videoBase64" if kind == "video" else "imageBase64"
        body[payload_key] = data_url(path)
        return body

    source = item.get("source_url") or ""
    if _is_http_url(source):
        body["videoUrl" if kind == "video" else "imageUrl"] = source
        return body

    raise PublishError(
        f"{item.get('original_name') or item['filename']} is "
        f"{size / (1024 * 1024):.1f} MB — over the "
        f"{limit // (1024 * 1024)} MB limit for {kind}s, and it has no source "
        "URL to fall back to."
    )


def character_payload(character, portrait_path=None, nsfw=False, visuals=False):
    """The create-character body for a local character."""
    tags = [t.strip() for t in (character.get("tags") or "").split(",") if t.strip()]
    description = (character.get("description") or "").strip()

    payload = {
        "name": character["name"],
        "tags": tags[:5],
        "nsfw": bool(nsfw),
        "generateCharacterSheet": bool(visuals),
        "generateCloseup": bool(visuals),
        # The supplied portrait is the character's actual look; keep it as the
        # thumbnail rather than letting a derived close-up replace it.
        "closeupAsThumbnail": False,
    }
    if description:
        payload["personalityInput"] = description

    if portrait_path and portrait_path.is_file():
        if portrait_path.stat().st_size > MAX_PORTRAIT_BYTES:
            raise PublishError(
                "The avatar image is too large to send as the portrait "
                f"(max {MAX_PORTRAIT_BYTES // (1024 * 1024)} MB). Pick a "
                "smaller image as the avatar."
            )
        payload["imageUrl"] = data_url(portrait_path, "image/jpeg")
    elif description:
        # No image at all: the remote draws the portrait from the description.
        payload["imagePrompt"] = description
    else:
        raise PublishError(
            "This character has no avatar image and no description, so there "
            "is nothing to build a portrait from. Add an image (and set it as "
            "the avatar) or write a description first."
        )
    return payload


# --- the publish run --------------------------------------------------


def _pick_portrait(char_id, character, images):
    """The avatar if one is set, else the first image, else nothing."""
    chosen = None
    avatar_id = character.get("avatar_media_id")
    if avatar_id:
        chosen = next((m for m in images if m["id"] == avatar_id), None)
    if not chosen and images:
        chosen = images[0]
    if not chosen:
        return None
    path = store.media_path(char_id, chosen["filename"])
    return path if path.is_file() else None


def publish_character(char_id, *, client=None, nsfw=False, visuals=False,
                      remote_id=None, include_images=True, include_videos=True):
    """Create (or reuse) the remote character and push everything not yet sent.

    Returns a report dict; individual file failures are collected rather than
    aborting the run, so one bad file never strands the rest.
    """
    client = client or Client()
    character = store.get_character(char_id)
    if not character:
        raise PublishError("Character not found.")

    images = store.list_media(char_id, "image")
    videos = store.list_media(char_id, "video")

    link = store.get_remote_character(char_id)
    created = False

    if remote_id:
        # Attaching to a character that already exists on the remote.
        store.link_remote_character(char_id, remote_id, base_url=client.base)
        chat_id = str(remote_id)
    elif link and link["base_url"] == client.base:
        chat_id = link["remote_id"]
    else:
        portrait = _pick_portrait(char_id, character, images)
        result = client.create_character(
            character_payload(character, portrait, nsfw=nsfw, visuals=visuals)
        )
        chat_id = str(result.get("chatId") or "")
        if not chat_id:
            raise PublishError("The API created no character (no chatId returned).")
        store.link_remote_character(
            char_id, chat_id, slug=result.get("slug") or "", base_url=client.base
        )
        created = True
        # The portrait became the character's thumbnail; don't also push it as
        # a gallery image on this first run.
        if portrait:
            used = next(
                (m for m in images if store.media_path(char_id, m["filename"]) == portrait),
                None,
            )
            if used:
                store.link_remote_media(
                    char_id, used["id"], remote_url=result.get("faceImageUrl") or "",
                    kind="image",
                )

    already = store.published_media_ids(char_id)
    queue = []
    if include_images:
        queue += [m for m in images if m["id"] not in already]
    if include_videos:
        queue += [m for m in videos if m["id"] not in already]

    added, failed = {"image": 0, "video": 0}, []
    for item in queue:
        path = store.media_path(char_id, item["filename"])
        label = item.get("original_name") or item["filename"]
        if not path.is_file():
            failed.append(f"{label}: file missing on disk")
            continue
        try:
            body = media_payload(item, path, caption_default=character.get("description", ""))
            if item["kind"] == "video":
                result = client.add_video(chat_id, body)
                remote_media_id = result.get("videoId", "")
                remote_url = result.get("videoUrl", "")
            else:
                result = client.add_image(chat_id, body)
                remote_media_id = result.get("imageId", "")
                remote_url = result.get("imageUrl", "")
            store.link_remote_media(
                char_id, item["id"], remote_id=remote_media_id,
                remote_url=remote_url, kind=item["kind"],
            )
            added[item["kind"]] += 1
        except (PublishError, OSError) as exc:
            failed.append(f"{label}: {exc}")

    link = store.get_remote_character(char_id)
    return {
        "created": created,
        "chat_id": chat_id,
        "slug": link["slug"] if link else "",
        "url": character_url(client.base, chat_id, link["slug"] if link else ""),
        "images_added": added["image"],
        "videos_added": added["video"],
        "skipped": len(images) + len(videos) - len(queue),
        "failed": failed[:20],
        "failed_count": len(failed),
        "summary": store.publish_summary(char_id),
    }


def character_url(base, chat_id, slug=""):
    return f"{base}/character/slug/{slug}" if slug else f"{base}/character/{chat_id}"
