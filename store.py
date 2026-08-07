"""SQLite storage for characters and their media.

Characters group images and videos — either uploaded by hand in the
character creator, or imported from a scanned X/Twitter timeline.
Media files live on disk under DATA_DIR/media/<character_id>/.
"""

import mimetypes
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Hosted deployments have an ephemeral filesystem, so point DATA_DIR at a
# mounted volume there — otherwise the database and every uploaded file are
# wiped on each redeploy. Defaults to ./data for local runs.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "app.db"

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"}
VIDEO_EXTS = {"mp4", "m4v", "mov", "webm", "mkv", "avi"}
ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    handle          TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    avatar_media_id INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_media (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    tweet_url     TEXT NOT NULL DEFAULT '',
    caption       TEXT NOT NULL DEFAULT '',
    bytes         INTEGER NOT NULL DEFAULT 0,
    mime          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_character ON character_media(character_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_source
    ON character_media(character_id, source_url) WHERE source_url <> '';

-- Games: one local player wallet, per-character bond, and game sessions.
CREATE TABLE IF NOT EXISTS player (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    coins           INTEGER NOT NULL DEFAULT 250,
    total_earned    INTEGER NOT NULL DEFAULT 0,
    total_spent     INTEGER NOT NULL DEFAULT 0,
    plays           INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    streak          INTEGER NOT NULL DEFAULT 0,
    best_streak     INTEGER NOT NULL DEFAULT 0,
    last_bonus_day  TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS character_bond (
    character_id INTEGER PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    xp           INTEGER NOT NULL DEFAULT 0,
    level        INTEGER NOT NULL DEFAULT 1,
    plays        INTEGER NOT NULL DEFAULT 0,
    wins         INTEGER NOT NULL DEFAULT 0,
    streak       INTEGER NOT NULL DEFAULT 0,
    last_played  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS character_unlocks (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    media_id     INTEGER NOT NULL REFERENCES character_media(id) ON DELETE CASCADE,
    level        INTEGER NOT NULL DEFAULT 1,
    unlocked_at  TEXT NOT NULL,
    PRIMARY KEY (character_id, media_id)
);

CREATE TABLE IF NOT EXISTS game_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id   INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    game           TEXT NOT NULL DEFAULT 'wordle',
    word           TEXT NOT NULL,
    theme          TEXT NOT NULL DEFAULT '',
    theme_label    TEXT NOT NULL DEFAULT '',
    clue           TEXT NOT NULL DEFAULT '',
    guesses        TEXT NOT NULL DEFAULT '[]',
    max_guesses    INTEGER NOT NULL DEFAULT 6,
    revealed       TEXT NOT NULL DEFAULT '[]',
    hints_used     INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'active',
    coins_earned   INTEGER NOT NULL DEFAULT 0,
    coins_spent    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    finished_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_character
    ON game_sessions(character_id, status);

CREATE TABLE IF NOT EXISTS coin_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    amount       INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    character_id INTEGER,
    created_at   TEXT NOT NULL
);

-- Local-only key/value settings. This is where a user-supplied
-- myaimodelmanager API key lives; DATA_DIR is gitignored, so the key never
-- leaves this machine and is never committed.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

-- Publishing: which remote character a local one became, so publishing twice
-- adds to the existing character instead of creating a duplicate.
CREATE TABLE IF NOT EXISTS character_remote (
    character_id INTEGER PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    remote_id    TEXT NOT NULL,
    slug         TEXT NOT NULL DEFAULT '',
    base_url     TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL
);

-- Per-file publish log, so a re-run only uploads what is genuinely new.
CREATE TABLE IF NOT EXISTS media_remote (
    media_id     INTEGER PRIMARY KEY REFERENCES character_media(id) ON DELETE CASCADE,
    character_id INTEGER NOT NULL,
    remote_id    TEXT NOT NULL DEFAULT '',
    remote_url   TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_remote_character
    ON media_remote(character_id);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO player (id, updated_at) VALUES (1, ?)", (now(),)
        )


def safe_ext(name):
    """Lowercased extension of a filename, if it's one we accept."""
    ext = Path(name or "").suffix.lstrip(".").lower()
    return ext if ext in ALLOWED_EXTS else ""


def kind_for_ext(ext):
    return "video" if ext in VIDEO_EXTS else "image"


def _slug(text, fallback="media"):
    text = re.sub(r"[^\w.\-]+", "_", (text or "").strip())[:80].strip("._-")
    return text or fallback


# --- characters -------------------------------------------------------


def list_characters():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM character_media m
                     WHERE m.character_id = c.id AND m.kind = 'image') AS image_count,
                   (SELECT COUNT(*) FROM character_media m
                     WHERE m.character_id = c.id AND m.kind = 'video') AS video_count,
                   (SELECT COALESCE(SUM(bytes), 0) FROM character_media m
                     WHERE m.character_id = c.id) AS total_bytes,
                   (SELECT m.filename FROM character_media m
                     WHERE m.id = c.avatar_media_id) AS avatar_filename
              FROM characters c
             ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_character(char_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE id = ?", (char_id,)
        ).fetchone()
    return dict(row) if row else None


def create_character(name, handle="", description="", tags=""):
    stamp = now()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO characters (name, handle, description, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name.strip(), handle.strip(), description.strip(), tags.strip(), stamp, stamp),
        )
        char_id = cur.lastrowid
    (MEDIA_DIR / str(char_id)).mkdir(parents=True, exist_ok=True)
    return char_id


def update_character(char_id, **fields):
    allowed = {"name", "handle", "description", "tags", "avatar_media_id"}
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            values.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    values += [now(), char_id]
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE characters SET {', '.join(sets)} WHERE id = ?", values
        )
        return cur.rowcount > 0


def touch_character(char_id):
    with connect() as conn:
        conn.execute(
            "UPDATE characters SET updated_at = ? WHERE id = ?", (now(), char_id)
        )


def delete_character(char_id):
    with connect() as conn:
        cur = conn.execute("DELETE FROM characters WHERE id = ?", (char_id,))
        deleted = cur.rowcount > 0
    folder = MEDIA_DIR / str(char_id)
    if folder.is_dir():
        for path in folder.iterdir():
            path.unlink(missing_ok=True)
        folder.rmdir()
    return deleted


def stats():
    with connect() as conn:
        row = conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM characters) AS characters,
                   (SELECT COUNT(*) FROM character_media WHERE kind='image') AS images,
                   (SELECT COUNT(*) FROM character_media WHERE kind='video') AS videos,
                   (SELECT COALESCE(SUM(bytes), 0) FROM character_media) AS total_bytes
            """
        ).fetchone()
    return dict(row)


# --- media ------------------------------------------------------------


def list_media(char_id, kind=None):
    query = "SELECT * FROM character_media WHERE character_id = ?"
    params = [char_id]
    if kind in ("image", "video"):
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY id DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_media(char_id, media_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM character_media WHERE id = ? AND character_id = ?",
            (media_id, char_id),
        ).fetchone()
    return dict(row) if row else None


def media_path(char_id, filename):
    return MEDIA_DIR / str(char_id) / filename


def source_exists(char_id, source_url):
    if not source_url:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM character_media WHERE character_id = ? AND source_url = ?",
            (char_id, source_url),
        ).fetchone()
    return row is not None


def save_media(char_id, data, original_name, source_url="", tweet_url="", caption=""):
    """Store media for a character and record the row.

    `data` is either raw bytes or a file-like object (uploads stream to
    disk in chunks so large videos never sit in memory).
    """
    ext = safe_ext(original_name) or safe_ext(source_url.split("?")[0])
    if not ext:
        raise ValueError(f"Unsupported file type: {original_name or source_url}")
    kind = kind_for_ext(ext)
    folder = MEDIA_DIR / str(char_id)
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    target = folder / filename
    if isinstance(data, (bytes, bytearray)):
        target.write_bytes(data)
        size = len(data)
    else:
        size = 0
        with open(target, "wb") as out:
            while True:
                chunk = data.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                out.write(chunk)
    mime = mimetypes.guess_type(filename)[0] or ""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO character_media
                 (character_id, kind, filename, original_name, source_url,
                  tweet_url, caption, bytes, mime, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                char_id, kind, filename, _slug(original_name, f"media.{ext}"),
                source_url, tweet_url, (caption or "").strip()[:500],
                size, mime, now(),
            ),
        )
        media_id = cur.lastrowid
        conn.execute(
            "UPDATE characters SET updated_at = ? WHERE id = ?", (now(), char_id)
        )
    return media_id


def random_locked_image(char_id):
    """An image this character hasn't revealed yet, if any."""
    with connect() as conn:
        row = conn.execute(
            """SELECT m.* FROM character_media m
                WHERE m.character_id = ? AND m.kind = 'image'
                  AND m.id NOT IN (SELECT media_id FROM character_unlocks
                                    WHERE character_id = ?)
                ORDER BY RANDOM() LIMIT 1""",
            (char_id, char_id),
        ).fetchone()
    return dict(row) if row else None


# --- settings ---------------------------------------------------------


def get_setting(key, default=""):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value or "", now()),
        )


def delete_setting(key):
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --- publishing links -------------------------------------------------


def get_remote_character(char_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM character_remote WHERE character_id = ?", (char_id,)
        ).fetchone()
    return dict(row) if row else None


def link_remote_character(char_id, remote_id, slug="", base_url=""):
    with connect() as conn:
        conn.execute(
            """INSERT INTO character_remote
                 (character_id, remote_id, slug, base_url, published_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(character_id) DO UPDATE SET
                 remote_id = excluded.remote_id,
                 slug = excluded.slug,
                 base_url = excluded.base_url,
                 published_at = excluded.published_at""",
            (char_id, str(remote_id), slug or "", base_url or "", now()),
        )


def unlink_remote_character(char_id):
    """Forget the remote link (and the per-file log) for a local character."""
    with connect() as conn:
        conn.execute("DELETE FROM character_remote WHERE character_id = ?", (char_id,))
        conn.execute("DELETE FROM media_remote WHERE character_id = ?", (char_id,))


def published_media_ids(char_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT media_id FROM media_remote WHERE character_id = ?", (char_id,)
        ).fetchall()
    return {r["media_id"] for r in rows}


def link_remote_media(char_id, media_id, remote_id="", remote_url="", kind=""):
    with connect() as conn:
        conn.execute(
            """INSERT INTO media_remote
                 (media_id, character_id, remote_id, remote_url, kind, published_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(media_id) DO UPDATE SET
                 remote_id = excluded.remote_id,
                 remote_url = excluded.remote_url,
                 published_at = excluded.published_at""",
            (media_id, char_id, str(remote_id or ""), remote_url or "", kind, now()),
        )


def publish_summary(char_id):
    """Counts of what is published vs still local, for the UI."""
    with connect() as conn:
        row = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM character_media WHERE character_id = ?) AS total,
                 (SELECT COUNT(*) FROM media_remote WHERE character_id = ?) AS published
            """,
            (char_id, char_id),
        ).fetchone()
    total, published = row["total"], row["published"]
    return {"total": total, "published": published, "pending": max(0, total - published)}


def delete_media(char_id, media_id):
    item = get_media(char_id, media_id)
    if not item:
        return False
    media_path(char_id, item["filename"]).unlink(missing_ok=True)
    with connect() as conn:
        conn.execute(
            "DELETE FROM character_media WHERE id = ? AND character_id = ?",
            (media_id, char_id),
        )
        conn.execute(
            "UPDATE characters SET avatar_media_id = NULL "
            "WHERE id = ? AND avatar_media_id = ?",
            (char_id, media_id),
        )
        conn.execute(
            "UPDATE characters SET updated_at = ? WHERE id = ?", (now(), char_id)
        )
    return True
