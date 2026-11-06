#!/usr/bin/env python3
"""Mint a Gmail refresh token for creatorwise. Run this on your own Mac.

    python3 tools/gmail_auth.py

It asks for the OAuth client id and secret you created in Google Cloud, opens
your browser so you can sign in to erin@readwise.io yourself, and prints the
refresh token. Nothing is written to disk and nothing is sent anywhere except
Google.

Stdlib only, so it runs with the system python and needs no virtualenv.

The sign-in happens in your browser, under your control — the script never sees
your password, only the authorization code Google hands back afterwards.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

result = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result.update({k: v[0] for k, v in params.items()})

        ok = "code" in result
        body = (
            "<h2>Done.</h2><p>You can close this tab and go back to the terminal.</p>"
            if ok else
            f"<h2>Something went wrong.</h2><pre>{result.get('error', 'no code returned')}</pre>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body style='font:16px -apple-system'>{body}</body></html>".encode())
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # the progress we care about is printed below, not the HTTP log


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    client_id = os.environ.get("GMAIL_CLIENT_ID") or input("OAuth client ID: ").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET") or input("OAuth client secret: ").strip()
    if not (client_id and client_secret):
        sys.exit("Need both a client ID and a client secret.")

    # PKCE: ties the authorization code to this process, so a code leaking out of
    # a browser log or history can't be redeemed by anyone else.
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    port = free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent is what actually produces a refresh token; without
        # prompt=consent Google may return only an access token on a re-auth.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": "erin@readwise.io",
    })

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print("\nOpening Google in your browser. Sign in as erin@readwise.io and")
    print("approve read-only Gmail access.\n")
    print("If the browser doesn't open, paste this in yourself:\n")
    print(url + "\n")
    webbrowser.open(url)
    server.serve_forever()

    if result.get("state") != state:
        sys.exit("State mismatch — aborting rather than trusting that response.")
    if "code" not in result:
        sys.exit(f"No authorization code came back: {result.get('error', 'unknown error')}")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()

    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        sys.exit(f"Token exchange failed: HTTP {exc.code}\n{exc.read().decode()[:500]}")

    refresh = payload.get("refresh_token")
    if not refresh:
        sys.exit(
            "Google returned no refresh_token. That usually means this account "
            "has already granted this client. Revoke it at "
            "https://myaccount.google.com/permissions and run this again."
        )

    print("=" * 68)
    print("Refresh token (treat it like a password — it reads your mail):\n")
    print(refresh)
    print("\n" + "=" * 68)
    print("\nStore it, and the client id/secret, as contextone env vars:\n")
    print("  contextone env set --project creatorwise --key GMAIL_CLIENT_ID --value '<id>'")
    print("  contextone env set --project creatorwise --key GMAIL_CLIENT_SECRET --value '<secret>'")
    print("  contextone env set --project creatorwise --key GMAIL_REFRESH_TOKEN --value '<token above>'")
    print("\nThen redeploy. Don't paste any of these into a file in the repo.\n")


if __name__ == "__main__":
    main()
