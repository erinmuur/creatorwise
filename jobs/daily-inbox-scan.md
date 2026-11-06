<!--
STATUS: not scheduled as of 2026-11-06.

This ran once as contextone periodic job 97 and did not work: it reported
success, produced an empty transcript, and queued nothing, despite qualifying
mail sitting inside its 3-day window. The likely cause is that a headless
periodic session does not inherit the Superhuman connector, which is bound to
an interactive Claude session — so the job had no way to read the mailbox, and
failed silently rather than erroring.

The job was deleted rather than left firing daily for nothing (the contextone
CLI has no disable, only create/run/delete).

The dashboard side IS verified working: POSTing a real creator email to
/api/ingest with the production token parses the links and queues the item for
review. Only the "something reads the mailbox on a schedule" half is missing.

To recreate once connector access is solved:

  bash "$HOME/.claude/plugins/cache/context-one-plugin/contextone/<version>/scripts/periodic.sh" create \
    --name "creatorwise-inbox-scan" \
    --schedule "0 13 * * *" \
    --timeout-seconds 900 \
    --workspace-path creatorwise \
    --prompt "$(cat jobs/daily-inbox-scan.md)"

Schedule is UTC (13:00 UTC = 8am ET). Requires INGEST_TOKEN in contextone env,
which is already set.
-->

Scan erin@readwise.io for creator content submissions and send them to the
creatorwise dashboard for review.

READ-ONLY. Do not send, reply to, forward, draft, archive, label, delete, or
modify any email or calendar event. Do not contact any creator. The only write
this job performs is the HTTP POST described below.

Steps:

1. Call list_accounts to confirm erin@readwise.io is linked. If it is not,
   stop and report that — do not substitute another account.

2. Find candidate messages from the last 3 days. Run list_threads several
   times with these body_contains values, each with limit 50:
     - "instagram.com"
     - "tiktok.com"
     - "youtube.com"
     - "youtu.be"
   Use start_date = 3 days ago. The overlap with previous runs is deliberate:
   the dashboard de-duplicates on message_id, so re-sending is harmless and
   missing a message is not.

3. For each thread, use get_thread to read the full message bodies. Keep only
   messages where ALL of these hold:
     - the sender is NOT erin@readwise.io or any @readwise.io address
       (her own replies quote the creator's links and would double-count)
     - the message body actually contains a link to instagram.com, tiktok.com,
       youtube.com or youtu.be
   Do NOT try to filter out platform notification mail yourself, and do NOT
   try to decide which links are real posts — the dashboard does both, and
   doing it here would create two places where that logic can drift.

4. POST the messages to the dashboard, in batches of at most 25:

   curl -sS -X POST http://creatorwise.infra.contextone.dev/api/ingest \
     -H "Authorization: Bearer $INGEST_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"messages":[ ... ]}'

   Each message object is:
     {"message_id": "<the message_id from Superhuman>",
      "sender": "<from address only, no display name>",
      "subject": "<subject>",
      "body": "<full plain-text body>",
      "received_on": "<YYYY-MM-DD>"}

   Use plain http://, not https:// — this subdomain's TLS certificate is
   currently broken, so https will fail certificate verification.

   $INGEST_TOKEN is injected into this session from contextone env. If it is
   empty, stop and report that rather than posting without it.

5. The response looks like {"queued":N,"duplicates":N,"skipped_no_links":N}.
   Report the totals across all batches, and list the sender and subject of
   each newly queued message. A 503 means INGEST_TOKEN is not set on the
   dashboard; a 401 means this job's token does not match it.

Erin reviews and confirms each queued item at
http://creatorwise.infra.contextone.dev/inbox — nothing is added to a campaign
without her clicking Add, so err toward sending a message rather than skipping
it.
