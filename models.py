"""Data model for creatorwise.

Shape of the domain:

    Creator ──< Participation >── Campaign
       │                              │
       └──────< UgcPiece >────────────┘
                   │
                   ├──< PlatformPost   (same video, per platform)
                   └──< ReferralSnapshot (referrals measured N days after post)

A UgcPiece is one *creative* — one video. The same video often gets posted to
Instagram, TikTok and YouTube, so engagement lives on PlatformPost and is summed
up to the piece. Referrals are snapshotted rather than stored as a single number
because the campaign metric is "referrals driven 14 days after post date".
"""

import unicodedata
from datetime import date, timedelta

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Platforms we track performance on. Campaign and UGC metrics only ever roll up
# these three.
PLATFORMS = ("instagram", "tiktok", "youtube")

# Platforms a creator may have a handle on. Threads and X are here but not in
# PLATFORMS: 21 creators in the real list have one, so dropping them would lose
# real contact information, but no content is measured there.
PROFILE_PLATFORMS = PLATFORMS + ("threads", "x")

PLATFORM_LABELS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "threads": "Threads",
    "x": "X",
}

# How a creator came to us. These are the real categories from the recruitment
# export, not invented ones: 215 referral, 57 outbound, 5 existing Readwise
# users, 2 alpha testers, 1 inbound. Plain strings, so a new source is a
# one-line change rather than a migration.
RECRUITMENT_SOURCES = (
    "referral",       # another creator referred them — by far the biggest channel
    "outbound",       # cold outreach, DM or email
    "inbound",        # they approached us
    "existing_user",  # already a Readwise customer or affiliate
    "alpha_tester",   # migrated from the Bookwise alpha
    "agency",         # came via a talent agency
)

RECRUITMENT_LABELS = {
    "referral": "Referral",
    "outbound": "Cold outreach",
    "inbound": "Inbound",
    "existing_user": "Existing Readwise user",
    "alpha_tester": "Alpha tester",
    "agency": "Agency",
}

# Where a creator stands in a given campaign, in funnel order. A campaign is
# worked as a pipeline — contacted, replied, briefed, posted — and the outreach
# sheet records every stage, not just the end of it. Keeping them apart is the
# difference between "113 people didn't deliver" and "97 of them were never
# briefed in the first place".
PARTICIPATION_STATUSES = (
    "not_contacted",      # on the list; nobody has reached out yet
    "contacted",          # reached out to, never replied
    "opted_in",           # said yes; nothing scheduled or sent yet
    "wave_2",             # volunteered for, or pushed to, a later wave
    "briefed",            # had the briefing; hasn't posted
    "content_delivered",  # posted
    "declined",           # said no
    "no_show",            # agreed, then went quiet
)
PARTICIPATION_LABELS = {
    "not_contacted": "Not contacted",
    "contacted": "Contacted",
    "opted_in": "Opted in",
    "wave_2": "Wave 2",
    "briefed": "Briefed",
    "content_delivered": "Delivered",
    "declined": "Declined",
    "no_show": "No show",
}

# Statuses that mean we are still expecting content from them.
PENDING_STATUSES = ("opted_in", "wave_2", "briefed")

# The follow-up list, most actionable first: somebody who has been briefed is
# one nudge from delivering, somebody who declined is not. This is deliberately
# not the funnel order — that one runs coldest-to-warmest, which is the wrong
# way round for a list you work through.
FOLLOW_UP_ORDER = (
    "briefed", "wave_2", "opted_in", "contacted", "not_contacted",
    "no_show", "declined",
)

# The window used for the campaign referral metric.
REFERRAL_WINDOW_DAYS = 14

# A snapshot window of 0 means "as reported" — a referral count copied from a
# spreadsheet with no measurement window attached to it. It is kept distinct
# from a real 14-day figure so the two are never summed as if they were the
# same measurement, and so the UI can say which it is showing.
REPORTED_WINDOW = 0

# How a piece of content reached us.
DISCOVERY_LABELS = {
    "manual": "Added by hand",
    "email": "Inbox scan",
    "crm_export": "CRM export",
}


class Creator(db.Model):
    __tablename__ = "creators"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False)
    # Who referred them in. 214 of 280 creators came through another creator, so
    # this is a real graph, not a note — a self-reference rather than free text.
    # When the referrer couldn't be resolved to a creator (a first name only, or
    # somebody outside the programme) the raw value is kept in
    # recruited_by_note instead of being guessed at.
    recruited_by_id = db.Column(db.Integer, db.ForeignKey("creators.id"))
    recruited_by_note = db.Column(db.String(254))

    # When they joined the programme, from the recruitment export. Distinct from
    # created_at, which is when this row was written.
    onboarded_on = db.Column(db.Date)

    # Everything that isn't a tracked platform: linktree, Bluesky, Letterboxd,
    # personal sites. Kept verbatim rather than parsed into columns nobody asked
    # for; newline-separated.
    links = db.Column(db.Text)

    recruitment_source = db.Column(db.String(32))
    recruitment_note = db.Column(db.Text)
    notes = db.Column(db.Text)

    # Payment details. Deliberately coarse for v1 — no card/bank numbers here,
    # just the handle needed to send money and the terms agreed.
    payment_method = db.Column(db.String(32))      # paypal | venmo | wise | gift_card
    payment_handle = db.Column(db.String(254))
    payment_note = db.Column(db.Text)

    created_at = db.Column(db.Date, default=date.today)

    emails = db.relationship(
        "CreatorEmail", back_populates="creator",
        cascade="all, delete-orphan", lazy="selectin",
    )
    handles = db.relationship(
        "SocialHandle", back_populates="creator",
        cascade="all, delete-orphan", lazy="selectin",
    )
    participations = db.relationship(
        "Participation", back_populates="creator",
        cascade="all, delete-orphan", lazy="selectin",
    )
    ugc = db.relationship(
        "UgcPiece", back_populates="creator",
        cascade="all, delete-orphan", lazy="selectin",
    )
    campaign_referrals = db.relationship(
        "CampaignReferral", back_populates="creator",
        cascade="all, delete-orphan", lazy="selectin",
    )
    collaborations = db.relationship(
        "PieceCollaborator", back_populates="creator",
        cascade="all, delete-orphan", lazy="selectin",
    )

    # The recruitment graph. `recruited_by` is the creator who referred this one;
    # `recruits` is everyone they brought in. remote_side points at the parent
    # end of the self-join, which is what tells SQLAlchemy which way it runs.
    # Named apart from `total_referrals` on purpose: that counts signups driven
    # by content, this counts people. Conflating the two would be easy and wrong.
    recruited_by = db.relationship(
        "Creator", remote_side=[id], back_populates="recruits", lazy="selectin",
    )
    recruits = db.relationship(
        "Creator", back_populates="recruited_by", lazy="selectin",
    )

    @property
    def initials(self):
        """Up to two letters to stand in where there is no profile photo."""
        words = [w for w in (self.name or "").replace("(", " ").split() if w[:1].isalnum()]
        if not words:
            return "?"
        if len(words) == 1:
            return words[0][:1].upper()
        return (words[0][:1] + words[1][:1]).upper()

    @property
    def recruit_count(self):
        return len(self.recruits)

    @property
    def recruited_by_label(self):
        """Who brought them in — a linked creator, raw text, or nothing."""
        if self.recruited_by:
            return self.recruited_by.name
        return self.recruited_by_note or None

    @property
    def source_label(self):
        return RECRUITMENT_LABELS.get(
            self.recruitment_source, self.recruitment_source or "—"
        )

    @property
    def shares_address(self):
        """True when somebody else is on one of this creator's addresses."""
        for row in self.emails:
            if len(row.creator.emails) and CreatorEmail.query.filter(
                CreatorEmail.address == row.address,
                CreatorEmail.creator_id != self.id,
            ).first():
                return True
        return False

    @property
    def search_text(self):
        """Everything this creator can be found by, lowercased and unaccented.

        Name, every address (not just the primary) and every handle, so the
        search box finds somebody by whichever of those you happen to remember.
        Accents are stripped so "zoe" finds "Zoë"; the page strips them
        from the query the same way.
        """
        parts = [self.name or ""] + self.all_emails
        for handle in self.handles:
            bare = (handle.handle or "").lstrip("@")
            parts += [bare, "@" + bare]
        text = unicodedata.normalize("NFKD", " ".join(parts))
        return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()

    @property
    def all_emails(self):
        """Every address for this creator, primary first."""
        extra = sorted(
            (e.address for e in self.emails
             if e.address.lower() != (self.email or "").lower()),
        )
        return ([self.email] if self.email else []) + extra

    @property
    def other_emails(self):
        """Addresses besides the primary — usually none."""
        return self.all_emails[1:]

    @property
    def link_list(self):
        return [l for l in (self.links or "").splitlines() if l.strip()]

    @property
    def total_referrals(self):
        """Prefers per-campaign windowed totals; falls back to per-piece.

        A creator can have referrals and no content at all, so this must not
        be a sum over their videos.
        """
        if self.campaign_referrals:
            return sum(r.referrals for r in self.campaign_referrals)
        return sum(p.referrals for p in self.ugc)

    @property
    def all_pieces(self):
        """Everything this creator made — as lead or alongside somebody else."""
        seen, out = set(), []
        for piece in list(self.ugc) + [c.piece for c in self.collaborations]:
            if piece.id not in seen:
                seen.add(piece.id)
                out.append(piece)
        return out

    @property
    def total_engagement(self):
        return sum(p.engagement for p in self.all_pieces)

    @property
    def follower_count(self):
        """Largest single-platform audience, not the sum — summing double-counts."""
        return max((h.follower_count or 0 for h in self.handles), default=0)

    def handles_on(self, platform):
        return [h for h in self.handles if h.platform == platform]

    def followers_on(self, platform):
        """Audience on one platform, or None when it hasn't been measured.

        None and zero are different answers: zero is a reading, None is the
        absence of one, and the creators page shows them differently.
        """
        counts = [
            h.follower_count for h in self.handles_on(platform)
            if h.follower_count is not None
        ]
        return max(counts) if counts else None

    @property
    def delivered_count(self):
        return sum(1 for p in self.participations if p.status == "content_delivered")

    def __repr__(self):
        return f"<Creator {self.name}>"


class CreatorEmail(db.Model):
    """Every address we know for a creator, including the primary one.

    `Creator.email` stays the primary and stays unique — it's what the rest of
    the app, the inbox matcher and both importers key on. This table is the
    rest: the second address somebody mails from, the personal one beside the
    business one, the address a later sheet uses for a creator already in the
    list under a different one.

    That last case is why this exists. Matching an import on the primary alone
    produced a duplicate creator for somebody already in the database, and
    "import the first address and warn about the second" quietly loses the
    information needed to avoid it next time.
    """

    __tablename__ = "creator_emails"
    __table_args__ = (db.UniqueConstraint("creator_id", "address"),)

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)
    # NOT unique. One address usually belongs to one person, and a sheet that
    # puts the same address on two names is usually an error — but not always:
    # a duo running one channel share a mailbox, and both are real creators
    # with their own names. Unique here forced them into one record.
    #
    # The cost is that a shared address stops being a unique key, so matching
    # must treat it as ambiguous rather than picking whoever comes first. See
    # CreatorIndex._email_signals.
    address = db.Column(db.String(254), nullable=False, index=True)
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    # Where it came from — which sheet, or "recruitment export". Free text.
    source = db.Column(db.String(120))
    created_at = db.Column(db.Date, default=date.today)

    creator = db.relationship("Creator", back_populates="emails")

    def __repr__(self):
        return f"<CreatorEmail {self.address}>"


class SocialHandle(db.Model):
    __tablename__ = "social_handles"

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)
    platform = db.Column(db.String(16), nullable=False)
    handle = db.Column(db.String(128), nullable=False)
    follower_count = db.Column(db.Integer)
    profile_url = db.Column(db.String(512))

    creator = db.relationship("Creator", back_populates="handles")
    snapshots = db.relationship(
        "FollowerSnapshot", back_populates="handle",
        cascade="all, delete-orphan", lazy="selectin",
    )

    @property
    def platform_label(self):
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def latest_snapshot(self):
        """The most recent check of this handle, whatever it found."""
        if not self.snapshots:
            return None
        return max(self.snapshots, key=lambda s: (s.measured_at or date.min, s.id or 0))

    @property
    def followers_checked_on(self):
        latest = self.latest_snapshot
        return latest.measured_at if latest else None

    @property
    def is_unresolved(self):
        """True when the last check could not find the account at all."""
        latest = self.latest_snapshot
        return latest is not None and latest.status == "not_found"


class FollowerSnapshot(db.Model):
    """One reading of a handle's audience, and where the number came from.

    `SocialHandle.follower_count` keeps the latest good value, because that is
    what every page wants. This keeps the readings behind it: when it was
    checked, by what, and what it said. Without that a follower count is just
    a number somebody typed once — there is no way to tell a figure confirmed
    last week from one copied out of a spreadsheet a year ago, and no way to
    see an audience grow.

    A check that finds no such account is recorded too (`status="not_found"`,
    no count), rather than leaving the handle looking merely unmeasured.
    """

    __tablename__ = "follower_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    social_handle_id = db.Column(
        db.Integer, db.ForeignKey("social_handles.id"), nullable=False
    )
    follower_count = db.Column(db.Integer)
    status = db.Column(db.String(16), nullable=False, default="ok")  # ok | not_found
    measured_at = db.Column(db.Date, default=date.today)
    source = db.Column(db.String(120))

    handle = db.relationship("SocialHandle", back_populates="snapshots")


class CreatorGroup:
    """One person's content in one campaign, with their referrals for it."""

    def __init__(self, creator, pieces, referrals):
        self.creator = creator
        self.pieces = pieces              # newest first
        self.referrals = referrals        # None = campaign not measured

    @property
    def latest(self):
        return self.pieces[0].posted_on if self.pieces else None

    @property
    def platforms(self):
        seen = []
        for piece in self.pieces:
            for post in piece.posts:
                if post.platform_label not in seen:
                    seen.append(post.platform_label)
        return seen


class Campaign(db.Model):
    __tablename__ = "campaigns"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.Text)
    starts_on = db.Column(db.Date)
    ends_on = db.Column(db.Date)
    status = db.Column(db.String(24), default="active")  # active | complete | planned

    # Free text for v1. Invoice generation will formalise this later.
    payment_terms = db.Column(db.Text)

    participations = db.relationship(
        "Participation", back_populates="campaign",
        cascade="all, delete-orphan", lazy="selectin",
    )
    ugc = db.relationship(
        "UgcPiece", back_populates="campaign",
        cascade="all, delete-orphan", lazy="selectin",
    )
    referral_totals = db.relationship(
        "CampaignReferral", back_populates="campaign",
        cascade="all, delete-orphan", lazy="selectin",
    )

    @property
    def creator_count(self):
        return len(self.participations)

    # --- the funnel, as cumulative counts ------------------------------------
    # These narrow rather than partition: everyone who delivered also opted in,
    # and everyone who opted in was also contacted. The per-status breakdown in
    # status_counts is the disjoint view; these three are the headline.

    @property
    def contacted_count(self):
        """Everyone we actually reached out to.

        Not simply everyone on the list: a campaign can carry people nobody
        has approached yet, and counting them as contacted overstates the
        reach and understates the opt-in rate.
        """
        return sum(
            1 for p in self.participations if p.status != "not_contacted"
        )

    @property
    def opted_in_count(self):
        """Everyone who said yes — contacted, minus no-replies and declines.

        `contacted` as a *status* means the opposite of this one: reached out
        to and never heard from again. As a *metric*, "contacted" is everybody
        we approached. Don't collapse the two.
        """
        return sum(
            1 for p in self.participations
            if p.status not in ("not_contacted", "contacted", "declined")
        )

    @property
    def delivered_count(self):
        return sum(1 for p in self.participations if p.status == "content_delivered")

    @property
    def total_referrals(self):
        """Prefers the per-creator windowed totals when they exist.

        Those are counted against a stated window from the source of truth;
        the per-piece snapshots are whatever a spreadsheet said. Never sum
        both — they measure the same thing twice.
        """
        if self.referral_totals:
            return sum(r.referrals for r in self.referral_totals)
        return sum(p.referrals for p in self.ugc)

    @property
    def referral_window(self):
        """The window the referral totals were counted over, if they agree."""
        windows = {
            (r.window_from, r.window_to) for r in self.referral_totals
            if r.window_from and r.window_to
        }
        return next(iter(windows)) if len(windows) == 1 else None

    @property
    def referral_window_label(self):
        window = self.referral_window
        if not window:
            return None
        start, end = window
        return f"{start:%b %-d} – {end:%b %-d, %Y}"

    @property
    def referrals_still_counting(self):
        """True when the totals were counted before the campaign ended.

        A running campaign's referral number is a figure to date, and has to
        say so — otherwise it reads as the campaign's final result. A campaign
        with no end date never finishes, so its figure is always to date.
        """
        window = self.referral_window
        return bool(window and (self.ends_on is None or window[1] < self.ends_on))

    @property
    def credited_count(self):
        """How many creators have a referral number for this campaign."""
        return sum(1 for r in self.referral_totals if r.referrals)

    def referrals_for(self, creator):
        """One creator's referrals for this campaign — their own, not a piece's.

        Zero when the campaign was measured and they earned none; None when it
        has no measurement at all, so a blank never reads as a zero. Without
        per-creator totals this falls back to snapshots on pieces they made.
        """
        if self.referral_totals:
            return next(
                (r.referrals for r in self.referral_totals
                 if r.creator_id == creator.id), 0)
        mine = [p for p in self.ugc if creator in p.creators and p.referral_snapshots]
        return sum(p.referrals for p in mine) if mine else None

    def pieces_by(self, creator):
        """What this creator made in this campaign, lead or collaborator, newest first."""
        mine = [p for p in self.ugc if creator in p.creators]
        return sorted(mine, key=lambda p: (p.posted_on or date.min, p.id), reverse=True)

    @property
    def creator_groups(self):
        """The campaign's content, one group per person who made some.

        A piece two people made sits in both groups — each of them made it —
        while campaign totals still count it once, from `ugc`.
        """
        makers = {}
        for piece in self.ugc:
            for person in piece.creators:
                makers.setdefault(person.id, person)
        return [
            CreatorGroup(person, self.pieces_by(person), self.referrals_for(person))
            for person in makers.values()
        ]

    @property
    def total_engagement(self):
        return sum(p.engagement for p in self.ugc)

    @property
    def status_counts(self):
        """Participants by status, in funnel order, skipping empty ones."""
        counts = {}
        for part in self.participations:
            counts[part.status] = counts.get(part.status, 0) + 1
        return [
            (status, PARTICIPATION_LABELS.get(status, status), counts[status])
            for status in PARTICIPATION_STATUSES if status in counts
        ]

    @property
    def referrals_are_reported(self):
        """True when no piece here carries a real measurement window.

        The campaign total is then a sum of numbers copied off a sheet, and
        labelling it "14 days after post" would be inventing a methodology.
        """
        if self.referral_totals:
            return False
        pieces = [p for p in self.ugc if p.referral_snapshots]
        return bool(pieces) and not any(p.has_window_referrals for p in pieces)

    @property
    def briefed_count(self):
        """Everyone who got as far as a briefing, delivered or not.

        Delivering implies having been briefed, so the delivered are counted
        here too — but only `records_briefing` says whether that number means
        anything beyond "the people who posted".
        """
        return sum(
            1 for p in self.participations
            if p.status in ("briefed", "content_delivered", "no_show")
        )

    @property
    def records_briefing(self):
        """Does this campaign have a briefing stage with people in it?

        False covers two different situations — a campaign that briefed people
        without recording it, and one that had no briefing step at all — and
        the dashboard shouldn't guess which, so it says neither. It just falls
        back to opt-ins. What matters either way is that `briefed_count`
        collapses to `delivered_count` here, so it must not be used as a
        denominator: dividing the deliverers by themselves reports a 100%
        follow-through that means nothing.
        """
        return any(p.status == "briefed" for p in self.participations)

    @property
    def commitment_count(self):
        """The strongest commitment this campaign can actually evidence.

        Briefed where briefing was tracked; opted in otherwise. Opting in is
        cheap — 143 people said yes to the softlaunch and 52 were briefed —
        so where the stronger number exists it is the one worth showing.
        """
        return self.briefed_count if self.records_briefing else self.opted_in_count

    @property
    def commitment_label(self):
        return "Briefed" if self.records_briefing else "Opted in"

    @property
    def delivery_rate(self):
        """Delivered as a share of the people who committed.

        Measured against whatever commitment the campaign can evidence, so a
        campaign that never tracked briefing doesn't report 100% follow-through
        by dividing the deliverers by themselves.
        """
        if not self.commitment_count:
            return 0
        return round(100 * self.delivered_count / self.commitment_count)

    def __repr__(self):
        return f"<Campaign {self.slug}>"


class Participation(db.Model):
    """A creator opting into one campaign, and whether they followed through."""

    __tablename__ = "participations"
    __table_args__ = (db.UniqueConstraint("creator_id", "campaign_id"),)

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    status = db.Column(db.String(24), default="opted_in", nullable=False)
    opted_in_on = db.Column(db.Date)

    creator = db.relationship("Creator", back_populates="participations")
    campaign = db.relationship("Campaign", back_populates="participations")

    @property
    def status_label(self):
        return PARTICIPATION_LABELS.get(self.status, self.status)

    @property
    def pieces(self):
        """This creator's UGC for this campaign."""
        return [
            p for p in self.creator.all_pieces if p.campaign_id == self.campaign_id
        ]

    @property
    def referrals(self):
        """Referrals credited to this creator for this campaign, or None."""
        for row in self.creator.campaign_referrals:
            if row.campaign_id == self.campaign_id:
                return row.referrals
        return None


class UgcPiece(db.Model):
    """One creative (one video), independent of where it was posted."""

    __tablename__ = "ugc_pieces"

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)

    title = db.Column(db.String(240))
    hook = db.Column(db.Text)          # opening line — what we want to pattern-match on
    posted_on = db.Column(db.Date)
    thumbnail_url = db.Column(db.String(512))
    video_path = db.Column(db.String(512))   # local/downloaded copy, under data/
    notes = db.Column(db.Text)

    # How this piece reached us — set to "email" by the inbox scan once that lands.
    discovered_via = db.Column(db.String(32), default="manual")

    creator = db.relationship("Creator", back_populates="ugc")
    campaign = db.relationship("Campaign", back_populates="ugc")
    posts = db.relationship(
        "PlatformPost", back_populates="piece",
        cascade="all, delete-orphan", lazy="selectin",
    )
    referral_snapshots = db.relationship(
        "ReferralSnapshot", back_populates="piece",
        cascade="all, delete-orphan", lazy="selectin",
    )
    collaborations = db.relationship(
        "PieceCollaborator", back_populates="piece",
        cascade="all, delete-orphan", lazy="selectin",
    )

    @property
    def creators(self):
        """Everyone on this piece, lead first."""
        out = [self.creator]
        for row in self.collaborations:
            if row.creator is not None and row.creator not in out:
                out.append(row.creator)
        return out

    @property
    def byline(self):
        return " & ".join(c.name for c in self.creators)

    @property
    def discovery_label(self):
        return DISCOVERY_LABELS.get(
            self.discovered_via, (self.discovered_via or "").replace("_", " ")
        )

    @property
    def platform_summary(self):
        """Where it ran, e.g. "Instagram + TikTok" — shown beside the title."""
        return " + ".join(sorted({p.platform_label for p in self.posts})) or "No link"

    @property
    def display_title(self):
        """What to show when the piece has no title.

        Most pieces don't have one: a CRM export is a list of links, and the
        title field stays empty rather than being filled with something nobody
        wrote. This builds a label out of facts we do have instead.
        """
        if self.title:
            return self.title
        platforms = sorted({p.platform_label for p in self.posts})
        where = " + ".join(platforms) if platforms else "Content"
        when = self.posted_on.strftime("%b %Y") if self.posted_on else None
        return f"{where} post, {when}" if when else f"{where} post"

    @property
    def likes(self):
        return sum(p.likes or 0 for p in self.posts)

    @property
    def comments(self):
        return sum(p.comments or 0 for p in self.posts)

    @property
    def views(self):
        return sum(p.views or 0 for p in self.posts)

    @property
    def engagement(self):
        """Likes + comments, summed across every platform this ran on."""
        return self.likes + self.comments

    @property
    def platforms(self):
        return [p.platform for p in self.posts]

    @property
    def is_cross_posted(self):
        return len(self.posts) > 1

    @property
    def referrals(self):
        """Referrals in the campaign's measurement window (14 days post-date).

        Falls back to the latest snapshot when the window one isn't in yet, so a
        recent post still shows something rather than a bare zero.
        """
        if not self.referral_snapshots:
            return 0
        windowed = [s for s in self.referral_snapshots if s.window_days == REFERRAL_WINDOW_DAYS]
        if windowed:
            return max(s.referrals for s in windowed)
        return max(s.referrals for s in self.referral_snapshots)

    @property
    def has_window_referrals(self):
        """True when a real 14-day measurement exists, not just a copied total."""
        return any(
            s.window_days == REFERRAL_WINDOW_DAYS for s in self.referral_snapshots
        )

    @property
    def referrals_are_final(self):
        """True once the 14-day window has actually closed."""
        if not self.posted_on:
            return False
        return date.today() >= self.posted_on + timedelta(days=REFERRAL_WINDOW_DAYS)

    @property
    def creator_referrals(self):
        """Referrals credited to the people who made this, for its campaign.

        This is not the video's own number, and nothing here can produce one:
        the source counts invite-code redemptions per account, so a creator
        with three videos in a campaign has one figure for all three, and a
        creator with none still has theirs. What a video page can honestly
        show is that figure, labelled as the creator's.

        For a piece made by several people it is their combined figure. Zero
        when the campaign was measured and they earned none; None when the
        campaign has no measurement at all (falling back to any per-piece
        snapshot that predates per-creator totals).
        """
        campaign = self.campaign
        if campaign is None:
            return None
        if not campaign.referral_totals:
            return self.referrals if self.referral_snapshots else None
        ids = {c.id for c in self.creators}
        return sum(
            r.referrals for r in campaign.referral_totals if r.creator_id in ids
        )

    @property
    def engagement_rate(self):
        if not self.views:
            return None
        return round(100 * self.engagement / self.views, 1)

    def post_for(self, platform):
        for p in self.posts:
            if p.platform == platform:
                return p
        return None

    def __repr__(self):
        return f"<UgcPiece {self.id} {self.title!r}>"


class PieceCollaborator(db.Model):
    """A second (or third) creator on one piece of content.

    `UgcPiece.creator_id` stays the piece's lead creator. This is for the rest:
    two people who run one channel post one video, and both delivered it.
    Giving each a copy of the piece would count the video — and later its
    views and likes — twice in the campaign; crediting only one of them
    reports the other as never having delivered. So the piece exists once and
    lists everyone on it.
    """

    __tablename__ = "piece_collaborators"
    __table_args__ = (db.UniqueConstraint("ugc_piece_id", "creator_id"),)

    id = db.Column(db.Integer, primary_key=True)
    ugc_piece_id = db.Column(db.Integer, db.ForeignKey("ugc_pieces.id"), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)

    piece = db.relationship("UgcPiece", back_populates="collaborations")
    creator = db.relationship("Creator", back_populates="collaborations")


class PlatformPost(db.Model):
    """The same creative, as it appeared on one platform."""

    __tablename__ = "platform_posts"
    __table_args__ = (db.UniqueConstraint("ugc_piece_id", "platform"),)

    id = db.Column(db.Integer, primary_key=True)
    ugc_piece_id = db.Column(db.Integer, db.ForeignKey("ugc_pieces.id"), nullable=False)
    platform = db.Column(db.String(16), nullable=False)
    url = db.Column(db.String(512))
    posted_on = db.Column(db.Date)

    views = db.Column(db.Integer, default=0)
    likes = db.Column(db.Integer, default=0)
    comments = db.Column(db.Integer, default=0)
    shares = db.Column(db.Integer, default=0)
    saves = db.Column(db.Integer, default=0)

    measured_at = db.Column(db.Date)

    piece = db.relationship("UgcPiece", back_populates="posts")

    @property
    def platform_label(self):
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def engagement(self):
        return (self.likes or 0) + (self.comments or 0)

    @property
    def engagement_rate(self):
        if not self.views:
            return None
        return round(100 * self.engagement / self.views, 1)


class InboxItem(db.Model):
    """A creator email that looks like a content submission, awaiting review.

    Populated by the periodic inbox scan (see inbox.py). Kept as a queue rather
    than writing straight through to UgcPiece, because sender-to-creator
    matching is a guess and a wrong guess silently corrupts campaign numbers.
    Accepting one is a single click.
    """

    __tablename__ = "inbox_items"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(512), unique=True, nullable=False)
    sender = db.Column(db.String(254))
    subject = db.Column(db.String(512))
    received_on = db.Column(db.Date)

    links_json = db.Column(db.Text)          # JSON [[platform, url], ...]

    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"))
    match_note = db.Column(db.String(128))   # how the creator was matched
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"))
    campaign_note = db.Column(db.String(128))

    status = db.Column(db.String(16), default="pending")  # pending|accepted|dismissed
    ugc_piece_id = db.Column(db.Integer, db.ForeignKey("ugc_pieces.id"))

    creator = db.relationship("Creator", lazy="selectin")
    campaign = db.relationship("Campaign", lazy="selectin")
    created_piece = db.relationship("UgcPiece", lazy="selectin")

    @property
    def links(self):
        import json

        if not self.links_json:
            return []
        try:
            return [tuple(x) for x in json.loads(self.links_json)]
        except ValueError:
            return []

    @property
    def platforms(self):
        return sorted({p for p, _ in self.links})

    @property
    def needs_attention(self):
        """Can't be accepted as-is — missing a creator or a campaign."""
        return not (self.creator_id and self.campaign_id)


class ReferralSnapshot(db.Model):
    """Referrals attributed to a piece, measured some number of days after posting.

    Stored as snapshots so the 14-day campaign metric stays comparable across
    pieces posted at different times, and so we can see the curve later.
    """

    __tablename__ = "referral_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    ugc_piece_id = db.Column(db.Integer, db.ForeignKey("ugc_pieces.id"), nullable=False)
    window_days = db.Column(db.Integer, nullable=False, default=REFERRAL_WINDOW_DAYS)
    referrals = db.Column(db.Integer, default=0)
    measured_at = db.Column(db.Date)

    piece = db.relationship("UgcPiece", back_populates="referral_snapshots")

    @property
    def window_label(self):
        if self.window_days == REPORTED_WINDOW:
            return "As reported"
        return f"{self.window_days} days"


class CampaignReferral(db.Model):
    """Signups credited to one creator for one campaign, over a stated window.

    Referrals are earned by a person in a campaign, not by a particular video:
    the source counts invite-code redemptions per account, and a creator who
    posted three videos — or none at all — still has one number. Hanging that
    number off a UgcPiece meant picking an arbitrary piece to carry it, and
    dropped it entirely for creators with no content (the real export has one
    creator with 25 referrals and nothing posted).

    The window is stored with the count, because "how many referrals" is
    meaningless without "between when and when", and because a later refresh
    over a different window must not look like the same measurement.

    `UgcPiece.referral_snapshots` stays for genuine per-piece measurement, if
    attribution ever gets that precise.
    """

    __tablename__ = "campaign_referrals"
    __table_args__ = (db.UniqueConstraint("creator_id", "campaign_id"),)

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)

    referrals = db.Column(db.Integer, default=0, nullable=False)
    window_from = db.Column(db.Date)
    window_to = db.Column(db.Date)
    measured_at = db.Column(db.Date, default=date.today)
    source = db.Column(db.String(120))   # where the number came from

    creator = db.relationship("Creator", back_populates="campaign_referrals")
    campaign = db.relationship("Campaign", back_populates="referral_totals")

    @property
    def window_label(self):
        if self.window_from and self.window_to:
            return f"{self.window_from:%b %-d} – {self.window_to:%b %-d, %Y}"
        return "window not recorded"


class ScanRun(db.Model):
    """One attempt to read the inbox.

    Recorded whether it succeeded or not, and surfaced in /inbox. This is
    deliberate: the reason the scheduled Claude job was abandoned is that it
    failed invisibly — contextone keeps no transcript for periodic sessions and
    reports `success` for a job that did nothing. A scanner you can't observe is
    worse than no scanner, so every run leaves a row.

    The `started_at` column doubles as the lock. Gunicorn runs more than one
    worker, each with its own scheduler thread; a worker claims the next scan by
    inserting a row inside a transaction and checking it won.
    """

    __tablename__ = "scan_runs"

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, nullable=False, index=True)
    finished_at = db.Column(db.DateTime)
    # scheduled | manual | cli
    trigger = db.Column(db.String(16), nullable=False, default="scheduled")
    # running | ok | error
    status = db.Column(db.String(16), nullable=False, default="running")
    messages_seen = db.Column(db.Integer, default=0)
    queued = db.Column(db.Integer, default=0)
    duplicates = db.Column(db.Integer, default=0)
    skipped = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)

    @property
    def duration_seconds(self):
        if not (self.started_at and self.finished_at):
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def summary(self):
        if self.status == "running":
            return "running…"
        if self.status == "error":
            return (self.error or "failed").splitlines()[0][:140]
        if self.queued:
            return f"{self.queued} queued from {self.messages_seen} scanned"
        return f"nothing new in {self.messages_seen} scanned"
