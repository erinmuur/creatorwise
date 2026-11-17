"""Saved videos and carousel slides: stored by content, served only by name.

These guard three things. A file is what its bytes say, not what its name
says. Nothing outside a piece's own stored files can be fetched through the
media route. And the importer files what a person saved without guessing:
one video per post, slides in the order they were saved.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update(
    DASHBOARD_PASSWORD="test-password", SECRET_KEY="test-secret",
    INGEST_TOKEN="t", SEED_SAMPLE_DATA="0", DATABASE_URL="sqlite://",
)

import app as appmod  # noqa: E402
import media  # noqa: E402
from models import Campaign, Creator, PlatformPost, UgcPiece, db  # noqa: E402
from tools import import_media  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
HTML = b"<!doctype html><title>Not found</title>"


class SniffTests(unittest.TestCase):
    def test_files_are_what_their_bytes_say(self):
        self.assertEqual(media.sniff(JPEG), ".jpg")
        self.assertEqual(media.sniff(PNG), ".png")
        self.assertEqual(media.sniff(MP4), "video")
        self.assertEqual(media.sniff(HEIC), "heic")

    def test_an_error_page_saved_as_a_video_is_not_one(self):
        self.assertIsNone(media.sniff(HTML))
        self.assertIsNone(media.sniff(b""))


class TranscriptTests(unittest.TestCase):
    def test_segments_are_rejoined_into_one_sentence_per_line(self):
        from tools.transcribe_media import first_line, reflow
        lines = ["I read over 250 more words a minute when I read",
                 "romance. It's wild! Do you track", "yours?"]
        self.assertEqual(reflow(lines).splitlines(), [
            "I read over 250 more words a minute when I read romance.",
            "It's wild!", "Do you track yours?"])
        self.assertEqual(first_line(lines),
                         "I read over 250 more words a minute when I read romance.")


class ScreenTextTests(unittest.TestCase):
    def test_app_chrome_is_not_a_caption(self):
        from tools.transcribe_media import is_caption
        for junk in ("2h 27m", "+10.8%", "Pages", "45,208", "• 1h 13m"):
            with self.subTest(junk=junk):
                self.assertFalse(is_caption(junk))
        self.assertTrue(is_caption("read a book with me"))

    def test_music_only_says_so_and_keeps_what_was_shown(self):
        from tools.transcribe_media import NO_SPEECH, with_screen_text
        text = with_screen_text(None, ["How much I read this week"])
        self.assertTrue(text.startswith("[No speech"))
        self.assertIn("On-screen text:\nHow much I read this week", text)
        self.assertEqual(with_screen_text(None, []), NO_SPEECH)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        path = os.path.join(self.folder, "7")
        os.makedirs(path)
        for name, content in (("video.mp4", MP4), ("poster.jpg", JPEG),
                              ("transcript.txt", b"Hello there\n")):
            with open(os.path.join(path, name), "wb") as handle:
                handle.write(content)
        with open(os.path.join(self.folder, "secret.db"), "wb") as handle:
            handle.write(b"x")

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_listing_reads_what_is_there(self):
        stored = media.listing(self.folder, 7)
        self.assertEqual(stored["video"], "video.mp4")
        self.assertEqual(stored["poster"], "poster.jpg")
        self.assertEqual(stored["transcript"], "Hello there")
        self.assertEqual(media.ids(self.folder), {7})
        self.assertEqual(media.thumbnail(self.folder, 7), "poster.jpg")

    def test_only_a_pieces_own_files_are_servable(self):
        self.assertIsNotNone(media.servable(self.folder, 7, "video.mp4"))
        for name in ("../secret.db", "secret.db", "source.json", "image-01.jpg"):
            with self.subTest(name=name):
                self.assertIsNone(media.servable(self.folder, 7, name))

    def test_a_piece_with_nothing_saved_has_nothing(self):
        stored = media.listing(self.folder, 99)
        self.assertIsNone(stored["video"])
        self.assertEqual(stored["images"], [])
        self.assertIsNone(media.thumbnail(self.folder, 99))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = os.path.join(self.root, "media")
        self.inbox = os.path.join(self.root, "media-inbox")
        os.makedirs(self.store)
        self.ctx = appmod.app.app_context()
        self.ctx.push()
        db.create_all()
        for model in (PlatformPost, UgcPiece, Campaign, Creator):
            db.session.query(model).delete()
        self.campaign = Campaign(name="Spring Pilot", slug="spring-pilot")
        bea = Creator(name="Bea Lune", email="bea@example.com")
        db.session.add_all([self.campaign, bea])
        db.session.flush()
        self.video = UgcPiece(creator_id=bea.id, campaign_id=self.campaign.id)
        self.slides = UgcPiece(creator_id=bea.id, campaign_id=self.campaign.id)
        db.session.add_all([self.video, self.slides])
        db.session.flush()
        db.session.add(PlatformPost(ugc_piece_id=self.video.id, platform="tiktok",
                                    url="https://www.tiktok.com/@bea/video/1"))
        db.session.add(PlatformPost(ugc_piece_id=self.slides.id, platform="tiktok",
                                    url="https://www.tiktok.com/@bea/photo/2"))
        db.session.commit()
        import_media.prepare(self.campaign, self.inbox)
        base = os.path.join(self.inbox, "spring-pilot")
        self.folders = {import_media.piece_id_of(n): os.path.join(base, n)
                        for n in os.listdir(base) if import_media.piece_id_of(n)}

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.root)

    def drop(self, piece, name, content):
        with open(os.path.join(self.folders[piece.id], name), "wb") as handle:
            handle.write(content)

    def plan(self):
        return import_media.plan(self.campaign, self.inbox, self.store)

    def test_prepare_makes_a_folder_per_piece_with_a_link_to_the_post(self):
        self.assertEqual(set(self.folders), {self.video.id, self.slides.id})
        self.assertIn("video", os.path.basename(self.folders[self.video.id]))
        self.assertIn("carousel", os.path.basename(self.folders[self.slides.id]))
        self.assertTrue(any(n.endswith(".webloc")
                            for n in os.listdir(self.folders[self.video.id])))

    def test_empty_folders_are_waiting_not_errors(self):
        result = self.plan()
        self.assertEqual(len(result["waiting"]), 2)
        self.assertEqual(result["import"], [])

    def test_slides_keep_the_order_they_were_saved_in(self):
        for name in ("IMG_10.jpg", "IMG_2.jpg", "IMG_9.png"):
            self.drop(self.slides, name, JPEG if name.endswith("jpg") else PNG)
        entry = self.plan()["import"][0]
        self.assertEqual([os.path.basename(p) for p in entry["images"]],
                         ["IMG_2.jpg", "IMG_9.png", "IMG_10.jpg"])

    def test_screenshots_sort_by_the_time_they_were_taken(self):
        names = ["Screenshot 2026-11-11 at 1.02.07 PM.png",
                 "Screenshot 2026-11-11 at 12.59.40 PM.png",
                 "Screenshot 2026-11-11 at 11.58.00 AM.png"]
        self.assertEqual(sorted(names, key=import_media.natural),
                         [names[2], names[1], names[0]])

    def test_two_videos_for_one_post_are_held_not_guessed(self):
        self.drop(self.video, "a.mp4", MP4)
        self.drop(self.video, "b.mov", MP4)
        result = self.plan()
        self.assertEqual(result["import"], [])
        self.assertIn("2 videos", result["problems"][0][1])

    def test_something_that_is_not_media_is_reported(self):
        self.drop(self.video, "post.html", HTML)
        self.assertIn("post.html", self.plan()["problems"][0][1])

    def test_slides_are_stored_in_order_and_re_running_is_a_no_op(self):
        self.drop(self.slides, "1.jpg", JPEG)
        self.drop(self.slides, "2.png", PNG)
        done, failures = import_media.apply(self.plan(), self.store)
        self.assertEqual((done, failures), (1, []))
        self.assertEqual(media.listing(self.store, self.slides.id)["images"],
                         ["image-01.jpg", "image-02.png"])
        again = self.plan()
        self.assertEqual(len(again["unchanged"]), 1)
        self.assertEqual(again["import"], [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_the_same_video_saved_for_two_pieces_is_held(self):
        """A reel and a TikTok of one video are one piece, not two cards."""
        clip = os.path.join(self.root, "clip.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=duration=3:size=64x64:rate=10", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", clip], check=True)
        for piece in (self.video, self.slides):
            shutil.copyfile(clip, os.path.join(self.folders[piece.id], "clip.mp4"))
        result = self.plan()
        self.assertEqual(len(result["import"]), 1)
        self.assertIn("probably one video posted twice", result["problems"][0][1])

        # Two different videos can share a length; once a person has checked,
        # the piece they name goes through — and only that one.
        held = int(result["problems"][0][0].split()[0])
        checked = import_media.plan(self.campaign, self.inbox, self.store,
                                    checked={held})
        self.assertEqual(len(checked["import"]), 2)
        self.assertEqual(checked["problems"], [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_video_is_stored_as_mp4_with_a_poster(self):
        source = os.path.join(self.root, "clip.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=duration=2:size=64x64:rate=10", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", source], check=True)
        shutil.copyfile(source, os.path.join(self.folders[self.video.id], "clip.mov"))
        done, failures = import_media.apply(self.plan(), self.store)
        self.assertEqual(failures, [])
        stored = media.listing(self.store, self.video.id)
        self.assertEqual((stored["video"], stored["poster"]), ("video.mp4", "poster.jpg"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        appmod.app.config["MEDIA_DIR"] = self.folder
        os.makedirs(os.path.join(self.folder, "3"))
        with open(os.path.join(self.folder, "3", "image-01.jpg"), "wb") as handle:
            handle.write(JPEG)
        self.client = appmod.app.test_client()

    def tearDown(self):
        appmod.app.config.pop("MEDIA_DIR", None)
        shutil.rmtree(self.folder)

    def test_media_is_behind_the_login(self):
        r = self.client.get("/ugc/3/media/image-01.jpg")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_a_stored_slide_is_served_and_nothing_else_is(self):
        self.client.post("/login", data={"password": "test-password"})
        r = self.client.get("/ugc/3/media/image-01.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, JPEG)
        r.close()
        self.assertEqual(self.client.get("/ugc/3/media/source.json").status_code, 404)
        self.assertEqual(self.client.get("/ugc/4/media/image-01.jpg").status_code, 404)


if __name__ == "__main__":
    unittest.main()
