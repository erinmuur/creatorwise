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
2. **Private repos need GitHub App access.** The first deploy fails with a clone
   error until the contextone GitHub App is granted access to this repo at
   `contextone.dev/dashboard/integrations`. One-time fix.
3. **Docker layer caching** can serve old code even after a successful deploy. Bump
   `ARG BUILD_ID` in the `Dockerfile` to force a rebuild if that happens.
4. **Never destroy a deployed instance** to fix it — the SQLite volume and SSL certs
   are lost, and repeated recreates hit Let's Encrypt rate limits. Redeploy in place.

## Adding a model

1. Define it in `models.py`.
2. `db.create_all()` in `app.py` picks up new tables on boot. There are no
   migrations yet — if you need to change an existing column, add Alembic rather
   than editing the SQLite file by hand.
