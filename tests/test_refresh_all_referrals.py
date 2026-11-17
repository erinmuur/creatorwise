"""The daily all-campaigns referral refresh.

The result file crosses a hand-off (a person or a model copies it out of the
query tool), so the guards here are about refusing a file that was copied
wrong, and about every campaign getting its own window.
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import Campaign, CampaignReferral, Creator, CreatorEmail, Participation, db
from tools import refresh_all_referrals as refresh_all

TODAY = date(2026, 11, 17)


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


class RefreshAllTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.done = Campaign(name="Done", slug="done",
                             starts_on=date(2026, 7, 1), ends_on=date(2026, 10, 1))
        self.running = Campaign(name="Running", slug="running",
                                starts_on=date(2026, 10, 28), ends_on=date(2026, 12, 1))
        self.ongoing = Campaign(name="Ongoing", slug="ongoing",
                                starts_on=date(2026, 10, 28))
        self.undated = Campaign(name="Undated", slug="undated")
        db.session.add_all([self.done, self.running, self.ongoing, self.undated])
        db.session.flush()

        self.bea = Creator(name="Bea Lune", email="bea@example.com")
        self.rune = Creator(name="Rune Pryor", email="rune@example.com")
        db.session.add_all([self.bea, self.rune])
        db.session.flush()
        for creator in (self.bea, self.rune):
            db.session.add(CreatorEmail(creator_id=creator.id,
                                        address=creator.email, is_primary=True))
            for campaign in (self.done, self.running):
                db.session.add(Participation(creator_id=creator.id,
                                             campaign_id=campaign.id,
                                             status="content_delivered"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def counted(self):
        return refresh_all.windows(
            Campaign.query.order_by(Campaign.id).all(), TODAY)

    def result(self, rows, total=None, n=None):
        """A query result shaped like query_readwise's, with a ROLLUP total."""
        counted, _ = self.counted()
        names = [refresh_all.column_name(c.id, f, t) for c, f, t in counted]
        body = [[email] + [1] + list(values) for email, values in rows]
        if total is None:
            total = [sum(v[i] for _e, v in rows) for i in range(len(names))]
        body.insert(0, [None, len(rows) if n is None else n] + list(total))
        return {"columns": ["email", "n"] + names, "rows": body}

    # -- windows ---------------------------------------------------------

    def test_each_campaign_is_counted_over_its_own_window(self):
        counted, skipped = self.counted()
        windows = {c.slug: (f, t) for c, f, t in counted}
        self.assertEqual(windows["done"], (date(2026, 7, 1), date(2026, 10, 1)))
        self.assertEqual(windows["running"], (date(2026, 10, 28), TODAY))
        self.assertEqual(windows["ongoing"], (date(2026, 10, 28), TODAY))
        self.assertEqual([c.slug for c, _r in skipped], ["undated"])

    def test_a_finished_campaign_stops_at_its_end_date(self):
        later = date(2027, 1, 15)
        counted, _ = refresh_all.windows([self.running], later)
        self.assertEqual(counted[0][2], date(2026, 12, 1))

    def test_the_query_asks_only_about_addresses_on_file(self):
        counted, _ = self.counted()
        sql = refresh_all.build_sql(counted, refresh_all.addresses_on_file())
        self.assertIn("'bea@example.com','rune@example.com'", sql)
        self.assertIn("ROLLUP", sql)
        # The window's last day is inclusive: counted up to the next midnight.
        self.assertIn("x.redeemed_at < DATE '2026-11-18'", sql)
        self.assertIn("x.redeemed_at < DATE '2026-10-02'", sql)

    def test_an_address_with_a_quote_cannot_break_the_query(self):
        counted, _ = self.counted()
        sql = refresh_all.build_sql(counted, ["o'neil@example.com"])
        self.assertIn("'o''neil@example.com'", sql)

    # -- the checksum ----------------------------------------------------

    def test_a_good_file_splits_into_one_count_per_campaign(self):
        data = self.result([("bea@example.com", (5, 7, 0)),
                            ("rune@example.com", (0, 2, 1))])
        parsed = refresh_all.parse_result(data, refresh_all.addresses_on_file())
        self.assertEqual(parsed[self.done.id][2], {"bea@example.com": 5})
        self.assertEqual(parsed[self.running.id][2],
                         {"bea@example.com": 7, "rune@example.com": 2})
        self.assertEqual(parsed[self.running.id][1], TODAY)

    def test_a_dropped_row_is_refused(self):
        data = self.result([("bea@example.com", (5, 7, 0)),
                            ("rune@example.com", (0, 2, 1))])
        del data["rows"][2]
        with self.assertRaises(refresh_all.RefusedFile):
            refresh_all.parse_result(data, refresh_all.addresses_on_file())

    def test_a_miscopied_number_is_refused(self):
        data = self.result([("bea@example.com", (5, 7, 0)),
                            ("rune@example.com", (0, 2, 1))])
        data["rows"][1][3] = 8      # bea's "running" count, 7 -> 8
        with self.assertRaises(refresh_all.RefusedFile):
            refresh_all.parse_result(data, refresh_all.addresses_on_file())

    def test_a_miscopied_address_is_refused(self):
        data = self.result([("bea@exampel.com", (5, 7, 0))])
        with self.assertRaises(refresh_all.RefusedFile):
            refresh_all.parse_result(data, refresh_all.addresses_on_file())

    def test_a_file_with_no_total_row_is_refused(self):
        data = self.result([("bea@example.com", (5, 7, 0))])
        del data["rows"][0]
        with self.assertRaises(refresh_all.RefusedFile):
            refresh_all.parse_result(data, refresh_all.addresses_on_file())

    def test_rows_as_objects_are_accepted_too(self):
        data = self.result([("bea@example.com", (5, 7, 0))])
        data["rows"] = [dict(zip(data["columns"], r)) for r in data["rows"]]
        parsed = refresh_all.parse_result(data, refresh_all.addresses_on_file())
        self.assertEqual(parsed[self.running.id][2], {"bea@example.com": 7})

    # -- loading ---------------------------------------------------------

    def test_loading_writes_every_campaign_with_its_window(self):
        data = self.result([("bea@example.com", (5, 7, 0)),
                            ("rune@example.com", (0, 2, 1))])
        parsed = refresh_all.parse_result(data, refresh_all.addresses_on_file())
        _report, totals = refresh_all.load(parsed, apply_changes=True)
        self.assertEqual(totals, {"done": 5, "running": 9, "ongoing": 0})
        self.assertEqual(self.running.total_referrals, 9)
        row = CampaignReferral.query.filter_by(
            campaign_id=self.running.id, creator_id=self.rune.id).one()
        self.assertEqual((row.window_from, row.window_to),
                         (date(2026, 10, 28), TODAY))
        # Rune isn't in the ongoing campaign: skipped, not credited.
        self.assertEqual(CampaignReferral.query.filter_by(
            campaign_id=self.ongoing.id).count(), 0)

    def test_a_dry_run_writes_nothing(self):
        data = self.result([("bea@example.com", (5, 7, 0))])
        parsed = refresh_all.parse_result(data, refresh_all.addresses_on_file())
        refresh_all.load(parsed, apply_changes=False)
        self.assertEqual(CampaignReferral.query.count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
