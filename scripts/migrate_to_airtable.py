#!/usr/bin/env python3
"""One-time migration: houses.json -> the Airtable Houses table.

Run once after the base exists. Idempotent -- it skips any house whose
address is already present, so re-running won't duplicate.
"""
import json
import sys
from datetime import date
from pathlib import Path

from airtable import Airtable, TABLE_HOUSES
import deals

HOUSES_PATH = Path(__file__).resolve().parent.parent / "houses.json"

STATUS_MAP = {
    "Interested": "Interested",
    "Touring Scheduled": "Touring",
    "Toured": "Toured",
    "Offer Made": "Offer",
    "Under Contract": "Under Contract",
    "Purchased": "Purchased",
    "Rejected": "Rejected",
}


def main():
    if not HOUSES_PATH.exists():
        print("No houses.json to migrate.")
        return 0
    houses = json.loads(HOUSES_PATH.read_text() or "[]")
    if not houses:
        print("houses.json is empty, nothing to migrate.")
        return 0

    at = Airtable()
    existing = {
        str(r.get("fields", {}).get("Address", "")).strip().lower()
        for r in at.list_records(TABLE_HOUSES)
    }

    rows = []
    for h in houses:
        address = (h.get("address") or "").strip()
        if not address or address.lower() in existing:
            print(f"  skip (already there): {address}")
            continue

        verdict = deals.qualify(h.get("price"), h.get("rehabCost"), h.get("arv"), h.get("rentEstimate"))
        m = verdict["metrics"]
        rows.append({
            "Address": address,
            "Market": h.get("market") or "Atlanta",
            "Status": STATUS_MAP.get(h.get("status"), "Interested"),
            "Price": h.get("price"),
            "Beds": h.get("beds"),
            "Baths": h.get("baths"),
            "Sqft": h.get("sqft"),
            "Rehab Cost": h.get("rehabCost"),
            "ARV": h.get("arv"),
            "Rent Estimate": h.get("rentEstimate"),
            "Flip Profit": round(m["flipProfit"]) if m["flipProfit"] is not None else None,
            "Cash on Cash": round(m["cashOnCash"] * 100, 1) if m["cashOnCash"] is not None else None,
            "One Percent": round(m["onePercentRatio"] * 100, 2) if m["onePercentRatio"] is not None else None,
            "Flip Verdict": verdict["flipVerdict"],
            "BRRRR Verdict": verdict["brrrrVerdict"],
            "Best Strategy": verdict["bestStrategy"] or "",
            "Qualified": verdict["qualified"],
            "Listing URL": h.get("url") or "",
            "Photo URL": h.get("photoUrl") if str(h.get("photoUrl", "")).startswith("http") else "",
            "Source": "migrated",
            "Notes": h.get("notes") or "",
            "Date Added": h.get("dateAdded") or date.today().isoformat(),
        })

    if rows:
        at.create_records(TABLE_HOUSES, rows)
        print(f"Migrated {len(rows)} house(s) into Airtable.")
    else:
        print("Nothing new to migrate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
