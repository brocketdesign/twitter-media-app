"""Everything the app knows about X/Twitter login cookies.

Reading whatever the user pasted, checking it is usable *before* a scan burns
a minute finding out it wasn't, writing the Netscape file gallery-dl wants,
and asking X directly whether the session behind those cookies is still alive.
"""

import json
import re
import tempfile

import requests

from fetch import UA

# Cookie names X's own session depends on. auth_token is the session itself;
# ct0 is the CSRF token every authenticated call has to echo back, so a paste
# with only auth_token gets 403s that look exactly like a bad login.
REQUIRED = ("auth_token", "ct0")

# A full-profile export carries every site the user is logged into. Only the
# X ones are ever sent anywhere, so unrelated sessions stay on their machine.
X_DOMAINS = ("x.com", "twitter.com")

# Public bearer the x.com web client ships to every visitor — the same one
# gallery-dl uses. It identifies the client, not the user; the cookies do that.
WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Tried in order, first straight answer wins. X retires legacy REST paths
# without warning — the old verify_credentials now answers 404 "that page does
# not exist" for perfectly good sessions — so a 404 here means the endpoint is
# gone, never that the cookies are bad. /i/api is what x.com's own web client
# calls, which makes it the likeliest of the three to outlive the others.
VERIFY_URLS = (
    "https://x.com/i/api/1.1/account/settings.json",
    "https://api.x.com/1.1/account/settings.json",
    "https://api.x.com/1.1/account/verify_credentials.json",
)

# Statuses that say something about the session. Anything else (404, 5xx) is
# the endpoint's problem, not the user's.
DECISIVE = (200, 401, 403, 429)

# RFC 6265 token characters, minus the ones no real cookie name uses. Kept
# tight so stray prose in a sloppy paste doesn't register as a cookie.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_PAIR_RE = re.compile(r'^\s*"?([A-Za-z0-9_.\-]+)"?\s*[=:]\s*"?(.*?)"?\s*$')

# Header pastes ("Cookie: auth_token=…") and DevTools table headers are common
# enough to be worth stripping rather than reporting as unreadable input.
_HEADER_PREFIX_RE = re.compile(r"^\s*(?:set-)?cookie\s*:\s*", re.I)
_NOT_COOKIES = {"cookie", "set-cookie", "name", "host", "domain", "path"}

# What a healthy value looks like — used only to warn, never to reject, since
# X is free to change its formats without telling us.
_SHAPES = {
    "auth_token": re.compile(r"^[0-9a-f]{30,60}$"),
    "ct0": re.compile(r"^[0-9a-f]{30,200}$"),
}


def parse(raw):
    """Read pasted text in any of the shapes people actually paste.

    Handles a JSON cookie export, a Netscape cookies.txt, the tab-separated
    block DevTools copies out of its cookie table, and header-style text —
    with ``=`` or ``:`` between name and value, separated by ``;`` or newlines.
    Returns a ``{name: value}`` dict, empty when nothing looked like a cookie.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    pairs = {}
    if raw[0] in "[{":
        pairs = _from_json(raw)
    if not pairs and "\t" in raw:
        pairs = _from_table(raw)
    if not pairs:
        pairs = _from_text(raw)
    return {k: v for k, v in pairs.items() if k.lower() not in _NOT_COOKIES and v}


def _domain_ok(domain):
    # No domain column at all means the user copied only the x.com cookies,
    # so there is nothing to filter on and we take them at their word.
    d = (domain or "").strip().lstrip(".").lower()
    if not d:
        return True
    return any(d == x or d.endswith("." + x) for x in X_DOMAINS)


def _from_json(raw):
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        # A flat {"auth_token": "…", "ct0": "…"} object is as common a paste
        # as a proper cookie array, and unambiguous as long as it has no
        # "name" key of its own.
        if "name" not in parsed and all(isinstance(v, str) for v in parsed.values()):
            return {k: v.strip() for k, v in parsed.items() if _NAME_RE.match(k)}
        parsed = [parsed]
    if not isinstance(parsed, list):
        return {}
    out = {}
    for c in parsed:
        if not isinstance(c, dict):
            continue
        name, value = c.get("name"), c.get("value")
        if not name or value is None or not _domain_ok(c.get("domain")):
            continue
        name = str(name).strip()
        if _NAME_RE.match(name):
            out[name] = str(value).strip()
    return out


def _from_table(raw):
    """Netscape cookies.txt rows, or the columns DevTools copies."""
    out = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 7 and cols[1].strip().upper() in ("TRUE", "FALSE"):
            domain, name, value = cols[0], cols[5], cols[6]
        elif len(cols) >= 2:
            # DevTools' table pastes as Name, Value, Domain, Path, …
            domain = cols[2] if len(cols) > 2 else ""
            name, value = cols[0], cols[1]
        else:
            continue
        name, value = name.strip(), value.strip()
        if not _NAME_RE.match(name) or not _domain_ok(domain):
            continue
        out[name] = value
    return out


def _from_text(raw):
    raw = _HEADER_PREFIX_RE.sub("", raw.strip())
    out = {}
    for chunk in re.split(r"[;\n\r]+", raw):
        chunk = chunk.strip().strip(",")
        if not chunk or chunk.startswith("#"):
            continue
        match = _PAIR_RE.match(chunk)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        # "https://x.com" splits into a name and a value that are neither.
        if not value or value.startswith("//"):
            continue
        out[name] = value
    return out


def problems(pairs, pasted_something):
    """Why these cookies cannot be used, or None if they can.

    Returns a JSON-ready dict so the caller can hand it straight to the
    browser, with a ``code`` the UI keys off to point at the right field.
    """
    if not pairs:
        if pasted_something:
            return {
                "code": "cookies_unreadable",
                "error": (
                    "Those cookies could not be read. Paste either "
                    "auth_token and ct0 into the two boxes, or a whole "
                    "cookies.txt / JSON export from a cookie extension."
                ),
            }
        return {
            "code": "cookies_missing",
            "error": (
                "X/Twitter needs a logged-in session. Open 'Login cookies' "
                "and paste auth_token and ct0 from x.com."
            ),
        }

    missing = [name for name in REQUIRED if not pairs.get(name)]
    if missing:
        found = ", ".join(sorted(pairs)) or "nothing usable"
        which = " and ".join(missing)
        why = (
            " ct0 is the CSRF token X checks on every call, so a session "
            "without it is refused even when auth_token is right."
            if "ct0" in missing else ""
        )
        return {
            "code": "cookies_incomplete",
            "error": f"Missing {which}. Found: {found}.{why}",
            "found": sorted(pairs),
        }
    return None


def shape_warnings(pairs):
    """Values that parsed fine but do not look like what X hands out."""
    notes = []
    for name, pattern in _SHAPES.items():
        value = pairs.get(name)
        if value and not pattern.match(value):
            notes.append(
                f"{name} does not look like a value X hands out — it should "
                f"be plain hex, and this is {len(value)} characters of "
                "something else. Check for a stray quote, a space, or a "
                "copied label."
            )
    return notes


def write_file(pairs):
    """Write the Netscape cookie file gallery-dl reads. Caller deletes it."""
    lines = ["# Netscape HTTP Cookie File"]
    for domain in (".x.com", ".twitter.com"):
        for name, value in pairs.items():
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t2000000000\t{name}\t{value}")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="twcookies_", delete=False
    )
    tmp.write("\n".join(lines) + "\n")
    tmp.close()
    return tmp.name


def check_session(pairs, timeout=20):
    """Ask X whether this session is still logged in.

    One cheap call that separates the things a failed scan cannot tell apart
    on its own: cookies that expired, an account X has locked or suspended,
    and a scan that failed for some reason other than login.

    When X will not answer the question — a retired endpoint, a network the
    server cannot cross — the result is "inconclusive", never a failure. A
    check that cannot see the session has learned nothing about it, and
    saying otherwise contradicts scans that are working fine.
    """
    headers = {
        "Authorization": f"Bearer {WEB_BEARER}",
        "x-csrf-token": pairs.get("ct0", ""),
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "User-Agent": UA["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    }
    jar = dict(pairs)
    tried, reached = [], False
    for url in VERIFY_URLS:
        try:
            resp = requests.get(url, headers=headers, cookies=jar, timeout=timeout)
        except requests.RequestException as exc:
            tried.append(f"{_host(url)}: {str(exc)[:80]}")
            continue
        reached = True
        if resp.status_code in DECISIVE:
            return _verdict(resp)
        tried.append(f"{_host(url)}: HTTP {resp.status_code} {_first_message(resp)}")
    return {
        "ok": False,
        "code": "inconclusive",
        "message": (
            "X would not answer the check, so this says nothing about your "
            "cookies either way — a scan that works is the real proof they "
            "are fine. X retires these endpoints from time to time; that is "
            "a fault in the check, not in your login."
            if reached else
            "This server could not reach X at all, so the check learned "
            "nothing about your cookies. That is the network where the app "
            "runs — if your scans work, they work."
        ),
        "detail": " | ".join(tried)[:400],
    }


def _host(url):
    return url.split("/")[2]


def _first_message(resp):
    _, messages = _api_errors(resp)
    return messages[0] if messages else ""


def _verdict(resp):
    if resp.status_code == 200:
        try:
            me = resp.json()
        except ValueError:
            me = {}
        handle = me.get("screen_name") or ""
        return {
            "ok": True,
            "code": "ok",
            "screen_name": handle,
            "message": (
                f"Signed in as @{handle}. These cookies work — if a scan still "
                "finds nothing, the problem is with that account, not your login."
                if handle else "Signed in. These cookies work."
            ),
        }

    api_codes, api_msgs = _api_errors(resp)

    if resp.status_code == 401:
        return {
            "ok": False,
            "code": "expired",
            "message": (
                "X rejected this session (401). The cookies have expired or "
                "were invalidated — log in to x.com again in your browser and "
                "copy a fresh auth_token and ct0. Logging out anywhere kills "
                "the old ones. You do not need a different account for this."
            ),
            "detail": "; ".join(api_msgs),
        }
    if resp.status_code == 403:
        if 326 in api_codes:
            return {
                "ok": False,
                "code": "locked",
                "message": (
                    "X has temporarily locked this account (error 326). Open "
                    "x.com in your browser, clear the challenge it shows you, "
                    "then copy the cookies again."
                ),
                "detail": "; ".join(api_msgs),
            }
        if 64 in api_codes:
            return {
                "ok": False,
                "code": "suspended",
                "message": (
                    "This account is suspended (error 64), so nothing signed "
                    "in as it can read timelines. This is the one case where "
                    "you do need another account."
                ),
                "detail": "; ".join(api_msgs),
            }
        return {
            "ok": False,
            "code": "forbidden",
            "message": (
                "X refused the session (403)."
                + (" The ct0 cookie and the CSRF header do not match (error "
                   "353)." if 353 in api_codes else "")
                + " This is usually a ct0 that no longer goes with the "
                "auth_token — copy both again in one go, from the same "
                "browser profile."
            ),
            "detail": "; ".join(api_msgs),
        }
    if resp.status_code == 429:
        return {
            "ok": False,
            "code": "rate_limited",
            "message": (
                "X is rate-limiting this account (429). The login is fine; "
                "wait about 15 minutes and try again."
            ),
            "detail": "; ".join(api_msgs),
        }
    # Only decisive statuses reach here, so this is belt and braces — and it
    # still refuses to blame the cookies for an answer it cannot read.
    return {
        "ok": False,
        "code": "inconclusive",
        "message": (
            f"X answered {resp.status_code}, which does not say whether the "
            "session is good. Trust a working scan over this."
        ),
        "detail": "; ".join(api_msgs) or resp.text[:200],
    }


def _api_errors(resp):
    """X puts the useful part in a JSON errors array; status alone is vague."""
    try:
        body = resp.json()
    except ValueError:
        return set(), []
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return set(), []
    codes, messages = set(), []
    for err in errors:
        if not isinstance(err, dict):
            continue
        try:
            codes.add(int(err.get("code")))
        except (TypeError, ValueError):
            pass
        if err.get("message"):
            messages.append(str(err["message"]))
    return codes, messages
