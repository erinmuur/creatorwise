"""Content emailed in (tools/content_requests.py): matching an email to one
creator and one campaign, or holding it — never guessing."""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from inbox import extract_links
from models import (Campaign, Creator, Participation, PlatformPost, SocialHandle,
                    UgcPiece, db)
from tools import content_requests as cr

ME = "me@example.com"
MODELS = dict(db=db, Campaign=Campaign, Creator=Creator, Participation=Participation,
              PlatformPost=PlatformPost, UgcPiece=UgcPiece)

# A TikTok id whose top 32 bits are 1_795_000_000 s: 2026-11-18 UTC.
TT_ID = str(1_795_000_000 << 32)


def msg(body, subject="", id="m1", frm=ME, attachments=()):
    return {"id": id, "from": f"Me <{frm}>", "date": "2026-11-19", "subject": subject,
            "body": body, "attachments": list(attachments)}


class PureTests(unittest.TestCase):
    def test_every_instagram_form_names_the_same_post(self):
        for url in ("https://www.instagram.com/reel/AbC_12-x/",
                    "https://www.instagram.com/p/AbC_12-x/?igsh=abc",
                    "https://www.instagram.com/bea.lune/reel/AbC_12-x/"):
            self.assertEqual(cr.post_key(url), ("instagram", "AbC_12-x"))

    def test_tiktok_needs_its_id(self):
        self.assertEqual(cr.post_key(f"https://www.tiktok.com/@bea/video/{TT_ID}?_r=1"),
                         ("tiktok", TT_ID))
        self.assertIsNone(cr.post_key("https://www.tiktok.com/t/ZT8abc/"))

    def test_handle_first_links_are_posts_not_profiles(self):
        found = extract_links("https://www.instagram.com/bea.lune/reel/AbC123/ and "
                              f"https://www.tiktok.com/@bea/photo/{TT_ID} and "
                              "https://www.instagram.com/bea.lune/")
        self.assertEqual([p for p, _ in found], ["instagram", "tiktok"])

    def test_campaign_is_named_or_held(self):
        camps = [Campaign(name="Readwise Books Announcement", slug="books-announcement"),
                 Campaign(name="Bookwise Softlaunch", slug="bookwise-softlaunch"),
                 Campaign(name="Organic Plugs", slug="organic-plugs")]
        c, _ = cr.find_campaign("Bea's second piece for the Announcement", camps)
        self.assertEqual(c.slug, "books-announcement")
        c, _ = cr.find_campaign("from the soft launch", camps)
        self.assertEqual(c.slug, "bookwise-softlaunch")
        c, why = cr.find_campaign("Bea's new video", camps)
        self.assertIsNone(c)
        self.assertIn("no campaign", why)

    def test_cross_post_only_when_one_link_per_platform(self):
        self.assertEqual(len(cr.group_links([("tiktok", 1), ("instagram", 2)])), 1)
        self.assertEqual(len(cr.group_links([("tiktok", 1), ("tiktok", 2)])), 2)

    def test_title_is_a_quoted_sentence_not_a_greeting(self):
        text = "Hi guys, welcome back to my channel.\nI read forty books this month and here is how I tracked them. More stuff."
        self.assertEqual(cr.pick_title(text),
                         "“I read forty books this month and here is how I tracked them”")
        self.assertIsNone(cr.pick_title("[No speech — music only]\n\nOn-screen text:\nshort"))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ann = Campaign(name="Readwise Books Announcement", slug="books-announcement")
        self.soft = Campaign(name="Bookwise Softlaunch", slug="bookwise-softlaunch")
        db.session.add_all([self.ann, self.soft])
        self.bea = self.creator("Bea Lune", "bea.lune", tiktok="bealune")
        self.sam1 = self.creator("Sam Quill", "samquill")
        self.sam2 = self.creator("Sam Vale", "samvale.reads")
        self.duo_a = self.creator("Pax Hart", "duo.channel")
        self.duo_b = self.creator("Juno Hart", "duo.channel")
        for c in (self.bea, self.sam1, self.sam2):
            db.session.add(Participation(creator_id=c.id, campaign_id=self.ann.id,
                                         status="opted_in"))
        db.session.commit()
        self.state = {"messages": {}}
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def creator(self, name, ig, tiktok=None):
        c = Creator(name=name, email=f"{name.lower().replace(' ', '.')}@example.com")
        db.session.add(c)
        db.session.flush()
        db.session.add(SocialHandle(creator_id=c.id, platform="instagram", handle=ig))
        if tiktok:
            db.session.add(SocialHandle(creator_id=c.id, platform="tiktok", handle=tiktok))
        db.session.flush()
        return c

    def plan(self, *messages, links=None):
        payload = {"messages": list(messages), "links": links or {}}
        return cr.plan(payload, ME, self.state, MODELS)

    def test_the_handle_on_the_post_decides(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"Bea's second piece for the Announcement {url}"),
                            links={url: {"author": "bea.lune", "kind": "video"}})
        self.assertEqual(r["held"], [])
        (p,) = r["pieces"]
        self.assertEqual((p["creator"], p["matched_by"], p["campaign"]),
                         (self.bea.id, "handle", "books-announcement"))

    def test_a_first_name_alone_is_held_when_it_fits_two(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"Sam's video for the Announcement {url}"),
                            links={url: {"author": "someone.else"}})
        self.assertEqual(r["pieces"], [])
        self.assertIn("more than one creator", r["held"][0]["reason"])

    def test_the_email_naming_someone_else_is_held(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"Sam Quill's piece for the Announcement {url}"),
                            links={url: {"author": "bea.lune"}})
        self.assertIn("names Sam Quill", r["held"][0]["reason"])

    def test_a_shared_account_needs_a_name(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"new one for the Announcement {url}"),
                            links={url: {"author": "duo.channel"}})
        self.assertIn("more than one creator", r["held"][0]["reason"])
        (r,), _ = self.plan(msg(f"Juno's new one for the Announcement {url}", id="m2"),
                            links={url: {"author": "duo.channel"}})
        self.assertEqual(r["pieces"][0]["creator"], self.duo_b.id)

    def test_mail_from_anyone_else_is_ignored(self):
        url = "https://www.instagram.com/reel/AbC123/"
        results, ignored = self.plan(msg(f"for the Announcement {url}",
                                         frm="stranger@example.com"))
        self.assertEqual((results, len(ignored)), ([], 1))

    def test_a_post_already_on_file_is_not_added_twice(self):
        piece = UgcPiece(creator_id=self.bea.id, campaign_id=self.ann.id)
        db.session.add(piece)
        db.session.flush()
        db.session.add(PlatformPost(ugc_piece_id=piece.id, platform="instagram",
                                    url="https://www.instagram.com/bea.lune/reel/AbC123/"))
        db.session.commit()
        url = "https://www.instagram.com/p/AbC123/"
        (r,), _ = self.plan(msg(f"Bea for the Announcement {url}"),
                            links={url: {"author": "bea.lune"}})
        self.assertEqual(r["pieces"][0]["action"], "already on file")

    def test_apply_adds_the_piece_and_marks_delivery(self):
        tt = f"https://www.tiktok.com/@bealune/video/{TT_ID}?_r=1"
        ig = "https://www.instagram.com/reel/AbC123/?igsh=x"
        results, ignored = self.plan(
            msg(f"Bea's cross-post for the Announcement\n{tt}\n{ig}"),
            links={tt: {"author": "bealune", "kind": "video"},
                   ig: {"author": "bea.lune", "kind": "video"}})
        (p,) = results[0]["pieces"]
        self.assertEqual(p["action"], "new piece")
        captures = cr.apply(results, MODELS, self.tmp)
        (c,) = captures
        self.assertEqual(c["method"], "tiktok-download")
        self.assertTrue(os.path.isdir(c["folder"]))
        piece = db.session.get(UgcPiece, c["piece"])
        self.assertEqual(piece.discovered_via, "email")
        self.assertEqual(sorted(x.url for x in piece.posts),
                         ["https://www.instagram.com/reel/AbC123/",
                          f"https://www.tiktok.com/@bealune/video/{TT_ID}"])
        self.assertEqual(piece.posted_on, date(2026, 11, 18))
        part = Participation.query.filter_by(creator_id=self.bea.id,
                                             campaign_id=self.ann.id).one()
        self.assertEqual(part.status, "content_delivered")

    def test_a_creator_new_to_the_campaign_joins_it_as_delivered(self):
        url = "https://www.instagram.com/p/ZZ999/"
        results, _ = self.plan(msg(f"Bea's carousel for the softlaunch {url}"),
                               links={url: {"author": "bea.lune", "kind": "carousel"}})
        (c,) = cr.apply(results, MODELS, self.tmp)
        self.assertEqual(c["method"], "instagram-slides")
        self.assertEqual(results[0]["pieces"][0]["status_change"],
                         "added to the campaign as delivered")

    def test_processed_mail_is_not_read_again(self):
        url = "https://www.instagram.com/reel/AbC123/"
        results, ignored = self.plan(msg(f"Bea for the Announcement {url}"),
                                     links={url: {"author": "bea.lune"}})
        cr.apply(results, MODELS, self.tmp)
        cr.record_processed(self.tmp, results, ignored, self.state)
        again, _ = self.plan(msg(f"Bea for the Announcement {url}"),
                             links={url: {"author": "bea.lune"}})
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main()
