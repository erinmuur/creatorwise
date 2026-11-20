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

    def test_a_label_is_not_a_name(self):
        f = ii.parse("From: To:\nInvoice for Readwise\nAccount number: 12345678")
        self.assertIsNone(f["payee_name"])
        self.assertEqual(ii.parse("From: To\nName: Bea Lune")["payee_name"], "Bea Lune")

    def test_file_names_with_nicknames_in_brackets(self):
        self.assertIn("joe stusynski", ii.file_names("Joseph (Joe) Stusynski - 2026 - X.pdf"))
        self.assertIn("liv devine", ii.file_names("Olivia (Liv Devine) Evanoff - 2026 - X.pdf"))
        self.assertIn("bea lune", ii.file_names("Bea Lune - 2026 - Announcement.pdf"))

    def test_address_lines_lose_stray_commas(self):
        f = ii.parse("Address: 1 Example St NW,,\nSpringfield, IL 62701\n")
        self.assertEqual(f["address"], "1 Example St NW\nSpringfield, IL 62701")

    def test_a_paypal_address(self):
        self.assertEqual(ii.parse("Payment via PayPal: Bea.Lune@Example.com")["paypal"],
                         "bea.lune@example.com")
        self.assertEqual(ii.parse("Pay me at paypal.me/bealune")["paypal"], "paypal.me/bealune")

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
        self.jo = Creator(name="Jo Brightwater", email="jo@example.com")
        db.session.add_all([self.bea, self.pax, self.juno, self.jo])
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

    def test_a_nickname_in_the_file_name_matches(self):
        (e,) = self.plan({"Josephine (Jo) Brightwater - 2026 - Announcement.pdf":
                          "From: To\nAccount number: 12345678"})
        self.assertEqual((e["creator"].name, e["how"]), ("Jo Brightwater", "file name"))

    def test_a_paypal_invoice_makes_them_paid_by_paypal(self):
        text = "bea@example.com\nAddress: 1 Example Street\nSpringfield, IL 62701\nPayPal: bea.pays@example.com"
        entries = self.plan({"inv.pdf": text})
        self.assertEqual(entries[0]["fields"]["payment_method"], "paypal")
        out = ii.render(entries)
        self.assertIn("PayPal", out)
        self.assertNotIn("not in the invoice: bank", out)
        ii.apply(entries, db)
        c = db.session.get(Creator, self.bea.id)
        self.assertEqual((c.payment_method, c.payment_handle), ("paypal", "bea.pays@example.com"))
        self.assertTrue(c.payment_ready)

    def test_full_bank_details_make_it_a_bank_transfer(self):
        (e,) = self.plan({"inv.pdf": LABELLED})
        self.assertEqual(e["fields"]["payment_method"], "bank")

    def close(self, payee, filename="invoice.pdf", extra=()):
        creators = Creator.query.all() + list(extra)
        return [c.name for c in ii.close_matches(ii.people(payee, filename), creators)]

    def test_close_names(self):
        for name, email in (("Allie Brook", "ab@example.com"), ("Becca Stone", "bs@example.com"),
                            ("Juno W", "jw@example.com"), ("Pax", "paxwren@example.com"),
                            ("Zuri", "zz@example.com"), ("Serena Ann Vale", "sv@example.com")):
            db.session.add(Creator(name=name, email=email))
        db.session.commit()
        self.assertEqual(self.close("Allison Brook"), ["Allie Brook"])          # short form
        self.assertEqual(self.close("Rebecca Stone"), ["Becca Stone"])          # inside it
        self.assertEqual(self.close("Juno Wren"), ["Juno W"])                   # an initial
        self.assertEqual(self.close("Pax Wren"), ["Pax"])                       # surname in email
        self.assertEqual(self.close("Zuriel Banks"), ["Zuri"])                  # only Zuri
        self.assertEqual(self.close("Serena Vale"), ["Serena Ann Vale"])        # middle name
        self.assertEqual(self.close(None, "Samantha Quillon (Stone) - 2026 - X.docx",
                                    [Creator(name="Samantha Stone", email="ss@example.com")]),
                         ["Samantha Stone"])                                    # maiden name
        self.assertEqual(self.close("Brook Allison"), [])                       # not reversed

    def test_close_matches_are_flagged_for_checking(self):
        db.session.add(Creator(name="Becca Stone", email="bs@example.com"))
        db.session.commit()
        (e,) = self.plan({"Rebecca Stone - 2026 - X.docx": "From: Rebecca Stone\nAccount number: 12345678"})
        self.assertEqual((e["creator"].name, e["how"]), ("Becca Stone", "close name — check it"))

    def test_the_bill_to_address_is_dropped(self):
        to = "Bill to: Readwise\nAddress: 9 Company Way\nSpringfield, IL 62701\n"
        texts = {f"inv{i}.pdf": to + f"bea@example.com\n{10 + i} Home Street\nShelbyville, IL 6270{i}"
                 for i in range(3)}
        entries = self.plan(texts)
        self.assertEqual([e["fields"].get("address") for e in entries],
                         [f"{10 + i} Home Street\nShelbyville, IL 6270{i}" for i in range(3)])

    def test_paypal_is_not_a_name(self):
        self.assertIsNone(ii.parse("Name: Paypal\nPayPal: bea@example.com")["payee_name"])

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


REPLY = """From: Bea Lune <bea@example.com>
To: Erin <erin@readwise.io>
Subject: Re: Your Readwise announcement payment
Content-Type: text/plain; charset=utf-8

Hi Erin! Here you go:

Full name associated with bank account: Beatrice Lune
Account #: 000123456789
Routing #: 021000021
Bank Name: Example Bank
Phone: 555-010-0199
Address: 1 Example Street
Springfield, IL 62701

Thanks!

On Fri, Nov 20, 2026 at 9:00 AM Erin <erin@readwise.io> wrote:
> When you have a chance, can you please confirm your payment details?
> Full name associate with bank account
> Account #
> Routing #
"""


class ReplyTests(unittest.TestCase):
    def test_a_saved_reply_email(self):
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "Re payment.eml")
            with open(path, "w") as f:
                f.write(REPLY)
            self.assertEqual(ii.find_pdfs([folder]), [path])
            text = ii.read_invoice(path)
            self.assertNotIn("wrote:", text)                  # the quoted original is gone
            self.assertIn("bea@example.com", ii.emails_in(text))
            f = ii.parse(text)
            self.assertEqual((f["payee_name"], f["account_number"], f["routing_number"],
                              f["bank_name"]),
                             ("Beatrice Lune", "000123456789", "021000021", "Example Bank"))
            self.assertEqual(f["address"], "1 Example Street\nSpringfield, IL 62701")
        finally:
            shutil.rmtree(folder)

    def test_a_paypal_reply(self):
        text = ii.strip_quoted("Sure, PayPal is fine: bea.pays@example.com\n\nOn Fri, Nov 20, 2026 Erin wrote:\n> Account #")
        self.assertEqual(ii.parse(text)["paypal"], "bea.pays@example.com")
        self.assertIsNone(ii.parse(text)["account_number"])


@unittest.skipUnless(shutil.which("cupsfilter") and shutil.which("swiftc")
                     and shutil.which("textutil"), "needs macOS tools")
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

    def test_a_word_invoice_is_read_too(self):
        folder = tempfile.mkdtemp()
        try:
            src = os.path.join(folder, "invoice.txt")
            with open(src, "w") as handle:
                handle.write(LABELLED)
            sub = os.path.join(folder, "October")
            os.makedirs(sub)
            docx = os.path.join(sub, "Bea Lune - 2026 - Announcement.docx")
            subprocess.run(["textutil", "-convert", "docx", src, "-output", docx],
                           capture_output=True, check=True)
            os.unlink(src)
            self.assertEqual(ii.find_pdfs([folder]), [docx])     # found in a subfolder
            f = ii.parse(ii.read_invoice(docx))
            self.assertEqual(f["account_number"], ACCOUNT)
            self.assertEqual(f["routing_number"], ROUTING)
            self.assertEqual(f["bank_name"], "Example Bank")
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
