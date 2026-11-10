#!/usr/bin/env python3
"""Download creators' profile photos into data/avatars.

The photo used is the one a creator set on their own Readwise Books profile.
That beats scraping a social profile on every count that matters here: it is
the picture they chose for this product, it sits at a stable public URL, and
fetching it needs no login — where Instagram and TikTok both wall profile
pages and hand out image URLs that expire within days.

creatorwise still has no production database access, so which image belongs
to which creator arrives as a JSON file of {email: storage key}, produced by
somebody who can query it:

    SELECT lower(u.email) AS email, b.profile_image
    FROM auth_user u
    JOIN main_profile p ON p.user_id = u.id
    JOIN bookwise_bookwiseprofile b
      ON b.profile_id = p.id AND b.deleted_at IS NULL
    WHERE b.profile_image <> ''

    python3 tools/fetch_avatars.py --keys keys.json
    python3 tools/fetch_avatars.py --keys keys.json --apply

Dry run unless --apply. Re-running refreshes photos in place; --missing-only
skips creators who already have one.
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import avatars  # noqa: E402
from models import Creator, CreatorEmail, db  # noqa: E402

DEFAULT_BASE = "https://readwise-assets.s3.amazonaws.com/media/"


# Where the operating system keeps its CA bundle. Some Python builds (Homebrew
# and python.org on macOS among them) ship pointing at a bundle that isn't
# there, and every HTTPS request then fails verification. The answer to that is
# to find the real bundle, never to switch verification off.
_SYSTEM_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt")


def _ssl_context():
    context = ssl.create_default_context()
    for bundle in _SYSTEM_BUNDLES:
        if os.path.isfile(bundle):
            context.load_verify_locations(cafile=bundle)
            break
    return context


def http_fetch(url, timeout=20):
    """GET a URL and return its bytes. Raises OSError on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "creatorwise"})
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response:
            return response.read(avatars.MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise OSError(f"HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise OSError(str(error.reason)) from error


def plan(keys):
    """Tie each address to a creator. Returns a dict of lists.

    An address two creators share decides nothing — the photo belongs to one
    account and the address can't say whose — so it is reported, not assigned.
    """
    holders = {}
    for row in CreatorEmail.query.all():
        holders.setdefault(row.address.strip().lower(), set()).add(row.creator_id)
    for creator in Creator.query.all():
        if creator.email:
            holders.setdefault(creator.email.strip().lower(), set()).add(creator.id)

    result = {"fetch": [], "unknown": [], "shared": [], "duplicate": []}
    claimed = {}
    for address, key in sorted(keys.items()):
        address = address.strip().lower()
        ids = holders.get(address)
        if not ids:
            result["unknown"].append(address)
            continue
        if len(ids) > 1:
            result["shared"].append(address)
            continue
        creator_id = next(iter(ids))
        if creator_id in claimed:
            # Two of one creator's addresses each have an account with a
            # photo. Keep the first; say so.
            result["duplicate"].append((address, claimed[creator_id]))
            continue
        claimed[creator_id] = address
        creator = db.session.get(Creator, creator_id)
        result["fetch"].append((creator_id, creator.name, key))
    return result


def apply(result, folder, base=DEFAULT_BASE, fetch=http_fetch, missing_only=False):
    """Download and store. Returns (saved, skipped, failures)."""
    have = avatars.ids(folder) if missing_only else set()
    saved, skipped, failures = 0, 0, []
    for creator_id, name, key in result["fetch"]:
        if creator_id in have:
            skipped += 1
            continue
        url = base.rstrip("/") + "/" + key.lstrip("/")
        try:
            avatars.save(folder, creator_id, fetch(url))
            saved += 1
        except (OSError, ValueError) as error:
            failures.append((name, str(error)))
    return saved, skipped, failures


def render(result, total_creators, already):
    out = []
    add = out.append
    add(f"{len(result['fetch'])} photo(s) to fetch for {total_creators} creators "
        f"({already} already have one on disk)")
    if result["shared"]:
        add("")
        add(f"SHARED ADDRESS ({len(result['shared'])}) — skipped, nobody assigned:")
        for address in result["shared"]:
            add(f"    {address}")
    if result["duplicate"]:
        add("")
        add(f"SECOND ACCOUNT ({len(result['duplicate'])}) — creator already "
            f"has a photo from another address:")
        for address, first in result["duplicate"]:
            add(f"    {address} (using {first})")
    if result["unknown"]:
        add("")
        add(f"NO CREATOR WITH THAT ADDRESS ({len(result['unknown'])}) — skipped:")
        for address in result["unknown"]:
            add(f"    {address}")
    return "\n".join(out)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys", required=True, metavar="FILE",
                        help='JSON: {"email": "storage/key.jpg", ...}')
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="URL prefix the storage keys hang off")
    parser.add_argument("--missing-only", action="store_true",
                        help="skip creators who already have a photo")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    with open(args.keys) as handle:
        keys = json.load(handle)

    from app import app

    with app.app_context():
        folder = avatars.directory(app.config["SQLALCHEMY_DATABASE_URI"])
        result = plan(keys)
        print(render(result, Creator.query.count(), len(avatars.ids(folder))))

        if not args.apply:
            print("\n(dry run — nothing downloaded. Re-run with --apply.)")
            return 0

        saved, skipped, failures = apply(
            result, folder, base=args.base, missing_only=args.missing_only
        )
        print(f"\nDone: {saved} saved, {skipped} skipped, {len(failures)} failed")
        for name, why in failures:
            print(f"    FAILED {name}: {why}")

        without = Creator.query.count() - len(avatars.ids(folder))
        print(f"{without} creator(s) still have no photo.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
