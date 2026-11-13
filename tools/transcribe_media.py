#!/usr/bin/env python3
"""Write transcript.txt for every stored video in a campaign.

Speech is transcribed on this machine with Whisper (faster-whisper), so no
creator's audio leaves it. Carousels have no audio; their transcript is the
text on the slides, read with macOS's built-in Vision text recognition.

    python3 tools/transcribe_media.py --campaign bookwise-softlaunch
    python3 tools/transcribe_media.py --campaign bookwise-softlaunch --force

Needs the media extras, which the deployed app does not:

    ./venv/bin/pip install -r requirements-media.txt

A transcript is machine output and can be wrong — names and book titles
especially. A video with no speech gets a transcript that says so, not an
empty file and not Whisper's guesses at the music. When a piece has no hook
yet, its first spoken line becomes the hook.
"""

import json
import os
import re
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media  # noqa: E402
from models import Campaign, db  # noqa: E402

NO_SPEECH = "[No speech detected — music or on-screen text only]"
NO_TEXT = "[No text found on the slides]"
OCR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.swift")
HOOK_LIMIT = 160


def load_audio(path):
    """16 kHz mono float samples, decoded by ffmpeg.

    faster-whisper can decode files itself through PyAV, but its call no
    longer matches current PyAV releases. ffmpeg is already required by the
    importer, so it does the decoding here too.
    """
    import shutil

    import numpy as np

    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", path, "-f", "f32le", "-ac", "1",
         "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def transcribe_video(model, path, vad=True):
    """Return (lines, language). Lines are Whisper's segments, trimmed."""
    segments, info = model.transcribe(
        load_audio(path), vad_filter=vad, beam_size=5,
        condition_on_previous_text=False,
    )
    lines = [s.text.strip() for s in segments if s.text.strip()]
    return lines, info.language


_OCR_BIN = None


def ocr_command():
    """The OCR helper, compiled once per run (interpreting it per image is slow)."""
    global _OCR_BIN
    if _OCR_BIN is None:
        import tempfile
        target = os.path.join(tempfile.mkdtemp(), "ocr")
        done = subprocess.run(["swiftc", "-O", OCR_SCRIPT, "-o", target],
                              capture_output=True)
        _OCR_BIN = [target] if done.returncode == 0 else ["swift", OCR_SCRIPT]
    return _OCR_BIN


def ocr(path):
    done = subprocess.run(ocr_command() + [path], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def read_slides(paths):
    """Text on each slide, via Vision. Returns one string per slide."""
    return [ocr(path) for path in paths]


def is_caption(line):
    """Words someone put on screen, not app chrome caught in a recording.

    Screen recordings of the app read as a stream of numbers, times and
    one-word labels ("2h 27m", "+10.8%", "Pages"). A caption has at least
    two real words in it.
    """
    real = [w for w in re.findall(r"[A-Za-z']+", line) if len(w) >= 2]
    return len(real) >= 2


def screen_text(video, every=1.5):
    """Text shown on screen through a video, each line once, in order of
    first appearance. Frames are sampled every `every` seconds — captions on
    short-form video stay up for longer than that."""
    import shutil
    import tempfile

    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    work = tempfile.mkdtemp()
    try:
        subprocess.run(
            [ffmpeg, "-v", "error", "-i", video, "-vf",
             f"fps=1/{every},scale=720:-2", os.path.join(work, "f%04d.png")],
            check=True, capture_output=True,
        )
        seen, out = [], []
        for name in sorted(os.listdir(work)):
            lines = [l for l in ocr(os.path.join(work, name)).splitlines()
                     if is_caption(l)]
            # A caption is a line or a few. A frame full of text is a book
            # page or an app screen being filmed — someone else's words, and
            # not what the creator put on screen. Skip the frame.
            if len(lines) > MAX_CAPTION_LINES:
                continue
            for line in lines:
                key = " ".join(words(line))
                # The same caption read slightly differently frame to frame
                # ("Best session" / "Best sessio") is one caption.
                if any(key.startswith(k) or k.startswith(key) for k in seen):
                    continue
                seen.append(key)
                out.append(line.strip())
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


MAX_CAPTION_LINES = 8
SPARSE = 15   # fewer spoken words than this, and the video's text is read too


def with_screen_text(spoken_text, screen_lines):
    """A transcript that carries both what is said and what is shown."""
    parts = []
    if spoken_text and spoken_text != NO_SPEECH:
        parts.append("Spoken:\n" + spoken_text)
    elif screen_lines:
        parts.append("[No speech — music only]")
    else:
        parts.append(NO_SPEECH)
    if screen_lines:
        parts.append("On-screen text:\n" + "\n".join(screen_lines))
    return "\n\n".join(parts)


def reflow(lines):
    """One sentence per line. Whisper's segments break mid-sentence."""
    text = " ".join(line.strip() for line in lines if line.strip())
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)
    return "\n".join(s.strip() for s in sentences if s.strip())


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def check_edges(model, path, transcript, seconds=15):
    """Does the transcript start and end where the video's speech does?

    The first and last `seconds` of audio are transcribed on their own,
    without the voice-activity filter, and their opening / closing words must
    turn up at the start / end of the stored transcript. That catches a
    transcript cut short, or one whose filter dropped speech under music.
    Returns a list of problems; empty means both edges are covered.
    """
    audio = load_audio(path)
    span = seconds * 16000
    stored = words(transcript)
    problems = []
    for edge, clip in (("start", audio[:span]), ("end", audio[-span:])):
        segments, _ = model.transcribe(clip, beam_size=5,
                                       condition_on_previous_text=False)
        heard = words(" ".join(s.text for s in segments))
        if len(heard) < 3:
            continue          # no speech at that edge: nothing to miss
        probe = heard[:6] if edge == "start" else heard[-6:]
        window = stored[:40] if edge == "start" else stored[-40:]
        found = sum(1 for w in probe if w in window)
        if found < max(3, len(probe) // 2):
            problems.append(f"{edge}: heard '{' '.join(probe)}', not in transcript")
    seconds_long = len(audio) / 16000
    if stored and seconds_long > 20 and len(stored) / (seconds_long / 60) < 60:
        problems.append(f"only {len(stored)} words in {seconds_long:.0f}s")
    return problems


def first_line(lines):
    text = " ".join(lines).strip()
    for end in (". ", "? ", "! "):
        cut = text.find(end)
        if 0 < cut < HOOK_LIMIT:
            return text[: cut + 1]
    return text[:HOOK_LIMIT].rsplit(" ", 1)[0] + ("…" if len(text) > HOOK_LIMIT else "")


def record(folder, piece_id, text, how):
    path = media.piece_dir(folder, piece_id)
    with open(os.path.join(path, media.TRANSCRIPT), "w", encoding="utf-8") as handle:
        handle.write(text.strip() + "\n")
    source_path = os.path.join(path, media.SOURCE)
    source = {}
    if os.path.exists(source_path):
        with open(source_path) as handle:
            source = json.load(handle)
    source["transcript"] = {"how": how, "on": date.today().isoformat()}
    with open(source_path, "w") as handle:
        json.dump(source, handle, indent=1)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, metavar="SLUG")
    parser.add_argument("--model", default="small",
                        help="Whisper model size (default: small)")
    parser.add_argument("--force", action="store_true",
                        help="redo pieces that already have a transcript")
    parser.add_argument("--no-hooks", action="store_true",
                        help="don't fill empty hooks from the transcript")
    parser.add_argument("--redo", action="store_true",
                        help="with --verify: re-transcribe flagged videos "
                             "without the speech filter (check for song lyrics)")
    parser.add_argument("--screen-text", action="store_true",
                        help="add on-screen text to existing transcripts with "
                             "little or no speech")
    parser.add_argument("--verify", action="store_true",
                        help="check every video transcript reaches both ends of "
                             "the speech; redo failures without the speech filter")
    args = parser.parse_args(argv)

    from app import app

    with app.app_context():
        campaign = Campaign.query.filter_by(slug=args.campaign).first()
        if campaign is None:
            print(f"No campaign with slug {args.campaign!r}")
            return 1
        folder = media.directory(app.config["SQLALCHEMY_DATABASE_URI"])

        if args.screen_text:
            done = 0
            for piece in sorted(campaign.ugc, key=lambda p: p.id):
                stored = media.listing(folder, piece.id)
                text = stored["transcript"] or ""
                if not stored["video"] or "On-screen text:" in text:
                    continue
                spoken = "" if text == NO_SPEECH else text
                if len(words(spoken)) >= SPARSE:
                    continue
                shown = screen_text(
                    os.path.join(media.piece_dir(folder, piece.id), stored["video"]))
                record(folder, piece.id, with_screen_text(spoken or None, shown),
                       "whisper + on-screen text (macOS Vision)")
                # No hook from on-screen text: OCR of stylised captions reads
                # too poorly ("ONI MANN oF") to stand as the opening line.
                done += 1
                print(f"    {piece.id:>4}  {len(shown):>3} on-screen lines  {piece.byline}")
            print(f"Done: {done} transcripts given their on-screen text")
            return 0

        if args.verify:
            from faster_whisper import WhisperModel
            model = WhisperModel(args.model, device="cpu", compute_type="int8")
            failed = 0
            for piece in sorted(campaign.ugc, key=lambda p: p.id):
                stored = media.listing(folder, piece.id)
                if not (stored["video"] and stored["transcript"]):
                    continue
                video = os.path.join(media.piece_dir(folder, piece.id), stored["video"])
                problems = check_edges(model, video, stored["transcript"])
                if not problems:
                    # Transcripts from before one-sentence-per-line get the
                    # same layout; the words themselves are untouched.
                    text = stored["transcript"]
                    if text != NO_SPEECH and reflow(text.splitlines()) != text:
                        path = os.path.join(media.piece_dir(folder, piece.id),
                                            media.TRANSCRIPT)
                        with open(path, "w", encoding="utf-8") as handle:
                            handle.write(reflow(text.splitlines()) + "\n")
                    print(f"    ok   {piece.id:>4}  {piece.byline}")
                    continue
                failed += 1
                print(f"    FLAG {piece.id:>4}  {piece.byline}: {'; '.join(problems)}")
                # Not redone automatically: on a music-only video, what the
                # edges "hear" is the song, and a redo without the speech
                # filter writes its lyrics into the creator's transcript. A
                # person decides; --redo applies it.
                if not args.redo:
                    continue
                lines, language = transcribe_video(model, video, vad=False)
                text = reflow(lines) if lines else NO_SPEECH
                after = check_edges(model, video, text)
                record(folder, piece.id, text,
                       f"whisper {args.model}, language {language}, no speech filter")
                print(f"         -> {'ok now' if not after else 'STILL: ' + '; '.join(after)}")
            print(f"Done: {failed} flagged" + ("" if args.redo else
                  " — listen to them; if one really has missed speech, re-run with --redo"))
            return 0

        todo = []
        for piece in sorted(campaign.ugc, key=lambda p: p.id):
            stored = media.listing(folder, piece.id)
            if not (stored["video"] or stored["images"]):
                continue
            if stored["transcript"] and not args.force:
                continue
            todo.append((piece, stored))
        print(f"{len(todo)} to transcribe")

        model = None
        hooks = 0
        for piece, stored in todo:
            path = media.piece_dir(folder, piece.id)
            if stored["video"]:
                if model is None:
                    from faster_whisper import WhisperModel
                    model = WhisperModel(args.model, device="cpu", compute_type="int8")
                lines, language = transcribe_video(
                    model, os.path.join(path, stored["video"]))
                text = reflow(lines) if lines else NO_SPEECH
                how = f"whisper {args.model}, language {language}"
                spoken = lines
                if len(words(" ".join(lines))) < SPARSE:
                    shown = screen_text(os.path.join(path, stored["video"]))
                    text = with_screen_text(text if lines else None, shown)
                    how += " + on-screen text (macOS Vision)"
                    # Hooks come from speech only; OCR'd captions read too poorly.
                record(folder, piece.id, text, how)
            else:
                slides = read_slides(
                    [os.path.join(path, n) for n in stored["images"]])
                blocks = [f"Slide {i}:\n{t}" for i, t in enumerate(slides, 1) if t]
                text = "\n\n".join(blocks) if blocks else NO_TEXT
                record(folder, piece.id, text, "macOS Vision text recognition")
                spoken = [t.replace("\n", " ") for t in slides[:1] if t]

            if spoken and not piece.hook and not args.no_hooks:
                piece.hook = first_line(spoken)
                hooks += 1
            word_count = len(text.split()) if spoken else 0
            print(f"    {piece.id:>4}  {word_count:>4} words  {piece.byline}")
            db.session.commit()

        print(f"Done: {len(todo)} transcripts written, {hooks} hooks filled")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
