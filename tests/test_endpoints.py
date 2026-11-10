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


if __name__ == "__main__":
    unittest.main(verbosity=2)
