"""Load a campaign's content from a CRM export into creatorwise.

The companion to `importer.py`. That one answers "who are our creators?"; this
one answers "who took part in this campaign, and what did they post?" — it
creates the Participation rows and the UgcPiece/PlatformPost rows for one
campaign from a spreadsheet of creator names, profile links and post links.

Same two phases, same habits:

    plan(text, campaign_slug=...)  -> what would happen, touching nothing
    apply(plan)                    -> commits it

The CLI dry-runs by default and needs `--apply`.

It never creates a creator. A row it can't tie to an existing creator with
confidence is reported for review and skipped, because a wrong tie is worse
than a missing one: campaign totals are summed per creator, so attributing one
person's video to another corrupts both their numbers and hides the error.

How a row is tied to a creator
------------------------------
Three independent signals, each either *strong* (the handle matches on the
platform it claims, or the name matches exactly) or *weak* (the handle matches
but on a different platform, or the name only partly matches):

    profile handle   the Handle/Profile URL column
    post handle      the handle inside the post link
    name             the Creator Name column

One strong signal that no other strong signal contradicts is enough. Weak
signals alone are not — two of them agreeing is still a guess, and this export
contains a row where they agree and are probably wrong. Everything else is
reported for review.

Why some posts carry a date and some don't
------------------------------------------
A TikTok post id has its creation time in the top 32 bits, so `posted_on` is
read off the URL rather than left blank or invented. Instagram shortcodes and
YouTube ids carry no retrievable date, and TikTok's `/t/ABC123` shortlinks
don't contain the id at all, so those pieces are stored without one.

No engagement numbers are written. This export has none, and a zero that looks
like a measurement is worse than an obvious blank — the views and likes have to
come from the platforms themselves.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from importer import (
    clean,
    normalize_header,
    parse_date,
    parse_emails,
    parse_status,
    read_rows,
    record_email,
)
from models import (
    PARTICIPATION_STATUSES,
    PLATFORMS,
    REPORTED_WINDOW,
    Campaign,
    Creator,
    CreatorEmail,
    Participation,
    PlatformPost,
    ReferralSnapshot,
    UgcPiece,
    db,
)

# --- column aliases -----------------------------------------------------------

# Keys are our field names, values are header spellings. Matched on a
# normalized header, so "Handle/Profile URL" arrives as "handle profile url".
ALIASES = {
    "name": [
        "creator name", "name", "creator", "full name", "display name",
    ],
    "profile": [
        "handle profile url", "profile url", "profile", "handle", "handle url",
        "creator profile", "profile link", "channel",
    ],
    "posts": [
        "post", "posts", "post url", "post urls", "post link", "post links",
        "content links",
        "content", "content url", "content links", "video", "video url",
        "video link", "deliverable", "deliverables",
    ],
    "status": [
        "status", "participation status", "delivery status", "campaign status",
    ],
    "response": [
        "response", "reply", "outreach response", "their response", "outcome",
    ],
    "email": [
        "email", "e mail", "email address", "contact email", "creator email",
    ],
    "referrals": [
        "referrals", "referral count", "signups", "sign ups", "referred signups",
        "conversions",
    ],
    "referrer": [
        "referrer", "referred by", "recruited by", "invited by",
    ],
    "posted_on": [
        "posted", "posted on", "post date", "date posted", "published",
        "published on",
    ],
    "notes": ["notes", "note", "comments", "internal notes", "remarks"],
}

HEADER_MAP = {alias: name for name, aliases in ALIASES.items() for alias in aliases}

# --- URLs ---------------------------------------------------------------------

# host fragment -> platform. Ordered longest-first on match so youtu.be doesn't
# fall through to a youtube.com test that never fires.
_HOSTS = (
    ("instagram.com", "instagram"),
    ("tiktok.com", "tiktok"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("threads.net", "threads"),
    ("threads.com", "threads"),
    ("twitter.com", "x"),
    ("x.com", "x"),
)

_URL_RE = re.compile(r"https?://\S+", re.I)

# Path segments that belong to the site, not to a person. parse_handle in
# importer.py knows most of these; "t" is TikTok's shortlink prefix and is only
# ever seen here.
_NOT_A_HANDLE = {
    "p", "reel", "reels", "tv", "watch", "shorts", "channel", "c", "user", "t",
    "embed", "post", "status", "video", "photo", "tag", "discover", "music",
    "explore", "stories", "about", "search", "hashtag", "i", "live",
}

# Platforms whose profile URLs always carry the @. On TikTok and YouTube a bare
# first path segment is a site path or a video id, never somebody's handle;
# on Instagram, Threads and X it usually is their handle.
_AT_REQUIRED = {"tiktok", "youtube"}

_FIRST_SEGMENT = re.compile(
    r"(?:instagram\.com|tiktok\.com|youtube\.com|youtu\.be|threads\.(?:net|com)"
    r"|x\.com|twitter\.com)/+(@?[^/?#\s]+)", re.I
)
# TikTok puts the post id in the path, as /video/<id> or /photo/<id>.
_TIKTOK_ID = re.compile(r"tiktok\.com/+@[^/]+/+(?:video|photo)/+(\d{15,21})", re.I)


def platform_for(url):
    """Which platform a URL belongs to, or None if we don't recognize the host."""
    text = (url or "").lower()
    for fragment, platform in _HOSTS:
        if fragment in text:
            return platform
    return None


def handle_in(url):
    """The handle inside a profile or post URL, lowercased without the @.

    Instagram and X write a profile as /thecassianvale with no @, TikTok and
    YouTube as /@bealune. Both spellings turn up in the profile column, so both
    have to be read — and a site path like /p/DQWp or /t/ZP8f has to be refused
    rather than returned as somebody's name.
    """
    platform = platform_for(url)
    if platform is None:
        return None
    match = _FIRST_SEGMENT.search(url or "")
    if not match:
        return None

    segment = match.group(1)
    at_prefixed = segment.startswith("@")
    if platform in _AT_REQUIRED and not at_prefixed:
        return None

    handle = segment.lstrip("@").strip(".").lower()
    if not handle or handle in _NOT_A_HANDLE:
        return None
    if not re.fullmatch(r"[a-z0-9._-]+", handle):
        return None
    return handle


def tiktok_posted_on(url):
    """Read a TikTok post's date out of its id.

    The id is a 64-bit value whose top 32 bits are the Unix second it was
    created. That makes the date a fact in the URL rather than something to
    leave blank, and it's the only post date this export contains.
    """
    match = _TIKTOK_ID.search(url or "")
    if not match:
        return None
    seconds = int(match.group(1)) >> 32
    # Sanity-bound it. A plausible id gives a plausible date; a 15-digit id
    # that isn't one gives 1970 or the far future, and that is not a date.
    if not 1_400_000_000 < seconds < 2_500_000_000:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).date()


# Two links pasted end to end with nothing between them: ".../ZTUXgWL9Q/https://...".
# Only split where the second scheme follows a path separator, so a URL that
# legitimately carries another one in its query string ("?url=https://...")
# stays in one piece.
_RUN_TOGETHER = re.compile(r"(?<=/)(?=https?://)", re.I)


def split_urls(cell):
    """Every URL in a cell.

    They arrive newline-separated, comma-separated, and — in at least one row
    of a real sheet — with no separator at all.
    """
    out, seen = [], set()
    for match in _URL_RE.findall(clean(cell).replace(",", " ")):
        for part in _RUN_TOGETHER.split(match):
            url = part.rstrip(").,;'\"")
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


# --- what stage of the campaign a row is at -----------------------------------

# An outreach sheet records two things per creator: what they said (Response)
# and how far the process got (Status). Neither alone is the whole picture —
# "Scheduled Call" says nothing about whether they posted, and "Not Yet
# Briefed" says nothing about whether they ever replied — so both are read,
# with Status winning wherever it is definite.
STATUS_COLUMN = {
    "posted": "content_delivered",
    "briefed": "briefed",
    "contacted": "contacted",
    "not contacted": "not_contacted",
    "declined": "declined",
    "opted in": "opted_in",
    "expressed interest": "opted_in",
}

# Statuses a post link is allowed to override. "Opted in" is the weakest
# positive a sheet can record, and some sheets have no stronger value in their
# vocabulary at all — the announcement outreach tops out at "Opted In", so its
# 42 creators with content links would otherwise all read as not delivered.
# Anything more specific than this stands, and a contradiction is reported.
PROMOTABLE = ("opted_in",)

RESPONSE_COLUMN = {
    "no reply": "contacted",
    "no response": "contacted",
    "never replied": "contacted",
    "declined": "declined",
    "not interested": "declined",
    "volunteered for wave 2": "wave_2",
    "bumped to wave 2": "wave_2",
    "pushed to wave 2": "wave_2",
    "wave 2": "wave_2",
    "opted in": "opted_in",
    "extra enthusiastic": "opted_in",
    "scheduled call": "opted_in",
    "asked for more time": "opted_in",
    "follow up needed": "opted_in",
}


def derive_status(status_cell, response_cell, has_content):
    """Return (status, from_the_sheet).

    from_the_sheet is False when nothing in either column mapped and the status
    had to be inferred from whether there are post links — which is how the
    first export, with no status column worth the name, has to be read.
    """
    for table, cell in ((STATUS_COLUMN, status_cell), (RESPONSE_COLUMN, response_cell)):
        hit = table.get(normalize_header(clean(cell)))
        if hit:
            return _promote(hit, has_content), True

    hit = parse_status(status_cell)
    if hit in PARTICIPATION_STATUSES:
        return _promote(hit, has_content), True

    return ("content_delivered" if has_content else "opted_in"), False


def _promote(status, has_content):
    if has_content and status in PROMOTABLE:
        return "content_delivered"
    return status


def parse_referrals(value):
    """"63" -> 63, "1,204" -> 1204, blank or junk -> None."""
    text = clean(value).replace(",", "")
    match = re.match(r"^-?\d+$", text)
    return int(text) if match else None


# --- matching a row to a creator ----------------------------------------------

@dataclass
class Signal:
    """One piece of evidence that a row is a particular creator."""

    creator_id: int
    kind: str       # profile | post | name
    strong: bool
    detail: str


_WORD = re.compile(r"[a-z0-9]+")


def name_tokens(name):
    return [w for w in _WORD.findall((name or "").lower()) if len(w) > 2]


class CreatorIndex:
    """Every loaded creator, searchable by handle and by name."""

    def __init__(self, creators, extra_emails=()):
        self.by_handle = {}        # (platform, handle) -> {id}
        self.by_handle_any = {}    # handle -> {id}
        self.by_name = {}          # casefolded full name -> {id}
        self.by_email = {}         # lowercased address -> id (unique holders only)
        self.shared_emails = {}    # address -> {ids}, when more than one holds it
        self.names = {}            # id -> name
        self.primaries = {}        # id -> primary address
        self.tokens = {}           # id -> set of name tokens

        for creator in creators:
            self.names[creator.id] = creator.name
            self.tokens[creator.id] = set(name_tokens(creator.name))
            self.by_name.setdefault(creator.name.strip().casefold(), set()).add(creator.id)
            if creator.email:
                self.by_email[creator.email.strip().lower()] = creator.id
                self.primaries[creator.id] = creator.email.strip().lower()
            for handle in creator.handles:
                key = (handle.handle or "").lstrip("@").casefold()
                if not key:
                    continue
                self.by_handle.setdefault((handle.platform, key), set()).add(creator.id)
                self.by_handle_any.setdefault(key, set()).add(creator.id)

        # Secondary addresses count just as much: one person mails from two
        # accounts, and a later sheet using the second one must find them
        # rather than look like somebody new.
        holders = {}
        for address, creator_id in extra_emails:
            holders.setdefault(address.strip().lower(), set()).add(creator_id)
        for address, creator_id in self.by_email.items():
            holders.setdefault(address, set()).add(creator_id)

        # An address two people share is not a key. A duo running one channel
        # really do share a mailbox, and picking whichever of them the query
        # returned first would attribute one person's work to the other.
        for address, ids in holders.items():
            if len(ids) == 1:
                self.by_email[address] = next(iter(ids))
            else:
                self.by_email.pop(address, None)
                self.shared_emails[address] = ids

    def _handle_signals(self, kind, platform, handle):
        """A handle match is strong on its own platform, weak on another's.

        "@thecassianvale" being somebody's Instagram name does not make a
        tiktok.com/@thecassianvale link theirs.
        """
        if not handle:
            return []
        exact = self.by_handle.get((platform, handle), set())
        if exact:
            return [
                Signal(cid, kind, True, f"{kind} handle @{handle} on {platform}")
                for cid in exact
            ]
        anywhere = self.by_handle_any.get(handle, set())
        return [
            Signal(cid, kind, False,
                   f"{kind} handle @{handle} is their handle on another platform")
            for cid in anywhere
        ]

    def _name_signals(self, name):
        text = clean(name)
        if not text:
            return []
        exact = self.by_name.get(text.casefold(), set())
        if exact:
            return [Signal(cid, "name", True, f"name matches {self.names[cid]!r}")
                    for cid in exact]

        # A partial name is corroboration, never proof. A first name can be
        # unique in the list today and still be just a first name.
        wanted = set(name_tokens(text))
        if not wanted:
            return []
        out = []
        for cid, tokens in self.tokens.items():
            if wanted & tokens:
                shared = ", ".join(sorted(wanted & tokens))
                out.append(Signal(cid, "name", False,
                                  f"name partly matches {self.names[cid]!r} ({shared})"))
        return out

    def primary_of(self, creator_id):
        return self.primaries.get(creator_id, "no address on file")

    def _email_signals(self, email):
        """An address is the strongest signal there is — while only one person
        holds it. A shared mailbox is a weak signal for each of its holders,
        so the name or handle on the row has to settle which of them it is."""
        if not email:
            return []
        address = email.strip().lower()
        cid = self.by_email.get(address)
        if cid is not None:
            return [Signal(cid, "email", True, f"email {address}")]
        shared = self.shared_emails.get(address)
        if shared:
            return [
                Signal(other, "email", False,
                       f"{address} is shared by {len(shared)} creators")
                for other in shared
            ]
        return []

    def match(self, name, profile_platform, profile_handle, post_handles, email=None):
        """Return (creator_id, note, warnings). creator_id is None if unresolved.

        One uncontradicted strong signal resolves a row. Weak signals never do.
        """
        signals = []
        signals += self._email_signals(email)
        signals += self._handle_signals("profile", profile_platform, profile_handle)
        for platform, handle in post_handles:
            signals += self._handle_signals("post", platform, handle)
        signals += self._name_signals(name)

        strong = {s.creator_id for s in signals if s.strong}

        # A unique address beats a disagreeing name outright: two people can
        # share a name, and a sheet can spell one wrong, but Creator.email is
        # unique by construction. The disagreement is still reported.
        by_email = {s.creator_id for s in signals if s.kind == "email"}
        if len(by_email) == 1 and len(strong) > 1:
            cid = next(iter(by_email))
            others = ", ".join(sorted(self.names[c] for c in strong - by_email))
            reasons = sorted({s.detail for s in signals if s.creator_id == cid})
            return cid, "; ".join(reasons), [
                f"matched on email; the name also matched {others}"
            ]

        if len(strong) > 1:
            who = ", ".join(sorted(self.names[c] for c in strong))
            return None, "", [f"conflicting matches: {who}"]

        if len(strong) == 1:
            cid = next(iter(strong))
            reasons = sorted({s.detail for s in signals if s.creator_id == cid})
            warnings = []
            # A weak signal pointing somewhere else is worth saying out loud
            # even when a strong one has already decided the row.
            others = {s.creator_id for s in signals if not s.strong} - {cid}
            if others:
                who = ", ".join(sorted(self.names[c] for c in others))
                warnings.append(f"weaker evidence also pointed at {who}")
            return cid, "; ".join(reasons), warnings

        if not signals:
            return None, "", ["no creator matches this name, handle or post link"]

        candidates = sorted({self.names[s.creator_id] for s in signals})
        detail = "; ".join(sorted({s.detail for s in signals}))
        return None, "", [
            f"only weak evidence, for {', '.join(candidates)} ({detail})"
        ]


# --- the plan -----------------------------------------------------------------

@dataclass
class PostPlan:
    url: str
    platform: str = None
    handle: str = None
    posted_on: date = None
    action: str = "create"      # create | exists | skipped
    reason: str = ""
    piece_id: int = None        # set when action == "exists"

    @property
    def usable(self):
        return self.action == "create"


@dataclass
class RowPlan:
    line: int
    name: str = ""
    profile_url: str = ""
    profile_platform: str = None
    profile_handle: str = None
    creator_id: int = None
    creator_name: str = ""
    match_note: str = ""
    action: str = "link"        # link | update | unchanged | review
    reason: str = ""
    status: str = "opted_in"
    existing_status: str = None
    email: str = ""
    # An address on this row that we don't have for this creator yet. Recorded
    # against their existing profile rather than treated as a new person —
    # matching on the primary address alone is how a duplicate got created.
    new_email: str = ""
    posts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)
    referrer: str = ""

    # Referral count from the sheet, and where it will be recorded.
    referrals: int = None
    referral_piece_id: int = None   # an existing piece, when there is one
    referral_existing: int = None   # what that piece already holds

    @property
    def new_posts(self):
        return [p for p in self.posts if p.usable]

    @property
    def groups(self):
        """New posts, grouped into the pieces they will become.

        One creative posted to TikTok and Instagram is one piece with two
        platform rows — that is what the model is for, and summing its
        engagement twice would be wrong. But the grouping is only safe when
        each platform appears once: four Instagram reels on a row are four
        videos, and two TikToks beside two Reels give no way to tell which
        pairs with which. So links are merged only when there is exactly one
        per platform and more than one platform.
        """
        usable = self.new_posts
        if not usable:
            return []
        by_platform = {}
        for post in usable:
            by_platform.setdefault(post.platform, []).append(post)
        if len(by_platform) > 1 and all(len(v) == 1 for v in by_platform.values()):
            return [usable]
        return [[post] for post in usable]

    @property
    def records_referrals(self):
        """A referral count that has both changed and somewhere to go.

        A row with referrals and no content has nowhere to put them. Treating
        that as pending work would make the row read as "to update" on every
        future run of a file that is already fully imported.
        """
        return bool(self.referral_change) and bool(self.groups or self.referral_piece_id)

    @property
    def referral_change(self):
        """(old, new) when the sheet's referral count differs from what's stored."""
        if self.referrals is None:
            return None
        if self.referral_existing == self.referrals:
            return None
        return (self.referral_existing, self.referrals)

    @property
    def status_change(self):
        if self.existing_status and self.existing_status != self.status:
            return (self.existing_status, self.status)
        return None


@dataclass
class ImportPlan:
    rows: list = field(default_factory=list)
    mapped: dict = field(default_factory=dict)
    unmapped: list = field(default_factory=list)
    duplicate_headers: list = field(default_factory=list)
    campaign: object = None
    campaign_action: str = "existing"     # existing | create
    campaign_slug: str = ""
    campaign_name: str = ""
    error: str = ""

    @property
    def ok(self):
        return not self.error

    def of(self, action):
        return [r for r in self.rows if r.action == action]

    @property
    def writing(self):
        """The rows apply() will actually touch."""
        return [r for r in self.rows if r.action in ("link", "update")]

    @property
    def counts(self):
        out = {k: 0 for k in ("link", "update", "unchanged", "review")}
        for row in self.rows:
            out[row.action] = out.get(row.action, 0) + 1
        # Only rows that get written, so the dry run's number is the number
        # apply() reports back. A row held for review carries post links that
        # are deliberately not imported.
        out["pieces"] = sum(len(r.groups) for r in self.writing)
        out["posts"] = sum(len(r.new_posts) for r in self.writing)
        # Only the ones that have somewhere to land. A row with referrals and
        # no content has nowhere to put them, and counting it here would make
        # the dry run promise a number apply() can't deliver.
        out["referrals"] = sum(1 for r in self.writing if r.records_referrals)
        out["emails"] = sum(1 for r in self.writing if r.new_email)
        return out


def _slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")


def read_extras(text, unmapped):
    """line number -> {header: value} for the columns we don't import.

    read_rows drops unrecognized cells; this picks them back up so the report
    can say what is in them. The export's device-fulfilment columns are real
    data with nowhere to go in the model yet, and a column that quietly
    evaporates is how you find out six months later that it mattered.
    """
    if not unmapped:
        return {}
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return {}

    wanted = {h.strip() for h in unmapped}
    columns = [
        (i, raw.strip()) for i, raw in enumerate(header) if raw.strip() in wanted
    ]
    out = {}
    for line, record in enumerate(reader, start=2):
        if not any(clean(c) for c in record):
            continue
        values = {
            name: clean(record[i])
            for i, name in columns
            if i < len(record) and clean(record[i])
        }
        if values:
            out[line] = values
    return out


def plan(text, campaign_slug=None, campaign_name=None):
    """Work out what importing `text` into one campaign would do."""
    result = ImportPlan()
    rows, result.mapped, result.unmapped, result.duplicate_headers = read_rows(
        text, header_map=HEADER_MAP
    )
    extras = read_extras(text, result.unmapped)

    if "name" not in result.mapped and "profile" not in result.mapped:
        result.error = (
            "no creator name or profile column found — add the header spelling "
            "to ALIASES in content_importer.py"
        )
        return result

    # --- the campaign ---------------------------------------------------------
    if not (campaign_slug or campaign_name):
        result.error = "no campaign given: pass --campaign SLUG (and --campaign-name for a new one)"
        return result

    slug = campaign_slug or _slugify(campaign_name)
    result.campaign_slug = slug
    existing = Campaign.query.filter_by(slug=slug).first()
    if existing:
        result.campaign = existing
        result.campaign_action = "existing"
        result.campaign_name = existing.name
    elif campaign_name:
        result.campaign_action = "create"
        result.campaign_name = campaign_name
    else:
        result.error = (
            f"no campaign with slug {slug!r}. Pass --campaign-name to create it."
        )
        return result

    index = CreatorIndex(
        Creator.query.all(),
        extra_emails=[
            (e.address, e.creator_id) for e in CreatorEmail.query.all()
        ],
    )
    # Addresses already on file, so a row only records one that is genuinely
    # new. Both tables, in case the creator_emails backfill hasn't been run.
    known_addresses = {e.address.strip().lower() for e in CreatorEmail.query.all()}
    known_addresses |= {
        c.email.strip().lower() for c in Creator.query.all() if c.email
    }
    campaign_id = result.campaign.id if result.campaign else None

    # Existing state, so a re-run is a no-op rather than a duplicate.
    existing_parts = {}
    existing_urls = {}
    existing_pieces = {}
    if campaign_id:
        for part in Participation.query.filter_by(campaign_id=campaign_id):
            existing_parts[part.creator_id] = part
        for post in (
            PlatformPost.query.join(UgcPiece)
            .filter(UgcPiece.campaign_id == campaign_id)
            .all()
        ):
            if post.url:
                existing_urls[post.url.strip()] = post
        for piece in UgcPiece.query.filter_by(campaign_id=campaign_id).all():
            existing_pieces[piece.id] = piece

    seen_creators = {}
    for line, values in rows:
        row = RowPlan(line=line)
        row.name = clean(values.get("name"))
        row.referrer = clean(values.get("referrer"))
        addresses = parse_emails(values.get("email"))
        row.email = addresses[0] if addresses else ""
        if len(addresses) > 1:
            row.warnings.append(
                f"more than one address in the cell; used {row.email}"
            )
        row.referrals = parse_referrals(values.get("referrals"))
        row.extras = extras.get(line, {})
        # --- profile ----------------------------------------------------------
        row.profile_url = clean(values.get("profile"))
        profile_urls = split_urls(row.profile_url)
        if profile_urls:
            row.profile_platform = platform_for(profile_urls[0])
            row.profile_handle = handle_in(profile_urls[0])
        elif row.profile_url.startswith("@"):
            row.profile_handle = row.profile_url[1:].lower()

        if not (row.name or row.profile_handle or row.email):
            row.action = "review"
            row.reason = "no creator name, profile link or email"
            result.rows.append(row)
            continue

        # --- posts ------------------------------------------------------------
        for url in split_urls(values.get("posts")):
            post = PostPlan(url=url)
            post.platform = platform_for(url)
            post.handle = handle_in(url)
            post.posted_on = tiktok_posted_on(url) or parse_date(values.get("posted_on"))

            if post.platform is None:
                post.action = "skipped"
                post.reason = "unrecognized host"
            elif post.platform not in PLATFORMS:
                post.action = "skipped"
                post.reason = f"{post.platform} isn't a measured platform"
            elif url.strip() in existing_urls:
                post.action = "exists"
                post.piece_id = existing_urls[url.strip()].ugc_piece_id
            row.posts.append(post)

        # --- which creator ----------------------------------------------------
        post_handles = [
            (p.platform, p.handle) for p in row.posts if p.platform and p.handle
        ]
        row.creator_id, row.match_note, warnings = index.match(
            row.name, row.profile_platform, row.profile_handle, post_handles,
            email=row.email,
        )
        row.warnings.extend(warnings)

        if row.creator_id is None:
            row.action = "review"
            row.reason = warnings[0] if warnings else "could not be matched to a creator"
            result.rows.append(row)
            continue

        row.creator_name = index.names[row.creator_id]

        if row.creator_id in seen_creators:
            row.action = "review"
            row.reason = (
                f"{row.creator_name} is also on line {seen_creators[row.creator_id]}"
            )
            result.rows.append(row)
            continue
        seen_creators[row.creator_id] = line

        # A profile link and a post link that name different handles means one
        # of the two columns is stale. The row still imports; it gets said.
        mismatched = {
            p.handle for p in row.posts
            if p.handle and row.profile_handle and p.handle != row.profile_handle
        }
        if mismatched:
            row.warnings.append(
                f"profile says @{row.profile_handle}, post is from "
                f"@{', @'.join(sorted(mismatched))}"
            )

        for post in row.posts:
            if post.usable and not post.posted_on:
                row.warnings.append(f"no date available for {post.url}")

        # --- status -----------------------------------------------------------
        has_content = any(p.action in ("create", "exists") for p in row.posts)
        row.status, from_sheet = derive_status(
            values.get("status"), values.get("response"), has_content
        )

        # The sheet says one thing and the links say another. Both are kept —
        # the status because somebody wrote it down, the content because it
        # exists — and the contradiction is reported rather than resolved by
        # quietly preferring one column.
        if from_sheet and has_content and row.status != "content_delivered":
            row.warnings.append(
                f"has {len(row.posts)} post link(s) but the sheet says "
                f"{row.status.replace('_', ' ')}"
            )
        if from_sheet and not has_content and row.status == "content_delivered":
            row.warnings.append("marked as posted, but no post link")

        # --- referrals ----------------------------------------------------------
        if row.referrals is not None:
            landing = _referral_target(row, existing_pieces)
            row.referral_piece_id = landing
            if landing is not None:
                row.referral_existing = _snapshot_value(landing)
            if not row.posts:
                row.warnings.append(
                    f"{row.referrals} referrals, but no content to attach them to"
                )
            elif len(row.groups) > 1:
                row.warnings.append(
                    f"{row.referrals} referrals is a row total; the sheet counts "
                    f"per creator, so it sits on the earliest of "
                    f"{len(row.groups)} pieces"
                )

        # --- an address we don't have for them yet ------------------------------
        if row.email and row.email.lower() not in known_addresses:
            row.new_email = row.email.lower()
            known_addresses.add(row.new_email)
            row.warnings.append(
                f"new address for {row.creator_name}: {row.new_email} "
                f"(their profile has {index.primary_of(row.creator_id)})"
            )

        part = existing_parts.get(row.creator_id)
        if part is not None:
            row.existing_status = part.status
            if (row.status_change or row.new_posts or row.records_referrals
                    or row.new_email):
                row.action = "update"
            else:
                row.action = "unchanged"
        else:
            row.action = "link"

        result.rows.append(row)

    return result


def _referral_target(row, existing_pieces):
    """The piece a row's referral count belongs on, when it already exists.

    None means "the first piece this row creates" — apply() fills that in.
    """
    known = [p.piece_id for p in row.posts if p.action == "exists" and p.piece_id]
    if row.new_posts or not known:
        return None
    pieces = [existing_pieces[i] for i in known if i in existing_pieces]
    if not pieces:
        return None
    pieces.sort(key=lambda p: (p.posted_on or date.max, p.id))
    return pieces[0].id


def _snapshot_value(piece_id):
    snapshot = ReferralSnapshot.query.filter_by(
        ugc_piece_id=piece_id, window_days=REPORTED_WINDOW
    ).first()
    return snapshot.referrals if snapshot else None


# --- applying it --------------------------------------------------------------

def apply(plan_result):
    """Commit a plan. One transaction: all of the file, or none of it."""
    if not plan_result.ok:
        raise ValueError(plan_result.error)

    summary = {
        "campaign_created": 0, "linked": 0, "status_changed": 0,
        "pieces": 0, "posts": 0, "referrals": 0, "emails": 0, "reviewed": 0,
    }

    campaign = plan_result.campaign
    if campaign is None:
        campaign = Campaign(
            name=plan_result.campaign_name,
            slug=plan_result.campaign_slug,
            status="complete",
        )
        db.session.add(campaign)
        db.session.flush()
        summary["campaign_created"] = 1
        plan_result.campaign = campaign

    for row in plan_result.rows:
        if row.action in ("review", "unchanged"):
            summary["reviewed"] += row.action == "review"
            continue

        part = Participation.query.filter_by(
            creator_id=row.creator_id, campaign_id=campaign.id
        ).first()
        if part is None:
            part = Participation(
                creator_id=row.creator_id, campaign_id=campaign.id, status=row.status
            )
            db.session.add(part)
            summary["linked"] += 1
        elif part.status != row.status:
            part.status = row.status
            summary["status_changed"] += 1

        if row.new_email:
            creator = db.session.get(Creator, row.creator_id)
            if record_email(creator, row.new_email,
                            source=plan_result.campaign_slug) is not None:
                summary["emails"] += 1

        created = []
        for group in row.groups:
            # A group is one creative. Usually one link; several when the same
            # video went to more than one platform and the grouping was
            # unambiguous (see RowPlan.groups).
            dates = [p.posted_on for p in group if p.posted_on]
            piece = UgcPiece(
                creator_id=row.creator_id,
                campaign_id=campaign.id,
                posted_on=min(dates) if dates else None,
                discovered_via="crm_export",
            )
            db.session.add(piece)
            db.session.flush()
            for post in group:
                db.session.add(PlatformPost(
                    ugc_piece_id=piece.id,
                    platform=post.platform,
                    url=post.url,
                    posted_on=post.posted_on,
                ))
                summary["posts"] += 1
            created.append(piece)
            summary["pieces"] += 1

        # --- the referral count -------------------------------------------
        # The sheet gives one number per creator, not per video. It lands on
        # the row's earliest piece so the creator and campaign totals are
        # exact; the plan warns when a row had more than one piece and the
        # per-piece split is therefore unknown.
        if row.referrals is not None:
            target = None
            if created:
                created.sort(key=lambda p: (p.posted_on or date.max, p.id))
                target = created[0].id
            elif row.referral_piece_id:
                target = row.referral_piece_id
            if target is not None and row.referral_change:
                snapshot = ReferralSnapshot.query.filter_by(
                    ugc_piece_id=target, window_days=REPORTED_WINDOW
                ).first()
                if snapshot is None:
                    snapshot = ReferralSnapshot(
                        ugc_piece_id=target, window_days=REPORTED_WINDOW
                    )
                    db.session.add(snapshot)
                snapshot.referrals = row.referrals
                snapshot.measured_at = date.today()
                summary["referrals"] += 1

    db.session.commit()
    return summary


# --- reporting ----------------------------------------------------------------

def render(plan_result, verbose=False):
    """A readable report of a plan. This is the thing you act on."""
    out = []
    add = out.append

    if not plan_result.ok:
        return f"CANNOT IMPORT: {plan_result.error}"

    counts = plan_result.counts
    add(f"Columns recognized: {len(plan_result.mapped)}")
    for our_field, header in sorted(plan_result.mapped.items()):
        add(f"    {header!r} -> {our_field}")
    if plan_result.unmapped:
        add("")
        add("Columns NOT imported (nothing in the model holds these):")
        for header in plan_result.unmapped:
            filled = sum(1 for r in plan_result.rows if r.extras.get(header))
            add(f"    {header!r} — {filled} row(s) have a value")
    if plan_result.duplicate_headers:
        add("")
        add("Duplicate columns (second one ignored):")
        for header in plan_result.duplicate_headers:
            add(f"    {header}")

    add("")
    if plan_result.campaign_action == "create":
        add(f"Campaign to CREATE: {plan_result.campaign_name!r} "
            f"(slug {plan_result.campaign_slug})")
    else:
        add(f"Campaign: {plan_result.campaign.name} "
            f"(slug {plan_result.campaign.slug})")

    add(f"{counts['link']} creators to join, {counts['update']} to update, "
        f"{counts['unchanged']} unchanged, {counts['review']} need review")
    add(f"{counts['pieces']} content pieces to create "
        f"from {counts['posts']} post link(s)")

    dated = sum(1 for r in plan_result.writing for g in r.groups
                if any(p.posted_on for p in g))
    if counts["pieces"]:
        add(f"    {dated} with a post date read from the URL, "
            f"{counts['pieces'] - dated} without one")
    if counts["referrals"]:
        add(f"{counts['referrals']} referral counts to record")
    if counts["emails"]:
        add(f"{counts['emails']} new address(es) to record against existing creators")

    # Where everybody stands. This is the point of an outreach sheet: the
    # people who never replied and the people who were never briefed are
    # different problems, and a single "didn't deliver" number hides both.
    stages = {}
    for row in plan_result.rows:
        if row.action != "review":
            stages[row.status] = stages.get(row.status, 0) + 1
    if len(stages) > 1:
        add("")
        add("Where they stand:")
        for status in PARTICIPATION_STATUSES:
            if status in stages:
                label = status.replace("_", " ")
                add(f"    {stages[status]:>4}  {label}")

    review = plan_result.of("review")
    if review:
        add("")
        add(f"NEEDS REVIEW ({len(review)}) — skipped, nothing written:")
        for row in review:
            who = row.name or f"@{row.profile_handle}" or "(empty)"
            add(f"    line {row.line}: {who} — {row.reason}")
            if row.match_note:
                add(f"        matched on: {row.match_note}")
            # A held row is the one you most need to know about, so its
            # warnings belong here rather than in the section below that
            # deliberately skips held rows. The near-miss on a row whose
            # address matched somebody else is usually the whole story: it
            # means the sheet has the wrong address on that line.
            for warning in row.warnings:
                add(f"        note:       {warning}")
            if row.email:
                add(f"        email:      {row.email}")
            if row.profile_url:
                add(f"        profile:    {row.profile_url}")
            for post in row.posts:
                add(f"        post:       {post.url}")

    warned = [r for r in plan_result.rows if r.warnings and r.action != "review"]
    if warned:
        add("")
        add(f"WARNINGS ({len(warned)} rows) — imported, but check these:")
        for row in warned:
            for warning in row.warnings:
                add(f"    line {row.line}: {row.creator_name} — {warning}")

    skipped = [(r, p) for r in plan_result.rows for p in r.posts
               if p.action == "skipped"]
    if skipped:
        add("")
        add(f"POST LINKS SKIPPED ({len(skipped)}):")
        for row, post in skipped:
            add(f"    line {row.line}: {post.reason} — {post.url}")

    updates = plan_result.of("update")
    if updates:
        add("")
        add(f"UPDATES ({len(updates)}):")
        for row in updates:
            bits = []
            if row.status_change:
                bits.append("status {0} -> {1}".format(*row.status_change))
            if row.new_posts:
                bits.append(f"{len(row.new_posts)} new piece(s)")
            add(f"    line {row.line}: {row.creator_name} — {', '.join(bits)}")

    if verbose:
        carrying = [r for r in plan_result.rows if r.extras]
        if carrying:
            add("")
            add(f"VALUES IN NOT-IMPORTED COLUMNS ({len(carrying)} rows):")
            for row in carrying:
                pairs = ", ".join(f"{k}={v!r}" for k, v in row.extras.items())
                add(f"    line {row.line}: {row.name or row.creator_name} — {pairs}")

        links = plan_result.of("link") + plan_result.of("update")
        if links:
            add("")
            add(f"MATCHED ({len(links)}):")
            for row in links:
                extra = f" · {row.referrals} referrals" if row.referrals is not None else ""
                add(f"    line {row.line}: {row.name!r} -> {row.creator_name} "
                    f"[{row.status}]{extra}")
                add(f"        matched on: {row.match_note}")
                for index, group in enumerate(row.groups, start=1):
                    if len(row.groups) > 1:
                        add(f"        piece {index}:")
                    for post in group:
                        when = post.posted_on.isoformat() if post.posted_on else "no date"
                        add(f"        {post.platform or '?':<9} {when:<10} "
                            f"{post.action:<8} {post.url}")
                for post in row.posts:
                    if not post.usable:
                        add(f"        {post.platform or '?':<9} {'-':<10} "
                            f"{post.action:<8} {post.url}")

    return "\n".join(out)


# --- CLI ----------------------------------------------------------------------

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Import one campaign's participants and content from a CSV. "
                    "Dry run unless --apply.",
    )
    parser.add_argument("csv_path")
    parser.add_argument("--campaign", metavar="SLUG",
                        help="campaign slug to import into")
    parser.add_argument("--campaign-name", metavar="NAME",
                        help="create the campaign under this name if it doesn't exist")
    parser.add_argument("--apply", action="store_true",
                        help="actually write to the database")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="list every matched row and post link")
    args = parser.parse_args(argv)

    with open(args.csv_path, encoding="utf-8-sig") as handle:
        text = handle.read()

    from app import app

    with app.app_context():
        result = plan(text, campaign_slug=args.campaign,
                      campaign_name=args.campaign_name)
        print(render(result, verbose=args.verbose))

        if not result.ok:
            return 1
        if not args.apply:
            print("\n(dry run — nothing written. Re-run with --apply to commit.)")
            return 0

        summary = apply(result)
        add = (" campaign created," if summary["campaign_created"] else "")
        print(
            f"\nDone:{add} {summary['linked']} creators joined, "
            f"{summary['status_changed']} statuses changed, "
            f"{summary['pieces']} pieces with {summary['posts']} posts, "
            f"{summary['referrals']} referral counts recorded, "
            f"{summary['emails']} addresses added, "
            f"{summary['reviewed']} rows left for review."
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
