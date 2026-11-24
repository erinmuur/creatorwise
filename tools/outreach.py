"""Track who Erin has emailed about a campaign, and who said yes.

Erin recruits for a campaign by email, one thread per creator. A scheduled
Claude task reads those threads with the Superhuman connector, decides from
each creator's replies whether they opted in, and hands this tool a JSON file:

    {"campaign": "year-end-campaign",
     "contacts": [{"email": "bea@example.com", "name": "Bea",
                   "thread": "<thread id>", "contacted_on": "2026-11-24",
                   "reply": "opted_in" | "declined" | "none" | "unclear",
                   "replied_on": "2026-11-24", "evidence": "I'm in!"}]}

`name` is how Erin greeted them (often a first name), `evidence` a few words
of their reply. Reading the reply is the task's judgement; everything after
that is here:

- Matching is on the address only, against every address in creator_emails
  (and the primary). A first name is never enough to pick a creator.
- An address nobody holds is held as "not on file" — a new person needs a
  full name before they become a creator (CLAUDE.md: check names as well as
  addresses). An address two creators share is held as ambiguous.
- Statuses only move forward through the funnel: contacted -> opted_in.
  Anyone already briefed or delivered is left alone. "declined" is recorded
  for somebody contacted or opted in; a later yes from somebody declined is
  applied and reported. Nothing is ever deleted.
- Re-running over the same threads changes nothing, so the daily task can
  hand over every thread since outreach began.

    plan  FILE            dry run: what would change
    apply FILE            write it (backs up the database first, logs a line)
"""

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPLIES = ("opted_in", "declined", "none", "unclear")

# Statuses this tool may move somebody out of, per target.
MOVABLE = {
    "contacted": {"not_contacted"},
    "opted_in": {"not_contacted", "contacted", "declined"},
    "declined": {"not_contacted", "contacted", "opted_in"},
}


def target_status(reply):
    if reply not in REPLIES:
        raise ValueError(f"reply must be one of {', '.join(REPLIES)}, not {reply!r}")
    return {"opted_in": "opted_in", "declined": "declined"}.get(reply, "contacted")


def holders_of(address, models):
    """Creator ids holding this address, primary or secondary."""
    Creator, CreatorEmail = models["Creator"], models["CreatorEmail"]
    address = address.strip().lower()
    ids = {e.creator_id for e in CreatorEmail.query.filter(
        models["db"].func.lower(CreatorEmail.address) == address)}
    ids |= {c.id for c in Creator.query.filter(
        models["db"].func.lower(Creator.email) == address)}
    return sorted(ids)


def merge_contacts(contacts):
    """One entry per address. Two threads to one person: the furthest reply
    wins (a yes in either thread is a yes), and the earliest contact date."""
    rank = {"none": 0, "unclear": 1, "declined": 2, "opted_in": 3}
    out = {}
    for c in contacts:
        key = c["email"].strip().lower()
        target_status(c.get("reply", "none"))  # validate
        if key not in out:
            out[key] = dict(c, email=key, threads=[c.get("thread")])
            continue
        prev = out[key]
        prev["threads"].append(c.get("thread"))
        if c.get("contacted_on") and (not prev.get("contacted_on")
                                      or c["contacted_on"] < prev["contacted_on"]):
            prev["contacted_on"] = c["contacted_on"]
        if rank[c.get("reply", "none")] > rank[prev.get("reply", "none")]:
            for k in ("reply", "replied_on", "evidence"):
                prev[k] = c.get(k)
    return list(out.values())


def plan(payload, models):
    Campaign, Creator, Participation = (models["Campaign"], models["Creator"],
                                        models["Participation"])
    db = models["db"]
    campaign = Campaign.query.filter_by(slug=payload["campaign"]).first()
    if campaign is None:
        raise SystemExit(f"no campaign {payload['campaign']!r}")
    rows = []
    for c in merge_contacts(payload.get("contacts", [])):
        row = {"email": c["email"], "name": c.get("name"), "reply": c.get("reply", "none"),
               "evidence": c.get("evidence"), "replied_on": c.get("replied_on"),
               "contacted_on": c.get("contacted_on")}
        ids = holders_of(c["email"], models)
        if not ids:
            row.update(action="held", reason="not on file")
        elif len(ids) > 1:
            row.update(action="held", reason="address shared by creators "
                       + ", ".join(str(i) for i in ids))
        else:
            creator = db.session.get(Creator, ids[0])
            row["creator"] = creator.id
            row["creator_name"] = creator.name
            want = target_status(row["reply"])
            part = Participation.query.filter_by(creator_id=creator.id,
                                                 campaign_id=campaign.id).first()
            if part is None:
                row.update(action="add", status=want)
            elif part.status == want:
                row.update(action="unchanged", status=want)
            elif part.status in MOVABLE[want]:
                row.update(action="change", status=want, was=part.status)
            else:
                row.update(action="unchanged", status=part.status,
                           note=(f"reply reads {row['reply']}, left at {part.status}"
                                 if want != "contacted" else None))
        rows.append(row)
    return campaign, rows


def apply(campaign, rows, models, today=None):
    Participation, db = models["Participation"], models["db"]
    today = today or date.today()
    for row in rows:
        if row["action"] not in ("add", "change"):
            continue
        part = Participation.query.filter_by(creator_id=row["creator"],
                                             campaign_id=campaign.id).first()
        if part is None:
            part = Participation(creator_id=row["creator"], campaign_id=campaign.id)
            db.session.add(part)
        part.status = row["status"]
        if row["status"] == "opted_in" and not part.opted_in_on:
            replied = row.get("replied_on")
            part.opted_in_on = date.fromisoformat(replied) if replied else today
    db.session.commit()


def counts(rows):
    out = {}
    for r in rows:
        out[r["action"]] = out.get(r["action"], 0) + 1
    return out


def render(campaign, rows):
    lines = [f"{campaign.name}: {len(rows)} people emailed"]
    order = {"add": 0, "change": 1, "held": 2, "unchanged": 3}
    for r in sorted(rows, key=lambda r: (order[r["action"]], r.get("creator_name") or r["email"])):
        who = (f"{r['creator_name']} ({r['creator']})" if r.get("creator")
               else f"{r.get('name') or '?'} <{r['email']}>")
        if r["action"] == "add":
            what = f"add as {r['status']}"
        elif r["action"] == "change":
            what = f"{r['was']} -> {r['status']}"
        elif r["action"] == "held":
            what = f"HELD: {r['reason']}"
        else:
            what = f"unchanged ({r['status']})" + (f" — {r['note']}" if r.get("note") else "")
        ev = f'  "{r["evidence"]}"' if r.get("evidence") and r["action"] in ("add", "change") \
            and r["reply"] in ("opted_in", "declined") else ""
        lines.append(f"  {who}: {what}{ev}")
    c = counts(rows)
    lines.append("  " + ", ".join(f"{v} {k}" for k, v in sorted(c.items())))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("plan", "apply"))
    p.add_argument("file")
    args = p.parse_args(argv)

    from app import app
    import models as m
    models = {k: getattr(m, k) for k in ("db", "Campaign", "Creator", "CreatorEmail",
                                         "Participation")}
    with open(args.file) as f:
        payload = json.load(f)
    with app.app_context():
        campaign, rows = plan(payload, models)
        print(render(campaign, rows))
        if args.command == "plan":
            print("\n(dry run — nothing written. Re-run with apply.)")
            return
        data = os.path.join(ROOT, "data")
        db_path = os.path.join(data, "creatorwise.db")
        if os.path.exists(db_path):
            shutil.copy2(db_path, db_path + ".bak-outreach")
        apply(campaign, rows, models)
        status = {}
        for part in campaign.participations:
            status[part.status] = status.get(part.status, 0) + 1
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(os.path.join(data, "outreach.log"), "a") as f:
            f.write(f"{stamp} {campaign.slug} emailed={len(rows)} "
                    + " ".join(f"{k}={v}" for k, v in sorted(counts(rows).items()))
                    + " | " + " ".join(f"{k}={v}" for k, v in sorted(status.items())) + "\n")
        print("\nWritten. Campaign now: "
              + ", ".join(f"{v} {k}" for k, v in sorted(status.items())))


if __name__ == "__main__":
    main()
