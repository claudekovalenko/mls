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
TABLE_RECIPIENTS = "Recipients"

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
        ("Zip Codes", "singleLineText"),       # comma-separated; how "within 10 mi" is expressed
        ("Property Types", "singleLineText"),  # comma-separated
        ("Keywords", "singleLineText"),        # comma-separated, any-match
        ("Must Haves", "singleLineText"),      # comma-separated, ALL required; "/" = alternatives
        ("Strategy", "singleSelect"),          # Flip / BRRRR / Either
        # What kind of building this search is for. Single Family is the
        # default and the rule for every house search; Multifamily turns that
        # gate around rather than off, so a 20-unit complex is admitted and a
        # detached house is not. Blank reads as Single Family, which keeps
        # every existing row behaving exactly as it did.
        ("Property Class", "singleSelect"),
        ("Min Units", "number"),               # multifamily only
        ("Max Price Per Sqft", "number"),      # skip anything above; missing sqft passes
        ("Max All In", "number"),              # price + rehab cap
        ("Target Total Sqft", "number"),       # post-reno goal, informational
        ("Min Baths After Reno", "number"),    # post-reno goal, informational
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
        ("Lot Sqft", "number"),
        ("Price Per Sqft", "number"),
        ("Value Signals", "singleLineText"),  # Basement, ADU potential, FSBO, ...
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
        ("Property Type", "singleLineText"),  # as the feed reported it
        ("Units", "number"),                  # multifamily: unit count
        # Which criteria row found this house. Lets a digest be scoped to one
        # search, which is how the multifamily email stays separate from the
        # house email without a second table.
        ("Found By", "singleLineText"),
        # The observable shadow of "dated, poorly marketed, motivated seller",
        # which no feed states outright but all three of these imply.
        ("Year Built", "number"),
        ("Days on Market", "number"),
        ("Price Cut", "number"),              # percent off the first asking price
        ("Source", "singleLineText"),
        ("Notes", "multilineText"),
        ("Date Added", "date"),
    ],
    # Who gets the digest. This lives in Airtable rather than an EMAIL_TO
    # repo secret because the recipient list is the one piece of config that
    # genuinely changes -- adding a partner, an agent, a lender for one deal --
    # and editing a GitHub secret from a phone to do it is the wrong shape.
    # Credentials (SMTP_USER/SMTP_PASS) stay secrets; addresses are not secrets.
    TABLE_RECIPIENTS: [
        ("Email", "singleLineText"),
        ("Name", "singleLineText"),
        ("Active", "checkbox"),
        ("Notes", "multilineText"),
    ],
}


# Choices for the singleSelect columns. Airtable will not accept a value that
# isn't an existing choice unless typecast is on, so these have to match the
# strings deals.py and app.js actually produce.
SELECT_OPTIONS = {
    "Strategy": ["Flip", "BRRRR", "Either"],
    "Property Class": ["Single Family", "Multifamily", "Any"],
    "Status": ["New", "Interested", "Touring", "Toured", "Offer",
               "Under Contract", "Purchased", "Rejected"],
    "Flip Verdict": ["STRONG", "GOOD", "MARGINAL", "PASS", "NO DATA"],
    "BRRRR Verdict": ["STRONG", "GOOD", "MARGINAL", "PASS", "NO DATA"],
}

# Decimal places per number field. Dollars are whole; ratios and bath counts
# are not, and rounding them to integers in Airtable would quietly destroy the
# distinction between a 7.9% and an 8.4% cash-on-cash.
NUMBER_PRECISION = {
    "Baths": 1, "Min Baths": 1, "Min Baths After Reno": 1,
    "Cash on Cash": 1, "One Percent": 2, "Price Cut": 1,
    "Target Cash on Cash": 1, "Target One Percent": 2,
}


# Airtable rejects a checkbox field created without options ("Active.options
# is missing", HTTP 422), so every checkbox needs an icon and colour even
# though neither carries meaning. Qualified is the one worth spotting in a
# grid at a glance, so it gets the star.
CHECKBOX_STYLE = {
    "Qualified": {"icon": "star", "color": "yellowBright"},
}
CHECKBOX_DEFAULT = {"icon": "check", "color": "greenBright"}


def field_spec(name, kind):
    """SCHEMA entry -> the payload shape Airtable's Meta API expects."""
    spec = {"name": name, "type": kind}
    if kind == "number":
        spec["options"] = {"precision": NUMBER_PRECISION.get(name, 0)}
    elif kind == "singleSelect":
        spec["options"] = {"choices": [{"name": c} for c in SELECT_OPTIONS.get(name, [])]}
    elif kind == "date":
        spec["options"] = {"dateFormat": {"name": "iso"}}
    elif kind == "checkbox":
        spec["options"] = CHECKBOX_STYLE.get(name, CHECKBOX_DEFAULT)
    return spec


class AirtableError(RuntimeError):
    pass


# The base this project uses. A base ID is not a secret -- it identifies a base
# but grants nothing without a token -- so it lives here as a default rather
# than as one more thing to configure by hand before anything works. The
# AIRTABLE_BASE_ID environment variable still wins when set, which is what makes
# a second base (a test copy, a fork) possible without editing code.
DEFAULT_BASE_ID = "appQzhYbA9NV3RWZe"


class Airtable:
    def __init__(self, token=None, base_id=None):
        self.token = token or os.environ.get("AIRTABLE_TOKEN")
        self.base_id = base_id or os.environ.get("AIRTABLE_BASE_ID") or DEFAULT_BASE_ID
        if not self.token:
            raise AirtableError(
                "AIRTABLE_TOKEN must be set (an Airtable Personal Access Token "
                "with data.records:read and data.records:write on this base)."
            )

    def _request(self, method, path, payload=None, query=None, raw_query=None):
        url = f"{API_ROOT}/{self.base_id}/{urllib.parse.quote(path)}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        elif raw_query:
            # Airtable's delete endpoint wants repeated records[] params, which
            # urlencode's doseq cannot express with a single key.
            url += "?" + raw_query
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


    def delete_records(self, table, record_ids):
        """Airtable caps deletes at 10 ids per request, so batch."""
        deleted = []
        for i in range(0, len(record_ids), 10):
            batch = record_ids[i:i + 10]
            query = [("records[]", rid) for rid in batch]
            url_query = urllib.parse.urlencode(query)
            payload = self._request("DELETE", table, query=None, raw_query=url_query)
            deleted.extend(payload.get("records", []))
        return deleted


def parse_list_field(value):
    """Comma-separated Airtable text -> list of trimmed strings."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]
