#!/usr/bin/env python3
"""Which database the project talks to.

One import for every script, so switching backends is an environment
variable rather than an edit in six files -- and so switching back is too,
which matters more. A migration you cannot reverse in one setting is a
migration nobody wants to run on a Friday.

  DB_BACKEND=airtable   the default, unchanged behaviour
  DB_BACKEND=supabase   Postgres via PostgREST

Unset, it infers: Supabase when its credentials are present, Airtable
otherwise. So adding the two secrets is enough to move, and removing them is
enough to move back.

Table names are the Airtable ones in both cases. The Supabase client
translates them, so callers never learn which backend they got.
"""
import os

TABLE_CRITERIA = "Search Criteria"
TABLE_HOUSES = "Houses"
TABLE_RECIPIENTS = "Recipients"


def backend_name():
    explicit = (os.environ.get("DB_BACKEND") or "").strip().lower()
    if explicit in ("airtable", "supabase"):
        return explicit
    if explicit:
        raise RuntimeError(
            f"DB_BACKEND={explicit!r} is not a backend. Use 'airtable' or "
            f"'supabase', or leave it unset to infer from the credentials.")
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        return "supabase"
    return "airtable"


def connect():
    """The database, ready to use. Callers do not branch on the backend."""
    name = backend_name()
    if name == "supabase":
        from supabase_db import Supabase
        return Supabase()
    from airtable import Airtable
    return Airtable()


def parse_list_field(value):
    """Comma-separated text -> list of trimmed strings.

    Identical in both clients, so it lives here and neither backend owns it.
    """
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]
