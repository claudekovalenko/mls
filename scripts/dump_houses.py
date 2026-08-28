#!/usr/bin/env python3
"""Print the Houses table as JSON, for reading out of a workflow log.

The Airtable token lives in repo secrets and only the runner holds it, so
there is otherwise no way to see what the table actually contains without
opening Airtable by hand. This prints the fields the digest renders, which
makes "what is in the app right now" answerable from a run log.

Costs nothing: one Airtable read, no listing API calls.

Notes stay in Airtable -- only the scored fields are printed.
"""
import json
import sys

from db import connect, TABLE_HOUSES

FIELDS = ("Address", "Status", "Price", "Beds", "Baths", "Sqft", "Lot Sqft",
          "Price Per Sqft", "Value Signals", "Flip Verdict", "BRRRR Verdict",
          "Best Strategy", "Qualified", "Date Added", "Property Type",
          "Year Built", "Days on Market", "Price Cut")


def main():
    at = connect()
    rows = []
    for rec in at.list_records(TABLE_HOUSES):
        f = rec.get("fields", {})
        rows.append({k: f[k] for k in FIELDS if k in f})
    # Most signals first: that is the ranking the digest uses, so a glance at
    # the top of this dump is a glance at the top of the sheet.
    rows.sort(key=lambda r: -len((r.get("Value Signals") or "").split(",")))

    print("HOUSES_JSON_BEGIN")
    print(json.dumps(rows, indent=1))
    print("HOUSES_JSON_END")
    print(f"{len(rows)} house(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
