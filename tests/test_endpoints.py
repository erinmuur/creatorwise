"""The session gate vs the token-authed API endpoints.

Written because this broke once: /api/scan and /api/gmail/check weren't in
PUBLIC_ENDPOINTS, so the before_request gate redirected them to /login and the
bearer check never ran. A 302 to a login form is a confusing way for an API to
say "unauthorized", and it looks identical to a deploy that didn't take.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "test-ingest-token"

os.environ.update(
    DASHBOARD_PASSWORD="test-password",
    SECRET_KEY="test-secret",
    INGEST_TOKEN=TOKEN,
    SEED_SAMPLE_DATA="0",
    DATABASE_URL="sqlite://",
)
for _k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
    os.environ.pop(_k, None)

import app as appmod  # noqa: E402  (env must be set before import)


class SessionGateTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

    def test_dashboard_pages_require_a_session(self):
        for path in ("/", "/creators", "/campaigns", "/inbox"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r.headers["Location"])

    def test_healthz_is_open(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_token_endpoints_are_not_redirected_to_login(self):
        """They must reach their own auth, not the session gate."""
        for path in ("/api/ingest", "/api/scan", "/api/gmail/check"):
            with self.subTest(path=path):
                r = self.client.post(path)
                self.assertNotEqual(r.status_code, 302, "redirected to /login")
                self.assertEqual(r.status_code, 401)
                self.assertEqual(r.get_json(), {"error": "unauthorized"})

    def test_token_endpoints_accept_the_bearer_token(self):
        auth = {"Authorization": f"Bearer {TOKEN}"}

        # Gmail is unconfigured in this test env, so these two answer 503 —
        # which still proves the bearer check ran instead of a redirect.
        for path in ("/api/scan", "/api/gmail/check"):
            with self.subTest(path=path):
                r = self.client.post(path, headers=auth)
                self.assertEqual(r.status_code, 503)
                self.assertIn("error", r.get_json())

        r = self.client.post("/api/ingest", headers=auth, json={"messages": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["queued"], 0)

    def test_the_manual_scan_button_does_require_a_session(self):
        """Unlike the API routes, the UI form has no token of its own."""
        r = self.client.post("/inbox/scan")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_logging_in_opens_the_dashboard(self):
        self.client.post("/login", data={"password": "test-password"})
        self.assertEqual(self.client.get("/inbox").status_code, 200)

    def test_a_wrong_password_is_rejected(self):
        r = self.client.post("/login", data={"password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/inbox").status_code, 302)


class AvatarTests(unittest.TestCase):
    """Photos are real people's faces: gated like every other page, and drawn
    only for the creators who actually have one."""

    JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

    def setUp(self):
        import tempfile

        import avatars
        from models import Creator, db

        self.tmp = tempfile.TemporaryDirectory()
        appmod.app.config["AVATAR_DIR"] = self.tmp.name
        self.client = appmod.app.test_client()
        with appmod.app.app_context():
            db.session.query(Creator).delete()
            with_photo = Creator(name="Bea Lune", email="bea@example.com")
            without = Creator(name="Pax Quill", email="pax@example.com")
            db.session.add_all([with_photo, without])
            db.session.commit()
            self.with_photo, self.without = with_photo.id, without.id
        avatars.save(self.tmp.name, self.with_photo, self.JPEG)

    def tearDown(self):
        appmod.app.config.pop("AVATAR_DIR", None)
        self.tmp.cleanup()

    def login(self):
        self.client.post("/login", data={"password": "test-password"})

    def test_a_photo_needs_a_session(self):
        r = self.client.get(f"/creators/{self.with_photo}/avatar")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_a_photo_is_served_to_a_signed_in_user(self):
        self.login()
        r = self.client.get(f"/creators/{self.with_photo}/avatar")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.data, self.JPEG)
        r.close()

    def test_no_photo_is_a_404_not_a_broken_image(self):
        self.login()
        self.assertEqual(
            self.client.get(f"/creators/{self.without}/avatar").status_code, 404
        )

    def test_the_list_draws_a_photo_or_initials_never_a_dead_link(self):
        """Also guards the macro import: without `with context` it cannot see
        who has a photo and silently renders initials for everybody."""
        self.login()
        html = self.client.get("/creators").get_data(as_text=True)
        self.assertIn(f'/creators/{self.with_photo}/avatar"', html)
        self.assertNotIn(f'/creators/{self.without}/avatar"', html)
        self.assertIn(">PQ</span>", html)

    def test_the_profile_page_shows_it_too(self):
        self.login()
        html = self.client.get(f"/creators/{self.with_photo}").get_data(as_text=True)
        self.assertIn(f'/creators/{self.with_photo}/avatar"', html)
        self.assertIn("avatar-lg", html)


class CreatorSearchTests(unittest.TestCase):
    """The search box filters in the page; the server's job is to hand each
    row everything a creator can be found by."""

    def setUp(self):
        from models import Creator, CreatorEmail, SocialHandle, db

        self.client = appmod.app.test_client()
        with appmod.app.app_context():
            db.session.query(SocialHandle).delete()
            db.session.query(CreatorEmail).delete()
            db.session.query(Creator).delete()
            bea = Creator(name="Béa Lune", email="bea@example.com")
            pax = Creator(name="Pax Quill", email="pax@example.com")
            db.session.add_all([bea, pax])
            db.session.flush()
            db.session.add(CreatorEmail(creator_id=bea.id, address="bea@example.com",
                                        is_primary=True))
            db.session.add(CreatorEmail(creator_id=bea.id, address="second@example.net"))
            db.session.add(SocialHandle(creator_id=bea.id, platform="tiktok",
                                        handle="@BeaReads"))
            db.session.commit()
            self.search_text = bea.search_text
        self.client.post("/login", data={"password": "test-password"})

    def test_a_creator_is_findable_by_name_any_email_and_handle(self):
        for needle in ("bea lune", "bea@example.com", "second@example.net",
                       "beareads", "@beareads"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.search_text)

    def test_accents_do_not_get_in_the_way(self):
        """Typing "bea" has to find "Béa"."""
        self.assertIn("bea lune", self.search_text)
        self.assertNotIn("é", self.search_text)

    def test_the_page_carries_the_box_and_a_search_string_per_row(self):
        html = self.client.get("/creators").get_data(as_text=True)
        self.assertIn('id="creator-search"', html)
        self.assertEqual(html.count('class="creator-row"'), 2)
        self.assertIn("second@example.net", html)   # only present via data-search
        self.assertIn('id="creator-no-match"', html)

    def test_a_query_in_the_url_prefills_the_box_and_survives_a_sort(self):
        html = self.client.get("/creators?q=bea&sort=name").get_data(as_text=True)
        self.assertIn('value="bea"', html)
        self.assertIn("sort=followers&amp;q=bea", html)

    def test_followers_are_shown_per_platform(self):
        """Blank where they aren't on a platform, a dash where they are but
        nobody has checked, a number where somebody has."""
        from models import Creator, SocialHandle, db

        with appmod.app.app_context():
            bea = Creator.query.filter_by(email="bea@example.com").one()
            bea.handles[0].follower_count = 12400          # tiktok, measured
            db.session.add(SocialHandle(creator_id=bea.id, platform="instagram",
                                        handle="@bea.lune"))   # unmeasured
            db.session.commit()
        html = self.client.get("/creators").get_data(as_text=True)
        for label in ("Instagram", "TikTok", "YouTube"):
            self.assertIn(f'<th class="num">{label}</th>', html)
        self.assertIn(">12.4k<", html)
        self.assertIn("followers not checked yet", html)

    def test_a_query_is_escaped_not_injected(self):
        html = self.client.get('/creators?q="><script>x</script>').get_data(as_text=True)
        self.assertNotIn("<script>x</script>", html)


class VideoReferralDisplayTests(unittest.TestCase):
    """Video cards show the creator's campaign figure, labelled as such —
    not a per-video number that cannot exist and used to read 0."""

    def setUp(self):
        from datetime import date

        from models import (
            Campaign, CampaignReferral, Creator, Participation, UgcPiece, db,
        )

        self.client = appmod.app.test_client()
        with appmod.app.app_context():
            for model in (CampaignReferral, UgcPiece, Participation, Campaign):
                db.session.query(model).delete()
            db.session.query(Creator).delete()
            camp = Campaign(name="Spring Pilot", slug="spring-pilot",
                            starts_on=date(2026, 10, 28), ends_on=date(2026, 12, 1))
            bea = Creator(name="Bea Lune", email="bea@example.com")
            db.session.add_all([camp, bea])
            db.session.flush()
            db.session.add(Participation(creator_id=bea.id, campaign_id=camp.id,
                                         status="content_delivered"))
            for _ in range(2):
                db.session.add(UgcPiece(creator_id=bea.id, campaign_id=camp.id))
            db.session.add(CampaignReferral(
                creator_id=bea.id, campaign_id=camp.id, referrals=1234,
                window_from=date(2026, 10, 28), window_to=date(2026, 11, 10),
            ))
            db.session.commit()
        self.client.post("/login", data={"password": "test-password"})

    def test_the_campaign_page_shows_one_card_per_creator(self):
        """Two videos by one person are one card carrying their figure once."""
        html = self.client.get("/campaigns/spring-pilot").get_data(as_text=True)
        self.assertEqual(html.count("<b>1,234</b> referrals"), 1)
        self.assertIn("2 pieces", html)
        self.assertNotIn("creator ref", html)
        self.assertNotIn("<b>0</b> ref", html)
        self.assertIn("(so far)", html)          # campaign still running
        self.assertNotIn("Window still open", html)

    def test_the_card_opens_that_creators_content_for_the_campaign(self):
        with appmod.app.app_context():
            from models import Creator
            bea = Creator.query.filter_by(email="bea@example.com").one().id
        html = self.client.get("/campaigns/spring-pilot").get_data(as_text=True)
        path = f"/campaigns/spring-pilot/creators/{bea}"
        self.assertIn(f'href="{path}"', html)
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertEqual(html.count('class="ugc-card"'), 2)
        self.assertIn("1,234", html)
        self.assertIn("for the creator, not per piece", html)

    def test_a_profiles_campaign_row_opens_that_creators_content(self):
        """Several pieces: their content for the campaign. One: the piece
        itself. None: the campaign page, as there is nothing of theirs."""
        from datetime import date

        from models import Campaign, Creator, Participation, UgcPiece, db
        with appmod.app.app_context():
            bea = Creator.query.filter_by(email="bea@example.com").one()
            solo = Campaign(name="Solo Run", slug="solo-run", starts_on=date(2026, 1, 1))
            quiet = Campaign(name="Quiet One", slug="quiet-one", starts_on=date(2025, 1, 1))
            db.session.add_all([solo, quiet])
            db.session.flush()
            db.session.add_all([
                Participation(creator_id=bea.id, campaign_id=solo.id, status="content_delivered"),
                Participation(creator_id=bea.id, campaign_id=quiet.id, status="opted_in"),
            ])
            piece = UgcPiece(creator_id=bea.id, campaign_id=solo.id)
            db.session.add(piece)
            db.session.commit()
            bea_id, piece_id = bea.id, piece.id
        html = self.client.get(f"/creators/{bea_id}").get_data(as_text=True)
        self.assertIn(f'href="/campaigns/spring-pilot/creators/{bea_id}">Spring Pilot</a>', html)
        self.assertIn(f'href="/ugc/{piece_id}">Solo Run</a>', html)
        self.assertIn('href="/campaigns/quiet-one">Quiet One</a>', html)
        self.assertIn('href="/campaigns/spring-pilot">Campaign page</a>', html)

    def test_a_shared_piece_is_under_both_makers_and_counted_once(self):
        from datetime import date

        from models import Campaign, Creator, PieceCollaborator, UgcPiece, db
        with appmod.app.app_context():
            camp = Campaign.query.filter_by(slug="spring-pilot").one()
            bea = Creator.query.filter_by(email="bea@example.com").one()
            cal = Creator(name="Cal Ombra", email="cal@example.com")
            db.session.add(cal)
            db.session.flush()
            shared = UgcPiece(creator_id=bea.id, campaign_id=camp.id,
                              posted_on=date(2026, 11, 2))
            db.session.add(shared)
            db.session.flush()
            db.session.add(PieceCollaborator(ugc_piece_id=shared.id, creator_id=cal.id))
            db.session.commit()
            groups = {g.creator.name: g for g in camp.creator_groups}
            self.assertEqual(len(groups["Bea Lune"].pieces), 3)
            self.assertEqual([p.id for p in groups["Cal Ombra"].pieces], [shared.id])
            # Measured campaign, nothing credited to Cal: a real zero, not a blank.
            self.assertEqual(groups["Cal Ombra"].referrals, 0)
            self.assertEqual(len(camp.ugc), 3)
            cal_id = cal.id
        html = self.client.get(f"/campaigns/spring-pilot/creators/{cal_id}").get_data(as_text=True)
        self.assertIn("Bea Lune &amp; Cal Ombra", html)

    def test_someone_with_no_part_in_the_campaign_is_a_404(self):
        from models import Creator, db
        with appmod.app.app_context():
            stranger = Creator(name="Dee Stranger", email="dee@example.com")
            db.session.add(stranger)
            db.session.commit()
            sid = stranger.id
        r = self.client.get(f"/campaigns/spring-pilot/creators/{sid}")
        self.assertEqual(r.status_code, 404)

    def test_top_videos_lists_a_creator_once(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Creator referrals", html)
        # Count within Top videos only: Top creators lists the same person too.
        videos = html.split("Top videos", 1)[1].split("Top creators", 1)[0]
        self.assertEqual(videos.count('<td class="num">1,234</td>'), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
