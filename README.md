# Twitter/X Media Lister

A small local web app that lists all media posted by a Twitter/X account — images and videos in separate tabs — ready to download individually or as a ZIP.

![stack](https://img.shields.io/badge/python-3.9%2B-blue) ![framework](https://img.shields.io/badge/flask-3-black)

## Features

- Paste a username, `@handle`, or profile URL (`https://x.com/...`)
- Scans the account's media timeline via [gallery-dl](https://github.com/mikf/gallery-dl)
- Images and videos listed in separate tabs with thumbnails, dates, and tweet text
- Videos show the tweet's own still frame with a duration badge and **play inline** on click
- Per-item download buttons (proxied through the server, forced as attachments)
- "Download all in tab as ZIP" bulk download
- Links back to the original tweets
- Configurable scan limit (up to 1000 media items)
- Select individual items across **both** tabs (or the whole tab) and **send them to a character**
- Cookies, the last scan, and a history of searches that landed media are kept in the browser — reload or navigate away without retyping anything
- **Admin dashboard** at `/admin` — character library with per-character image/video counts and storage
- **Character creator** — profile fields, avatar picking, drag-and-drop **image and video uploads**

## Requirements

X/Twitter blocks anonymous timeline access, so you must provide login cookies from a logged-in session on x.com. Either:

- Export cookies with a "Get cookies.txt" browser extension and paste the whole file into the **Login cookies** field in the UI, or
- Open DevTools → Application → Cookies → x.com and paste `auth_token=…; ct0=…`

Server-side, cookies are written to a temp file, passed to gallery-dl, and deleted after each scan — never stored or logged by the app.

In the browser they are kept in `localStorage` (`twmedia.cookies`) so a reload or a trip to `/admin` doesn't cost you another paste. The **Login cookies** panel shows `· saved`, and **Clear saved cookies** wipes them. If that trade-off isn't for you, clear the entry after each session — anything with access to the browser profile can read it.

## Setup & run

```bash
git clone https://github.com/brocketdesign/twitter-media-app.git
cd twitter-media-app
python3 -m venv .venv
.venv/bin/pip install flask gallery-dl requests
.venv/bin/python app.py
```

Then open http://127.0.0.1:5001

## Usage

### Scanning a timeline

1. Enter a username or profile URL and optionally adjust the scan limit.
2. Expand **Login cookies** and paste your cookies (see Requirements). Saved after the first paste.
3. Click **Scan** — large timelines can take a minute.
4. Browse the Images/Videos tabs, download items individually, or grab the whole tab as a ZIP.
5. Click a video to play it inline; the poster and duration come from the tweet itself.

The results, the handle, the scan limit, the active tab and your selections are saved as you go, so a refresh or a detour to `/admin` brings the same grid back. Under the search box, chips list the handles whose scans actually returned media — click one to re-scan it, or its `×` to forget it.

### Sending media to a character

1. Tick the checkbox on any items you want. Picks in **both** tabs are sent together; leave everything unticked to send just the tab you're looking at.
2. Click **Send to character…**, then pick an existing character or **Create a new character…** right in the dialog.
3. The server downloads each item and files it under that character. Re-sending the same media is skipped rather than duplicated (500 items max per send).

### Admin dashboard & character creator

- `/admin` — every character with image/video counts, storage used, tag pills, and a filter box. Delete removes the character and its files.
- `/admin/characters/new` — create a character (name, source handle, description, tags). Files dropped here are queued and uploaded the moment the character is created.
- `/admin/characters/<id>` — edit the profile and manage media. The **Images** and **Videos** tabs each have a drag-and-drop zone; the Videos tab accepts MP4, MOV, M4V, WEBM, MKV and AVI, with an upload progress bar for large files. Any image can be promoted to the character's avatar.

Uploads are capped at 2 GB per request (`MAX_CONTENT_LENGTH` in `app.py`).

### Publishing to myaimodelmanager

A character — with its images *and* videos — can be pushed to
[myaimodelmanager.com](https://myaimodelmanager.com) through its external API.

**1. Give the app an API key.** Create one on the site (Account → API keys), then
either paste it into the *myaimodelmanager connection* panel on `/admin`, or put
it in a local `.env`:

```bash
cp .env.example .env      # then fill in MYAIMODELMANAGER_API_KEY=clx_…
```

A key typed into the admin page wins over the `.env` one. Both stay on this
machine: the typed key goes into `data/app.db`, and both `data/` and `.env` are
gitignored. The browser only ever receives a masked preview (`clx_N-UP…sPRk`) —
the key itself is never sent to the front end and never committed. **Test
connection** verifies it against the live API.

**2. Publish.** On `/admin/characters/<id>`, the *Publish to myaimodelmanager*
panel creates the remote character and uploads its media. You can instead attach
to a character that already exists on the account: the picker lists them as cards
with their portrait, intro, tags and creation date, and a search box filters the
list — handy when several remote characters share a name.

- The avatar (or the first image) becomes the remote character's portrait; its
  description seeds the personality. With no images at all, the description is
  used to generate a portrait.
- Files are sent as raw bytes, since the remote can't reach `localhost`. Images
  up to 12 MB and videos up to 60 MB go inline; anything larger falls back to
  its original `twimg.com` URL for the remote to fetch.
- Every uploaded file is logged locally, so publishing again only sends what's
  new. Add videos to a character months later and just those videos go up.
- **Unlink** forgets the local link only — nothing is deleted remotely.

## How it works

- `app.py` — Flask backend. Runs `gallery-dl --dump-json` against the account's `/media` timeline, classifies entries into images vs. videos by file extension, and exposes `/api/scan`, `/api/download`, and `/api/zip` endpoints. Downloads are proxied and restricted to `twimg.com` hosts. Scans run with `previews=true` so each video's still frame comes along; those frames are folded into their video as a poster instead of being listed as separate images, and the request asks for double the limit to make room for them before trimming back.
- `admin.py` — admin dashboard, character creator, and the `/api/characters/*` endpoints (create/update/delete, `import` for scanned media, `upload` for local files), plus `/media/<char_id>/<file>` for serving stored media.
- `store.py` — SQLite schema and helpers. The database lives at `data/app.db` and media files under `data/media/<character_id>/`; both are gitignored.
- `fetch.py` — shared remote-media fetching with the `twimg.com` host allowlist.
- `publish.py` — the myaimodelmanager client: key resolution (local DB, then `.env`), masking, payload building, and the incremental publish run. Calls `/api/external/create-character`, `/api/external/character/:id/add-image` and `/add-video`.
- `templates/` + `static/base.css` — dependency-free UI, no build step.

The app is intended to run on localhost and has no authentication — don't expose `/admin` to a network you don't control.

## Notes

- Only publicly posted media from the account's Media tab is listed; protected accounts require cookies from a session that follows them.
- Be mindful of X's rate limits — keep the scan limit reasonable.
- Respect copyright and X's Terms of Service when downloading media.
