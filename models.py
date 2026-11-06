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

from datetime import date, timedelta

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

PLATFORMS = ("instagram", "tiktok", "youtube")
PLATFORM_LABELS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}

# How a creator came to us. Kept as plain strings so new sources don't need a migration.
RECRUITMENT_SOURCES = ("betawise", "outbound", "referral", "inbound", "agency")

# Where a creator stands in a given campaign.
PARTICIPATION_STATUSES = ("opted_in", "content_delivered", "no_show")
PARTICIPATION_LABELS = {
    "opted_in": "Opted in",
    "content_delivered": "Delivered",
    "no_show": "No show",
}

# The window used for the campaign referral metric.
REFERRAL_WINDOW_DAYS = 14


class Creator(db.Model):
    __tablename__ = "creators"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False)
    recruitment_source = db.Column(db.String(32))
    recruitment_note = db.Column(db.Text)
    notes = db.Column(db.Text)

    # Payment details. Deliberately coarse for v1 — no card/bank numbers here,
    # just the handle needed to send money and the terms agreed.
    payment_method = db.Column(db.String(32))      # paypal | venmo | wise | gift_card
    payment_handle = db.Column(db.String(254))
    payment_note = db.Column(db.Text)

    created_at = db.Column(db.Date, default=date.today)

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

    @property
    def total_referrals(self):
        return sum(p.referrals for p in self.ugc)

    @property
    def total_engagement(self):
        return sum(p.engagement for p in self.ugc)

    @property
    def follower_count(self):
        """Largest single-platform audience, not the sum — summing double-counts."""
        return max((h.follower_count or 0 for h in self.handles), default=0)

    @property
    def delivered_count(self):
        return sum(1 for p in self.participations if p.status == "content_delivered")

    def __repr__(self):
        return f"<Creator {self.name}>"


class SocialHandle(db.Model):
    __tablename__ = "social_handles"

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creators.id"), nullable=False)
    platform = db.Column(db.String(16), nullable=False)
    handle = db.Column(db.String(128), nullable=False)
    follower_count = db.Column(db.Integer)
    profile_url = db.Column(db.String(512))

    creator = db.relationship("Creator", back_populates="handles")

    @property
    def platform_label(self):
        return PLATFORM_LABELS.get(self.platform, self.platform)


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

    @property
    def creator_count(self):
        return len(self.participations)

    @property
    def delivered_count(self):
        return sum(1 for p in self.participations if p.status == "content_delivered")

    @property
    def total_referrals(self):
        return sum(p.referrals for p in self.ugc)

    @property
    def total_engagement(self):
        return sum(p.engagement for p in self.ugc)

    @property
    def delivery_rate(self):
        if not self.participations:
            return 0
        return round(100 * self.delivered_count / len(self.participations))

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
        return [p for p in self.creator.ugc if p.campaign_id == self.campaign_id]


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
    def referrals_are_final(self):
        """True once the 14-day window has actually closed."""
        if not self.posted_on:
            return False
        return date.today() >= self.posted_on + timedelta(days=REFERRAL_WINDOW_DAYS)

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
