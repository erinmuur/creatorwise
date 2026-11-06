"""Turn creator emails into reviewable dashboard entries.

The app can't read email itself — the Superhuman connector is a Claude-side
tool, not an API this container can call. So the flow is:

    periodic Claude job (has the connector)
        -> scans erin@readwise.io for creator emails
        -> POSTs {sender, subject, body, ...} to /api/ingest
        -> this module extracts links, matches a creator, queues for review
        -> Erin accepts in /inbox, which creates the UgcPiece + PlatformPosts

Everything here is pure except `ingest_message`, so the parsing and matching can
be tested without a database or a live inbox.
"""

import json
import re
from datetime import date

from models import (
    Campaign,
    Creator,
    InboxItem,
    PlatformPost,
    SocialHandle,
    UgcPiece,
    db,
)

# Platform URL patterns. Deliberately permissive about the path — creators paste
# share links, mobile links, and links with tracking junk attached.
# Brackets are excluded as well as quotes and parens: creator mail often arrives
# as markdown-ish `[https://...](link_0)`, and without excluding `]` and `(` the
# wrapper gets swallowed into the URL.
_URL_TAIL = r"[^\s<>\"'()\[\]]+"

LINK_PATTERNS = [
    ("instagram", re.compile(r"https?://(?:www\.)?instagram\.com/" + _URL_TAIL, re.I)),
    ("tiktok", re.compile(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/" + _URL_TAIL, re.I)),
    ("youtube", re.compile(r"https?://(?:(?:www\.|m\.)?youtube\.com|youtu\.be)/" + _URL_TAIL, re.I)),
]

# Trailing punctuation that commonly rides along when a link ends a sentence.
_TRAILING = ".,;:!?)]}>\"'"

# Senders whose mail is never a content submission. Platform notification
# addresses are the important ones: TikTok alone sends a steady stream of
# "X posted:" / "X reposted:" / "X mentioned you" mails, every one of them
# carrying tiktok.com links. Without this they drown the review queue.
AUTOMATED_SENDER_PATTERNS = [
    re.compile(r"@service\.tiktok\.com$", re.I),
    re.compile(r"@(mail|notification|notify|noreply|no-reply|updates)\.", re.I),
    re.compile(r"^(no-?reply|donotreply|notifications?|updates?|mailer-daemon)@", re.I),
    re.compile(r"@(mail\.instagram\.com|facebookmail\.com|youtube\.com)$", re.I),
]


def is_automated_sender(sender_email):
    """True for platform notifications and other no-reply mail."""
    if not sender_email:
        return False
    addr = sender_email.strip().lower()
    return any(p.search(addr) for p in AUTOMATED_SENDER_PATTERNS)


def clean_url(url):
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    return url


def extract_links(text):
    """Find platform links in an email body.

    Returns [(platform, url)], de-duplicated, first occurrence wins. A link is
    attributed to exactly one platform — the first pattern that matches.
    """
    if not text:
        return []

    found = []
    seen = set()
    for platform, pattern in LINK_PATTERNS:
        for raw in pattern.findall(text):
            url = clean_url(raw)
            if not url or url in seen:
                continue
            # Skip bare profile/home links — they're not a piece of content.
            if _is_bare_profile(platform, url):
                continue
            seen.add(url)
            found.append((platform, url))
    return found


def _is_bare_profile(platform, url):
    """True for links that point at a profile or the site root, not a post."""
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].rstrip("/")
    if not path:
        return True
    segments = [s for s in path.split("/") if s]
    if platform == "instagram":
        # /p/<id>, /reel/<id>, /tv/<id> are content; /<handle> alone is a profile.
        return not (segments and segments[0] in {"p", "reel", "reels", "tv"})
    if platform == "tiktok":
        # /@handle/video/<id> is content; /@handle alone is a profile.
        # Short vm./vt. links are always content.
        if "tiktok.com/t/" in url.lower() or re.search(r"(vm|vt)\.tiktok\.com", url, re.I):
            return False
        return "video" not in segments
    if platform == "youtube":
        if "youtu.be" in url.lower():
            return False
        return not (segments and segments[0] in {"watch", "shorts", "v", "live"})
    return False


def match_creator(sender_email, body=None):
    """Find the creator who sent this, by email then by handle mentioned in the body.

    Returns (creator, how) where `how` explains the match for the review UI, or
    (None, reason) when nothing matched.
    """
    if sender_email:
        addr = sender_email.strip().lower()
        creator = Creator.query.filter(db.func.lower(Creator.email) == addr).first()
        if creator:
            return creator, "sender email"

    if body:
        handles = re.findall(r"@[A-Za-z0-9._]{3,30}", body)
        for h in handles:
            handle = SocialHandle.query.filter(
                db.func.lower(SocialHandle.handle) == h.lower()
            ).first()
            if handle:
                return handle.creator, f"handle {h} in message"

    return None, "no match"


def guess_campaign(received_on):
    """Pick the campaign whose run window contains the email date.

    Falls back to the single active campaign, then to the most recent one.
    """
    if received_on:
        hit = Campaign.query.filter(
            Campaign.starts_on <= received_on, Campaign.ends_on >= received_on
        ).first()
        if hit:
            return hit, "date inside campaign window"

    active = Campaign.query.filter_by(status="active").all()
    if len(active) == 1:
        return active[0], "only active campaign"

    latest = Campaign.query.order_by(Campaign.starts_on.desc()).first()
    return latest, "most recent campaign" if latest else "none"


def ingest_message(message_id, sender, subject, body, received_on=None):
    """Queue one email for review. Idempotent on message_id.

    Returns (item, created). An email with no platform links is skipped
    entirely — it's not a content submission.
    """
    existing = InboxItem.query.filter_by(message_id=message_id).first()
    if existing:
        return existing, False

    # Platform notification mail is full of content links but is never a
    # submission. Drop it before anything else.
    if is_automated_sender(sender):
        return None, False

    links = extract_links(body)
    if not links:
        return None, False

    creator, how = match_creator(sender, body)
    campaign, why = guess_campaign(received_on)

    item = InboxItem(
        message_id=message_id,
        sender=sender,
        subject=subject,
        received_on=received_on or date.today(),
        links_json=json.dumps(links),
        creator_id=creator.id if creator else None,
        match_note=how,
        campaign_id=campaign.id if campaign else None,
        campaign_note=why,
        status="pending",
    )
    db.session.add(item)
    db.session.commit()
    return item, True


def accept(item, campaign_id=None):
    """Turn a reviewed inbox item into a UgcPiece with one post per link."""
    if item.status == "accepted":
        return item.created_piece

    campaign_id = campaign_id or item.campaign_id
    if not (item.creator_id and campaign_id):
        raise ValueError("need both a creator and a campaign to accept")

    piece = UgcPiece(
        creator_id=item.creator_id,
        campaign_id=campaign_id,
        title=(item.subject or "Untitled")[:240],
        posted_on=item.received_on,
        discovered_via="email",
        notes=f"From email: {item.subject}",
    )
    db.session.add(piece)
    db.session.flush()

    for platform, url in item.links:
        db.session.add(PlatformPost(
            ugc_piece_id=piece.id,
            platform=platform,
            url=url,
            posted_on=item.received_on,
        ))

    item.status = "accepted"
    item.ugc_piece_id = piece.id
    db.session.commit()
    return piece
