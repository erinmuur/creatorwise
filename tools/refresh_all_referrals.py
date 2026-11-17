#!/usr/bin/env python3
"""Re-count referrals for every campaign at once, in two steps.

creatorwise has no access to the Readwise production database and shouldn't
(see refresh_referrals.py). So a refresh is a hand-off, the same as before,
just for all campaigns in one go:

1. ``--sql FILE`` writes one query covering every campaign, each over its own
   window: from ``starts_on`` to ``ends_on``, or to today while it is still
   running (an ongoing campaign with no end is always counted to today). It
   asks only about addresses already on file.

2. Somebody with production access runs it — the daily scheduled task does
   this with Erin's contextone tooling — and saves the result exactly as the
   query tool returns it: ``{"columns": [...], "rows": [...]}``.

3. ``--load FILE`` checks that file and loads it with refresh_referrals'
   plan/apply, one campaign per column. Dry run unless ``--apply``.

The result is copied by hand (or by a model) between the two steps, so it
carries its own checksum: the query adds a ROLLUP total row, and the loader
refuses a file whose rows don't add up to it, whose row count doesn't match,
or that names an address the query never asked about. A refused file is
re-fetched, never patched.

Each campaign's column is named ``c<id>_<from>_<to>`` so the file says which
window it was counted over even if it is loaded on a later day.

    ./venv/bin/python tools/refresh_all_referrals.py --sql /tmp/referrals.sql
    ./venv/bin/python tools/refresh_all_referrals.py --load result.json
    ./venv/bin/python tools/refresh_all_referrals.py --load result.json --apply

``--apply`` first copies the database to ``data/creatorwise.db.bak-referrals``
(overwritten each run) and appends one line per run to
``data/referral-refreshes.log``.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Campaign, Creator, CreatorEmail, db  # noqa: E402
from tools import refresh_referrals  # noqa: E402

SOURCE = "bookwise invite redemptions"
COLUMN = re.compile(r"^c(\d+)_(\d{8})_(\d{8})$")


class RefusedFile(ValueError):
    """The result file doesn't check out. Fetch it again; don't edit it."""


def windows(campaigns, today):
    """[(campaign, from, to)] to count, and [(campaign, reason)] skipped."""
    counted, skipped = [], []
    for campaign in campaigns:
        if campaign.starts_on is None:
            skipped.append((campaign, "no start date"))
            continue
        if campaign.starts_on > today:
            skipped.append((campaign, "hasn't started"))
            continue
        end = today if campaign.ends_on is None else min(campaign.ends_on, today)
        counted.append((campaign, campaign.starts_on, end))
    return counted, skipped


def column_name(campaign_id, window_from, window_to):
    return f"c{campaign_id}_{window_from:%Y%m%d}_{window_to:%Y%m%d}"


def addresses_on_file():
    found = {row.address.strip().lower() for row in CreatorEmail.query.all()}
    found |= {c.email.strip().lower() for c in Creator.query.all() if c.email}
    return sorted(a for a in found if a)


def _quote(text):
    return "'" + text.replace("'", "''") + "'"


def build_sql(counted, addresses):
    if not counted:
        raise ValueError("no campaign has a window to count")
    earliest = min(window_from for _c, window_from, _t in counted)
    columns = []
    for campaign, window_from, window_to in counted:
        after = window_to + timedelta(days=1)
        columns.append(
            f" count(x.id) FILTER (WHERE x.redeemed_at >= DATE '{window_from}'"
            f" AND x.redeemed_at < DATE '{after}')"
            f" AS {column_name(campaign.id, window_from, window_to)}"
        )
    return (
        "SELECT lower(u.email) AS email,\n"
        " count(DISTINCT lower(u.email)) AS n,\n"
        + ",\n".join(columns) + "\n"
        "FROM auth_user u\n"
        "JOIN main_profile p ON p.user_id = u.id\n"
        "JOIN bookwise_bookwiseinvitecode c"
        " ON c.profile_id = p.id AND c.deleted_at IS NULL\n"
        "JOIN bookwise_bookwiseinviteredemption x"
        " ON x.invite_code_id = c.id AND x.deleted_at IS NULL\n"
        f"WHERE lower(u.email) IN ({','.join(_quote(a) for a in addresses)})\n"
        f" AND x.redeemed_at >= DATE '{earliest}'\n"
        "GROUP BY ROLLUP (lower(u.email))\n"
        "ORDER BY email NULLS FIRST\n"
        "LIMIT 10000\n"
    )


def _day(text):
    return datetime.strptime(text, "%Y%m%d").date()


def _int(value, where):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RefusedFile(f"{where}: {value!r} is not a count")


def parse_result(data, asked_about):
    """Check a query result and split it into per-campaign counts.

    Returns {campaign_id: (window_from, window_to, {email: count})}, counts of
    zero left out (absent and zero mean the same thing to the loader).
    Raises RefusedFile if anything doesn't add up.
    """
    if not isinstance(data, dict) or "columns" not in data or "rows" not in data:
        raise RefusedFile('expected {"columns": [...], "rows": [...]}')
    columns = list(data["columns"])
    if columns[:2] != ["email", "n"]:
        raise RefusedFile(f"first columns should be email, n — got {columns[:2]}")
    campaigns = {}
    for name in columns[2:]:
        match = COLUMN.match(name)
        if not match:
            raise RefusedFile(f"unexpected column {name!r}")
        campaigns[name] = (int(match[1]), _day(match[2]), _day(match[3]))
    if not campaigns:
        raise RefusedFile("no campaign columns")

    rows = []
    for i, row in enumerate(data["rows"]):
        if isinstance(row, dict):
            row = [row.get(c) for c in columns]
        if len(row) != len(columns):
            raise RefusedFile(f"row {i + 1} has {len(row)} values, "
                              f"expected {len(columns)}")
        rows.append(dict(zip(columns, row)))

    totals = [r for r in rows if r["email"] is None]
    people = [r for r in rows if r["email"] is not None]
    if len(totals) != 1:
        raise RefusedFile(f"expected one total row (email null), "
                          f"found {len(totals)}")
    total = totals[0]

    if _int(total["n"], "total n") != len(people):
        raise RefusedFile(f"the total row counts {total['n']} addresses but "
                          f"the file has {len(people)} — rows are missing "
                          f"or duplicated")
    seen = set()
    asked = {a.lower() for a in asked_about}
    for r in people:
        address = str(r["email"]).strip().lower()
        if address in seen:
            raise RefusedFile(f"{address} appears twice")
        if address not in asked:
            raise RefusedFile(f"{address} wasn't in the query — "
                              f"probably mistyped in copying")
        seen.add(address)

    out = {}
    for name, (campaign_id, window_from, window_to) in campaigns.items():
        counts = {}
        for r in people:
            value = _int(r[name], f"{r['email']} / {name}")
            if value:
                counts[str(r["email"]).strip().lower()] = value
        expected = _int(total[name], f"total / {name}")
        if sum(counts.values()) != expected:
            raise RefusedFile(f"{name}: rows add up to {sum(counts.values())}, "
                              f"the total row says {expected}")
        out[campaign_id] = (window_from, window_to, counts)
    return out


def database_file():
    url = db.engine.url
    return url.database if url.get_backend_name() == "sqlite" else None


def backup(path):
    target = path + ".bak-referrals"
    source = sqlite3.connect(path)
    try:
        dest = sqlite3.connect(target)
        with dest:
            source.backup(dest)
        dest.close()
    finally:
        source.close()
    return target


def load(parsed, apply_changes):
    """Plan (and optionally apply) every campaign. Returns (report, totals)."""
    plans = []
    for campaign_id, (window_from, window_to, counts) in sorted(parsed.items()):
        campaign = db.session.get(Campaign, campaign_id)
        if campaign is None:
            raise RefusedFile(f"no campaign with id {campaign_id}")
        plans.append((campaign, refresh_referrals.plan(
            campaign, counts, window_from, window_to)))

    report, totals = [], {}
    for campaign, result in plans:
        report.append(refresh_referrals.render(campaign, result))
        totals[campaign.slug] = sum(
            r[2] for b in ("new", "changed", "same") for r in result[b])
        if apply_changes:
            summary = refresh_referrals.apply(campaign, result, SOURCE)
            report.append(f"-> {summary['written']} creator totals written")
        report.append("")
    return "\n".join(report), totals


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    step = parser.add_mutually_exclusive_group(required=True)
    step.add_argument("--sql", metavar="FILE", help="write the query here")
    step.add_argument("--load", metavar="FILE", help="the query's result, JSON")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--today", metavar="YYYY-MM-DD",
                        help="count as if it were this day (for testing)")
    args = parser.parse_args(argv)

    from app import app

    with app.app_context():
        if args.sql:
            today = (datetime.strptime(args.today, "%Y-%m-%d").date()
                     if args.today else date.today())
            counted, skipped = windows(
                Campaign.query.order_by(Campaign.id).all(), today)
            addresses = addresses_on_file()
            with open(args.sql, "w") as handle:
                handle.write(build_sql(counted, addresses))
            for campaign, window_from, window_to in counted:
                print(f"{campaign.name}: {window_from} to {window_to}")
            for campaign, reason in skipped:
                print(f"{campaign.name}: skipped, {reason}")
            print(f"{len(addresses)} addresses. Query written to {args.sql}")
            return 0

        with open(args.load) as handle:
            data = json.load(handle)
        try:
            parsed = parse_result(data, addresses_on_file())
        except RefusedFile as refused:
            print(f"REFUSED: {refused}\nRe-run the query and save its result "
                  f"again; don't edit the file by hand.")
            return 2

        if not args.apply:
            report, _totals = load(parsed, apply_changes=False)
            print(report)
            print("(dry run — nothing written. Re-run with --apply.)")
            return 0

        path = database_file()
        if path:
            print(f"Backed up to {backup(path)}")
        report, totals = load(parsed, apply_changes=True)
        print(report)
        if path:
            log = os.path.join(os.path.dirname(path), "referral-refreshes.log")
            with open(log, "a") as handle:
                handle.write(f"{datetime.now():%Y-%m-%d %H:%M} "
                             + " ".join(f"{k}={v}" for k, v in totals.items())
                             + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
