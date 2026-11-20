"""Engagement readings (tools/refresh_engagement.py). Invented data only."""

import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = tempfile.mkdtemp()
os.environ.update(
    DASHBOARD_PASSWORD="test-password", SECRET_KEY="test-secret",
    INGEST_TOKEN="t", SEED_SAMPLE_DATA="0",
    DATABASE_URL="sqlite:///" + os.path.join(DATA, "creatorwise.db"),
)

import app as appmod  # noqa: E402
from models import Campaign, Creator, PlatformPost, PostSnapshot, UgcPiece, db  # noqa: E402
from tools import refresh_engagement as re_  # noqa: E402


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.ctx = appmod.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        camp = Campaign(name="Test Campaign", slug="test")
        c = Creator(name="Bea Lune", email="bea@example.com")
        db.session.add_all([camp, c])
        db.session.flush()
        self.piece = UgcPiece(creator_id=c.id, campaign_id=camp.id)
        db.session.add(self.piece)
        db.session.flush()
        self.ig = PlatformPost(ugc_piece_id=self.piece.id, platform="instagram", url="https://example.com/p/1")
        self.tt = PlatformPost(ugc_piece_id=self.piece.id, platform="tiktok", url="https://example.com/v/2")
        db.session.add_all([self.ig, self.tt])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_counts_parse(self):
        self.assertEqual([re_.parse_count(v) for v in ("1,234", "12.5K", 7, "", "Share")],
                         [1234, 12500, 7, None, None])

    def test_a_status_reading_is_noted_not_skipped(self):
        result = re_.plan({str(self.ig.id): {"status": "likes_hidden"},
                           str(self.tt.id): {"likes": "5", "comments": "1", "views": "100"}},
                          {self.ig.id: self.ig, self.tt.id: self.tt})
        self.assertEqual([(p.id, v is None, why) for p, v, why in result],
                         [(self.ig.id, True, "likes_hidden"), (self.tt.id, False, None)])

    def test_hidden_likes_are_named_not_zero(self):
        db.session.add(PostSnapshot(post_id=self.ig.id, measured_at=date(2026, 11, 20),
                                    status="likes_hidden"))
        db.session.commit()
        self.assertTrue(self.ig.likes_hidden)
        self.assertIsNone(self.ig.measured_at)            # still unmeasured
        self.assertTrue(self.piece.likes_hidden)          # nothing else on the piece counted
        self.tt.likes, self.tt.measured_at = 4, date(2026, 11, 20)
        self.assertFalse(self.piece.likes_hidden)         # the TikTok's count stands
        self.assertEqual(self.piece.likes, 4)

    def test_a_later_reading_with_numbers_clears_the_status(self):
        db.session.add_all([
            PostSnapshot(post_id=self.ig.id, measured_at=date(2026, 11, 20), status="unavailable"),
            PostSnapshot(post_id=self.ig.id, measured_at=date(2026, 11, 27), likes=3)])
        db.session.commit()
        self.assertIsNone(self.ig.read_status)
        self.assertFalse(self.piece.unavailable)


if __name__ == "__main__":
    unittest.main()
