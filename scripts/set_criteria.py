#!/usr/bin/env python3
"""Edit a Search Criteria row from a workflow instead of the Airtable UI.

The Airtable connector drops often enough that changing a search should not
depend on it, and the Active checkbox in particular is the only thing the
worker reads -- a row's Notes saying "deactivated" while Active stays ticked
is a silent, expensive mistake (the Atlanta row cost a call a run for weeks
that way).

Costs nothing: Airtable reads and one update, no listing API calls.

  CRITERIA_NAME   the row to change; matched case-insensitively, and a
                  partial match is accepted when it is unambiguous
  FIELDS          JSON object of field -> value, e.g.
                  {"Zip Codes": "30068, 30062", "Max Price": 500000}
                  A null value clears the field.
  CREATE          "1" to create the row when no match exists. Off by default,
                  so a typo'd name fails loudly instead of quietly adding a
                  second nearly-identical search that then costs API calls
                  every run.
"""
import json
import os
import sys

from airtable import SCHEMA
from db import connect, TABLE_CRITERIA


def find_row(records, wanted):
    """Exact match first, then an unambiguous partial. Never guesses between
    two candidates -- editing the wrong search is silent and confusing."""
    wanted = wanted.strip().lower()
    exact = [r for r in records
             if (r.get("fields", {}).get("Name") or "").strip().lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = [r for r in records
               if wanted in (r.get("fields", {}).get("Name") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SystemExit(f"::error::No Search Criteria row matches {wanted!r}.")
    names = ", ".join(repr(r["fields"].get("Name")) for r in partial)
    raise SystemExit(f"::error::{wanted!r} matches several rows: {names}. "
                     f"Use the full name.")


def check_fields(fields):
    """Reject a field the schema doesn't define.

    Airtable with typecast on will happily accept an unknown name and drop
    it, so a typo'd 'Zip Code' would report success and change nothing.
    """
    known = {name for name, _ in SCHEMA[TABLE_CRITERIA]}
    unknown = [k for k in fields if k not in known]
    if unknown:
        raise SystemExit(
            f"::error::Not Search Criteria fields: {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(sorted(known))}")


def main():
    name = os.environ.get("CRITERIA_NAME", "").strip()
    if not name:
        raise SystemExit("::error::CRITERIA_NAME must be set.")
    raw = os.environ.get("FIELDS", "").strip()
    if not raw:
        raise SystemExit("::error::FIELDS must be a JSON object of field -> value.")
    try:
        fields = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"::error::FIELDS is not valid JSON: {exc}")
    if not isinstance(fields, dict) or not fields:
        raise SystemExit("::error::FIELDS must be a non-empty JSON object.")
    check_fields(fields)

    at = connect()
    records = at.list_records(TABLE_CRITERIA)

    if os.environ.get("CREATE") == "1":
        existing = [r for r in records
                    if (r.get("fields", {}).get("Name") or "").strip().lower()
                    == name.strip().lower()]
        if not existing:
            # Name comes from CRITERIA_NAME so the row is findable by the same
            # string that created it, and cannot disagree with FIELDS.
            created = at.create_records(TABLE_CRITERIA, [{**fields, "Name": name}])
            print(f"Created {name!r} with {len(fields) + 1} field(s).")
            return 0 if created else 1
        row = existing[0]
        print(f"{name!r} already exists; updating it rather than adding a second.")
    else:
        row = find_row(records, name)
    before = row.get("fields", {})

    print(f"{before.get('Name')!r}:")
    changed = {}
    for key, value in fields.items():
        was = before.get(key)
        if was == value:
            print(f"  {key}: already {value!r}")
            continue
        print(f"  {key}: {was!r} -> {value!r}")
        changed[key] = value
    if not changed:
        print("Nothing to change.")
        return 0

    at.update_records(TABLE_CRITERIA, [{"id": row["id"], "fields": changed}])
    print(f"Updated {len(changed)} field(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
