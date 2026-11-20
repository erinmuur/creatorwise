"""Payment details from PDF invoices (tools/import_invoices.py).

Every invoice here is invented: example.com addresses, made-up account
numbers and a published bank routing number. Never put a real one here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import Creator, CreatorEmail, PaymentDetails, db
from tools import import_invoices as ii
from tools.content_requests import address_holders

ROUTING = "021000021"
ACCOUNT = "000123456789"

LABELLED = f"""INVOICE #12
From: Bea Lune
bea@example.com
Bill to: Readwise, Inc.
Address: 1 Example Street
Springfield, IL 62701
Phone: (555) 010-0199
Bank name: Example Bank
Account number: {ACCOUNT}
Routing number: {ROUTING}
Amount due: $150.00
"""

# PDF text layers often run printed lines together.
RUN_TOGETHER = (f"Invoice  Name: Pax Quill  pax@example.com  Bank: Example Credit Union  "
                f"Account #: 9876 5432 10  ABA: {ROUTING}  Total: $200")

UNLABELLED = f"""Juno Hart
22 Sample Avenue Apt 4
Riverton, NY 10001
555-010-0142
Checking account number {ACCOUNT}
Routing {ROUTING}
"""


class ParseTests(unittest.TestCase):
    def test_labelled_invoice(self):
        f = ii.parse(LABELLED)
        self.assertEqual(f["payee_name"], "Bea Lune")
        self.assertEqual(f["address"], "1 Example Street\nSpringfield, IL 62701")
        self.assertEqual(f["phone"], "(555) 010-0199")
        self.assertEqual(f["bank_name"], "Example Bank")
        self.assertEqual(f["account_number"], ACCOUNT)
        self.assertEqual(f["routing_number"], ROUTING)

    def test_fields_run_together_on_one_line(self):
        f = ii.parse(RUN_TOGETHER)
        self.assertEqual(f["payee_name"], "Pax Quill")
        self.assertEqual(f["bank_name"], "Example Credit Union")
        self.assertEqual(f["account_number"].replace(" ", ""), "9876543210")
        self.assertEqual(f["routing_number"], ROUTING)

    def test_unlabelled_address_and_phone(self):
        f = ii.parse(UNLABELLED)
        self.assertEqual(f["address"], "22 Sample Avenue Apt 4\nRiverton, NY 10001")
        self.assertEqual(f["phone"], "555-010-0142")
        self.assertEqual(f["account_number"], ACCOUNT)

    def test_a_bank_number_is_never_taken_for_a_phone(self):
        f = ii.parse(f"Account number: 5550100199\nRouting: {ROUTING}")
        self.assertIsNone(f["phone"])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.bea = Creator(name="Bea Lune", email="bea@example.com")
        self.pax = Creator(name="Pax Quill", email="pax.main@example.com")
        self.juno = Creator(name="Juno Hart", email="juno@example.com")
        db.session.add_all([self.bea, self.pax, self.juno])
        db.session.flush()
        db.session.add(CreatorEmail(creator_id=self.pax.id, address="pax@example.com"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def plan(self, texts):
        creators = Creator.query.all()
        return ii.plan(list(texts), creators, address_holders(creators),
                       read=lambda name: texts[name])

    def test_matched_by_email_name_and_file_name(self):
        entries = self.plan({"inv1.pdf": LABELLED, "inv2.pdf": RUN_TOGETHER,
                             "Juno Hart - October.pdf": UNLABELLED.replace("Juno Hart\n", "")})
        how = {os.path.basename(e["file"]): (e["creator"].name, e["how"]) for e in entries}
        self.assertEqual(how["inv1.pdf"], ("Bea Lune", "email"))
        self.assertEqual(how["inv2.pdf"], ("Pax Quill", "email"))     # a secondary address
        self.assertEqual(how["Juno Hart - October.pdf"], ("Juno Hart", "file name"))

    def test_unmatched_is_held_not_guessed(self):
        (e,) = self.plan({"x.pdf": "From: Somebody Else\nAccount number: 12345678"})
        self.assertIsNone(e["creator"])
        self.assertIn("no creator matches", e["problems"][0])

    def test_a_bad_routing_number_is_left_out(self):
        (e,) = self.plan({"inv.pdf": LABELLED.replace(ROUTING, "021000022")})
        self.assertNotIn("routing_number", e["fields"])
        self.assertIn("fails the bank checksum", " ".join(e["problems"]))
        self.assertEqual(e["fields"]["account_number"], ACCOUNT)

    def test_the_preview_never_shows_a_full_number(self):
        out = ii.render(self.plan({"inv1.pdf": LABELLED, "inv2.pdf": RUN_TOGETHER}))
        self.assertNotIn(ACCOUNT, out)
        self.assertNotIn(ROUTING, out)
        self.assertNotIn("9876543210", out)
        self.assertIn("••••6789", out)

    def test_apply_keeps_what_the_invoice_doesnt_mention(self):
        db.session.add(PaymentDetails(creator_id=self.bea.id, phone="555-010-0000",
                                      bank_name="Old Bank"))
        db.session.commit()
        text = f"bea@example.com\nBank name: Example Bank\nAccount number: {ACCOUNT}"
        saved = ii.apply(self.plan({"inv.pdf": text}), db)
        self.assertEqual(saved, 1)
        row = db.session.get(Creator, self.bea.id).payment_details
        self.assertEqual((row.phone, row.bank_name, row.account_number),
                         ("555-010-0000", "Example Bank", ACCOUNT))
        self.assertIsNotNone(row.updated_at)


@unittest.skipUnless(shutil.which("cupsfilter") and shutil.which("swiftc"), "needs macOS tools")
class RealPdfTests(unittest.TestCase):
    def test_a_pdf_is_read_end_to_end(self):
        folder = tempfile.mkdtemp()
        try:
            src = os.path.join(folder, "invoice.txt")
            with open(src, "w") as handle:
                handle.write(LABELLED)
            pdf = os.path.join(folder, "invoice.pdf")
            with open(pdf, "wb") as out:
                subprocess.run(["cupsfilter", src], stdout=out, stderr=subprocess.DEVNULL,
                               check=True)
            f = ii.parse(ii.pdf_text(pdf))
            self.assertEqual(f["account_number"], ACCOUNT)
            self.assertEqual(f["routing_number"], ROUTING)
            self.assertEqual(f["bank_name"], "Example Bank")

            # The same invoice as a scan: no text layer, so it goes through
            # Vision — which once deadlocked when run on the main thread.
            png = os.path.join(folder, "page.png")
            scan = os.path.join(folder, "scan.pdf")
            subprocess.run(["sips", "-s", "format", "png", pdf, "--out", png],
                           capture_output=True, check=True)
            subprocess.run(["sips", "-s", "format", "pdf", png, "--out", scan],
                           capture_output=True, check=True)
            f = ii.parse(ii.pdf_text(scan, timeout=90))
            self.assertEqual(f["account_number"], ACCOUNT)
            self.assertEqual(f["routing_number"], ROUTING)
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
