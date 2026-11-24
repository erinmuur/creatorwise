"""Campaign outreach tracking (tools/outreach.py).

Invented creators, example.com addresses.
"""

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
import models as m  # noqa: E402
from models import Campaign, Creator, CreatorEmail, Participation, db  # noqa: E402
from tools import outreach  # noqa: E402

MODELS = {k: getattr(m, k) for k in ("db", "Campaign", "Creator", "CreatorEmail",
                                     "Participation")}


class OutreachTest(unittest.TestCase):
    def setUp(self):
        self.ctx = appmod.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.camp = Campaign(name="Winter Push", slug="winter-push")
        db.session.add(self.camp)
        self.bea = self.creator("Bea Lune", "bea@example.com")
        self.pax = self.creator("Pax Quill", "pax@example.com", ["quill.reads@example.com"])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def creator(self, name, email, more=()):
        c = Creator(name=name, email=email)
        db.session.add(c)
        db.session.flush()
        db.session.add(CreatorEmail(creator_id=c.id, address=email, is_primary=True))
        for a in more:
            db.session.add(CreatorEmail(creator_id=c.id, address=a))
        return c

    def run_once(self, contacts, today=date(2026, 11, 24)):
        campaign, rows = outreach.plan({"campaign": "winter-push", "contacts": contacts}, MODELS)
        outreach.apply(campaign, rows, MODELS, today=today)
        return {r["email"]: r for r in rows}

    def status(self, creator):
        p = Participation.query.filter_by(creator_id=creator.id, campaign_id=self.camp.id).first()
        return p and p.status

    def test_contacted_and_opted_in(self):
        rows = self.run_once([
            {"email": "bea@example.com", "name": "Bea", "reply": "none"},
            {"email": "QUILL.reads@example.com", "name": "Pax", "reply": "opted_in",
             "replied_on": "2026-11-23", "evidence": "I'm in"},
        ])
        self.assertEqual(self.status(self.bea), "contacted")
        self.assertEqual(self.status(self.pax), "opted_in")  # secondary address, any case
        part = Participation.query.filter_by(creator_id=self.pax.id).one()
        self.assertEqual(part.opted_in_on, date(2026, 11, 23))
        self.assertEqual(rows["bea@example.com"]["action"], "add")

    def test_rerun_changes_nothing_and_yes_moves_forward(self):
        self.run_once([{"email": "bea@example.com", "reply": "none"}])
        rows = self.run_once([{"email": "bea@example.com", "reply": "none"}])
        self.assertEqual(rows["bea@example.com"]["action"], "unchanged")
        rows = self.run_once([{"email": "bea@example.com", "reply": "opted_in"}],
                             today=date(2026, 11, 26))
        self.assertEqual(rows["bea@example.com"]["action"], "change")
        self.assertEqual(self.status(self.bea), "opted_in")
        part = Participation.query.filter_by(creator_id=self.bea.id).one()
        self.assertEqual(part.opted_in_on, date(2026, 11, 26))

    def test_never_moves_back_past_briefed(self):
        db.session.add(Participation(creator_id=self.bea.id, campaign_id=self.camp.id,
                                     status="briefed"))
        db.session.commit()
        rows = self.run_once([{"email": "bea@example.com", "reply": "none"}])
        self.assertEqual(self.status(self.bea), "briefed")
        self.assertEqual(rows["bea@example.com"]["action"], "unchanged")
        rows = self.run_once([{"email": "bea@example.com", "reply": "declined"}])
        self.assertEqual(self.status(self.bea), "briefed")
        self.assertIn("left at briefed", rows["bea@example.com"]["note"])

    def test_unknown_and_shared_addresses_are_held(self):
        juno = self.creator("Juno Hart", "juno@example.com", ["duo@example.com"])
        self.creator("Ivo Hart", "ivo@example.com", ["duo@example.com"])
        db.session.commit()
        rows = self.run_once([
            {"email": "stranger@example.com", "name": "Bea", "reply": "opted_in"},
            {"email": "duo@example.com", "reply": "opted_in"},
        ])
        self.assertEqual(rows["stranger@example.com"]["reason"], "not on file")
        self.assertTrue(rows["duo@example.com"]["reason"].startswith("address shared"))
        # A first name matching somebody on file is not a match.
        self.assertIsNone(self.status(self.bea))
        self.assertIsNone(self.status(juno))
        self.assertEqual(Participation.query.count(), 0)

    def test_two_threads_to_one_person_merge(self):
        rows = self.run_once([
            {"email": "bea@example.com", "thread": "a", "contacted_on": "2026-11-24",
             "reply": "none"},
            {"email": "bea@example.com", "thread": "b", "contacted_on": "2026-11-23",
             "reply": "opted_in", "replied_on": "2026-11-24", "evidence": "yes!"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["bea@example.com"]["contacted_on"], "2026-11-23")
        self.assertEqual(self.status(self.bea), "opted_in")

    def test_decline_and_bad_reply(self):
        self.run_once([{"email": "bea@example.com", "reply": "declined"}])
        self.assertEqual(self.status(self.bea), "declined")
        with self.assertRaises(ValueError):
            self.run_once([{"email": "pax@example.com", "reply": "maybe"}])


if __name__ == "__main__":
    unittest.main()
