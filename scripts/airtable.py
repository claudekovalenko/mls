#!/usr/bin/env python3
"""Minimal Airtable REST client + the schema this project expects.

Airtable is the database of record. Everything else (the PWA, the search
worker) reads and writes through this shape, so the schema lives in one
place instead of being restated in every file.

Auth is a Personal Access Token (AIRTABLE_TOKEN) scoped to one base
(AIRTABLE_BASE_ID). The token needs data.records:read and
data.records:write on that base.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.airtable.com/v0"
TIMEOUT = 30

# Table names, matched by name so the base can be created by hand or by API.
TABLE_CRITERIA = "Search Criteria"
TABLE_HOUSES = "Houses"

# The schema, kept here so setup docs and code can't drift apart.
SCHEMA = {
    TABLE_CRITERIA: [
        ("Name", "singleLineText"),          # e.g. "Atlanta flips"
        ("Active", "checkbox"),              # only Active rows are searched
        ("Market", "singleLineText"),        # Atlanta / Los Angeles / ...
        ("City", "singleLineText"),
        ("State", "singleLineText"),
        ("Min Price", "number"),
        ("Max Price", "number"),
        ("Min Beds", "number"),
        ("Min Baths", "number"),
        ("Min Sqft", "number"),
        ("Property Types", "singleLineText"),  # comma-separated
        ("Keywords", "singleLineText"),        # comma-separated, any-match
        ("Strategy", "singleSelect"),          # Flip / BRRRR / Either
        ("Target Flip Profit", "number"),      # qualification thresholds
        ("Target Cash on Cash", "number"),     # percent, e.g. 8
        ("Target One Percent", "number"),      # percent, e.g. 1
        ("Rehab Cost Per Sqft", "number"),     # used to estimate rehab
        ("Notes", "multilineText"),
    ],
    TABLE_HOUSES: [
        ("Address", "singleLineText"),
        ("Market", "singleLineText"),
        ("Status", "singleSelect"),   # New / Interested / Touring / Offer / Rejected ...
        ("Price", "number"),
        ("Beds", "number"),
        ("Baths", "number"),
        ("Sqft", "number"),
        ("Rehab Cost", "number"),
        ("ARV", "number"),
        ("Rent Estimate", "number"),
        ("Flip Profit", "number"),
        ("Cash on Cash", "number"),
        ("One Percent", "number"),
        ("Flip Verdict", "singleSelect"),
        ("BRRRR Verdict", "singleSelect"),
        ("Best Strategy", "singleLineText"),
        ("Qualified", "checkbox"),    # cleared every target on its criteria row
        ("Listing URL", "url"),
        ("Photo URL", "url"),
        ("Source", "singleLineText"),
        ("Notes", "multilineText"),
        ("Date Added", "date"),
    ],
}


class AirtableError(RuntimeError):
    pass


class Airtable:
    def __init__(self, token=None, base_id=None):
        self.token = token or os.environ.get("AIRTABLE_TOKEN")
        self.base_id = base_id or os.environ.get("AIRTABLE_BASE_ID")
        if not self.token or not self.base_id:
            raise AirtableError(
                "AIRTABLE_TOKEN and AIRTABLE_BASE_ID must both be set."
            )

    def _request(self, method, path, payload=None, query=None):
        url = f"{API_ROOT}/{self.base_id}/{urllib.parse.quote(path)}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise AirtableError(f"{method} {path} -> HTTP {exc.code}: {body}") from exc

    def list_records(self, table, formula=None, max_records=None):
        """All records, following pagination so a large table isn't silently
        truncated at Airtable's 100-per-page default."""
        records, offset = [], None
        while True:
            query = {"pageSize": 100}
            if formula:
                query["filterByFormula"] = formula
            if offset:
                query["offset"] = offset
            payload = self._request("GET", table, query=query)
            records.extend(payload.get("records", []))
            offset = payload.get("offset")
            if not offset or (max_records and len(records) >= max_records):
                break
        return records[:max_records] if max_records else records

    def create_records(self, table, fields_list):
        """Airtable caps writes at 10 records per request, so batch."""
        created = []
        for i in range(0, len(fields_list), 10):
            batch = [{"fields": f} for f in fields_list[i:i + 10]]
            payload = self._request("POST", table, {"records": batch, "typecast": True})
            created.extend(payload.get("records", []))
        return created

    def update_records(self, table, updates):
        """updates: [{"id": rec_id, "fields": {...}}, ...]"""
        updated = []
        for i in range(0, len(updates), 10):
            batch = updates[i:i + 10]
            payload = self._request("PATCH", table, {"records": batch, "typecast": True})
            updated.extend(payload.get("records", []))
        return updated


def parse_list_field(value):
    """Comma-separated Airtable text -> list of trimmed strings."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]
