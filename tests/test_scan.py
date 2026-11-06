"""Tests for the Gmail scanner. No network, no credentials, no live inbox.

Run:  python3 tests/test_scan.py
"""

import base64
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import gmail
import scan as scan_lib
from models import Campaign, Creator, InboxItem, ScanRun, db


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"  # in-memory
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


class ParsingTests(unittest.TestCase):
    """extract_body / parse_message against the message shapes Gmail actually returns."""

    def test_plain_text_preferred_over_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("plain version")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>html version</p>")}},
            ],
        }
        self.assertEqual(gmail.extract_body(payload), "plain version")

    def test_nested_multipart_is_walked(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "image/png", "body": {"attachmentId": "x"}},
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain",
                         "body": {"data": b64("https://www.tiktok.com/@erin/video/123")}},
                    ],
                },
            ],
        }
        self.assertIn("tiktok.com/@erin/video/123", gmail.extract_body(payload))

    def test_html_fallback_keeps_href_urls(self):
        """Creators often put the link only in the anchor and label it "my reel"."""
        html = '<div>here it is: <a href="https://www.instagram.com/reel/ABC123/">my reel</a></div>'
        payload = {"mimeType": "text/html", "body": {"data": b64(html)}}
        body = gmail.extract_body(payload)
        self.assertIn("https://www.instagram.com/reel/ABC123/", body)

    def test_html_scripts_and_styles_dropped(self):
        html = "<style>a{color:red}</style><script>var x=1</script><p>real text</p>"
        self.assertNotIn("color:red", gmail.strip_html(html))
        self.assertNotIn("var x", gmail.strip_html(html))
        self.assertIn("real text", gmail.strip_html(html))

    def test_parse_message_pulls_headers_and_date(self):
        msg = {
            "id": "18f0abc",
            "internalDate": "1762387200000",  # 2025-11-06 UTC
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Kirsten <kirstensbookshelves@gmail.com>"},
                    {"name": "Subject", "value": "my reel is up!"},
                ],
                "body": {"data": b64("https://www.instagram.com/reel/XYZ/")},
            },
        }
        parsed = gmail.parse_message(msg)
        self.assertEqual(parsed["message_id"], "18f0abc")
        self.assertEqual(parsed["sender"], "kirstensbookshelves@gmail.com")
        self.assertEqual(parsed["subject"], "my reel is up!")
        self.assertIsInstance(parsed["received_on"], date)

    def test_parse_message_survives_a_missing_date(self):
        msg = {"id": "x", "payload": {"headers": [], "body": {}}}
        self.assertEqual(gmail.parse_message(msg)["received_on"], date.today())

    def test_undecodable_part_does_not_raise(self):
        payload = {"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}
        self.assertEqual(gmail.extract_body(payload), "")


class ConfigTests(unittest.TestCase):
    KEYS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
            "GMAIL_SCAN_ENABLED", "GMAIL_SCAN_INTERVAL_HOURS")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_dormant_without_credentials(self):
        self.assertFalse(gmail.is_configured())
        self.assertFalse(scan_lib.enabled())

    def test_partial_credentials_are_not_configured(self):
        os.environ["GMAIL_CLIENT_ID"] = "id"
        os.environ["GMAIL_CLIENT_SECRET"] = "secret"
        self.assertFalse(gmail.is_configured())

    def test_blank_values_do_not_count(self):
        os.environ.update(GMAIL_CLIENT_ID="id", GMAIL_CLIENT_SECRET="s",
                          GMAIL_REFRESH_TOKEN="   ")
        self.assertFalse(gmail.is_configured())

    def test_the_deploy_placeholder_reads_as_unset(self):
        """app.yml seeds these so ${ENV.*} resolves; it must not look configured."""
        os.environ.update(GMAIL_CLIENT_ID=gmail.PLACEHOLDER,
                          GMAIL_CLIENT_SECRET=gmail.PLACEHOLDER,
                          GMAIL_REFRESH_TOKEN=gmail.PLACEHOLDER)
        self.assertFalse(gmail.is_configured())
        self.assertFalse(scan_lib.enabled())

    def test_a_real_value_alongside_placeholders_is_still_unset(self):
        os.environ.update(GMAIL_CLIENT_ID="real.apps.googleusercontent.com",
                          GMAIL_CLIENT_SECRET=gmail.PLACEHOLDER,
                          GMAIL_REFRESH_TOKEN="1//real")
        self.assertFalse(gmail.is_configured())

    def test_kill_switch_overrides_credentials(self):
        os.environ.update(GMAIL_CLIENT_ID="id", GMAIL_CLIENT_SECRET="s",
                          GMAIL_REFRESH_TOKEN="t", GMAIL_SCAN_ENABLED="0")
        self.assertTrue(gmail.is_configured())
        self.assertFalse(scan_lib.enabled())

    def test_access_token_refuses_when_unconfigured(self):
        with self.assertRaises(gmail.GmailError):
            gmail.access_token(force=True)

    def test_bad_interval_falls_back_to_the_default(self):
        os.environ["GMAIL_SCAN_INTERVAL_HOURS"] = "not-a-number"
        self.assertEqual(scan_lib.interval_hours(), scan_lib.DEFAULT_INTERVAL_HOURS)


class ScanRunTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.creator = Creator(name="Kirsten", email="kirstensbookshelves@gmail.com")
        self.campaign = Campaign(
            name="Softlaunch", slug="softlaunch", status="active",
            starts_on=date.today() - timedelta(days=5),
            ends_on=date.today() + timedelta(days=30),
        )
        db.session.add_all([self.creator, self.campaign])
        db.session.commit()

        self._real_fetch = gmail.fetch_messages

    def tearDown(self):
        gmail.fetch_messages = self._real_fetch
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def fake_fetch(self, messages):
        gmail.fetch_messages = lambda limit=None: list(messages)

    def test_successful_scan_queues_and_records(self):
        self.fake_fetch([{
            "message_id": "m1",
            "sender": "kirstensbookshelves@gmail.com",
            "subject": "reel is live",
            "body": "https://www.instagram.com/reel/ABC/",
            "received_on": date.today(),
        }])
        run = scan_lib.run_scan(trigger="manual")

        self.assertEqual(run.status, "ok")
        self.assertEqual((run.messages_seen, run.queued), (1, 1))
        self.assertIsNotNone(run.finished_at)

        item = InboxItem.query.one()
        self.assertEqual(item.creator_id, self.creator.id)
        self.assertEqual(item.status, "pending")

    def test_rescanning_the_same_message_is_a_duplicate_not_a_second_item(self):
        msg = {
            "message_id": "m1",
            "sender": "kirstensbookshelves@gmail.com",
            "subject": "reel is live",
            "body": "https://www.instagram.com/reel/ABC/",
            "received_on": date.today(),
        }
        self.fake_fetch([msg])
        scan_lib.run_scan()
        second = scan_lib.run_scan()

        self.assertEqual(second.duplicates, 1)
        self.assertEqual(second.queued, 0)
        self.assertEqual(InboxItem.query.count(), 1)

    def test_mail_without_links_is_skipped(self):
        self.fake_fetch([{
            "message_id": "m2", "sender": "someone@example.com",
            "subject": "quick question", "body": "when do I get paid?",
            "received_on": date.today(),
        }])
        run = scan_lib.run_scan()
        self.assertEqual((run.skipped, run.queued), (1, 0))
        self.assertEqual(InboxItem.query.count(), 0)

    def test_platform_notification_mail_is_skipped(self):
        """TikTok's "X posted:" mails are full of real links and never submissions."""
        self.fake_fetch([{
            "message_id": "m3", "sender": "noreply@service.tiktok.com",
            "subject": "kirsten posted a video",
            "body": "https://www.tiktok.com/@kirsten/video/999",
            "received_on": date.today(),
        }])
        run = scan_lib.run_scan()
        self.assertEqual(run.queued, 0)
        self.assertEqual(InboxItem.query.count(), 0)

    def test_gmail_failure_is_recorded_not_raised(self):
        def boom(limit=None):
            raise gmail.GmailError("HTTP 401 from /token: invalid_grant")
        gmail.fetch_messages = boom

        run = scan_lib.run_scan()
        self.assertEqual(run.status, "error")
        self.assertIn("invalid_grant", run.error)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(ScanRun.query.count(), 1)

    def test_unexpected_exception_is_also_recorded(self):
        def boom(limit=None):
            raise KeyError("payload")
        gmail.fetch_messages = boom

        run = scan_lib.run_scan()
        self.assertEqual(run.status, "error")
        self.assertIn("KeyError", run.error)

    def test_is_due_respects_the_interval(self):
        self.assertTrue(scan_lib.is_due())  # never run

        self.fake_fetch([])
        scan_lib.run_scan()
        self.assertFalse(scan_lib.is_due())

        run = ScanRun.query.one()
        run.started_at = scan_lib.utcnow() - timedelta(hours=scan_lib.interval_hours() + 1)
        db.session.commit()
        self.assertTrue(scan_lib.is_due())

    def test_a_running_scan_blocks_another(self):
        db.session.add(ScanRun(started_at=scan_lib.utcnow(), status="running"))
        db.session.commit()
        self.assertFalse(scan_lib.is_due())

    def test_an_interrupted_run_is_reaped(self):
        db.session.add(ScanRun(
            started_at=scan_lib.utcnow() - (scan_lib.STALE_AFTER + timedelta(minutes=5)),
            status="running",
        ))
        db.session.commit()

        scan_lib.is_due()
        reaped = ScanRun.query.one()
        self.assertEqual(reaped.status, "error")
        self.assertIn("interrupted", reaped.error)

    def test_a_failed_run_retries_on_the_shorter_clock(self):
        """A Google blip or a killed worker must not cost a whole day of mail."""
        failed = ScanRun(started_at=scan_lib.utcnow() - timedelta(minutes=10),
                         status="error", error="HTTP 503")
        db.session.add(failed)
        db.session.commit()
        self.assertFalse(scan_lib.is_due())  # not instantly

        failed.started_at = scan_lib.utcnow() - (scan_lib.RETRY_AFTER + timedelta(minutes=1))
        db.session.commit()
        self.assertTrue(scan_lib.is_due())   # but long before 24h

    def test_a_successful_run_waits_the_full_interval(self):
        db.session.add(ScanRun(
            started_at=scan_lib.utcnow() - (scan_lib.RETRY_AFTER + timedelta(minutes=1)),
            status="ok", finished_at=scan_lib.utcnow(),
        ))
        db.session.commit()
        self.assertFalse(scan_lib.is_due())

    def test_status_reports_the_latest_run_first(self):
        self.fake_fetch([])
        scan_lib.run_scan(trigger="manual")
        st = scan_lib.status()
        self.assertEqual(st["last"].trigger, "manual")
        self.assertEqual(len(st["recent"]), 1)
        self.assertFalse(st["configured"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
