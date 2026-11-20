"""Creators' payment details: checking and saving what the profile form sends.

Bank numbers are stored as Erin decided (see models.PaymentDetails). This
module never logs or prints them; errors name the field, not its value.
"""

import re
from datetime import datetime

from models import PaymentDetails, db

LIMITS = {"payee_name": 200, "phone": 40, "bank_name": 120, "address": 600}


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
    for field, value in values.items():
        setattr(row, field, value)
    row.updated_at = now or datetime.utcnow()
    db.session.commit()
    return []


# --- carrying live-site edits back to the Mac --------------------------------

def as_rows(details):
    """PaymentDetails rows as plain dicts, for /api/data/payment-details."""
    out = []
    for row in details:
        item = {f: getattr(row, f) for f in PaymentDetails.FIELDS}
        item["creator_id"] = row.creator_id
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
