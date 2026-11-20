"""Payments owed (payments.refresh, tools/payments_due.py, the Payments page).

Invented creators, example.com addresses, made-up bank numbers.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = tempfile.mkdtemp()
os.environ.update(
    DASHBOARD_PASSWORD="test-password", SECRET_KEY="test-secret",
    INGEST_TOKEN="t", SEED_SAMPLE_DATA="0",
    DATABASE_URL="sqlite:///" + os.path.join(DATA, "creatorwise.db"),
)

import app as appmod  # noqa: E402
import payments  # noqa: E402
from models import (Campaign, Creator, Participation, Payment, PaymentDetails,  # noqa: E402
                    PlatformPost, UgcPiece, db)
from tools import payments_due as pd  # noqa: E402

TODAY = date(2026, 11, 20)


class Base(unittest.TestCase):
    def setUp(self):
        self.ctx = appmod.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.camp = Campaign(name="Readwise Books Announcement", slug="books-announcement")
        db.session.add(self.camp)
        db.session.flush()
        self.bea = self.creator("Bea Lune", [date(2026, 11, 1), date(2026, 11, 5)])
        self.pax = self.creator("Pax Quill (Quill Reads)", [date(2026, 11, 12)])
        self.juno = self.creator("Juno Hart", [None, date(2026, 11, 2)])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def creator(self, name, dates):
        c = Creator(name=name, email=f"{name.split()[0].lower()}@example.com")
        db.session.add(c)
        db.session.flush()
        db.session.add(Participation(creator_id=c.id, campaign_id=self.camp.id,
                                     status="content_delivered"))
        for d in dates:
            piece = UgcPiece(creator_id=c.id, campaign_id=self.camp.id, posted_on=d)
            db.session.add(piece)
        db.session.flush()
        return c

    def pay(self, creator):
        return Payment.query.filter_by(creator_id=creator.id).one()


class RefreshTests(Base):
    def test_due_fourteen_days_after_the_first_post(self):
        payments.refresh(self.camp, TODAY)
        bea = self.pay(self.bea)
        self.assertEqual((bea.first_posted_on, bea.due_on, bea.status),
                         (date(2026, 11, 1), date(2026, 11, 15), "pending"))
        self.assertEqual((bea.referral_from, bea.referral_to), (date(2026, 11, 1), date(2026, 11, 14)))
        self.assertEqual(self.pay(self.pax).status, "upcoming")         # due Nov 26

    def test_an_undated_piece_holds_the_date_back(self):
        _, _, undated = payments.refresh(self.camp, TODAY)
        self.assertEqual(undated, ["Juno Hart"])
        self.assertIsNone(self.pay(self.juno).due_on)

    def test_paid_is_never_touched(self):
        payments.refresh(self.camp, TODAY)
        payments.mark_paid(self.pay(self.bea), date(2026, 11, 16))
        db.session.commit()
        payments.refresh(self.camp, date(2026, 12, 1))
        self.assertEqual(self.pay(self.bea).status, "paid")

    def test_amount_is_base_plus_bonus(self):
        payments.refresh(self.camp, TODAY)
        bea = self.pay(self.bea)
        self.assertFalse(bea.amount_known)
        bea.referrals = 12
        self.assertEqual((bea.total_cents, payments.money(bea.total_cents)), (16000, "$160"))


class ReferralTests(Base):
    def test_the_checksum_is_enforced(self):
        good = {"rows": [[None, 5], [1, 3], [3, 2]]}
        self.assertEqual(pd.check_result(good, {1, 3, 7}), {1: 3, 3: 2, 7: 0})
        with self.assertRaises(ValueError):
            pd.check_result({"rows": [[None, 6], [1, 3], [3, 2]]}, {1, 3})
        with self.assertRaises(ValueError):
            pd.check_result({"rows": [[None, 3], [99, 3]]}, {1})

    def test_the_query_covers_each_creators_own_window(self):
        payments.refresh(self.camp, TODAY)
        holders = {"bea@example.com": {self.bea.id}}
        sql, asked = pd.referral_sql(pd.to_count(Payment.query.all(), TODAY), holders)
        self.assertEqual(asked, [self.bea.id])
        self.assertIn("DATE '2026-11-01', DATE '2026-11-15'", sql)
        self.assertIn("GROUP BY ROLLUP", sql)


class EmailTests(Base):
    def setUp(self):
        super().setUp()
        payments.refresh(self.camp, TODAY)
        self.pay(self.bea).referrals = 2
        db.session.add(PaymentDetails(creator_id=self.bea.id, payee_name="Bea Lune",
                                      bank_name="Example Bank", account_number="000123456789",
                                      routing_number="021000021"))
        self.bea.payment_method = "bank"
        db.session.commit()

    def test_invoice_spec(self):
        spec = pd.invoice_spec(self.pay(self.bea), 1001, TODAY)
        self.assertEqual(spec["lines"][0], {
            "description": "Readwise Books Announcement Post — November 1, 2026", "amount": "$100"})
        self.assertEqual(spec["lines"][1]["description"], "Referral Bonus ($5 x 2)")
        self.assertEqual(spec["total"], "$110")
        self.assertIn("Routing #: 021000021", spec["payTo"])

    def test_the_right_template_and_first_name(self):
        self.assertEqual(pd.first_name(self.pax), "Pax")
        juno_pay = self.pay(self.juno)
        self.assertFalse(pd.ready_for_email(juno_pay))        # no date yet
        self.assertTrue(self.bea.payment_ready)
        body = pd.html(pd.DETAILS_ON_FILE.format(first_name="Bea"))
        self.assertIn("confirm the attached payment details", body)
        self.assertTrue(body.startswith("<p>Hey Bea"))


class FollowUpTests(Base):
    def test_details_arriving_later_bring_the_invoice_email(self):
        payments.refresh(self.camp, TODAY)
        bea = self.pay(self.bea)
        bea.referrals = 0
        db.session.commit()
        self.assertTrue(pd.ready_for_email(bea))               # never emailed
        bea.drafted_on, bea.draft_id = TODAY, "details:d1"
        db.session.commit()
        self.assertFalse(pd.ready_for_email(bea))              # asked; nothing back yet
        self.assertEqual([p.id for p in pd.awaiting(Payment.query.all())], [bea.id])
        db.session.add(PaymentDetails(creator_id=self.bea.id, account_number="000123456789",
                                      routing_number="021000021"))
        db.session.commit()
        db.session.refresh(self.bea)
        self.assertTrue(pd.ready_for_email(bea))               # details in: invoice email
        self.assertEqual(pd.awaiting(Payment.query.all()), [])
        bea.draft_id = "invoice:d2"
        self.assertFalse(pd.ready_for_email(bea))              # and only once


class PageTests(Base):
    def setUp(self):
        super().setUp()
        payments.refresh(self.camp, TODAY)
        self.client = appmod.app.test_client()
        self.client.post("/login", data={"password": "test-password"})

    def test_mark_paid_needs_the_token(self):
        pid = self.pay(self.bea).id
        page = self.client.get("/payments").get_data(as_text=True)
        self.assertIn("Bea Lune", page)
        self.assertEqual(self.client.post(f"/payments/{pid}/status",
                                          data={"status": "paid"}).status_code, 400)
        import re
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.client.post(f"/payments/{pid}/status", data={"status": "paid", "csrf_token": token})
        self.assertEqual(db.session.get(Payment, pid).status, "paid")


class MergeTests(unittest.TestCase):
    def test_a_newer_status_from_the_server_is_kept(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE payments (creator_id, campaign_id, status, paid_on, updated_at)")
        conn.execute("INSERT INTO payments VALUES (1, 6, 'pending', NULL, '2026-11-20 08:00:00.000000')")
        rows = [{"creator_id": 1, "campaign_id": 6, "status": "paid", "paid_on": "2026-11-20",
                 "updated_at": "2026-11-20T09:00:00"}]
        self.assertEqual(payments.merge_payments(conn, rows), 1)
        self.assertEqual(conn.execute("SELECT status, paid_on FROM payments").fetchone(),
                         ("paid", "2026-11-20"))
        rows[0].update(status="pending", updated_at="2026-11-20T07:00:00")   # older: ignored
        self.assertEqual(payments.merge_payments(conn, rows), 0)


if __name__ == "__main__":
    unittest.main()
