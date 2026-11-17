#!/usr/bin/env python3
"""Attach saved videos and carousel images to their pieces.

creatorwise does not download from Instagram or TikTok — both gate their
media against automated readers, and getting past that by pretending to be a
browser is not something this project does. A person saves each post with the
platform's own controls; this files what they saved.

Two steps:

    # 1. One folder per post, each with a link that opens the post.
    python3 tools/import_media.py --prepare --campaign bookwise-stickers-beta

    #    -> data/media-inbox/bookwise-stickers-beta/011 Bea Lune - Instagram post/
    #       Drop the saved video, or every carousel slide in order, in there.

    # 2. File whatever has arrived.
    python3 tools/import_media.py --campaign bookwise-stickers-beta
    python3 tools/import_media.py --campaign bookwise-stickers-beta --apply

Files are recognised by their bytes, not their names. iPhone HEIC photos are
converted to JPEG and videos to H.264 MP4 so every browser can show them.
Carousel slides keep the order of their file names, so save them in order.
Dry run unless --apply; re-running replaces a piece's media only when the
files in its folder have changed.
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media  # noqa: E402
from models import Campaign  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
IGNORED = {".DS_Store", "README.txt"}
# One video saved from two platforms comes out within hundredths of a second
# of itself; two different short reels by one creator can be 0.2s apart.
SAME_LENGTH = 0.15


# --- folders --------------------------------------------------------------

def kind_of(url):
    """What a post link says it is, as far as the URL can tell."""
    u = (url or "").lower()
    if "tiktok.com" in u:
        if "/photo/" in u:
            return "carousel"
        if "/video/" in u:
            return "video"
        return "post"            # /t/ shortlinks say nothing
    if "instagram.com" in u:
        if "/reel" in u:
            return "video"
        return "post"            # /p/ is a carousel or a video; can't tell
    if "youtube.com" in u or "youtu.be" in u:
        return "video"
    return "post"


def folder_name(piece):
    platforms = " + ".join(sorted({p.platform_label for p in piece.posts})) or "Post"
    kinds = {kind_of(p.url) for p in piece.posts}
    kind = "video" if kinds == {"video"} else "carousel" if "carousel" in kinds else "post"
    name = re.sub(r'[/:\\"*?<>|]+', "-", piece.byline)
    return f"{piece.id:03d} {name} - {platforms} {kind}"


def piece_id_of(folder):
    match = re.match(r"(\d+)\b", folder)
    return int(match.group(1)) if match else None


def prepare(campaign, root):
    """Make a folder per piece with a clickable link to each of its posts."""
    base = os.path.join(root, campaign.slug)
    os.makedirs(base, exist_ok=True)
    existing = {piece_id_of(n): n for n in os.listdir(base) if piece_id_of(n)}
    made = 0
    for piece in sorted(campaign.ugc, key=lambda p: p.id):
        name = existing.get(piece.id) or folder_name(piece)
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            os.makedirs(path)
            made += 1
        for post in piece.posts:
            if not post.url:
                continue
            link = os.path.join(path, f"Open on {post.platform_label}.webloc")
            with open(link, "wb") as handle:
                plistlib.dump({"URL": post.url}, handle)
    with open(os.path.join(base, "README.txt"), "w") as handle:
        handle.write(
            "One folder per post. Open the link inside, save the post with the\n"
            "platform's own Save/Download option, and drop the file here.\n"
            "Carousels: save every slide, in order. Cross-posted pieces need\n"
            "only one copy. Then run tools/import_media.py.\n"
        )
    return made, len(campaign.ugc)


# --- planning ---------------------------------------------------------------

# "Screenshot 2026-11-11 at 1.02.07 PM.png", "CleanShot 2026-11-11 at 12.59.40 PM@2x.png":
# a 12-hour clock, so 12.59 PM must sort before 1.02 PM.
CLOCK = re.compile(r"(\d{4}-\d{2}-\d{2}) at (\d{1,2})[.:](\d{2})[.:](\d{2})\s*([AP]M)", re.I)


def natural(name):
    m = CLOCK.search(name)
    if m:
        day, h, mi, se, half = m.groups()
        h = int(h) % 12 + (12 if half.upper() == "PM" else 0)
        name = name[:m.start()] + f"{day} {h:02d}.{mi}.{se}" + name[m.end():]
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def read_head(path):
    with open(path, "rb") as handle:
        return handle.read(16)


def duration(path):
    try:
        return float(run([FFPROBE, "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", path]))
    except (RuntimeError, ValueError):
        return None


def same_video(piece, seconds, pieces, store, claimed):
    """Another piece by the same people whose video is this long, or None.

    One video posted to Instagram and TikTok is one piece. A sheet that lists
    its links on separate rows makes two, and saving the file twice would put
    the same video on the page twice — so a match is held, not imported.
    """
    people = {c.id for c in piece.creators}
    for other in pieces.values():
        if other.id == piece.id or not people & {c.id for c in other.creators}:
            continue
        known = claimed.get(other.id)
        if known is None:
            stored = media.listing(store, other.id)
            if stored["video"]:
                known = duration(os.path.join(media.piece_dir(store, other.id),
                                              stored["video"]))
        if known is not None and abs(known - seconds) < SAME_LENGTH:
            return other
    return None


def plan(campaign, root, store, checked=()):
    """What importing would do, per piece. Touches nothing.

    `checked` names pieces a person has looked at and found to be a different
    video from any same-length one by the same creator (two reels can share a
    length to the hundredth). Only those skip the posted-twice check.
    """
    base = os.path.join(root, campaign.slug)
    pieces = {p.id: p for p in campaign.ugc}
    claimed = {}   # piece id -> length of the video this run would store
    result = {"import": [], "unchanged": [], "waiting": [], "problems": []}
    folders = sorted(os.listdir(base)) if os.path.isdir(base) else []
    for folder in folders:
        path = os.path.join(base, folder)
        piece_id = piece_id_of(folder)
        if not os.path.isdir(path) or piece_id is None:
            continue
        if piece_id not in pieces:
            result["problems"].append((folder, "no piece with that id in this campaign"))
            continue
        videos, images, unknown = [], [], []
        for name in sorted(os.listdir(path), key=natural):
            full = os.path.join(path, name)
            if name in IGNORED or name.endswith(".webloc") or not os.path.isfile(full):
                continue
            kind = media.sniff(read_head(full))
            if kind == "video":
                videos.append(full)
            elif kind in (".jpg", ".png", ".webp", "heic"):
                images.append(full)
            else:
                unknown.append(name)
        if unknown:
            result["problems"].append((folder, "not a video or image: " + ", ".join(unknown)))
        if len(videos) > 1:
            result["problems"].append((folder, f"{len(videos)} videos — one per post, please"))
            continue
        if not videos and not images:
            result["waiting"].append(folder)
            continue
        if videos:
            seconds = duration(videos[0])
            twin = (seconds and piece_id not in checked
                    and same_video(pieces[piece_id], seconds, pieces,
                                   store, claimed))
            if twin:
                result["problems"].append((
                    folder, f"same length ({seconds:.0f}s) as piece {twin.id} by "
                            f"the same creator — probably one video posted twice; "
                            f"merge the pieces instead of importing it again"))
                continue
            if seconds:
                claimed[piece_id] = seconds
        files = videos + images
        fingerprint = [[os.path.basename(f), os.path.getsize(f)] for f in files]
        stored = media.listing(store, piece_id)
        entry = {"piece_id": piece_id, "folder": folder,
                 "video": videos[0] if videos else None, "images": images,
                 "fingerprint": fingerprint}
        if stored["source"].get("files") == fingerprint:
            result["unchanged"].append(entry)
        else:
            result["import"].append(entry)
    return result


# --- applying ---------------------------------------------------------------

def run(cmd):
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip().splitlines()[-1] if done.stderr else "failed")
    return done.stdout


def video_codec(path):
    out = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=codec_name", "-of", "csv=p=0", path])
    return out.strip()


def store_video(source, target):
    """H.264 MP4 every browser plays. Copies the stream when it already is."""
    if video_codec(source) == "h264":
        cmd = [FFMPEG, "-y", "-v", "error", "-i", source, "-c", "copy",
               "-movflags", "+faststart", target]
    else:
        cmd = [FFMPEG, "-y", "-v", "error", "-i", source, "-c:v", "libx264",
               "-crf", "23", "-preset", "veryfast", "-c:a", "aac",
               "-movflags", "+faststart", target]
    run(cmd)


def store_poster(video, target):
    for offset in ("1", "0"):
        try:
            run([FFMPEG, "-y", "-v", "error", "-ss", offset, "-i", video,
                 "-frames:v", "1", "-vf", "scale=480:-2", target])
            if os.path.getsize(target):
                return
        except RuntimeError:
            continue


def store_image(source, target_stem):
    kind = media.sniff(read_head(source))
    if kind == "heic":
        target = target_stem + ".jpg"
        run(["sips", "-s", "format", "jpeg", source, "--out", target])
    else:
        target = target_stem + kind
        shutil.copyfile(source, target)
    return os.path.basename(target)


def apply(result, store):
    """Write each piece's media into a fresh folder, then swap it in."""
    done, failures = 0, []
    for entry in result["import"]:
        final = media.piece_dir(store, entry["piece_id"])
        work = tempfile.mkdtemp(dir=store)
        try:
            if entry["video"]:
                video = os.path.join(work, media.VIDEO)
                store_video(entry["video"], video)
                store_poster(video, os.path.join(work, media.POSTER))
            for i, image in enumerate(entry["images"], start=1):
                store_image(image, os.path.join(work, f"image-{i:02d}"))
            # A transcript belongs to the media it was made from; keep it only
            # when this is a re-import of the same kind of thing.
            old = media.listing(store, entry["piece_id"])
            if old["transcript"] and bool(old["video"]) == bool(entry["video"]):
                shutil.copyfile(os.path.join(final, media.TRANSCRIPT),
                                os.path.join(work, media.TRANSCRIPT))
            with open(os.path.join(work, media.SOURCE), "w") as handle:
                json.dump({"files": entry["fingerprint"], "folder": entry["folder"],
                           "imported_on": date.today().isoformat()}, handle, indent=1)
            if os.path.isdir(final):
                shutil.rmtree(final)
            os.rename(work, final)
            done += 1
        except Exception as error:   # one bad file must not stop the rest
            shutil.rmtree(work, ignore_errors=True)
            failures.append((entry["folder"], str(error)))
    return done, failures


def render(campaign, result):
    out = [f"Campaign: {campaign.name}"]
    add = out.append
    videos = sum(1 for e in result["import"] if e["video"])
    carousels = sum(1 for e in result["import"] if not e["video"])
    add(f"{len(result['import'])} to import ({videos} videos, {carousels} carousels), "
        f"{len(result['unchanged'])} unchanged, {len(result['waiting'])} folders still empty")
    for entry in result["import"]:
        what = "video" if entry["video"] else f"{len(entry['images'])} images"
        add(f"    {entry['folder']}: {what}")
    if result["problems"]:
        add("")
        add(f"PROBLEMS ({len(result['problems'])}) — skipped:")
        for folder, why in result["problems"]:
            add(f"    {folder}: {why}")
    return "\n".join(out)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, metavar="SLUG")
    parser.add_argument("--prepare", action="store_true",
                        help="make the drop folders and stop")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--not-a-twin", type=int, action="append", default=[],
                        metavar="PIECE",
                        help="import this piece's video even though another of "
                             "the creator's videos is the same length — only "
                             "after checking by eye/ear that it is different")
    args = parser.parse_args(argv)

    from app import app

    with app.app_context():
        campaign = Campaign.query.filter_by(slug=args.campaign).first()
        if campaign is None:
            print(f"No campaign with slug {args.campaign!r}")
            return 1
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        root, store = media.inbox(uri), media.directory(uri, create=True)

        if args.prepare:
            made, total = prepare(campaign, root)
            print(f"{total} folders under {os.path.join(root, campaign.slug)} "
                  f"({made} new)")
            return 0

        result = plan(campaign, root, store, checked=set(args.not_a_twin))
        print(render(campaign, result))
        if not args.apply:
            print("\n(dry run — nothing written. Re-run with --apply.)")
            return 0
        done, failures = apply(result, store)
        print(f"\nDone: {done} pieces stored, {len(failures)} failed")
        for folder, why in failures:
            print(f"    FAILED {folder}: {why}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
