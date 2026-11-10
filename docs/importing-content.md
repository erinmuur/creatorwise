# Importing a campaign's content

`importer.py` answers *who are our creators?* This one answers *who took part
in this campaign, and what did they post?*

```bash
./venv/bin/python content_importer.py ~/Downloads/crm-export.csv \
    --campaign spring-pilot --campaign-name "Spring Pilot"
```

Dry run by default. Read the report, then add `--apply`. `--campaign-name` is
only needed the first time; after that the slug is enough.

It creates **Participation** rows (who was in the campaign, and whether they
delivered) and **UgcPiece** / **PlatformPost** rows (one per post link). It
never creates a creator — run `importer.py` first.

## The columns it looks for

| column | what it does |
|---|---|
| `Creator Name` | matched against loaded creators |
| `Email` | matched against their address — the strongest signal there is |
| `Handle/Profile URL` | matched against their stored handles |
| `Post` / `Content` | one or more links in one cell |
| `Status` | how far the process got — Posted, Briefed |
| `Response` | what they said — Opted-in, No reply, Declined, Wave 2 |
| `Referrals` | signups credited to them, recorded as reported |
| `Referrer`, `Posted` | read, reported; the referral graph comes from `importer.py` |

Other spellings go in `ALIASES` at the top of the file — a one-line addition,
not a reshape of the spreadsheet. Columns it doesn't know are **reported with a
count of how many rows have a value**, and listed in full under `--verbose`, so
nothing vanishes quietly.

## How a row is tied to a creator

Four signals, each either strong or weak:

| signal | strong when | weak when |
|---|---|---|
| email | it matches a creator's address | — it is never weak |
| profile handle | it matches on the platform the link claims | it's their handle on a *different* platform |
| post handle | same | same |
| name | it matches a creator exactly | it only shares a word |

An address outranks everything, including a name that points somewhere else:
addresses are unique by construction, while two creators can share a name and a
sheet can spell one wrong. The disagreement is still reported.

**Every** address is matched on, not just the primary — a creator who was
recruited under one address and opts in under another is still one creator.
When a row matches somebody and carries an address we don't have for them yet,
that address is filed under their existing profile and the report says so. It
is never grounds for creating a second copy of them, which is how a duplicate
got made before this existed.

**One strong signal that nothing else contradicts resolves the row.** Weak
signals never do, even two agreeing — that's still a guess. Two strong signals
pointing at different people is a conflict, and the row is held.

Anything unresolved is listed under `NEEDS REVIEW` and skipped. This matters
more here than in the creator import: campaign totals are summed per creator,
so attributing one person's video to another corrupts both their numbers *and*
hides the mistake behind a plausible-looking page.

The ladder is what lets a stale profile column through safely. If the CRM says
`@beapeaches` but the post link is from `@bea.reads`, and the second is a
handle we hold, the row imports against that creator — and the disagreement is
printed as a warning, because one of the two columns is wrong and somebody
should fix it at the source.

A handle that only matches on another platform is the case this is careful
about. `@thecassianvale` being somebody's Instagram name is not evidence that
a `tiktok.com/@thecassianvale` link is theirs, so on its own it won't resolve
a row.

## Where everybody stands

A campaign is a pipeline, and an outreach sheet records the whole of it — not
just the end. Two columns are read together, with `Status` winning wherever it
is definite and `Response` filling in the rest:

| status | what it means | typical sheet values |
|---|---|---|
| `contacted` | reached out to, never replied | Response: No reply |
| `opted_in` | said yes, nothing sent yet | Opted-in, Extra enthusiastic, Scheduled Call |
| `wave_2` | volunteered for, or pushed to, a later wave | Volunteered/Bumped to Wave 2 |
| `briefed` | had the briefing, hasn't posted | Status: Briefed |
| `content_delivered` | posted | Status: Posted |
| `declined` | said no | Response: Declined |

Keeping these apart is the whole point. "112 didn't deliver" and "97 of them
were never briefed" are very different sentences about the same campaign, and
only the second one is true.

**Follow-through is measured against the strongest commitment the campaign can
evidence** — the briefed where briefing was tracked, the opt-ins otherwise —
not against everyone in the sheet. Somebody who was never briefed hasn't
failed to deliver, and a campaign whose sheet never recorded briefing would
otherwise divide its deliverers by themselves and report 100%.

A row whose status isn't `content_delivered` but which has post links anyway is
imported as the sheet describes it, with the contradiction reported. The sheet
is somebody's record of what happened; the links are evidence. Neither gets to
silently overrule the other.

A sheet with no usable status column falls back to the old rule — links mean
delivered, no links mean opted in — so the earlier CRM export still reads the
same way it did.

## Referral counts

A `Referrals` column is stored as a `ReferralSnapshot` with `window_days = 0`,
which means **as reported**: a number copied off a sheet with no measurement
window attached. It is deliberately not stored as a 14-day figure, so the two
are never summed as if they were the same measurement, and the dashboard says
which kind it is showing.

The sheet counts per creator, not per video. When a row yields one piece the
number goes on it. When a row yields several, it goes on the earliest and the
report says so — the creator and campaign totals stay exact, only the
per-piece split is unknown. A row with referrals and no content at all is
reported and dropped, because there is nowhere to put it.

Re-running updates a changed count in place rather than adding a second
snapshot.

## Post dates come out of the URL

A TikTok post id has its creation time in the top 32 bits, so `posted_on` is
read straight off the link. Nothing else offers that: Instagram shortcodes and
YouTube ids carry no retrievable date, and TikTok's `/t/ABC123` shortlinks
don't contain the id at all. Those pieces import **without a date**, and the
report says how many.

## What it won't do

**Invent engagement.** No views, likes or comments are written — a CRM export
doesn't have them, and a zero that looks like a measurement is worse than an
obvious blank. Those numbers have to come from the platforms.

**Guess which link pairs with which.** Links on a row are merged into one
cross-posted piece only when there is exactly one per platform and more than
one platform — a TikTok and a Reel and a Short are one video in three places,
and counting its engagement three times would be wrong. Four Instagram reels
on a row are four videos. Two TikToks beside two Reels give no way to tell
which pairs with which, so they stay four pieces rather than two guesses.

**Guess a campaign window.** A campaign it creates has a name and a slug and
nothing else. Dates and payment terms are things you know and the spreadsheet
doesn't.

## Re-running it

Safe, and the normal way to work. Participations are keyed on
(creator, campaign) and posts on their URL, so a second run reports everything
as `unchanged`. Add a post link to the sheet and re-run: that creator moves
from `Opted in` to `Delivered` and the new piece appears.

The whole file commits in one transaction — a failure leaves the database
exactly as it was.

## Privacy

Same rule as everywhere else here: this repository is **public**, so no real
creator data goes in it. Exports stay outside the repo, every handle on this
page is invented, and the fixtures in `tests/test_content_importer.py` use
invented names with `example.com` addresses. See [importing.md](importing.md).
