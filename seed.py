"""Seed creatorwise with representative sample data.

Run with:  python seed.py          (wipes and reseeds)
           python seed.py --keep   (no-op if data already present)

The numbers here are invented but shaped to behave like the real thing: a long
tail of small creators, a couple of breakout pieces, cross-posting on only some
creatives, and a visible drop-off between opting in and actually delivering.
Replace this with the real import once the Betawise/Notion export is ready.
"""

import random
import sys
from datetime import date, timedelta

from models import (
    Campaign,
    Creator,
    Participation,
    PlatformPost,
    ReferralSnapshot,
    SocialHandle,
    UgcPiece,
    db,
    REFERRAL_WINDOW_DAYS,
)

rng = random.Random(20261105)  # fixed seed → reproducible sample data

CAMPAIGNS = [
    {
        "name": "Fall 2025 Sticker Beta",
        "slug": "fall-2025-sticker-beta",
        "description": "First pilot. Creators got a sticker pack and early Bookwise access.",
        "starts_on": date(2025, 9, 15),
        "ends_on": date(2025, 11, 30),
        "status": "complete",
        "payment_terms": "$150 flat per delivered video. Sticker pack + 1 year Bookwise.",
    },
    {
        "name": "Summer 2026 Softlaunch",
        "slug": "summer-2026-softlaunch",
        "description": "Second pilot, wider cohort. Softlaunch build with referral links.",
        "starts_on": date(2026, 6, 1),
        "ends_on": date(2026, 8, 31),
        "status": "complete",
        "payment_terms": "$250 flat per delivered video, plus $2 per referred signup.",
    },
    {
        "name": "Books Announcement",
        "slug": "books-announcement",
        "description": "Readwise Books announcement push. 200+ creators recruited.",
        "starts_on": date(2026, 11, 1),
        "ends_on": date(2027, 1, 31),
        "status": "active",
        "payment_terms": "$400 flat per delivered video, plus $3 per referred signup.",
    },
]

CREATORS = [
    ("Maya Okonkwo", "maya.okonkwo@example.com", "betawise", "instagram", 48200, "paypal"),
    ("Devon Reyes", "devon.reyes@example.com", "betawise", "tiktok", 112000, "venmo"),
    ("Priya Raghunathan", "priya.r@example.com", "outbound", "instagram", 31500, "wise"),
    ("Sam Bauer", "sam.bauer@example.com", "referral", "youtube", 86400, "paypal"),
    ("Ines Varela", "ines.varela@example.com", "betawise", "tiktok", 204000, "paypal"),
    ("Theo Lindqvist", "theo.l@example.com", "inbound", "instagram", 9800, "venmo"),
    ("Nadia Haddad", "nadia.haddad@example.com", "agency", "tiktok", 377000, "wise"),
    ("Caleb Mwangi", "caleb.mwangi@example.com", "outbound", "youtube", 24100, "paypal"),
    ("Juno Park", "juno.park@example.com", "betawise", "instagram", 63700, "paypal"),
    ("Rosa Delgado", "rosa.delgado@example.com", "referral", "tiktok", 15300, "venmo"),
    ("Felix Brandt", "felix.brandt@example.com", "inbound", "youtube", 141000, "wise"),
    ("Amara Boateng", "amara.boateng@example.com", "betawise", "instagram", 27800, "paypal"),
    ("Wes Tanaka", "wes.tanaka@example.com", "outbound", "tiktok", 52600, "paypal"),
    ("Liv Sorensen", "liv.sorensen@example.com", "agency", "instagram", 198000, "wise"),
]

HOOKS = [
    "I read 40 books this year and this app is the only reason.",
    "POV: you finally stop pretending you'll finish that book.",
    "Nobody talks about how lonely reading got.",
    "Three years of reading slumps, fixed in a week.",
    "My TBR was 212 books. Here's what actually worked.",
    "Reading apps are all the same. This one isn't.",
    "The stat that made me put my phone down.",
    "I tracked every page I read for 90 days.",
    "Why I quit Goodreads after eleven years.",
    "This is what reading with friends actually looks like.",
    "I didn't expect a reading app to make me cry.",
    "Stop buying books you'll never open. Do this instead.",
]

TITLES = [
    "40 books this year",
    "TBR reality check",
    "Reading is lonely now",
    "Slump fix",
    "212 books deep",
    "Not another reading app",
    "The stat that got me",
    "90 days of pages",
    "Leaving Goodreads",
    "Reading with friends",
    "Unexpectedly emotional",
    "Stop buying books",
]


def wipe():
    for model in (ReferralSnapshot, PlatformPost, UgcPiece, Participation, SocialHandle, Creator, Campaign):
        db.session.query(model).delete()
    db.session.commit()


def seed():
    campaigns = {}
    for spec in CAMPAIGNS:
        c = Campaign(**spec)
        db.session.add(c)
        campaigns[spec["slug"]] = c
    db.session.flush()

    creators = []
    for name, email, source, main_platform, followers, pay in CREATORS:
        handle = "@" + name.lower().replace(" ", "").replace("'", "")[:18]
        c = Creator(
            name=name,
            email=email,
            recruitment_source=source,
            recruitment_note={
                "betawise": "Signed up through the Betawise beta list.",
                "outbound": "Cold outreach after a book video performed well.",
                "referral": "Introduced by another creator in the cohort.",
                "inbound": "Emailed asking to join the program.",
                "agency": "Via talent agency roster.",
            }[source],
            payment_method=pay,
            payment_handle=email if pay != "venmo" else handle,
            payment_note="Invoice on delivery; net 15." if pay == "wise" else None,
            created_at=date(2025, 8, 1) + timedelta(days=rng.randint(0, 420)),
        )
        c.handles.append(SocialHandle(
            platform=main_platform, handle=handle, follower_count=followers,
            profile_url=f"https://{main_platform}.com/{handle.lstrip('@')}",
        ))
        # Most creators are active on a second platform with a smaller audience.
        if rng.random() < 0.6:
            second = rng.choice([p for p in ("instagram", "tiktok", "youtube") if p != main_platform])
            c.handles.append(SocialHandle(
                platform=second, handle=handle,
                follower_count=int(followers * rng.uniform(0.08, 0.45)),
                profile_url=f"https://{second}.com/{handle.lstrip('@')}",
            ))
        db.session.add(c)
        creators.append(c)
    db.session.flush()

    title_pool = list(zip(TITLES, HOOKS))

    for slug, opt_in_rate, deliver_rate in [
        ("fall-2025-sticker-beta", 0.55, 0.70),
        ("summer-2026-softlaunch", 0.85, 0.62),
        ("books-announcement", 1.00, 0.35),   # in flight — most haven't delivered yet
    ]:
        campaign = campaigns[slug]
        for creator in creators:
            if rng.random() > opt_in_rate:
                continue

            delivered = rng.random() < deliver_rate
            status = "content_delivered" if delivered else rng.choice(["opted_in", "no_show"])
            if campaign.status == "active" and not delivered:
                status = "opted_in"  # still time to deliver

            db.session.add(Participation(
                creator_id=creator.id,
                campaign_id=campaign.id,
                status=status,
                opted_in_on=campaign.starts_on + timedelta(days=rng.randint(0, 14)),
            ))

            if not delivered:
                continue

            title, hook = rng.choice(title_pool)
            window_end = min(campaign.ends_on, date.today())
            span = max((window_end - campaign.starts_on).days, 1)
            # An in-flight campaign may only be days old, so don't assume there's
            # room for a 7-day lead-in.
            earliest = min(7, span)
            posted = campaign.starts_on + timedelta(days=rng.randint(earliest, span))

            piece = UgcPiece(
                creator_id=creator.id,
                campaign_id=campaign.id,
                title=title,
                hook=hook,
                posted_on=posted,
                thumbnail_url=None,
                video_path=f"data/ugc/{slug}/{creator.id}-{posted.isoformat()}.mp4",
                discovered_via=rng.choice(["email", "email", "manual"]),
            )
            db.session.add(piece)
            db.session.flush()

            # Which platforms this creative ran on — their main one, sometimes more.
            platforms = [h.platform for h in creator.handles]
            if len(platforms) > 1 and rng.random() < 0.45:
                chosen = platforms
            else:
                chosen = platforms[:1]

            # One breakout piece per cohort keeps the sort order interesting.
            breakout = rng.random() < 0.08
            base_views = int(creator.follower_count * rng.uniform(0.4, 2.2))
            if breakout:
                base_views = int(base_views * rng.uniform(6, 14))

            total_views = 0
            for platform in chosen:
                views = max(int(base_views * rng.uniform(0.35, 1.0)), 120)
                like_rate = rng.uniform(0.035, 0.11)
                likes = int(views * like_rate)
                db.session.add(PlatformPost(
                    ugc_piece_id=piece.id,
                    platform=platform,
                    url=f"https://{platform}.com/p/{slug[:6]}{piece.id}{platform[:2]}",
                    posted_on=posted,
                    views=views,
                    likes=likes,
                    comments=int(likes * rng.uniform(0.02, 0.09)),
                    shares=int(likes * rng.uniform(0.01, 0.06)),
                    saves=int(likes * rng.uniform(0.03, 0.15)),
                    measured_at=min(posted + timedelta(days=REFERRAL_WINDOW_DAYS), date.today()),
                ))
                total_views += views

            # Referral curve: most of it lands in the first few days.
            closed = date.today() >= posted + timedelta(days=REFERRAL_WINDOW_DAYS)
            final = int(total_views * rng.uniform(0.0008, 0.006))
            for window in (3, 7, REFERRAL_WINDOW_DAYS):
                if not closed and date.today() < posted + timedelta(days=window):
                    continue
                share = {3: 0.45, 7: 0.78, REFERRAL_WINDOW_DAYS: 1.0}[window]
                db.session.add(ReferralSnapshot(
                    ugc_piece_id=piece.id,
                    window_days=window,
                    referrals=int(final * share),
                    measured_at=posted + timedelta(days=window),
                ))

    db.session.commit()


def summarise():
    print(f"campaigns:   {Campaign.query.count()}")
    print(f"creators:    {Creator.query.count()}")
    print(f"handles:     {SocialHandle.query.count()}")
    print(f"opt-ins:     {Participation.query.count()}")
    print(f"ugc pieces:  {UgcPiece.query.count()}")
    print(f"posts:       {PlatformPost.query.count()}")
    print(f"snapshots:   {ReferralSnapshot.query.count()}")
    for c in Campaign.query.all():
        print(f"  {c.name:<26} {c.delivered_count}/{c.creator_count} delivered  "
              f"{c.total_referrals} referrals  {c.total_engagement} engagement")


if __name__ == "__main__":
    # Imported here, not at module scope, so app.py can import this module
    # without a circular import.
    from app import app

    with app.app_context():
        db.create_all()
        if "--keep" in sys.argv and Creator.query.count():
            print("data already present, leaving it alone")
        else:
            wipe()
            seed()
        summarise()
