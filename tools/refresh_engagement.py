#!/usr/bin/env python3
"""Views, likes, comments, shares and saves for a campaign's posts.

Same hand-off as the follower counts (tools/refresh_followers.py):
creatorwise doesn't fetch Instagram or TikTok pages. The numbers are read
off each post's page in the desktop app's browser pane, one post at a time,
and loaded here:

    refresh_engagement.py --list --campaign books-announcement > posts.json
    refresh_engagement.py --load readings.json --source "browser pane, 2026-11-20"
    ... --apply

readings.json: {"<post id>": {"views": 1234, "likes": 56, "comments": 7,
"shares": 1, "saves": 3}} — a metric the page doesn't show is left out
(Instagram's website shows no views), never written as 0. A reading with
no numbers says why — {"status": "likes_hidden"} (the creator hides the
count) or {"status": "unavailable"} (gone or private) — and is kept as a
snapshot so the page can say so; the post itself stays unmeasured.

Each reading is kept as a PostSnapshot; the post holds the latest.
Dry run unless --apply.
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

METRICS = ("views", "likes", "comments", "shares", "saves")
LOG_NAME = "engagement-refreshes.log"
STATUSES = ("likes_hidden", "unavailable")


def parse_count(value):
    """'1,234' / 1234 / '12.5K' -> 1234 / 12500. None for anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").upper()
    mult = 1
    if text.endswith("K"):
        mult, text = 1000, text[:-1]
    elif text.endswith("M"):
        mult, text = 1_000_000, text[:-1]
    try:
        return int(round(float(text) * mult))
    except ValueError:
        return None


def plan(readings, posts_by_id):
    """[(post, values or None, problem)]"""
    out = []
    for key, reading in readings.items():
        post = posts_by_id.get(int(key))
        if post is None:
            out.append((None, None, f"no post {key}"))
            continue
        status = (reading or {}).get("status")
        if status in STATUSES:
            out.append((post, None, status))
            continue
        values = {m: parse_count(reading.get(m)) for m in METRICS}
        if all(v is None for v in values.values()):
            out.append((post, None, "no numbers in the reading"))
            continue
        out.append((post, values, None))
    return out


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--campaign")
    parser.add_argument("--load")
    parser.add_argument("--source")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--today")
    args = parser.parse_args(argv)

    import logging
    logging.disable(logging.WARNING)
    import app as appmod
    import media
    from models import Campaign, PlatformPost, PostSnapshot, UgcPiece, db
    logging.disable(logging.NOTSET)
    today = date.fromisoformat(args.today) if args.today else date.today()

    with appmod.app.app_context():
        db.create_all()
        if args.list:
            camp = Campaign.query.filter_by(slug=args.campaign).one()
            rows = (PlatformPost.query.join(UgcPiece).filter(UgcPiece.campaign_id == camp.id)
                    .order_by(PlatformPost.platform, PlatformPost.id).all())
            print(json.dumps([{"post": p.id, "piece": p.ugc_piece_id, "platform": p.platform,
                               "creator": p.piece.creator.name, "url": p.url,
                               "measured_at": p.measured_at.isoformat() if p.measured_at else None}
                              for p in rows], indent=1))
            return 0

        if not args.load or not args.source:
            parser.error("--load needs --source")
        readings = json.load(open(args.load))
        posts = {p.id: p for p in PlatformPost.query.all()}
        result = plan(readings, posts)
        ok = [(p, v) for p, v, problem in result if v]
        noted = [(p, problem) for p, v, problem in result if problem in STATUSES]
        for post, values, problem in result:
            if problem and problem not in STATUSES:
                print(f"  skipped {post.id if post else '?'}: {problem}")
        for status in STATUSES:
            ids = [p.id for p, s in noted if s == status]
            if ids:
                print(f"  {status}: {len(ids)} post(s) {ids}")
        skipped = len(result) - len(ok) - len(noted)
        print(f"{len(ok)} post(s) with numbers, {len(noted)} noted without; {skipped} skipped.")
        if not args.apply:
            print("(dry run — nothing written. Re-run with --apply.)")
            return 0
        for post, values in ok:
            for m in METRICS:
                setattr(post, m, values[m])
            post.measured_at = today
            db.session.add(PostSnapshot(post_id=post.id, measured_at=today, source=args.source,
                                        **values))
        for post, status in noted:
            db.session.add(PostSnapshot(post_id=post.id, measured_at=today, source=args.source,
                                        status=status))
        db.session.commit()
        data_dir = media.data_dir(appmod.app.config["SQLALCHEMY_DATABASE_URI"])
        with open(os.path.join(data_dir, LOG_NAME), "a") as log:
            log.write(f"{today} posts={len(ok)} noted={len(noted)} skipped={skipped} "
                      f"source={args.source}\n")
        print(f"-> {len(ok)} post(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
