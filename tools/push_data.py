#!/usr/bin/env python3
"""Copy the local database, media and photos to the deployed instance.

The real data lives here, on the Mac: `data/` is gitignored because the
repository is public, so deploying brings code and no data. This sends the
data straight to the server's token-protected upload endpoints (datasync.py),
never through GitHub.

The local copy is the source of truth. The server's database is replaced
whole by a snapshot of this one; media and photos are sent only where the
server's copy is missing or differs (by sha256). Files the server has that
this machine doesn't are reported, and removed only with ``--prune``.

    ./venv/bin/python tools/push_data.py                 # dry run: what would change
    ./venv/bin/python tools/push_data.py --apply         # send it
    ./venv/bin/python tools/push_data.py --apply --prune # and remove remote extras

Needs SYNC_TOKEN (the same value as the contextone env var) and SYNC_URL, the
deployed site's https address — both from the environment or `.env`, never
from the repo. Plain http is refused except for localhost: the token can
replace the whole database.

Run it after anything changes the local data — an import, a merge, the daily
referral refresh — or the site shows yesterday's numbers.
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datasync  # noqa: E402

CHUNK = 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def local_files(root):
    """{relative path: (absolute path, size, sha256)} for what can be synced."""
    return {rel: (full, os.path.getsize(full), sha256_of(full))
            for rel, full in datasync.stored_files(root).items()}


def compare(local, remote):
    """What to send and what the server has extra.

    Returns {"send": [rel...], "same": n, "extra": [rel...]}, sorted.
    """
    send, same = [], 0
    for rel, (_full, size, sha) in local.items():
        theirs = remote.get(rel)
        if theirs and theirs.get("sha256") == sha and theirs.get("size") == size:
            same += 1
        else:
            send.append(rel)
    extra = sorted(set(remote) - set(local))
    return {"send": sorted(send), "same": same, "extra": extra}


def snapshot(database, dest):
    """A consistent copy of a live SQLite file, taken with SQLite's backup API
    so a write in progress can't tear it."""
    src = sqlite3.connect(database)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


class Remote:
    """The server's upload endpoints, over HTTPS with the bearer token."""

    def __init__(self, base_url, token, timeout=900):
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "https" and parts.hostname not in LOCAL_HOSTS:
            raise ValueError(f"refusing to send the token over {parts.scheme}:// "
                             f"— use the https address")
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        from tools.fetch_avatars import _ssl_context
        self.context = _ssl_context() if parts.scheme == "https" else None

    def _call(self, method, path, body=None, size=None, sha=None):
        headers = {"Authorization": f"Bearer {self.token}",
                   "User-Agent": "creatorwise-push"}
        if body is not None:
            headers["Content-Length"] = str(size)
            headers["Content-Type"] = "application/octet-stream"
            headers["X-Content-SHA256"] = sha
        request = urllib.request.Request(self.base + path, data=body,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self.context) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode("utf-8", "replace")
            raise OSError(f"{method} {path}: HTTP {error.code} {detail}") from error
        except urllib.error.URLError as error:
            raise OSError(f"{method} {path}: {error.reason}") from error

    def manifest(self):
        return self._call("GET", "/api/data/manifest")

    def put_file(self, rel, full, size, sha):
        with open(full, "rb") as handle:
            return self._call("PUT", "/api/data/file/" + urllib.parse.quote(rel),
                              handle, size, sha)

    def delete_file(self, rel):
        return self._call("DELETE", "/api/data/file/" + urllib.parse.quote(rel))

    def payment_details(self):
        """The server's payment details and payment statuses
        ({"rows": [...], "payments": [...]}), or None if it predates them."""
        try:
            return self._call("GET", "/api/data/payment-details")
        except OSError as error:
            if "HTTP 404" in str(error):
                return None
            raise

    def put_db(self, path):
        size, sha = os.path.getsize(path), sha256_of(path)
        with open(path, "rb") as handle:
            return self._call("PUT", "/api/data/db", handle, size, sha)


def megabytes(n):
    return f"{n / 1024 / 1024:,.1f} MB"


def push(remote, root, database, apply_changes, prune, say=print):
    """Compare, then (with apply_changes) send. Returns the comparison and
    a list of failures."""
    local = local_files(root)
    theirs = remote.manifest()
    diff = compare(local, theirs.get("files") or {})
    to_send = sum(local[rel][1] for rel in diff["send"])
    say(f"{len(local)} files here, {diff['same']} already on the server, "
        f"{len(diff['send'])} to send ({megabytes(to_send)}).")
    if diff["extra"]:
        verb = "will be removed" if prune else "kept (use --prune to remove)"
        say(f"{len(diff['extra'])} on the server but not here — {verb}:")
        for rel in diff["extra"][:20]:
            say(f"  {rel}")
    say(f"Database: {megabytes(os.path.getsize(database))} will replace the "
        f"server's" + (f" ({megabytes(theirs['database']['size'])})"
                       if theirs.get("database") else " (it has none)") + ".")
    if not apply_changes:
        say("(dry run — nothing sent. Re-run with --apply.)")
        return diff, []

    failures = []
    for i, rel in enumerate(diff["send"], 1):
        full, size, sha = local[rel]
        for attempt in (1, 2):
            try:
                remote.put_file(rel, full, size, sha)
                break
            except OSError as error:
                if attempt == 2:
                    failures.append((rel, str(error)))
        if i % 25 == 0 or i == len(diff["send"]):
            say(f"  sent {i}/{len(diff['send'])}")
    if prune:
        for rel in diff["extra"]:
            try:
                remote.delete_file(rel)
            except OSError as error:
                failures.append((rel, str(error)))

    # Payment details are the one thing typed in on the live site. Take the
    # server's newer ones into the local database first, or this upload
    # would replace them with the Mac's older copy.
    try:
        synced = remote.payment_details()
    except OSError as error:
        failures.append(("payment details", str(error)))
        for rel, err in failures:
            say(f"FAILED {rel}: {err}")
        say("Database NOT replaced: couldn't read the server's payment details, "
            "and replacing it could lose ones entered there.")
        return diff, failures
    synced = synced or {}
    if synced.get("rows") or synced.get("payments"):
        import payments
        conn = sqlite3.connect(database)
        try:
            taken = payments.merge_rows(conn, synced.get("rows") or [])
            marked = payments.merge_payments(conn, synced.get("payments") or [])
        finally:
            conn.close()
        if taken:
            say(f"Kept {taken} payment detail edit(s) made on the live site.")
        if marked:
            say(f"Kept {marked} payment status change(s) made on the live site.")

    with tempfile.TemporaryDirectory() as scratch:
        copy = snapshot(database, os.path.join(scratch, "creatorwise.db"))
        try:
            remote.put_db(copy)
            say("Database replaced.")
        except OSError as error:
            failures.append(("database", str(error)))
    for rel, error in failures:
        say(f"FAILED {rel}: {error}")
    return diff, failures


def main(argv=None):
    import argparse

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="the deployed site (default: SYNC_URL)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--prune", action="store_true",
                        help="remove server files that aren't here")
    args = parser.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(here, ".env"))
    url = args.url or os.environ.get("SYNC_URL")
    token = os.environ.get("SYNC_TOKEN")
    if not url or not token:
        print("Set SYNC_URL and SYNC_TOKEN (in .env or the environment).")
        return 2

    root = os.path.join(here, "data")
    database = os.path.join(root, "creatorwise.db")
    remote = Remote(url, token)
    _diff, failures = push(remote, root, database, args.apply, args.prune)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
