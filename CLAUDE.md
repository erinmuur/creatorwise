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
| `models.py` | SQLAlchemy models (`db` lives here) |
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

5. **TLS is broken on this subdomain as of 2026-11-05.** `https://creatorwise.infra.contextone.dev`
   serves a certificate for an unrelated app (`CN=cx-emails-agent.app.contextone.dev`),
   so HTTPS fails verification. Use `http://` meanwhile — the app itself is healthy.
   A redeploy and a `restart` both failed to trigger issuance; `spark.infra.contextone.dev`
   serves a correct cert, so issuance works platform-wide and this is specific to
   this subdomain. Suspected Let's Encrypt rate limiting from the earlier failed
   deploys. Retry later with a redeploy (not a restart — that doesn't re-trigger it):
   ```
   contextone infra deploy --file .contextone/infra/app.yml --project creatorwise \
     --subdomain creatorwise --ref main --commit $(git rev-parse HEAD)
   ```
   Do NOT `destroy` the instance to fix this — it loses the volume and risks
   rate-limiting the subdomain for days.

## Adding a model

1. Define it in `models.py`.
2. `db.create_all()` in `app.py` picks up new tables on boot. There are no
   migrations yet — if you need to change an existing column, add Alembic rather
   than editing the SQLite file by hand.
