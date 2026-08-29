#!/usr/bin/env python3
"""Do the four places that declare the schema still agree?

The field names live in four files that cannot import each other: the Python
schema, the SQL that builds the tables, and the JavaScript that maps Postgres
columns back to field names in the browser. Drift between them is silent --
a house is written with a column nobody reads, or the app shows a blank where
a number should be -- so it gets checked rather than remembered.

Run: python check_schema_sync.py
"""
import pathlib
import re
import sys

from airtable import SCHEMA, TABLE_CRITERIA, TABLE_HOUSES
from supabase_db import to_column

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = ROOT / "docs" / "app.js"
SQL = ROOT / "supabase" / "schema.sql"

# Only the tables the browser reads. Recipients is worker-side only, so its
# fields are deliberately not in the app's list.
APP_TABLES = (TABLE_CRITERIA, TABLE_HOUSES)


def js_field_names():
    text = APP_JS.read_text()
    block = re.search(r"const FIELD_NAMES = \[(.*?)\];", text, re.S)
    if not block:
        raise SystemExit("FIELD_NAMES not found in docs/app.js")
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def sql_columns(table):
    text = SQL.read_text()
    block = re.search(rf"create table if not exists {table} \((.*?)\n\);", text, re.S)
    if not block:
        raise SystemExit(f"no create table for {table} in supabase/schema.sql")
    cols, depth = set(), 0
    for line in block.group(1).splitlines():
        stripped = line.strip()
        # A column definition starts at the top level. A multi-line check
        # constraint's continuation lines do not, and reading them as column
        # names produces nonsense like "'Rejected'))".
        starts_column = depth == 0
        depth += line.count("(") - line.count(")")
        if not stripped or stripped.startswith("--") or not starts_column:
            continue
        name = stripped.split()[0]
        if name in ("id", "created_at", "updated_at", "unique", "primary",
                    "constraint", "check", "foreign"):
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            continue
        cols.add(name)
    return cols


def main():
    problems = []

    wanted = {name for table in APP_TABLES for name, _ in SCHEMA[table]}
    have = js_field_names()
    for missing in sorted(wanted - have):
        problems.append(f"docs/app.js FIELD_NAMES is missing {missing!r} -- "
                        f"the app will show nothing for that column")
    # Extra names in the JS list are harmless but usually mean a rename went
    # half-done, so they are worth saying out loud without failing the run.
    extra = have - wanted - {"Email"}
    for name in sorted(extra):
        print(f"note: docs/app.js lists {name!r}, which no table declares")

    for table, sql_name in ((TABLE_CRITERIA, "search_criteria"),
                            (TABLE_HOUSES, "houses")):
        declared = {to_column(name) for name, _ in SCHEMA[table]}
        actual = sql_columns(sql_name)
        for missing in sorted(declared - actual):
            problems.append(f"supabase/schema.sql {sql_name} has no column {missing!r}")
        for orphan in sorted(actual - declared):
            problems.append(f"supabase/schema.sql {sql_name} has {orphan!r}, "
                            f"which no field maps to")

    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1
    print(f"Schema in sync: {len(wanted)} app fields, "
          f"{sum(len(SCHEMA[t]) for t in SCHEMA)} declared across {len(SCHEMA)} tables.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
