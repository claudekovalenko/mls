#!/usr/bin/env python3
"""Supabase (Postgres) client.

Exposes list_records, create_records, update_records, delete_records and the
{"id": ..., "fields": {...}} record shape, so callers name fields ("Days on
Market") and never columns (days_on_market). FIELD_TO_COLUMN does that
translation in one place rather than in every query.

Auth:
  SUPABASE_URL          https://<project>.supabase.co
  SUPABASE_SERVICE_KEY  service_role key -- bypasses RLS, so it lives only
                        in GitHub secrets and never in the browser
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30

TABLE_CRITERIA = "search_criteria"
TABLE_HOUSES = "houses"
TABLE_RECIPIENTS = "recipients"

# The logical table names callers use, mapped to their Postgres tables.
TABLE_ALIASES = {
    "Search Criteria": TABLE_CRITERIA,
    "Houses": TABLE_HOUSES,
    "Recipients": TABLE_RECIPIENTS,
}


def to_column(field):
    """"Days on Market" -> days_on_market. Already-snake names pass through,
    so a caller may use either spelling."""
    return field.strip().lower().replace(" ", "_").replace("-", "_")


def _build_column_to_field():
    """Every field name the schema declares, keyed by its column name.

    Derived rather than hand-listed, because title-casing is wrong for more
    names than it is obvious about -- "Days on Market" round-trips to "Days
    On Market", "Cash on Cash" to "Cash On Cash", "Listing URL" to "Listing
    Url". Each of those is a silently missing value at every call site that
    spells it correctly, not an error anyone would see. A hand-written list
    would fix today's five and miss the sixth field somebody adds next month.
    """
    try:
        from schema import SCHEMA
    except ImportError:      # standalone use, e.g. a one-off script
        return {}
    return {to_column(name): name
            for fields in SCHEMA.values() for name, _ in fields}


COLUMN_TO_FIELD = _build_column_to_field()


def to_field(column):
    """days_on_market -> "Days on Market", exactly as the schema spells it."""
    return COLUMN_TO_FIELD.get(column, column.replace("_", " ").title())


class SupabaseError(RuntimeError):
    pass


class Supabase:
    def __init__(self, url=None, key=None):
        self.url = (url or os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY")
        if not (self.url and self.key):
            raise SupabaseError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must both be set. The "
                "service_role key belongs in GitHub secrets only -- it bypasses "
                "row level security and must never reach the browser."
            )

    def _request(self, method, table, payload=None, query="", prefer=None):
        table = TABLE_ALIASES.get(table, table)
        url = f"{self.url}/rest/v1/{table}"
        if query:
            url += "?" + query
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("apikey", self.key)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Content-Type", "application/json")
        # Without this PostgREST returns 204 and no body, and every caller
        # here wants the rows back.
        req.add_header("Prefer", prefer or "return=representation")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read()
                return json.loads(body) if body else []
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise SupabaseError(f"{method} {table} -> HTTP {exc.code}: {detail}") from exc

    # ---------------------------------------------------------- read/write
    def list_records(self, table, formula=None, max_records=None):
        """All rows, in the {"id", "fields"} shape callers expect.

        `formula` accepts the one filter this project actually uses --
        "{Active}" -- and rejects anything else rather than silently
        returning every row, which is how a filter quietly stops filtering.
        """
        query = "select=*"
        if formula:
            if formula.strip() not in ("{Active}", "{active}"):
                raise SupabaseError(
                    f"Unsupported filter {formula!r}. This client understands "
                    f"'{{Active}}' only; use PostgREST query syntax directly "
                    f"for anything else."
                )
            query += "&active=is.true"
        if max_records:
            query += f"&limit={int(max_records)}"
        rows = self._request("GET", table, query=query)
        return [{"id": r.get("id"), "fields": self._as_fields(r)} for r in rows]

    @staticmethod
    def _as_fields(row):
        """Postgres row -> the field-name shape callers expect, dropping nulls
        so `.get(...)` truthiness keeps working."""
        return {to_field(k): v for k, v in row.items()
                if k != "id" and v is not None}

    @staticmethod
    def _as_row(fields):
        return {to_column(k): v for k, v in fields.items()}

    def create_records(self, table, fields_list):
        if not fields_list:
            return []
        rows = self._request("POST", table, [self._as_row(f) for f in fields_list])
        return [{"id": r.get("id"), "fields": self._as_fields(r)} for r in rows]

    def update_records(self, table, updates):
        out = []
        for item in updates:
            rid = item["id"]
            rows = self._request("PATCH", table, self._as_row(item["fields"]),
                                 query=f"id=eq.{urllib.parse.quote(str(rid))}")
            out.extend({"id": r.get("id"), "fields": self._as_fields(r)} for r in rows)
        return out

    def delete_records(self, table, record_ids):
        if not record_ids:
            return []
        quoted = ",".join(f'"{urllib.parse.quote(str(r))}"' for r in record_ids)
        rows = self._request("DELETE", table, query=f"id=in.({quoted})")
        return [{"id": r.get("id")} for r in rows]

    def upsert_records(self, table, fields_list, on_conflict):
        """Insert-or-update in one round trip.

        The search worker reads every house, matches on address in Python,
        then splits into creates and updates. Postgres does that server-side
        and atomically, so two runs overlapping cannot produce a duplicate.
        """
        if not fields_list:
            return []
        rows = self._request(
            "POST", table, [self._as_row(f) for f in fields_list],
            query=f"on_conflict={on_conflict}",
            prefer="return=representation,resolution=merge-duplicates")
        return [{"id": r.get("id"), "fields": self._as_fields(r)} for r in rows]


def parse_list_field(value):
    """Comma-separated text -> list of trimmed strings."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]
