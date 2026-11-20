"""Creators' payment details: checking and saving what the profile form sends.

Bank numbers are stored as Erin decided (see models.PaymentDetails). This
module never logs or prints them; errors name the field, not its value.
"""

import re
from datetime import datetime, timezone

from models import PaymentDetails, db

LIMITS = {"payee_name": 200, "phone": 40, "bank_name": 120, "address": 600}

# How a creator is paid. Stored on the creator (payment_method /
# payment_handle, columns that already existed), so adding PayPal needed no
# migration. A PayPal payout needs only the address.
METHODS = {"bank": "Bank transfer (ACH)", "paypal": "PayPal"}
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")


def digits(value):
    """Numbers are often pasted with spaces or dashes: keep the digits."""
    return re.sub(r"[\s-]", "", value or "")


def routing_ok(number):
    """A US ABA routing number: 9 digits whose weighted sum (3,7,1,...) is a
    multiple of 10. Catches nearly every typo before it bounces a payment."""
    if not re.fullmatch(r"\d{9}", number):
        return False
    weights = (3, 7, 1) * 3
    return sum(int(d) * w for d, w in zip(number, weights)) % 10 == 0


def check(form):
    """(values, errors) from the submitted form. Blank fields are allowed —
    details often arrive a piece at a time — but a number that is filled in
    has to look like one."""
    values = {}
    for field in PaymentDetails.FIELDS:
        raw = (form.get(field) or "").strip()
        if field in ("account_number", "routing_number"):
            raw = digits(raw)
        values[field] = raw or None

    errors = []
    for field, limit in LIMITS.items():
        if values[field] and len(values[field]) > limit:
            errors.append(f"{field.replace('_', ' ')} is too long")
    account = values["account_number"]
    if account and not re.fullmatch(r"\d{4,17}", account):
        errors.append("account number should be 4–17 digits")
    routing = values["routing_number"]
    if routing and not routing_ok(routing):
        errors.append("routing number isn't a valid 9-digit US routing number")

    method = (form.get("payment_method") or "").strip().lower() or None
    if method and method not in METHODS:
        errors.append("unknown payment method")
    paypal = (form.get("paypal_email") or "").strip().lower() or None
    if paypal and not EMAIL.match(paypal):
        errors.append("PayPal email doesn't look like an email address")
    if method == "paypal" and not paypal:
        errors.append("PayPal needs the email address to send to")
    values["payment_method"] = method
    values["payment_handle"] = paypal if method == "paypal" else None
    return values, errors


def save(creator, form, now=None):
    """Store the form on the creator. Returns a list of errors (nothing is
    written if there are any)."""
    values, errors = check(form)
    if errors:
        return errors
    row = creator.payment_details
    if row is None:
        row = PaymentDetails(creator_id=creator.id)
        db.session.add(row)
    for field in PaymentDetails.FIELDS:
        setattr(row, field, values[field])
    creator.payment_method = values["payment_method"]
    creator.payment_handle = values["payment_handle"]
    # One timestamp for the whole payment section, method included: it is
    # what decides whether the live site's copy or the Mac's is newer.
    row.updated_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    return []


# --- carrying live-site edits back to the Mac --------------------------------

def as_rows(details):
    """PaymentDetails rows as plain dicts, for /api/data/payment-details."""
    out = []
    for row in details:
        item = {f: getattr(row, f) for f in PaymentDetails.FIELDS}
        item["creator_id"] = row.creator_id
        item["payment_method"] = row.creator.payment_method if row.creator else None
        item["payment_handle"] = row.creator.payment_handle if row.creator else None
        item["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
        out.append(item)
    return out


def merge_rows(conn, rows):
    """Write the server's rows into a local SQLite database where the server's
    copy is newer (or the Mac has none). Returns how many were taken.

    tools/push_data.py calls this before replacing the server's database, so
    details typed in on the live site aren't wiped by the next upload.
    """
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(PaymentDetails.__table__).compile(dialect=sqlite.dialect()))
    conn.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    taken = 0
    for row in rows:
        local = conn.execute(
            "SELECT updated_at FROM payment_details WHERE creator_id = ?",
            (row["creator_id"],)).fetchone()
        exists = conn.execute("SELECT 1 FROM creators WHERE id = ?",
                              (row["creator_id"],)).fetchone()
        if not exists:
            continue
        theirs = row.get("updated_at") or ""
        mine = (local[0] or "") if local else None
        if local is not None and _when(mine) >= _when(theirs):
            continue
        cols = list(PaymentDetails.FIELDS) + ["updated_at"]
        vals = [row.get(f) for f in PaymentDetails.FIELDS] + [_sql_time(theirs)]
        if local is None:
            conn.execute(
                f"INSERT INTO payment_details (creator_id, {', '.join(cols)}) "
                f"VALUES (?, {', '.join('?' * len(cols))})", [row["creator_id"]] + vals)
        else:
            conn.execute(
                f"UPDATE payment_details SET {', '.join(c + ' = ?' for c in cols)} "
                "WHERE creator_id = ?", vals + [row["creator_id"]])
        if "payment_method" in row:     # a server from before PayPal sends neither
            conn.execute("UPDATE creators SET payment_method = ?, payment_handle = ? "
                         "WHERE id = ?", (row.get("payment_method"), row.get("payment_handle"),
                                          row["creator_id"]))
        taken += 1
    conn.commit()
    return taken


def _when(text):
    if not text:
        return datetime.min
    return datetime.fromisoformat(str(text).replace(" ", "T"))


def _sql_time(text):
    """SQLAlchemy stores DateTime in SQLite as 'YYYY-MM-DD HH:MM:SS.ffffff'."""
    return _when(text).strftime("%Y-%m-%d %H:%M:%S.%f") if text else None


# --- what creators are owed ----------------------------------------------------

# Per campaign (Erin, 2026-11-20). Only campaigns listed here raise payments.
PAY_RULES = {
    "books-announcement": {
        "base_cents": 10000,            # $100 for the post
        "per_referral_cents": 500,      # $5 per referral...
        "window_days": 14,              # ...in the 14 days from their first post
        "line": "Readwise Books Announcement Post",
        "bonus_line": "Referral Bonus",
    },
}
PAY_AFTER_DAYS = 14                     # paid two weeks after the first post


def first_posted_on(creator, campaign):
    """The date of their first piece in the campaign, or None while any of
    their pieces is undated (an undated one might be the first)."""
    pieces = [p for p in creator.all_pieces if p.campaign_id == campaign.id]
    if not pieces or any(p.posted_on is None for p in pieces):
        return None
    return min(p.posted_on for p in pieces)


def refresh(campaign, today, now=None):
    """Create or update the payment for everyone who delivered. Never
    touches a paid one. Returns (created, updated, undated creator names)."""
    from datetime import timedelta
    from models import Payment

    rule = PAY_RULES[campaign.slug]
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    created = updated = 0
    undated = []
    for part in campaign.participations:
        if part.status != "content_delivered":
            continue
        row = Payment.query.filter_by(creator_id=part.creator_id, campaign_id=campaign.id).first()
        if row is None:
            row = Payment(creator_id=part.creator_id, campaign_id=campaign.id,
                          status="upcoming", base_cents=rule["base_cents"],
                          per_referral_cents=rule["per_referral_cents"], updated_at=now)
            db.session.add(row)
            created += 1
        if row.status == "paid":
            continue
        first = first_posted_on(part.creator, campaign)
        if first is None:
            undated.append(part.creator.name)
        changed = (row.first_posted_on != first)
        row.first_posted_on = first
        row.due_on = first + timedelta(days=PAY_AFTER_DAYS) if first else None
        row.referral_from = first
        row.referral_to = first + timedelta(days=rule["window_days"] - 1) if first else None
        if changed:
            row.referrals = None          # a new window needs a new count
        status = "pending" if row.due_on and row.due_on <= today else "upcoming"
        if row.status != status or changed:
            row.status = status
            row.updated_at = now
            updated += 1
    db.session.commit()
    return created, updated, sorted(undated)


def mark_paid(payment, paid_on, note=None, now=None):
    payment.status = "paid"
    payment.paid_on = paid_on
    if note:
        payment.note = note
    payment.updated_at = now or datetime.now(timezone.utc).replace(tzinfo=None)


def mark_unpaid(payment, today, now=None):
    payment.status = "pending" if payment.due_on and payment.due_on <= today else "upcoming"
    payment.paid_on = None
    payment.updated_at = now or datetime.now(timezone.utc).replace(tzinfo=None)


def money(cents):
    return f"${(cents or 0) / 100:,.2f}".replace(".00", "")


# --- carrying "Mark paid" from the live site back to the Mac --------------------

def payment_rows(rows):
    """Payment statuses for /api/data/payment-details — the only part of a
    payment the live site changes."""
    return [{"creator_id": p.creator_id, "campaign_id": p.campaign_id, "status": p.status,
             "paid_on": p.paid_on.isoformat() if p.paid_on else None,
             "updated_at": p.updated_at.isoformat() if p.updated_at else None} for p in rows]


def merge_payments(conn, rows):
    """Take the server's status and paid date where its change is newer."""
    have = conn.execute("SELECT name FROM sqlite_master WHERE name = 'payments'").fetchone()
    if not have:
        return 0
    taken = 0
    for row in rows:
        local = conn.execute(
            "SELECT updated_at FROM payments WHERE creator_id = ? AND campaign_id = ?",
            (row["creator_id"], row["campaign_id"])).fetchone()
        if local is None or _when(local[0]) >= _when(row.get("updated_at")):
            continue
        conn.execute("UPDATE payments SET status = ?, paid_on = ?, updated_at = ? "
                     "WHERE creator_id = ? AND campaign_id = ?",
                     (row["status"], row.get("paid_on"), _sql_time(row.get("updated_at")),
                      row["creator_id"], row["campaign_id"]))
        taken += 1
    conn.commit()
    return taken
