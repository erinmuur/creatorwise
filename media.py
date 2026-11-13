"""Saved copies of a piece's video or carousel images, plus its transcript.

One folder per piece, `<data dir>/media/<piece id>/`:

    video.mp4         the video, if the piece is one
    poster.jpg        a frame from it, for cards
    image-01.jpg ...  carousel slides, in order
    transcript.txt    what is said (video) or written (carousel)
    source.json       which files they came from, and when

No table: whether a piece has media is whether the folder has files in it,
the same arrangement as avatars.py. It sits under `data/` because that is
gitignored — this repository is public and creators' videos must never be
committed — and because it is the persistent volume.

creatorwise does not download from Instagram or TikTok. A person saves the
files with the platforms' own controls and drops them into the inbox
(`tools/import_media.py --prepare` makes one folder per post); this module
only stores and serves what arrives.
"""

import json
import os

from avatars import data_dir

VIDEO = "video.mp4"
POSTER = "poster.jpg"
TRANSCRIPT = "transcript.txt"
SOURCE = "source.json"
IMAGE_EXTENSIONS = (".jpg", ".png", ".webp")


def directory(database_uri, create=False):
    path = os.path.join(data_dir(database_uri), "media")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def inbox(database_uri):
    return os.path.join(data_dir(database_uri), "media-inbox")


def piece_dir(folder, piece_id):
    return os.path.join(folder, str(int(piece_id)))


def sniff(head):
    """What these leading bytes are: 'video', 'heic', an image extension, or None.

    Decided on content, never on a file name: a saved file can carry any
    extension, and an HTML error page saved as .mp4 must not be served.
    """
    if not head:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
        return "video"   # isom, mp42, qt  , M4V  ... — mp4 and mov alike
    return None


def listing(folder, piece_id):
    """Everything stored for a piece. Empty lists / None when nothing is."""
    path = piece_dir(folder, piece_id)
    try:
        names = sorted(os.listdir(path))
    except OSError:
        names = []
    images = [
        n for n in names
        if n.startswith("image-") and os.path.splitext(n)[1] in IMAGE_EXTENSIONS
    ]
    transcript = None
    if TRANSCRIPT in names:
        with open(os.path.join(path, TRANSCRIPT), encoding="utf-8") as handle:
            transcript = handle.read().strip() or None
    source = {}
    if SOURCE in names:
        with open(os.path.join(path, SOURCE), encoding="utf-8") as handle:
            source = json.load(handle)
    return {
        "video": VIDEO if VIDEO in names else None,
        "poster": POSTER if POSTER in names else None,
        "images": images,
        "transcript": transcript,
        "source": source,
    }


def servable(folder, piece_id, name):
    """Path to a stored file if `name` is one we serve for this piece, else None."""
    stored = listing(folder, piece_id)
    allowed = set(stored["images"])
    for key in ("video", "poster"):
        if stored[key]:
            allowed.add(stored[key])
    if stored["transcript"] is not None:
        allowed.add(TRANSCRIPT)
    if name not in allowed:
        return None
    return os.path.join(piece_dir(folder, piece_id), name)


def ids(folder):
    """Every piece id with a video or at least one image — one listing per piece."""
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    out = set()
    for name in names:
        if not name.isdigit():
            continue
        stored = listing(folder, int(name))
        if stored["video"] or stored["images"]:
            out.add(int(name))
    return out


def thumbnail(folder, piece_id):
    """The file a card should show: the video's poster, else the first slide."""
    stored = listing(folder, piece_id)
    return stored["poster"] or (stored["images"][0] if stored["images"] else None)
