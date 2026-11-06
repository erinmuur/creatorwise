# Giving creatorwise its own Gmail access

The app reads `erin@readwise.io` itself, with a read-only OAuth token you mint
once. Until the three env vars below are set the scanner stays dormant: the
`/inbox` page says it isn't connected, and nothing tries to reach Google.

**This is the part I can't do for you** — creating OAuth credentials means
signing in to Google Cloud as you and granting an app access to your mail.
Steps 1–3 are yours. Step 4 onwards I can run, or you can.

Budget 15 minutes.

---

## The one decision that matters

Make the Google Cloud project **inside the readwise.io organization**, and set
the OAuth app's user type to **Internal**.

`gmail.readonly` is what Google calls a *restricted* scope. The consequences
differ sharply:

| | Internal | External + Testing | External + Published |
|---|---|---|---|
| Refresh token lifetime | indefinite | **7 days** | indefinite |
| Google review needed | no | no | yes — CASA security assessment |

An External app in Testing mode would mean the daily scan silently dies every
week. Internal is the only setup that works unattended, and it's free and
instant. If "Internal" isn't offered, the project isn't in the Workspace
organization — recreate it there rather than settling for External.

---

## 1. Project and API

1. Go to <https://console.cloud.google.com/>.
2. Create a project — call it `creatorwise` — and make sure the
   **Organization** field says `readwise.io`, not "No organization".
3. In that project, enable the Gmail API:
   <https://console.cloud.google.com/apis/library/gmail.googleapis.com> →
   **Enable**.

## 2. OAuth consent screen

Under **APIs & Services → OAuth consent screen** (Google has been relabelling
this area "Google Auth Platform", so the heading may differ):

- User type: **Internal**
- App name: `creatorwise`
- Support email and developer email: your own address
- Scopes: you can leave this empty. The token request names the scope it needs,
  and an Internal app doesn't require it declared up front.

## 3. OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- Application type: **Desktop app**
- Name: `creatorwise scanner`

Copy the **client ID** and **client secret**. The secret is shown once; you can
always come back and reset it.

Desktop clients accept `http://127.0.0.1:<any port>` as a redirect, which is
what the script in the next step uses. You don't need to register a redirect URI
yourself.

## 4. Mint the refresh token

On your Mac, in this repo:

```bash
python3 tools/gmail_auth.py
```

It asks for the client ID and secret, opens your browser, and you sign in as
`erin@readwise.io` and approve read-only Gmail access. The sign-in is yours —
the script only receives the authorization code Google hands back, and prints
the resulting refresh token.

The token reads your mail. Treat it like a password: don't put it in a file in
this repo, don't paste it into chat.

## 5. Store the credentials

These three names already exist, seeded with the placeholder
`not-configured` so that `app.yml` can reference them before they're real.
Overwrite all three:

```bash
contextone env set --key GMAIL_CLIENT_ID --value '<client id>'
```

```bash
contextone env set --key GMAIL_CLIENT_SECRET --value '<client secret>'
```

```bash
contextone env set --key GMAIL_REFRESH_TOKEN --value '<refresh token>'
```

Quote the values. A value starting with `-` is otherwise read as a CLI flag and
`env set` fails quietly — that already happened once with `SECRET_KEY`.

## 6. Deploy

```bash
contextone infra deploy --file .contextone/infra/app.yml --project creatorwise --subdomain creatorwise --ref main --commit $(git rev-parse HEAD)
```

`app.yml` already passes the three variables through, so no infra edit is needed.

## 7. Verify

Does the token work? This reads the account profile, no mail:

`contextone env get` prints a `KEY=value` line *and* a JSON line, so pull the
value out properly rather than piping the last line straight through:

```bash
TOKEN=$(contextone env get --key INGEST_TOKEN | tail -1 | python3 -c 'import sys,json; print(json.load(sys.stdin)["value"])')
```

```bash
curl -s -X POST http://creatorwise.infra.contextone.dev/api/gmail/check -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Expect `"account": "erin@readwise.io"`. A `401 unauthorized` means `$TOKEN`
didn't come out right; a `503` means one of the three GMAIL_* values is still
the placeholder.

Then scan for real — either press **Scan now** on `/inbox`, or:

```bash
curl -s -X POST http://creatorwise.infra.contextone.dev/api/scan -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`/inbox` then shows the scanner panel with the last run, a run history, and
anything it queued.

---

## How it behaves once connected

- **Every 24 hours**, from a thread in the web process. Override with
  `GMAIL_SCAN_INTERVAL_HOURS`.
- **It searches, it doesn't read everything.** The default query is
  `newer_than:3d -in:spam -in:trash (instagram.com OR tiktok.com OR youtube.com
  OR youtu.be)` — mail that mentions a platform domain, from the last three
  days. Override with `GMAIL_QUERY`.
- **The 3-day window overlaps on purpose.** Ingest is idempotent on Gmail's
  message id, so re-seeing mail costs nothing and a missed day heals itself.
- **Nothing is auto-published.** Mail becomes a *pending* item in `/inbox` that
  you accept or dismiss, exactly as before. Platform notification mail
  (`noreply@service.tiktok.com` and friends) is dropped before it gets that far.
- **Every run is recorded**, success or failure, and shown in `/inbox`. This is
  the point: the previous scheduled job failed invisibly, because contextone
  keeps no transcript for periodic sessions and reported `success` for a job
  that did nothing.
- **Failures retry in an hour**, not in 24, so a Google blip doesn't cost a day.
- **One worker scans.** Gunicorn runs several; they contend for an `flock` on
  `data/scanner.lock` and the winner owns the timer.
- `GMAIL_SCAN_ENABLED=0` turns it off without deleting the credentials.

## Troubleshooting

**`invalid_grant` on every run.** The refresh token is dead. Either the app is
External+Testing and hit the 7-day expiry (fix the user type, see above), or the
grant was revoked at <https://myaccount.google.com/permissions>. Re-run step 4.

**The script says Google returned no refresh token.** The account has already
granted this client, so Google issued only an access token. Revoke it at
<https://myaccount.google.com/permissions> and run it again.

**`/inbox` says "Not connected yet" after deploying.** One of the three vars is
still the `not-configured` placeholder, which the app reads as unset on purpose.
`contextone env list` shows the names; check the values with
`contextone env get --key GMAIL_CLIENT_ID` (the client ID is not secret — don't
print the other two).

**Scans succeed but queue nothing.** Probably correct — the query only matches
mail containing a platform link, and `skipped` counts mail it looked at and
rejected. Widen the window to confirm it's working:
`contextone env set --key GMAIL_QUERY --value 'newer_than:14d -in:spam'`,
scan once, then set it back.

## Revoking access

<https://myaccount.google.com/permissions> → `creatorwise` → Remove access.
That kills the token immediately. Scans then fail with `invalid_grant` and say
so in `/inbox`.
