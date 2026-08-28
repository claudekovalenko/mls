#!/usr/bin/env python3
"""Copy Airtable's contents into Supabase.

Reads from Airtable, writes to Supabase, changes nothing in Airtable. That
asymmetry is the point: run it, check the result, and if anything is wrong
the original is untouched and DB_BACKEND still says airtable.

Idempotent. Rows are upserted on their natural key -- criteria on name,
houses on address, recipients on email -- so running it twice does not
duplicate, and running it again after a few days catches whatever the
searches added in between. That is what makes a gradual switch possible
rather than a single cutover with no way back.

  DRY_RUN=1   report what would be written, write nothing

Both sets of credentials must be present:
  AIRTABLE_TOKEN, AIRTABLE_BASE_ID
  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import os
import sys

from airtable import Airtable, TABLE_CRITERIA, TABLE_HOUSES, TABLE_RECIPIENTS
from supabase_db import Supabase, to_column

# Natural key per table: what makes two rows the same row. Airtable has no
# uniqueness at all, so these are also what the Postgres unique indexes are
# built on.
CONFLICT_KEY = {
    TABLE_CRITERIA: "name",
    TABLE_HOUSES: "address",
    TABLE_RECIPIENTS: "email",
}

# Airtable computes these; Postgres does too, from the trigger and defaults.
# Copying them across would be copying a value that is about to be recomputed.
SKIP_FIELDS = {"updated_at", "created_at"}


def rows_for(records, table):
    """Airtable records -> Postgres rows, keyed the way the schema expects.

    Drops rows with no natural key rather than inventing one: a criteria row
    with no name or a house with no address cannot be upserted, and silently
    giving it a placeholder would create a row nobody can find again.
    """
    key = CONFLICT_KEY[table]
    out, skipped = [], []
    for rec in records:
        fields = rec.get("fields", {})
        row = {to_column(k): v for k, v in fields.items()
               if to_column(k) not in SKIP_FIELDS and v is not None}
        if not row.get(key):
            skipped.append(rec.get("id"))
            continue
        out.append(row)
    return out, skipped


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    src = Airtable()
    dst = None if dry_run else Supabase()
    if dry_run:
        # Still construct it, so a missing credential fails here rather than
        # after a successful-looking dry run.
        Supabase()

    total = 0
    for table in (TABLE_CRITERIA, TABLE_HOUSES, TABLE_RECIPIENTS):
        try:
            records = src.list_records(table)
        except Exception as exc:
            print(f"::warning::Could not read {table} from Airtable ({exc}); skipping.")
            continue
        rows, skipped = rows_for(records, table)
        for rid in skipped:
            print(f"::warning::{table}: skipping {rid} -- no "
                  f"{CONFLICT_KEY[table]}, nothing to key an upsert on.")
        print(f"{table}: {len(rows)} row(s) to write"
              f"{f', {len(skipped)} skipped' if skipped else ''}")
        if dry_run or not rows:
            continue
        written = dst.upsert_records(table, rows, on_conflict=CONFLICT_KEY[table])
        print(f"  wrote {len(written)}")
        total += len(written)

    if dry_run:
        print("\nDRY_RUN=1 -- nothing written. Airtable is never modified either way.")
    else:
        print(f"\nWrote {total} row(s). Airtable is unchanged; set DB_BACKEND=supabase "
              f"when you're ready to read from Postgres.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
