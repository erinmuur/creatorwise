"""Creator profile photos, kept on disk beside the database.

One file per creator, `<data dir>/avatars/<creator id>.<ext>`. No column and
no table: whether a creator has a photo is whether the file exists, so adding
this needed no migration and removing a photo is deleting a file.

They live under `data/` on purpose. It is gitignored — this repository is
public and a creator's face is exactly the kind of thing that must never be
committed — and it is the directory contextone mounts as the persistent
volume, so photos survive a redeploy alongside the database they belong to.
"""

import os

# What we will store, by the bytes the file starts with. The check is on
# content, not on a URL's extension or a Content-Type header: a CDN that
# answers a missing object with a 200 and an HTML page would otherwise leave
# "photos" on disk that no browser can draw.
_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
)
EXTENSIONS = (".jpg", ".png", ".webp")
MAX_BYTES = 5 * 1024 * 1024


def data_dir(database_uri):
    """The directory the SQLite file lives in, from its SQLAlchemy URI."""
    uri = database_uri or ""
    if uri.startswith("sqlite:////"):
        return os.path.dirname(uri.replace("sqlite:////", "/"))
    if uri.startswith("sqlite:///"):
        return os.path.dirname(uri.replace("sqlite:///", ""))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def directory(database_uri, create=False):
    path = os.path.join(data_dir(database_uri), "avatars")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def sniff(content):
    """The extension for these bytes, or None if they aren't an image we keep."""
    if not content:
        return None
    for signature, extension in _SIGNATURES:
        if content.startswith(signature):
            return extension
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


def find(folder, creator_id):
    """Path to a creator's photo, or None."""
    for extension in EXTENSIONS:
        path = os.path.join(folder, f"{int(creator_id)}{extension}")
        if os.path.isfile(path):
            return path
    return None


def ids(folder):
    """Every creator id that has a photo — one directory listing, not N stats."""
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    out = set()
    for name in names:
        stem, extension = os.path.splitext(name)
        if extension in EXTENSIONS and stem.isdigit():
            out.add(int(stem))
    return out


def save(folder, creator_id, content):
    """Write a creator's photo, replacing any earlier one. Returns the path.

    Raises ValueError for anything that isn't a plausible image, so a bad
    download fails loudly instead of becoming a broken picture in the UI.
    """
    if len(content or b"") > MAX_BYTES:
        raise ValueError(f"{len(content):,} bytes is too large for a profile photo")
    extension = sniff(content)
    if extension is None:
        raise ValueError("not a JPEG, PNG or WebP image")

    os.makedirs(folder, exist_ok=True)
    for old in EXTENSIONS:
        stale = os.path.join(folder, f"{int(creator_id)}{old}")
        if os.path.isfile(stale):
            os.remove(stale)

    path = os.path.join(folder, f"{int(creator_id)}{extension}")
    with open(path, "wb") as handle:
        handle.write(content)
    return path
