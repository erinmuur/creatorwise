#!/usr/bin/env python3
"""Read subscriber counts for creators' YouTube channels from the official API.

YouTube is the one platform here with a public API that answers "how many
subscribers does this channel have?" for any channel, so it is the one
platform creatorwise looks up directly. It needs an API key, which is free:
Google Cloud console -> APIs & Services -> enable "YouTube Data API v3" ->
Credentials -> Create API key. Give it to this tool in the environment, never
on the command line and never in the repo:

    YOUTUBE_API_KEY=... python3 tools/fetch_youtube_followers.py --out yt.json
    python3 tools/refresh_followers.py --counts yt.json --source "youtube data api" --apply

It writes readings in the shape refresh_followers.py loads; it does not touch
the database itself. One request per channel, one quota unit each — the free
daily quota is 10,000.

A channel that hides its subscriber count is skipped and reported, not
recorded as zero.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fetch_avatars import _ssl_context  # noqa: E402
from tools.refresh_followers import worklist  # noqa: E402

API = "https://www.googleapis.com/youtube/v3/channels"


def http_json(url, timeout=20):
    """GET a URL and parse the JSON body. Raises OSError on failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise OSError(f"HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise OSError(str(error.reason)) from error


def lookup(handle, key, fetch=http_json):
    """Return a reading dict for one channel, or (None, reason) to skip it."""
    # A legacy /channel/UC… id is looked up by id; everything else is an @handle.
    if handle.lower().startswith("uc") and len(handle) >= 22:
        params = {"part": "statistics", "id": handle, "key": key}
    else:
        params = {"part": "statistics", "forHandle": "@" + handle, "key": key}
    data = fetch(API + "?" + urllib.parse.urlencode(params))

    items = data.get("items") or []
    if not items:
        return {"platform": "youtube", "handle": handle, "status": "not_found"}, None
    stats = items[0].get("statistics") or {}
    if stats.get("hiddenSubscriberCount"):
        return None, "subscriber count is hidden"
    if "subscriberCount" not in stats:
        return None, "no subscriber count in the response"
    return {
        "platform": "youtube", "handle": handle,
        "followers": int(stats["subscriberCount"]),
    }, None


def collect(handles, key, fetch=http_json):
    """Look up every handle. Returns (readings, skipped)."""
    readings, skipped = [], []
    for handle in handles:
        try:
            reading, why = lookup(handle, key, fetch=fetch)
        except OSError as error:
            skipped.append((handle, str(error)))
            continue
        if reading is None:
            skipped.append((handle, why))
        else:
            readings.append(reading)
    return readings, skipped


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, metavar="FILE")
    args = parser.parse_args(argv)

    key = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if not key:
        print("YOUTUBE_API_KEY is not set. See the top of this file for how to "
              "create one.")
        return 1

    from app import app

    with app.app_context():
        handles = [row["handle"] for row in worklist("youtube")]

    readings, skipped = collect(handles, key)
    with open(args.out, "w") as handle:
        json.dump(readings, handle, indent=1)

    found = sum(1 for r in readings if "followers" in r)
    print(f"{len(handles)} channels: {found} read, "
          f"{len(readings) - found} not found, {len(skipped)} skipped")
    for name, why in skipped:
        print(f"    skipped @{name}: {why}")
    print(f"Readings written to {args.out}. Load them with refresh_followers.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
