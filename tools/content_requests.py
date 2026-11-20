#!/usr/bin/env python3
"""Add content that Erin emails in.

Erin emails a post link with a line saying whose it is and for which campaign
("Bea's second piece for the Announcement") to her own address with a
+creatorwise tag. A scheduled Claude task on her Mac reads that mail with the
Gmail connector and drives this tool; the media capture in between happens in
the desktop app's browser pane (see docs/content-by-email.md). This runs on
the Mac because the local database is the source of truth — anything written
on the server is replaced by the next tools/push_data.py.

The task hands over the mail as a JSON file:

    {"messages": [{"id": "...", "from": "...", "date": "2026-11-19",
                   "subject": "...", "body": "...",
                   "attachments": ["/abs/path/clip.mp4", ...]}],
     "links": {"<url as sent>": {"author": "handle", "kind": "video",
                                 "resolved": "https://..."}}}

`links` is what the task read off each post page in the browser pane: the
author's handle, whether it is a video or a carousel, and where a short link
went. Subcommands:

    links  FILE --sender ADDR       the links still to look up
    plan   FILE --sender ADDR       dry run: what each email would add
    apply  FILE --sender ADDR --out PLAN.json
                                    add the pieces, make their drop folders,
                                    and write what each one needs captured
    finish --pieces 201,202         titles from transcripts; summary lines
    pending                         emailed pieces still without media, with
                                    their drop folder and capture method

Like the importers: a creator is never created, nothing is guessed. An email
that can't be tied to one creator and one campaign is held and reported, and
the creator's handle on the post outranks a first name in the description.
"""

import json
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from content_importer import handle_in, tiktok_posted_on  # noqa: E402
from inbox import extract_links  # noqa: E402

STATE_NAME = "content-requests.json"
LOG_NAME = "content-requests.log"

# What a post page can be, as the task reports it.
KINDS = ("video", "carousel")


# --- pure helpers -------------------------------------------------------------

_IG_CODE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)
_TT_ID = re.compile(r"tiktok\.com/@[^/?#]+/(?:video|photo)/(\d{15,21})", re.I)
_YT_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})", re.I)


def post_key(url):
    """(platform, id) naming one post, whichever form its URL takes.

    Instagram's /p/, /reel/ and /<handle>/reel/ forms name the same post by
    shortcode; TikTok's by the numeric id. A TikTok /t/ shortlink carries no
    id, so it has no key until it has been resolved.
    """
    u = url or ""
    for platform, pattern in (("instagram", _IG_CODE), ("tiktok", _TT_ID), ("youtube", _YT_ID)):
        m = pattern.search(u)
        if m:
            return platform, m.group(1)
    return None


def canonical(url):
    """The URL to store: no query string or fragment, no trailing junk."""
    u = (url or "").split("#")[0]
    if "youtube.com/watch" in u.lower():
        m = _YT_ID.search(u)
        return f"https://www.youtube.com/watch?v={m.group(1)}" if m else u
    return u.split("?")[0]


def platform_of(url):
    u = (url or "").lower()
    if "instagram.com" in u:
        return "instagram"
    if "tiktok.com" in u:
        return "tiktok"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return None


_WORD = re.compile(r"[a-z0-9]+")
_LINKISH = re.compile(r"https?://\S+", re.I)


def describe(message):
    """The words of an email, links taken out."""
    text = f"{message.get('subject') or ''}\n{message.get('body') or ''}"
    text = _LINKISH.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def words(text):
    # "Bea's" -> "bea": a possessive is the commonest way a name appears.
    return _WORD.findall(re.sub(r"['’]s\b", "", (text or "").lower()))


# Words in campaign names that say nothing about which campaign is meant.
_GENERIC = {"readwise", "books", "book", "bookwise", "campaign", "the", "and",
            "for", "of", "beta", "launch"}


def campaign_keywords(campaign):
    found = set(words(campaign.name)) | set(words(campaign.slug.replace("-", " ")))
    # "Softlaunch" is written "soft launch" too: match on letters only.
    keys = {w for w in found if w not in _GENERIC and len(w) >= 5}
    return keys or {re.sub(r"[^a-z0-9]", "", campaign.name.lower())}


def find_campaign(text, campaigns):
    """(campaign, None) or (None, reason). Named in the email, or held."""
    compact = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    scored = []
    for c in campaigns:
        score = sum(1 for k in campaign_keywords(c) if k in compact)
        if score:
            scored.append((score, c))
    if not scored:
        return None, "no campaign named — say which (e.g. \"for the Announcement\")"
    best = max(s for s, _ in scored)
    top = [c for s, c in scored if s == best]
    if len(top) > 1:
        return None, "names more than one campaign: " + ", ".join(c.name for c in top)
    return top[0], None


def _norm_handle(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower().lstrip("@"))


def creators_by_handle(handle, platform, creators):
    """Creators holding `handle` on `platform` (punctuation-insensitive)."""
    want = _norm_handle(handle)
    if not want:
        return set()
    out = set()
    for c in creators:
        for h in c.handles:
            if h.platform == platform and _norm_handle(h.handle) == want:
                out.add(c.id)
    return out


def creators_by_name(text, creators):
    """Creators whose first name (or full name) appears in the text.

    Returns (full_name_ids, first_name_ids). Full names win; a first name is
    only good when it is unique among the creators passed in.
    """
    tokens = words(text)
    joined = " " + " ".join(tokens) + " "
    full, first = set(), set()
    for c in creators:
        parts = words(c.name)
        if not parts:
            continue
        if len(parts) > 1 and f" {' '.join(parts)} " in joined:
            full.add(c.id)
        if len(parts[0]) >= 3 and parts[0] in tokens:
            first.add(c.id)
    return full, first


def group_links(links):
    """Links -> pieces. One per platform across several platforms is one
    cross-posted piece; anything else is a piece per link (the importer's
    rule: two links on one platform are two videos)."""
    platforms = [p for p, _ in links]
    if len(links) > 1 and len(set(platforms)) == len(platforms):
        return [links]
    return [[link] for link in links]


# --- the plan ---------------------------------------------------------------

def load_state(data_dir):
    path = os.path.join(data_dir, STATE_NAME)
    if os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    return {"messages": {}}


def save_state(data_dir, state):
    path = os.path.join(data_dir, STATE_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


# The job answers held emails by replying in their thread. Those replies sit
# in the same thread, from the same sender, so they carry this line and go to
# the untagged address — and are never read as a request themselves.
JOB_MARK = "[creatorwise job]"
TAG = "+creatorwise@"


def pending_messages(payload, sender, state):
    """(to_handle, ignored): unprocessed mail from the sender, oldest first.

    `ignored` holds (message, why). Mail from anyone else, the job's own
    replies, and anything in a thread not addressed to the tagged address
    (when the task passed "to") are skipped, and remembered as skipped.
    """
    sender = sender.strip().lower()
    todo, ignored = [], []
    for m in sorted(payload.get("messages", []), key=lambda m: m.get("date") or ""):
        if m.get("id") in state["messages"]:
            continue
        frm = re.search(r"[\w.+-]+@[\w.-]+", (m.get("from") or "").lower())
        to = " ".join(m.get("to") or []).lower() if isinstance(m.get("to"), list) \
            else (m.get("to") or "").lower()
        if JOB_MARK in (m.get("body") or "") or JOB_MARK in (m.get("subject") or ""):
            ignored.append((m, "the job's own reply"))
        elif not frm or frm.group(0) != sender:
            ignored.append((m, "not from the sender"))
        elif m.get("to") is not None and TAG not in to:
            ignored.append((m, "not sent to the tagged address"))
        else:
            todo.append(m)
    return todo, ignored


def existing_posts(PlatformPost):
    out = {}
    for post in PlatformPost.query.all():
        key = post_key(post.url)
        if key:
            out[key] = post
    return out


def link_info(payload, url):
    return (payload.get("links") or {}).get(url) or {}


def lookups_needed(payload, sender, state, existing):
    todo, _ = pending_messages(payload, sender, state)
    out = []
    for m in todo:
        for platform, url in extract_links(f"{m.get('subject') or ''}\n{m.get('body') or ''}"):
            key = post_key(url)
            if key and key in existing:
                continue
            if url not in out:
                out.append(url)
    return out


def plan(payload, sender, state, models):
    """What each pending email would do. Pure apart from reading the DB."""
    Campaign, Creator, PlatformPost = models["Campaign"], models["Creator"], models["PlatformPost"]
    campaigns = Campaign.query.order_by(Campaign.id).all()
    creators = Creator.query.all()
    by_id = {c.id: c for c in creators}
    existing = existing_posts(PlatformPost)
    todo, ignored = pending_messages(payload, sender, state)

    results = []
    for m in todo:
        text = describe(m)
        raw = extract_links(f"{m.get('subject') or ''}\n{m.get('body') or ''}")
        entry = {"message": m.get("id"), "date": m.get("date"), "description": text,
                 "attachments": list(m.get("attachments") or []),
                 "pieces": [], "held": []}
        results.append(entry)
        if not raw:
            entry["held"].append({"reason": "no Instagram, TikTok or YouTube post link in it"})
            continue
        campaign, why = find_campaign(text, campaigns)
        if campaign is None:
            entry["held"].append({"reason": why, "links": [u for _, u in raw]})
            continue

        links = []
        for platform, url in raw:
            info = link_info(payload, url)
            resolved = info.get("resolved") or url
            links.append({"platform": platform_of(resolved) or platform, "url": url,
                          "store": canonical(resolved), "key": post_key(resolved),
                          "author": info.get("author"), "kind": info.get("kind"),
                          "unavailable": info.get("unavailable")})

        participants = {p.creator_id for p in campaign.participations}
        for group in group_links([(l["platform"], l) for l in links]):
            group = [l for _, l in group]
            urls = [l["url"] for l in group]
            gone = [l for l in group if l.get("unavailable")]
            if gone:
                entry["held"].append({"reason": "the post page says it is unavailable",
                                      "links": [l["url"] for l in gone]})
                continue
            unkeyed = [l for l in group if not l["key"]]
            if unkeyed:
                entry["held"].append({"reason": "couldn't read the post's id from the link "
                                      "(a short link that wasn't opened?)", "links": urls})
                continue

            # Who made it: the handle on the post, then the email's words.
            handle_ids = set()
            for l in group:
                handle = l["author"] or handle_in(l["store"])
                handle_ids |= creators_by_handle(handle, l["platform"], creators)
            full, first = creators_by_name(text, creators)
            named = full or {i for i in first if i in participants} or first
            if len(handle_ids) == 1:
                cid = next(iter(handle_ids))
                if named and cid not in named and cid not in first:
                    who = ", ".join(sorted(by_id[i].name for i in named))
                    entry["held"].append({
                        "reason": f"the post is by {by_id[cid].name} but the email names {who}",
                        "links": urls})
                    continue
                matched_by = "handle"
            elif len(handle_ids) > 1:
                pick = handle_ids & named
                if len(pick) != 1:
                    who = ", ".join(sorted(by_id[i].name for i in handle_ids))
                    entry["held"].append({
                        "reason": f"the post's account belongs to more than one creator ({who}) "
                                  "— name which one", "links": urls})
                    continue
                cid, matched_by = next(iter(pick)), "handle + name"
            else:
                if len(named) != 1:
                    if not named:
                        reason = ("no creator on file has this account, and the email "
                                  "names nobody we know")
                    else:
                        reason = "the name fits more than one creator: " + ", ".join(
                            sorted(by_id[i].name for i in named))
                    handles = sorted({l["author"] for l in group if l["author"]})
                    if handles:
                        reason += f" (post by @{', @'.join(handles)})"
                    entry["held"].append({"reason": reason, "links": urls})
                    continue
                cid, matched_by = next(iter(named)), "name only"

            on_file = [existing[l["key"]] for l in group if l["key"] in existing]
            new = [l for l in group if l["key"] not in existing]
            if not new:
                entry["pieces"].append({"action": "already on file", "creator": cid,
                                        "piece": on_file[0].ugc_piece_id, "links": urls})
                continue
            target = None
            if on_file:
                piece = on_file[0].piece
                if piece.creator_id != cid:
                    entry["held"].append({
                        "reason": f"part of this is already on file as piece {piece.id}, "
                                  f"credited to {piece.creator.name}", "links": urls})
                    continue
                if any(p.platform == l["platform"] for p in piece.posts for l in new):
                    entry["held"].append({
                        "reason": f"piece {piece.id} already has a post on that platform",
                        "links": urls})
                    continue
                target = piece.id
            dates = [d for d in (tiktok_posted_on(l["store"]) for l in new) if d]
            entry["pieces"].append({
                "action": "add cross-post" if target else "new piece",
                "piece": target, "creator": cid, "creator_name": by_id[cid].name,
                "matched_by": matched_by, "campaign": campaign.slug,
                "campaign_name": campaign.name,
                "posts": [{"platform": l["platform"], "url": l["store"], "kind": l["kind"]}
                          for l in new],
                "posted_on": min(dates).isoformat() if dates else None,
                "links": urls})

        if len([p for p in entry["pieces"] if p["action"] == "new piece"]) != 1:
            # Files attached to an email with several new pieces could be
            # anybody's: not guessed.
            if entry["attachments"] and entry["pieces"]:
                entry["attachments_unused"] = True
    return results, ignored


def capture_method(posts, has_attachments):
    """How the media for a new piece is to be got, in order of preference."""
    if has_attachments:
        return "attachment"
    by = {p["platform"]: p for p in posts}
    if "tiktok" in by:
        return "tiktok-photos" if by["tiktok"].get("kind") == "carousel" or \
            "/photo/" in by["tiktok"]["url"] else "tiktok-download"
    if "instagram" in by:
        return "instagram-slides" if by["instagram"].get("kind") == "carousel" \
            else "instagram-record"
    if "youtube" in by:
        return "youtube-transcript"
    return "none"


def apply(results, models, media_inbox_root, today=None):
    """Write the plan. Returns the capture list for the task."""
    from tools import import_media
    db = models["db"]
    Campaign, Creator, UgcPiece = models["Campaign"], models["Creator"], models["UgcPiece"]
    PlatformPost, Participation = models["PlatformPost"], models["Participation"]
    today = today or date.today()

    captures = []
    touched = set()
    for entry in results:
        for p in entry["pieces"]:
            if p["action"] == "already on file":
                continue
            campaign = Campaign.query.filter_by(slug=p["campaign"]).one()
            creator = db.session.get(Creator, p["creator"])
            if p["action"] == "add cross-post":
                piece = db.session.get(UgcPiece, p["piece"])
            else:
                piece = UgcPiece(
                    creator_id=creator.id, campaign_id=campaign.id,
                    posted_on=date.fromisoformat(p["posted_on"]) if p["posted_on"] else None,
                    discovered_via="email",
                    notes=f"Emailed in {entry['date'] or today.isoformat()}: {entry['description']}")
                db.session.add(piece)
                db.session.flush()
            for post in p["posts"]:
                posted = tiktok_posted_on(post["url"])
                piece.posts.append(PlatformPost(platform=post["platform"], url=post["url"],
                                                posted_on=posted))
                if posted and (piece.posted_on is None or posted < piece.posted_on):
                    piece.posted_on = posted
            p["piece"] = piece.id

            part = Participation.query.filter_by(creator_id=creator.id,
                                                 campaign_id=campaign.id).first()
            if part is None:
                db.session.add(Participation(creator_id=creator.id, campaign_id=campaign.id,
                                             status="content_delivered", opted_in_on=today))
                p["status_change"] = "added to the campaign as delivered"
            elif part.status != "content_delivered":
                p["status_change"] = f"{part.status} -> content_delivered"
                part.status = "content_delivered"
            touched.add(campaign.slug)

            if p["action"] == "new piece":
                attachments = [] if entry.get("attachments_unused") else entry["attachments"]
                captures.append({
                    "piece": piece.id, "creator": creator.name, "campaign": campaign.slug,
                    "method": capture_method(p["posts"], bool(attachments)),
                    "posts": p["posts"], "attachments": attachments})
    db.session.commit()

    # Drop folders, the same ones tools/import_media.py files from.
    for slug in touched:
        import_media.prepare(Campaign.query.filter_by(slug=slug).one(), media_inbox_root)
    for c in captures:
        base = os.path.join(media_inbox_root, c["campaign"])
        name = next((n for n in os.listdir(base) if import_media.piece_id_of(n) == c["piece"]), None)
        c["folder"] = os.path.join(base, name) if name else None
    return captures


def record_processed(data_dir, results, ignored, state):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for entry in results:
        state["messages"][entry["message"]] = {
            "on": stamp,
            "pieces": [p.get("piece") for p in entry["pieces"]],
            "held": [h["reason"] for h in entry["held"]]}
    for m, why in ignored:
        state["messages"][m.get("id")] = {"on": stamp, "ignored": why}
    save_state(data_dir, state)
    added = sum(1 for e in results for p in e["pieces"] if p["action"] != "already on file")
    held = sum(len(e["held"]) for e in results)
    with open(os.path.join(data_dir, LOG_NAME), "a") as handle:
        handle.write(f"{stamp} emails={len(results)} added={added} held={held} "
                     f"ignored={len(ignored)}\n")


# --- titles -----------------------------------------------------------------

_GREETING = re.compile(r"^(hi|hey|hello|okay|ok|so|um|uh|welcome)\b", re.I)
_MARKER = re.compile(r"^\[.*\]$")
_TOPIC = re.compile(r"\b(read\w*|books?|app|readwise|tracker|tracking)\b", re.I)


def sentences(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or _MARKER.match(line) or line.lower().startswith("on-screen text"):
            continue
        out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip())
    return out


def pick_title(transcript):
    """A phrase lifted verbatim from the transcript, in curly quotes.

    The hand-picked titles were a line that says what the video is about. The
    nearest mechanical thing: the first sentence of a sensible length that
    isn't a greeting. None when there is nothing usable — the card then keeps
    its platform label, as before.
    """
    fit = [s for s in sentences(transcript)[:12]
           if 6 <= len(s.split()) <= 18 and not _GREETING.match(s)]
    if not fit:
        return None
    # Prefer an early line about the subject over scene-setting.
    on_topic = [s for s in fit if _TOPIC.search(s)]
    s = (on_topic or fit)[0].rstrip(" .,;:—-")
    return f"“{s}”"


def finish(piece_ids, models, media_root):
    db, UgcPiece = models["db"], models["UgcPiece"]
    lines = []
    for pid in piece_ids:
        piece = db.session.get(UgcPiece, pid)
        if piece is None:
            continue
        path = os.path.join(media_root, str(pid), "transcript.txt")
        transcript = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if not piece.title:
            piece.title = pick_title(transcript)
        has = sorted(n for n in os.listdir(os.path.join(media_root, str(pid)))) \
            if os.path.isdir(os.path.join(media_root, str(pid))) else []
        media = ("video" if "video.mp4" in has
                 else f"{sum(n.startswith('image-') for n in has)} slides"
                 if any(n.startswith("image-") for n in has)
                 else "transcript only" if "transcript.txt" in has else "no media")
        lines.append(f"{pid}  {piece.creator.name} — {piece.campaign.name}: {media}; "
                     f"title {piece.title or '(platform label)'}; hook {piece.hook or '—'}")
    db.session.commit()
    return lines


def pending(models, media_root, inbox_root):
    """Emailed pieces whose media never arrived — typically a reel that had
    to wait because the browser pane wasn't on screen to record it."""
    from tools import import_media
    UgcPiece = models["UgcPiece"]
    out = []
    for piece in UgcPiece.query.filter_by(discovered_via="email").order_by(UgcPiece.id):
        if os.path.isdir(os.path.join(media_root, str(piece.id))):
            continue
        base = os.path.join(inbox_root, piece.campaign.slug)
        name = next((n for n in os.listdir(base) if import_media.piece_id_of(n) == piece.id),
                    None) if os.path.isdir(base) else None
        posts = [{"platform": p.platform, "url": p.url,
                  "kind": "carousel" if "/photo/" in (p.url or "") else None}
                 for p in piece.posts]
        out.append({"piece": piece.id, "creator": piece.creator.name,
                    "campaign": piece.campaign.slug, "method": capture_method(posts, False),
                    "posts": posts, "attachments": [],
                    "folder": os.path.join(base, name) if name else None})
    return out


# --- CLI ----------------------------------------------------------------------

def render(results, ignored):
    out = []
    for e in results:
        out.append(f"Email {e['date'] or ''}: {e['description'][:90]}")
        for p in e["pieces"]:
            if p["action"] == "already on file":
                out.append(f"  already on file as piece {p['piece']}: {' '.join(p['links'])}")
            else:
                what = "new piece" if p["action"] == "new piece" else f"cross-post on piece {p['piece']}"
                out.append(f"  {what}: {p['creator_name']} ({p['matched_by']}) — "
                           f"{p['campaign_name']} — "
                           + ", ".join(f"{x['platform']} {x['kind'] or ''}".strip() for x in p["posts"]))
        for h in e["held"]:
            out.append(f"  HELD: {h['reason']}" + (f" — {' '.join(h.get('links', []))}"
                                                  if h.get("links") else ""))
        if e.get("attachments_unused"):
            out.append("  attachments not used: more than one new piece in this email")
    if ignored:
        reasons = sorted({why for _, why in ignored})
        out.append(f"Skipped {len(ignored)} message(s): " + "; ".join(reasons) + ".")
    if not results and not ignored:
        out.append("No new emails.")
    return "\n".join(out)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["links", "plan", "apply", "finish", "pending"])
    parser.add_argument("file", nargs="?")
    parser.add_argument("--sender", help="only mail from this address is acted on")
    parser.add_argument("--out", help="apply: where to write the capture list")
    parser.add_argument("--pieces", help="finish: comma-separated piece ids")
    args = parser.parse_args(argv)

    import app as appmod
    import media
    from models import Campaign, Creator, Participation, PlatformPost, UgcPiece, db
    models = dict(db=db, Campaign=Campaign, Creator=Creator, Participation=Participation,
                  PlatformPost=PlatformPost, UgcPiece=UgcPiece)
    uri = appmod.app.config["SQLALCHEMY_DATABASE_URI"]
    data_dir = media.data_dir(uri)

    with appmod.app.app_context():
        if args.command == "finish":
            ids = [int(x) for x in (args.pieces or "").split(",") if x.strip()]
            for line in finish(ids, models, media.directory(uri)):
                print(line)
            return 0
        if args.command == "pending":
            print(json.dumps({"captures": pending(models, media.directory(uri), media.inbox(uri))},
                             indent=1))
            return 0
        if not args.file or not args.sender:
            parser.error("links/plan/apply need FILE and --sender")
        with open(args.file) as handle:
            payload = json.load(handle)
        state = load_state(data_dir)
        if args.command == "links":
            for url in lookups_needed(payload, args.sender, state, existing_posts(PlatformPost)):
                print(url)
            return 0
        results, ignored = plan(payload, args.sender, state, models)
        print(render(results, ignored))
        if args.command == "plan":
            print("\n(dry run — nothing written. Re-run with apply.)")
            return 0
        if not args.out:
            parser.error("apply needs --out")
        backup = os.path.join(data_dir, "creatorwise.db.bak-content")
        db_path = uri.replace("sqlite:///", "", 1)
        if os.path.exists(db_path):
            import shutil
            shutil.copyfile(db_path, backup)
        captures = apply(results, models, media.inbox(uri))
        for entry in results:
            for p in entry["pieces"]:
                if p.get("status_change"):
                    print(f"  status: {p['creator_name']} in {p['campaign_name']}: "
                          f"{p['status_change']}")
        record_processed(data_dir, results, ignored, state)
        with open(args.out, "w") as handle:
            json.dump({"captures": captures}, handle, indent=1)
        print(f"\n-> {len(captures)} new piece(s); capture list in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
