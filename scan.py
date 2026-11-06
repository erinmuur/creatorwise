"""Run an inbox scan and record it.

One scan = read matching mail from Gmail, hand each message to
`inbox.ingest_message`, write a ScanRun row saying what happened. Nothing here
touches the review decision: messages land in /inbox as pending, exactly as they
did when a Claude job was POSTing them to /api/ingest.

Scheduling lives here too, as a background thread inside the web process rather
than an external cron. That's a deliberate trade: contextone's periodic jobs
can't reach a mail connector and keep no transcript, so the app owning its own
timer is both the only option that works and the one we can actually observe.

Gunicorn runs several workers. Exactly one of them schedules, decided by an
flock on a file in the mounted data volume — whichever worker gets the lock owns
the timer, and if it dies the fd closes and the next worker takes over.
"""

import os
import threading
import traceback
from datetime import datetime, timedelta, timezone

import gmail
import inbox as inbox_lib
from models import ScanRun, db

DEFAULT_INTERVAL_HOURS = 24

# A failed run shouldn't wait a whole interval to try again — a Google blip or a
# worker killed mid-scan would silently cost a day of mail. But it shouldn't
# hammer either, so errors retry on their own, shorter clock.
RETRY_AFTER = timedelta(hours=1)

# A run still marked `running` after this long was interrupted (worker killed
# mid-scan). Don't let it block the schedule forever.
STALE_AFTER = timedelta(minutes=30)

# How often the thread wakes to ask "is a scan due?". Short relative to the
# interval so a restart doesn't skew the schedule much.
TICK_SECONDS = 300


def utcnow():
    """Naive UTC, matching the DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def interval_hours():
    try:
        return max(1, int(os.environ.get("GMAIL_SCAN_INTERVAL_HOURS", "")
                          or DEFAULT_INTERVAL_HOURS))
    except ValueError:
        return DEFAULT_INTERVAL_HOURS


def enabled():
    """Dormant unless credentials exist. GMAIL_SCAN_ENABLED=0 forces it off."""
    if os.environ.get("GMAIL_SCAN_ENABLED", "1") == "0":
        return False
    return gmail.is_configured()


def status():
    """What /inbox shows about the scanner."""
    last = ScanRun.query.order_by(ScanRun.started_at.desc()).first()
    return {
        "configured": gmail.is_configured(),
        "enabled": enabled(),
        "interval_hours": interval_hours(),
        "query": gmail.query(),
        "last": last,
        "recent": ScanRun.query.order_by(ScanRun.started_at.desc()).limit(10).all(),
    }


# --- running a scan -----------------------------------------------------------

def _reap_stale(now):
    cutoff = now - STALE_AFTER
    stale = ScanRun.query.filter(
        ScanRun.status == "running", ScanRun.started_at < cutoff
    ).all()
    for run in stale:
        run.status = "error"
        run.error = "interrupted — the worker went away mid-scan"
        run.finished_at = now
    if stale:
        db.session.commit()
    return len(stale)


def is_due(now=None):
    now = now or utcnow()
    _reap_stale(now)

    if ScanRun.query.filter_by(status="running").first():
        return False

    last = ScanRun.query.order_by(ScanRun.started_at.desc()).first()
    if last is None:
        return True

    wait = RETRY_AFTER if last.status == "error" else timedelta(hours=interval_hours())
    return last.started_at <= now - wait


def run_scan(trigger="scheduled", limit=None):
    """Do one scan. Returns the ScanRun row, committed either way.

    Never raises for an expected failure (bad credentials, Google unreachable):
    those are recorded on the row, because a scanner that crashes its thread
    stops scanning forever and tells nobody.
    """
    now = utcnow()
    _reap_stale(now)

    run = ScanRun(started_at=now, trigger=trigger, status="running")
    db.session.add(run)
    db.session.commit()

    try:
        messages = gmail.fetch_messages(limit=limit)
        queued = duplicates = skipped = 0

        for m in messages:
            if not m.get("message_id"):
                skipped += 1
                continue
            item, created = inbox_lib.ingest_message(
                message_id=m["message_id"],
                sender=m.get("sender"),
                subject=m.get("subject"),
                body=m.get("body") or "",
                received_on=m.get("received_on"),
            )
            if created:
                queued += 1
            elif item is not None:
                duplicates += 1
            else:
                # No platform links, or an automated sender — not a submission.
                skipped += 1

        run.messages_seen = len(messages)
        run.queued = queued
        run.duplicates = duplicates
        run.skipped = skipped
        run.status = "ok"
    except gmail.GmailError as exc:
        run.status = "error"
        run.error = str(exc)
    except Exception:
        run.status = "error"
        run.error = traceback.format_exc(limit=4)
    finally:
        run.finished_at = utcnow()
        db.session.commit()

    return run


# --- the scheduler thread -----------------------------------------------------

_lock_handle = None  # keep the fd alive for the process lifetime


def _claim_scheduler_slot(app):
    """True in exactly one worker. Uses flock, so it releases when the process dies."""
    global _lock_handle
    import fcntl

    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite:////"):
        data_dir = os.path.dirname(uri.replace("sqlite:////", "/"))
    elif uri.startswith("sqlite:///"):
        data_dir = os.path.dirname(uri.replace("sqlite:///", ""))
    else:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    try:
        os.makedirs(data_dir, exist_ok=True)
        handle = open(os.path.join(data_dir, "scanner.lock"), "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False

    _lock_handle = handle
    return True


def _loop(app):
    import time

    while True:
        try:
            with app.app_context():
                if enabled() and is_due():
                    run = run_scan(trigger="scheduled")
                    app.logger.warning(
                        "inbox scan %s: %s", run.status, run.summary
                    )
        except Exception:
            # Keep the thread alive through anything — including a database that
            # went away — or scanning stops silently until the next deploy.
            try:
                app.logger.exception("inbox scan loop error")
            except Exception:
                pass
        time.sleep(TICK_SECONDS)


def start(app):
    """Start the timer in this worker, if it should own it."""
    if not enabled():
        app.logger.warning(
            "inbox scanner dormant: %s",
            "GMAIL_SCAN_ENABLED=0" if gmail.is_configured()
            else "GMAIL_* credentials not set",
        )
        return False

    if not _claim_scheduler_slot(app):
        return False

    threading.Thread(target=_loop, args=(app,), daemon=True, name="inbox-scan").start()
    app.logger.warning(
        "inbox scanner started: every %sh, query %r", interval_hours(), gmail.query()
    )
    return True
