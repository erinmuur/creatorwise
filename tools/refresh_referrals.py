#!/usr/bin/env python3
"""Load per-creator referral counts for one campaign, over a stated window.

creatorwise has no access to the Readwise production database and shouldn't:
it's a public repo on a box that only needs its own SQLite file. So the counts
come in as a JSON file produced by somebody who can query production, and this
loads them against the right creators.

The query that produces the file counts invite-code redemptions per account
inside the window — redeemed_at >= FROM and < the day after TO:

    SELECT lower(u.email) AS email, count(x.id) AS referrals
    FROM auth_user u
    JOIN main_profile p ON p.user_id = u.id
    JOIN bookwise_bookwiseinvitecode c
      ON c.profile_id = p.id AND c.deleted_at IS NULL
    JOIN bookwise_bookwiseinviteredemption x
      ON x.invite_code_id = c.id AND x.deleted_at IS NULL
    WHERE x.redeemed_at >= DATE '<from>' AND x.redeemed_at < DATE '<to + 1 day>'
    GROUP BY 1

Input is {"someone@example.com": 12, ...}. Addresses are matched against every
address on file for a creator, primary or secondary, so a creator recruited
under one address and signed up under another is still found.

    python3 tools/refresh_referrals.py --campaign bookwise-softlaunch \
        --from 2026-07-15 --to 2026-10-01 --counts counts.json
    ... --apply

Dry run unless --apply. Re-running is safe: a creator's row is updated in
place, never duplicated.
"""

import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (  # noqa: E402
    Campaign,
    CampaignReferral,
    Creator,
    CreatorEmail,
    Participation,
    ReferralSnapshot,
    UgcPiece,
    db,
)


def parse_day(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def plan(campaign, counts, window_from, window_to):
    """Work out what loading `counts` would do. Returns a dict of lists."""
    holders = {}
    for row in CreatorEmail.query.all():
        holders.setdefault(row.address.strip().lower(), set()).add(row.creator_id)
    for creator in Creator.query.all():
        if creator.email:
            holders.setdefault(creator.email.strip().lower(), set()).add(creator.id)

    # An address two creators share can't be credited to either of them: the
    # redemptions came from one mailbox and the source doesn't say whose link
    # it was. Reported, not split and not guessed.
    by_address, shared_addresses = {}, {}
    for address, ids in holders.items():
        if len(ids) == 1:
            by_address[address] = next(iter(ids))
        else:
            shared_addresses[address] = ids

    in_campaign = {
        p.creator_id for p in
        Participation.query.filter_by(campaign_id=campaign.id).all()
    }
    existing = {
        r.creator_id: r for r in
        CampaignReferral.query.filter_by(campaign_id=campaign.id).all()
    }

    result = {
        "new": [], "changed": [], "same": [],
        "unknown": [],      # address matches no creator
        "shared": [],       # address belongs to more than one creator
        "outsiders": [],    # a creator, but not in this campaign
        "zeroed": [],       # had a number, absent from the file now
    }

    seen = set()
    for address, referrals in counts.items():
        address = address.strip().lower()
        if address in shared_addresses:
            names = sorted(
                db.session.get(Creator, i).name for i in shared_addresses[address]
            )
            result["shared"].append((address, referrals, names))
            continue
        creator_id = by_address.get(address)
        if creator_id is None:
            result["unknown"].append((address, referrals))
            continue
        if creator_id not in in_campaign:
            creator = db.session.get(Creator, creator_id)
            result["outsiders"].append((creator.name, address, referrals))
            continue
        if creator_id in seen:
            # Two of this creator's addresses in one file: add them up, they
            # are the same person's invite codes under different accounts.
            for bucket in ("new", "changed", "same"):
                for item in result[bucket]:
                    if item[0] == creator_id:
                        item[2] += referrals
            continue
        seen.add(creator_id)

        creator = db.session.get(Creator, creator_id)
        current = existing.get(creator_id)
        if current is None:
            result["new"].append([creator_id, creator.name, referrals, None])
        elif current.referrals != referrals:
            result["changed"].append(
                [creator_id, creator.name, referrals, current.referrals]
            )
        else:
            result["same"].append([creator_id, creator.name, referrals, referrals])

    for creator_id, row in existing.items():
        if creator_id not in seen and row.referrals:
            creator = db.session.get(Creator, creator_id)
            result["zeroed"].append([creator_id, creator.name, 0, row.referrals])

    result["window"] = (window_from, window_to)
    return result


def apply(campaign, result, source, drop_reported=True):
    """Commit a plan. One transaction."""
    window_from, window_to = result["window"]
    written = 0
    for bucket in ("new", "changed", "same", "zeroed"):
        for creator_id, _name, referrals, _old in result[bucket]:
            row = CampaignReferral.query.filter_by(
                creator_id=creator_id, campaign_id=campaign.id
            ).first()
            if row is None:
                row = CampaignReferral(
                    creator_id=creator_id, campaign_id=campaign.id
                )
                db.session.add(row)
            row.referrals = referrals
            row.window_from = window_from
            row.window_to = window_to
            row.measured_at = date.today()
            row.source = source
            written += 1

    # The per-piece "as reported" snapshots measured the same thing from a
    # worse source — a hand-copied column with no window. Leaving them in
    # place means two different answers to one question on two pages.
    dropped = 0
    if drop_reported:
        stale = (
            ReferralSnapshot.query
            .join(UgcPiece)
            .filter(UgcPiece.campaign_id == campaign.id)
            .filter(ReferralSnapshot.window_days == 0)
            .all()
        )
        for snapshot in stale:
            db.session.delete(snapshot)
            dropped += 1

    db.session.commit()
    return {"written": written, "dropped_reported": dropped}


def render(campaign, result):
    out = []
    add = out.append
    window_from, window_to = result["window"]
    add(f"Campaign: {campaign.name}")
    add(f"Window:   {window_from} to {window_to} inclusive")
    add("")

    total = sum(r[2] for b in ("new", "changed", "same") for r in result[b])
    add(f"{len(result['new'])} new, {len(result['changed'])} changed, "
        f"{len(result['same'])} unchanged, {len(result['zeroed'])} dropped to zero")
    add(f"{total} referrals in total")

    if result["changed"]:
        add("")
        add(f"CHANGED ({len(result['changed'])}):")
        for _id, name, new, old in sorted(result["changed"], key=lambda r: -r[2]):
            add(f"    {name[:34]:<36} {old} -> {new}")
    if result["zeroed"]:
        add("")
        add(f"NO LONGER IN THE FILE ({len(result['zeroed'])}) — set to zero:")
        for _id, name, _new, old in result["zeroed"]:
            add(f"    {name[:34]:<36} was {old}")
    if result["outsiders"]:
        add("")
        add(f"NOT IN THIS CAMPAIGN ({len(result['outsiders'])}) — skipped:")
        for name, address, referrals in sorted(result["outsiders"], key=lambda r: -r[2]):
            add(f"    {name[:34]:<36} {referrals}")
    if result["shared"]:
        add("")
        add(f"SHARED ADDRESS ({len(result['shared'])}) — skipped, nobody credited:")
        for address, referrals, names in result["shared"]:
            add(f"    {address:<40} {referrals} — shared by {', '.join(names)}")
    if result["unknown"]:
        add("")
        add(f"NO CREATOR WITH THAT ADDRESS ({len(result['unknown'])}) — skipped:")
        for address, referrals in sorted(result["unknown"], key=lambda r: -r[1]):
            add(f"    {address:<40} {referrals}")
    return "\n".join(out)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, metavar="SLUG")
    parser.add_argument("--from", dest="window_from", required=True,
                        metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="window_to", required=True,
                        metavar="YYYY-MM-DD", help="inclusive")
    parser.add_argument("--counts", required=True, metavar="FILE",
                        help='JSON: {"email": count, ...}')
    parser.add_argument("--source", default="bookwise invite redemptions")
    parser.add_argument("--keep-reported", action="store_true",
                        help="leave the old per-piece sheet numbers in place")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    with open(args.counts) as handle:
        counts = json.load(handle)

    from app import app

    with app.app_context():
        campaign = Campaign.query.filter_by(slug=args.campaign).first()
        if campaign is None:
            print(f"No campaign with slug {args.campaign!r}")
            return 1

        result = plan(campaign, counts,
                      parse_day(args.window_from), parse_day(args.window_to))
        print(render(campaign, result))

        if not args.apply:
            print("\n(dry run — nothing written. Re-run with --apply.)")
            return 0

        summary = apply(campaign, result, args.source,
                        drop_reported=not args.keep_reported)
        print(f"\nDone: {summary['written']} creator totals written"
              + (f", {summary['dropped_reported']} stale per-piece sheet "
                 f"numbers removed" if summary["dropped_reported"] else ""))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
