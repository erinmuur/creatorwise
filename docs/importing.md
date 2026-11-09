# Importing creators

The importer meets your export where it is. It matches columns on aliases
rather than demanding an exact header row, so a Betawise CSV, a Notion export
and a hand-kept Google Sheet can all go in without being reshaped first.

**It never writes unless you ask it to.** A plain run is a dry run that prints
what it *would* do; `--apply` commits.

## The short version

```bash
./venv/bin/python importer.py ~/Downloads/creators.csv
```

Read the report. Then:

```bash
./venv/bin/python importer.py ~/Downloads/creators.csv --apply --campaign fall-2025-sticker-beta
```

## Clear the sample data first

Everything in the dashboard right now is invented. Real creators must not land
next to the sample ones — once mixed, there's no reliable way to separate them.

```bash
./venv/bin/python tools/reset_data.py
```

That only counts. Add `--yes` to actually clear it. Campaigns are kept by
default, since the three in there carry the real names and dates from the two
pilots; `--campaigns` clears those too.

Then set `SEED_SAMPLE_DATA=0` in `.env`, or the sample data comes straight back
on the next restart.

## What the report tells you

```
Columns recognized: 10
    'IG Handle' -> instagram
    ...
Columns NOT recognized (ignored — add them to ALIASES if they matter):
    'Vibe Check'

4 to create, 0 to update, 0 unchanged, 3 blocked

BLOCKED (3) — these rows are skipped:
    line 5: noname@example.com — no name
    line 6: Priya S — no usable email address
    line 7: Bea H — duplicate of line 2, under a different name ('Bea Hall')

WARNINGS (2 rows) — imported, but check these:
    line 4: Marco Reyes — extra addresses in this cell, not imported: marco.reyes@work.com
```

Three things worth knowing about that output:

- **Blocked rows are skipped, never guessed at.** A row with no email, no name,
  or a duplicate address is reported with its line number so you can fix the
  sheet. The rest of the file still imports.
- **Unrecognized columns are reported, not silently dropped.** If your export
  has a column that matters, tell me its name and I'll add it to `ALIASES` in
  [importer.py](../importer.py) — it's a one-line change.
- **Warnings mean imported-but-check.** The row went in; something in it
  couldn't be read cleanly.

## What it understands

| Our field | Header spellings it accepts |
|---|---|
| name | Name, Creator, Creator Name, Full Name, Display Name |
| email | Email, Email Address, E-mail, Contact Email, Primary Email |
| instagram / tiktok / youtube | the platform name, IG/TT/YT, "… Handle", "… URL", "… Username" |
| threads / x | Threads, X, Twitter, "… URL" — stored as handles, but no metrics |
| referred by | Referred By, Referrer, Recruited By, Invited By |
| onboarded | Onboarded, Onboarded Date, Joined On, Date Onboarded |
| other links | Other URLs, Links, Linktree, Website |
| follower counts | "Instagram Followers", "IG Follower Count", "Subscribers", … |
| recruitment source | Source, Recruited Via, Channel, How Recruited |
| payment | Pay via, Payment Method, PayPal, Venmo, Payout Handle |
| status | Status, Participation Status, Follow Through |
| opted in | Opted In, Opt In Date, Joined, Signed Up |

Values are normalized too:

- **Followers**: `12.4K` → 12400, `1,234` → 1234, `1.2M` → 1200000, `~5k` → 5000.
  A comma is always a thousands separator, never a decimal point.
- **Handles**: `@bea`, `bea`, `instagram.com/bea` and
  `https://www.instagram.com/bea/?hl=en` all become `@bea`, and the URL
  is kept when there was one. A link to a *post* (`/reel/ABC`) is not a handle
  and won't be mistaken for one.
- **Source**: "Beta Tester" → betawise, "Cold DM" → outbound, and so on.
  Anything unrecognized is kept verbatim as a note, with a warning — never
  dropped and never forced into the wrong bucket.
- **Status**: "Posted"/"Delivered" → content_delivered, "Ghosted"/"Declined" →
  no_show, "Confirmed"/"Yes" → opted_in.
- **Dates**: `2025-09-15`, `09/15/2025`, `Sep 15, 2025`, `15 Sep 2025`.
- **Source**: the real categories are Referral, Cold outreach, Existing Readwise
  user, Inbound and Alpha tester. Anything else is kept verbatim as a note.
- **YouTube**: legacy `/channel/UC…` URLs keep the channel id, since that is the
  only identifier those channels have.
- **Blanks**: `—`, `n/a`, `TBD`, `?`, `-` all count as empty.

## Re-running it

Safe, and the normal way to work. Creators are matched on lowercased email:

- A creator already there with no changes → **unchanged**, nothing written.
- Changed values → **update**, and the report lists each old → new value.
- **A blank cell never erases existing data.** Partial exports are normal; a
  missing column means "no new information", not "delete what you have".

So you can import a rough list now and re-import a fuller one later without
losing anything or creating duplicates.

## The referral graph

`referred_by` isn't stored as a note — it's a link between creators. In the real
list, 214 of 280 came in through someone else, and 204 of those referrers are
creators in the same file. A profile shows who referred them and everyone they
referred, both clickable.

Referrer cells hold whatever got typed into the CRM, so resolution tries, in
order: the creator's name, their handle, and the local part of an email address
(people are often listed by their TikTok handle rather than their name).

Anything ambiguous stays as text and is labelled "not matched" on the profile.
From the real import, five of 214 didn't resolve — three were a first name
shared by several creators, one named somebody outside the programme, and one
cell named two people. Picking one of four creators called Sam would have been
inventing a fact.

Fix those by making the sheet unambiguous and re-importing, or set them by hand.
A creator named as their own referrer is refused — that would loop the graph.

## Two things it deliberately won't do

**Attribute an ambiguous follower count.** One `Followers` column with both an
Instagram and a TikTok handle on the row — it warns and leaves both empty
rather than picking one. Inventing a number is worse than missing one.

**Merge a creator's second email address.** `Creator.email` is unique and
single. A cell holding `bookshelfbea@example.com / bea.k@example.net`
imports the first and warns about the second. If people in your list genuinely
have two addresses — and from the inbox scan, at least one does — that needs a
small schema change (a `creator_emails` table) rather than a guess. Worth doing
if it's common in your data; tell me how often it comes up.

## If the import fails partway

It can't. The whole file commits in one transaction, so a failure leaves the
database exactly as it was. Fix the sheet and run it again.


## A note on privacy

This repository is **public** — it has to be, or contextone can't deploy it.

So no real creator data goes in it. The database is gitignored, exports stay
outside the repo, and tests use invented names and `example.com` addresses.
Real names, emails and handles don't belong in fixtures, docs, comments or
commit messages either, where they're just as public and much harder to remove.
