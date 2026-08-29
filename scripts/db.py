#!/usr/bin/env python3
"""The database, ready to use.

One import for every script. Supabase (Postgres via PostgREST) is the only
backend; Airtable was removed once its data was migrated.

Table names are the logical ones -- "Houses", "Search Criteria" -- and the
client translates them to their Postgres tables, so callers never spell a
column name.
"""
import os

from schema import parse_list_field  # noqa: F401  (re-exported for callers)

TABLE_CRITERIA = "Search Criteria"
TABLE_HOUSES = "Houses"
TABLE_RECIPIENTS = "Recipients"


def connect():
    """Connect, or explain exactly what is missing.

    A worker that cannot reach its database should say which secret is absent
    and where to get it, not raise something generic three frames deep. This
    is the message somebody reads at 8am when the digest did not arrive.
    """
    missing = [name for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
               if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} not set. Add "
            f"{'them' if len(missing) > 1 else 'it'} at GitHub -> Settings -> "
            f"Secrets -> Actions. SUPABASE_URL is the project URL and "
            f"SUPABASE_SERVICE_KEY is the service_role key (not the anon key: "
            f"the worker writes rows). Both are in the Supabase dashboard "
            f"under Settings -> API.")
    from supabase_db import Supabase
    return Supabase()
