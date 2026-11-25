# creatorwise

_(Project purpose: fill this in — it's the most useful line in this file.)_

## Stack

- **Flask** + **Flask-SQLAlchemy**, served by **gunicorn** on port 5000
- **SQLite**, stored in `data/` (mounted by contextone as a persistent volume)
- Deployed on **contextone** via `Dockerfile`, defined in `.contextone/infra/app.yml`

## Layout

| path | purpose |
|---|---|
| `app.py` | Flask app, routes, DB wiring |
| `importer.py` | CSV -> creators. Alias-matched columns, dry run by default |
| `content_importer.py` | CSV -> one campaign's participants + their posts |
| `tools/reset_data.py` | clear sample data before a real import |
| `inbox.py` | email -> reviewable InboxItem (link extraction, creator matching) |
| `gmail.py` | Gmail API client, stdlib only — reads erin@readwise.io |
| `scan.py` | one scan + the in-process scheduler |
| `tests/` | run each with the venv python; no network, no credentials |
| `tools/gmail_auth.py` | one-off: mint the Gmail refresh token (run it yourself) |
| `docs/gmail-setup.md` | the Google Cloud steps only Erin can do |
| `models.py` | SQLAlchemy models (`db` lives here) |
| `tools/backfill_emails.py` | one-off: mirror primaries into `creator_emails` |
| `tools/refresh_referrals.py` | load per-creator referral counts for a campaign |
| `tools/refresh_all_referrals.py` | every campaign at once: write the query, load its checked result (the daily job) |
| `avatars.py` | profile photos on disk under `data/avatars/` |
| `tools/fetch_avatars.py` | download profile photos from Readwise Books profiles |
| `media.py` | saved videos / carousel slides / transcripts under `data/media/` |
| `tools/import_media.py` | drop folders per post, then file what a person saved |
| `tools/transcribe_media.py` | local Whisper transcripts; Vision OCR for slides |
| `datasync.py` | token-guarded upload endpoints for `data/` (see below) |
| `payments.py` | creators' payment details: form checks, and merging live-site edits back |
| `tools/import_invoices.py` | Erin runs it: payment details from creators' PDF invoices (masked preview, `--apply`) |
| `tools/pdf_text.swift` | PDF text via PDFKit, Vision for scanned pages; built by import_invoices.py |
| `tools/outreach.py` | who Erin emailed about a campaign, who opted in (daily job) |
| `tools/payments_due.py` | who's due, referral bonus, invoices, Superhuman draft data (daily job) |
| `tools/invoice_pdf.swift` | renders one invoice PDF from a JSON spec on stdin |
| `tools/push_data.py` | send the local DB, media and photos to the server |
| `tools/refresh_followers.py` | worklist of handles; load follower readings with history |
| `tools/fetch_youtube_followers.py` | subscriber counts from the YouTube Data API |
| `tools/content_requests.py` | content Erin emails in -> pieces (see below) |
| `tools/capture.py` | get a post's media into its drop folder (attachment, TikTok's Download, recording, slides) |
| `tools/screen_record.swift` | records the Claude window + its audio (ScreenCaptureKit); built by capture.py |
| `docs/content-by-email.md` | how emailed content is matched, captured and filed |
| `templates/` | Jinja templates |
| `static/` | CSS and assets |
| `data/` | SQLite DB — gitignored, persistent in production |
| `.contextone/infra/app.yml` | contextone service definition |

## Conventions

- Keep `db` defined in `models.py` and import it into `app.py` — not the reverse,
  which creates a circular import.
- `DATABASE_URL` is unset locally (falls back to `data/creatorwise.db`) and set to
  `sqlite:////app/data/creatorwise.db` in production. Don't hardcode either path.
- Secrets never go in the repo. Add them as contextone env vars and reference them
  as `${ENV.KEY}` in the infra file.
- Validate infra changes with
  `contextone infra validate --file .contextone/infra/app.yml` before deploying.
- `expose`, `env`, and `source_mounts` are siblings of `app:` in the infra YAML, not
  nested inside it.

## Deploy gotchas

These have bitten this setup before:

1. **Contextone clones from GitHub, not from your machine.** Always `git push` first,
   then sync the remote workspace to your commit — otherwise you deploy stale code
   and the change silently doesn't appear:
   `contextone repo sync --project creatorwise --ref main --commit $(git rev-parse HEAD)`
2. **This repo must stay PUBLIC to be deployable.** Contextone's workspace git
   fetch is unauthenticated. While the repo was private, `repo sync`, `repo reset`
   and `deploy` all failed with `could not read Username`, *even with* the
   contextone GitHub App granted "All repositories" access. All three began
   working the moment the repo was made public. Granting App access is NOT the
   fix, despite what the dashboard implies. (Verified 2026-11-05.)
   Note: after flipping visibility, the first sync can still fail — GitHub takes
   a few seconds to propagate. Retry once before assuming it's broken.
3. **Docker layer caching** can serve old code even after a successful deploy. Bump
   `ARG BUILD_ID` in the `Dockerfile` to force a rebuild if that happens.
4. **Never destroy a deployed instance** to fix it — the SQLite volume and SSL certs
   are lost, and repeated recreates hit Let's Encrypt rate limits. Redeploy in place.

5. **TLS: fixed on 2026-11-18, after a platform outage.** From Sep 2026 the
   platform issued no certificates, and every creatorwise address served an
   unrelated app's (`CN=cx-emails-agent.app.contextone.dev`), so no browser
   could open the dashboard. `http://` was never a workaround: all of `.dev` is
   HSTS-preloaded, so browsers upgrade it to https before sending anything.

   The cause, found 2026-11-17: `dokku letsencrypt:enable` solves DNS-01
   through Cloudflare, and the platform's Cloudflare token had lost access to
   the contextone.dev zone (contextone ticket #26). `contextone infra deploy`
   hides that step's failure; `contextone compute up` (the deployment preset,
   `.contextone/creatorwise.yml`, compute id 131) is what reported it.

   Issuance resumed on 2026-11-18, and an in-place redeploy of instance 454
   gave `creatorwise.infra.contextone.dev` its own certificate within a
   minute. http now redirects to https. If a certificate is ever wrong again,
   check whether the platform is issuing at all (crt.sh,
   `%.infra.contextone.dev`) before redeploying, and try `compute up` to see
   the error.

   Do NOT `destroy` the instance to fix a certificate — it loses the volume and
   risks rate-limiting the subdomain for days.

   For local work, run the app on localhost — no TLS involved:
   ```
   ./venv/bin/python app.py    # http://localhost:5000
   ```
   On macOS, port 5000 may be taken by the AirPlay Receiver (it answers 403);
   the browser-pane preview runs the app on 5001 instead.

## Sample data

Cleared locally on 2026-11-09, ahead of the real import. `SEED_SAMPLE_DATA` is
now `0` in `.env` and in `app.yml` — leave it off. `seed.py` fills an **empty**
database on boot, so turning it back on means a fresh volume comes up holding 14
invented creators instead of nothing, and once those mix with real ones there's
no telling them apart.

The deployed instance still holds its own copy of the sample data; it hasn't
been touched.

## Importing creators

`importer.py` is `plan()` then `apply()` — never one without the other. The CLI
dry-runs unless given `--apply`.

- Columns are matched on **aliases** (`ALIASES` at the top of the file), not an
  exact header. A new export with an unknown column name is a one-line addition
  there, not a reshape of their spreadsheet.
- Matching is on **lowercased email**, so re-running an updated export is the
  normal workflow.
- **A blank cell never overwrites existing data.** Partial exports are normal.
- Rows that can't be resolved are **blocked and reported with a line number**,
  never guessed at — no email, no name, duplicate address in the same file.
- Unrecognized columns are **reported**, not silently dropped.
- The whole file commits in one transaction.
- `referred_by` resolves to a real `recruited_by_id` link (name, then handle,
  then email local part), in a second pass after every creator exists. Ambiguous
  values stay in `recruited_by_note` — never guessed. Don't "improve" this by
  taking the first match: four creators share the first name Sam.
- `recruited_by` / `recruits` is the recruitment graph (people). `total_referrals`
  is signups driven by content. Different things, named apart on purpose.
- A creator can have more than one address, and an address can have more than
  one creator. `Creator.email` is still the unique primary and still what the
  app keys on; `creator_emails` holds every address, primary included, unique
  per (creator, address) but **not** unique on address alone — a duo running
  one channel really do share a mailbox, and forcing them into one record lost
  a real person. A shared address is therefore not a key: matching treats it
  as a weak signal for each holder, `record_email` refuses one by default
  (`allow_shared=True` to override), and `tools/refresh_referrals.py` credits
  nobody for it rather than guessing whose link earned the signups. Both
  importers record what they see, `inbox.py` matches senders on secondaries
  too, and `tools/backfill_emails.py` mirrors primaries in (idempotent).
  An address already held by a *different* creator is never reassigned — that
  is a conflict to report. Adding a third address is a row, not a migration.

## Real data, and why none of it is in this repo

**This repository is public** (it has to be — see deploy gotcha #2), so no real
creator data goes in it. `data/` is gitignored, exports stay outside the repo,
and tests use invented names with `example.com` addresses.

That applies to fixtures, docstrings, comments, docs and commit messages alike.
Real addresses were committed here once, picked up from live mail while building
the inbox scanner, and had to be scrubbed on 2026-11-09 — they remain in git
history.

Currently loaded (locally only): 280 creators from the recruitment export,
501 handles (234 TikTok, 198 Instagram, 48 YouTube, 17 Threads, 4 X), 209
referral links, and one campaign — Bookwise Stickers Beta, from the CRM export
on 2026-11-09: 24 participants, 20 delivered, 21 pieces (19 TikTok, 2
Instagram), 18 of them dated. Now 23, 19 and 20: on 2026-11-13 Erin had
creator 106 removed from it entirely — piece 005, participation and
referral row — because she took her video down (creator record kept). One of those pieces is dated 2026-02-23, well
after the rest of the cohort (2025-10-13 to 2025-12-08) — the CRM lists that
post against a participant, so it was kept on the CRM's word, not because the
date fits. Don't "fix" the range by dropping it. The other 257 creators have no campaign: the
recruitment export has no campaign column and its onboarding dates span Jul
2025 to Jul 2026, so assigning them would be fabrication.

Bookwise Softlaunch, from the opt-ins sheet on 2026-11-09: 154 participants —
8 contacted, 69 opted in, 22 wave 2, 11 briefed, 42 delivered, 2 declined —
with 49 pieces from 76 links (26 of them cross-posted). Referrals are the
live figures, not the sheet's: 581 across 52 creators (the duo's personal
addresses added 4), counted over the campaign's own dates, Jul 1 – Oct 1 2026,
from invite-code redemptions. Widening from Jul 15 to Jul 1 added nothing.

Readwise Books Announcement, from the October outreach sheet on 2026-11-09:
202 participants — 5 not contacted, 57 contacted, 94 opted in, 42 delivered,
4 declined — with 57 pieces from 79 links. Referrals were 1,700 across 81
creators counted Oct 28 – Nov 9 2026, then 1,758 when refreshed through
Nov 10, then 2,188 across 89 through Nov 16, then 2,214 through Nov 17 (the campaign runs Oct 28 – Dec 1 2026; the
daily refresh counts it to date and stops at Dec 1 on its own). **41 of those creators earned referrals
without posting any content** (643 between them), which is why referrals are
not stored per piece.

Those 643 are real but not content-attributable. Invite codes are permanent
and personal (one per profile, created Jun–Sep 2026, some capped at 5
redemptions), not campaign-scoped — so a creator can drive signups by sharing
their link any way they like. The window is what ties them to the campaign,
and the jump is unambiguous: for the top 18 creators, redemptions ran 6 in
Oct 2–14 and 7 in Oct 15–27, then 1,027 in Oct 28 – Nov 9. Attribution here
is reach-based, not content-based; per-post attribution would need tracked
links that don't exist. 20 of the referrals come from creators who never
replied and 7 from one who declined — they simply already had codes.

**"Wave 1" in that sheet means the softlaunch** — in the `Group` column and
in the Notes alike (confirmed with Erin, 2026-11-09). All 38 rows noting
"Posted content in Wave 1" are softlaunch deliverers (38/38), and their
content is already loaded under `bookwise-softlaunch`. Likewise "Briefed, no
content delivered in time for Wave 1" is history, not this campaign's state —
don't remap either into announcement statuses.

Its sheet's Status column tops out at "Opted In" — there is no "Posted" value
— so a post link promotes `opted_in` to `content_delivered` (`PROMOTABLE` in
content_importer.py). Anything more specific than `opted_in` stands, and a
contradiction is reported instead.

**The Stickers Beta's zero referrals is a measurement, not a gap.** It ran
Oct 1 2025 – Mar 31 2026 (dates from Erin, 2026-11-10), and counted over that
window every participant has 0 redemptions — because no participant had an
invite code yet: the earliest was created 2026-06-05. The 24 (now 23) zero rows are
stored with that window. Don't go looking for more.

Campaign dates (from Erin, 2026-11-10): Stickers Beta Oct 1 2025 – Mar 31
2026, Softlaunch Jul 1 – Oct 1 2026, Announcement Oct 28 – Dec 1 2026
(`active` until then). Referral windows match them. Only participants are
credited: in the softlaunch window, 35 redemptions came from people who were
in the announcement but not the softlaunch, and they are skipped, not counted.

**Organic Plugs** (`organic-plugs`, created 2026-11-16) is for creators who
post about the app unprompted. It runs from Oct 28 2026 with no end date
(Erin, 2026-11-16) and reads "ongoing"; with no end, its referral total
always says "(so far)". Nobody is contacted or asked to opt in, so its cards and page show no contacted / opted-in figures (`ORGANIC_CAMPAIGNS` in models.py, `Campaign.is_organic`; Erin, 2026-11-24). Referrals through Nov 17: 211, nearly all from
one creator (307). Its first
creator (306) was found by her Readwise Books username, at Erin's request:
Erin's contextone query to the Readwise production database
(`bookwise_bookwiseprofile.username` → `main_profile` → `auth_user.email`)
gave the address, which Erin confirmed. The username is kept in the creator's
notes. That was a one-off lookup by Erin's tooling — creatorwise itself still has no
production access. Her two TikToks (194, 195) came via TikTok's own
Download; Whisper wrote the background song into 194's transcript and those
lines were removed. On 2026-11-16 every campaign was re-counted in one
query over all four windows (Erin's contextone access, limited to addresses on
file); the Stickers Beta stayed 0 and the softlaunch stayed 581 exactly.
Creator 307's video (196) came as a file from Erin
("volunteer UGC"), with her Whisper transcript; so did creator 308's
reel (197). 308's surname spelling matches his email address (confirmed
by Erin, 2026-11-16). Creator 309's reel (198) came the same way, and so did creator 310's (199).

Engagement numbers exist for the Announcement only (see "Engagement" below).

One opt-ins row is knowingly not imported: it carries one creator's address
under a different creator's name. Confirmed with Erin on 2026-11-09 — the two
are separate people, the address belongs to the first, and the row is a stale
duplicate of a row already imported for the second under her own address. It
stays held; the fix belongs in the sheet, not here. The report prints the
near-miss that gives this away, so don't suppress warnings on held rows.

One address on two names has turned out to mean both things, so neither is
the default reading:

- **Two people, one mailbox, one channel.** A duo who run a single channel
  (creators 77 and 78; set up as Erin specified on 2026-11-10). Each has his
  own name and his own primary address; both carry the shared mailbox as a
  secondary and both carry the channel's TikTok and Instagram handles and its
  profile photo. There is no separate joint profile — record 77 used to be
  one and is now one of the two people. The channel's softlaunch piece has 77
  as its lead and 78 as a collaborator, and both are `content_delivered`.
  Consequences that are correct and should be left alone: their shared
  address and their shared handles both match two creators, so any sheet row
  that identifies them only by those is reported as ambiguous or conflicting
  rather than assigned, and `refresh_referrals` credits neither for the
  shared address.
- **One person, two spellings.** Two announcement rows for one creator: the
  importer has only ever created one record for him, so there was never a
  duplicate to merge; the second row is held because the first already
  claimed him.

No real names, handles, channel names or address fragments in this file —
it is committed to a public repo. Refer to creators by id.

The creator who appears in the recruitment export and the opt-ins sheet under
different addresses now holds both, via `creator_emails`. Importing "creators
whose email is new" had created a duplicate of her first; that was deleted.
**Check names as well as addresses before creating anyone from a new sheet** —
a new address is usually a second address, not a new person.

Also: do not run `importer.py` over the opt-ins sheet. Its `Creator` column
holds short forms ("Aly", "Georgia"), and a full-file run renames 92 creators
from their recruitment-export names down to those.

The three sample campaigns (`fall-2025-sticker-beta`, `summer-2026-softlaunch`,
`books-announcement`) were seed-data leftovers — `tools/reset_data.py` keeps
campaigns unless given `--campaigns` — and were deleted on 2026-11-09, empty.
Bookwise Stickers Beta is now the only campaign. Note that `seed.py` would
recreate all three if `SEED_SAMPLE_DATA` were ever turned back on against an
empty database; it is `0` and should stay there.

## Importing a campaign's content

`content_importer.py` is the second pass: Participation rows plus
UgcPiece/PlatformPost rows for one campaign. Same `plan()` / `apply()` shape as
`importer.py`, same dry-run-unless-`--apply` CLI. Full guide:
`docs/importing-content.md`.

- It **never creates a creator**. Rows it can't tie to one are held for review.
- Matching is a ladder of four signals (email, profile handle, post handle,
  name), each strong or weak. One uncontradicted strong signal resolves a row;
  two agreeing weak ones do not. Don't "improve" this by accepting weak
  agreement: the first real export contains a row where two weak signals agree
  and are probably wrong.
- **Email outranks everything**, including a name pointing elsewhere —
  `Creator.email` is unique, names aren't.
- Participation status is a funnel, not a boolean: `not_contacted`,
  `contacted`, `opted_in`,
  `wave_2`, `briefed`, `content_delivered`, `declined`, `no_show`. The outreach
  sheet's `Status` and `Response` columns are read together, `Status` winning
  where it's definite. **`delivery_rate` is measured against `commitment_count`,
  not every participant** — somebody never briefed hasn't failed to deliver,
  and the softlaunch has 97 of those.
- The headline campaign metrics are cumulative, so they narrow:
  `contacted_count` (everyone we actually approached — excludes
  `not_contacted`), `commitment_count`, `delivered_count`.
- **The middle metric is the strongest commitment a campaign can evidence.**
  `commitment_count` is `briefed_count` where the source tracked briefing
  (`records_briefing`: at least one explicit `briefed` row) and
  `opted_in_count` otherwise, with `commitment_label` naming which. Opting in
  is cheap — 143 said yes to the softlaunch and 52 were briefed. Without this
  fallback, `briefed_count` collapses to `delivered_count` on a campaign with
  no briefing stage and reports a meaningless 100% follow-through. Only the
  softlaunch has one: **the announcement ran no briefing calls at all**, so
  its `records_briefing` being False is correct, not a gap in the data. The
  UI says nothing about why — it just shows opt-ins. **`contacted` as a status is the opposite of
  `contacted` as a metric** — the status means reached out to and never heard
  from, the metric means everybody. `status_counts` is the disjoint view.
- Referral counts from a sheet are stored with `window_days = REPORTED_WINDOW`
  (0, "as reported"), never as a 14-day figure we didn't measure. These are a
  fallback: `tools/refresh_referrals.py` replaces them with real per-creator
  totals, and clears them so one question doesn't have two answers.
- Links on a row merge into one cross-posted piece **only** when there's
  exactly one per platform and more than one platform. When saved media later
  proves a pairing the sheet couldn't (same length, same transcript), merge
  the pieces by hand: move the posts, delete the empty piece. Move them with
  `keep.posts.append(post)` (or remove them from the old piece's collection
  first) — changing `post.ugc_piece_id` alone leaves them in the old piece's
  `posts`, and its `delete-orphan` cascade deletes them with it. That lost two
  TikTok links once; they were restored from the backup. Four reels on a row are
  four videos; two TikToks beside two Reels is an unknowable pairing, so it
  stays four pieces.
- A handle match is **strong only on the platform the link claims**. Somebody's
  Instagram name appearing in a tiktok.com URL is not evidence.
- `posted_on` is read out of TikTok post ids (top 32 bits are the Unix
  second). Instagram and YouTube ids carry no date and TikTok `/t/` shortlinks
  carry no id, so those pieces have no date. Don't backfill them with a guess.
- **No engagement numbers are written.** A CRM export has none, and a zero that
  looks like a measurement is worse than a blank. Views and likes have to come
  from the platforms.
- Two post links on one row become two pieces, never one cross-posted piece.
  Whether they're the same video isn't in the export.
- The dry run's counts must equal what `apply()` writes. This has gone wrong
  twice: rows held for review carry links that aren't imported, and a referral
  count on a row with no content has nowhere to land. Both are now asserted in
  the tests; add the same assertion to anything new that gets counted.

## Shared credit on a piece

`UgcPiece.creator_id` is the lead; `PieceCollaborator` adds anyone else who
made it. Use `piece.creators` / `piece.byline` for display and
`creator.all_pieces` for "what did this person make" — `creator.ugc` is only
the pieces they lead. **Never duplicate a piece to credit a second person**:
campaign totals sum `campaign.ugc`, so a copy double-counts the video and,
later, its views and likes. The softlaunch has one such piece, which is why it
reads 154 participants and 42 delivered on 46 pieces.

## Referrals

**Referrals belong to a creator in a campaign, not to a video.** The source
counts invite-code redemptions per account, and a creator who posted three
videos — or none — still has one number. `CampaignReferral` holds it, with the
window it was counted over; `UgcPiece.referral_snapshots` remains for genuine
per-piece measurement if attribution ever gets that precise.

- `Campaign.total_referrals` and `Creator.total_referrals` **prefer**
  `CampaignReferral` and fall back to the per-piece snapshots. Never sum both:
  they measure the same signups twice.
- A number without a window is meaningless, so `window_from` / `window_to` are
  stored on every row and shown on the campaign page. Rows with disagreeing
  windows make `referral_window` return None rather than pick one.
- The source of truth is the Readwise production DB:
  `bookwise_bookwiseinvitecode` (a code per `main_profile`) joined to
  `bookwise_bookwiseinviteredemption`. Join to creators through
  `auth_user.email` → every address in `creator_emails`.
- **creatorwise has no production DB access and should not get any.** The
  counts arrive as a JSON file `{email: count}` produced by somebody who can
  query it; `tools/refresh_referrals.py` has the exact SQL in its docstring.
- **Referrals refresh daily, outside the app** (Erin asked, 2026-11-17). A
  Claude desktop scheduled task on Erin's Mac, `creatorwise-referrals`
  (07:00, prompt in `~/.claude/scheduled-tasks/creatorwise-referrals/`), runs
  `tools/refresh_all_referrals.py --sql`, runs that query with Erin's
  contextone `query_readwise`, saves the result and loads it with `--load
  --apply`. The app itself still has no production access — that is why this
  isn't a thread in the web process like the mail scanner. It only runs while
  the desktop app is open (a missed run happens on next launch). It
  refreshes the **local** database, which is where the real data lives,
  then uploads it with `tools/push_data.py` (since 2026-11-19).
- Every campaign is counted over its own window: `starts_on` to `ends_on`,
  or to today while running; no end date means always to today. A campaign
  with no `starts_on` is skipped and reported.
- The result is copied out of the query tool by a model, so it carries a
  checksum: the query adds a ROLLUP total row, and `--load` refuses a file
  whose rows don't sum to it, whose row count is off, or that names an
  address the query didn't ask about. A refused file is fetched again, never
  patched by hand. Columns are named `c<id>_<from>_<to>`, so a file states
  its own windows.
- `--apply` overwrites `data/creatorwise.db.bak-referrals` first and appends
  a line of per-campaign totals to `data/referral-refreshes.log` — check that
  log, not the scheduler's status, to see whether runs are happening.
- A creator missing from a later file drops to zero rather than keeping a
  stale number that looks current.
- **A video shows its creator's figure for the campaign, labelled as such**
  (`UgcPiece.creator_referrals`, "creator ref" on cards). Every video by one
  creator in a campaign shows the same number; a shared video shows its
  makers' combined figure. Sorting and the overview's top videos use it, with
  one video per creator per campaign. Never divide it among videos — no
  source says which video drove which signup.
- A running campaign's total says "(so far)" when the counted window ends
  before `ends_on` (`Campaign.referrals_still_counting`).
- **The campaign page is one card per creator** (Erin's call, 2026-11-13):
  thumbnail from their newest piece with saved media, their name, and
  `Campaign.referrals_for(creator)` — their own figure, not a combined one.
  The card opens `/campaigns/<slug>/creators/<id>` with everything they
  posted for that campaign. `Campaign.creator_groups` puts a shared piece
  under each maker, so cards can outnumber pieces' leads; campaign totals
  still count every piece once, from `ugc`. Card count equals
  `delivered_count` on all three campaigns.

Verified on 2026-11-09 against the softlaunch: over Jul 15 – Oct 1 2026,
35 of the sheet's 41 numbers matched the live count exactly, which is what
established that the sheet was campaign-windowed and that this window is the
right one. The sheet missed 13 creators who had earned referrals (58 in all,
one of them 25 with no content at all), and was ~6 weeks stale — its totals
matched the live cumulative figure around late September.

## Engagement

`PlatformPost` holds a post's latest views, likes, comments, shares and
saves; `PostSnapshot` keeps every reading behind it. Unmeasured is None,
shown as a dash, never 0, and totals sum only measured posts. A reading
with no numbers is a snapshot with a `status` — `likes_hidden` (the
creator hides the count; the page says so) or `unavailable` (removed,
archived or private) — and leaves the post unmeasured.

- Read in the browser pane, one post at a time, and loaded with
  `tools/refresh_engagement.py` (`--list`, then `--load FILE --source`,
  dry run unless `--apply`). The same no-fetching rule as follower counts.
- TikTok videos: `statsV2` in the page's rehydration data (exact counts,
  views included). TikTok photo posts have no views and no rehydration
  item; their counts are the first `like-count` / `comment-count` /
  `share-count` / `favorite-count` on the page. Instagram: the
  `og:description` "N likes, M comments" — no views on the web, and
  nothing at all when likes are hidden. YouTube: the description's
  factoids (Shorts round views above 10K).
- **Mute every page as it loads** (`video,audio` muted and paused, kept so
  on an interval) — the pane plays sound on Erin's Mac.
- First read 2026-11-20, Announcement: 148 of 168 posts with numbers,
  19 Instagram posts with likes hidden, one reel taken down (piece 139).

## Follower counts

`SocialHandle.follower_count` is the latest good value; `FollowerSnapshot`
keeps every reading behind it — count, date, source, and `status="not_found"`
for an account that could not be found. The creators page shows one column per
measured platform, and **three states that must not look alike**: blank (not
on the platform), a dash (on it, never checked), a number (checked — zero
included). `Creator.followers_on()` returns None, not 0, for unmeasured.

- `tools/refresh_followers.py --list` is the worklist; `--counts FILE
  --source "..."` loads readings. `--source` is mandatory. One account on two
  creators' profiles gets the same reading on both — one audience, not an
  ambiguity.
- **creatorwise does not fetch Instagram or TikTok profiles, and must not be
  made to by disguising requests.** Both gate profile pages against automated
  readers. A probe that sent a browser User-Agent and a link-preview bot's
  User-Agent to get past that was refused on 2026-11-10 as bot-check
  circumvention, correctly. Readings for those platforms come from an official
  API, a data vendor, or a person — and are loaded with the tool above.
- YouTube is the exception because it has a real public API:
  `tools/fetch_youtube_followers.py` needs `YOUTUBE_API_KEY` in the
  environment (never in the repo) and writes readings for the loader. A hidden
  subscriber count is skipped, never stored as zero.
- As of 2026-11-10: Instagram 188 of 201 confirmed, TikTok 208 of 235, YouTube
  0 of 48 (waiting on an API key). Read one profile at a time in the browser
  pane, ~30s apart, Instagram signed in, TikTok logged out. TikTok's exact
  count is `statsV2.followerCount`; `stats.followerCount` rounds above 10K.
- **An account with no posts and no followers is not a reading of zero.** Two
  Instagram handles on file (and one earlier, since corrected) turned out to be
  empty placeholder accounts, not the creator's real one. They are left
  unmeasured rather than shown as 0.
- TikTok's **audience controls** hide counts from logged-out visitors (seven
  creators so far). Those, and handles TikTok calls "couldn't find" while
  logged out, need a signed-in read before anything is recorded.

## Profile photos

One file per creator at `data/avatars/<creator id>.<ext>` — no column, no
table. Whether a creator has a photo is whether the file exists, so this needed
no migration. They sit under `data/` because it is gitignored (**a creator's
face must never be committed to this public repo**) and because it is the
persistent volume. Served by `/creators/<id>/avatar`, behind the session gate.

- **The source is the creator's own Readwise Books profile, not their social
  accounts.** `bookwise_bookwiseprofile.profile_image_thumbnail` is a storage
  key under the public `readwise-assets` bucket at `media/<key>`. It is the
  picture they chose for this product, at a stable URL, with no login.
  `tools/fetch_avatars.py` takes `{email: key}` — same hand-off pattern as the
  referral tool, because creatorwise has no production access.
- Use the **thumbnail** key. The full-size `profile_image` originals came to
  106 MB for 262 creators; the thumbnails are 3.8 MB.
- Social profiles are the fallback for creators with no Books photo, and only
  that: Instagram and TikTok image URLs are signed and expire within days, so
  they must be downloaded, never hot-linked. The 32 social photos loaded on
  2026-11-10 were read one profile at a time in the browser pane, but the image
  files were then downloaded with a browser-like User-Agent — the same
  disguise later refused for follower counts. Don't repeat that; have the
  browser itself save the image, or ask the creator for one.
- Files are validated by their leading bytes, not by extension or
  Content-Type — a CDN can answer a missing object with a 200 and an HTML page.
- `{% from "_avatar.html" import avatar with context %}` — **the `with context`
  is load-bearing.** Without it the macro can't see `avatar_ids` and silently
  renders initials for everybody. There is a test for exactly this.
- This venv's Python has no CA bundle configured; the fetch tool loads the
  system one. The fix for a failed certificate check is to find the bundle,
  never to turn verification off.

Loaded 2026-11-10: 297 of 302 — 262 from Books profiles, 9 YouTube, 14 TikTok,
12 Instagram. The five without have no working social handle on file.

**"Couldn't find this account" on TikTok is not proof a handle is dead.** That
is what a logged-out visitor sees, and at least one creator's own link page
points at a TikTok handle that shows exactly that message. Treat it as "not
visible without a login", report it, and don't delete the handle on that
evidence alone. A handle is confirmed stale only when the creator has visibly
moved — a renamed account, or Erin saying so.

## Saved media and transcripts

A piece's video or carousel slides live in `data/media/<piece id>/`
(`video.mp4`, `poster.jpg`, `image-01.jpg`…, `transcript.txt`, `source.json`).
No table, same as avatars: having media is having files. Gitignored, on the
persistent volume, served by `/ugc/<id>/media/<name>` behind the session gate,
and only names the piece actually has are served.

- **creatorwise does not download from Instagram or TikTok.** Logged out,
  TikTok walls the page and streams video as a `blob:`; Instagram's website
  has no download at all. Downloader tools (yt-dlp, gallery-dl) work by
  imitating a browser, which this project has ruled out. Erin chose
  (2026-11-10) to save files herself with the platforms' own controls.
  The browser pane is no way round this either. Having it read a carousel's
  image bytes and hand them to a local receiver was refused (2026-11-11) as
  getting around the sites' access controls; a test of the pane saving one
  slide as a download did land in ~/Downloads, late, but is the same act and
  wasn't pursued. Reading a post page to label its folder is fine.
- **TikTok's own "Download video" is the way to get TikTok videos** (at
  Erin's request, 2026-11-11). In the browser pane, logged out, right-click
  the playing video: TikTok's menu offers Download video when the creator
  allows saving, and the file lands in ~/Downloads as `Download.mp4`
  (H.264, 576x1024). TikTok's interests picker has its own close button;
  closing it is fine. **When the creator has turned saving off
  (`author.downloadSetting` 3) the menu has no Download option — that is
  their choice, so leave it**: those need a screen recording by Erin or the
  file from the creator.
- **Carousel slides can be screenshots of the screen** (Erin's call,
  2026-11-11): in the browser pane, signed in to Instagram as Erin, step
  through the slides with the post's own arrow and capture what is displayed,
  two or three shots per slide (the slide is taller than the pane), joined
  with ffmpeg from the clean band between the fixed header and the floating
  share button. Result ~782-800px wide versus 1080 originals, with
  Instagram's arrows and slide dots on them as in any screenshot. This is
  capture of the rendered page, not the image files — keep it that way.
  Logged-out TikTok walls photo posts behind a login modal; don't dismiss
  it to get at them.
- `tools/import_media.py --prepare --campaign SLUG` makes one folder per post
  in `data/media-inbox/SLUG/`, named `<piece id> <creator> - <platform> <kind>`
  with a `.webloc` that opens the post. Drop the file(s) in; run the importer
  (dry run unless `--apply`). Files are recognised by bytes; HEIC becomes JPEG
  and HEVC becomes H.264 so every browser plays them. Slides keep file-name
  order. Two videos in one post's folder is held, not guessed.
- **Match saved files to posts by length, not by name.** The softlaunch batch
  came named by first name; ambiguous ones (four of one creator's reels, two
  creators sharing a first name) were settled by comparing the file's duration with
  the post's. That also showed one creator's two TikToks are the same videos
  as her two reels (172s, 91s): the CRM had listed them as four pieces, and
  they were merged into two cross-posted ones on 2026-11-10. **No video
  appears twice** — the importer holds a file within 0.15s of a video
  already stored for the same creator (0.15s, not 1s: two of one creator's
  different reels are 0.23s apart). Merge the pieces instead.
- `tools/transcribe_media.py` runs Whisper (`small`) locally — no audio leaves
  the machine — and reads carousel text with macOS Vision (`tools/ocr.swift`).
  Extras in `requirements-media.txt`, not installed in production. It decodes
  audio with ffmpeg itself because faster-whisper's PyAV call breaks on
  current PyAV. A video with no speech says so rather than keeping Whisper's
  guesses at music. An empty hook is filled with the first spoken line.
- Transcripts are machine output: book titles and names will be wrong at
  times. The page says so.
- **Song lyrics are not a transcript.** Six softlaunch videos are music over
  on-screen text. Without the speech filter Whisper writes out the song (and
  invents "thanks for watching" over music), so `--verify` only *flags* a
  transcript that misses its edges; `--redo` is a deliberate second step.
  Videos with under 15 spoken words get their on-screen text instead, read
  from a frame every 1.5s. Frames with more than 8 lines of text are skipped:
  that is a filmed book page or app screen, someone else's words. Hooks come
  from speech only — OCR'd captions read too poorly to stand as a hook.
- **Titles are quotes.** A piece's `title` holds a phrase lifted verbatim from
  its transcript, in curly quotes, so a card says what the video is about
  rather than "Instagram + TikTok post". Platforms move to the line beneath
  (`UgcPiece.platform_summary`). Picked by hand on 2026-11-10 and checked to
  occur in the transcript; captions from on-screen text had obvious OCR
  misreads corrected. Pieces with nothing usable keep the platform label.
- Announcement, 2026-11-11: 24 of 57 have video, all via TikTok's own
  Download, transcribed and titled. Six TikToks had no Download option
  (saving off), skipped. The pane sometimes leaves a finished download as a
  hidden `.…claudefordesktop.…` temp file instead of `Download.mp4`; it is
  the complete file once its size stops changing and ffprobe reads it.
  Still missing: Instagram-only reels (12), the YouTube-only video, the
  expired story, and the 3 TikTok-only carousels (103, 116, 122). The 10
  Instagram carousels were loaded from screenshots the same afternoon,
  bringing it to 34 of 57. Video slides inside a carousel (071 has two) are
  paused on their first frame before capture, or the shots of one slide
  catch different frames; Instagram's play button shows on that still.
- Announcement after the re-import, 2026-11-13: 109 pieces, 49 with media.
  15 more TikToks via TikTok's own Download (signed in as Erin: TikTok now
  shows a logged-out visitor a birthday age gate, which is Erin's to answer,
  never ours), plus 072, an Instagram-only reel whose identical TikTok
  (same day, same length, same caption) was found on her profile and added
  as its cross-post. Finding TikTok copies of Instagram-only reels works,
  but TikTok's profile grid starts answering "Something went wrong" after a
  few profiles in quick succession; stop rather than push, since it is
  Erin's account. Saving off: 084, 087, 089, 095, 106, 121, 128, 159.
  On 2026-11-16 153's TikTok (same day, same "what I read this week" +
  leaderboard caption) was added as its cross-post; saving is off on it
  too, and Instagram had signed Erin out, so its length is unchecked and
  153 still has no media.
  Erin's folder of 2026-11-16: 14 Instagram posts with files and her
  transcripts. Four (131, 179, 180, 181) matched videos already stored, to
  0.03s, and were left alone. Three filled pieces that had no media (136,
  152, 162). Seven were new posts, added as 183–189 with
  `discovered_via="manual"`; that made three creators `content_delivered`
  (two had only opted in and one was `contacted`; on Erin's word). Her
  Whisper transcripts were used with the mishearings her notes name
  corrected; lyrics and a lip-synced clip were left out, as before. 186 is
  0.2s off 98 by the same creator but is a different video (different
  speech); the importer now flags 098's folder as a twin of 186 on every
  run (58.2s vs 58.33s once stored) — a false alarm, 098 is just skipped.
  The 4-minute readathon by the creator of 176 arrived with its TikTok
  link afterwards and is 190 (a different video from 176). Announcement:
  117 pieces, 75 delivered, 62 with media. Three more the same day: 191
  (a cross-post; its TikTok is dated four days before the reel, both links
  from Erin; it made that creator `content_delivered`), 192, and 193 —
  a video Erin first sent under 192's creator and then refiled under
  193's. 193's Instagram link came separately (Erin had first sent only
  the creator's profile). Announcement: 120 pieces, 76
  delivered, 65 with media.
  Two duplicates merged the same day (Erin's yes), leaving 107 pieces with
  49 holding media: 174 was 123's TikTok again under its full URL (123 had a
  short link), so 123's link was rewritten to the full form — keep it that
  way, or a re-import recreates the duplicate — and 174 deleted; 170's reel
  was the same video as TikTok 172 (126s, same day), so the reel moved to
  172 and 170 was deleted. Later that day Erin added three Instagram posts
  by hand (180–182, `discovered_via="manual"`), which made their creators
  `content_delivered` — one of them had been `contacted`, overruled on
  Erin's word that he posted: 110 pieces, 72 delivered, 50 with media. 181
  came from its identical TikTok; 180 and 182 (reels over music, no TikTok
  copy) came as files from Erin with her own transcripts of the on-screen
  text, used as given (`source.json` how: "transcribed by hand", which the
  piece page shows instead of the machine-transcript note). 52 with media. Not merged: 088's reel matches saving-off TikTok
  089 (119s), but neither has media yet to prove it.
  146 and 148 are 7s clips over a film quote and a song: like lyrics, that
  audio is not a transcript, so they carry their on-screen text instead.
  169's caption is about voting, not the app; it stays on the sheet's word.
  The pane names every TikTok download `Download.mp4` and renames it from a
  hidden temp file when finished; moving the temp file before that rename
  left 0-byte files and made the pane ask Erin to replace. Wait for the
  rename, and start no download while a `Download*.mp4` is in ~/Downloads.
- Announcement carousels, 2026-11-17 (Erin's go-ahead): 15 Instagram
  photo/carousel posts captured from screenshots, 130 slides — 132, 137,
  140, 143, 151, 154, 155, 156, 158, 161, 163, 165, 166, 177, 178. 80 of
  120 now have media. 141, 142 and 144 looked like posts but are single
  videos, so they still need files. Method, quicker than the old 2–3 shots
  per slide: emulate the pane at 720×1120 (screenshots come back 800×1244,
  ×1.111), open the plain `/p/<code>/` link (the `/<user>/p/` form renders
  smaller), read the slide's on-page rectangle and scroll it to sit just
  below the fixed header (y=45), take one screenshot per slide and crop
  that rectangle. Script reads only layout (and pauses a video slide on its
  first frame); the page's own Next arrow steps the slides. Let the page
  settle before positioning slide 1 — it shifted once. 4:5 posts render
  720 wide; 3:4 posts render 470 wide (slides ~522px). Titles and hooks
  were set by hand from each cover slide, OCR misreads fixed; 143 (a
  talking-head video cover) and 166 (a family photo dump with no text)
  keep the platform label.
- TikTok photo posts, same day, signed in as Erin (she allowed tiktok.com
  in the pane): 103, 116, 122, 147 captured. Emulate 800×1200 (screenshots
  ~1:1); the slideshow auto-advances, so click the photo once to pause,
  then hover the player before clicking its own arrow buttons (they ignore
  a click without hover) and move the mouse off before each shot. Slides
  are `object-fit: contain`, so crop the contained rectangle computed from
  the img's natural size. TikTok's name/caption/dots overlay stays on the
  image, as in Erin's own screenshots; those lines are stripped from the
  OCR transcript. 122 was imported. The other three were cross-posts of
  Instagram carousels already loaded — 116 the same 9 slides as 118, 103
  the same photos as 102 (one fewer slide, reordered), 147 the same 9 stat
  cards as 143 (photo cover instead of video) — and were merged into them
  on Erin's yes: the TikTok post moved onto the Instagram piece (links
  kept exactly as the sheet has them, shortlinks included, so a re-import
  matches them), the empty piece deleted, the Instagram slides kept, and
  the date taken from the TikTok id. Announcement: 117 pieces, 81 with
  media.
- Erin's folder of 2026-11-17, the evening: 34 videos (the reels and
  saving-off TikToks) with her transcripts, each naming its post's URL, so
  files were matched by URL, not name. Eight more cross-posts surfaced and
  were merged into the piece holding the video, as above: 097 into 098
  (same speech, 58.3s vs 58.2s), 089 into 088 (her file says same video),
  and six Instagram reels the importer's length guard caught against
  stored TikToks — 100→101, 104→105, 119→115, 120→117, 141→145, 144→149,
  each confirmed by identical speech or on-screen text. One of her
  files was 186's post, already stored, so skipped. 168 and 167 are
  different videos that happen to share a length to the millisecond
  (different songs, different files), so `import_media.py` gained
  `--not-a-twin PIECE`: it lets a named piece past the guard once a person
  has checked, and only that piece. 26 videos imported. Transcripts: her
  Whisper text for 15 speech videos (how: "Whisper transcript supplied by
  Erin"), her on-screen descriptions for 8 music-only ones ("on-screen
  text, transcribed by hand"); 108 had a song in the middle of its speech,
  cut to "[music]". One creator's three (111, 167, 168) were lyrics only, so
  they carry Vision OCR of their on-screen text instead, and the lyric
  hooks the tool filled were cleared. 086 (a 22-minute YouTube review)
  came as a transcript only — YouTube wouldn't let her save it — from the
  creator's own captions; it is stored without a video and the page says
  "From the creator's own captions on YouTube". Titles quote each
  transcript; hooks are the first spoken line, speech only. Announcement:
  109 pieces, 107 with media — only 086 (no video) and 099 (expired story)
  have none. On 2026-11-18 Erin added 200, a TikTok by creator 260 posted
  2026-11-17 (3m24s), which made her `content_delivered` (she had only
  opted in). Saved with TikTok's own Download once Erin had signed in
  (203.4s), Whisper transcript with the author's name, "Maasverse" and
  "ACOTAR" corrected: 110 pieces, 77 delivered, 109 with media.
- Stickers Beta, 2026-11-11: 13 of 21 have video, all via TikTok's own
  Download, transcribed and titled (one, a music video over a stats
  screen, keeps its platform label). Four of the 13 are music over
  on-screen text; Whisper's "It's so easy" on two of them was a lyric and
  was replaced with the no-speech marker. Still missing: the four
  saving-off TikToks, the unavailable one, and the TikTok carousel (019).
  The two Instagram posts (011, a photo; 015, 8 slides) were captured from
  screenshots later that day — 15 of 21. Both are reading-stats screens
  with no phrase to quote, so they keep the platform label. The
  unavailable one (005) was removed from the campaign on 2026-11-13: 15 of
  20. The same day Erin supplied the last five — the four saving-off
  videos (002, 007, 013, 016; 002 as its TikTok file, the reel being the
  same video) and 019's eight slides — with transcripts: TikTok's own
  auto-captions for the three with speech (`how`: "TikTok's
  auto-generated captions", shown as such on the page; 007's "phobia"
  corrected to the book's title per her note), and her hand transcription
  of on-screen text for the other two. Stickers Beta is complete, 20 of 20.
- Stickers Beta cross-posts, 2026-11-12: the CRM listed only TikTok links,
  but six of those videos were also posted as Instagram reels, found by
  reading each creator's grid within a week of the TikTok and confirmed by
  length plus caption or transcript (002, 006, 008, 009, 017, 021). 017's
  reel is the same review cut to 176s for Instagram's 3-minute cap (the
  TikTok is 229s), so length alone would have missed it. Every other
  Stickers creator was checked and had no copy, or a private or missing
  Instagram account.
- Stickers Beta, 2026-11-10: nothing saved yet. Its 21 drop folders name
  each post's kind and length, read from the post page: 17 TikTok videos,
  1 TikTok carousel (8 slides), 1 Instagram carousel (8), 1 Instagram single
  photo, and one TikTok shortlink that resolves to a video TikTok reports
  unavailable (posted 2026-01-08, probably deleted or made private). Four
  creators have TikTok saving turned off (`author.downloadSetting` 3), so
  those need a screen recording or the creator's file.
- Softlaunch coverage, 2026-11-10: 46 pieces after de-duplication, 34 with
  video (from Erin's 35 files; one 4-second silent clip matched no post). The
  12 missing are all carousels. The sheet's 42 delivered creators are 30
  complete, 1 partial, 11 with nothing — every one of those 11 posted a
  carousel. A TikTok the sheet put on a Wave 2 creator's softlaunch row was
  posted 2026-10-24 (from its id), after the softlaunch ended, and is the
  same post as her announcement piece; the softlaunch copy was removed.
  On 2026-11-13 Erin decided it counts for the softlaunch after all: piece
  093 was moved there (not copied — one piece, one campaign) and her
  softlaunch status went from `wave_2` to `content_delivered`. She stays
  delivered in the announcement through her other piece (157). So the
  softlaunch holds one piece dated after its end, on Erin's call — don't
  "fix" it back. Softlaunch: 47 pieces, 43 delivered; announcement: 109.
- Softlaunch carousels, 2026-11-11: 10 of 12 loaded from screenshots
  (the 10 Instagram ones, 71 slides), read with Vision and titled from
  their first slide (one cover slide is in a script font Vision missed, so
  its title comes from slide 2). The two TikTok-only carousels came from
  Erin's own screenshots (piece 47: 4 of 5 slides — slide 4 deliberately
  skipped; piece 63: 9), with the creators' captions added to the transcript
  and TikTok's overlay lines (name, caption preview, sound) stripped. All 46
  softlaunch pieces now have media. On 2026-11-13 Erin supplied 47's full
  set of 5 slides and a hand transcript, which replaced the OCR (mostly
  book-spine noise); 46's Whisper transcript had the book's title misheard
  and was corrected from her notes.
- Carousels, 2026-11-11: every folder now names its slide count, read from
  the post page — 12 softlaunch, 13 announcement (whose 44 other posts are
  videos, one an expired story). TikTok photo pages only expose their count
  when opened as `/video/<id>`, which redirects. Two pairs on the
  announcement may be one carousel cross-posted (same creator, same or
  near count): compare the slides before keeping both — 102 against 103,
  118 against 116. Settled 2026-11-17: both were cross-posts (so was
  143/147) and are merged. The importer's duplicate guard only checks
  videos.

## Content by email

Erin emails post links (usually with the video attached) to her address with
a `+creatorwise` tag, saying whose and which campaign. A Claude desktop
scheduled task on her Mac, `creatorwise-content` (every 2h, 08:00–20:00,
prompt in `~/.claude/scheduled-tasks/creatorwise-content/`), reads the mail
with the Gmail connector, runs `tools/content_requests.py` (match and add),
`tools/capture.py` (file the attachment; otherwise TikTok's own Download, or
record an Instagram reel as it plays in the browser pane), then the usual
`import_media.py`, `transcribe_media.py`, titles and `push_data.py`. Full
guide: `docs/content-by-email.md`.

- Matching: the account on the post, then an address in the email; a
  first name only when the author couldn't be read. **An author nobody
  holds is a new creator** (Erin, 2026-11-19), made from the email's
  address, the posts' accounts and the page's name — never a first-name
  match to someone on file. A creator's invite code (looked up in Readwise)
  makes them that creator's referral. Full-name twins, missing address or
  name, and anything ambiguous are held. Only mail from Erin counts.
- Recruitment source `organic` (posted unprompted) was added for these.
- An attachment always wins. Without one (or over Gmail's 12 MB): TikTok's
  own Download, then recording the Instagram post as a last resort — only
  while the pane is on screen (a hidden pane isn't drawn).
  Unrecorded pieces wait on `content_requests.py pending`.
- Recording is capture of what the pane shows, like the carousel
  screenshots — Erin's call, 2026-11-19, after a script that pulled files
  from Instagram's internal API was declined. Keep it that way.
- It replies in the email's thread, to Erin's plain address, only when she
  has to act (held, waiting to record, too big, failed push). Replies start
  with `[creatorwise job]` and the tool skips that marker and anything not
  sent to the tagged address — otherwise its own replies, which sit in the
  same threads from the same sender, would loop.
- **Every emailed post gets a date** (2026-11-23). A TikTok link with an
  id dates itself; Instagram posts and TikTok short links don't, so the
  job's step 6b runs `content_requests.py undated`, reads each page in the
  pane (muted) — Instagram's `<time datetime>`, the URL a short link lands
  on, YouTube's `datePublished` — and stores it with `content_requests.py
  date --post N --when VALUE` (UTC date; the piece takes its earliest
  post's). Undated, a creator has no payment due date and no CAC.
- Processed mail ids live in `data/content-requests.json`; a line per run in
  `data/content-requests.log`.
- First run, 2026-11-19: piece 201 (new, with Erin's file) and an Instagram
  reel added to 190 as its cross-post (same 240.3s readathon video).

## Recruiting by email (Year End Campaign)

**Year End Campaign** (`year-end-campaign`, created 2026-11-24): Dec 8 2026 –
Jan 31 2027, status `planned` until it starts. Terms, from Erin's outreach
email: $100 base + $5 per sign-up in the 2 weeks after posting, plus $100 at
50K views or $500 at 250K. It is **not in `payments.PAY_RULES` yet** — the
view bonus needs engagement readings, which nothing loads automatically.

Erin recruits with one email thread per creator. `tools/outreach.py` takes
`{campaign, contacts: [{email, name, thread, contacted_on, reply, ...}]}`,
where `reply` (`opted_in` / `declined` / `unclear` / `none`) is the reading
of the creator's reply, and records `contacted` or `opted_in`. It matches on
addresses only (creator_emails and the primary) — an unknown address is held
as "not on file", a shared one as ambiguous, and a first name never counts.
Statuses only move forward; briefed or delivered is never touched. Re-running
over the same threads is a no-op, so the job hands over every thread each day.

A Claude desktop scheduled task, `creatorwise-outreach` (19:00 daily, through
2026-12-08 at Erin's request; prompt in
`~/.claude/scheduled-tasks/creatorwise-outreach/`), finds the threads in
Superhuman (read-only), judges each reply, runs the tool and uploads. Some of
the emails also ask for an Announcement grid post by Dec 4: a yes to that
alone is not an opt-in. Invitations come in three forms and under several
subjects (the long email, a short "lining up creators" note, and a short
"we DMed briefly over Instagram" note to people new to Erin), so the job
judges each thread by its first message. First run 2026-11-24, by hand: 75
emailed (all on file), 14 opted in. `data/outreach.log` has a line per run.

## Payment details

Each creator profile has a Payment panel: payee name, address, phone, bank
name, account number and routing number (`PaymentDetails`, one row per
creator, its own table so it needed no migration). **Full account and
routing numbers are stored in plain text at Erin's decision (2026-11-19),
against advice**: the database is copied to the live site behind one shared
password. So, always:

- Never put real payment details in the repo — tests, fixtures, docs,
  commits. Tests use invented accounts and a published routing number.
- Never log, export, print or email them. Errors name the field, not the
  value. The profile shows the account masked (••••1234); the full number is
  only in the edit form. Drafted payment emails must never include them.
- Claude never types a creator's bank numbers in; Erin does — in the form,
  or by running `tools/import_invoices.py <folder of PDFs>` herself, which
  reads invoices locally (PDFKit, Vision for scans), matches each to one
  creator (an address they hold, then exact full name, then full name in
  the file name; otherwise held), previews with account and routing
  masked, and saves only with `--apply`. Fields an invoice doesn't state
  are left as they were; a routing number failing the checksum is left out.
  Claude tested it on invented invoices only and doesn't run it on real ones.
- The form posts with a per-session CSRF token (`csrf_token()`).
- Routing numbers are checked against the ABA checksum; account numbers
  must be 4–17 digits. Blank fields are fine — details arrive piecemeal.

This is the one thing edited **on the live site** as well as the Mac. Since
`push_data.py` replaces the server's database, it first reads
`/api/data/payment-details` (SYNC_TOKEN) and merges any server row newer than
the Mac's (`updated_at`) into the local database. If it can't read them, it
refuses to replace the database rather than risk losing them. The endpoint
is in `datasync.ENDPOINTS`, so the session gate lets the token through —
leaving it out made every upload fail with a 302 in the tests.

Paid by bank transfer (ACH) or **PayPal**: the method and the PayPal address
live on the creator (`payment_method` = `bank` | `paypal`, `payment_handle`
= the PayPal email — columns that already existed, so no migration), edited
in the same panel and carried back from the live site with the bank fields
under the one `updated_at`. The invoice importer sets PayPal from "PayPal:
x@y" or a paypal.me link, and bank transfer when an invoice has both numbers.

A third method, **International ACH Transfer** (`intl_ach`, 2026-11-24, at
Erin's request), adds a SWIFT code (`PaymentDetails.swift_code`, 8 or 11
characters). For it the account may be an IBAN (letters and digits, up to
34) and the routing field takes a foreign bank code without the US checksum;
its invoice lists the SWIFT code. `swift_code` was the first column added to
an existing table: `payments.ensure_columns` adds it with an `ALTER TABLE` on
boot and before `push_data` merges, since `create_all` only makes missing
tables. A server that predates a column doesn't send it, and the merge leaves
the Mac's value alone.

`Creator.payment_ready` — a PayPal address for PayPal, an account and SWIFT
code for an international transfer, otherwise both bank numbers — is what
decides which of Erin's two payment emails a creator gets.

## Payments

One `Payment` per creator per campaign (Erin, 2026-11-20). Only campaigns in
`payments.PAY_RULES` raise them — today the Readwise Books Announcement:
**$100 for the post + $5 per referral in the 14 days from their first post
in the campaign** (first post's day included), **due 14 days after that
first post**. Statuses: `upcoming`, `pending` (due, unpaid), `paid` — the
Payments page lists them with Mark paid / Mark unpaid; a click on the live
site comes back through `push_data.py` like payment details do.

- The first-post date needs every one of their pieces dated (an undated one
  might be the first). On 2026-11-20 the 27 undated pieces of unpaid
  creators were dated: TikTok from its id (short links opened in the pane),
  Instagram from the post page's own `<time datetime>`, YouTube from its
  `datePublished` — all UTC dates, like the TikTok ones. One
  creator's first Announcement piece (111) is dated Oct 26, two days
  before the campaign; it counts from then.
- **A bonus window can start before the first post** when Erin says so
  (2026-11-24): two creators had signups from sharing their link before
  the post on file (stories, an earlier post), so their windows were set to
  Oct 20 → first post + 13 days and recounted (notes on each `Payment`).
  It can also **move forward to a later post** (2026-11-25): one
  creator's first post drew no signups and a later one drew 23 on its
  first day, so her window is that post's day + 13, and she is due the
  day after it closes. `refresh` keeps any hand-set window (`referral_from`
  not the first post) until the first-post date itself changes, and sets
  `due_on` to the later of first post + 14 and the window's close + 1.
- The bonus is counted once the window has closed: `referral-sql` writes one
  query over each creator's own window (shared addresses left out), with a
  ROLLUP checksum; `referral-load` refuses a result that doesn't add up.
- 38 creators were marked paid from Erin's list on 2026-11-20 ("paid before
  creatorwise tracked payments"). Creator 91 was merged into
  creator 292 the same day — one person (Erin): her second address,
  TikTok and payment details moved to 292, record 91 deleted (backup
  `data/creatorwise.db.bak-merge`; its old photo still sits on the server,
  unused). One creator posted but has no piece on file yet, so his
  payment has no date.
- **CAC** (customer acquisition cost) is on each creator's tile on the
  Announcement page (`Campaign.cac_for`): what they were paid — base +
  their 14-day bonus, from their `Payment` — divided by their referrals for
  the whole campaign (`referrals_for`). Asked for by Erin's co-founder,
  2026-11-23; only campaigns in `PAY_RULES` show it. It reads "after
  <date>" until the bonus window closes and is counted, "no referrals"
  for zero, "not dated yet" without a first-post date. The page's
  top row adds **Blended CAC** (`Campaign.blended_cac`): total paid ÷
  those same creators' campaign referrals, over creators whose bonus is
  counted (zero-referral ones included — their cost had no return).
- **The softlaunch has CAC too, from invoices** (2026-11-23). It paid $100
  + $10 per 1,000 impressions, outside creatorwise, so it is not in
  `PAY_RULES`; Erin's 40 invoice files gave 38 `Payment` rows (status
  `paid`, `base_cents` = the invoice total, `referrals` 0, the breakdown in
  `note`). One creator's invoice was saved twice under two titles, and one
  creator had a second, inconsistent copy — Erin confirmed which to use;
  six names on invoices differ from the record's (short forms, a spelling,
  a different surname for 220), matched on her word. `has_pay_rules`
  separates the two kinds: the Payments page lists only rule-based
  campaigns, and a softlaunch creator with no invoice reads "no invoice on
  file". Only the amounts were read from the invoices (a script masked
  every other digit); nothing else was stored. Blended $14.38 ($7,016 ÷
  488 referrals, 38 of 43 creators). Then, on Erin's word the same day:
  creator 40 was paid $2,900 (no invoice on file; a `paid` row with that
  as `base_cents`), and 252 (posted after the window) and the duo 77/78
  (never sent details) were not paid — status `not_paid`, $0, reason in
  `note`. A `not_paid` row reads "CAC · not paid" on the tile, counts in
  blended CAC at $0 (its referrals came free), and `refresh` never
  touches it. Blended $18.96 ($9,916 ÷ 523, 42 of 43); only 70 has no
  invoice. So paid rows get
  counted too: `refresh` gives a paid row the window it lacks (never its
  status), and `referral-sql` / `referral-load` cover paid as well as
  pending. 2026-11-23: the 36 undated pieces of paid creators were dated
  as above, and 37 bonus counts loaded; blended CAC $6.50 over 58
  creators. Emailed posts are dated by the content job (below).
  On 2026-11-23 Erin had the expired story removed (piece 099, its post
  and link deleted), so creator 56's first post counts from Oct 28, and
  creator 103's taken-down reel (139) was dated Oct 29 on her word.
  Creator 232's post arrived the same day as a link from Erin: piece 211,
  a 4-slide Instagram carousel (screenshots, likes hidden), Oct 29 — which
  gave his payment a date, his bonus (2) and invoice 1009.
- **Replies with details**: Claude doesn't enter bank numbers, and doesn't
  read replies to copy them. The daily task lists who has replied (Gmail
  metadata only: sender and date), and Erin saves each reply (.eml via
  "Download message", or a PDF) into her invoices folder and runs
  `tools/import_invoices.py`, which reads saved replies too — it drops the
  quoted original and matches on the sender. Once details are on file, the
  next run drafts that creator's invoice email. Which email a payment last
  got is in `draft_id`'s prefix (`details:` / `invoice:`).
- **Invoices** are PDFs in Erin's creators' layout (`tools/invoice_pdf.swift`,
  numbered from 1001), with full bank numbers at Erin's decision. They live
  in `data/invoices/` (gitignored, never synced: push_data only sends media
  and photos). Nothing prints their contents.
- **Emails** are Erin's two templates (`payments_due.py`: details on file →
  "confirm the attached payment details", with the invoice; otherwise the
  one asking for bank details or PayPal), drafted — never sent — in her
  Superhuman by the daily task `creatorwise-payments` (08:30, after the 7am
  referral refresh). Invoices are attached with Superhuman's
  `upload_attachment`: the task gets an upload URL and `payments_due.py
  upload` POSTs the file's bytes from disk, so the numbers never pass through
  the model. First run 2026-11-20: 20 drafts, 3 with invoices.

## The mail scanner

The app reads `erin@readwise.io` itself over the Gmail API, every 24h, from a
background thread in the web process. Mail becomes a *pending* item in `/inbox`;
nothing is published without Erin accepting it.

- Dormant unless `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and
  `GMAIL_REFRESH_TOKEN` are all set to real values. They're currently seeded
  with the literal `not-configured`, which `gmail.py` reads as unset — contextone
  rejects a blank env value, and `app.yml` has to reference something.
- Setup is `docs/gmail-setup.md`. The Google Cloud half needs Erin.
- **Don't try to schedule this as a periodic job.** It was built that way first
  and cannot work: a Claude connector is bound to an interactive session, and
  contextone's `.mcp.json` ships only its own MCP server. Two cloud sessions
  confirmed it, producing no output at all. (Verified 2026-11-06.)
- Every run writes a `ScanRun` row, shown in `/inbox`, success or failure. This
  is deliberate — contextone keeps **no transcript** for periodic sessions, their
  `status` is stale (sessions from a week ago still read `running`), and the job
  list reported `success` for a job that did nothing. Don't rely on contextone to
  tell you a scheduled thing is working.
- One gunicorn worker owns the timer, decided by an `flock` on
  `data/scanner.lock`. Don't add a second scheduler.
- Failed runs retry after 1h, not 24h.

## contextone CLI gotchas

- `contextone env` has **no `--project` flag** — variables are account-wide.
  `contextone infra deploy` does take one.
- `contextone env get --key K` prints a `K=value` line *and* a JSON line.
  `| tail -1` gives you the JSON blob, not the value. Parse it:
  `| tail -1 | python3 -c 'import sys,json; print(json.load(sys.stdin)["value"])'`
- `contextone env set --value ""` is rejected ("Values must not contain blank
  values"), so a variable `app.yml` references must hold something. That's why
  the GMAIL_* vars are seeded with `not-configured`.
- `contextone infra logs` wants `--id <instance db id>` and `--service app`,
  not `--project`/`--subdomain`. The id is in the deploy output (currently 454).
- Deploy reports `status: degraded` / `unknown` even on a healthy instance.
  Check `/healthz` and the logs instead of trusting it.

## Getting the data onto the server

`data/` is gitignored (public repo), so a deploy carries code and an empty
volume. `tools/push_data.py` copies the local database, `data/media/` and
`data/avatars/` to the deployed instance through `datasync.py`'s endpoints,
never through GitHub. **The local copy is the source of truth**: the server's
database is replaced whole (its previous copy kept as
`creatorwise.db.bak-sync`), files are sent only where their sha256 differs,
and server-only files are removed only with `--prune`.

- `SYNC_TOKEN` guards `/api/data/*` (contextone env var, and in local `.env`;
  503 when unset). `SYNC_URL` in `.env` is the site's address. The tool
  refuses to send the token over plain http except to localhost. Don't add a
  flag to skip verification. First full upload: 2026-11-18, once the
  subdomain had its certificate (deploy gotcha 5).
- Only the shapes media.py and avatars.py store are accepted
  (`media/<id>/video.mp4`, `avatars/<id>.jpg` …); `media-inbox/`, logs and
  backups never leave the Mac.
- A replacement database must pass `integrity_check` and contain the
  creatorwise tables, and is copied in with SQLite's backup API so the other
  gunicorn worker's open handle stays valid.
- Re-run it after anything changes local data by hand — imports, merges.
  The two scheduled jobs (referrals, content by email) upload on their own.

## Adding a model

1. Define it in `models.py`.
2. `db.create_all()` in `app.py` picks up new tables on boot. There are no
   migrations yet — if you need to change an existing column, add Alembic rather
   than editing the SQLite file by hand.
