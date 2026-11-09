"""Importer tests, against the kind of CSV a real export actually produces.

The fixtures here are deliberately ugly — smart quotes, "12.4K" followers, a
BOM, profile URLs where a handle was asked for, em-dashes meaning blank, two
addresses in one cell. Every one of these is a thing spreadsheets do.
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import importer
from models import Campaign, Creator, Participation, SocialHandle, db


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


class ParsingTests(unittest.TestCase):
    def test_follower_counts(self):
        cases = {
            "12.4K": 12400, "12.4k": 12400, "1.2M": 1200000,
            "1,234": 1234, "48200": 48200, "~5k": 5000,
            "  3.5K  ": 3500, "890": 890, "1,234,567": 1234567,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(importer.parse_followers(raw), expected)

    def test_follower_blanks_and_junk(self):
        for raw in ("", "—", "n/a", "TBD", "?", "ask her", None, "lots"):
            with self.subTest(raw=raw):
                self.assertIsNone(importer.parse_followers(raw))

    def test_comma_is_thousands_not_decimal(self):
        """"1,234" must never become 1.234 -> 1."""
        self.assertEqual(importer.parse_followers("1,234"), 1234)

    def test_handles_from_every_spelling(self):
        cases = {
            "@bea": "@bea",
            "bea": "@bea",
            "https://www.instagram.com/bea/": "@bea",
            "https://www.tiktok.com/@bea": "@bea",
            "instagram.com/bea": "@bea",
            "https://www.instagram.com/bea?hl=en": "@bea",
            "@bea.reads": "@bea.reads",
            "bea_reads": "@bea_reads",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(importer.parse_handle(raw)[0], expected)

    def test_a_sentence_is_not_a_handle(self):
        for raw in ("ask her for it", "", "—", "no instagram"):
            with self.subTest(raw=raw):
                self.assertIsNone(importer.parse_handle(raw)[0])

    def test_profile_url_is_kept(self):
        handle, url = importer.parse_handle("https://www.instagram.com/bea/")
        self.assertEqual(handle, "@bea")
        self.assertEqual(url, "https://www.instagram.com/bea/")

    def test_post_urls_do_not_yield_a_handle(self):
        """A link to a reel is not a profile, and /reel/ is not a username."""
        self.assertIsNone(
            importer.parse_handle("https://www.instagram.com/reel/ABC123/")[0]
        )

    def test_multiple_emails_in_one_cell(self):
        self.assertEqual(
            importer.parse_emails("bookshelfbea@example.com / bea.k@example.net"),
            ["bookshelfbea@example.com", "bea.k@example.net"],
        )

    def test_email_is_lowercased_and_trimmed(self):
        self.assertEqual(importer.parse_emails(" Bea@Example.COM "),
                         ["bea@example.com"])

    def test_dates_in_several_formats(self):
        for raw in ("2025-09-15", "09/15/2025", "Sep 15, 2025", "15 Sep 2025"):
            with self.subTest(raw=raw):
                self.assertEqual(importer.parse_date(raw), date(2025, 9, 15))

    def test_header_normalization(self):
        for raw in ("Instagram Handle", "instagram_handle", "  INSTAGRAM HANDLE  "):
            self.assertEqual(importer.normalize_header(raw), "instagram handle")

    def test_synonyms(self):
        self.assertEqual(importer.parse_source("Referral"), "referral")
        self.assertEqual(importer.parse_source("Cold outreach"), "outbound")
        self.assertEqual(importer.parse_source("Cold DM"), "outbound")
        self.assertEqual(importer.parse_source("Existing Readwise user"), "existing_user")
        self.assertEqual(importer.parse_source("Alpha tester"), "alpha_tester")
        self.assertEqual(importer.parse_payment_method("PayPal"), "paypal")
        self.assertEqual(importer.parse_payment_method("Amazon Gift Card"), "gift_card")
        self.assertEqual(importer.parse_status("Ghosted"), "no_show")
        self.assertEqual(importer.parse_status("Posted"), "content_delivered")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.campaign = Campaign(
            name="Fall 2025 Sticker Beta", slug="fall-2025-sticker-beta",
            status="complete", starts_on=date(2025, 9, 15), ends_on=date(2025, 11, 30),
        )
        db.session.add(self.campaign)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_a_realistic_messy_export(self):
        csv_text = (
            "﻿Creator Name,Email Address,IG Handle,IG Followers,TikTok,"
            "TikTok Followers,Source,Status,Pay via,PayPal Email,Vibe\n"
            "Bea Hall,BEA@example.com,https://www.instagram.com/bea.reads/,"
            "12.4K,@beatok,1.2M,Referral,Posted,PayPal,bea@example.com,sweet\n"
            "Dana Lu,dana@example.com,@danareads,890,—,—,Cold DM,Ghosted,Venmo,,quiet\n"
        )
        result = importer.plan(csv_text, campaign_slug="fall-2025-sticker-beta")

        self.assertTrue(result.ok)
        self.assertEqual(result.counts["create"], 2)
        self.assertEqual(result.counts["blocked"], 0)
        self.assertEqual(result.unmapped, ["Vibe"])  # reported, not silently dropped

        bea = result.rows[0]
        self.assertEqual(bea.email, "bea@example.com")
        self.assertEqual(bea.fields["recruitment_source"], "referral")
        self.assertEqual(bea.fields["payment_method"], "paypal")
        self.assertEqual(bea.status, "content_delivered")
        handles = {h["platform"]: h for h in bea.handles}
        self.assertEqual(handles["instagram"]["handle"], "@bea.reads")
        self.assertEqual(handles["instagram"]["follower_count"], 12400)
        self.assertEqual(handles["tiktok"]["follower_count"], 1200000)

        dana = result.rows[1]
        self.assertEqual(dana.status, "no_show")
        self.assertEqual([h["platform"] for h in dana.handles], ["instagram"])

    def test_rows_without_an_email_are_blocked_not_dropped(self):
        csv_text = "Name,Email\nNo Address,\nFine Person,ok@example.com\n"
        result = importer.plan(csv_text)
        blocked = result.of("blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].line, 2)
        self.assertIn("no usable email", blocked[0].reason)
        self.assertEqual(result.counts["create"], 1)

    def test_rows_without_a_name_are_blocked(self):
        result = importer.plan("Name,Email\n,someone@example.com\n")
        self.assertEqual(result.of("blocked")[0].reason, "no name")

    def test_duplicate_email_in_the_file_is_blocked_with_the_first_line(self):
        csv_text = (
            "Name,Email\n"
            "Bea Hall,bea@example.com\n"
            "Bea H,BEA@example.com\n"
        )
        result = importer.plan(csv_text)
        blocked = result.of("blocked")
        self.assertEqual(len(blocked), 1)
        self.assertIn("duplicate of line 2", blocked[0].reason)
        self.assertIn("different name", blocked[0].reason)

    def test_a_second_address_in_a_cell_is_flagged_not_guessed(self):
        csv_text = ("Name,Email\n"
                    "Bea,bookshelfbea@example.com / bea.k@example.net\n")
        result = importer.plan(csv_text)
        row = result.rows[0]
        self.assertEqual(row.email, "bookshelfbea@example.com")
        self.assertTrue(any("bea.k@example.net" in w for w in row.warnings))

    def test_missing_email_column_stops_the_import(self):
        result = importer.plan("Name,Instagram\nBea,@k\n")
        self.assertFalse(result.ok)
        self.assertIn("no email column", result.campaign_error)

    def test_unknown_campaign_stops_the_import(self):
        result = importer.plan("Name,Email\nK,k@example.com\n", campaign_slug="nope")
        self.assertFalse(result.ok)
        self.assertIn("fall-2025-sticker-beta", result.campaign_error)

    def test_unrecognized_source_becomes_a_note_with_a_warning(self):
        result = importer.plan(
            "Name,Email,Source\nK,k@example.com,Met at a conference\n"
        )
        row = result.rows[0]
        self.assertNotIn("recruitment_source", row.fields)
        self.assertEqual(row.fields["recruitment_note"], "Met at a conference")
        self.assertTrue(any("isn't one of" in w for w in row.warnings))

    def test_ambiguous_follower_column_is_not_guessed(self):
        """One "Followers" column, two platforms — attributing it would invent data."""
        result = importer.plan(
            "Name,Email,Instagram,TikTok,Followers\n"
            "K,k@example.com,@k_ig,@k_tt,50000\n"
        )
        row = result.rows[0]
        self.assertTrue(all(h["follower_count"] is None for h in row.handles))
        self.assertTrue(any("can't tell which" in w for w in row.warnings))

    def test_single_platform_takes_the_general_follower_column(self):
        result = importer.plan(
            "Name,Email,Instagram,Followers\nK,k@example.com,@k_ig,50000\n"
        )
        self.assertEqual(result.rows[0].handles[0]["follower_count"], 50000)

    def test_duplicate_columns_are_reported(self):
        result = importer.plan("Name,Email,E-mail Address\nK,a@x.com,b@y.com\n")
        self.assertTrue(result.duplicate_headers)
        self.assertEqual(result.rows[0].email, "a@x.com")

    def test_blank_spacer_rows_are_ignored(self):
        result = importer.plan("Name,Email\nK,k@example.com\n,,\n\nD,d@example.com\n")
        self.assertEqual(result.counts["create"], 2)
        self.assertEqual(result.counts["blocked"], 0)


class RecruiterTests(unittest.TestCase):
    """Resolving the referred_by column into an actual graph.

    214 of 280 rows in the real export name a referrer, and 204 of those are a
    creator in the same file — so this is the difference between a text note and
    something you can traverse.
    """

    CSV = (
        "name,email,tiktok_url,source,referred_by\n"
        "Wes Harlan,wil@example.com,https://www.tiktok.com/@wesreads,Cold outreach,\n"
        "Nadia Moreau,nadia@example.com,https://www.tiktok.com/@nattiereads,Referral,Wes Harlan\n"
        "Dev Okafor,dev@example.com,https://www.tiktok.com/@devreads,Referral,@nattiereads\n"
        "Fern Walsh,fern.w@example.com,https://www.tiktok.com/@fernreads,Referral,Wes Harlan\n"
        "Kira B,kira@example.com,,Referral,fernreads@example.com\n"
        "Sam Price,sam1@example.com,,Referral,Wes Harlan\n"
        "Sam Quinn,sam2@example.com,,Referral,Wes Harlan\n"
        "Ash Lee,wren@example.com,,Referral,Sam\n"
        "Robin Vale,robin@example.com,,Referral,Genevieve Harp\n"
        "Joint Case,joint@example.com,,Referral,Wes Harlan + Nadia Moreau\n"
    )

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        importer.apply(importer.plan(self.CSV))

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def by(self, email):
        return Creator.query.filter_by(email=email).one()

    def test_referrer_resolved_by_name(self):
        self.assertEqual(self.by("nadia@example.com").recruited_by.name, "Wes Harlan")

    def test_referrer_resolved_by_handle(self):
        """'@nattiereads' is Nadia's TikTok handle, not her name."""
        self.assertEqual(self.by("dev@example.com").recruited_by.name, "Nadia Moreau")

    def test_referrer_resolved_by_email(self):
        self.assertEqual(self.by("kira@example.com").recruited_by.name, "Fern Walsh")

    def test_ambiguous_first_name_is_not_guessed(self):
        """Two creators called Sam — picking one would be inventing a fact."""
        wren = self.by("wren@example.com")
        self.assertIsNone(wren.recruited_by)
        self.assertEqual(wren.recruited_by_note, "Sam")

    def test_referrer_outside_the_programme_is_kept_as_text(self):
        robin = self.by("robin@example.com")
        self.assertIsNone(robin.recruited_by)
        self.assertEqual(robin.recruited_by_note, "Genevieve Harp")

    def test_two_referrers_in_one_cell_is_kept_as_text(self):
        joint = self.by("joint@example.com")
        self.assertIsNone(joint.recruited_by)
        self.assertIn("Wes Harlan", joint.recruited_by_note)

    def test_recruits_is_the_reverse_edge(self):
        wil_row = self.by("wil@example.com")
        self.assertEqual(wil_row.recruit_count, 4)
        self.assertIn("Nadia Moreau", {c.name for c in wil_row.recruits})
        self.assertIsNone(wil_row.recruited_by)

    def test_recruited_by_label_prefers_the_linked_creator(self):
        self.assertEqual(self.by("nadia@example.com").recruited_by_label, "Wes Harlan")
        self.assertEqual(self.by("wren@example.com").recruited_by_label, "Sam")

    def test_a_self_reference_is_refused(self):
        """A sheet typo naming someone as their own referrer would loop the graph."""
        result = importer.plan(
            "name,email,referred_by\nLoopy,loopy@example.com,Loopy\n"
        )
        importer.apply(result)
        loopy = self.by("loopy@example.com")
        self.assertIsNone(loopy.recruited_by_id)
        self.assertEqual(loopy.recruited_by_note, "Loopy")

    def test_plan_projects_resolution_before_writing(self):
        db.session.query(Creator).delete()
        db.session.commit()
        result = importer.plan(self.CSV)
        resolved, unresolved = result.recruiters
        # 9 rows name a referrer. Six resolve: four by name, one by handle
        # (@nattiereads), one by the local part of an email (fernreads@…).
        # Three don't: "Sam" (two of them), someone outside the programme, and
        # a cell naming two people.
        self.assertEqual((resolved, unresolved), (6, 3))
        self.assertIn("UNRESOLVED REFERRERS", importer.render(result))


class ExtraColumnTests(unittest.TestCase):
    """Columns the real export has that the first version didn't know about."""

    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_threads_and_x_become_handles(self):
        importer.apply(importer.plan(
            "name,email,threads_url,x_url\n"
            "Adi,adi@example.com,https://www.threads.com/@aviinthelife,https://x.com/adi\n"
        ))
        handles = {h.platform: h.handle for h in Creator.query.one().handles}
        self.assertEqual(handles["threads"], "@aviinthelife")
        self.assertEqual(handles["x"], "@adi")

    def test_threads_and_x_are_not_tracked_platforms(self):
        """They're contact details — no content metrics roll up through them."""
        from models import PLATFORMS, PROFILE_PLATFORMS
        self.assertNotIn("threads", PLATFORMS)
        self.assertIn("threads", PROFILE_PLATFORMS)

    def test_youtube_channel_id_url(self):
        """Legacy /channel/UC… has no @handle, but the id still identifies it."""
        importer.apply(importer.plan(
            "name,email,youtube_url\n"
            "Rowan,rowan@example.com,https://www.youtube.com/channel/UCa1b2c3d4e5f6g7h8i9j0kL\n"
        ))
        handle = SocialHandle.query.one()
        self.assertEqual(handle.handle, "UCa1b2c3d4e5f6g7h8i9j0kL")
        self.assertIn("/channel/", handle.profile_url)

    def test_onboarded_date_is_kept(self):
        importer.apply(importer.plan(
            "name,email,onboarded_date\nAdi,adi@example.com,2025-09-30\n"
        ))
        self.assertEqual(Creator.query.one().onboarded_on, date(2025, 9, 30))

    def test_other_urls_are_split_one_per_line(self):
        importer.apply(importer.plan(
            "name,email,other_urls\n"
            "Juno,j@example.com,https://linktr.ee/x; https://bsky.app/profile/y\n"
        ))
        self.assertEqual(
            Creator.query.one().link_list,
            ["https://linktr.ee/x", "https://bsky.app/profile/y"],
        )

    def test_real_source_vocabulary_round_trips(self):
        importer.apply(importer.plan(
            "name,email,source\n"
            "A,a@example.com,Existing Readwise user\n"
            "B,b@example.com,Alpha tester\n"
            "C,c@example.com,Cold outreach\n"
        ))
        got = {c.name: (c.recruitment_source, c.source_label) for c in Creator.query.all()}
        self.assertEqual(got["A"], ("existing_user", "Existing Readwise user"))
        self.assertEqual(got["B"], ("alpha_tester", "Alpha tester"))
        self.assertEqual(got["C"], ("outbound", "Cold outreach"))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.campaign = Campaign(
            name="Fall 2025 Sticker Beta", slug="fall-2025-sticker-beta",
            status="complete", starts_on=date(2025, 9, 15), ends_on=date(2025, 11, 30),
        )
        db.session.add(self.campaign)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    CSV = (
        "Name,Email,Instagram,IG Followers,Source,Pay via\n"
        "Bea Hall,bea@example.com,@bea.reads,12.4K,Referral,PayPal\n"
        "Dana Lu,dana@example.com,@danareads,890,Cold DM,Venmo\n"
    )

    def run_import(self, text=None, campaign=None):
        result = importer.plan(text or self.CSV, campaign_slug=campaign)
        return importer.apply(result)

    def test_creates_creators_with_handles(self):
        summary = self.run_import()
        self.assertEqual(summary["created"], 2)

        bea = Creator.query.filter_by(email="bea@example.com").one()
        self.assertEqual(bea.name, "Bea Hall")
        self.assertEqual(bea.recruitment_source, "referral")
        self.assertEqual(bea.payment_method, "paypal")

        handle = SocialHandle.query.filter_by(creator_id=bea.id).one()
        self.assertEqual(handle.handle, "@bea.reads")
        self.assertEqual(handle.follower_count, 12400)

    def test_running_it_twice_changes_nothing(self):
        self.run_import()
        second = importer.plan(self.CSV)
        self.assertEqual(second.counts["unchanged"], 2)
        self.assertEqual(second.counts["create"], 0)

        summary = importer.apply(second)
        self.assertEqual((summary["created"], summary["updated"]), (0, 0))
        self.assertEqual(Creator.query.count(), 2)
        self.assertEqual(SocialHandle.query.count(), 2)

    def test_an_updated_export_updates_in_place(self):
        self.run_import()
        updated_csv = (
            "Name,Email,Instagram,IG Followers,Source,Pay via\n"
            "Bea Hall,bea@example.com,@bea.reads,19.8K,Referral,PayPal\n"
        )
        result = importer.plan(updated_csv)
        self.assertEqual(result.counts["update"], 1)
        self.assertIn("instagram followers", result.rows[0].changes)

        importer.apply(result)
        handle = SocialHandle.query.join(Creator).filter(
            Creator.email == "bea@example.com"
        ).one()
        self.assertEqual(handle.follower_count, 19800)
        self.assertEqual(Creator.query.count(), 2)  # Dana untouched

    def test_a_blank_cell_never_erases_existing_data(self):
        """Partial exports are normal. They must not wipe what's already known."""
        self.run_import()
        sparse = "Name,Email,Instagram,IG Followers,Source,Pay via\n" \
                 "Bea Hall,bea@example.com,,,,\n"
        importer.apply(importer.plan(sparse))

        bea = Creator.query.filter_by(email="bea@example.com").one()
        self.assertEqual(bea.recruitment_source, "referral")
        self.assertEqual(bea.payment_method, "paypal")
        self.assertEqual(
            SocialHandle.query.filter_by(creator_id=bea.id).one().follower_count,
            12400,
        )

    def test_campaign_links_are_created_once(self):
        summary = self.run_import(campaign="fall-2025-sticker-beta")
        self.assertEqual(summary["participations"], 2)

        again = importer.plan(self.CSV, campaign_slug="fall-2025-sticker-beta")
        self.assertEqual(importer.apply(again)["participations"], 0)
        self.assertEqual(Participation.query.count(), 2)

    def test_status_column_sets_participation_status(self):
        csv_text = (
            "Name,Email,Status,Opted In\n"
            "Bea Hall,bea@example.com,Delivered,2025-09-20\n"
            "Dana Lu,dana@example.com,Ghosted,09/22/2025\n"
        )
        self.run_import(csv_text, campaign="fall-2025-sticker-beta")

        statuses = {
            p.creator.email: (p.status, p.opted_in_on)
            for p in Participation.query.all()
        }
        self.assertEqual(statuses["bea@example.com"],
                         ("content_delivered", date(2025, 9, 20)))
        self.assertEqual(statuses["dana@example.com"],
                         ("no_show", date(2025, 9, 22)))

    def test_blocked_rows_are_skipped_but_the_rest_import(self):
        csv_text = (
            "Name,Email\n"
            "Good One,good@example.com\n"
            "No Email,\n"
            "Also Good,also@example.com\n"
        )
        summary = self.run_import(csv_text)
        self.assertEqual((summary["created"], summary["blocked"]), (2, 1))
        self.assertEqual(Creator.query.count(), 2)

    def test_a_failure_rolls_the_whole_file_back(self):
        """Half an import is worse than none — you can't tell what landed."""
        result = importer.plan(self.CSV)
        result.rows[1].fields["name"] = None  # force a NOT NULL violation
        result.rows[1].email = None

        with self.assertRaises(Exception):
            importer.apply(result)
        self.assertEqual(Creator.query.count(), 0)

    def test_render_reports_blocked_rows_and_warnings(self):
        csv_text = (
            "Name,Email,Source,Vibe\n"
            "Good,good@example.com,Met at a conference,sunny\n"
            "No Email,,,\n"
        )
        text = importer.render(importer.plan(csv_text))
        self.assertIn("BLOCKED", text)
        self.assertIn("no usable email", text)
        self.assertIn("WARNINGS", text)
        self.assertIn("Vibe", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
