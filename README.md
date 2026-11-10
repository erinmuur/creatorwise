# creatorwise

> **What this is:** _(fill in — one or two lines on what creatorwise does.)_

A Flask app deployed on [contextone](https://contextone.dev).

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000. `GET /healthz` returns `{"status": "ok"}`.

Local runs use a SQLite database at `data/creatorwise.db`, created automatically on
first boot. The `data/` directory is gitignored except for `.gitkeep`.

## Tests

```bash
for t in tests/test_*.py; do ./venv/bin/python "$t" || break; done
```

No network and no credentials needed — the Gmail tests run against a fake Google
on loopback.

## Importing creators

```bash
./venv/bin/python importer.py ~/Downloads/creators.csv          # dry run
./venv/bin/python importer.py ~/Downloads/creators.csv --apply  # commit
```

Matches columns on aliases rather than an exact header row, so most exports go
in unchanged. Dry run by default; re-running is safe and never erases data.
Full guide: **[docs/importing.md](docs/importing.md)**.

## Importing a campaign's content

```bash
./venv/bin/python content_importer.py ~/Downloads/crm-export.csv --campaign spring-pilot
```

The second pass: who took part in a campaign, how far each of them got, and
what they posted. Ties each row to a creator already in the database (it never
creates one), tracks the whole funnel from `contacted` to `content_delivered`,
reads TikTok post dates out of the URL, and holds anything it can't resolve
for review rather than guessing. Full guide:
**[docs/importing-content.md](docs/importing-content.md)**.

Everything in the dashboard today is sample data — clear it before a real
import with `./venv/bin/python tools/reset_data.py` (counts only without
`--yes`).

## Referrals

Referral counts are per creator per campaign, over a stated window — a creator
with no posted content can still have earned some. They come from the Readwise
production database (invite-code redemptions), which creatorwise deliberately
cannot reach, so they arrive as a JSON file of `{email: count}`:

```bash
./venv/bin/python tools/refresh_referrals.py --campaign bookwise-softlaunch --from 2026-07-15 --to 2026-10-01 --counts counts.json
```

Dry run by default. The SQL that produces the file is in the tool's docstring.

## The mail scanner

creatorwise reads `erin@readwise.io` over the Gmail API every 24 hours and
queues anything that looks like a creator content submission in `/inbox` for
review. Nothing is added to the dashboard without being accepted there.

It stays dormant until the Gmail credentials exist — `/inbox` says so, and
nothing contacts Google. Setup: **[docs/gmail-setup.md](docs/gmail-setup.md)**.

## Deploying

Contextone builds from the `Dockerfile` and clones the repo **from GitHub**, so your
work has to be pushed before it can be deployed:

```bash
git push origin main
```

```bash
contextone repo sync --project creatorwise --ref main --commit $(git rev-parse HEAD)
```

```bash
contextone infra deploy --file .contextone/infra/app.yml --project creatorwise --subdomain creatorwise
```

Use `contextone infra run` instead of `deploy` for a throwaway instance; `deploy` is
for the persistent one with a stable subdomain.

Secrets are **not** committed. Set them as contextone env vars and reference them in
`.contextone/infra/app.yml` as `${ENV.KEY}`.
