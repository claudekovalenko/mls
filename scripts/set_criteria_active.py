#!/usr/bin/env python3
"""Turn a Search Criteria row on or off by name.

The Active checkbox is the only thing the worker reads when deciding what to
search, and it is easy for a row's Notes to say one thing while the checkbox
says another -- the Atlanta row read "Deactivated, not deleted" in its Notes
while Active was still ticked, and kept costing a call and pulling metro-wide
houses every run.

This exists because the Airtable connector is unreliable from a chat session
and editing a checkbox should not require one. Costs nothing: Airtable reads
and one update, no listing API calls.

  CRITERIA_NAME   the row to change; matched case-insensitively, and a
                  partial match is accepted when it is unambiguous
  ACTIVE          "1" to activate, "0" to deactivate
"""
import os
import sys

from airtable import Airtable, TABLE_CRITERIA


def find_row(records, wanted):
    """Exact match first, then an unambiguous partial. Never guesses between
    two candidates -- deactivating the wrong search is silent and confusing."""
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


def main():
    name = os.environ.get("CRITERIA_NAME", "").strip()
    if not name:
        raise SystemExit("::error::CRITERIA_NAME must be set.")
    active = os.environ.get("ACTIVE") == "1"

    at = Airtable()
    records = at.list_records(TABLE_CRITERIA)
    row = find_row(records, name)
    fields = row.get("fields", {})
    was = bool(fields.get("Active"))

    print(f"{fields.get('Name')!r}: Active {was} -> {active}")
    if was == active:
        print("Already in that state; nothing to change.")
        return 0

    at.update_records(TABLE_CRITERIA, [{"id": row["id"], "fields": {"Active": active}}])
    print("Updated." if active else
          "Updated. The row is kept, not deleted -- re-run with ACTIVE=1 to restore it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
