# Adding content by email

Erin emails a post link to her own address with a `+creatorwise` tag, with a
line saying whose it is and which campaign it's for:

> Bea's second piece for the Announcement
> https://www.instagram.com/reel/…

A scheduled Claude task on her Mac (`creatorwise-content`, in the desktop
app's Scheduled list) picks the mail up, adds the piece, saves its media,
transcribes it, titles it and pushes the database to the live site.

**It replies only when Erin has to act** — something held, a reel waiting to
be recorded, an attachment too big, a post it couldn't open, a failed push —
in the thread of her own email, to her plain address (never the tagged one,
never anyone else). Clean runs stay silent. Every reply starts with the line
`[creatorwise job]`, and the tool skips any message carrying it, and any
message in the thread not sent to the tagged address: the job reads whole
threads, so without that its own reply would come back as a new request
from Erin and be answered in a loop. A correction is a new email (or a
reply addressed to the tagged address). The run's full summary is in the
task's history; `data/content-requests.log` has a line per run.

## Why it runs on the Mac, not the server

- The local database is the source of truth. Anything written on the server
  is replaced by the next `tools/push_data.py`.
- Transcription (Whisper, macOS Vision) only runs on the Mac.
- The Gmail connector and the browser pane belong to the desktop app. The
  in-app Gmail scanner (`scan.py`) is a separate, older path and stays
  dormant.

The desktop app has to be open for a run to happen; a missed run happens on
next launch.

## What decides who and which campaign

`tools/content_requests.py`, the same rules as the importers:

- **Only mail from Erin's own address is acted on.** Anything else sent to
  the tagged address is ignored and logged.
- **The campaign must be named** ("Announcement", "softlaunch", "organic",
  "stickers"). Not named, or two named: held.
- **Who made it, in order:** the account on the post (the task reads the
  author's username off each page); then an email address written in the
  email; then — only when the post's author couldn't be read at all — a name
  in its words, if it fits exactly one creator.
- **An author nobody holds is a new creator, never a first-name match.** On
  2026-11-19 an organic plug from a new "Liz" nearly matched the Liz already
  on file by first name; that path is gone. The new creator is made from the
  address in the email, the post's accounts (every platform in the email),
  and the name the task read off the page — "Name (Channel)" when the page
  shows a channel name. Held instead: no address in the email, two
  addresses, no readable name, or a creator with the same full name already
  on file (CLAUDE.md: a new address is usually a second address).
- **How they joined:** the task looks the address up in Readwise
  (`bookwise_bookwiseinviteredemption.redeemer_id`). Another creator's
  invite code makes them a `referral` with `recruited_by` set — a fact,
  so it wins over the email's words. Otherwise "referred" in the email is a
  referral (unlinked), "organic"/"unprompted" or an Organic Plugs post is
  `organic`, and "recruited"/"reached out" is `outbound`.
- **Profile photo:** their Readwise Books photo when they have one
  (`tools/fetch_avatars.py`), otherwise a screenshot of their Instagram or
  TikTok profile picture in the pane (`capture.py avatar`).
- An address on file with an account that isn't adds that account to them.
- A shared account (the duo) needs a name or address to say which of them.
- A post already on file (any URL form of it) is not added again. A new
  link for a piece that exists on another platform becomes its cross-post.
- One link per platform across several platforms is one cross-posted piece
  — unless their lengths (read off the pages) differ by more than 2s with
  the shorter under 175s: then they're two videos (Liz's "Day 15" TikTok and
  "Day 8" reel). Instagram's 3-minute cut of a longer TikTok stays one
  piece. Two links on the same platform are two pieces.
- Emailing a piece is Erin's word that it was delivered: the creator's
  participation becomes `content_delivered`, or is created as such if they
  weren't in the campaign yet. The summary says what changed.
- `discovered_via` is `email`, and the email's text is kept in the piece's
  notes.

Processed message ids are kept in `data/content-requests.json`, so mail is
never read twice. A held email is not retried; send a corrected one.

## How the media is saved

`tools/capture.py`, in the browser pane. Nothing is downloaded from
Instagram's or TikTok's servers by script, and no page code reads media
bytes; see CLAUDE.md, "Saved media and transcripts".

**Attaching the video is the normal case.** Erin usually emails the
Instagram and TikTok links together with the video file; then the file is
filed as is, the links are recorded as the piece's posts, and nothing is
recorded or downloaded. The rest of the table is for emails with no file.
Every piece is transcribed either way.

| post | how |
|---|---|
| file attached to the email | used as is (Gmail connector, 12 MB limit) |
| attachment over 12 MB, or none, with a TikTok link | TikTok's own right-click "Download video" |
| TikTok saving off or signed out, or no TikTok link | the Instagram post recorded as it plays (the last resort); the TikTok page only if there's no Instagram post |
| none of those possible | left on `pending`, Erin told what's needed |
| Instagram carousel | a screenshot per slide, stepped with the post's own arrow |
| TikTok photo post | the same, signed in |
| YouTube | no video; the transcript from YouTube's "Show transcript" panel |

**Recording a reel** (`record-start` / `record-finish`): the pane is set to a
1000×1000 desktop view and the reel opened at `/reels/<code>/`, where it is
shown whole. Instagram's own speaker icon turns the sound on, playback is
started from zero, and `tools/screen_record.swift` (ScreenCaptureKit)
records Claude's window and Claude's audio only — other apps are excluded
from the capture. The video is then found in the recording by motion (it is
the only part that changes), cut out and trimmed to the playback. Result on
the first test (piece 92): same framing and length as the original file to
0.03s, 680×1204 against 1080×1920, with Instagram's overlay (name, caption,
speaker) along the bottom, and a transcript matching the original's.
Needs Screen Recording permission for Claude. A tap on the video mutes it,
so a silent take is flagged and redone.

Things the first real run (2026-11-19) taught:

- **The pane has to be on screen.** A hidden pane isn't drawn, so a run
  that happens while Erin is in another conversation can't record. Those
  pieces stay on `content_requests.py pending`; opening the task's
  conversation and saying "record" finishes them.
- **The rest of the window moves too.** The conversation scrolling beside
  the pane is motion as well, and the first crop picked it. The crop now
  takes the moving region with the reel's own shape (`--aspect`, the
  video's width/height from the page) and trims it to that shape.
- **Instagram keeps its own volume.** Setting `video.volume` from the page
  is overridden; its speaker icon and slider are what work.
- **The browser tool gives up after 45 seconds**, so playback is started in
  one call and the recorder waits for the end on its own.

Then `tools/import_media.py` files what arrived (its duplicate guards apply),
`tools/transcribe_media.py` transcribes new pieces and fills hooks, and
`content_requests.py finish` sets a title: the first sentence of 6–18 words
that isn't a greeting, quoted. Machine-picked titles can be off; the run
summary lists each one so they can be fixed by hand.

## Dating the posts

A piece's date is its earliest post's, and the creator's first-post date
drives their payment due date, their referral-bonus window and their CAC.
A TikTok link with a post id dates itself (the top 32 bits of the id are
the Unix second). Instagram links, TikTok short links (`/t/…`) and YouTube
links carry no date, so after capture the job runs

    content_requests.py undated                      # emailed posts with no date
    content_requests.py date --post N --when VALUE   # store one

reading `VALUE` off the post page in the pane, muted: Instagram's
`<time datetime>`, the URL a TikTok short link lands on, or YouTube's
`datePublished`. Dates are UTC. Anything that isn't a date is refused
("NOT DATED") and left for the report.
