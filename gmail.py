"""Read erin@readwise.io directly from the Gmail API.

Why this exists: the review queue was originally fed by a periodic Claude job
using the Superhuman connector. That can't work — connectors are bound to an
interactive Claude session, and a scheduled cloud job has no path to one. Two
cloud sessions confirmed it by producing no output at all.

So the app reads mail itself. Three env vars, one long-lived refresh token:

    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN

With any of them missing the scanner stays dormant — `is_configured()` is false
and nothing here is ever called. See docs/gmail-setup.md for how to mint them.

Deliberately stdlib-only (urllib, not requests, not google-api-python-client):
the whole surface we need is two GETs and a POST, and a dependency that ships
its own auth, retry and discovery machinery is a lot of moving parts to carry
for that.

Read-only by construction: the token is minted with gmail.readonly, and nothing
here calls a mutating endpoint.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from email.utils import parseaddr

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Narrow the scan server-side. Gmail indexes link text, so asking for mail that
# mentions a platform domain removes almost everything before it reaches us,
# and the link extraction in inbox.py decides the rest. The date window overlaps
# deliberately: ingest is idempotent on message_id, so re-seeing a message is
# free and a missed day heals itself on the next run.
DEFAULT_QUERY = (
    "newer_than:3d -in:spam -in:trash "
    "(instagram.com OR tiktok.com OR youtube.com OR youtu.be)"
)

# Hard ceiling on one scan, so a weird query can't turn into thousands of calls.
MAX_MESSAGES = 150

TIMEOUT = 30

# contextone rejects a blank env value ("Values must not contain blank values"),
# so app.yml can't reference a variable that doesn't exist yet without risking
# the deploy. The three GMAIL_* variables are therefore seeded with this
# placeholder, and it reads here as "not set" — which keeps the scanner dormant
# until real credentials replace it.
PLACEHOLDER = "not-configured"


class GmailError(RuntimeError):
    """Anything that went wrong talking to Google. Carries a readable reason."""


# --- configuration ------------------------------------------------------------

def _env(name):
    value = (os.environ.get(name) or "").strip()
    return "" if value == PLACEHOLDER else value


def is_configured():
    return all(
        _env(k)
        for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    )


def query():
    return _env("GMAIL_QUERY") or DEFAULT_QUERY


# --- HTTP ---------------------------------------------------------------------

def _request(url, data=None, headers=None, method=None):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method or ("POST" if body else "GET")
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:
            pass
        raise GmailError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).path}: {detail}")
    except urllib.error.URLError as exc:
        raise GmailError(f"could not reach Google: {exc.reason}")


# Access tokens last an hour. Cache in-process so a scan is one token exchange,
# not one per message.
_token_cache = {"value": None, "expires_at": 0.0}


def access_token(force=False):
    now = time.time()
    if not force and _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    if not is_configured():
        raise GmailError("Gmail is not configured (missing GMAIL_* env vars)")

    payload = _request(
        TOKEN_URL,
        data={
            "client_id": _env("GMAIL_CLIENT_ID"),
            "client_secret": _env("GMAIL_CLIENT_SECRET"),
            "refresh_token": _env("GMAIL_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
    )
    token = payload.get("access_token")
    if not token:
        raise GmailError("token response had no access_token")

    # Expire a minute early rather than racing the boundary mid-scan.
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + int(payload.get("expires_in", 3600)) - 60
    return token


def _api(path, params=None):
    url = API_ROOT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url, headers={"Authorization": f"Bearer {access_token()}"})


# --- message parsing ----------------------------------------------------------

def _b64(data):
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:
        return ""


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")


def strip_html(html):
    """Crude but adequate: we only need the URLs and enough text to match a handle.

    href values are kept as visible text, because plenty of creator mail puts the
    post link only in the anchor and uses "my reel" as the label.
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(
        r"""(?i)<a\b[^>]*href=["']([^"']+)["'][^>]*>""", r" \1 ", text
    )
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = _TAG.sub(" ", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return _WS.sub("\n", text).strip()


def _walk(part):
    """Yield every part of a MIME tree, parents included."""
    if not part:
        return
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def extract_body(payload):
    """Best available text for a message: text/plain if present, else HTML stripped."""
    plain, html = [], []
    for part in _walk(payload):
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(_b64(data))
        elif mime == "text/html":
            html.append(_b64(data))

    if plain:
        return "\n".join(t for t in plain if t.strip())
    return "\n".join(strip_html(t) for t in html if t.strip())


def _header(payload, name):
    for h in (payload or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def parse_message(msg):
    """Turn a Gmail `messages.get` response into the shape inbox.ingest_message wants."""
    payload = msg.get("payload") or {}
    _, addr = parseaddr(_header(payload, "From"))

    received = None
    internal = msg.get("internalDate")
    if internal:
        try:
            received = datetime.fromtimestamp(
                int(internal) / 1000, tz=timezone.utc
            ).date()
        except (ValueError, OSError):
            received = None
    if received is None:
        received = date.today()

    return {
        # Gmail's own immutable id, so re-scanning the same mail is a no-op.
        "message_id": msg.get("id"),
        "sender": addr or _header(payload, "From"),
        "subject": _header(payload, "Subject"),
        "body": extract_body(payload),
        "received_on": received,
    }


# --- the one call the scanner makes -------------------------------------------

def fetch_messages(q=None, limit=MAX_MESSAGES):
    """Return parsed messages matching `q`, newest first.

    Raises GmailError on any auth or transport failure — the caller records that
    on the scan run rather than letting it pass silently.
    """
    q = q or query()
    limit = min(int(limit or MAX_MESSAGES), MAX_MESSAGES)

    ids, page = [], None
    while len(ids) < limit:
        params = {"q": q, "maxResults": min(100, limit - len(ids))}
        if page:
            params["pageToken"] = page
        batch = _api("/messages", params)
        ids.extend(m["id"] for m in batch.get("messages") or [])
        page = batch.get("nextPageToken")
        if not page:
            break

    out = []
    for mid in ids[:limit]:
        out.append(parse_message(_api(f"/messages/{mid}", {"format": "full"})))
    return out


def check():
    """Prove the credentials work, without reading any mail. Returns the address."""
    profile = _api("/profile")
    return profile.get("emailAddress") or "(unknown)"
