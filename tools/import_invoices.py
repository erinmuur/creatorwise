#!/usr/bin/env python3
"""Fill creators' payment details from their invoices (PDF or Word). Erin runs this.

Bank transfer or PayPal: an invoice with full bank details makes them paid by
bank transfer; one with only a PayPal address ("PayPal: x@y", paypal.me/x)
makes them paid by PayPal.

    ./venv/bin/python tools/import_invoices.py ~/Desktop/invoices
    ./venv/bin/python tools/import_invoices.py ~/Desktop/invoices --apply

Give it invoices or folders of them (subfolders too): PDF, or Word .docx /
.doc / .rtf — or a creator's reply to the payment email, saved as .eml
("Download message") or printed to PDF. Each is read on this Mac (macOS's own PDFKit, Vision for a
scanned page, textutil for Word), matched to one creator, and
its payee name, address, phone, bank name, account and routing number are
shown — the account and routing numbers masked — so the match can be
checked. Nothing is saved without --apply.

Matching, strongest first: an email address in the invoice that a creator
holds; their exact full name (as payee or sender); their full name in the
file name. Anything else is held and reported — an invoice is never filed
under a guess.

Only what an invoice states is written: a field it doesn't mention keeps
what the profile already had. A routing number that fails the ABA checksum,
or an account number that isn't 4–17 digits, is left out and reported
rather than saved wrong.

This tool exists so the bank numbers go from the invoice to the database
without anybody retyping them — and without Claude handling them: Claude
wrote it and tested it on invented invoices, and Erin runs it. It never
prints a full account or routing number.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT = os.path.join(HERE, "pdf_text.swift")
BINARY = os.path.expanduser("~/Library/Caches/creatorwise/pdf_text")


# --- reading a PDF ---------------------------------------------------------------

def reader():
    if not os.path.exists(BINARY) or os.path.getmtime(BINARY) < os.path.getmtime(SWIFT):
        os.makedirs(os.path.dirname(BINARY), exist_ok=True)
        subprocess.run(["swiftc", "-O", SWIFT, "-o", BINARY], check=True)
    return BINARY


def pdf_text(path, timeout=120):
    try:
        done = subprocess.run([reader(), path], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError(f"reading it took over {timeout}s — enter this one by hand")
    if done.returncode != 0:
        raise ValueError(done.stderr.strip() or "couldn't read the PDF")
    return done.stdout


# Word files are read with macOS's own textutil — no extra installs.
WORD = (".docx", ".doc", ".rtf")
# A creator's reply to the payment email, saved with "Download message"
# (.eml) or as text. Read with Python's email parser.
MAIL = (".eml", ".txt")
KINDS = (".pdf",) + WORD + MAIL

# Where a reply's quoted original starts: everything after is Erin's own
# email, whose blank "Account #" / "Routing #" lines must not be read.
_QUOTE_START = re.compile(r"^(?:on .{4,120} wrote:|-+\s*original message\s*-+|from: .+@readwise\.io)",
                          re.I | re.M)


def mail_text(path):
    """The reply's own words, with its sender's address on top (so it can be
    matched), minus the quoted email it answers."""
    import email
    from email import policy
    if path.lower().endswith(".txt"):
        with open(path, encoding="utf-8", errors="replace") as f:
            return strip_quoted(f.read())
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        body = re.sub(r"<br\s*/?>|</p>|</div>", "\n", body, flags=re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        import html as h
        body = h.unescape(body)
    sender = msg.get("From", "")
    return f"From: {sender}\n{strip_quoted(body)}"


def strip_quoted(text):
    m = _QUOTE_START.search(text or "")
    text = text[: m.start()] if m else text
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def word_text(path, timeout=60):
    try:
        done = subprocess.run(["textutil", "-convert", "txt", "-stdout", path],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError(f"reading it took over {timeout}s — enter this one by hand")
    if done.returncode != 0:
        raise ValueError(done.stderr.strip() or "couldn't read the document")
    return done.stdout


def read_invoice(path):
    """The text of an invoice (PDF, Word) or a saved reply email."""
    if path.lower().endswith(MAIL):
        return mail_text(path)
    if path.lower().endswith(WORD):
        return word_text(path)
    return pdf_text(path)


def find_pdfs(paths):
    """Invoices (PDF or Word) in the given files and folders, subfolders too."""
    out = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(names):
                    if name.lower().endswith(KINDS) and not name.startswith((".", "~$")):
                        out.append(os.path.join(root, name))
        elif p.lower().endswith(KINDS):
            out.append(p)
    return out


# --- pulling the fields out ---------------------------------------------------------

# Labels that start a new field: a value stops where the next one begins,
# because PDF text often runs two printed lines into one.
_STOP = (r"(?=\s+(?:bank(?:\s+name)?|account(?:\s+(?:holder|name|number|no|num|#))?|acct|"
         r"routing|aba|swift|bic|iban|address|phone|tel|mobile|cell|email|e-mail|"
         r"invoice|date|amount|total|due)\b\s*[:#]|\s{2,}|\s+[\w.+-]+@|$)")

_NUMBER = r"([0-9](?:[0-9 -]{0,30}[0-9])?)"

ACCOUNT = [
    re.compile(r"\b(?:bank\s+)?(?:account|acct)\.?\s*(?:number|no\.?|num|#)\s*[:#.]?\s*" + _NUMBER, re.I),
    re.compile(r"\b(?:checking|savings)\s+(?:account\s*)?(?:number|no\.?|#)?\s*[:#]?\s*" + _NUMBER, re.I),
    re.compile(r"\baccount\s*:\s*" + _NUMBER, re.I),
]
ROUTING = [
    re.compile(r"\b(?:ach\s+|wire\s+)?(?:routing|aba|rtn)\s*(?:transit\s*)?(?:number|no\.?|num|#)?"
               r"\s*[:#.]?\s*" + _NUMBER, re.I),
]
BANK = re.compile(r"\bbank(?:\s+name)?\s*[:\-]\s*(.+?)" + _STOP, re.I | re.M)
PAYEE = [
    re.compile(r"\b(?:account\s+holder(?:\s+name)?|name\s+on\s+(?:the\s+)?account|"
               r"account\s+name|beneficiary(?:\s+name)?|"
               r"full\s+name(?:\s+associated?\s+with\s+(?:the\s+)?(?:bank\s+)?account)?)"
               r"\s*[:\-]\s*(.+?)" + _STOP, re.I | re.M),
    re.compile(r"\b(?:payee|pay\s+to|bill(?:ed)?\s+from|from)\s*[:\-]\s*(.+?)" + _STOP, re.I | re.M),
    re.compile(r"(?:^|\s{2,})name\s*[:\-]\s*(.+?)" + _STOP, re.I | re.M),
]
PHONE_LABELLED = re.compile(r"\b(?:phone|tel|telephone|mobile|cell)\s*(?:number|no\.?|#)?\s*[:\-]?\s*"
                            r"(\+?[\d().\s-]{10,20}\d)", re.I)
PHONE_ANY = re.compile(r"(?<![\d-])(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d-])")
ADDRESS_LABELLED = re.compile(r"\b(?:mailing\s+|billing\s+|home\s+)?address\s*[:\-]\s*(.+?)" + _STOP,
                              re.I | re.M)
STREET = re.compile(r"^\s*\d+[A-Za-z]?\s+\S.*\b(st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|"
                    r"dr|drive|way|ct|court|pl|place|pkwy|parkway|cir|circle|ter|terrace|hwy|"
                    r"highway|apt|suite|unit)\b", re.I)
CITY_LINE = re.compile(r"[A-Za-z .'-]+,?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PAYPAL = re.compile(r"pay\s*pal[^@\n]{0,40}?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.I)
PAYPAL_ME = re.compile(r"\b(paypal\.me/[\w.-]+)", re.I)


def _first(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1).strip(" .,;:-")
    return None


def _clean(value, limit=200):
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" .,;:-")
    return value[:limit] or None


# What a "From:"/"Name:" pattern catches when an invoice prints its labels
# side by side ("From  To" over two columns): a label, or the client.
_NOT_A_NAME = re.compile(r"^(?:to|from|bill(?:ed)?\s+to|ship\s+to|pay(?:ee)?|pay\s*pal\b.*|"
                         r"venmo\b.*|zelle\b.*|invoice\b.*|"
                         r"date\b.*|amount\b.*|description\b.*|n/?a|none|-+)$", re.I)


def _payee(text):
    """The account holder or sender, if the invoice states one plainly.
    A label or the invoiced company (Readwise) is not a name: blank beats
    wrong."""
    for p in PAYEE:
        for m in p.finditer(text):
            value = _clean(m.group(1))
            if value and not _NOT_A_NAME.match(value) and "readwise" not in value.lower() \
                    and re.search(r"[A-Za-z]{2}", value):
                return value
    return None


def parse(text):
    """The payment fields an invoice states, as written. Missing ones are
    None. Pure: no validation beyond shape, no database."""
    fields = {"payee_name": None, "address": None, "phone": None, "bank_name": None,
              "account_number": None, "routing_number": None, "paypal": None}
    m = PAYPAL.search(text) or PAYPAL_ME.search(text)
    if m:
        fields["paypal"] = m.group(1).lower().strip(".")
    fields["account_number"] = _first(ACCOUNT, text)
    fields["routing_number"] = _first(ROUTING, text)
    m = BANK.search(text)
    fields["bank_name"] = _clean(m.group(1), 120) if m else None
    fields["payee_name"] = _payee(text)

    numbers = {re.sub(r"\D", "", v) for v in (fields["account_number"],
                                              fields["routing_number"]) if v}
    phone = PHONE_LABELLED.search(text)
    candidate = phone.group(1) if phone else None
    if not candidate:
        for m in PHONE_ANY.finditer(text):
            if re.sub(r"\D", "", m.group(0)) not in numbers:
                candidate = m.group(0)
                break
    fields["phone"] = _clean(candidate, 40)

    fields["addresses"] = addresses(text)
    fields["address"] = fields["addresses"][0] if fields["addresses"] else None
    return fields


def addresses(text):
    """Every address on the invoice, labelled ones first. The invoice's own
    "bill to" (Readwise) is one of them; plan() drops it."""
    found = []

    def add(parts):
        value = "\n".join(re.sub(r",\s*,", ",", re.sub(r"\s+", " ", p)).strip(" ,")
                          for p in parts if p and p.strip(" ,"))[:600]
        if value and value not in found:
            found.append(value)

    lines = [l.strip() for l in text.splitlines()]
    for m in ADDRESS_LABELLED.finditer(text):
        first = m.group(1).strip()
        parts = [first]
        at = next((i for i, l in enumerate(lines) if first[:20] and first[:20] in l), None)
        if at is not None and not CITY_LINE.search(first):
            for nxt in lines[at + 1: at + 3]:
                if nxt and not re.search(r"[:@]", nxt):
                    parts.append(nxt)
                    if CITY_LINE.search(nxt):
                        break
        add(parts)
    for i, line in enumerate(lines):
        if STREET.search(line):
            parts = [line]
            if not CITY_LINE.search(line) and i + 1 < len(lines) and CITY_LINE.search(lines[i + 1]):
                parts.append(lines[i + 1])
            add(parts)
    return found


def emails_in(text):
    return sorted({e.lower().strip(".") for e in EMAIL.findall(text or "")
                   if not e.lower().endswith("@readwise.io")})


def mask(number):
    digits = re.sub(r"\D", "", number or "")
    return f"••••{digits[-4:]}" if digits else "—"


# --- matching an invoice to a creator ---------------------------------------------------

def file_names(filename):
    """Names a file name could be giving, for "Name - 2026 - Campaign.pdf".

    "Joseph (Joe) Stusynski" gives "joseph stusynski", "joe stusynski" and
    "joe"; "Olivia (Liv Devine) Evanoff" gives "olivia evanoff" and
    "liv devine" — a nickname in brackets is often how they're on file.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    head = re.split(r"\s+[-–—]\s+", stem)[0]
    head = re.sub(r"[_]+", " ", head).strip()
    out = {re.sub(r"\s+", " ", head).casefold()}
    m = re.search(r"\(([^)]+)\)", head)
    if m:
        inner = m.group(1).strip()
        outer = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", head)).strip()
        out |= {outer.casefold(), inner.casefold()}
        last = outer.split()[-1] if outer.split() else ""
        if last and last.casefold() not in inner.casefold():
            out.add(f"{inner} {last}".casefold())
    return out


_TOKEN = re.compile(r"[a-z0-9']+")


def _tokens(name):
    return _TOKEN.findall(re.sub(r"\([^)]*\)", " ", (name or "").casefold()).replace("’", "'"))


def people(payee, filename):
    """(first, last) pairs the invoice could be naming: its payee, and the
    file name with any bracketed nickname or maiden name tried in turn."""
    out = []
    names = [payee] if payee else []
    stem = os.path.splitext(os.path.basename(filename))[0].lstrip("_ ")
    head = re.split(r"\s*[-–—]\s+\d|\s+[-–—]\s+", stem)[0].strip()
    names.append(head)
    m = re.search(r"\(([^)]+)\)", head)
    if m:
        inner, outer = m.group(1), re.sub(r"\([^)]*\)", " ", head)
        o = _tokens(outer)
        names += [inner, f"{inner} {o[-1]}" if o else inner,
                  f"{o[0]} {inner}" if o else inner]
    for n in names:
        t = _tokens(n)
        if t:
            pair = (t[0], t[-1] if len(t) > 1 else None)
            if pair not in out:
                out.append(pair)
    return out


def _first_fits(a, b):
    """Allie/Allison, Sam/Samantha, Becca/Rebecca, Liv/Olivia."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 2 and long_.startswith(short):
        return True
    if len(short) >= 4 and short in long_:
        return True
    shared = len(os.path.commonprefix([short, long_]))
    return shared >= max(3, len(short) - 1)        # Allie / Allison


def close_matches(candidates, creators):
    """Creators an invoice's name plausibly is, when no exact name matched.

    Same surname and a fitting first name (a nickname, a short form, a
    middle name in between); a surname given as an initial ("Juno W"); or,
    for creators on file by first name only, the invoice's surname in one of
    their addresses (alyssa + trask@…) or a first name nobody else has.
    Shown as "close name — check it": Erin confirms in the preview.
    """
    firsts = {}
    for c in creators:
        t = _tokens(c.name)
        if t:
            firsts.setdefault(t[0], []).append(c)
    hits = []
    for c in creators:
        t = _tokens(c.name)
        if not t:
            continue
        for first, last in candidates:
            if not _first_fits(t[0], first):
                continue
            if len(t) > 1 and last:
                surname = t[-1]
                if surname == last or (len(surname) == 1 and last.startswith(surname)):
                    hits.append(c)
                    break
            elif len(t) == 1:
                addresses_ = [c.email or ""] + [e.address for e in c.emails]
                if last and len(last) >= 4 and any(last in (a or "").lower() for a in addresses_):
                    hits.append(c)
                    break
                fitting = [x for x in creators if _tokens(x.name)
                           and _first_fits(_tokens(x.name)[0], first)]
                if fitting == [c]:
                    hits.append(c)
                    break
    seen, out = set(), []
    for c in hits:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


def match(text, filename, fields, creators, holders):
    """(creator, how) or (None, why)."""
    ids = set()
    for address in emails_in(text):
        ids |= holders.get(address, set())
    by_id = {c.id: c for c in creators}
    if len(ids) == 1:
        return by_id[next(iter(ids))], "email"
    if len(ids) > 1:
        return None, "its email address belongs to more than one creator: " + ", ".join(
            sorted(by_id[i].name for i in ids))

    def by_full_name(name):
        want = re.sub(r"\s+", " ", (name or "").strip()).casefold()
        return [c for c in creators if want and c.name.strip().casefold() == want]

    hits = by_full_name(fields.get("payee_name"))
    if len(hits) == 1:
        return hits[0], "name"
    candidates = file_names(filename)
    named = [c for c in creators if len(c.name.split()) > 1
             and c.name.strip().casefold() in candidates]
    if not named:
        stem = " ".join(candidates)
        named = [c for c in creators if len(c.name.split()) > 1 and
                 re.search(r"\b" + re.escape(c.name.strip().casefold()) + r"\b", stem)]
    if len(named) == 1:
        return named[0], "file name"
    if len(hits) > 1 or len(named) > 1:
        return None, "the name fits more than one creator"
    close = close_matches(people(fields.get("payee_name"), filename), creators)
    if len(close) == 1:
        return close[0], "close name — check it"
    if len(close) > 1:
        return None, "close to more than one creator: " + ", ".join(sorted(c.name for c in close))
    return None, ("no creator matches — rename the file to include their full name as it "
                  "is in creatorwise, or add them as a creator first")


# --- plan / apply -------------------------------------------------------------------------

def plan(files, creators, holders, read=read_invoice):
    """One entry per file: creator, how, fields to write, problems."""
    from payments import routing_ok
    out, texts = [], {}
    for path in files:
        try:
            texts[path] = read(path)
        except Exception as error:   # a broken file shouldn't stop the others
            texts[path] = error
    # The invoices' own "bill to" (Readwise's address) is on every one of
    # them: an address on three or more different files is that, not theirs.
    counts = {}
    for text in texts.values():
        if isinstance(text, str):
            for a in set(addresses(text)):
                counts[a] = counts.get(a, 0) + 1
    shared = {a for a, n in counts.items() if n >= 3}

    for path in files:
        entry = {"file": path, "creator": None, "how": None, "fields": {}, "problems": []}
        out.append(entry)
        text = texts[path]
        if isinstance(text, Exception):
            entry["problems"].append(f"couldn't read it ({text})")
            continue
        if not text.strip():
            entry["problems"].append("no text found in it")
            continue
        fields = parse(text)
        own = [a for a in fields.pop("addresses", []) if a not in shared]
        fields["address"] = own[0] if own else None
        account = re.sub(r"[\s-]", "", fields["account_number"] or "")
        routing = re.sub(r"[\s-]", "", fields["routing_number"] or "")
        if account and not re.fullmatch(r"\d{4,17}", account):
            entry["problems"].append(f"account number {mask(account)} isn't 4–17 digits — left out")
            account = ""
        if routing and not routing_ok(routing):
            entry["problems"].append(f"routing number {mask(routing)} fails the bank checksum "
                                     "(a misread or a typo) — left out")
            routing = ""
        fields["account_number"], fields["routing_number"] = account or None, routing or None
        # How they're paid: full bank details mean a bank transfer; otherwise
        # a PayPal address on the invoice means PayPal.
        paypal = fields.pop("paypal", None)
        if fields["account_number"] and fields["routing_number"]:
            fields["payment_method"] = "bank"
        elif paypal:
            fields["payment_method"], fields["payment_handle"] = "paypal", paypal
        creator, how = match(text, path, fields, creators, holders)
        if creator is None:
            entry["problems"].append(how)
            entry["found"] = {k: v for k, v in fields.items() if v}
            continue
        entry["creator"], entry["how"] = creator, how
        entry["fields"] = {k: v for k, v in fields.items() if v}
        if not entry["fields"]:
            entry["problems"].append("no payment details found in it")
    return out


def render(entries):
    lines = []
    labels = [("payment_method", "paid by"), ("payment_handle", "paypal"),
              ("payee_name", "name"), ("address", "address"), ("phone", "phone"),
              ("bank_name", "bank"), ("account_number", "account"), ("routing_number", "routing")]
    from payments import METHODS as methods
    for e in entries:
        who = f"{e['creator'].name} (matched by {e['how']})" if e["creator"] else "NOT MATCHED"
        lines.append(f"{os.path.basename(e['file'])} -> {who}")
        current = e["creator"].payment_details if e["creator"] else None
        shown_fields = e["fields"] or e.get("found") or {}
        if not e["creator"] and shown_fields:
            lines.append("    found (not saved):")
        for key, label in labels:
            if key not in shown_fields:
                continue
            value = shown_fields[key]
            shown = mask(value) if key in ("account_number", "routing_number") \
                else methods.get(value, value) if key == "payment_method" \
                else value.replace("\n", ", ")
            holder = e["creator"] if key in ("payment_method", "payment_handle") else current
            old = getattr(holder, key, None) if holder else None
            note = "" if not old or old == value else "   (replaces what's on file)"
            lines.append(f"    {label:<8} {shown}{note}")
        if e["creator"]:
            wanted = (["payment_handle"] if e["fields"].get("payment_method") == "paypal"
                      else ["payee_name", "address", "phone", "bank_name",
                            "account_number", "routing_number"])
            missing = [label for key, label in labels if key in wanted and key not in e["fields"]]
            if missing:
                lines.append(f"    not in the invoice: {', '.join(missing)}")
        for p in e["problems"]:
            lines.append(f"    ! {p}")
    return "\n".join(lines)


def apply(entries, db, now=None):
    from models import PaymentDetails
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    saved = 0
    for e in entries:
        if not e["creator"] or not e["fields"]:
            continue
        row = e["creator"].payment_details
        if row is None:
            row = PaymentDetails(creator_id=e["creator"].id)
            db.session.add(row)
        for key, value in e["fields"].items():
            if key in ("payment_method", "payment_handle"):
                setattr(e["creator"], key, value)
            else:
                setattr(row, key, value)
        if e["fields"].get("payment_method") == "bank":
            e["creator"].payment_handle = None
        row.updated_at = now
        saved += 1
    db.session.commit()
    return saved


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="invoices (PDF or Word) or folders of them")
    parser.add_argument("--apply", action="store_true", help="save what the preview shows")
    args = parser.parse_args(argv)

    files = find_pdfs(args.paths)
    if not files:
        print("No invoices or saved replies (PDF, Word, .eml) found there.")
        return 1

    import logging
    logging.disable(logging.WARNING)   # the app's "scanner dormant" notice isn't news here
    import app as appmod
    from models import Creator, db
    from tools.content_requests import address_holders
    logging.disable(logging.NOTSET)
    with appmod.app.app_context():
        creators = Creator.query.all()
        entries = plan(files, creators, address_holders(creators))
        print(render(entries))
        ready = sum(1 for e in entries if e["creator"] and e["fields"])
        print(f"\n{ready} of {len(entries)} invoice(s) ready to save.")
        if not args.apply:
            print("Check the matches above, then run again with --apply to save them.")
            return 0
        uri = appmod.app.config["SQLALCHEMY_DATABASE_URI"]
        db_path = uri.replace("sqlite:///", "", 1)
        if os.path.exists(db_path):
            shutil.copyfile(db_path, db_path + ".bak-invoices")
        saved = apply(entries, db)
        print(f"Saved payment details for {saved} creator(s). They reach the live site "
              "with the next upload (the scheduled jobs do one), or run "
              "tools/push_data.py --apply now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
