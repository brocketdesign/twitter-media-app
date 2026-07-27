# Twitter/X Media Lister

A small local web app that lists all media posted by a Twitter/X account — images and videos in separate tabs — ready to download individually or as a ZIP.

![stack](https://img.shields.io/badge/python-3.9%2B-blue) ![framework](https://img.shields.io/badge/flask-3-black)

## Features

- Paste a username, `@handle`, or profile URL (`https://x.com/...`)
- Scans the account's media timeline via [gallery-dl](https://github.com/mikf/gallery-dl)
- Images and videos listed in separate tabs with thumbnails, dates, and tweet text
- Per-item download buttons (proxied through the server, forced as attachments)
- "Download all in tab as ZIP" bulk download
- Links back to the original tweets
- Configurable scan limit (up to 1000 media items)

## Requirements

X/Twitter blocks anonymous timeline access, so you must provide login cookies from a logged-in session on x.com. Either:

- Export cookies with a "Get cookies.txt" browser extension and paste the whole file into the **Login cookies** field in the UI, or
- Open DevTools → Application → Cookies → x.com and paste `auth_token=…; ct0=…`

Cookies are written to a temp file, passed to gallery-dl, and deleted after each scan. They are never stored or logged by the app.

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

1. Enter a username or profile URL and optionally adjust the scan limit.
2. Expand **Login cookies** and paste your cookies (see Requirements).
3. Click **Scan** — large timelines can take a minute.
4. Browse the Images/Videos tabs, download items individually, or grab the whole tab as a ZIP.

## How it works

- `app.py` — Flask backend. Runs `gallery-dl --dump-json` against the account's `/media` timeline, classifies entries into images vs. videos by file extension, and exposes `/api/scan`, `/api/download`, and `/api/zip` endpoints. Downloads are proxied and restricted to `twimg.com` hosts.
- `templates/index.html` — dependency-free single-page UI.

## Notes

- Only publicly posted media from the account's Media tab is listed; protected accounts require cookies from a session that follows them.
- Be mindful of X's rate limits — keep the scan limit reasonable.
- Respect copyright and X's Terms of Service when downloading media.
