"""One video, two creators: counted once in the campaign, shown on both."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import (
    Campaign,
    Creator,
    Participation,
    PieceCollaborator,
    PlatformPost,
    UgcPiece,
    db,
)


class CollaboratorTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.campaign = Campaign(name="Softlaunch", slug="soft")
        self.pax = Creator(name="Pax Quill", email="pax@example.com")
        self.juno = Creator(name="Juno Quill", email="juno@example.com")
        self.solo = Creator(name="Bea Lune", email="bea@example.com")
        db.session.add_all([self.campaign, self.pax, self.juno, self.solo])
        db.session.flush()

        self.piece = UgcPiece(creator_id=self.pax.id, campaign_id=self.campaign.id)
        db.session.add(self.piece)
        db.session.flush()
        from datetime import date
        db.session.add(PlatformPost(ugc_piece_id=self.piece.id, platform="tiktok",
                                    likes=40, comments=10, measured_at=date(2026, 11, 20)))
        db.session.add(PieceCollaborator(ugc_piece_id=self.piece.id,
                                         creator_id=self.juno.id))
        for creator in (self.pax, self.juno):
            db.session.add(Participation(creator_id=creator.id,
                                         campaign_id=self.campaign.id,
                                         status="content_delivered"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_the_piece_lists_everyone_lead_first(self):
        self.assertEqual([c.name for c in self.piece.creators],
                         ["Pax Quill", "Juno Quill"])
        self.assertEqual(self.piece.byline, "Pax Quill & Juno Quill")

    def test_a_solo_piece_has_a_one_name_byline(self):
        piece = UgcPiece(creator_id=self.solo.id, campaign_id=self.campaign.id)
        db.session.add(piece)
        db.session.commit()
        self.assertEqual(piece.byline, "Bea Lune")

    def test_it_shows_up_for_both_creators(self):
        self.assertEqual(self.pax.all_pieces, [self.piece])
        self.assertEqual(self.juno.all_pieces, [self.piece])
        self.assertEqual(self.juno.ugc, [])   # she is not the lead

    def test_the_campaign_counts_the_video_once(self):
        """Two copies would double its views and likes in every total."""
        self.assertEqual(len(self.campaign.ugc), 1)
        self.assertEqual(self.campaign.total_engagement, 50)
        self.assertEqual(self.campaign.delivered_count, 2)

    def test_each_creator_sees_the_engagement_on_their_own_page(self):
        self.assertEqual(self.pax.total_engagement, 50)
        self.assertEqual(self.juno.total_engagement, 50)

    def test_a_collaborators_participation_lists_the_piece(self):
        parts = {p.creator.name: p for p in self.campaign.participations}
        self.assertEqual(parts["Juno Quill"].pieces, [self.piece])

    def test_nobody_is_listed_twice(self):
        db.session.add(PieceCollaborator(ugc_piece_id=self.piece.id,
                                         creator_id=self.pax.id))
        db.session.commit()
        self.assertEqual(len(self.piece.creators), 2)
        self.assertEqual(len(self.pax.all_pieces), 1)

    def test_the_same_person_cannot_be_added_to_a_piece_twice(self):
        db.session.add(PieceCollaborator(ugc_piece_id=self.piece.id,
                                         creator_id=self.juno.id))
        with self.assertRaises(Exception):
            db.session.commit()
        db.session.rollback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
