#!/usr/bin/env python3
"""Record follower counts against creators' handles, and keep the history.

Where the numbers come from is deliberately not this tool's business. It
takes readings — from an official API, a data vendor's export, or a person
reading profiles — and files them. That keeps one path for every source, and
keeps creatorwise from ever fetching social profiles itself: the platforms
gate those pages against automated readers, and getting past that by
pretending to be a browser is not something this project does.

Two jobs:

    # 1. What needs checking? A worklist to hand to whatever does the fetching.
    python3 tools/refresh_followers.py --list
    python3 tools/refresh_followers.py --list --platform youtube --out todo.csv

    # 2. Load the readings that came back.
    python3 tools/refresh_followers.py --counts readings.csv --source "youtube data api"
    python3 tools/refresh_followers.py --counts readings.csv --source "..." --apply

Readings are CSV or JSON rows of: platform, handle, followers — and optionally
status ("not_found" for an account that no longer exists). Followers may be
written as 12400, "12,400" or "12.4K".

Dry run unless --apply. Every reading becomes a FollowerSnapshot, so the
dashboard can say when a number was last confirmed and by what.
"""

import csv
import io
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import clean, parse_followers  # noqa: E402
from models import PLATFORMS, FollowerSnapshot, SocialHandle, db  # noqa: E402

NOT_FOUND = {"not_found", "not found", "notfound", "missing", "gone", "404"}


def bare(handle):
    return (handle or "").strip().lstrip("@").lower()


def read_readings(text):
    """Parse CSV or JSON into a list of dicts with lowercase keys."""
    text = (text or "").strip()
    if not text:
        return []
    if text[0] in "[{":
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("readings", [])
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    return [
        {str(k).strip().lower(): v for k, v in row.items() if k is not None}
        for row in rows
    ]


def worklist(platform=None):
    """Every handle on a measured platform, with what we last knew."""
    query = SocialHandle.query.filter(SocialHandle.platform.in_(PLATFORMS))
    if platform:
        query = query.filter_by(platform=platform)
    out = []
    for handle in query.order_by(SocialHandle.platform, SocialHandle.handle).all():
        out.append({
            "platform": handle.platform,
            "handle": bare(handle.handle),
            "profile_url": handle.profile_url or "",
            "followers": "" if handle.follower_count is None else handle.follower_count,
            "last_checked": handle.followers_checked_on or "",
        })
    # One account can sit on two creators' profiles (a duo sharing a channel).
    # It is still one account to look up.
    seen, unique = set(), []
    for row in out:
        key = (row["platform"], row["handle"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def plan(readings):
    """Tie each reading to the handle rows it belongs to. Touches nothing."""
    by_key = {}
    for handle in SocialHandle.query.all():
        by_key.setdefault((handle.platform, bare(handle.handle)), []).append(handle)

    result = {
        "changed": [], "first": [], "same": [], "not_found": [],
        "unknown": [], "unreadable": [],
    }
    seen = set()
    for row in readings:
        platform = clean(row.get("platform")).lower()
        handle = bare(clean(row.get("handle")))
        key = (platform, handle)
        if not platform or not handle:
            result["unreadable"].append((platform, handle, "no platform or handle"))
            continue
        if key in seen:
            result["unreadable"].append((platform, handle, "listed twice"))
            continue
        seen.add(key)

        rows = by_key.get(key)
        if not rows:
            result["unknown"].append((platform, handle))
            continue

        status = clean(row.get("status")).lower()
        if status in NOT_FOUND:
            result["not_found"].append((platform, handle, rows))
            continue

        raw = row.get("followers")
        count = raw if isinstance(raw, int) else parse_followers(raw)
        if count is None:
            result["unreadable"].append((platform, handle, f"followers {raw!r}"))
            continue

        # The same account on two creators' profiles gets the same reading on
        # both: it is one audience, not an ambiguity to resolve.
        old = rows[0].follower_count
        entry = (platform, handle, rows, old, count)
        if old is None:
            result["first"].append(entry)
        elif old != count:
            result["changed"].append(entry)
        else:
            result["same"].append(entry)
    return result


def apply(result, source, measured_at=None):
    """Write the readings. One transaction."""
    measured_at = measured_at or date.today()
    written = 0
    for bucket in ("changed", "first", "same"):
        for _platform, _handle, rows, _old, count in result[bucket]:
            for row in rows:
                row.follower_count = count
                db.session.add(FollowerSnapshot(
                    social_handle_id=row.id, follower_count=count, status="ok",
                    measured_at=measured_at, source=source,
                ))
                written += 1
    for _platform, _handle, rows in result["not_found"]:
        for row in rows:
            # The old count is left alone: it was true once, and the snapshot
            # is what says the account could not be found on this date.
            db.session.add(FollowerSnapshot(
                social_handle_id=row.id, follower_count=None, status="not_found",
                measured_at=measured_at, source=source,
            ))
            written += 1
    db.session.commit()
    return written


def render(result):
    out = []
    add = out.append
    add(f"{len(result['first'])} first readings, {len(result['changed'])} changed, "
        f"{len(result['same'])} unchanged, {len(result['not_found'])} not found")

    if result["changed"]:
        add("")
        add(f"CHANGED ({len(result['changed'])}):")
        for platform, handle, _rows, old, new in sorted(
            result["changed"], key=lambda r: -abs(r[4] - r[3])
        ):
            add(f"    {platform:<10} @{handle:<28} {old:>9,} -> {new:>9,}")
    if result["not_found"]:
        add("")
        add(f"NOT FOUND ({len(result['not_found'])}) — recorded; counts left as they were:")
        for platform, handle, _rows in result["not_found"]:
            add(f"    {platform:<10} @{handle}")
    if result["unknown"]:
        add("")
        add(f"NO SUCH HANDLE ON FILE ({len(result['unknown'])}) — skipped:")
        for platform, handle in result["unknown"]:
            add(f"    {platform:<10} @{handle}")
    if result["unreadable"]:
        add("")
        add(f"COULD NOT READ ({len(result['unreadable'])}) — skipped:")
        for platform, handle, why in result["unreadable"]:
            add(f"    {platform or '?':<10} @{handle or '?'} — {why}")
    return "\n".join(out)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="print the handles that need checking, as CSV")
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--out", metavar="FILE", help="write --list here")
    parser.add_argument("--counts", metavar="FILE", help="readings to load")
    parser.add_argument("--source", help="where the readings came from")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="when they were taken (default: today)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    from app import app

    with app.app_context():
        if args.list:
            rows = worklist(args.platform)
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=[
                "platform", "handle", "profile_url", "followers", "last_checked",
            ])
            writer.writeheader()
            writer.writerows(rows)
            if args.out:
                with open(args.out, "w", newline="") as handle:
                    handle.write(buffer.getvalue())
                print(f"{len(rows)} handles written to {args.out}")
            else:
                print(buffer.getvalue(), end="")
            return 0

        if not args.counts:
            parser.error("give --list or --counts FILE")
        if not args.source:
            parser.error("--source is required: a count nobody can trace is "
                         "worth less than no count")

        with open(args.counts) as handle:
            result = plan(read_readings(handle.read()))
        print(render(result))

        if not args.apply:
            print("\n(dry run — nothing written. Re-run with --apply.)")
            return 0
        when = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
        print(f"\nDone: {apply(result, args.source, when)} readings recorded.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
