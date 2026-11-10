#!/usr/bin/env python3
"""Mirror every creator's primary address into creator_emails.

`Creator.email` stays the primary and stays authoritative. This copies it into
the creator_emails table so that table holds the whole picture rather than only
the extras — matching code can then ask one question ("whose address is this?")
instead of two.

Safe to run repeatedly; it only ever adds rows that are missing.

    python3 tools/backfill_emails.py          # show what it would do
    python3 tools/backfill_emails.py --yes    # write it
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Creator, CreatorEmail, db  # noqa: E402


def backfill(commit=False, source="primary"):
    """Return (added, already_there, conflicts)."""
    known = {e.address.lower(): e for e in CreatorEmail.query.all()}
    added, present, conflicts = 0, 0, []

    for creator in Creator.query.order_by(Creator.id).all():
        address = (creator.email or "").strip().lower()
        if not address:
            continue
        existing = known.get(address)
        if existing is not None:
            if existing.creator_id != creator.id:
                # One address, two creators. Creator.email is unique, so this
                # can only happen if creator_emails already gave it to someone
                # else — a real conflict, not something to overwrite.
                conflicts.append((address, existing.creator_id, creator.id))
            else:
                present += 1
            continue
        row = CreatorEmail(
            creator_id=creator.id, address=address, is_primary=True, source=source
        )
        db.session.add(row)
        known[address] = row
        added += 1

    if commit:
        db.session.commit()
    else:
        db.session.rollback()
    return added, present, conflicts


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually write")
    args = parser.parse_args(argv)

    from app import app

    with app.app_context():
        added, present, conflicts = backfill(commit=args.yes)
        print(f"{added} primary address(es) to record, {present} already there")
        for address, held_by, wanted_by in conflicts:
            print(f"  CONFLICT {address}: held by creator {held_by}, "
                  f"also the primary of {wanted_by}")
        if not args.yes:
            print("\n(dry run — nothing written. Re-run with --yes.)")
        return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
