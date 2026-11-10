"""Content/campaign importer tests.

The fixtures are the shapes this export actually has: a profile column and a
post column that disagree, two post links in one cell, a TikTok shortlink with
no id in it, an Instagram permalink with no handle in it, a channel name in the
creator column, and a creator who only ever opted in.

Every name here is invented and every address is @example.com — the real list
does not go in this repo.
"""

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import content_importer as ci
from models import (
    REPORTED_WINDOW,
    Campaign,
    Creator,
    CreatorEmail,
    Participation,
    PlatformPost,
    ReferralSnapshot,
    SocialHandle,
    UgcPiece,
    db,
)


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def tiktok_url(handle, when, kind="video"):
    """A TikTok post URL whose id really does encode `when`."""
    moment = datetime(when.year, when.month, when.day, 12, 0, tzinfo=timezone.utc)
    post_id = (int(moment.timestamp()) << 32) | 0x1234ABCD
    return f"https://www.tiktok.com/@{handle}/{kind}/{post_id}"


class UrlTests(unittest.TestCase):
    def test_platform_from_host(self):
        cases = {
            "https://www.instagram.com/p/ABC/": "instagram",
            "https://www.tiktok.com/@bea/video/7": "tiktok",
            "https://www.youtube.com/@bea": "youtube",
            "https://youtu.be/abc123": "youtube",
            "https://www.threads.com/@bea": "threads",
            "https://x.com/bea": "x",
            "https://example.com/bea": None,
            "": None,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(ci.platform_for(url), expected)

    def test_handle_out_of_a_url(self):
        cases = {
            "https://www.tiktok.com/@bea.reads": "bea.reads",
            "https://www.tiktok.com/@bea?_t=ZT-90Fd&_r=1": "bea",
            "https://www.tiktok.com/@bea/video/7566328526333381910": "bea",
            "https://www.youtube.com/@BeaReadsBooks": "beareadsbooks",
            "https://www.instagram.com/thecassianvale": "thecassianvale",
            "https://www.instagram.com/thecassianvale/": "thecassianvale",
            "https://x.com/bealune": "bealune",
            "https://www.instagram.com/p/DQWpCvCkz88/?hl=en": None,
            "https://www.instagram.com/reel/DQWp/": None,
            "https://www.tiktok.com/t/ZP8fJ4nBg/": None,
            "https://www.youtube.com/channel/UC123": None,
            # A bare first segment is a video id on these, not a handle.
            "https://youtu.be/abc123": None,
            "https://www.tiktok.com/discover/books": None,
            "https://example.com/bealune": None,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(ci.handle_in(url), expected)

    def test_tiktok_id_carries_the_date(self):
        for when in (date(2025, 10, 13), date(2025, 12, 8), date(2026, 2, 23)):
            with self.subTest(when=when):
                self.assertEqual(ci.tiktok_posted_on(tiktok_url("bea", when)), when)

    def test_a_photo_post_dates_the_same_way(self):
        when = date(2025, 10, 13)
        self.assertEqual(
            ci.tiktok_posted_on(tiktok_url("bea", when, kind="photo")), when
        )

    def test_no_date_where_there_is_no_id(self):
        for url in (
            "https://www.tiktok.com/t/ZP8fJ4nBg/",            # shortlink
            "https://www.instagram.com/p/DQWpCvCkz88/?hl=en",  # no id scheme
            "https://www.tiktok.com/@bea/video/123",           # too short to be one
            "",
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(ci.tiktok_posted_on(url))

    def test_an_implausible_id_is_not_a_date(self):
        """A 19-digit number that isn't a post id would give 1970 or 2200."""
        self.assertIsNone(
            ci.tiktok_posted_on("https://www.tiktok.com/@bea/video/1000000000000000000")
        )

    def test_two_links_in_one_cell(self):
        cell = (
            "https://www.tiktok.com/@bea/photo/7560774199418064141\n"
            "https://www.tiktok.com/@bea/video/7566758296229596429"
        )
        self.assertEqual(len(ci.split_urls(cell)), 2)

    def test_urls_are_deduplicated_and_trimmed(self):
        cell = "(https://www.tiktok.com/@bea), https://www.tiktok.com/@bea"
        self.assertEqual(ci.split_urls(cell), ["https://www.tiktok.com/@bea"])


class StatusTests(unittest.TestCase):
    """An outreach sheet records two columns; neither alone is the answer."""

    def test_the_status_column_wins_when_it_is_definite(self):
        self.assertEqual(ci.derive_status("Posted", "Scheduled Call", True),
                         ("content_delivered", True))
        self.assertEqual(ci.derive_status("Briefed", "Scheduled Call", False),
                         ("briefed", True))

    def test_the_response_column_fills_in_the_rest(self):
        cases = {
            ("Not Yet Briefed", "No reply"): "contacted",
            ("Not Yet Briefed", "Declined"): "declined",
            ("Not Yet Briefed", "Volunteered for Wave 2"): "wave_2",
            ("Missed Call", "Bumped to Wave 2"): "wave_2",
            ("Not Yet Briefed", "Opted-in"): "opted_in",
            ("Not Yet Briefed", "Extra enthusiastic"): "opted_in",
            ("Not Yet Briefed", "Asked for More Time"): "opted_in",
            ("Not Yet Briefed", "Follow-up needed"): "opted_in",
        }
        for (status, response), expected in cases.items():
            with self.subTest(status=status, response=response):
                self.assertEqual(
                    ci.derive_status(status, response, False), (expected, True)
                )

    def test_a_sheet_whose_best_word_is_opted_in_still_finds_the_posters(self):
        """The announcement outreach has no "Posted" value at all — its
        vocabulary tops out at "Opted In", so 42 creators with content links
        would otherwise all read as not delivered."""
        self.assertEqual(ci.derive_status("Opted In", "", True),
                         ("content_delivered", True))
        self.assertEqual(ci.derive_status("Opted In", "", False),
                         ("opted_in", True))
        self.assertEqual(ci.derive_status("Expressed Interest", "", True),
                         ("content_delivered", True))

    def test_the_stages_before_a_reply(self):
        cases = {
            "Contacted": "contacted",
            "Not Contacted": "not_contacted",
            "Declined": "declined",
        }
        for cell, expected in cases.items():
            with self.subTest(cell=cell):
                self.assertEqual(ci.derive_status(cell, "", False),
                                 (expected, True))

    def test_a_wave_2_creator_who_posted_is_still_wave_2(self):
        """Content doesn't override what somebody wrote down; it's reported."""
        self.assertEqual(
            ci.derive_status("Not Yet Briefed", "Volunteered for Wave 2", True),
            ("wave_2", True),
        )

    def test_a_sheet_with_no_usable_status_falls_back_to_the_links(self):
        for cell in ("Accepted, Onboarded", "Book Read", "", None):
            with self.subTest(cell=cell):
                self.assertEqual(ci.derive_status(cell, "", True),
                                 ("content_delivered", False))
                self.assertEqual(ci.derive_status(cell, "", False),
                                 ("opted_in", False))

    def test_referral_counts(self):
        self.assertEqual(ci.parse_referrals("63"), 63)
        self.assertEqual(ci.parse_referrals("1,204"), 1204)
        for junk in ("", "—", "n/a", "lots", None, "3.5"):
            with self.subTest(junk=junk):
                self.assertIsNone(ci.parse_referrals(junk))


class MatchingTests(unittest.TestCase):
    """The ladder: one uncontradicted strong signal, or nothing."""

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.bea = self._creator("Beatrice Lune", "bea@example.com",
                                 [("tiktok", "@bealune"), ("instagram", "@bea.lune")])
        self.cass = self._creator("Cassian Vale", "cass@example.com",
                                  [("instagram", "@thecassianvale")])
        self.bea_other = self._creator("Bea Hollis", "hollis@example.com",
                                       [("tiktok", "@beahollis")])
        self._creator("Hollis Grant", "grant@example.com",
                      [("tiktok", "@hollisgrant")])
        self.index = ci.CreatorIndex(Creator.query.all())

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _creator(name, email, handles):
        creator = Creator(name=name, email=email)
        db.session.add(creator)
        db.session.flush()
        for platform, handle in handles:
            db.session.add(SocialHandle(
                creator_id=creator.id, platform=platform, handle=handle
            ))
        db.session.flush()
        return creator

    def test_handle_on_its_own_platform_is_enough(self):
        cid, note, warnings = self.index.match("Totally Different Name", "tiktok",
                                               "bealune", [])
        self.assertEqual(cid, self.bea.id)
        self.assertIn("on tiktok", note)
        self.assertEqual(warnings, [])

    def test_exact_name_on_its_own_is_enough(self):
        cid, _, _ = self.index.match("Cassian Vale", None, None, [])
        self.assertEqual(cid, self.cass.id)

    def test_a_handle_from_another_platform_is_not_enough(self):
        """@thecassianvale is Cassian's Instagram; a TikTok link of that name
        is not evidence the TikTok account is his."""
        cid, _, warnings = self.index.match(
            "Cass Reads", "tiktok", "cassreads", [("tiktok", "thecassianvale")]
        )
        self.assertIsNone(cid)
        self.assertIn("only weak evidence", warnings[0])

    def test_a_partial_name_on_its_own_is_not_enough(self):
        cid, _, warnings = self.index.match("Bea", None, None, [])
        self.assertIsNone(cid)
        self.assertIn("only weak evidence", warnings[0])

    def test_post_handle_plus_partial_name_resolves(self):
        """A profile column can be stale while the post link is right."""
        cid, note, _ = self.index.match(
            "Beatrice", "tiktok", "beapeaches", [("tiktok", "bealune")]
        )
        self.assertEqual(cid, self.bea.id)
        self.assertIn("post handle", note)

    def test_conflicting_strong_signals_are_refused(self):
        cid, _, warnings = self.index.match("Cassian Vale", "tiktok", "bealune", [])
        self.assertIsNone(cid)
        self.assertIn("conflicting matches", warnings[0])

    def test_an_address_resolves_a_row_on_its_own(self):
        cid, note, _ = self.index.match("Totally Wrong", None, None, [],
                                        email="bea@example.com")
        self.assertEqual(cid, self.bea.id)
        self.assertIn("email bea@example.com", note)

    def test_an_address_beats_a_name_that_points_elsewhere(self):
        """Two people can share a name; Creator.email is unique."""
        cid, _, warnings = self.index.match("Cassian Vale", None, None, [],
                                            email="bea@example.com")
        self.assertEqual(cid, self.bea.id)
        self.assertTrue(any("matched on email" in w and "Cassian Vale" in w
                            for w in warnings))

    def test_an_unknown_address_is_not_a_signal(self):
        cid, _, _ = self.index.match("Cassian Vale", None, None, [],
                                     email="nobody@example.com")
        self.assertEqual(cid, self.cass.id)

    def test_nothing_at_all(self):
        cid, _, warnings = self.index.match("Nobody Here", "tiktok", "nobody", [])
        self.assertIsNone(cid)
        self.assertIn("no creator matches", warnings[0])

    def test_an_exact_name_is_not_muddied_by_a_shared_surname(self):
        """Bea Hollis and Hollis Grant share a word. That is not a near miss."""
        cid, note, warnings = self.index.match("Bea Hollis", None, None, [])
        self.assertEqual(cid, self.bea_other.id)
        self.assertEqual(warnings, [])
        self.assertNotIn("Hollis Grant", note)

    def test_a_strong_match_still_mentions_weaker_near_misses(self):
        """A trusted profile link, and a post link from somebody else's account."""
        cid, _, warnings = self.index.match(
            "Bea L", "tiktok", "bealune", [("tiktok", "thecassianvale")]
        )
        self.assertEqual(cid, self.bea.id)
        self.assertTrue(any("also pointed at" in w and "Cassian Vale" in w
                            for w in warnings))


class PlanTests(unittest.TestCase):
    CSV = (
        "Creator Name,Boox status,Status,Indexed ,Handle/Profile URL,Referrer,Post\n"
        "Beatrice Lune,Delivered,\"Accepted, Onboarded\",Yes,"
        "https://www.tiktok.com/@bealune,,"
        + tiktok_url("bealune", date(2025, 10, 28)) + "\n"
        "Cass Vale,,Onboarded,No,https://www.instagram.com/thecassianvale,Beatrice Lune,"
        "https://www.instagram.com/p/DQWpCvCkz88/?hl=en\n"
        "Fern Okafor,,\"Accepted, Onboarded\",No,https://www.tiktok.com/@fernreads,,\n"
    )

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        for name, email, handles in (
            ("Beatrice Lune", "bea@example.com", [("tiktok", "@bealune")]),
            ("Cassian Vale", "cass@example.com", [("instagram", "@thecassianvale")]),
            ("Fern Okafor", "fern@example.com", [("tiktok", "@fernreads")]),
        ):
            creator = Creator(name=name, email=email)
            db.session.add(creator)
            db.session.flush()
            for platform, handle in handles:
                db.session.add(SocialHandle(
                    creator_id=creator.id, platform=platform, handle=handle
                ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def plan(self, csv_text=None, **kwargs):
        kwargs.setdefault("campaign_slug", "stickers")
        kwargs.setdefault("campaign_name", "Stickers Beta")
        return ci.plan(csv_text or self.CSV, **kwargs)

    # --- the campaign ---------------------------------------------------------

    def test_a_missing_campaign_is_created_not_assumed(self):
        result = self.plan()
        self.assertEqual(result.campaign_action, "create")
        self.assertIsNone(result.campaign)
        ci.apply(result)
        self.assertEqual(Campaign.query.filter_by(slug="stickers").count(), 1)

    def test_an_existing_campaign_is_reused(self):
        db.session.add(Campaign(name="Stickers Beta", slug="stickers"))
        db.session.commit()
        result = self.plan()
        self.assertEqual(result.campaign_action, "existing")
        ci.apply(result)
        self.assertEqual(Campaign.query.count(), 1)

    def test_an_unknown_slug_without_a_name_is_refused(self):
        result = ci.plan(self.CSV, campaign_slug="nope")
        self.assertFalse(result.ok)
        self.assertIn("Pass --campaign-name", result.error)
        self.assertIn("CANNOT IMPORT", ci.render(result))

    def test_no_campaign_at_all_is_refused(self):
        result = ci.plan(self.CSV)
        self.assertFalse(result.ok)
        self.assertIn("no campaign given", result.error)

    def test_a_file_without_the_right_columns_is_refused(self):
        result = ci.plan("Colour,Shape\nred,round\n", campaign_slug="stickers",
                         campaign_name="Stickers Beta")
        self.assertFalse(result.ok)
        self.assertIn("no creator name or profile column", result.error)

    # --- statuses and pieces --------------------------------------------------

    def test_a_post_means_delivered_and_no_post_means_opted_in(self):
        rows = {r.creator_name: r for r in self.plan().rows}
        self.assertEqual(rows["Beatrice Lune"].status, "content_delivered")
        self.assertEqual(rows["Cassian Vale"].status, "content_delivered")
        self.assertEqual(rows["Fern Okafor"].status, "opted_in")

    def test_apply_writes_participations_and_pieces(self):
        summary = ci.apply(self.plan())
        self.assertEqual(summary["linked"], 3)
        self.assertEqual(summary["pieces"], 2)
        self.assertEqual(Participation.query.count(), 3)
        self.assertEqual(UgcPiece.query.count(), 2)
        self.assertEqual(PlatformPost.query.count(), 2)

    def test_the_tiktok_date_lands_on_the_piece_and_the_post(self):
        ci.apply(self.plan())
        piece = UgcPiece.query.join(PlatformPost).filter(
            PlatformPost.platform == "tiktok"
        ).one()
        self.assertEqual(piece.posted_on, date(2025, 10, 28))
        self.assertEqual(piece.posts[0].posted_on, date(2025, 10, 28))

    def test_an_instagram_permalink_imports_without_a_date(self):
        ci.apply(self.plan())
        post = PlatformPost.query.filter_by(platform="instagram").one()
        self.assertIsNone(post.posted_on)
        self.assertIn("instagram.com/p/", post.url)

    def test_no_engagement_numbers_are_invented(self):
        ci.apply(self.plan())
        for post in PlatformPost.query.all():
            self.assertEqual((post.views, post.likes, post.comments), (0, 0, 0))
        for piece in UgcPiece.query.all():
            self.assertEqual(piece.referrals, 0)

    def test_pieces_are_marked_as_coming_from_the_export(self):
        ci.apply(self.plan())
        self.assertEqual(
            {p.discovered_via for p in UgcPiece.query.all()}, {"crm_export"}
        )

    def test_two_links_in_one_cell_become_two_pieces(self):
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Beatrice Lune,https://www.tiktok.com/@bealune,\""
            + tiktok_url("bealune", date(2025, 10, 13), kind="photo") + "\n"
            + tiktok_url("bealune", date(2025, 10, 29)) + "\"\n"
        )
        ci.apply(self.plan(csv_text))
        self.assertEqual(UgcPiece.query.count(), 2)
        self.assertEqual(
            sorted(p.posted_on for p in UgcPiece.query.all()),
            [date(2025, 10, 13), date(2025, 10, 29)],
        )

    def test_a_post_on_an_unmeasured_platform_is_skipped(self):
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Beatrice Lune,https://www.tiktok.com/@bealune,https://www.threads.com/@bealune/post/1\n"
        )
        result = self.plan(csv_text)
        row = result.rows[0]
        self.assertEqual(row.posts[0].action, "skipped")
        self.assertIn("isn't a measured platform", row.posts[0].reason)
        self.assertEqual(row.new_posts, [])
        self.assertIn("POST LINKS SKIPPED", ci.render(result))

    def test_an_unrecognized_host_is_skipped_not_guessed(self):
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Beatrice Lune,https://www.tiktok.com/@bealune,https://vimeo.com/12345\n"
        )
        row = self.plan(csv_text).rows[0]
        self.assertEqual(row.posts[0].reason, "unrecognized host")

    # --- re-running -----------------------------------------------------------

    def test_running_it_twice_changes_nothing(self):
        ci.apply(self.plan())
        before = (Participation.query.count(), UgcPiece.query.count())

        again = self.plan()
        self.assertEqual(again.counts["unchanged"], 3)
        self.assertEqual(again.counts["pieces"], 0)
        ci.apply(again)
        self.assertEqual((Participation.query.count(), UgcPiece.query.count()), before)

    def test_a_new_post_link_on_a_later_run_is_an_update(self):
        ci.apply(self.plan())
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Fern Okafor,https://www.tiktok.com/@fernreads,"
            + tiktok_url("fernreads", date(2025, 11, 2)) + "\n"
        )
        result = self.plan(csv_text)
        row = result.rows[0]
        self.assertEqual(row.action, "update")
        self.assertEqual(row.status_change, ("opted_in", "content_delivered"))

        summary = ci.apply(result)
        self.assertEqual(summary["status_changed"], 1)
        self.assertEqual(summary["pieces"], 1)
        fern = Creator.query.filter_by(name="Fern Okafor").one()
        self.assertEqual(fern.participations[0].status, "content_delivered")

    def test_the_same_creator_twice_in_one_file_is_refused(self):
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Beatrice Lune,https://www.tiktok.com/@bealune,\n"
            "Beatrice Lune,https://www.tiktok.com/@bealune,\n"
        )
        result = self.plan(csv_text)
        self.assertEqual(result.rows[0].action, "link")
        self.assertEqual(result.rows[1].action, "review")
        self.assertIn("also on line 2", result.rows[1].reason)

    def test_a_failure_rolls_the_whole_file_back(self):
        result = self.plan()
        result.rows[0].creator_id = None  # force a NOT NULL violation
        with self.assertRaises(Exception):
            ci.apply(result)
        db.session.rollback()
        self.assertEqual(Participation.query.count(), 0)
        self.assertEqual(UgcPiece.query.count(), 0)

    # --- what the report has to say -------------------------------------------

    def test_the_dry_run_count_is_what_apply_writes(self):
        """A row held for review carries post links that are not imported, and
        promising them in the dry run is how you stop trusting the dry run."""
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Beatrice Lune,https://www.tiktok.com/@bealune,"
            + tiktok_url("bealune", date(2025, 10, 28)) + "\n"
            "Cass Reads,https://www.tiktok.com/@cassreads,"
            "https://www.tiktok.com/@thecassianvale/video/7566328526333381910\n"
        )
        result = self.plan(csv_text)
        self.assertEqual(result.counts["review"], 1)
        self.assertEqual(result.counts["pieces"], 1)
        self.assertIn("1 content pieces to create", ci.render(result))

        summary = ci.apply(result)
        self.assertEqual(summary["pieces"], result.counts["pieces"])
        self.assertEqual(UgcPiece.query.count(), 1)

    def test_a_held_row_shows_what_it_nearly_matched(self):
        """A row whose address matched one creator while its name points at
        another usually means the sheet has the wrong address on that line.
        That near-miss is the most useful thing on the page, so it has to
        print — the warnings section skips held rows on purpose."""
        csv_text = (
            "Creator Name,Email,Handle/Profile URL,Post\n"
            "Beatrice Lune,bea@example.com,https://www.tiktok.com/@bealune,\n"
            "Cassian,bea@example.com,,\n"
        )
        result = self.plan(csv_text)
        held = result.of("review")[0]
        self.assertEqual(held.line, 3)
        text = ci.render(result)
        self.assertIn("matched on: email bea@example.com", text)
        self.assertIn("note:", text)
        self.assertIn("Cassian Vale", text)
        self.assertIn("email:      bea@example.com", text)

    def test_an_unmatchable_row_is_reported_not_guessed(self):
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Cass Reads,https://www.tiktok.com/@cassreads,"
            "https://www.tiktok.com/@thecassianvale/video/7566328526333381910\n"
        )
        result = self.plan(csv_text)
        self.assertEqual(result.rows[0].action, "review")
        text = ci.render(result)
        self.assertIn("NEEDS REVIEW", text)
        self.assertIn("only weak evidence", text)
        ci.apply(result)
        self.assertEqual(Participation.query.count(), 0)

    def test_a_stale_profile_column_is_warned_about_not_dropped(self):
        csv_text = (
            "Creator Name,Handle/Profile URL,Post\n"
            "Beatrice Lune,https://www.tiktok.com/@beapeaches,"
            + tiktok_url("bealune", date(2025, 10, 28)) + "\n"
        )
        result = self.plan(csv_text)
        row = result.rows[0]
        self.assertEqual(row.action, "link")
        self.assertTrue(any("profile says @beapeaches" in w for w in row.warnings))
        self.assertIn("WARNINGS", ci.render(result))

    def test_a_missing_date_is_said_out_loud(self):
        result = self.plan()
        row = {r.creator_name: r for r in result.rows}["Cassian Vale"]
        self.assertTrue(any("no date available" in w for w in row.warnings))
        self.assertIn("1 without one", ci.render(result))

    def test_columns_we_do_not_import_are_reported_with_their_values(self):
        result = self.plan()
        self.assertEqual(sorted(result.unmapped), ["Boox status", "Indexed"])
        text = ci.render(result, verbose=True)
        self.assertIn("'Boox status' — 1 row(s) have a value", text)
        self.assertIn("VALUES IN NOT-IMPORTED COLUMNS", text)
        self.assertIn("Boox status='Delivered'", text)

    def test_the_report_names_the_campaign_it_would_create(self):
        self.assertIn("Campaign to CREATE: 'Stickers Beta'", ci.render(self.plan()))


class CrossPostTests(unittest.TestCase):
    """One creative on three platforms is one piece. Four reels are four."""

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        creator = Creator(name="Beatrice Lune", email="bea@example.com")
        db.session.add(creator)
        db.session.flush()
        db.session.add(SocialHandle(
            creator_id=creator.id, platform="tiktok", handle="@bealune"
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def plan(self, content):
        csv_text = (
            'Creator,Email,Content\n'
            'Beatrice Lune,bea@example.com,"' + content + '"\n'
        )
        return ci.plan(csv_text, campaign_slug="stickers",
                       campaign_name="Stickers Beta")

    def test_one_link_per_platform_is_one_cross_posted_piece(self):
        result = self.plan(
            tiktok_url("bealune", date(2025, 11, 2)) + "\n"
            "https://www.instagram.com/reel/DcKBJHjC/\n"
            "https://www.youtube.com/shorts/M8pO8xk2IJs"
        )
        self.assertEqual(len(result.rows[0].groups), 1)
        self.assertEqual(result.counts["pieces"], 1)
        self.assertEqual(result.counts["posts"], 3)

        ci.apply(result)
        piece = UgcPiece.query.one()
        self.assertEqual(sorted(piece.platforms),
                         ["instagram", "tiktok", "youtube"])
        self.assertTrue(piece.is_cross_posted)
        # The date comes from the one link that carries one.
        self.assertEqual(piece.posted_on, date(2025, 11, 2))

    def test_four_posts_on_one_platform_are_four_pieces(self):
        result = self.plan("\n".join(
            f"https://www.instagram.com/reel/DcKBJHj{n}/" for n in "ABCD"
        ))
        self.assertEqual(result.counts["pieces"], 4)
        ci.apply(result)
        self.assertEqual(UgcPiece.query.count(), 4)

    def test_two_each_on_two_platforms_are_not_paired_up(self):
        """Which Reel goes with which TikTok isn't in the export, so no guess."""
        result = self.plan(
            "https://www.instagram.com/reel/DcKBJHjA/\n"
            "https://www.instagram.com/reel/DcKBJHjB/\n"
            + tiktok_url("bealune", date(2025, 11, 2)) + "\n"
            + tiktok_url("bealune", date(2025, 11, 9))
        )
        self.assertEqual(result.counts["pieces"], 4)
        for group in result.rows[0].groups:
            self.assertEqual(len(group), 1)

    def test_two_shortlinks_run_together_are_two_links(self):
        """The sheet has a cell holding two URLs with no separator at all."""
        result = self.plan(
            "https://www.tiktok.com/t/ZTUXgWL9Q/https://www.tiktok.com/t/ZTUXgaWoP/"
        )
        self.assertEqual(result.counts["posts"], 2)
        self.assertEqual(result.counts["pieces"], 2)


class ReferralTests(unittest.TestCase):
    """A referral count copied from a sheet is not a 14-day measurement."""

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        creator = Creator(name="Beatrice Lune", email="bea@example.com")
        db.session.add(creator)
        db.session.flush()
        db.session.add(SocialHandle(
            creator_id=creator.id, platform="tiktok", handle="@bealune"
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def plan(self, content, referrals):
        csv_text = (
            'Creator,Email,Referrals,Content\n'
            f'Beatrice Lune,bea@example.com,{referrals},"{content}"\n'
        )
        return ci.plan(csv_text, campaign_slug="stickers",
                       campaign_name="Stickers Beta")

    def test_a_count_is_stored_as_reported_not_as_a_window(self):
        ci.apply(self.plan(tiktok_url("bealune", date(2025, 11, 2)), 63))
        snapshot = ReferralSnapshot.query.one()
        self.assertEqual(snapshot.referrals, 63)
        self.assertEqual(snapshot.window_days, REPORTED_WINDOW)
        self.assertEqual(snapshot.window_label, "As reported")

        piece = UgcPiece.query.one()
        self.assertEqual(piece.referrals, 63)
        self.assertFalse(piece.has_window_referrals)

    def test_a_row_total_lands_on_the_earliest_piece_and_says_so(self):
        result = self.plan(
            tiktok_url("bealune", date(2025, 11, 9)) + "\n"
            + tiktok_url("bealune", date(2025, 11, 2)),
            8,
        )
        self.assertTrue(any("row total" in w for w in result.rows[0].warnings))
        ci.apply(result)
        self.assertEqual(ReferralSnapshot.query.count(), 1)
        holder = UgcPiece.query.join(ReferralSnapshot).one()
        self.assertEqual(holder.posted_on, date(2025, 11, 2))
        # The creator total is still exactly what the sheet said.
        self.assertEqual(Creator.query.one().total_referrals, 8)

    def test_referrals_with_no_content_are_reported_not_dropped(self):
        result = self.plan("", 17)
        self.assertTrue(any("no content to attach them to" in w
                            for w in result.rows[0].warnings))
        # ...and the dry run must not promise a count it can't record.
        self.assertEqual(result.counts["referrals"], 0)
        summary = ci.apply(result)
        self.assertEqual(summary["referrals"], result.counts["referrals"])
        self.assertEqual(ReferralSnapshot.query.count(), 0)

    def test_homeless_referrals_do_not_make_a_row_pending_forever(self):
        ci.apply(self.plan("", 17))
        again = self.plan("", 17)
        self.assertEqual(again.rows[0].action, "unchanged")

    def test_a_changed_count_updates_rather_than_duplicating(self):
        url = tiktok_url("bealune", date(2025, 11, 2))
        ci.apply(self.plan(url, 63))

        again = self.plan(url, 71)
        self.assertEqual(again.rows[0].action, "update")
        self.assertEqual(again.rows[0].referral_change, (63, 71))
        ci.apply(again)
        self.assertEqual(ReferralSnapshot.query.count(), 1)
        self.assertEqual(ReferralSnapshot.query.one().referrals, 71)

    def test_an_unchanged_count_is_a_no_op(self):
        url = tiktok_url("bealune", date(2025, 11, 2))
        ci.apply(self.plan(url, 63))
        again = self.plan(url, 63)
        self.assertEqual(again.rows[0].action, "unchanged")
        self.assertIsNone(again.rows[0].referral_change)


class FunnelTests(unittest.TestCase):
    """Never briefed and didn't deliver are different things."""

    CSV = (
        "Creator,Email,Response,Status,Content\n"
        "Beatrice Lune,bea@example.com,Scheduled Call,Posted,"
        + tiktok_url("bealune", date(2025, 11, 2)) + "\n"
        "Cassian Vale,cass@example.com,Scheduled Call,Briefed,\n"
        "Fern Okafor,fern@example.com,No reply,Not Yet Briefed,\n"
        "Rune Pryor,rune@example.com,Volunteered for Wave 2,Not Yet Briefed,\n"
        "Wren Ashby,wren@example.com,Declined,Not Yet Briefed,\n"
    )

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        for name, email in (
            ("Beatrice Lune", "bea@example.com"),
            ("Cassian Vale", "cass@example.com"),
            ("Fern Okafor", "fern@example.com"),
            ("Rune Pryor", "rune@example.com"),
            ("Wren Ashby", "wren@example.com"),
        ):
            creator = Creator(name=name, email=email)
            db.session.add(creator)
            db.session.flush()
            if name == "Beatrice Lune":
                db.session.add(SocialHandle(
                    creator_id=creator.id, platform="tiktok", handle="@bealune"
                ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_every_stage_is_recorded(self):
        result = ci.plan(self.CSV, campaign_slug="soft",
                         campaign_name="Softlaunch")
        got = {r.creator_name: r.status for r in result.rows}
        self.assertEqual(got, {
            "Beatrice Lune": "content_delivered",
            "Cassian Vale": "briefed",
            "Fern Okafor": "contacted",
            "Rune Pryor": "wave_2",
            "Wren Ashby": "declined",
        })

    def test_the_report_shows_where_everyone_stands(self):
        text = ci.render(ci.plan(self.CSV, campaign_slug="soft",
                                 campaign_name="Softlaunch"))
        self.assertIn("Where they stand:", text)
        for label in ("contacted", "wave 2", "briefed", "content delivered",
                      "declined"):
            self.assertIn(label, text)

    def test_follow_through_is_measured_against_the_briefed_not_everyone(self):
        """1 of 2 briefed is 50%. 1 of 5 contacted would read as 20% and be
        a lie about a campaign that simply hasn't finished running."""
        ci.apply(ci.plan(self.CSV, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(campaign.creator_count, 5)
        self.assertEqual(campaign.briefed_count, 2)
        self.assertEqual(campaign.delivered_count, 1)
        self.assertEqual(campaign.delivery_rate, 50)

    def test_briefing_is_the_headline_where_it_was_tracked(self):
        """Opting in is cheap; being briefed is the real commitment. Where a
        campaign recorded both, the stronger number is the one shown."""
        ci.apply(ci.plan(self.CSV, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertTrue(campaign.records_briefing)
        self.assertEqual(campaign.opted_in_count, 3)
        self.assertEqual(campaign.commitment_label, "Briefed")
        self.assertEqual(campaign.commitment_count, 2)

    def test_a_campaign_that_never_tracked_briefing_falls_back_to_opt_ins(self):
        """briefed_count would otherwise collapse to delivered_count and
        report a 100% follow-through that means nothing at all."""
        csv_text = (
            "Creator,Email,Status,Content\n"
            "Beatrice Lune,bea@example.com,Opted In,"
            + tiktok_url("bealune", date(2025, 11, 2)) + "\n"
            "Cassian Vale,cass@example.com,Opted In,\n"
            "Fern Okafor,fern@example.com,Contacted,\n"
        )
        ci.apply(ci.plan(csv_text, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertFalse(campaign.records_briefing)
        self.assertEqual(campaign.briefed_count, 1)       # == delivered
        self.assertEqual(campaign.commitment_label, "Opted in")
        self.assertEqual(campaign.commitment_count, 2)
        self.assertEqual(campaign.delivery_rate, 50)      # not 100

    def test_the_headline_counts_narrow_rather_than_partition(self):
        """Contacted is everybody; opted in is everybody who said yes.

        The `contacted` *status* means the opposite of the `contacted`
        *metric* — one is "never replied", the other is "reached out to".
        Five people: one delivered, one briefed, one no-reply, one wave 2,
        one declined.
        """
        ci.apply(ci.plan(self.CSV, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(campaign.contacted_count, 5)
        self.assertEqual(campaign.opted_in_count, 3)   # minus no-reply, declined
        self.assertEqual(campaign.delivered_count, 1)
        self.assertGreaterEqual(campaign.contacted_count, campaign.opted_in_count)
        self.assertGreaterEqual(campaign.opted_in_count, campaign.delivered_count)

    def test_people_nobody_has_approached_are_not_counted_as_contacted(self):
        """A campaign can carry names nobody has written to yet. Counting
        them as contacted overstates reach and understates the opt-in rate."""
        csv_text = (
            "Creator,Email,Status\n"
            "Fern Okafor,fern@example.com,Not Contacted\n"
            "Wren Ashby,wren@example.com,Contacted\n"
            "Rune Pryor,rune@example.com,Opted In\n"
        )
        ci.apply(ci.plan(csv_text, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(campaign.creator_count, 3)
        self.assertEqual(campaign.contacted_count, 2)
        self.assertEqual(campaign.opted_in_count, 1)

    def test_a_campaign_nobody_answered_has_no_opt_ins(self):
        csv_text = (
            "Creator,Email,Response,Status\n"
            "Fern Okafor,fern@example.com,No reply,Not Yet Briefed\n"
            "Wren Ashby,wren@example.com,Declined,Not Yet Briefed\n"
        )
        ci.apply(ci.plan(csv_text, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(campaign.contacted_count, 2)
        self.assertEqual(campaign.opted_in_count, 0)
        self.assertEqual(campaign.delivered_count, 0)

    def test_the_campaign_reports_its_funnel_in_order(self):
        ci.apply(ci.plan(self.CSV, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(
            [(status, count) for status, _, count in campaign.status_counts],
            [("contacted", 1), ("wave_2", 1), ("briefed", 1),
             ("content_delivered", 1), ("declined", 1)],
        )

    def test_a_campaign_of_copied_numbers_says_so(self):
        csv_text = (
            "Creator,Email,Referrals,Content\n"
            "Beatrice Lune,bea@example.com,63,"
            + tiktok_url("bealune", date(2025, 11, 2)) + "\n"
        )
        ci.apply(ci.plan(csv_text, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(campaign.total_referrals, 63)
        self.assertTrue(campaign.referrals_are_reported)

    def test_a_campaign_with_no_referrals_claims_nothing_either_way(self):
        ci.apply(ci.plan(self.CSV, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        self.assertFalse(Campaign.query.one().referrals_are_reported)

    def test_nobody_briefed_is_not_a_zero_percent_failure(self):
        csv_text = (
            "Creator,Email,Response,Status\n"
            "Fern Okafor,fern@example.com,No reply,Not Yet Briefed\n"
        )
        ci.apply(ci.plan(csv_text, campaign_slug="soft",
                         campaign_name="Softlaunch"))
        campaign = Campaign.query.one()
        self.assertEqual(campaign.briefed_count, 0)
        self.assertEqual(campaign.delivery_rate, 0)


class SecondAddressTests(unittest.TestCase):
    """One person, two addresses — the thing that made a duplicate creator."""

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.bea = Creator(name="Beatrice Lune", email="bea@example.com")
        db.session.add(self.bea)
        db.session.flush()
        db.session.add(CreatorEmail(
            creator_id=self.bea.id, address="bea@example.com", is_primary=True
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def plan(self, name, email):
        csv_text = f"Creator,Email,Response\n{name},{email},Opted-in\n"
        return ci.plan(csv_text, campaign_slug="soft", campaign_name="Softlaunch")

    def test_a_new_address_is_filed_under_the_existing_profile(self):
        """Matched by name; the address is new. Record it, don't ignore it."""
        result = self.plan("Beatrice Lune", "beatrice.lune@example.net")
        row = result.rows[0]
        self.assertEqual(row.creator_id, self.bea.id)
        self.assertEqual(row.new_email, "beatrice.lune@example.net")
        self.assertEqual(result.counts["emails"], 1)

        summary = ci.apply(result)
        self.assertEqual(summary["emails"], result.counts["emails"])
        self.assertEqual(Creator.query.count(), 1)
        self.assertEqual(
            Creator.query.one().all_emails,
            ["bea@example.com", "beatrice.lune@example.net"],
        )

    def test_the_primary_address_does_not_change(self):
        ci.apply(self.plan("Beatrice Lune", "beatrice.lune@example.net"))
        self.assertEqual(Creator.query.one().email, "bea@example.com")
        primary = CreatorEmail.query.filter_by(is_primary=True).one()
        self.assertEqual(primary.address, "bea@example.com")

    def test_the_second_address_then_matches_her_on_its_own(self):
        """The point of storing it: next sheet, no duplicate."""
        ci.apply(self.plan("Beatrice Lune", "beatrice.lune@example.net"))
        result = self.plan("B. Lune", "beatrice.lune@example.net")
        self.assertEqual(result.rows[0].creator_id, self.bea.id)
        self.assertIn("email beatrice.lune@example.net",
                      result.rows[0].match_note)

    def test_recording_it_happens_once(self):
        ci.apply(self.plan("Beatrice Lune", "beatrice.lune@example.net"))
        again = self.plan("Beatrice Lune", "beatrice.lune@example.net")
        self.assertEqual(again.rows[0].action, "unchanged")
        self.assertEqual(again.counts["emails"], 0)
        ci.apply(again)
        self.assertEqual(CreatorEmail.query.count(), 2)

    def test_an_address_belonging_to_somebody_else_is_not_reassigned(self):
        other = Creator(name="Cassian Vale", email="cass@example.com")
        db.session.add(other)
        db.session.flush()
        db.session.add(CreatorEmail(
            creator_id=other.id, address="cass@example.com", is_primary=True
        ))
        db.session.commit()

        # A row naming Beatrice but carrying Cassian's address: the address
        # wins the match, because it is unique and a name is not.
        result = self.plan("Beatrice Lune", "cass@example.com")
        self.assertEqual(result.rows[0].creator_id, other.id)
        self.assertEqual(result.rows[0].new_email, "")
        ci.apply(result)
        self.assertEqual(CreatorEmail.query.filter_by(
            address="cass@example.com").one().creator_id, other.id)

    def test_the_inbox_recognises_mail_from_the_second_address(self):
        """The reason this matters day to day: a submission from the other
        account should land against the creator, not as 'no match'."""
        import inbox as inbox_lib

        ci.apply(self.plan("Beatrice Lune", "beatrice.lune@example.net"))
        creator, how = inbox_lib.match_creator("Beatrice.Lune@Example.NET")
        self.assertIsNotNone(creator)
        self.assertEqual(creator.id, self.bea.id)
        self.assertIn("second address", how)

        creator, how = inbox_lib.match_creator("bea@example.com")
        self.assertEqual(creator.id, self.bea.id)
        self.assertEqual(how, "sender email")

        creator, how = inbox_lib.match_creator("stranger@example.org")
        self.assertIsNone(creator)

    def test_a_shared_mailbox_is_allowed_but_stops_being_a_key(self):
        """A duo running one channel are two creators on one address. The
        address can no longer decide which of them a row is about."""
        import importer

        pax = Creator(name="Pax Quill", email="duo@example.com")
        juno = Creator(name="Juno Quill", email="juno@example.com")
        db.session.add_all([pax, juno])
        db.session.flush()
        db.session.add(CreatorEmail(
            creator_id=pax.id, address="duo@example.com", is_primary=True
        ))
        db.session.add(CreatorEmail(
            creator_id=juno.id, address="juno@example.com", is_primary=True
        ))
        db.session.commit()

        # Not by default: one address on two names is usually a mistake.
        self.assertIsNone(importer.record_email(juno, "duo@example.com"))
        self.assertIsNotNone(
            importer.record_email(juno, "duo@example.com", allow_shared=True)
        )
        db.session.commit()

        self.assertTrue(pax.shares_address)
        self.assertTrue(juno.shares_address)

        index = ci.CreatorIndex(
            Creator.query.all(),
            extra_emails=[(e.address, e.creator_id)
                          for e in CreatorEmail.query.all()],
        )
        self.assertIn("duo@example.com", index.shared_emails)
        self.assertNotIn("duo@example.com", index.by_email)

        # The address alone no longer resolves anybody...
        cid, _, warnings = index.match("Someone Else", None, None, [],
                                       email="duo@example.com")
        self.assertIsNone(cid)
        self.assertIn("shared by 2 creators", warnings[0])

        # ...but an exact name still does, with the address corroborating.
        cid, _, _ = index.match("Juno Quill", None, None, [],
                                email="duo@example.com")
        self.assertEqual(cid, juno.id)

    def test_an_unshared_address_is_still_a_key(self):
        index = ci.CreatorIndex(
            Creator.query.all(),
            extra_emails=[(e.address, e.creator_id)
                          for e in CreatorEmail.query.all()],
        )
        self.assertEqual(index.shared_emails, {})
        cid, _, _ = index.match("Wrong Name", None, None, [],
                                email="bea@example.com")
        self.assertEqual(cid, self.bea.id)

    def test_other_emails_is_empty_for_the_ordinary_case(self):
        self.assertEqual(Creator.query.one().other_emails, [])
        self.assertEqual(Creator.query.one().all_emails, ["bea@example.com"])


class DisplayTitleTests(unittest.TestCase):
    """A piece imported from a link list has no title; it still needs a label."""

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _piece(self, title=None, posted_on=None, platforms=()):
        creator = Creator(name="Beatrice Lune", email="bea@example.com")
        campaign = Campaign(name="Stickers Beta", slug="stickers")
        db.session.add_all([creator, campaign])
        db.session.flush()
        piece = UgcPiece(creator_id=creator.id, campaign_id=campaign.id,
                         title=title, posted_on=posted_on)
        db.session.add(piece)
        db.session.flush()
        for platform in platforms:
            db.session.add(PlatformPost(ugc_piece_id=piece.id, platform=platform))
        db.session.flush()
        return piece

    def test_the_export_gets_a_readable_provenance_label(self):
        piece = self._piece()
        piece.discovered_via = "crm_export"
        self.assertEqual(piece.discovery_label, "CRM export")

    def test_a_real_title_is_left_alone(self):
        self.assertEqual(self._piece(title="My sticker haul").display_title,
                         "My sticker haul")

    def test_platform_and_month_stand_in_for_a_missing_title(self):
        piece = self._piece(posted_on=date(2025, 10, 28), platforms=["tiktok"])
        self.assertEqual(piece.display_title, "TikTok post, Oct 2025")

    def test_a_cross_posted_piece_names_both(self):
        piece = self._piece(posted_on=date(2025, 10, 28),
                            platforms=["tiktok", "instagram"])
        self.assertEqual(piece.display_title, "Instagram + TikTok post, Oct 2025")

    def test_no_date_and_no_platform_still_gives_something(self):
        self.assertEqual(self._piece().display_title, "Content post")


if __name__ == "__main__":
    unittest.main(verbosity=2)
