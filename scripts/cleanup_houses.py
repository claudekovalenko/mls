#!/usr/bin/env python3
"""Remove houses the current rules would never have written.

The Houses table was filled before two corrections landed: attached housing
(condos, townhouse units, apartments) was being surfaced because it is cheap
per square foot by nature rather than by mispricing, and every row carried a
PASS verdict computed from an ARV that was really just the list price -- an
arithmetic artefact, not a judgement about the house.

Both are fixed going forward. This clears what the old rules left behind so
the app shows only what the current ones would produce.

Costs nothing: Airtable reads and deletes only, no listing API calls.

Two things are never touched, because deleting is irreversible:

  * any house whose Status is something other than New -- once a human has
    marked it Interested, Touring, Offer, anything at all, it is theirs and
    not the worker's to remove.
  * any house with no Value Signals, which is how a hand-added or migrated
    house looks. Only rows the scorer wrote are the scorer's to clean up.

DRY_RUN=1 lists what would go without deleting anything.
"""
import os
import sys

from db import connect, TABLE_HOUSES
from search_worker import is_single_family, looks_attached

PROTECTED_STATUSES = {"Interested", "Touring", "Toured", "Offer",
                      "Under Contract", "Purchased", "Rejected"}

# The highest ceiling anywhere in the brief: a flip is capped at $500k and both
# BRRRR variants well below that. A house above this fits no strategy we have,
# however cheap it is per square foot -- which is exactly how a $1,575,000
# house ended up on the sheet for being 17% under the area median.
BRIEF_MAX_PRICE = 500_000

# A detached house needs land around it. Even over two storeys, and even
# before setbacks, a house whose lot is smaller than this share of its
# finished area cannot be standing on its own -- it is a townhouse or a
# condo. 594 Parkside Village Way is 3,424 sqft of house on a 1,394 sqft
# lot; 128 River Ridge Ln is 2,857 on 1,830. Neither is detached.
#
# Only applied to rows with no Property Type -- the ones written before that
# column existed, where the address is otherwise the only evidence. Anything
# the feed has actually typed is judged on the type, not on arithmetic.
# Deliberately conservative: real Marietta lots in this table run 3-10x the
# finished area, so 0.75 sits far below every genuine detached house here
# and leaves the ambiguous 0.9-1.1 cases alone.
MIN_LOT_TO_SQFT_RATIO = 0.75


def looks_attached_by_lot(f):
    sqft, lot = f.get("Sqft"), f.get("Lot Sqft")
    if not sqft or not lot:
        return False
    return lot < sqft * MIN_LOT_TO_SQFT_RATIO


def reason_to_drop(f):
    """Why this row would not be written today, or None to keep it."""
    if (f.get("Status") or "New") in PROTECTED_STATUSES:
        return None
    if not (f.get("Value Signals") or "").strip():
        return None  # hand-added or migrated; not ours to delete

    # The $500k ceiling and the detached-house rules are the house brief. A
    # multifamily row is a different search with different economics -- a
    # 20-unit complex is priced in millions and is *supposed* to be attached --
    # so none of those rules may reach it. This has to be checked before the
    # single-family rule, not after, or the complex is deleted for being
    # exactly what it was bought to be. Identified by the search that found
    # it, which is the only thing that records what was being hunted.
    if "multifamily" in (f.get("Found By") or "").lower() or f.get("Units"):
        return None

    listing = {"address": f.get("Address") or "",
               "propertyType": f.get("Property Type") or ""}

    # Rows written before Property Type existed carry no type at all, and the
    # whitelist would delete every one of them on no evidence. For those the
    # address is the only signal there is, so they get the old blacklist; rows
    # that do record a type are held to the single-family rule.
    if listing["propertyType"]:
        if not is_single_family(listing):
            return f"not single family ({listing['propertyType']})"
    elif looks_attached(listing):
        return "attached housing (condo/townhouse/unit)"
    elif looks_attached_by_lot(f):
        return (f"attached: {f['Sqft']:,.0f} sqft of house on a "
                f"{f['Lot Sqft']:,.0f} sqft lot")

    price = f.get("Price")
    if price and price > BRIEF_MAX_PRICE:
        return f"${price / 1000:.0f}k is over the ${BRIEF_MAX_PRICE / 1000:.0f}k brief ceiling"

    if f.get("Flip Verdict") == "PASS" and f.get("BRRRR Verdict") == "PASS":
        return "both verdicts PASS"
    return None


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    at = connect()
    records = at.list_records(TABLE_HOUSES)
    print(f"{len(records)} house(s) in the table.")

    drop, keep = [], []
    for rec in records:
        why = reason_to_drop(rec.get("fields", {}))
        (drop if why else keep).append((rec, why))

    for rec, why in drop:
        print(f"  DROP  {rec['fields'].get('Address', '?')}  -- {why}")
    print(f"\nKeeping {len(keep)}, dropping {len(drop)}.")

    if not drop:
        print("Nothing to clean.")
        return 0
    if dry_run:
        print("DRY_RUN=1 -- nothing deleted.")
        return 0

    at.delete_records(TABLE_HOUSES, [rec["id"] for rec, _ in drop])
    print(f"Deleted {len(drop)}. {len(keep)} remain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
