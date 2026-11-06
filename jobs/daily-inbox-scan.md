<!--
SUPERSEDED — 2026-11-06.

This prompt was written for a contextone periodic job that would read
erin@readwise.io with the Superhuman connector and POST matching mail to
/api/ingest. It cannot work, and the job has been deleted.

Why: a Claude connector is bound to an interactive session. contextone's
.mcp.json ships only its own MCP server, which is why the other periodic jobs
(New Relic, Sentry, Slack, Readwise DB) run fine — none of them needs a
connector. Two cloud sessions confirmed it empirically, both producing no
output at all and never reaching the endpoint.

Replaced by the app reading Gmail directly: gmail.py + scan.py, scheduled
in-process. See docs/gmail-setup.md.

Kept for the record, and because the matching rules below are still the right
description of what a submission looks like.
-->

# Daily inbox scan (historical)

Scan erin@readwise.io for creator content submissions and queue them in
creatorwise for review.

## What counts as a submission

An email is a content submission when it contains at least one link to a
*specific post* on Instagram, TikTok or YouTube:

- Instagram: `/p/`, `/reel/`, `/reels/` or `/tv/` — not a bare `/handle`
- TikTok: `/video/` in the path, or a `vm.`/`vt.`/`/t/` short link — not a bare
  `/@handle`
- YouTube: `/watch`, `/shorts`, `/v/`, `/live` or a `youtu.be` link — not a
  channel

Profile and channel links are not submissions.

## What to exclude

Platform notification mail, which is full of real content links and never a
submission. TikTok alone sends a steady stream of "X posted:" and "X mentioned
you" mails from `@service.tiktok.com`. Also skip `noreply@`, `notifications@`
and anything from `mail.instagram.com` or `facebookmail.com`.

## Where it goes

Nothing is published automatically. Each message becomes a pending item in
`/inbox` showing the extracted links, the creator it matched and how, and the
campaign guessed from the date. Erin accepts or dismisses it.
