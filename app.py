import os
from datetime import date

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, abort, jsonify, render_template, request

from models import (
    PLATFORMS,
    PLATFORM_LABELS,
    REFERRAL_WINDOW_DAYS,
    Campaign,
    Creator,
    Participation,
    UgcPiece,
    db,
)

app = Flask(__name__)

# Resolve the SQLite path. In production Contextone mounts ./data at /app/data,
# so DATABASE_URL points inside it; locally we fall back to ./data/creatorwise.db.
_db_url = os.environ.get("DATABASE_URL", "")
if not _db_url:
    _data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(_data_dir, exist_ok=True)
    _db_url = "sqlite:///" + os.path.join(_data_dir, "creatorwise.db")
elif _db_url.startswith("sqlite:////"):
    os.makedirs(os.path.dirname(_db_url.replace("sqlite:////", "/")), exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
with app.app_context():
    db.create_all()

    # Sample data for the deployed demo. Opt-in only, and only into an empty
    # database — it will never overwrite real data. Turn this off (and wipe the
    # volume) as soon as the real Betawise/Notion import lands.
    if os.environ.get("SEED_SAMPLE_DATA") == "1" and not Creator.query.first():
        import seed as _seed

        _seed.seed()
        app.logger.warning("seeded sample data (SEED_SAMPLE_DATA=1)")


# --- formatting helpers -------------------------------------------------------

@app.template_filter("compact")
def compact(n):
    """1234 -> 1.2k, 1234567 -> 1.2m. Keeps table columns narrow."""
    if n is None:
        return "—"
    n = int(n)
    if abs(n) < 1000:
        return str(n)
    if abs(n) < 1_000_000:
        v = n / 1000
        return f"{v:.1f}k".replace(".0k", "k")
    v = n / 1_000_000
    return f"{v:.1f}m".replace(".0m", "m")


@app.template_filter("thousands")
def thousands(n):
    return "—" if n is None else f"{int(n):,}"


@app.template_filter("shortdate")
def shortdate(d):
    if not d:
        return "—"
    return d.strftime("%b %-d, %Y") if os.name != "nt" else d.strftime("%b %d, %Y")


@app.context_processor
def inject_globals():
    return {
        "nav_campaigns": Campaign.query.order_by(Campaign.starts_on.desc()).all(),
        "platform_labels": PLATFORM_LABELS,
        "platforms": PLATFORMS,
        "referral_window": REFERRAL_WINDOW_DAYS,
        "today": date.today(),
    }


# --- routes -------------------------------------------------------------------

@app.route("/")
def index():
    campaigns = Campaign.query.order_by(Campaign.starts_on.desc()).all()
    pieces = UgcPiece.query.all()

    top_pieces = sorted(pieces, key=lambda p: p.referrals, reverse=True)[:5]
    creators = Creator.query.all()
    top_creators = sorted(creators, key=lambda c: c.total_referrals, reverse=True)[:5]

    totals = {
        "creators": len(creators),
        "pieces": len(pieces),
        "referrals": sum(p.referrals for p in pieces),
        "engagement": sum(p.engagement for p in pieces),
        "views": sum(p.views for p in pieces),
    }
    return render_template(
        "index.html",
        campaigns=campaigns,
        totals=totals,
        top_pieces=top_pieces,
        top_creators=top_creators,
    )


@app.route("/campaigns")
def campaigns():
    items = Campaign.query.order_by(Campaign.starts_on.desc()).all()
    return render_template("campaigns.html", campaigns=items)


SORTS = {
    "posted": ("Post date", lambda p: (p.posted_on or date.min)),
    "referrals": ("Referrals", lambda p: p.referrals),
    "likes": ("Likes", lambda p: p.likes),
    "comments": ("Comments", lambda p: p.comments),
}


@app.route("/campaigns/<slug>")
def campaign_detail(slug):
    campaign = Campaign.query.filter_by(slug=slug).first() or abort(404)

    sort = request.args.get("sort", "referrals")
    if sort not in SORTS:
        sort = "referrals"
    direction = request.args.get("dir", "desc")
    reverse = direction != "asc"

    pieces = sorted(campaign.ugc, key=SORTS[sort][1], reverse=reverse)

    # Everyone who opted in but hasn't delivered — the follow-up list.
    outstanding = [
        p for p in campaign.participations if p.status != "content_delivered"
    ]
    outstanding.sort(key=lambda p: p.creator.name)

    return render_template(
        "campaign_detail.html",
        campaign=campaign,
        pieces=pieces,
        outstanding=outstanding,
        sort=sort,
        direction=direction,
        sorts=SORTS,
    )


@app.route("/creators")
def creators():
    items = Creator.query.all()
    sort = request.args.get("sort", "referrals")
    keys = {
        "name": lambda c: c.name.lower(),
        "referrals": lambda c: -c.total_referrals,
        "engagement": lambda c: -c.total_engagement,
        "followers": lambda c: -c.follower_count,
        "delivered": lambda c: -c.delivered_count,
    }
    items.sort(key=keys.get(sort, keys["referrals"]))
    return render_template("creators.html", creators=items, sort=sort)


@app.route("/creators/<int:creator_id>")
def creator_detail(creator_id):
    creator = db.session.get(Creator, creator_id) or abort(404)
    pieces = sorted(creator.ugc, key=lambda p: (p.posted_on or date.min), reverse=True)
    participations = sorted(
        creator.participations,
        key=lambda p: (p.campaign.starts_on or date.min),
        reverse=True,
    )
    return render_template(
        "creator_detail.html",
        creator=creator,
        pieces=pieces,
        participations=participations,
    )


@app.route("/ugc/<int:piece_id>")
def ugc_detail(piece_id):
    piece = db.session.get(UgcPiece, piece_id) or abort(404)
    posts = sorted(piece.posts, key=lambda p: -(p.views or 0))
    snapshots = sorted(piece.referral_snapshots, key=lambda s: s.window_days)
    return render_template(
        "ugc_detail.html", piece=piece, posts=posts, snapshots=snapshots
    )


@app.route("/healthz")
def healthz():
    """Liveness probe — Contextone uses this to tell if the container is up."""
    return jsonify(status="ok")


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
