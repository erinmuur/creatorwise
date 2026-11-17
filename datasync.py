"""Receive the database, media and photos from the machine that holds them.

The real data lives on Erin's Mac: `data/` is gitignored because this
repository is public, so a deploy brings the code and nothing else. These
endpoints let `tools/push_data.py` copy the data across to the deployed
instance directly, never through GitHub.

Everything here is behind SYNC_TOKEN, a bearer token of its own (not
INGEST_TOKEN: that one is for mail clients; this one can replace the whole
database). Unset means every endpoint answers 503 — closed, not open.

    GET    /api/data/manifest      every stored file: size and sha256
    PUT    /api/data/file/<path>   one media file or photo, checked by sha256
    DELETE /api/data/file/<path>   one media file or photo
    PUT    /api/data/db            the whole database, replaced in one step

Only paths shaped like what media.py and avatars.py store are accepted —
`media/<piece>/video.mp4`, `avatars/<creator>.jpg` and so on — so nothing
else in the data directory can be read, written or removed this way.

A file is written to a temporary name beside its target and renamed into
place only once its hash matches, so a broken upload never leaves half a
video. The database is checked (SQLite header, integrity_check) before it
replaces anything, and the server's previous copy is kept as
`creatorwise.db.bak-sync`.
"""

import hashlib
import hmac
import os
import re
import sqlite3
import tempfile

from flask import Blueprint, current_app, jsonify, request

import avatars
import media

bp = Blueprint("datasync", __name__)

ENDPOINTS = {"datasync.manifest", "datasync.put_file",
             "datasync.delete_file", "datasync.put_db"}

ALLOWED = re.compile(
    r"^(?:media/\d+/(?:video\.mp4|poster\.jpg|transcript\.txt|source\.json"
    r"|image-\d+\.(?:jpg|png|webp))"
    r"|avatars/\d+\.(?:jpg|png|webp))$"
)
CHUNK = 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"

# (path, size, mtime_ns) -> sha256, so a manifest doesn't re-read 2 GB of
# video every time. Per worker; a changed file has a new mtime and misses.
_hashes = {}


def _token_ok():
    expected = os.environ.get("SYNC_TOKEN") or ""
    if not expected:
        return None
    given = request.headers.get("Authorization", "")
    return hmac.compare_digest(given.encode(), f"Bearer {expected}".encode())


@bp.before_request
def _require_token():
    ok = _token_ok()
    if ok is None:
        return jsonify(error="SYNC_TOKEN is not configured"), 503
    if not ok:
        return jsonify(error="unauthorized"), 401
    return None


def data_root():
    return current_app.config.get("DATA_DIR") or avatars.data_dir(
        current_app.config["SQLALCHEMY_DATABASE_URI"])


def database_path():
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite:///") or uri == "sqlite://":
        return None
    return uri.replace("sqlite:////", "/") if uri.startswith("sqlite:////") \
        else uri.replace("sqlite:///", "")


def sha256_of(path):
    stat = os.stat(path)
    key = (path, stat.st_size, stat.st_mtime_ns)
    if key not in _hashes:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(CHUNK), b""):
                digest.update(block)
        _hashes[key] = digest.hexdigest()
    return _hashes[key]


def stored_files(root):
    """{relative path: absolute path} for every syncable file under root."""
    found = {}
    for top in ("media", "avatars"):
        base = os.path.join(root, top)
        for folder, _dirs, names in os.walk(base):
            for name in names:
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if ALLOWED.match(rel):
                    found[rel] = full
    return found


def _target(relpath):
    if not ALLOWED.match(relpath or ""):
        return None
    return os.path.join(data_root(), *relpath.split("/"))


def _receive(folder, expected_sha):
    """Stream the request body to a temp file in `folder`. Returns its path,
    or raises ValueError if the body doesn't hash to `expected_sha`."""
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".upload-")
    digest = hashlib.sha256()
    try:
        with os.fdopen(handle, "wb") as out:
            while True:
                block = request.stream.read(CHUNK)
                if not block:
                    break
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != (expected_sha or "").lower():
            raise ValueError(f"body hashes to {digest.hexdigest()}, "
                             f"expected {expected_sha}")
        return temp
    except BaseException:
        os.unlink(temp)
        raise


@bp.route("/api/data/manifest")
def manifest():
    root = data_root()
    files = {rel: {"size": os.path.getsize(full), "sha256": sha256_of(full)}
             for rel, full in stored_files(root).items()}
    db_path = database_path()
    database = None
    if db_path and os.path.isfile(db_path):
        database = {"size": os.path.getsize(db_path)}
    return jsonify(files=files, database=database)


@bp.route("/api/data/file/<path:relpath>", methods=["PUT"])
def put_file(relpath):
    target = _target(relpath)
    if target is None:
        return jsonify(error=f"not a path that can be synced: {relpath}"), 400
    try:
        temp = _receive(os.path.dirname(target),
                        request.headers.get("X-Content-SHA256"))
    except ValueError as bad:
        return jsonify(error=str(bad)), 400
    os.replace(temp, target)
    return jsonify(stored=relpath, size=os.path.getsize(target))


@bp.route("/api/data/file/<path:relpath>", methods=["DELETE"])
def delete_file(relpath):
    target = _target(relpath)
    if target is None:
        return jsonify(error=f"not a path that can be synced: {relpath}"), 400
    if not os.path.isfile(target):
        return jsonify(error="no such file"), 404
    os.unlink(target)
    folder = os.path.dirname(target)
    if relpath.startswith("media/") and not os.listdir(folder):
        os.rmdir(folder)
    return jsonify(deleted=relpath)


def check_database(path):
    """None if `path` is a sound SQLite database, else what's wrong with it."""
    with open(path, "rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            return "not an SQLite database"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as broken:
        return f"unreadable: {broken}"
    finally:
        conn.close()
    if result != "ok":
        return f"integrity_check says {result}"
    if not {"creators", "campaigns"} <= tables:
        return "doesn't look like a creatorwise database"
    return None


def _copy_database(source, dest):
    src = sqlite3.connect(source)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


@bp.route("/api/data/db", methods=["PUT"])
def put_db():
    live = database_path()
    if live is None:
        return jsonify(error="this instance has no SQLite file to replace"), 400
    try:
        temp = _receive(os.path.dirname(live),
                        request.headers.get("X-Content-SHA256"))
    except ValueError as bad:
        return jsonify(error=str(bad)), 400
    try:
        problem = check_database(temp)
        if problem:
            return jsonify(error=f"refused: {problem}"), 400
        if os.path.isfile(live):
            _copy_database(live, live + ".bak-sync")
        # Copy page by page into the live file under SQLite's own lock, rather
        # than renaming over it: the other worker keeps an open handle, and a
        # rename would leave it reading the old, unlinked file.
        _copy_database(temp, live)
    finally:
        os.unlink(temp)
    from models import db
    db.engine.dispose()
    return jsonify(replaced="database", size=os.path.getsize(live))
