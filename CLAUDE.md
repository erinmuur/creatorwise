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
| `avatars.py` | profile photos on disk under `data/avatars/` |
| `tools/fetch_avatars.py` | download profile photos from Readwise Books profiles |
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

5. **TLS is broken on this subdomain, and the site is therefore unreachable in a
   browser.** `https://creatorwise.infra.contextone.dev` serves a certificate for
   an unrelated app (`CN=cx-emails-agent.app.contextone.dev`, issued Sep 5), so
   HTTPS fails verification.

   **`http://` is NOT a workaround.** The whole `.dev` TLD is on the HSTS preload
   list baked into every major browser, so `http://` on any `*.dev` host is
   upgraded to HTTPS before the request leaves the machine — you get
   `ERR_CERT_COMMON_NAME_INVALID`, not the app. Confirmed against
   `hstspreload.org/api/v2/status?domain=dev` → `"status": "preloaded"`, and by
   loading it in a real browser. (An earlier note here said to use `http://`
   meanwhile; that was wrong, and only ever true for `curl`, which ignores HSTS.)

   So: `curl` and the `/api/*` endpoints work fine over `http://`. A human with a
   browser cannot open the dashboard at all until a certificate exists.

   Four deploys and a restart across 2026-11-05 → 11-09 produced no new cert.
   `spark.infra.contextone.dev` serves a correct cert, so issuance works
   platform-wide; this is specific to this subdomain. Suspected Let's Encrypt
   rate limiting from the earlier failed (private-repo) deploys. The CLI exposes
   no cert or custom-domain command, so the options are a contextone support
   ticket or a different subdomain.

   For local work, just run the app on localhost — no TLS involved:
   ```
   ./venv/bin/python app.py    # http://localhost:5000
   ```
   Do NOT `destroy` the instance to fix this — it loses the volume and risks
   rate-limiting the subdomain for days.

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
Instagram), 18 of them dated. One of those pieces is dated 2026-02-23, well
after the rest of the cohort (2025-10-13 to 2025-12-08) — the CRM lists that
post against a participant, so it was kept on the CRM's word, not because the
date fits. Don't "fix" the range by dropping it. The other 257 creators have no campaign: the
recruitment export has no campaign column and its onboarding dates span Jul
2025 to Jul 2026, so assigning them would be fabrication.

Bookwise Softlaunch, from the opt-ins sheet on 2026-11-09: 154 participants —
8 contacted, 69 opted in, 22 wave 2, 11 briefed, 42 delivered, 2 declined —
with 49 pieces from 76 links (26 of them cross-posted). Referrals are the
live figures, not the sheet's: 577 across 50 creators, counted over
Jul 15 – Oct 1 2026 from invite-code redemptions.

Readwise Books Announcement, from the October outreach sheet on 2026-11-09:
202 participants — 5 not contacted, 57 contacted, 94 opted in, 42 delivered,
4 declined — with 57 pieces from 79 links, and 1,700 referrals across 81
creators counted Oct 28 – Nov 9 2026. **41 of those creators earned referrals
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

**The Stickers Beta had no referral component** — its zero is correct, not a
gap, and no window exists to pull one over. Don't go looking.

Still absent: engagement numbers for every piece, both campaigns' dates and
payment terms, and all payment details.

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
  exactly one per platform and more than one platform. Four reels on a row are
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
reads 154 participants and 42 delivered on 49 videos.

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
- A creator missing from a later file drops to zero rather than keeping a
  stale number that looks current.

Verified on 2026-11-09 against the softlaunch: over Jul 15 – Oct 1 2026,
35 of the sheet's 41 numbers matched the live count exactly, which is what
established that the sheet was campaign-windowed and that this window is the
right one. The sheet missed 13 creators who had earned referrals (58 in all,
one of them 25 with no content at all), and was ~6 weeks stale — its totals
matched the live cumulative figure around late September.

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
  they must be downloaded, never hot-linked. YouTube gives `og:image` to a
  plain request; Instagram and TikTok only render in a real browser.
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

## Adding a model

1. Define it in `models.py`.
2. `db.create_all()` in `app.py` picks up new tables on boot. There are no
   migrations yet — if you need to change an existing column, add Alembic rather
   than editing the SQLite file by hand.
