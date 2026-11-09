#!/usr/bin/env python3
"""Clear dashboard data before a real import.

Everything currently in creatorwise is invented — 14 sample creators generated
by seed.py. Real creators must not land next to them, because once mixed there's
no reliable way to tell which is which.

Counts only, unless you pass --yes:

    python3 tools/reset_data.py                  # show what's there
    python3 tools/reset_data.py --yes            # clear creators and content
    python3 tools/reset_data.py --yes --campaigns  # clear campaigns too

Campaigns are kept by default: the three in there have the real names and dates
from the two pilots, so they're worth keeping even though their payment terms
are placeholder text.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (  # noqa: E402
    Campaign, Creator, InboxItem, Participation, PlatformPost,
    ReferralSnapshot, SocialHandle, UgcPiece, db,
)

# Delete order matters: children before parents, or the foreign keys complain.
CONTENT = [
    ("referral snapshots", ReferralSnapshot),
    ("platform posts", PlatformPost),
    ("inbox items", InboxItem),
    ("UGC pieces", UgcPiece),
    ("participations", Participation),
    ("social handles", SocialHandle),
    ("creators", Creator),
]


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually delete")
    parser.add_argument("--campaigns", action="store_true",
                        help="also delete campaigns")
    args = parser.parse_args(argv)

    from app import app

    targets = list(CONTENT)
    if args.campaigns:
        targets.append(("campaigns", Campaign))

    with app.app_context():
        counts = [(label, model, model.query.count()) for label, model in targets]

        print("Currently in the database:")
        for label, _, count in counts:
            print(f"  {count:>6}  {label}")
        if not args.campaigns:
            print(f"  {Campaign.query.count():>6}  campaigns (kept — pass --campaigns to clear)")

        total = sum(c for _, _, c in counts)
        if total == 0:
            print("\nNothing to delete.")
            return 0

        if not args.yes:
            print(f"\n{total} rows would be deleted. Re-run with --yes to do it.")
            return 0

        for label, model, count in counts:
            if count:
                model.query.delete()
                print(f"deleted {count} {label}")
        db.session.commit()

        print(f"\nDone — {total} rows deleted.")
        print(
            "\nNOTE: seed.py refills an EMPTY database on boot when "
            "SEED_SAMPLE_DATA=1.\nSet it to 0 (locally in .env, in production "
            "via contextone env set)\nor the sample data comes straight back on "
            "the next restart."
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
