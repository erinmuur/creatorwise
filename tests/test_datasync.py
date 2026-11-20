"""Uploading data/ to the deployed instance (datasync.py, tools/push_data.py).

These endpoints can replace the whole database, so the guards are: nothing
without the token, nothing outside the shapes media.py and avatars.py store,
nothing half-written, and no database that isn't a sound creatorwise one.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = tempfile.mkdtemp()
os.environ.update(
    DASHBOARD_PASSWORD="test-password", SECRET_KEY="test-secret",
    INGEST_TOKEN="t", SEED_SAMPLE_DATA="0",
    DATABASE_URL="sqlite:///" + os.path.join(DATA, "creatorwise.db"),
)
os.environ.pop("SYNC_TOKEN", None)

import app as appmod  # noqa: E402
from models import Creator, db  # noqa: E402
from tools import push_data  # noqa: E402

TOKEN = "sync-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096


def sha(content):
    return hashlib.sha256(content).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["SYNC_TOKEN"] = TOKEN
        self.client = appmod.app.test_client()
        for top in ("media", "avatars"):
            shutil.rmtree(os.path.join(DATA, top), ignore_errors=True)

    def tearDown(self):
        os.environ.pop("SYNC_TOKEN", None)

    def put(self, rel, content, digest=None, headers=AUTH):
        return self.client.put(f"/api/data/file/{rel}", data=content, headers={
            **headers, "X-Content-SHA256": digest or sha(content)})

    def leftovers(self):
        return [n for _d, _s, names in os.walk(DATA) for n in names
                if n.startswith(".upload-")]


class TokenTests(Base):
    def test_closed_without_a_configured_token(self):
        os.environ.pop("SYNC_TOKEN")
        r = self.client.get("/api/data/manifest", headers=AUTH)
        self.assertEqual(r.status_code, 503)

    def test_wrong_or_missing_token_is_refused_not_redirected(self):
        self.assertEqual(self.client.get("/api/data/manifest").status_code, 401)
        r = self.client.get("/api/data/manifest",
                            headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.put("avatars/1.jpg", JPEG, headers={}).status_code, 401)
        self.assertFalse(os.path.exists(os.path.join(DATA, "avatars", "1.jpg")))

    def test_a_dashboard_login_is_not_a_token(self):
        self.client.post("/login", data={"password": "test-password"})
        self.assertEqual(self.client.get("/api/data/manifest").status_code, 401)


class FileTests(Base):
    def test_a_file_is_stored_and_listed_with_its_hash(self):
        r = self.put("media/7/video.mp4", MP4)
        self.assertEqual(r.status_code, 200, r.data)
        with open(os.path.join(DATA, "media", "7", "video.mp4"), "rb") as handle:
            self.assertEqual(handle.read(), MP4)
        files = self.client.get("/api/data/manifest", headers=AUTH).json["files"]
        self.assertEqual(files["media/7/video.mp4"],
                         {"size": len(MP4), "sha256": sha(MP4)})

    def test_a_body_that_doesnt_match_its_hash_leaves_nothing(self):
        r = self.put("media/7/video.mp4", MP4, digest=sha(b"something else"))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(DATA, "media", "7", "video.mp4")))
        self.assertEqual(self.leftovers(), [])

    def test_only_stored_shapes_can_be_written(self):
        for rel in ("creatorwise.db", "media/7/run.sh", "media/x/video.mp4",
                    "avatars/1.html", "media-inbox/a/video.mp4",
                    "media/7/../../creatorwise.db", "..%2Fcreatorwise.db"):
            with self.subTest(rel=rel):
                self.assertNotEqual(self.put(rel, JPEG).status_code, 200)
        self.assertEqual(self.leftovers(), [])

    def test_delete_removes_the_file_and_an_emptied_folder(self):
        self.put("media/7/poster.jpg", JPEG)
        r = self.client.delete("/api/data/file/media/7/poster.jpg", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(DATA, "media", "7")))
        r = self.client.delete("/api/data/file/creatorwise.db", headers=AUTH)
        self.assertEqual(r.status_code, 400)


class DatabaseTests(Base):
    def setUp(self):
        super().setUp()
        with appmod.app.app_context():
            db.create_all()
            Creator.query.delete()
            db.session.add(Creator(name="Server Copy", email="old@example.com"))
            db.session.commit()

    def replacement(self, name):
        """A creatorwise database holding one creator called `name`."""
        path = os.path.join(tempfile.mkdtemp(), "new.db")
        shutil.copyfile(os.path.join(DATA, "creatorwise.db"), path)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE creators SET name = ?", (name,))
        conn.commit()
        conn.close()
        with open(path, "rb") as handle:
            return handle.read()

    def names(self):
        with appmod.app.app_context():
            return [c.name for c in Creator.query.all()]

    def put_db(self, content, digest=None):
        return self.client.put("/api/data/db", data=content, headers={
            **AUTH, "X-Content-SHA256": digest or sha(content)})

    def test_a_sound_database_replaces_the_live_one(self):
        self.assertEqual(self.names(), ["Server Copy"])
        r = self.put_db(self.replacement("Local Copy"))
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.names(), ["Local Copy"])
        backup = sqlite3.connect(os.path.join(DATA, "creatorwise.db.bak-sync"))
        self.assertEqual(backup.execute("SELECT name FROM creators").fetchall(),
                         [("Server Copy",)])
        backup.close()

    def test_anything_else_is_refused_and_changes_nothing(self):
        bad = sqlite3.connect(os.path.join(DATA, "other.db"))
        bad.execute("CREATE TABLE t (x)")
        bad.commit()
        bad.close()
        with open(os.path.join(DATA, "other.db"), "rb") as handle:
            other = handle.read()
        os.unlink(os.path.join(DATA, "other.db"))
        for content in (b"<!doctype html>", other):
            with self.subTest(content=content[:15]):
                self.assertEqual(self.put_db(content).status_code, 400)
        good = self.replacement("Local Copy")
        self.assertEqual(self.put_db(good, digest=sha(b"x")).status_code, 400)
        self.assertEqual(self.names(), ["Server Copy"])
        self.assertEqual(self.leftovers(), [])


class TestClientRemote(push_data.Remote):
    """push_data.Remote, but talking to the Flask test client."""

    def __init__(self, client):
        self.client = client

    def _check(self, r):
        if r.status_code != 200:
            raise OSError(f"HTTP {r.status_code} {r.data[:200]}")
        return r.json

    def manifest(self):
        return self._check(self.client.get("/api/data/manifest", headers=AUTH))

    def put_file(self, rel, full, size, digest):
        with open(full, "rb") as handle:
            return self._check(self.client.put(
                f"/api/data/file/{rel}", data=handle.read(),
                headers={**AUTH, "X-Content-SHA256": digest}))

    def delete_file(self, rel):
        return self._check(self.client.delete(f"/api/data/file/{rel}", headers=AUTH))

    def put_db(self, path):
        with open(path, "rb") as handle:
            content = handle.read()
        return self._check(self.client.put("/api/data/db", data=content, headers={
            **AUTH, "X-Content-SHA256": sha(content)}))

    def payment_details(self):
        return self._check(self.client.get("/api/data/payment-details", headers=AUTH))


class PushTests(Base):
    def setUp(self):
        super().setUp()
        self.local = tempfile.mkdtemp()
        for rel, content in (("media/3/video.mp4", MP4),
                             ("media/3/transcript.txt", b"Hello\n"),
                             ("avatars/9.jpg", JPEG),
                             ("media-inbox/x/clip.mp4", MP4)):
            path = os.path.join(self.local, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content)
        with appmod.app.app_context():
            db.create_all()
        self.database = os.path.join(self.local, "creatorwise.db")
        shutil.copyfile(os.path.join(DATA, "creatorwise.db"), self.database)
        self.remote = TestClientRemote(self.client)
        self.said = []

    def tearDown(self):
        shutil.rmtree(self.local)
        super().tearDown()

    def push(self, apply_changes, prune=False):
        return push_data.push(self.remote, self.local, self.database,
                              apply_changes, prune, say=self.said.append)

    def test_a_dry_run_sends_nothing(self):
        diff, failures = self.push(False)
        self.assertEqual(len(diff["send"]), 3)    # the inbox isn't synced
        self.assertEqual(failures, [])
        self.assertEqual(self.remote.manifest()["files"], {})

    def test_apply_sends_and_a_second_run_sends_nothing(self):
        _diff, failures = self.push(True)
        self.assertEqual(failures, [])
        self.assertEqual(set(self.remote.manifest()["files"]),
                         {"media/3/video.mp4", "media/3/transcript.txt",
                          "avatars/9.jpg"})
        again, _ = self.push(True)
        self.assertEqual((again["send"], again["same"]), ([], 3))

    def test_server_extras_are_kept_unless_pruned(self):
        self.put("avatars/4.jpg", JPEG)
        diff, _ = self.push(True)
        self.assertEqual(diff["extra"], ["avatars/4.jpg"])
        self.assertIn("avatars/4.jpg", self.remote.manifest()["files"])
        self.push(True, prune=True)
        self.assertNotIn("avatars/4.jpg", self.remote.manifest()["files"])

    def test_details_entered_on_the_server_survive_a_push(self):
        from datetime import datetime
        from models import PaymentDetails
        with appmod.app.app_context():
            c = Creator(name="Server Edit", email="edit@example.com")
            db.session.add(c)
            db.session.flush()
            db.session.add(PaymentDetails(creator_id=c.id, bank_name="Example Bank",
                                          updated_at=datetime(2026, 11, 19, 12)))
            db.session.commit()
            cid = c.id
        # The Mac's copy knows the creator but not the details.
        conn = sqlite3.connect(self.database)
        conn.execute("INSERT INTO creators (id, name, email) VALUES (?, ?, ?)",
                     (cid, "Server Edit", "edit@example.com"))
        conn.commit()
        conn.close()
        _diff, failures = self.push(True)
        self.assertEqual(failures, [])
        with appmod.app.app_context():
            self.assertEqual(db.session.get(Creator, cid).payment_details.bank_name,
                             "Example Bank")

    def test_payment_details_need_the_token(self):
        self.assertEqual(self.client.get("/api/data/payment-details").status_code, 401)

    def test_the_token_never_goes_over_plain_http(self):
        with self.assertRaises(ValueError):
            push_data.Remote("http://creatorwise.example.com", TOKEN)
        push_data.Remote("http://localhost:5000", TOKEN)   # local testing is fine


if __name__ == "__main__":
    unittest.main()
