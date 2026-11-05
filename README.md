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
