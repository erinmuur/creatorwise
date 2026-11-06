"""Exercise gmail.py's real HTTP paths against a fake Gmail on localhost.

The mocked tests in test_scan.py stop at `fetch_messages`. These go through
urllib, the token exchange, the token cache, pagination and error handling — the
layer that only shows its problems once deployed.

No network: everything is served from a throwaway loopback server.
"""

import base64
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmail


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


MESSAGES = {
    "m1": {
        "id": "m1",
        "internalDate": "1762387200000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Kirsten <kirstensbookshelves@gmail.com>"},
                {"name": "Subject", "value": "posted!"},
            ],
            "mimeType": "text/plain",
            "body": {"data": b64("https://www.instagram.com/reel/AAA/")},
        },
    },
    "m2": {
        "id": "m2",
        "internalDate": "1762387200000",
        "payload": {
            "headers": [
                {"name": "From", "value": "noreply@service.tiktok.com"},
                {"name": "Subject", "value": "someone posted"},
            ],
            "mimeType": "text/plain",
            "body": {"data": b64("https://www.tiktok.com/@x/video/1")},
        },
    },
}


class FakeGoogle(BaseHTTPRequestHandler):
    # Flipped by tests to make the token endpoint fail.
    token_status = 200
    token_calls = 0

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path != "/token":
            return self._send(404, {"error": "not found"})
        FakeGoogle.token_calls += 1
        if FakeGoogle.token_status != 200:
            return self._send(FakeGoogle.token_status, {"error": "invalid_grant"})
        self._send(200, {"access_token": "fake-access-token", "expires_in": 3600})

    def do_GET(self):
        parsed = urlparse(self.path)
        if self.headers.get("Authorization") != "Bearer fake-access-token":
            return self._send(401, {"error": "bad bearer"})

        if parsed.path == "/u/profile":
            return self._send(200, {"emailAddress": "erin@readwise.io"})

        if parsed.path == "/u/messages":
            params = parse_qs(parsed.query)
            # Paginate one id at a time, to prove the loop works.
            page = params.get("pageToken", [None])[0]
            if page is None:
                return self._send(200, {"messages": [{"id": "m1"}], "nextPageToken": "p2"})
            return self._send(200, {"messages": [{"id": "m2"}]})

        if parsed.path.startswith("/u/messages/"):
            mid = parsed.path.rsplit("/", 1)[-1]
            if mid not in MESSAGES:
                return self._send(404, {"error": "no such message"})
            return self._send(200, MESSAGES[mid])

        self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


class GmailHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeGoogle)
        host, port = cls.server.server_address
        cls.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self._saved_urls = (gmail.TOKEN_URL, gmail.API_ROOT)
        gmail.TOKEN_URL = self.base + "/token"
        gmail.API_ROOT = self.base + "/u"

        self._saved_env = {
            k: os.environ.get(k)
            for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                      "GMAIL_REFRESH_TOKEN", "GMAIL_QUERY")
        }
        os.environ.update(GMAIL_CLIENT_ID="id", GMAIL_CLIENT_SECRET="secret",
                          GMAIL_REFRESH_TOKEN="refresh")
        os.environ.pop("GMAIL_QUERY", None)

        gmail._token_cache.update(value=None, expires_at=0.0)
        FakeGoogle.token_status = 200
        FakeGoogle.token_calls = 0

    def tearDown(self):
        gmail.TOKEN_URL, gmail.API_ROOT = self._saved_urls
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        gmail._token_cache.update(value=None, expires_at=0.0)

    def test_check_reads_the_account_profile(self):
        self.assertEqual(gmail.check(), "erin@readwise.io")

    def test_fetch_paginates_and_parses(self):
        msgs = gmail.fetch_messages()
        self.assertEqual([m["message_id"] for m in msgs], ["m1", "m2"])
        self.assertEqual(msgs[0]["sender"], "kirstensbookshelves@gmail.com")
        self.assertIn("instagram.com/reel/AAA", msgs[0]["body"])

    def test_limit_stops_the_pagination_early(self):
        self.assertEqual(len(gmail.fetch_messages(limit=1)), 1)

    def test_token_is_fetched_once_per_scan(self):
        gmail.fetch_messages()
        self.assertEqual(FakeGoogle.token_calls, 1)

    def test_a_revoked_refresh_token_surfaces_as_gmail_error(self):
        FakeGoogle.token_status = 400
        with self.assertRaises(gmail.GmailError) as cm:
            gmail.fetch_messages()
        self.assertIn("invalid_grant", str(cm.exception))

    def test_unreachable_google_surfaces_as_gmail_error(self):
        gmail.TOKEN_URL = "http://127.0.0.1:1/token"
        with self.assertRaises(gmail.GmailError) as cm:
            gmail.fetch_messages()
        self.assertIn("could not reach Google", str(cm.exception))

    def test_default_query_narrows_to_platform_links(self):
        for needle in ("instagram.com", "tiktok.com", "youtube.com", "-in:spam"):
            self.assertIn(needle, gmail.query())


if __name__ == "__main__":
    unittest.main(verbosity=2)
