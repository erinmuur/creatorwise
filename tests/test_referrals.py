"""Referral attribution: per creator, per campaign, over a stated window.

The thing these guard is that "how many referrals" is meaningless without
"between when and when", and that a creator who posted nothing can still have
earned some — the real export has one who earned 25 with no content at all.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import (
    REPORTED_WINDOW,
    Campaign,
    CampaignReferral,
    Creator,
    CreatorEmail,
    Participation,
    PieceCollaborator,
    PlatformPost,
    ReferralSnapshot,
    UgcPiece,
    db,
)
from tools import refresh_referrals as refresh

WINDOW = (date(2026, 7, 15), date(2026, 10, 1))


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


class ReferralTotalTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.campaign = Campaign(name="Softlaunch", slug="soft")
        db.session.add(self.campaign)
        db.session.flush()

        self.people = {}
        for name, email in (
            ("Bea Lune", "bea@example.com"),
            ("Rune Pryor", "rune@example.com"),     # referrals, no content
            ("Wren Ashby", "wren@example.com"),     # content, no referrals
        ):
            creator = Creator(name=name, email=email)
            db.session.add(creator)
            db.session.flush()
            db.session.add(CreatorEmail(
                creator_id=creator.id, address=email, is_primary=True
            ))
            db.session.add(Participation(
                creator_id=creator.id, campaign_id=self.campaign.id,
                status="content_delivered" if name != "Rune Pryor" else "briefed",
            ))
            self.people[name] = creator

        for name in ("Bea Lune", "Wren Ashby"):
            piece = UgcPiece(creator_id=self.people[name].id,
                             campaign_id=self.campaign.id)
            db.session.add(piece)
            db.session.flush()
            db.session.add(PlatformPost(
                ugc_piece_id=piece.id, platform="tiktok",
                url=f"https://www.tiktok.com/@{name.split()[0].lower()}/video/1",
            ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def credit(self, name, referrals):
        db.session.add(CampaignReferral(
            creator_id=self.people[name].id, campaign_id=self.campaign.id,
            referrals=referrals, window_from=WINDOW[0], window_to=WINDOW[1],
        ))
        db.session.commit()

    def test_a_creator_with_no_content_can_still_have_referrals(self):
        """The case the per-piece model could not hold at all."""
        self.credit("Rune Pryor", 25)
        rune = self.people["Rune Pryor"]
        self.assertEqual(len(rune.ugc), 0)
        self.assertEqual(rune.total_referrals, 25)
        self.assertEqual(self.campaign.total_referrals, 25)

    def test_the_campaign_total_is_the_sum_of_its_creators(self):
        self.credit("Bea Lune", 63)
        self.credit("Rune Pryor", 25)
        self.assertEqual(self.campaign.total_referrals, 88)
        self.assertEqual(self.campaign.credited_count, 2)

    def test_the_window_is_carried_with_the_number(self):
        self.credit("Bea Lune", 63)
        self.assertEqual(self.campaign.referral_window, WINDOW)
        self.assertEqual(self.campaign.referral_window_label,
                         "Jul 15 – Oct 1, 2026")
        self.assertFalse(self.campaign.referrals_are_reported)

    def test_disagreeing_windows_report_no_window_rather_than_one_of_them(self):
        self.credit("Bea Lune", 63)
        db.session.add(CampaignReferral(
            creator_id=self.people["Rune Pryor"].id, campaign_id=self.campaign.id,
            referrals=25, window_from=date(2026, 1, 1), window_to=date(2026, 2, 1),
        ))
        db.session.commit()
        self.assertIsNone(self.campaign.referral_window)
        self.assertIsNone(self.campaign.referral_window_label)

    def test_per_creator_totals_win_over_leftover_per_piece_numbers(self):
        """Both present would otherwise double-count the same signups."""
        piece = self.people["Bea Lune"].ugc[0]
        db.session.add(ReferralSnapshot(
            ugc_piece_id=piece.id, window_days=REPORTED_WINDOW, referrals=999
        ))
        db.session.commit()
        self.credit("Bea Lune", 63)
        self.assertEqual(self.campaign.total_referrals, 63)
        self.assertEqual(self.people["Bea Lune"].total_referrals, 63)

    def test_without_per_creator_totals_it_still_falls_back_to_pieces(self):
        piece = self.people["Bea Lune"].ugc[0]
        db.session.add(ReferralSnapshot(
            ugc_piece_id=piece.id, window_days=REPORTED_WINDOW, referrals=12
        ))
        db.session.commit()
        self.assertEqual(self.campaign.total_referrals, 12)
        self.assertTrue(self.campaign.referrals_are_reported)

    def test_participation_reports_its_own_creators_number(self):
        self.credit("Bea Lune", 63)
        parts = {p.creator.name: p for p in self.campaign.participations}
        self.assertEqual(parts["Bea Lune"].referrals, 63)
        self.assertIsNone(parts["Wren Ashby"].referrals)

    # --- what a video page shows ------------------------------------------
    # A video has no referral number of its own. It shows its creator's
    # figure for the campaign, which used to read as 0 everywhere once the
    # per-piece sheet numbers were cleared.

    def piece_of(self, name):
        return self.people[name].ugc[0]

    def test_a_video_shows_its_creators_campaign_figure(self):
        self.credit("Bea Lune", 63)
        self.assertEqual(self.piece_of("Bea Lune").creator_referrals, 63)
        self.assertEqual(self.piece_of("Bea Lune").referrals, 0)  # still none of its own

    def test_a_measured_campaign_with_nothing_for_this_creator_is_a_real_zero(self):
        self.credit("Bea Lune", 63)
        self.assertEqual(self.piece_of("Wren Ashby").creator_referrals, 0)

    def test_an_unmeasured_campaign_shows_nothing_rather_than_zero(self):
        self.assertIsNone(self.piece_of("Bea Lune").creator_referrals)

    def test_every_video_by_one_creator_carries_the_same_figure_not_a_share(self):
        bea = self.people["Bea Lune"]
        second = UgcPiece(creator_id=bea.id, campaign_id=self.campaign.id)
        db.session.add(second)
        db.session.commit()
        self.credit("Bea Lune", 63)
        self.assertEqual([p.creator_referrals for p in bea.ugc], [63, 63])
        self.assertEqual(self.campaign.total_referrals, 63)  # counted once

    def test_a_shared_video_carries_everyone_on_it(self):
        db.session.add(PieceCollaborator(
            ugc_piece_id=self.piece_of("Bea Lune").id,
            creator_id=self.people["Rune Pryor"].id,
        ))
        db.session.commit()
        self.credit("Bea Lune", 63)
        self.credit("Rune Pryor", 25)
        self.assertEqual(self.piece_of("Bea Lune").creator_referrals, 88)

    def test_a_figure_from_another_campaign_is_not_borrowed(self):
        other = Campaign(name="Announcement", slug="ann")
        db.session.add(other)
        db.session.flush()
        db.session.add(CampaignReferral(
            creator_id=self.people["Bea Lune"].id, campaign_id=other.id,
            referrals=500, window_from=WINDOW[0], window_to=WINDOW[1],
        ))
        self.credit("Wren Ashby", 3)
        self.assertEqual(self.piece_of("Bea Lune").creator_referrals, 0)

    def test_a_running_campaign_says_its_number_is_so_far(self):
        self.credit("Bea Lune", 63)
        self.campaign.ends_on = date(2026, 12, 1)
        self.assertTrue(self.campaign.referrals_still_counting)
        self.campaign.ends_on = WINDOW[1]
        self.assertFalse(self.campaign.referrals_still_counting)

    def test_an_ongoing_campaign_is_always_so_far(self):
        """No end date means it never closes, so no count is final."""
        self.credit("Bea Lune", 63)
        self.campaign.ends_on = None
        self.assertTrue(self.campaign.referrals_still_counting)


class RefreshToolTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.campaign = Campaign(name="Softlaunch", slug="soft")
        other = Campaign(name="Other", slug="other")
        db.session.add_all([self.campaign, other])
        db.session.flush()

        self.bea = self._creator("Bea Lune", "bea@example.com",
                                 extra="bea.lune@example.net", campaign=self.campaign)
        self.rune = self._creator("Rune Pryor", "rune@example.com",
                                  campaign=self.campaign)
        self.outsider = self._creator("Wren Ashby", "wren@example.com",
                                      campaign=other)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _creator(self, name, email, extra=None, campaign=None):
        creator = Creator(name=name, email=email)
        db.session.add(creator)
        db.session.flush()
        db.session.add(CreatorEmail(
            creator_id=creator.id, address=email, is_primary=True
        ))
        if extra:
            db.session.add(CreatorEmail(creator_id=creator.id, address=extra))
        if campaign is not None:
            db.session.add(Participation(
                creator_id=creator.id, campaign_id=campaign.id, status="briefed"
            ))
        db.session.flush()
        return creator

    def plan(self, counts):
        return refresh.plan(self.campaign, counts, *WINDOW)

    def test_a_straightforward_load(self):
        result = self.plan({"bea@example.com": 63, "rune@example.com": 25})
        self.assertEqual(len(result["new"]), 2)
        refresh.apply(self.campaign, result, source="test")
        self.assertEqual(self.campaign.total_referrals, 88)

    def test_a_second_address_finds_the_same_creator(self):
        result = self.plan({"bea.lune@example.net": 63})
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0][1], "Bea Lune")

    def test_two_addresses_for_one_creator_are_added_together(self):
        """Both are her invite codes; they are one person's referrals."""
        result = self.plan({"bea@example.com": 40, "bea.lune@example.net": 23})
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0][2], 63)
        refresh.apply(self.campaign, result, source="test")
        self.assertEqual(self.bea.total_referrals, 63)

    def test_somebody_not_in_this_campaign_is_skipped_and_named(self):
        result = self.plan({"wren@example.com": 99})
        self.assertEqual(len(result["outsiders"]), 1)
        self.assertEqual(result["outsiders"][0][0], "Wren Ashby")
        refresh.apply(self.campaign, result, source="test")
        self.assertEqual(self.campaign.total_referrals, 0)
        self.assertIn("NOT IN THIS CAMPAIGN", refresh.render(self.campaign, result))

    def test_a_shared_address_credits_nobody(self):
        """Two creators, one mailbox: the source doesn't say whose link it
        was, so splitting or picking one would be invention."""
        other = self._creator("Juno Quill", "juno@example.com",
                              campaign=self.campaign)
        db.session.add(CreatorEmail(
            creator_id=other.id, address="bea@example.com"
        ))
        db.session.commit()

        result = self.plan({"bea@example.com": 40})
        self.assertEqual(len(result["shared"]), 1)
        address, referrals, names = result["shared"][0]
        self.assertEqual((address, referrals), ("bea@example.com", 40))
        self.assertEqual(names, ["Bea Lune", "Juno Quill"])
        self.assertEqual(result["new"], [])

        refresh.apply(self.campaign, result, source="test")
        self.assertEqual(self.campaign.total_referrals, 0)
        self.assertIn("SHARED ADDRESS", refresh.render(self.campaign, result))

    def test_an_unknown_address_is_reported_not_guessed(self):
        result = self.plan({"nobody@example.org": 7})
        self.assertEqual(result["unknown"], [("nobody@example.org", 7)])
        self.assertIn("NO CREATOR WITH THAT ADDRESS",
                      refresh.render(self.campaign, result))

    def test_re_running_updates_in_place(self):
        refresh.apply(self.campaign, self.plan({"bea@example.com": 63}),
                      source="test")
        again = self.plan({"bea@example.com": 70})
        self.assertEqual(len(again["changed"]), 1)
        self.assertEqual(again["changed"][0][2:], [70, 63])
        refresh.apply(self.campaign, again, source="test")
        self.assertEqual(CampaignReferral.query.count(), 1)
        self.assertEqual(self.campaign.total_referrals, 70)

    def test_an_unchanged_number_is_reported_as_unchanged(self):
        refresh.apply(self.campaign, self.plan({"bea@example.com": 63}),
                      source="test")
        again = self.plan({"bea@example.com": 63})
        self.assertEqual(len(again["same"]), 1)
        self.assertEqual(again["changed"], [])

    def test_a_creator_absent_from_a_later_file_drops_to_zero(self):
        """Not silently left at their old number, which would look current."""
        refresh.apply(self.campaign, self.plan({"bea@example.com": 63}),
                      source="test")
        again = self.plan({"rune@example.com": 25})
        self.assertEqual([r[1] for r in again["zeroed"]], ["Bea Lune"])
        refresh.apply(self.campaign, again, source="test")
        self.assertEqual(self.bea.total_referrals, 0)
        self.assertEqual(self.campaign.total_referrals, 25)

    def test_the_window_lands_on_every_row(self):
        refresh.apply(self.campaign, self.plan({"bea@example.com": 63}),
                      source="bookwise invite redemptions")
        row = CampaignReferral.query.one()
        self.assertEqual((row.window_from, row.window_to), WINDOW)
        self.assertEqual(row.measured_at, date.today())
        self.assertEqual(row.source, "bookwise invite redemptions")

    def test_stale_per_piece_sheet_numbers_are_cleared(self):
        piece = UgcPiece(creator_id=self.bea.id, campaign_id=self.campaign.id)
        db.session.add(piece)
        db.session.flush()
        db.session.add(ReferralSnapshot(
            ugc_piece_id=piece.id, window_days=REPORTED_WINDOW, referrals=56
        ))
        db.session.commit()

        summary = refresh.apply(self.campaign, self.plan({"bea@example.com": 63}),
                                source="test")
        self.assertEqual(summary["dropped_reported"], 1)
        self.assertEqual(ReferralSnapshot.query.count(), 0)

    def test_keeping_them_is_possible_but_not_the_default(self):
        piece = UgcPiece(creator_id=self.bea.id, campaign_id=self.campaign.id)
        db.session.add(piece)
        db.session.flush()
        db.session.add(ReferralSnapshot(
            ugc_piece_id=piece.id, window_days=REPORTED_WINDOW, referrals=56
        ))
        db.session.commit()
        refresh.apply(self.campaign, self.plan({"bea@example.com": 63}),
                      source="test", drop_reported=False)
        self.assertEqual(ReferralSnapshot.query.count(), 1)
        # The per-creator total still wins where they are both present.
        self.assertEqual(self.campaign.total_referrals, 63)

    def test_the_report_states_the_window_it_used(self):
        text = refresh.render(self.campaign, self.plan({"bea@example.com": 63}))
        self.assertIn("2026-07-15 to 2026-10-01 inclusive", text)
        self.assertIn("63 referrals in total", text)

    def test_the_cli_dry_runs_by_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"bea@example.com": 63}, handle)
            path = handle.name
        try:
            result = refresh.plan(self.campaign,
                                  json.load(open(path)), *WINDOW)
            self.assertEqual(len(result["new"]), 1)
            self.assertEqual(CampaignReferral.query.count(), 0)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
