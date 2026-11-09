"""Load creators from a spreadsheet export into creatorwise.

Built to meet real exports where they are. Betawise, Notion and a hand-kept
Google Sheet all name the same column differently and none of them agrees with
`models.py`, so this maps on aliases rather than demanding an exact header row.
Add to ALIASES when a new export turns up a name we haven't seen.

Two phases, always:

    plan(rows)   -> what would happen, touching nothing
    apply(plan)  -> commits it

The CLI dry-runs by default and needs `--apply` to write. That isn't ceremony:
the first run of a real export is how you find out that the follower column is
"12.4K" strings, or that three people share an email. Better to read that as a
report than to find it in the database.

Idempotent on lowercased email, so re-running an updated export updates the
creators that changed and leaves the rest alone.

Nothing here guesses at a conflict. A row that can't be resolved — no email, no
name, two different names on one address — is reported as blocked, with the
reason and the row number, and skipped. Half an import is recoverable; a
silently mangled one isn't.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from models import (
    PLATFORMS,
    RECRUITMENT_SOURCES,
    Campaign,
    Creator,
    Participation,
    SocialHandle,
    db,
)

# --- column aliases -----------------------------------------------------------

# Keys are our field names; values are header spellings seen in the wild.
# Matching is done on a normalized header (lowercased, punctuation to spaces,
# whitespace collapsed), so "Creator Name", "creator_name" and "CREATOR NAME"
# all arrive here as "creator name".
ALIASES = {
    "name": [
        "name", "creator", "creator name", "full name", "display name",
        "contact name", "creator full name",
    ],
    "email": [
        "email", "e mail", "email address", "e mail address", "contact email",
        "primary email", "creator email",
    ],
    "recruitment_source": [
        "source", "recruitment source", "recruited via", "recruited from",
        "channel", "how recruited", "acquisition source",
    ],
    "recruitment_note": [
        "recruitment note", "source note", "how we found them", "referred by",
        "recruitment notes",
    ],
    "notes": ["notes", "note", "comments", "internal notes", "remarks"],
    "payment_method": [
        "payment method", "pay via", "payment type", "paid via", "payout method",
    ],
    "payment_handle": [
        "payment handle", "paypal", "venmo", "wise", "payment email",
        "paypal email", "venmo handle", "payout handle", "payment details",
    ],
    "payment_note": ["payment note", "payment notes", "payout note"],
    "instagram": [
        "instagram", "ig", "insta", "instagram handle", "ig handle",
        "instagram username", "instagram url", "instagram link",
    ],
    "tiktok": [
        "tiktok", "tik tok", "tiktok handle", "tiktok username", "tt",
        "tiktok url", "tiktok link",
    ],
    "youtube": [
        "youtube", "yt", "youtube handle", "youtube channel", "youtube url",
        "youtube link", "channel url",
    ],
    "instagram_followers": [
        "instagram followers", "ig followers", "instagram follower count",
        "ig follower count", "instagram reach",
    ],
    "tiktok_followers": [
        "tiktok followers", "tt followers", "tiktok follower count",
        "tiktok reach",
    ],
    "youtube_followers": [
        "youtube followers", "yt followers", "youtube subscribers",
        "yt subscribers", "subscribers", "subscriber count",
    ],
    "followers": [
        "followers", "follower count", "audience", "audience size",
        "total followers", "reach",
    ],
    "status": [
        "status", "participation status", "delivery status", "follow through",
        "follow up status",
    ],
    "opted_in_on": [
        "opted in", "opted in on", "opt in date", "signed up", "joined",
        "date opted in", "confirmed on",
    ],
}

# Built once: normalized header -> our field name.
_HEADER_MAP = {alias: field for field, names in ALIASES.items() for alias in names}

PLATFORM_FIELDS = {
    "instagram": ("instagram", "instagram_followers"),
    "tiktok": ("tiktok", "tiktok_followers"),
    "youtube": ("youtube", "youtube_followers"),
}

# Values that mean "blank" in a hand-maintained sheet.
BLANKS = {"", "-", "—", "–", "n/a", "na", "none", "null", "tbd", "?", "unknown"}


def normalize_header(raw):
    """"Instagram Handle " -> "instagram handle". Also strips a UTF-8 BOM."""
    text = (raw or "").replace("﻿", "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean(value):
    """Trim, normalize smart quotes, and turn placeholder text into ''."""
    if value is None:
        return ""
    text = str(value).replace(" ", " ").strip()
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return "" if text.lower() in BLANKS else text


# --- value parsing ------------------------------------------------------------

EMAIL_RE = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}")


def parse_emails(value):
    """All addresses in a cell, lowercased, in order, de-duplicated.

    Sheets routinely hold "a@x.com / b@y.com" or "a@x.com (personal)". The
    first is treated as primary; the rest are reported, because Creator.email
    is unique and a second address needs a human decision, not a guess.
    """
    out, seen = [], set()
    for match in EMAIL_RE.findall(clean(value)):
        addr = match.lower().strip(".,;")
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


_FOLLOWER_RE = re.compile(r"^~?\s*([\d.,]+)\s*([kKmM])?\b")


def parse_followers(value):
    """"12.4K" -> 12400, "1,234" -> 1234, "1.2M" -> 1200000, junk -> None."""
    text = clean(value)
    if not text:
        return None
    match = _FOLLOWER_RE.match(text)
    if not match:
        return None

    number, suffix = match.group(1), (match.group(2) or "").lower()
    # A comma is a thousands separator here, never a decimal point: these are
    # follower counts from US-facing tools, and "1,234" must not become 1.234.
    number = number.replace(",", "")
    if number.count(".") > 1:
        number = number.replace(".", "")
    try:
        amount = float(number)
    except ValueError:
        return None

    amount *= {"k": 1_000, "m": 1_000_000}.get(suffix, 1)
    return int(round(amount))


_HANDLE_FROM_URL = re.compile(
    r"(?:instagram\.com|tiktok\.com|youtube\.com|youtu\.be)/+(?:@)?([^/?#\s]+)", re.I
)
# Path segments that are the site's own, not somebody's handle.
_NOT_A_HANDLE = {"p", "reel", "reels", "tv", "watch", "shorts", "channel", "c", "user"}


def parse_handle(value):
    """Return (handle, profile_url). Accepts "@foo", "foo", or any profile URL."""
    text = clean(value)
    if not text:
        return None, None

    url = text if re.match(r"^https?://", text, re.I) else None
    match = _HANDLE_FROM_URL.search(text)
    if match:
        handle = match.group(1)
        if handle.lower() in _NOT_A_HANDLE:
            return None, url
        if not url:
            url = "https://" + text.lstrip("/")
    else:
        handle = text

    handle = handle.strip().lstrip("@").strip()
    # A whole sentence in the cell ("ask her for it") isn't a handle.
    if not handle or " " in handle or not re.match(r"^[A-Za-z0-9._-]+$", handle):
        return None, url
    return "@" + handle, url


DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y",
    "%B %d, %Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y",
)


def parse_date(value):
    text = clean(value)
    if not text:
        return None
    text = text.split("T")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


SOURCE_SYNONYMS = {
    "betawise": "betawise", "beta": "betawise", "beta wise": "betawise",
    "beta tester": "betawise", "sticker beta": "betawise",
    "outbound": "outbound", "cold outreach": "outbound", "outreach": "outbound",
    "dm": "outbound", "cold dm": "outbound", "prospected": "outbound",
    "referral": "referral", "referred": "referral", "word of mouth": "referral",
    "friend": "referral",
    "inbound": "inbound", "applied": "inbound", "reached out": "inbound",
    "application": "inbound", "waitlist": "inbound",
    "agency": "agency", "talent agency": "agency", "managed": "agency",
    "agent": "agency",
}

PAYMENT_SYNONYMS = {
    "paypal": "paypal", "pay pal": "paypal", "pp": "paypal",
    "venmo": "venmo",
    "wise": "wise", "transferwise": "wise", "transfer wise": "wise",
    "gift card": "gift_card", "giftcard": "gift_card", "gift": "gift_card",
    "amazon gift card": "gift_card", "amazon": "gift_card",
}

STATUS_SYNONYMS = {
    "opted in": "opted_in", "opted_in": "opted_in", "confirmed": "opted_in",
    "yes": "opted_in", "in": "opted_in", "signed up": "opted_in",
    "pending": "opted_in", "accepted": "opted_in",
    "delivered": "content_delivered", "content delivered": "content_delivered",
    "complete": "content_delivered", "completed": "content_delivered",
    "posted": "content_delivered", "done": "content_delivered",
    "no show": "no_show", "no_show": "no_show", "ghosted": "no_show",
    "dropped": "no_show", "declined": "no_show", "no": "no_show",
    "didn t deliver": "no_show", "never posted": "no_show",
}


def _lookup(value, table):
    text = normalize_header(clean(value))
    return table.get(text) if text else None


def parse_source(value):
    return _lookup(value, SOURCE_SYNONYMS)


def parse_payment_method(value):
    return _lookup(value, PAYMENT_SYNONYMS)


def parse_status(value):
    return _lookup(value, STATUS_SYNONYMS)


# --- reading the file ---------------------------------------------------------

def read_rows(text):
    """Parse CSV text into (line_number, {our_field: value}) plus header info.

    Returns (rows, mapped, unmapped, duplicate_headers). Unrecognized columns
    are reported rather than dropped silently — an export with a "Payment
    Details" column we don't know about should be visible, not invisible.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], {}, [], []

    mapped, unmapped, duplicate_headers = {}, [], []
    columns = []  # index -> our field name or None
    for raw in header:
        norm = normalize_header(raw)
        field_name = _HEADER_MAP.get(norm)
        if field_name is None:
            columns.append(None)
            if norm:
                unmapped.append(raw.strip())
        elif field_name in mapped:
            # Two columns claim the same field. First wins; say so.
            columns.append(None)
            duplicate_headers.append(f"{raw.strip()} (already using {mapped[field_name]!r})")
        else:
            columns.append(field_name)
            mapped[field_name] = raw.strip()

    rows = []
    for line, record in enumerate(reader, start=2):
        if not any(clean(c) for c in record):
            continue  # blank spacer row
        values = {}
        for index, cell in enumerate(record):
            if index < len(columns) and columns[index]:
                values[columns[index]] = cell
        rows.append((line, values))

    return rows, mapped, unmapped, duplicate_headers


# --- the plan -----------------------------------------------------------------

@dataclass
class RowPlan:
    line: int
    action: str                      # create | update | unchanged | blocked
    name: str = ""
    email: str = ""
    reason: str = ""                 # why it's blocked
    fields: dict = field(default_factory=dict)
    handles: list = field(default_factory=list)
    status: str = None
    opted_in_on: date = None
    warnings: list = field(default_factory=list)
    existing_id: int = None
    changes: dict = field(default_factory=dict)   # field -> (old, new)


@dataclass
class ImportPlan:
    rows: list = field(default_factory=list)
    mapped: dict = field(default_factory=dict)
    unmapped: list = field(default_factory=list)
    duplicate_headers: list = field(default_factory=list)
    campaign: object = None
    campaign_error: str = ""

    def of(self, action):
        return [r for r in self.rows if r.action == action]

    @property
    def counts(self):
        return {
            a: len(self.of(a))
            for a in ("create", "update", "unchanged", "blocked")
        }

    @property
    def ok(self):
        return not self.campaign_error


# Creator columns a row may set, in the order they're reported.
CREATOR_FIELDS = (
    "name", "recruitment_source", "recruitment_note", "notes",
    "payment_method", "payment_handle", "payment_note",
)


def _row_handles(values, warnings):
    out = []
    general = parse_followers(values.get("followers"))

    for platform, (handle_col, follower_col) in PLATFORM_FIELDS.items():
        handle, url = parse_handle(values.get(handle_col))
        raw = clean(values.get(handle_col))
        if raw and not handle:
            warnings.append(f"{platform}: couldn't read a handle from {raw!r}")
        if not handle:
            continue

        followers = parse_followers(values.get(follower_col))
        raw_followers = clean(values.get(follower_col))
        if raw_followers and followers is None:
            warnings.append(
                f"{platform} followers: couldn't read {raw_followers!r}, left empty"
            )
        out.append({
            "platform": platform,
            "handle": handle,
            "follower_count": followers,
            "profile_url": url,
        })

    # A single "Followers" column only makes sense when there's one platform to
    # attribute it to. With two, guessing would invent data.
    if general is not None:
        without = [h for h in out if h["follower_count"] is None]
        if len(out) == 1:
            out[0]["follower_count"] = out[0]["follower_count"] or general
        elif without:
            warnings.append(
                f"followers={general:,} not applied: "
                f"{len(out)} platforms on this row, can't tell which it refers to"
            )
    return out


def plan(text, campaign_slug=None):
    """Work out what importing this file would do. Reads the DB, writes nothing."""
    rows, mapped, unmapped, duplicate_headers = read_rows(text)
    result = ImportPlan(
        mapped=mapped, unmapped=unmapped, duplicate_headers=duplicate_headers
    )

    if campaign_slug:
        result.campaign = Campaign.query.filter_by(slug=campaign_slug).first()
        if not result.campaign:
            known = ", ".join(c.slug for c in Campaign.query.all()) or "none"
            result.campaign_error = (
                f"no campaign with slug {campaign_slug!r} (have: {known})"
            )
            return result

    if "email" not in mapped:
        result.campaign_error = (
            "no email column found — creators are matched on email, so the "
            f"import can't run. Columns seen: {', '.join(unmapped) or 'none'}"
        )
        return result

    seen_in_file = {}

    for line, values in rows:
        warnings = []
        name = clean(values.get("name"))
        emails = parse_emails(values.get("email"))

        if not emails:
            result.rows.append(RowPlan(
                line=line, action="blocked", name=name,
                reason="no usable email address",
            ))
            continue
        if not name:
            result.rows.append(RowPlan(
                line=line, action="blocked", email=emails[0],
                reason="no name",
            ))
            continue

        email = emails[0]
        if len(emails) > 1:
            warnings.append(
                "extra addresses in this cell, not imported: "
                + ", ".join(emails[1:])
            )

        if email in seen_in_file:
            first_line, first_name = seen_in_file[email]
            note = f"duplicate of line {first_line}"
            if first_name.lower() != name.lower():
                note += f", under a different name ({first_name!r})"
            result.rows.append(RowPlan(
                line=line, action="blocked", name=name, email=email, reason=note,
            ))
            continue
        seen_in_file[email] = (line, name)

        # Creator fields. A blank cell means "no new information", never
        # "erase what's there" — exports are routinely partial.
        fields = {"name": name}

        source = parse_source(values.get("recruitment_source"))
        raw_source = clean(values.get("recruitment_source"))
        if source:
            fields["recruitment_source"] = source
        elif raw_source:
            warnings.append(
                f"recruitment source {raw_source!r} isn't one of "
                f"{'/'.join(RECRUITMENT_SOURCES)} — kept as a note instead"
            )
            fields["recruitment_note"] = raw_source

        method = parse_payment_method(values.get("payment_method"))
        raw_method = clean(values.get("payment_method"))
        if method:
            fields["payment_method"] = method
        elif raw_method:
            warnings.append(f"payment method {raw_method!r} not recognized, kept as a note")
            fields["payment_note"] = raw_method

        for key in ("recruitment_note", "notes", "payment_handle", "payment_note"):
            value = clean(values.get(key))
            if value:
                # Don't let a note parsed above get clobbered by an empty column.
                fields[key] = value if key not in fields else f"{fields[key]} · {value}"

        handles = _row_handles(values, warnings)

        status = parse_status(values.get("status"))
        raw_status = clean(values.get("status"))
        if raw_status and not status:
            warnings.append(f"status {raw_status!r} not recognized, left as opted in")

        opted = parse_date(values.get("opted_in_on"))
        raw_opted = clean(values.get("opted_in_on"))
        if raw_opted and not opted:
            warnings.append(f"couldn't read date {raw_opted!r}")

        existing = Creator.query.filter(
            db.func.lower(Creator.email) == email
        ).first()

        row = RowPlan(
            line=line, action="create", name=name, email=email,
            fields=fields, handles=handles, status=status or "opted_in",
            opted_in_on=opted, warnings=warnings,
        )

        if existing:
            row.existing_id = existing.id
            changes = {}
            for key, value in fields.items():
                current = getattr(existing, key, None)
                if value and value != current:
                    changes[key] = (current, value)
            for handle in handles:
                current = next(
                    (h for h in existing.handles if h.platform == handle["platform"]),
                    None,
                )
                if current is None:
                    changes[f"{handle['platform']} handle"] = (None, handle["handle"])
                else:
                    if handle["handle"] != current.handle:
                        changes[f"{handle['platform']} handle"] = (
                            current.handle, handle["handle"]
                        )
                    if (handle["follower_count"] is not None
                            and handle["follower_count"] != current.follower_count):
                        changes[f"{handle['platform']} followers"] = (
                            current.follower_count, handle["follower_count"]
                        )
            row.changes = changes
            row.action = "update" if changes else "unchanged"

        result.rows.append(row)

    return result


# --- applying it --------------------------------------------------------------

def apply(plan_result):
    """Commit a plan. Returns a summary dict.

    One transaction: a file that fails halfway leaves nothing behind, so the
    fix is to correct the file and re-run, not to work out how far it got.
    """
    if not plan_result.ok:
        raise ValueError(plan_result.campaign_error)

    created = updated = 0
    participations = 0

    try:
        for row in plan_result.rows:
            if row.action == "blocked":
                continue
            # "unchanged" still runs: the creator needs nothing, but a campaign
            # link may still be missing.

            creator = (
                db.session.get(Creator, row.existing_id)
                if row.existing_id else None
            )
            if creator is None:
                creator = Creator(email=row.email, name=row.name)
                db.session.add(creator)
                created += 1
            elif row.action == "update":
                updated += 1

            for key, value in row.fields.items():
                if value:
                    setattr(creator, key, value)

            db.session.flush()  # need creator.id for handles

            for handle in row.handles:
                current = next(
                    (h for h in creator.handles if h.platform == handle["platform"]),
                    None,
                )
                if current is None:
                    db.session.add(SocialHandle(
                        creator_id=creator.id,
                        platform=handle["platform"],
                        handle=handle["handle"],
                        follower_count=handle["follower_count"],
                        profile_url=handle["profile_url"],
                    ))
                else:
                    current.handle = handle["handle"]
                    if handle["follower_count"] is not None:
                        current.follower_count = handle["follower_count"]
                    if handle["profile_url"]:
                        current.profile_url = handle["profile_url"]

            if plan_result.campaign:
                link = Participation.query.filter_by(
                    creator_id=creator.id, campaign_id=plan_result.campaign.id
                ).first()
                if link is None:
                    db.session.add(Participation(
                        creator_id=creator.id,
                        campaign_id=plan_result.campaign.id,
                        status=row.status or "opted_in",
                        opted_in_on=row.opted_in_on,
                    ))
                    participations += 1
                else:
                    if row.status:
                        link.status = row.status
                    if row.opted_in_on:
                        link.opted_in_on = row.opted_in_on

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return {
        "created": created,
        "updated": updated,
        "participations": participations,
        "blocked": len(plan_result.of("blocked")),
    }


# --- reporting ----------------------------------------------------------------

def render(plan_result, verbose=False):
    """A readable report of a plan. This is the thing you act on."""
    out = []
    add = out.append

    if not plan_result.ok:
        add(f"CANNOT IMPORT: {plan_result.campaign_error}")
        return "\n".join(out)

    counts = plan_result.counts
    add(f"Columns recognized: {len(plan_result.mapped)}")
    for our_field, header in sorted(plan_result.mapped.items()):
        add(f"    {header!r} -> {our_field}")
    if plan_result.unmapped:
        add("")
        add("Columns NOT recognized (ignored — add them to ALIASES if they matter):")
        for header in plan_result.unmapped:
            add(f"    {header!r}")
    if plan_result.duplicate_headers:
        add("")
        add("Duplicate columns (second one ignored):")
        for header in plan_result.duplicate_headers:
            add(f"    {header}")

    add("")
    add(f"{counts['create']} to create, {counts['update']} to update, "
        f"{counts['unchanged']} unchanged, {counts['blocked']} blocked")
    if plan_result.campaign:
        add(f"All imported creators joined to campaign: {plan_result.campaign.name}")

    blocked = plan_result.of("blocked")
    if blocked:
        add("")
        add(f"BLOCKED ({len(blocked)}) — these rows are skipped:")
        for row in blocked:
            who = row.name or row.email or "(empty)"
            add(f"    line {row.line}: {who} — {row.reason}")

    warned = [r for r in plan_result.rows if r.warnings and r.action != "blocked"]
    if warned:
        add("")
        add(f"WARNINGS ({len(warned)} rows) — imported, but check these:")
        for row in warned:
            for warning in row.warnings:
                add(f"    line {row.line}: {row.name} — {warning}")

    updates = plan_result.of("update")
    if updates:
        add("")
        add(f"UPDATES ({len(updates)}):")
        for row in updates:
            add(f"    line {row.line}: {row.name}")
            for key, (old, new) in row.changes.items():
                add(f"        {key}: {old!r} -> {new!r}")

    if verbose:
        creates = plan_result.of("create")
        if creates:
            add("")
            add(f"NEW CREATORS ({len(creates)}):")
            for row in creates:
                platforms = ", ".join(
                    f"{h['platform']} {h['handle']}"
                    + (f" ({h['follower_count']:,})" if h["follower_count"] else "")
                    for h in row.handles
                ) or "no handles"
                add(f"    line {row.line}: {row.name} <{row.email}> — {platforms}")

    return "\n".join(out)


# --- CLI ----------------------------------------------------------------------

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Import creators from a CSV export. Dry run unless --apply.",
    )
    parser.add_argument("csv_path")
    parser.add_argument("--apply", action="store_true",
                        help="actually write to the database")
    parser.add_argument("--campaign", metavar="SLUG",
                        help="also join every imported creator to this campaign")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="list every new creator, not just problems")
    args = parser.parse_args(argv)

    with open(args.csv_path, encoding="utf-8-sig") as handle:
        text = handle.read()

    from app import app

    with app.app_context():
        result = plan(text, campaign_slug=args.campaign)
        print(render(result, verbose=args.verbose))

        if not result.ok:
            return 1
        if not args.apply:
            print("\n(dry run — nothing written. Re-run with --apply to commit.)")
            return 0

        summary = apply(result)
        print(
            f"\nDone: {summary['created']} created, {summary['updated']} updated, "
            f"{summary['participations']} campaign links added, "
            f"{summary['blocked']} skipped."
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
