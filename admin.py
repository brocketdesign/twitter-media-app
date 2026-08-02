"""Admin dashboard + character creator.

Pages:
  /admin                    dashboard: stats and character list
  /admin/characters/new     character creator
  /admin/characters/<id>    character editor (images + videos)

JSON API under /api/characters, plus /media/<char_id>/<file> to serve
stored media back to the browser.
"""

import ast

import requests
from flask import Blueprint, abort, jsonify, render_template, request, send_from_directory

import publish
import store
from fetch import fetch_media, is_allowed_media_url

bp = Blueprint("admin", __name__)

MAX_IMPORT_ITEMS = 500


def _character_or_404(char_id):
    character = store.get_character(char_id)
    if not character:
        abort(404, description="Character not found")
    return character


def _payload():
    return request.get_json(force=True, silent=True) or {}


# --- pages ------------------------------------------------------------


@bp.get("/admin")
def dashboard():
    return render_template(
        "admin.html",
        characters=store.list_characters(),
        stats=store.stats(),
        publish_settings=publish.settings_view(),
    )


@bp.get("/admin/characters/new")
def character_new():
    return render_template("character.html", character=None, remote=None)


@bp.get("/admin/characters/<int:char_id>")
def character_edit(char_id):
    return render_template(
        "character.html",
        character=_character_or_404(char_id),
        remote=store.get_remote_character(char_id),
    )


# --- character CRUD ---------------------------------------------------


@bp.get("/api/characters")
def api_list_characters():
    return jsonify({"characters": store.list_characters(), "stats": store.stats()})


@bp.post("/api/characters")
def api_create_character():
    data = _payload()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    char_id = store.create_character(
        name,
        handle=(data.get("handle") or "").strip().lstrip("@"),
        description=data.get("description") or "",
        tags=data.get("tags") or "",
    )
    return jsonify({"character": store.get_character(char_id)}), 201


@bp.get("/api/characters/<int:char_id>")
def api_get_character(char_id):
    character = _character_or_404(char_id)
    return jsonify({
        "character": character,
        "images": store.list_media(char_id, "image"),
        "videos": store.list_media(char_id, "video"),
    })


@bp.patch("/api/characters/<int:char_id>")
def api_update_character(char_id):
    _character_or_404(char_id)
    data = _payload()
    fields = {}
    for key in ("name", "handle", "description", "tags"):
        if key in data:
            fields[key] = str(data[key]).strip()
    if "handle" in fields:
        fields["handle"] = fields["handle"].lstrip("@")
    if "avatar_media_id" in data:
        value = data["avatar_media_id"]
        if value in (None, "", 0):
            fields["avatar_media_id"] = None
        elif store.get_media(char_id, int(value)):
            fields["avatar_media_id"] = int(value)
        else:
            return jsonify({"error": "Unknown media item."}), 400
    if fields.get("name") == "":
        return jsonify({"error": "Name cannot be empty."}), 400
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400
    store.update_character(char_id, **fields)
    return jsonify({"character": store.get_character(char_id)})


@bp.delete("/api/characters/<int:char_id>")
def api_delete_character(char_id):
    _character_or_404(char_id)
    store.delete_character(char_id)
    return jsonify({"deleted": char_id})


# --- media ------------------------------------------------------------


@bp.post("/api/characters/<int:char_id>/import")
def api_import_media(char_id):
    """Pull scanned timeline media (twimg URLs) into a character."""
    _character_or_404(char_id)
    items = _payload().get("items") or []
    if not items:
        return jsonify({"error": "No items to send."}), 400
    if len(items) > MAX_IMPORT_ITEMS:
        return jsonify({"error": f"Send at most {MAX_IMPORT_ITEMS} items at a time."}), 400

    added, skipped, failed = 0, 0, []
    for item in items:
        url = (item.get("url") or "").strip()
        if not is_allowed_media_url(url):
            failed.append(url or "(no url)")
            continue
        if store.source_exists(char_id, url):
            skipped += 1
            continue
        try:
            data = fetch_media(url)
            store.save_media(
                char_id, data,
                original_name=item.get("filename") or "",
                source_url=url,
                tweet_url=item.get("tweet_url") or "",
                caption=item.get("text") or "",
            )
            added += 1
        except (requests.RequestException, ValueError, OSError) as exc:
            failed.append(f"{item.get('filename') or url}: {exc}")

    return jsonify({
        "added": added,
        "skipped": skipped,
        "failed": failed[:10],
        "failed_count": len(failed),
        "character": store.get_character(char_id),
    })


@bp.post("/api/characters/<int:char_id>/upload")
def api_upload_media(char_id):
    """Direct file uploads — images and videos alike."""
    _character_or_404(char_id)
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded."}), 400

    added, failed = [], []
    for storage in files:
        name = storage.filename or ""
        if not store.safe_ext(name):
            failed.append(f"{name or '(unnamed)'}: unsupported file type")
            continue
        try:
            media_id = store.save_media(
                char_id, storage.stream, original_name=name,
                caption=request.form.get("caption") or "",
            )
            added.append(store.get_media(char_id, media_id))
        except (ValueError, OSError) as exc:
            failed.append(f"{name}: {exc}")

    if not added and failed:
        return jsonify({"error": "; ".join(failed[:5])}), 400
    return jsonify({"added": added, "failed": failed})


@bp.delete("/api/characters/<int:char_id>/media/<int:media_id>")
def api_delete_media(char_id, media_id):
    _character_or_404(char_id)
    if not store.delete_media(char_id, media_id):
        return jsonify({"error": "Media not found."}), 404
    return jsonify({"deleted": media_id})


# --- publishing to myaimodelmanager -----------------------------------
#
# The API key is never sent to the browser. Reads return a masked preview
# only; writes accept a key and store it locally (see publish.py).


@bp.get("/api/publish/settings")
def api_publish_settings():
    return jsonify(publish.settings_view())


@bp.put("/api/publish/settings")
def api_save_publish_settings():
    data = _payload()

    if "base_url" in data:
        base = (data.get("base_url") or "").strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            return jsonify({"error": "Base URL must start with http:// or https://"}), 400
        if base:
            store.set_setting(publish.SETTING_BASE, base)
        else:
            store.delete_setting(publish.SETTING_BASE)

    if "api_key" in data:
        key = (data.get("api_key") or "").strip()
        if key:
            store.set_setting(publish.SETTING_KEY, key)
        else:
            # Clearing falls back to the .env key, if there is one.
            store.delete_setting(publish.SETTING_KEY)

    return jsonify(publish.settings_view())


@bp.post("/api/publish/test")
def api_publish_test():
    """Verify the configured key against the live API."""
    try:
        account = publish.Client().me()
    except publish.PublishError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "account": {
            "userId": account.get("userId"),
            "email": account.get("email"),
            "username": account.get("username"),
            "characterCount": account.get("characterCount"),
        },
        "settings": publish.settings_view(),
    })


def _first_url(value):
    """Pick one URL out of a remote field.

    The remote returns either a plain URL or a Python-repr list of URLs
    ("['https://…', 'https://…']"), depending on how the character was made.
    """
    if isinstance(value, (list, tuple)):
        candidates = value
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                candidates = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                candidates = []
        else:
            candidates = [text]
    else:
        candidates = []
    for item in candidates or []:
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            return item
    return ""


def _remote_card(char):
    """Flatten one remote character into what the picker needs."""
    tags = char.get("tags")
    if isinstance(tags, str) and tags.strip().startswith("["):
        try:
            tags = ast.literal_eval(tags)
        except (ValueError, SyntaxError):
            tags = []
    if not isinstance(tags, (list, tuple)):
        tags = [t.strip() for t in str(tags or "").split(",") if t.strip()]
    return {
        "id": char.get("_id") or "",
        "name": char.get("name") or char.get("slug") or char.get("_id") or "Untitled",
        "intro": (char.get("short_intro") or "").strip(),
        "thumbnail": _first_url(char.get("thumbnail") or char.get("chatImageUrl")),
        "slug": char.get("slug") or "",
        "created": str(char.get("createdAt") or "")[:10],
        "tags": [str(t) for t in tags][:4],
    }


@bp.get("/api/publish/characters")
def api_publish_remote_characters():
    """Remote characters, so media can be added to an existing one."""
    try:
        characters = publish.Client().characters(limit=100)
    except publish.PublishError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"characters": [_remote_card(c) for c in characters]})


@bp.post("/api/characters/<int:char_id>/publish")
def api_publish_character(char_id):
    _character_or_404(char_id)
    data = _payload()
    try:
        report = publish.publish_character(
            char_id,
            nsfw=bool(data.get("nsfw")),
            visuals=bool(data.get("visuals")),
            remote_id=(data.get("remote_id") or "").strip() or None,
            include_images=data.get("include_images", True),
            include_videos=data.get("include_videos", True),
        )
    except publish.PublishError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(report)


@bp.get("/api/characters/<int:char_id>/publish")
def api_publish_status(char_id):
    _character_or_404(char_id)
    link = store.get_remote_character(char_id)
    return jsonify({
        "remote": link,
        "url": publish.character_url(link["base_url"], link["remote_id"], link["slug"])
        if link else None,
        "summary": store.publish_summary(char_id),
        "settings": publish.settings_view(),
    })


@bp.delete("/api/characters/<int:char_id>/publish")
def api_unlink_publish(char_id):
    """Forget the remote link locally. The remote character is left alone."""
    _character_or_404(char_id)
    store.unlink_remote_character(char_id)
    return jsonify({"unlinked": char_id, "summary": store.publish_summary(char_id)})


@bp.get("/media/<int:char_id>/<path:filename>")
def serve_media(char_id, filename):
    folder = store.MEDIA_DIR / str(char_id)
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, conditional=True)
