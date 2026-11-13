"""Follower counts: where they come from is recorded, and unmeasured is not zero.

No network — the YouTube lookups run against an injected fetcher — and every
name and handle is invented.
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import Creator, FollowerSnapshot, SocialHandle, db
from tools import fetch_youtube_followers as youtube
from tools import refresh_followers as tool


class Base(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.bea = self._creator("Bea Lune", "bea@example.com", [
            ("tiktok", "@BeaReads", 1200), ("instagram", "@bea.lune", None),
        ])
        self.pax = self._creator("Pax Quill", "pax@example.com", [
            ("youtube", "@duochannel", None),
        ])
        self.juno = self._creator("Juno Quill", "juno@example.com", [
            ("youtube", "@duochannel", None),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _creator(self, name, email, handles):
        creator = Creator(name=name, email=email)
        db.session.add(creator)
        db.session.flush()
        for platform, handle, count in handles:
            db.session.add(SocialHandle(creator_id=creator.id, platform=platform,
                                        handle=handle, follower_count=count))
        db.session.flush()
        return creator


class PerPlatformTests(Base):
    def test_unmeasured_is_none_not_zero(self):
        self.assertEqual(self.bea.followers_on("tiktok"), 1200)
        self.assertIsNone(self.bea.followers_on("instagram"))   # on it, unmeasured
        self.assertIsNone(self.bea.followers_on("youtube"))     # not on it at all
        self.assertEqual(len(self.bea.handles_on("instagram")), 1)
        self.assertEqual(self.bea.handles_on("youtube"), [])

    def test_a_measured_zero_is_still_a_measurement(self):
        self.bea.handles_on("instagram")[0].follower_count = 0
        self.assertEqual(self.bea.followers_on("instagram"), 0)


class LoadingTests(Base):
    def load(self, rows, **kwargs):
        result = tool.plan(rows)
        tool.apply(result, kwargs.pop("source", "test"), **kwargs)
        return result

    def test_a_first_reading_and_a_changed_one(self):
        result = tool.plan([
            {"platform": "tiktok", "handle": "beareads", "followers": "1,350"},
            {"platform": "instagram", "handle": "@bea.lune", "followers": "12.4K"},
        ])
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0][3:], (1200, 1350))
        self.assertEqual(len(result["first"]), 1)
        tool.apply(result, "test")
        self.assertEqual(self.bea.followers_on("tiktok"), 1350)
        self.assertEqual(self.bea.followers_on("instagram"), 12400)

    def test_every_reading_leaves_a_dated_sourced_snapshot(self):
        self.load([{"platform": "tiktok", "handle": "beareads", "followers": 1350}],
                  source="vendor export", measured_at=date(2026, 11, 10))
        snapshot = FollowerSnapshot.query.one()
        self.assertEqual((snapshot.follower_count, snapshot.status), (1350, "ok"))
        self.assertEqual(snapshot.source, "vendor export")
        handle = self.bea.handles_on("tiktok")[0]
        self.assertEqual(handle.followers_checked_on, date(2026, 11, 10))

    def test_an_unchanged_count_is_still_recorded_as_checked(self):
        """Confirming a number is information too."""
        result = self.load([{"platform": "tiktok", "handle": "beareads",
                             "followers": 1200}])
        self.assertEqual(len(result["same"]), 1)
        self.assertEqual(FollowerSnapshot.query.count(), 1)

    def test_handles_match_whatever_the_case_or_the_at_sign(self):
        for spelling in ("BeaReads", "@beareads", " BEAREADS "):
            with self.subTest(spelling=spelling):
                result = tool.plan([{"platform": "tiktok", "handle": spelling,
                                     "followers": 5}])
                self.assertEqual(len(result["changed"]), 1)

    def test_one_account_on_two_profiles_updates_both(self):
        """A duo share a channel: one audience, shown on both of them."""
        self.load([{"platform": "youtube", "handle": "duochannel", "followers": 900}])
        self.assertEqual(self.pax.followers_on("youtube"), 900)
        self.assertEqual(self.juno.followers_on("youtube"), 900)
        self.assertEqual(FollowerSnapshot.query.count(), 2)

    def test_an_account_that_is_gone_is_recorded_without_wiping_the_count(self):
        self.load([{"platform": "tiktok", "handle": "beareads",
                    "status": "not_found"}])
        handle = self.bea.handles_on("tiktok")[0]
        self.assertTrue(handle.is_unresolved)
        self.assertEqual(handle.follower_count, 1200)
        self.assertIsNone(FollowerSnapshot.query.one().follower_count)

    def test_a_later_good_reading_clears_not_found(self):
        self.load([{"platform": "tiktok", "handle": "beareads", "status": "not_found"}],
                  measured_at=date(2026, 11, 1))
        self.load([{"platform": "tiktok", "handle": "beareads", "followers": 1500}],
                  measured_at=date(2026, 11, 10))
        self.assertFalse(self.bea.handles_on("tiktok")[0].is_unresolved)

    def test_a_handle_we_do_not_hold_is_reported_not_created(self):
        result = tool.plan([{"platform": "tiktok", "handle": "stranger",
                             "followers": 9}])
        self.assertEqual(result["unknown"], [("tiktok", "stranger")])
        self.assertIn("NO SUCH HANDLE ON FILE", tool.render(result))

    def test_the_same_handle_on_another_platform_is_not_a_match(self):
        result = tool.plan([{"platform": "instagram", "handle": "beareads",
                             "followers": 9}])
        self.assertEqual(result["unknown"], [("instagram", "beareads")])

    def test_junk_is_reported_not_stored_as_zero(self):
        result = tool.plan([
            {"platform": "tiktok", "handle": "beareads", "followers": "lots"},
            {"platform": "", "handle": "x", "followers": 1},
        ])
        self.assertEqual(len(result["unreadable"]), 2)
        tool.apply(result, "test")
        self.assertEqual(self.bea.followers_on("tiktok"), 1200)
        self.assertEqual(FollowerSnapshot.query.count(), 0)

    def test_readings_can_be_csv_or_json(self):
        csv_rows = tool.read_readings("Platform,Handle,Followers\ntiktok,beareads,5\n")
        json_rows = tool.read_readings(
            '[{"platform": "tiktok", "handle": "beareads", "followers": 5}]')
        self.assertEqual(csv_rows[0]["handle"], json_rows[0]["handle"])
        self.assertEqual(tool.read_readings(""), [])

    def test_the_worklist_lists_each_account_once(self):
        rows = tool.worklist()
        keys = [(r["platform"], r["handle"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("youtube", "duochannel"), keys)
        self.assertEqual(len(tool.worklist("youtube")), 1)


class YouTubeTests(unittest.TestCase):
    def answer(self, payload):
        self.urls = []

        def fetch(url):
            self.urls.append(url)
            return payload
        return fetch

    def test_a_handle_is_looked_up_by_handle(self):
        fetch = self.answer({"items": [{"statistics": {"subscriberCount": "4210"}}]})
        reading, why = youtube.lookup("beareads", "KEY", fetch=fetch)
        self.assertEqual(reading, {"platform": "youtube", "handle": "beareads",
                                   "followers": 4210})
        self.assertIsNone(why)
        self.assertIn("forHandle=%40beareads", self.urls[0])

    def test_a_legacy_channel_id_is_looked_up_by_id(self):
        fetch = self.answer({"items": [{"statistics": {"subscriberCount": "7"}}]})
        youtube.lookup("UCa1b2c3d4e5f6g7h8i9j0kL", "KEY", fetch=fetch)
        self.assertIn("id=UCa1b2c3d4e5f6g7h8i9j0kL", self.urls[0])
        self.assertNotIn("forHandle", self.urls[0])

    def test_no_such_channel_is_a_not_found_reading(self):
        reading, _ = youtube.lookup("gone", "KEY", fetch=self.answer({"items": []}))
        self.assertEqual(reading["status"], "not_found")

    def test_a_hidden_count_is_skipped_not_recorded_as_zero(self):
        fetch = self.answer({"items": [{"statistics": {
            "hiddenSubscriberCount": True, "subscriberCount": "0"}}]})
        reading, why = youtube.lookup("shy", "KEY", fetch=fetch)
        self.assertIsNone(reading)
        self.assertIn("hidden", why)

    def test_one_failure_does_not_lose_the_rest(self):
        def fetch(url):
            if "broken" in url:
                raise OSError("HTTP 500")
            return {"items": [{"statistics": {"subscriberCount": "10"}}]}

        readings, skipped = youtube.collect(["ok1", "broken", "ok2"], "KEY", fetch=fetch)
        self.assertEqual([r["handle"] for r in readings], ["ok1", "ok2"])
        self.assertEqual(skipped, [("broken", "HTTP 500")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
