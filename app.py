import os
from datetime import date

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from models import (
    PLATFORMS,
    PLATFORM_LABELS,
    REFERRAL_WINDOW_DAYS,
    Campaign,
    Creator,
    InboxItem,
    Participation,
    UgcPiece,
    db,
)
import inbox as inbox_lib
import auth
import gmail
import scan as scan_lib

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

# Session hardening + the password gate. Everything except /healthz, /login and
# the token-authed /api/ingest requires a session from here on.
auth.configure(app)
auth.login_view(app)
auth.install(app)

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

# Start the Gmail inbox scanner. No-op unless GMAIL_CLIENT_ID / _SECRET /
# _REFRESH_TOKEN are all set, and only one gunicorn worker wins the timer.
scan_lib.start(app)


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
        "inbox_pending": InboxItem.query.filter_by(status="pending").count(),
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


@app.route("/inbox")
def inbox():
    pending = InboxItem.query.filter_by(status="pending").order_by(
        InboxItem.received_on.desc()
    ).all()
    recent = InboxItem.query.filter(InboxItem.status != "pending").order_by(
        InboxItem.id.desc()
    ).limit(20).all()
    return render_template(
        "inbox.html",
        pending=pending,
        recent=recent,
        all_campaigns=Campaign.query.order_by(Campaign.starts_on.desc()).all(),
        scanner=scan_lib.status(),
    )


@app.route("/inbox/<int:item_id>/accept", methods=["POST"])
def inbox_accept(item_id):
    item = db.session.get(InboxItem, item_id) or abort(404)
    campaign_id = request.form.get("campaign_id", type=int) or item.campaign_id
    try:
        inbox_lib.accept(item, campaign_id=campaign_id)
    except ValueError as exc:
        return redirect(url_for("inbox", error=str(exc)))
    return redirect(url_for("inbox"))


@app.route("/inbox/<int:item_id>/dismiss", methods=["POST"])
def inbox_dismiss(item_id):
    item = db.session.get(InboxItem, item_id) or abort(404)
    item.status = "dismissed"
    db.session.commit()
    return redirect(url_for("inbox"))


@app.route("/inbox/scan", methods=["POST"])
def inbox_scan():
    """The "Scan now" button. Synchronous — a scan is a handful of API calls."""
    if not gmail.is_configured():
        return redirect(url_for("inbox", error="Gmail is not configured"))
    run = scan_lib.run_scan(trigger="manual")
    return redirect(url_for("inbox", scanned=run.id))


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger a scan from outside — for an external cron, or to test credentials.

    Shares INGEST_TOKEN with /api/ingest: both are the same privilege (write to
    the review queue), so a second secret would be bookkeeping without a benefit.
    """
    expected = os.environ.get("INGEST_TOKEN")
    if not expected:
        return jsonify(error="scan disabled: INGEST_TOKEN not set"), 503
    if request.headers.get("Authorization") != f"Bearer {expected}":
        return jsonify(error="unauthorized"), 401
    if not gmail.is_configured():
        return jsonify(error="gmail not configured: GMAIL_* env vars missing"), 503

    run = scan_lib.run_scan(trigger="manual")
    return jsonify(
        status=run.status,
        messages_seen=run.messages_seen,
        queued=run.queued,
        duplicates=run.duplicates,
        skipped=run.skipped,
        error=run.error,
    ), (200 if run.status == "ok" else 502)


@app.route("/api/gmail/check", methods=["POST"])
def api_gmail_check():
    """Does the refresh token work? Reads the account profile, no mail."""
    expected = os.environ.get("INGEST_TOKEN")
    if not expected:
        return jsonify(error="disabled: INGEST_TOKEN not set"), 503
    if request.headers.get("Authorization") != f"Bearer {expected}":
        return jsonify(error="unauthorized"), 401
    if not gmail.is_configured():
        return jsonify(configured=False,
                       error="GMAIL_CLIENT_ID / _SECRET / _REFRESH_TOKEN not all set"), 503
    try:
        return jsonify(configured=True, account=gmail.check(), query=gmail.query())
    except gmail.GmailError as exc:
        return jsonify(configured=True, error=str(exc)), 502


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive scanned emails from the periodic inbox job.

    Fails closed: with no INGEST_TOKEN configured the endpoint is disabled
    outright, so a public deployment can't be written to by strangers.
    """
    expected = os.environ.get("INGEST_TOKEN")
    if not expected:
        return jsonify(error="ingest disabled: INGEST_TOKEN not set"), 503
    if request.headers.get("Authorization") != f"Bearer {expected}":
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return jsonify(error="expected {\"messages\": [...]}"), 400

    queued, skipped, duplicates = 0, 0, 0
    for m in messages:
        if not m.get("message_id"):
            skipped += 1
            continue
        received = m.get("received_on")
        if isinstance(received, str):
            try:
                received = date.fromisoformat(received[:10])
            except ValueError:
                received = None
        item, created = inbox_lib.ingest_message(
            message_id=m["message_id"],
            sender=m.get("sender"),
            subject=m.get("subject"),
            body=m.get("body") or "",
            received_on=received,
        )
        if created:
            queued += 1
        elif item is not None:
            duplicates += 1
        else:
            skipped += 1

    return jsonify(queued=queued, duplicates=duplicates, skipped_no_links=skipped)


@app.route("/healthz")
def healthz():
    """Liveness probe — Contextone uses this to tell if the container is up."""
    return jsonify(status="ok")


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
