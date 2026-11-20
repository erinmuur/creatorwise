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

    def test_different_lengths_are_different_videos(self):
        tt, ig = {"duration": 111.0}, {"duration": 127.9}
        self.assertEqual(len(cr.group_links([("tiktok", tt), ("instagram", ig)])), 2)
        # ...but a reel cut to Instagram's 3 minutes can be the same video.
        self.assertEqual(len(cr.group_links([("tiktok", {"duration": 229}),
                                             ("instagram", {"duration": 176})])), 1)

    def test_title_is_a_quoted_sentence_not_a_greeting(self):
        text = "Hi guys, welcome back to my channel.\nI read forty books this month and here is how I tracked them. More stuff."
        self.assertEqual(cr.pick_title(text),
                         "“I read forty books this month and here is how I tracked them”")
        self.assertIsNone(cr.pick_title("[No speech — music only]\n\nOn-screen text:\nshort"))

    def test_a_series_intro_already_used_is_skipped(self):
        text = ("I'm making it my mission to read more books this month.\n"
                "Today's day 15 of my thirty day reading challenge.")
        used = ["“I'm making it my mission to read more books this month”"]
        self.assertEqual(cr.pick_title(text, avoid=used),
                         "“Today's day 15 of my thirty day reading challenge”")


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
        (r,), _ = self.plan(msg(f"Sam's video for the Announcement {url}"))
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

    def test_an_unknown_author_is_a_new_creator_not_a_first_name_match(self):
        # The email says "Sam", one Sam is on file — but the post's account is
        # nobody's. That is a new person, not the Sam we have.
        tt = f"https://www.tiktok.com/@samthereader/video/{TT_ID}"
        (r,), _ = self.plan(
            msg(f"An organic plug from a user named Sam (sam.new@example.com) {tt} "
                "for the Announcement"),
            links={tt: {"author": "samthereader", "author_name": "Sam Reader 📚 | books",
                        "kind": "video"}})
        self.assertEqual(r["held"], [])
        (p,) = r["pieces"]
        self.assertIsNone(p["creator"])
        nc = p["new_creator"]
        self.assertEqual((nc["name"], nc["email"], nc["source"]),
                         ("Sam Reader", "sam.new@example.com", "organic"))
        self.assertEqual(nc["handles"], [{"platform": "tiktok", "handle": "samthereader"}])
        captures, new = cr.apply([r], MODELS, self.tmp)
        made = db.session.get(Creator, new[0]["creator"])
        self.assertEqual(made.email, "sam.new@example.com")
        self.assertEqual([h.handle for h in made.handles], ["samthereader"])
        self.assertEqual(db.session.get(UgcPiece, captures[0]["piece"]).creator_id, made.id)

    def test_two_new_posts_by_one_new_person_make_one_creator(self):
        tt = f"https://www.tiktok.com/@newreader/video/{TT_ID}"
        ig = "https://www.instagram.com/reel/AbC123/"
        results, _ = self.plan(
            msg(f"organic, new.reader@example.com, for the Announcement {tt} {ig}"),
            links={tt: {"author": "newreader", "author_name": "New Reader", "duration": 111},
                   ig: {"author": "new.reader", "author_name": "New Reader", "duration": 128}})
        self.assertEqual(len(results[0]["pieces"]), 2)
        captures, new = cr.apply(results, MODELS, self.tmp)
        self.assertEqual(len(new), 1)
        made = db.session.get(Creator, new[0]["creator"])
        self.assertEqual(sorted(h.handle for h in made.handles), ["new.reader", "newreader"])
        self.assertEqual({db.session.get(UgcPiece, c["piece"]).creator_id for c in captures},
                         {made.id})

    def test_a_new_creator_needs_an_address(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"organic plug for the Announcement {url}"),
                            links={url: {"author": "stranger", "author_name": "Stranger"}})
        self.assertIn("send it again with their email address", r["held"][0]["reason"])

    def test_a_full_name_already_on_file_is_held(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"new person x@example.com for the Announcement {url}"),
                            links={url: {"author": "otheraccount", "author_name": "Sam Vale"}})
        self.assertIn("already on file", r["held"][0]["reason"])

    def test_an_invite_code_makes_them_a_referral(self):
        url = "https://www.instagram.com/reel/AbC123/"
        payload_people = {"new.reader@example.com": {"invited_by": "bea.lune@example.com",
                                                     "username": "newreader"}}
        results, _ = cr.plan({"messages": [msg(f"organic, new.reader@example.com, "
                                                f"for the Announcement {url}")],
                              "links": {url: {"author": "newreader", "author_name": "New Reader"}},
                              "people": payload_people}, ME, self.state, MODELS)
        nc = results[0]["pieces"][0]["new_creator"]
        self.assertEqual((nc["source"], nc["recruited_by"]), ("referral", self.bea.id))

    def test_an_address_on_file_matches_and_adds_the_account(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"Bea's other account, bea.lune@example.com, for the "
                                f"Announcement {url}"),
                            links={url: {"author": "bea.second", "author_name": "Bea"}})
        (p,) = r["pieces"]
        self.assertEqual((p["creator"], p["matched_by"]), (self.bea.id, "email"))
        cr.apply([r], MODELS, self.tmp)
        self.assertIn("bea.second", [h.handle for h in self.bea.handles])

    def test_a_second_address_in_the_email_is_added(self):
        url = "https://www.instagram.com/reel/AbC123/"
        (r,), _ = self.plan(msg(f"Bea's post (bea.lune@example.com and bea.other@example.com) "
                                f"for the Announcement {url}"),
                            links={url: {"author": "bea.lune"}})
        (p,) = r["pieces"]
        self.assertEqual(p["add_emails"], ["bea.other@example.com"])
        cr.apply([r], MODELS, self.tmp)
        self.assertIn("bea.other@example.com", [e.address for e in self.bea.emails])

    def test_the_jobs_own_reply_is_never_a_request(self):
        reply = msg(f"{cr.JOB_MARK}\nHeld: no campaign named.", id="r1")
        results, ignored = self.plan(reply)
        self.assertEqual(results, [])
        self.assertEqual(ignored[0][1], "the job's own reply")

    def test_thread_mail_not_sent_to_the_tag_is_skipped(self):
        url = "https://www.instagram.com/reel/AbC123/"
        m = msg(f"Bea for the Announcement {url}")
        m["to"] = ["me@example.com"]
        results, ignored = self.plan(m, links={url: {"author": "bea.lune"}})
        self.assertEqual((results, ignored[0][1]), ([], "not sent to the tagged address"))
        m["to"] = ["me+creatorwise@example.com"]
        results, _ = self.plan(m, links={url: {"author": "bea.lune"}})
        self.assertEqual(len(results), 1)

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
        captures, _ = cr.apply(results, MODELS, self.tmp)
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
        (c,), _ = cr.apply(results, MODELS, self.tmp)
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
