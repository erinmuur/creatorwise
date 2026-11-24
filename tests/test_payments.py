"""Payment details on a creator's profile (payments.py, the profile form).

Numbers here are invented, or public bank routing numbers. Never put a real
creator's details in this repository.
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = tempfile.mkdtemp()
os.environ.update(
    DASHBOARD_PASSWORD="test-password", SECRET_KEY="test-secret",
    INGEST_TOKEN="t", SEED_SAMPLE_DATA="0",
    DATABASE_URL="sqlite:///" + os.path.join(DATA, "creatorwise.db"),
)

import app as appmod  # noqa: E402
import payments  # noqa: E402
from models import Creator, PaymentDetails, db  # noqa: E402

ROUTING = "021000021"        # a published bank routing number, not anybody's account
ACCOUNT = "000123456789"
IBAN = "GB82WEST12345698765432"   # the published example IBAN
SWIFT = "DEUTDEFF500"             # a bank's public BIC, not an account


class CheckTests(unittest.TestCase):
    def test_routing_numbers_are_checksummed(self):
        self.assertTrue(payments.routing_ok(ROUTING))
        self.assertFalse(payments.routing_ok("021000022"))     # one digit off
        self.assertFalse(payments.routing_ok("12345678"))      # too short

    def test_pasted_numbers_lose_spaces_and_dashes(self):
        values, errors = payments.check({"account_number": "0001 2345-6789",
                                         "routing_number": "0210 00021"})
        self.assertEqual(errors, [])
        self.assertEqual((values["account_number"], values["routing_number"]),
                         (ACCOUNT, ROUTING))

    def test_bad_numbers_are_refused_without_echoing_them(self):
        _, errors = payments.check({"account_number": "12ab", "routing_number": "999999999"})
        self.assertEqual(len(errors), 2)
        self.assertFalse(any("12ab" in e or "999999999" in e for e in errors))


class FormTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        with appmod.app.app_context():
            db.create_all()
            PaymentDetails.query.delete()
            Creator.query.delete()
            c = Creator(name="Bea Lune", email="bea@example.com")
            db.session.add(c)
            db.session.commit()
            self.cid = c.id
        self.client.post("/login", data={"password": "test-password"})

    def token(self):
        page = self.client.get(f"/creators/{self.cid}").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def post(self, **fields):
        return self.client.post(f"/creators/{self.cid}/payment", data=fields)

    def test_saved_and_shown_masked(self):
        r = self.post(csrf_token=self.token(), payee_name="Bea Lune",
                      bank_name="Example Bank", account_number=ACCOUNT,
                      routing_number=ROUTING, address="1 Example St\nTown",
                      phone="555-0100")
        self.assertEqual(r.status_code, 302)
        with appmod.app.app_context():
            row = db.session.get(Creator, self.cid).payment_details
            self.assertTrue(row.has_bank_account)
            self.assertIsNotNone(row.updated_at)
        page = self.client.get(f"/creators/{self.cid}").get_data(as_text=True)
        self.assertIn("••••6789", page)
        self.assertIn("Bank details on file", page)
        # The full number appears only inside the edit form's input.
        self.assertEqual(page.count(ACCOUNT), 1)
        self.assertIn(f'name="account_number" autocomplete="off" '
                      f'value="{ACCOUNT}"', page)

    def test_paid_by_paypal(self):
        r = self.post(csrf_token=self.token(), payment_method="paypal",
                      paypal_email="Bea.Pays@Example.com", phone="555-0100")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("payment_error", r.headers["Location"])
        with appmod.app.app_context():
            c = db.session.get(Creator, self.cid)
            self.assertEqual((c.payment_method, c.payment_handle),
                             ("paypal", "bea.pays@example.com"))
            self.assertTrue(c.payment_ready)
        page = self.client.get(f"/creators/{self.cid}").get_data(as_text=True)
        self.assertIn("Paid by PayPal — ready to pay", page)

    def test_paypal_needs_an_address(self):
        r = self.post(csrf_token=self.token(), payment_method="paypal")
        self.assertIn("payment_error=", r.headers["Location"])
        r = self.post(csrf_token=self.token(), payment_method="paypal",
                      paypal_email="not an email")
        self.assertIn("payment_error=", r.headers["Location"])

    def test_bank_transfer_is_ready_only_with_both_numbers(self):
        self.post(csrf_token=self.token(), payment_method="bank", account_number=ACCOUNT)
        with appmod.app.app_context():
            self.assertFalse(db.session.get(Creator, self.cid).payment_ready)
        self.post(csrf_token=self.token(), payment_method="bank", account_number=ACCOUNT,
                  routing_number=ROUTING)
        with appmod.app.app_context():
            self.assertTrue(db.session.get(Creator, self.cid).payment_ready)

    def test_international_transfer_needs_account_and_swift(self):
        r = self.post(csrf_token=self.token(), payment_method="intl_ach",
                      account_number="gb82 west 1234 5698 7654 32", routing_number="WEST1234")
        self.assertNotIn("payment_error", r.headers["Location"])   # no US routing check
        with appmod.app.app_context():
            c = db.session.get(Creator, self.cid)
            self.assertEqual(c.payment_details.account_number, IBAN)
            self.assertFalse(c.payment_ready)                      # no SWIFT yet
        self.post(csrf_token=self.token(), payment_method="intl_ach", account_number=IBAN,
                  swift_code="deut de ff 500")
        with appmod.app.app_context():
            c = db.session.get(Creator, self.cid)
            self.assertEqual(c.payment_details.swift_code, SWIFT)
            self.assertTrue(c.payment_ready)
        page = self.client.get(f"/creators/{self.cid}").get_data(as_text=True)
        self.assertIn("International bank details on file", page)
        self.assertIn('<option value="intl_ach" selected', page)
        self.assertIn(f"<dt>SWIFT</dt><dd>{SWIFT}</dd>", page)

    def test_a_bad_swift_code_is_refused(self):
        for bad in ("DEUT", "DEUTDEFF5", "12UTDEFF"):
            r = self.post(csrf_token=self.token(), payment_method="intl_ach",
                          account_number=IBAN, swift_code=bad)
            self.assertIn("SWIFT", r.headers["Location"])
        r = self.post(csrf_token=self.token(), payment_method="bank", account_number=IBAN)
        self.assertIn("payment_error=", r.headers["Location"])     # IBANs only abroad

    def test_no_token_no_save(self):
        r = self.post(account_number=ACCOUNT, routing_number=ROUTING)
        self.assertEqual(r.status_code, 400)
        with appmod.app.app_context():
            self.assertIsNone(db.session.get(Creator, self.cid).payment_details)

    def test_a_bad_number_saves_nothing_and_says_which_field(self):
        r = self.post(csrf_token=self.token(), account_number=ACCOUNT,
                      routing_number="021000022")
        self.assertIn("payment_error=", r.headers["Location"])
        self.assertNotIn(ACCOUNT, r.headers["Location"])
        with appmod.app.app_context():
            self.assertIsNone(db.session.get(Creator, self.cid).payment_details)

    def test_signed_out_cannot_post(self):
        anon = appmod.app.test_client()
        r = anon.post(f"/creators/{self.cid}/payment", data={"account_number": ACCOUNT})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])


class MergeTests(unittest.TestCase):
    """push_data.py takes the server's newer rows before replacing its database."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "local.db")
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE creators (id INTEGER PRIMARY KEY, name TEXT, "
                     "payment_method TEXT, payment_handle TEXT)")
        conn.execute("INSERT INTO creators (id, name) VALUES (1, 'Bea'), (2, 'Pax')")
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()

    def row(self, cid, bank, when):
        return {"creator_id": cid, "payee_name": None, "address": None, "phone": None,
                "bank_name": bank, "account_number": ACCOUNT, "routing_number": ROUTING,
                "updated_at": when}

    def banks(self):
        return dict(self.conn.execute("SELECT creator_id, bank_name FROM payment_details"))

    def test_new_and_newer_rows_are_taken_older_are_not(self):
        self.assertEqual(payments.merge_rows(self.conn, [self.row(1, "Server Bank",
                                                                  "2026-11-19T10:00:00")]), 1)
        self.conn.execute("UPDATE payment_details SET bank_name='Mac Bank', "
                          "updated_at='2026-11-19 12:00:00.000000'")
        self.conn.commit()
        taken = payments.merge_rows(self.conn, [
            self.row(1, "Older Server Bank", "2026-11-19T11:00:00"),   # Mac's is newer
            self.row(2, "Pax Bank", "2026-11-19T09:00:00"),            # Mac has none
            self.row(99, "Nobody", "2026-11-19T09:00:00"),             # no such creator
        ])
        self.assertEqual(taken, 1)
        self.assertEqual(self.banks(), {1: "Mac Bank", 2: "Pax Bank"})
        newer = payments.merge_rows(self.conn, [self.row(1, "Newest", "2026-11-20T08:00:00")])
        self.assertEqual((newer, self.banks()[1]), (1, "Newest"))

    def test_a_swift_code_comes_back_and_an_old_server_leaves_it_alone(self):
        payments.merge_rows(self.conn, [dict(self.row(1, "B", "2026-11-20T09:00:00"),
                                             swift_code=SWIFT)])
        swift = lambda: self.conn.execute(
            "SELECT swift_code FROM payment_details WHERE creator_id = 1").fetchone()[0]
        self.assertEqual(swift(), SWIFT)
        payments.merge_rows(self.conn, [self.row(1, "C", "2026-11-21T09:00:00")])  # no key
        self.assertEqual(swift(), SWIFT)

    def test_the_column_is_added_to_an_older_table(self):
        self.conn.execute("CREATE TABLE payment_details (id INTEGER PRIMARY KEY, "
                          "creator_id INTEGER, bank_name TEXT)")
        payments.ensure_columns(self.conn)
        payments.ensure_columns(self.conn)                         # idempotent
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(payment_details)")}
        self.assertIn("swift_code", cols)

    def test_a_paypal_address_set_on_the_server_comes_back(self):
        row = dict(self.row(2, None, "2026-11-20T09:00:00"), account_number=None,
                   routing_number=None, payment_method="paypal",
                   payment_handle="pax@example.com")
        payments.merge_rows(self.conn, [row])
        self.assertEqual(self.conn.execute(
            "SELECT payment_method, payment_handle FROM creators WHERE id = 2").fetchone(),
            ("paypal", "pax@example.com"))


if __name__ == "__main__":
    unittest.main()
