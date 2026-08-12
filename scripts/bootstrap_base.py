#!/usr/bin/env python3
"""Build the Airtable base this project expects, from SCHEMA.

Creating twenty-odd fields by hand across two tables is tedious and easy to
get subtly wrong -- one mistyped field name and the worker writes into
nothing. This does it from the same SCHEMA everything else reads.

You should not have to go find a base id -- give it a token and it works it
out, and prints the id at the end for the app and the repo secrets.

  # make a blank base named "House Finder" in Airtable, grant the token on it
  AIRTABLE_TOKEN=pat... python bootstrap_base.py

  # or let it create the base (workspace id is the wsp... in the Airtable URL)
  AIRTABLE_TOKEN=pat... AIRTABLE_WORKSPACE_ID=wsp... python bootstrap_base.py

  # or name the base outright
  AIRTABLE_TOKEN=pat... AIRTABLE_BASE_ID=app... python bootstrap_base.py

If it can't identify a single base it lists every one the token can see, with
ids, rather than picking for you.

Token scopes: schema.bases:write (plus schema.bases:read). Creating a base
also needs the token granted on the whole workspace rather than one base.

Safe to re-run. Existing tables are left alone except that missing fields are
added -- nothing is renamed, retyped, or deleted, so a column you added
yourself will survive.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from airtable import SCHEMA, TABLE_CRITERIA, field_spec

META_ROOT = "https://api.airtable.com/v0/meta"
BASE_NAME = "House Finder"
TIMEOUT = 30


def call(method, url, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"{method} {url} -> HTTP {exc.code}: {body}")


def create_base(token, workspace_id):
    # Airtable requires at least one table at creation time, so the first one
    # is built here and the rest of the work is identical to the fill-in path.
    first = TABLE_CRITERIA
    payload = {
        "workspaceId": workspace_id,
        "name": BASE_NAME,
        "tables": [{
            "name": first,
            "fields": [field_spec(n, k) for n, k in SCHEMA[first]],
        }],
    }
    base = call("POST", f"{META_ROOT}/bases", payload, token)
    print(f"Created base {base['id']} with table {first!r}")
    # Returned rather than re-read: we know exactly what was just built, and
    # relying on the table listing to reflect it immediately would risk
    # creating a duplicate table if that read lags.
    return base["id"], first


def find_base(token):
    """Look the base up by name so nobody has to dig an id out of a URL.

    The token can already see every base it's been granted, so asking the
    human to transcribe an app... id is busywork. Only an exact BASE_NAME
    match is used automatically -- guessing at a base and then writing tables
    into the wrong one would be much worse than stopping.
    """
    bases = call("GET", f"{META_ROOT}/bases", token=token).get("bases", [])
    if not bases:
        raise SystemExit(
            "This token can't see any bases.\n"
            "Either create one in Airtable and grant the token access to it,\n"
            "or set AIRTABLE_WORKSPACE_ID and this script will create it."
        )
    match = [b for b in bases if b["name"] == BASE_NAME]
    if len(match) == 1:
        print(f"Found base {match[0]['name']!r} -> {match[0]['id']}")
        return match[0]["id"]

    if len(match) > 1:
        print(f"More than one base is called {BASE_NAME!r}. Pick one:")
        listing = match
    else:
        print(f"No base called {BASE_NAME!r}. Bases this token can see:")
        listing = bases
    for b in listing:
        print(f"  {b['id']}   {b['name']}")
    raise SystemExit(
        f"\nRe-run with the one you want:  AIRTABLE_BASE_ID=<id from above>\n"
        f"(or rename a base to {BASE_NAME!r} and re-run)"
    )


def main():
    token = os.environ.get("AIRTABLE_TOKEN")
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    workspace_id = os.environ.get("AIRTABLE_WORKSPACE_ID")
    if not token:
        raise SystemExit("Set AIRTABLE_TOKEN.")
    just_created = None
    if not base_id:
        if workspace_id:
            base_id, just_created = create_base(token, workspace_id)
        else:
            base_id = find_base(token)

    tables_url = f"{META_ROOT}/bases/{base_id}/tables"
    existing = {t["name"]: t for t in call("GET", tables_url, token=token).get("tables", [])}

    for table, fields in SCHEMA.items():
        if table == just_created:
            print(f"Table {table!r} ready (created with the base)")
            continue
        if table not in existing:
            # The first schema field becomes the primary column; Airtable will
            # not accept a checkbox or number there.
            call("POST", tables_url, {
                "name": table,
                "fields": [field_spec(n, k) for n, k in fields],
            }, token)
            print(f"Created table {table!r} with {len(fields)} fields")
            continue

        table_id = existing[table]["id"]
        have = {f["name"] for f in existing[table].get("fields", [])}
        missing = [(n, k) for n, k in fields if n not in have]
        for name, kind in missing:
            call("POST", f"{tables_url}/{table_id}/fields", field_spec(name, kind), token)
            print(f"  + {table}.{name}")
        print(f"Table {table!r} ready ({len(missing)} field(s) added)")

    print(f"\nDone. Base ID: {base_id}")
    print("Paste that and your token into the app, and set them as repo secrets.")


if __name__ == "__main__":
    sys.exit(main())
