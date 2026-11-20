#!/usr/bin/env python3
"""Get a post's media into its drop folder — the steps between the browser
pane and tools/import_media.py, for the content-by-email task.

Nothing here downloads from Instagram or TikTok. The media arrives the ways
this project allows (see CLAUDE.md, "Saved media and transcripts"):

    record-start / record-finish   an Instagram reel, recorded as it plays in
                                   the browser pane (screen + Claude's audio)
    downloads-busy / take-download a TikTok saved with TikTok's own
                                   right-click "Download video"
    slide                          a carousel slide, cropped from a pane
                                   screenshot
    attach                         files Erin attached to her email
    transcript                     a transcript read off the page (YouTube's
                                   "Show transcript" panel)

Raw window recordings are kept outside data/ and deleted once cut.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT = os.path.join(HERE, "screen_record.swift")
CACHE = os.path.expanduser("~/Library/Caches/creatorwise")
BINARY = os.path.join(CACHE, "screen_record")
WORK = os.path.join(tempfile.gettempdir(), "creatorwise-capture")
DOWNLOADS = os.path.expanduser("~/Downloads")


def die(msg):
    print(msg)
    sys.exit(1)


# --- recording a reel ---------------------------------------------------------

def recorder():
    """The compiled recorder, rebuilt whenever the Swift source changes."""
    if not os.path.exists(BINARY) or os.path.getmtime(BINARY) < os.path.getmtime(SWIFT):
        os.makedirs(CACHE, exist_ok=True)
        subprocess.run(["swiftc", "-O", SWIFT, "-o", BINARY], check=True)
    return BINARY


def raw_paths(tag):
    os.makedirs(WORK, exist_ok=True)
    return os.path.join(WORK, f"{tag}.mp4"), os.path.join(WORK, f"{tag}.log")


def record_start(tag, seconds):
    """Start recording in the background; print when frames began."""
    video, log = raw_paths(tag)
    for path in (video, log):
        if os.path.exists(path):
            os.unlink(path)
    with open(log, "w") as out:
        subprocess.Popen([recorder(), video, str(seconds)], stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        text = open(log).read()
        for line in text.splitlines():
            if line.startswith("recording "):
                print(line)
                return
        if "error" in text or "failed" in text or "no Claude window" in text:
            die("recorder failed: " + text.strip() +
                ("\nScreen Recording permission for Claude is needed (System Settings → "
                 "Privacy & Security)." if "TCC" in text or "declined" in text else ""))
        time.sleep(0.2)
    die("recorder did not start: " + open(log).read().strip())


def _components(mask):
    """Bounding boxes and sizes of the 4-connected regions of a boolean grid."""
    h, w = mask.shape
    seen = [[False] * w for _ in range(h)]
    rows = mask.tolist()
    out = []
    for y in range(h):
        for x in range(w):
            if not rows[y][x] or seen[y][x]:
                continue
            stack = [(y, x)]
            seen[y][x] = True
            n, x0, x1, y0, y1 = 0, x, x, y, y
            while stack:
                cy, cx = stack.pop()
                n += 1
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and rows[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((ny, nx))
            out.append((x0, y0, x1, y1, n))
    return out


def motion_box(video, start, duration, aspect, scale=4):
    """Where the reel is in the window recording.

    The reel is a region that keeps changing while it plays — but so can
    other things in the window (this conversation scrolling, a spinner). So
    every moving region is found, and the one with the reel's own shape
    (`aspect` = videoWidth / videoHeight, read from the page) wins.
    """
    import numpy as np
    w, h = map(int, subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", video],
        capture_output=True, text=True, check=True).stdout.strip().split(","))
    sw, sh = w // scale, h // scale
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(start + 0.5), "-t", str(max(duration - 1, 1)),
         "-i", video, "-vf", f"fps=4,scale={sw}:{sh},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, sh, sw).astype(np.float32)
    if len(frames) < 4:
        return None
    moving = frames.std(axis=0) > 6
    # Close small gaps (a still patch of wall in the reel) before grouping.
    grown = moving.copy()
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0), (0, 2), (0, -2)):
        grown |= np.roll(np.roll(moving, dy, axis=0), dx, axis=1)
    best = None
    for x0, y0, x1, y1, n in _components(grown):
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bw * bh < 0.01 * sw * sh:
            continue
        fill = n / (bw * bh)
        if fill < 0.6:
            continue
        miss = abs(np.log((bw / bh) / aspect))
        if miss > 0.15:          # not the reel's shape
            continue
        score = miss - 0.000001 * bw * bh
        if best is None or score < best[0]:
            best = (score, x0, y0, bw, bh)
    if best is None:
        return None
    _, x0, y0, bw, bh = best
    # Trim the 2-cell growth back off each side.
    x0, y0, bw, bh = x0 + 2, y0 + 2, max(bw - 4, 2), max(bh - 4, 2)
    # Then trim to the reel's exact shape, dropping whichever edge moves
    # least (a page margin or a floating icon caught alongside it).
    region = moving[y0:y0 + bh, x0:x0 + bw]
    cols, rows = region.mean(axis=0), region.mean(axis=1)
    a, b = 0, bw
    while (b - a) / bh > aspect * 1.01 and b - a > 2:
        if cols[a] <= cols[b - 1]:
            a += 1
        else:
            b -= 1
    c, d = 0, bh
    while bw and (b - a) / (d - c) < aspect / 1.01 and d - c > 2:
        if rows[c] <= rows[d - 1]:
            c += 1
        else:
            d -= 1
    x0, y0, bw, bh = x0 + a, y0 + c, b - a, d - c
    x, y, cw, ch = x0 * scale, y0 * scale, bw * scale, bh * scale
    return int(x), int(y), int(cw - cw % 2), int(ch - ch % 2)


def record_finish(tag, playing, duration, folder, aspect):
    """Wait for the recorder, cut the reel out, file it, delete the raw take."""
    video, log = raw_paths(tag)
    deadline = time.time() + duration + 60
    while time.time() < deadline:
        text = open(log).read() if os.path.exists(log) else ""
        if "finished" in text:
            break
        if "failed" in text or "stopped" in text or "error" in text:
            die("recording failed: " + text.strip())
        time.sleep(0.5)
    else:
        die("recording never finished")
    started = None
    for line in open(log):
        if line.startswith("recording "):
            started = float(line.split()[1])
    if started is None:
        die("no start time in the recorder's log")
    offset = playing - started
    if offset < 0:
        die("the reel started playing before the recording did — record again")
    box = motion_box(video, offset, duration, aspect)
    if box is None:
        os.unlink(video)
        os.unlink(log)
        die("couldn't find the playing reel in the recording — the browser pane has to be "
            "on screen while it plays (a hidden pane isn't drawn)")
    x, y, w, h = box
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "recording.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}",
         "-i", video, "-vf", f"crop={w}:{h}:{x}:{y}", "-c:v", "libx264", "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out],
        check=True)
    loud = subprocess.run(["ffmpeg", "-hide_banner", "-i", out, "-vn", "-af", "volumedetect",
                           "-f", "null", "-"], capture_output=True, text=True).stderr
    peak = next((float(l.split(":")[1].split()[0]) for l in loud.splitlines()
                 if "max_volume" in l), -91.0)
    os.unlink(video)
    os.unlink(log)
    print(json.dumps({"file": out, "crop": [w, h], "aspect": round(w / h, 3),
                      "peak_db": peak, "silent": peak <= -90}))
    if peak <= -90:
        print("WARNING: the recording is silent — the reel was muted (Instagram re-mutes "
              "on a tap). Record it again with the sound on.")
    if abs(aspect - w / h) > 0.05:
        print(f"WARNING: crop {w}x{h} doesn't match the reel's shape — check it before importing.")


# --- TikTok's own Download ----------------------------------------------------

def _candidates(since):
    found = []
    for path in glob.glob(os.path.join(DOWNLOADS, "Download*.mp4")) + \
            glob.glob(os.path.join(DOWNLOADS, ".*claudefordesktop*")):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        if st.st_mtime >= since and st.st_size > 0:
            found.append(path)
    return found


def downloads_busy():
    """A Download*.mp4 already waiting means a download is in flight or
    unclaimed — starting another lets the pane overwrite or rename it."""
    busy = glob.glob(os.path.join(DOWNLOADS, "Download*.mp4"))
    if busy:
        die("busy: " + ", ".join(os.path.basename(b) for b in busy))
    print("clear")


def playable(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip()) > 0
    except ValueError:
        return False


def take_download(folder, since, wait=180):
    """Move the finished TikTok download into the folder.

    The pane writes a hidden temp file and renames it to Download.mp4 when
    done; moving the temp file early left 0-byte files once. So: prefer a
    Download*.mp4, take a temp file only if it has sat unchanged for a while
    and plays.
    """
    deadline = time.time() + wait
    sizes = {}
    while time.time() < deadline:
        for path in _candidates(since):
            size = os.path.getsize(path)
            prev = sizes.get(path)
            sizes[path] = size
            named = os.path.basename(path).startswith("Download")
            steady = prev == size and size > 0
            if steady and playable(path) and (named or time.time() - os.path.getmtime(path) > 20):
                os.makedirs(folder, exist_ok=True)
                target = os.path.join(folder, "video.mp4")
                shutil.move(path, target)
                print(json.dumps({"file": target, "bytes": size}))
                return
        time.sleep(2)
    die("no finished download appeared in ~/Downloads")


# --- carousel slides ----------------------------------------------------------

def slide(shot, rect, viewport_width, folder, index):
    """Crop one slide out of a browser-pane screenshot.

    `rect` is the slide's on-page rectangle (CSS pixels, from the page's own
    layout); the screenshot is the same viewport at a different pixel size.
    """
    x, y, w, h = (float(v) for v in rect.split(","))
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width",
                        "-of", "csv=p=0", shot], capture_output=True, text=True, check=True)
    k = int(r.stdout.strip().split(",")[0]) / float(viewport_width)
    cw, ch, cx, cy = (int(round(v * k)) for v in (w, h, x, y))
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, f"image-{int(index):02d}.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", shot, "-vf",
                    f"crop={cw}:{ch}:{cx}:{cy}", "-q:v", "2", out], check=True)
    print(json.dumps({"file": out, "size": [cw, ch]}))


# --- attachments and transcripts ------------------------------------------------

def attach(folder, files):
    os.makedirs(folder, exist_ok=True)
    for i, f in enumerate(sorted(files), 1):
        name = os.path.basename(f)
        target = os.path.join(folder, f"{i:02d} {name}" if len(files) > 1 else name)
        shutil.copyfile(f, target)
        print(target)


def transcript(piece_id, how, path):
    import app as appmod
    import media
    from tools import transcribe_media
    folder = media.directory(appmod.app.config["SQLALCHEMY_DATABASE_URI"], create=True)
    os.makedirs(media.piece_dir(folder, piece_id), exist_ok=True)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    transcribe_media.record(folder, piece_id, text, how)
    print(f"transcript stored for piece {piece_id} ({len(text.split())} words)")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("record-start")
    s.add_argument("--tag", required=True)
    s.add_argument("--seconds", type=float, required=True)
    s = sub.add_parser("record-finish")
    s.add_argument("--tag", required=True)
    s.add_argument("--playing", type=float, required=True, help="unix time playback began")
    s.add_argument("--duration", type=float, required=True)
    s.add_argument("--aspect", type=float, required=True,
                   help="the reel's videoWidth / videoHeight, read from the page")
    s.add_argument("--folder", required=True)
    sub.add_parser("downloads-busy")
    s = sub.add_parser("take-download")
    s.add_argument("--folder", required=True)
    s.add_argument("--since", type=float, required=True, help="unix time the download began")
    s = sub.add_parser("slide")
    s.add_argument("--shot", required=True)
    s.add_argument("--rect", required=True, help="x,y,w,h in CSS pixels")
    s.add_argument("--viewport-width", required=True, type=float)
    s.add_argument("--folder", required=True)
    s.add_argument("--index", required=True, type=int)
    s = sub.add_parser("attach")
    s.add_argument("--folder", required=True)
    s.add_argument("files", nargs="+")
    s = sub.add_parser("transcript")
    s.add_argument("--piece", required=True, type=int)
    s.add_argument("--how", required=True)
    s.add_argument("file")
    a = p.parse_args(argv)

    if a.cmd == "record-start":
        record_start(a.tag, a.seconds)
    elif a.cmd == "record-finish":
        record_finish(a.tag, a.playing, a.duration, a.folder, a.aspect)
    elif a.cmd == "downloads-busy":
        downloads_busy()
    elif a.cmd == "take-download":
        take_download(a.folder, a.since)
    elif a.cmd == "slide":
        slide(a.shot, a.rect, a.viewport_width, a.folder, a.index)
    elif a.cmd == "attach":
        attach(a.folder, a.files)
    elif a.cmd == "transcript":
        transcript(a.piece, a.how, a.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
