#!/usr/bin/env python3
"""Who is due to be paid, for how much, and the emails that ask about it.

The rules are in payments.PAY_RULES (Readwise Books Announcement: $100 plus
$5 per referral in the 14 days from the creator's first post, paid 14 days
after it). A daily scheduled Claude task, `creatorwise-payments`, runs:

    refresh                       every delivered creator gets a payment row;
                                  due ones turn pending
    referral-sql OUT.sql          one query counting each pending creator's
                                  referrals over their own 14 days (writes
                                  OUT.sql.json: who it asked about)
    referral-load RESULT.json     load what Readwise returned (checksummed)
    invoices                      a PDF invoice per pending creator whose
                                  payment details are on file
    drafts                        what each email should say, as JSON (a
                                  creator asked for details gets the invoice
                                  email once their details are on file)
    upload --file F --url U       send an invoice to Superhuman's upload URL
    drafted --payment N --draft-id D --kind invoice|details
    awaiting                      asked for details, still without them

The emails are drafts in Erin's Superhuman: nothing is sent. Invoices carry
full bank numbers (Erin's decision, 2026-11-20), so: they are written under
data/invoices/ (gitignored, never synced to the server), this tool never
prints their contents, and `upload` moves the bytes straight from disk to
Superhuman's upload URL, so they never pass through the task's own hands.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT = os.path.join(HERE, "invoice_pdf.swift")
BINARY = os.path.expanduser("~/Library/Caches/creatorwise/invoice_pdf")

BILL_TO = os.environ.get("INVOICE_BILL_TO",
                         "Readwise.io|5540 Centerview Dr. suite 204|Raleigh, NC 27606").split("|")
FIRST_INVOICE = 1001
SUBJECT = "Your Readwise announcement payment"

# Erin's two emails (2026-11-20). {first_name} is filled in; nothing else.
NEEDS_DETAILS = """Hey {first_name}, hope you're well! It's been a little over two weeks since your Readwise announcement post went live and I want to make sure you get paid.

When you have a chance, can you please confirm your payment details?

Full name associate with bank account
Account #
Routing #
Bank Name
Personal or Business Phone Number
Personal or business address

Alternatively, we can pay you via PayPal. If you prefer that method, please send me the email connected to your PayPal account.

Warmly,
Erin"""

DETAILS_ON_FILE = """Hey {first_name}, hope you're well! It's been a little over two weeks since your Readwise announcement post went live and I want to make sure you get paid.

When you have a chance, can you please confirm the attached payment details are still correct?

Warmly,
Erin"""


def first_name(creator):
    words = re.sub(r"\([^)]*\)", " ", creator.name).split()
    return words[0] if words else creator.name


def html(text):
    import html as h
    paragraphs = text.split("\n\n")
    return "".join("<p>" + "<br>".join(h.escape(l) for l in p.split("\n")) + "</p>"
                   for p in paragraphs)


def long_date(d):
    return f"{d:%B} {d.day}, {d.year}"


# --- referrals over each creator's own window -------------------------------------

def to_count(payments, today):
    """Pending or paid payments whose window has closed and whose count isn't
    in. Paid ones are counted too: CAC needs what they were paid."""
    return [p for p in payments if p.status in ("pending", "paid") and p.referral_to
            and p.referral_to < today and p.referrals is None]


def referral_sql(rows, holders):
    """One query for every creator's window. Addresses shared by two
    creators are left out (refresh_referrals.py: whose link earned the
    signups can't be told). The ROLLUP row is a checksum."""
    values, asked = [], []
    for p in rows:
        addresses = {(p.creator.email or "").lower()} | {e.address.lower() for e in p.creator.emails}
        addresses = sorted(a for a in addresses if a and len(holders.get(a, ())) == 1)
        if not addresses:
            continue
        asked.append(p.creator_id)
        for a in addresses:
            values.append(f"({p.creator_id}, '{a.replace(chr(39), '')}', DATE '{p.referral_from}', "
                          f"DATE '{p.referral_to + timedelta(days=1)}')")
    if not values:
        return None, []
    sql = ("WITH w(creator_id, email, d_from, d_until) AS (VALUES\n " + ",\n ".join(values) + ")\n"
           "SELECT w.creator_id, count(x.id) AS referrals\n"
           "FROM w\n"
           "JOIN auth_user u ON lower(u.email) = w.email\n"
           "JOIN main_profile p ON p.user_id = u.id\n"
           "JOIN bookwise_bookwiseinvitecode c ON c.profile_id = p.id AND c.deleted_at IS NULL\n"
           "JOIN bookwise_bookwiseinviteredemption x ON x.invite_code_id = c.id\n"
           "  AND x.deleted_at IS NULL AND x.redeemed_at >= w.d_from AND x.redeemed_at < w.d_until\n"
           "GROUP BY ROLLUP (w.creator_id)\n"
           "ORDER BY w.creator_id NULLS FIRST\n"
           "LIMIT 10000")
    return sql, asked


def check_result(result, asked):
    """{creator_id: count} from the query tool's JSON, or raise. Creators
    asked about but absent had no redemptions in their window: 0."""
    rows = result.get("rows") or []
    if not rows or rows[0][0] is not None:
        raise ValueError("no checksum row (the first row's creator_id should be empty)")
    total = int(rows[0][1])
    counts = {}
    for cid, n in rows[1:]:
        if cid is None or int(cid) not in asked:
            raise ValueError(f"names creator {cid}, who wasn't asked about")
        counts[int(cid)] = int(n)
    if sum(counts.values()) != total:
        raise ValueError(f"rows add up to {sum(counts.values())}, the total row says {total}")
    for cid in asked:
        counts.setdefault(cid, 0)
    return counts


# --- invoices --------------------------------------------------------------------

def renderer():
    if not os.path.exists(BINARY) or os.path.getmtime(BINARY) < os.path.getmtime(SWIFT):
        os.makedirs(os.path.dirname(BINARY), exist_ok=True)
        subprocess.run(["swiftc", "-O", SWIFT, "-o", BINARY], check=True)
    return BINARY


def invoice_spec(payment, number, issued):
    """What the invoice says. Built from the database; never printed."""
    from payments import PAY_RULES, money
    c, d = payment.creator, payment.creator.payment_details
    rule = PAY_RULES[payment.campaign.slug]
    name = (d.payee_name if d and d.payee_name else c.name)
    sender = [name] + ((d.address or "").splitlines() if d else [])
    if d and d.phone:
        sender.append(f"Phone: {d.phone}")
    if c.payment_method == "paypal":
        pay_to = [name, f"PayPal: {c.payment_handle}"]
    else:
        pay_to = [name, f"Bank Name: {d.bank_name or '—'}", f"Account #: {d.account_number}",
                  f"Routing #: {d.routing_number}"]
    lines = [{"description": f"{rule['line']} — {long_date(payment.first_posted_on)}",
              "amount": money(payment.base_cents)}]
    if payment.referrals:
        lines.append({"description": f"{rule['bonus_line']} ({money(payment.per_referral_cents)} x "
                                     f"{payment.referrals})", "amount": money(payment.bonus_cents)})
    return {"number": str(number), "issued": long_date(issued), "due": "Upon Receipt",
            "from": sender, "to": BILL_TO, "lines": lines, "total": money(payment.total_cents),
            "payTo": pay_to}


def invoice_path(folder, payment):
    safe = re.sub(r'[/:\\"*?<>|]+', "-", payment.creator.name)
    return os.path.join(folder, f"Invoice {payment.invoice_number} - {safe}.pdf")


def write_invoice(payment, folder, issued):
    os.makedirs(folder, exist_ok=True)
    path = invoice_path(folder, payment)
    spec = json.dumps(invoice_spec(payment, payment.invoice_number, issued))
    done = subprocess.run([renderer(), path], input=spec, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("invoice renderer failed: " + done.stderr.strip()[:200])
    return path


def drafted_kind(p):
    """Which email the last draft was: draft_id is stored as "invoice:<id>" or
    "details:<id>" (older ids, bare, were from the first run)."""
    if not p.draft_id:
        return None
    return p.draft_id.split(":", 1)[0] if ":" in p.draft_id else "unknown"


def ready_for_email(p):
    """Pending, dated, counted — and either never emailed, or asked for
    their details before and now has them on file (so they get the invoice)."""
    if not (p.status == "pending" and p.amount_known and p.first_posted_on):
        return False
    if not p.drafted_on:
        return True
    return drafted_kind(p) == "details" and p.creator.payment_ready


def awaiting(rows):
    """Asked for their payment details and still without them: whose replies
    Erin may need to save and import."""
    return [p for p in rows if p.status == "pending" and drafted_kind(p) == "details"
            and not p.creator.payment_ready]


# --- CLI ---------------------------------------------------------------------------

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("refresh")
    s.add_argument("--today")
    s = sub.add_parser("referral-sql")
    s.add_argument("out")
    s.add_argument("--today")
    s = sub.add_parser("referral-load")
    s.add_argument("result")
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("invoices")
    s.add_argument("--today")
    s = sub.add_parser("drafts")
    s = sub.add_parser("upload")
    s.add_argument("--file", required=True)
    s.add_argument("--url", required=True)
    s = sub.add_parser("drafted")
    s.add_argument("--payment", type=int, required=True)
    s.add_argument("--draft-id", required=True)
    s.add_argument("--kind", choices=["invoice", "details"], required=True,
                   help="invoice: the email with the invoice; details: the one asking for them")
    sub.add_parser("awaiting")
    args = parser.parse_args(argv)

    if args.cmd == "upload":
        # Straight from disk to Superhuman's short-lived upload URL.
        with open(args.file, "rb") as f:
            body = f.read()
        req = urllib.request.Request(args.url, data=body, method="POST", headers={
            "Content-Type": "application/pdf", "Content-Length": str(len(body))})
        from tools.fetch_avatars import _ssl_context
        with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as r:
            print(f"uploaded: HTTP {r.status}")
        return 0

    import logging
    logging.disable(logging.WARNING)
    import app as appmod
    import media
    import payments
    from models import Campaign, Creator, Payment, db
    from tools.content_requests import address_holders
    logging.disable(logging.NOTSET)
    today = date.fromisoformat(getattr(args, "today", None) or date.today().isoformat())
    data_dir = media.data_dir(appmod.app.config["SQLALCHEMY_DATABASE_URI"])
    folder = os.path.join(data_dir, "invoices")

    with appmod.app.app_context():
        db.create_all()
        if args.cmd == "refresh":
            for slug in payments.PAY_RULES:
                camp = Campaign.query.filter_by(slug=slug).first()
                if camp is None:
                    continue
                created, updated, undated = payments.refresh(camp, today)
                rows = Payment.query.filter_by(campaign_id=camp.id).all()
                from collections import Counter
                tally = Counter(p.status for p in rows)
                print(f"{camp.name}: {tally['pending']} pending, {tally['upcoming']} upcoming, "
                      f"{tally['paid']} paid ({created} new, {updated} changed)")
                for name in undated:
                    print(f"  no first-post date yet (a piece is undated or missing): {name}")
            return 0

        if args.cmd == "referral-sql":
            creators = Creator.query.all()
            rows = to_count(Payment.query.all(), today)
            sql, asked = referral_sql(rows, address_holders(creators))
            if not sql:
                print("Nothing to count: no pending payment's window has closed uncounted.")
                return 0
            with open(args.out, "w") as f:
                f.write(sql + "\n")
            with open(args.out + ".json", "w") as f:
                json.dump({"asked": asked}, f)
            print(f"Query for {len(asked)} creator(s) written to {args.out}")
            return 0

        if args.cmd == "referral-load":
            sql_meta = args.result.rsplit(".", 1)[0] + ".sql.json"
            asked_file = next((p for p in (sql_meta, args.result + ".asked.json") if os.path.exists(p)),
                              None)
            if asked_file is None:
                print("REFUSED: can't find the .sql.json written with the query")
                return 1
            asked = set(json.load(open(asked_file))["asked"])
            try:
                counts = check_result(json.load(open(args.result)), asked)
            except ValueError as error:
                print(f"REFUSED: {error}. Fetch the result again; never edit it by hand.")
                return 1
            for cid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                c = db.session.get(Creator, cid)
                print(f"  {c.name:<32} {n} referral(s)")
            if not args.apply:
                print("(dry run — nothing written. Re-run with --apply.)")
                return 0
            for p in Payment.query.filter(Payment.creator_id.in_(counts)).all():
                if p.status in ("pending", "paid") and p.referrals is None:
                    p.referrals = counts[p.creator_id]
                    p.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()
            print(f"-> {len(counts)} count(s) stored")
            return 0

        if args.cmd == "invoices":
            made = 0
            for p in Payment.query.filter_by(status="pending").all():
                if not (p.amount_known and p.first_posted_on and p.creator.payment_ready):
                    continue
                if p.invoice_number is None:
                    top = db.session.query(db.func.max(Payment.invoice_number)).scalar()
                    p.invoice_number = max(top or 0, FIRST_INVOICE - 1) + 1
                    db.session.commit()
                path = write_invoice(p, folder, today)
                made += 1
                print(f"  {os.path.basename(path)}  ({payments.money(p.total_cents)})")
            print(f"-> {made} invoice(s) in {folder}")
            return 0

        if args.cmd == "drafts":
            out = []
            for p in Payment.query.filter_by(status="pending").order_by(Payment.due_on).all():
                if not ready_for_email(p):
                    continue
                ready = p.creator.payment_ready
                item = {"payment": p.id, "creator": p.creator.name, "to": p.creator.email,
                        "subject": SUBJECT, "amount": payments.money(p.total_cents),
                        "with_invoice": ready,
                        "body_html": html((DETAILS_ON_FILE if ready else NEEDS_DETAILS)
                                          .format(first_name=first_name(p.creator)))}
                if ready:
                    path = invoice_path(folder, p) if p.invoice_number else None
                    if not path or not os.path.exists(path):
                        item["problem"] = "invoice not generated — run `invoices` first"
                    else:
                        item.update(invoice=path, filename=os.path.basename(path),
                                    size=os.path.getsize(path), sha256=sha256(path))
                out.append(item)
            print(json.dumps({"drafts": out}, indent=1))
            return 0

        if args.cmd == "awaiting":
            out = [{"payment": p.id, "creator": p.creator.name,
                    "addresses": sorted({p.creator.email} | {e.address for e in p.creator.emails}),
                    "asked_on": p.drafted_on.isoformat() if p.drafted_on else None}
                   for p in awaiting(Payment.query.all())]
            print(json.dumps({"awaiting": out}, indent=1))
            return 0

        if args.cmd == "drafted":
            p = db.session.get(Payment, args.payment)
            p.drafted_on = today
            p.draft_id = f"{args.kind}:{args.draft_id}"
            p.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()
            print(f"payment {p.id} ({p.creator.name}) marked drafted")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
