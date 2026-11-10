"""Profile photos: stored as files, validated by content, never guessed at.

No network here — the fetcher is injected — and every name is invented.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import avatars
from models import Creator, CreatorEmail, db
from tools import fetch_avatars as tool

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
HTML = b"<html><body>AccessDenied</body></html>"


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "avatars")

    def tearDown(self):
        self.tmp.cleanup()

    def test_images_are_recognised_by_their_bytes(self):
        self.assertEqual(avatars.sniff(JPEG), ".jpg")
        self.assertEqual(avatars.sniff(PNG), ".png")
        self.assertEqual(avatars.sniff(WEBP), ".webp")

    def test_an_error_page_is_not_a_photo(self):
        """A CDN can answer a missing object with 200 and some HTML."""
        for junk in (HTML, b"", None, b"GIF89a"):
            with self.subTest(junk=junk):
                self.assertIsNone(avatars.sniff(junk))
        with self.assertRaises(ValueError):
            avatars.save(self.folder, 7, HTML)
        self.assertEqual(avatars.ids(self.folder), set())

    def test_save_then_find(self):
        path = avatars.save(self.folder, 7, JPEG)
        self.assertTrue(path.endswith("7.jpg"))
        self.assertEqual(avatars.find(self.folder, 7), path)
        self.assertIsNone(avatars.find(self.folder, 8))
        self.assertEqual(avatars.ids(self.folder), {7})

    def test_a_new_photo_replaces_the_old_one_even_across_formats(self):
        avatars.save(self.folder, 7, JPEG)
        avatars.save(self.folder, 7, PNG)
        self.assertEqual(os.listdir(self.folder), ["7.png"])

    def test_an_oversized_file_is_refused(self):
        with self.assertRaises(ValueError):
            avatars.save(self.folder, 7, JPEG + b"\x00" * avatars.MAX_BYTES)

    def test_a_missing_folder_just_means_nobody_has_one(self):
        self.assertEqual(avatars.ids(os.path.join(self.tmp.name, "nope")), set())

    def test_stray_files_are_not_mistaken_for_creators(self):
        avatars.save(self.folder, 7, JPEG)
        open(os.path.join(self.folder, "notes.txt"), "w").close()
        open(os.path.join(self.folder, "bea.jpg"), "w").close()
        self.assertEqual(avatars.ids(self.folder), {7})

    def test_the_folder_sits_beside_the_database(self):
        self.assertEqual(
            avatars.directory("sqlite:////app/data/creatorwise.db"),
            "/app/data/avatars",
        )


class FetchToolTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "avatars")

        self.bea = self._creator("Bea Lune", "bea@example.com", "bea.lune@example.net")
        self.pax = self._creator("Pax Quill", "duo@example.com")
        self.juno = self._creator("Juno Quill", "juno@example.com")
        db.session.add(CreatorEmail(creator_id=self.juno.id, address="duo@example.com"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.tmp.cleanup()

    def _creator(self, name, email, extra=None):
        creator = Creator(name=name, email=email)
        db.session.add(creator)
        db.session.flush()
        db.session.add(CreatorEmail(creator_id=creator.id, address=email, is_primary=True))
        if extra:
            db.session.add(CreatorEmail(creator_id=creator.id, address=extra))
        db.session.flush()
        return creator

    def test_an_address_finds_its_creator(self):
        result = tool.plan({"bea@example.com": "a/b.jpg"})
        self.assertEqual(result["fetch"], [(self.bea.id, "Bea Lune", "a/b.jpg")])

    def test_a_second_address_finds_the_same_creator(self):
        result = tool.plan({"bea.lune@example.net": "a/b.jpg"})
        self.assertEqual(result["fetch"][0][0], self.bea.id)

    def test_two_accounts_for_one_creator_use_the_first_and_say_so(self):
        result = tool.plan({"bea@example.com": "a/1.jpg",
                            "bea.lune@example.net": "a/2.jpg"})
        self.assertEqual(len(result["fetch"]), 1)
        self.assertEqual(len(result["duplicate"]), 1)

    def test_a_shared_mailbox_gets_nobody_a_photo(self):
        """The image belongs to one account; the address can't say whose."""
        result = tool.plan({"duo@example.com": "a/b.jpg"})
        self.assertEqual(result["fetch"], [])
        self.assertEqual(result["shared"], ["duo@example.com"])

    def test_an_unknown_address_is_reported(self):
        result = tool.plan({"nobody@example.org": "a/b.jpg"})
        self.assertEqual(result["unknown"], ["nobody@example.org"])

    def test_apply_downloads_from_base_plus_key(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return JPEG

        saved, skipped, failures = tool.apply(
            tool.plan({"bea@example.com": "dir/pic.jpg"}), self.folder,
            base="https://cdn.example.com/media/", fetch=fetch,
        )
        self.assertEqual((saved, skipped, failures), (1, 0, []))
        self.assertEqual(seen, ["https://cdn.example.com/media/dir/pic.jpg"])
        self.assertEqual(avatars.ids(self.folder), {self.bea.id})

    def test_a_failed_download_is_reported_and_leaves_nothing_behind(self):
        def fetch(url):
            raise OSError("HTTP 404")

        saved, _, failures = tool.apply(
            tool.plan({"bea@example.com": "dir/pic.jpg"}), self.folder, fetch=fetch
        )
        self.assertEqual(saved, 0)
        self.assertEqual(failures, [("Bea Lune", "HTTP 404")])
        self.assertEqual(avatars.ids(self.folder), set())

    def test_a_download_that_is_not_an_image_is_a_failure_too(self):
        saved, _, failures = tool.apply(
            tool.plan({"bea@example.com": "dir/pic.jpg"}), self.folder,
            fetch=lambda url: HTML,
        )
        self.assertEqual(saved, 0)
        self.assertIn("not a JPEG", failures[0][1])

    def test_missing_only_leaves_existing_photos_alone(self):
        avatars.save(self.folder, self.bea.id, PNG)
        saved, skipped, _ = tool.apply(
            tool.plan({"bea@example.com": "dir/pic.jpg"}), self.folder,
            fetch=lambda url: JPEG, missing_only=True,
        )
        self.assertEqual((saved, skipped), (0, 1))
        self.assertTrue(avatars.find(self.folder, self.bea.id).endswith(".png"))

    def test_verification_is_never_switched_off(self):
        """The fix for a missing CA bundle is to find one."""
        import ssl
        context = tool._ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class InitialsTests(unittest.TestCase):
    def test_initials(self):
        cases = {
            "Bea Lune": "BL",
            "bea": "B",
            "Pax (Reader's Nook)": "PR",
            "Juno Quill Vale": "JQ",
            "": "?",
            "& Co": "C",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(Creator(name=name, email="x@example.com").initials,
                                 expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
