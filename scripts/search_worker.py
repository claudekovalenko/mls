#!/usr/bin/env python3
"""Continuous search: read criteria from Airtable, find listings, qualify, write back.

Runs on a schedule. For every Active row in the Search Criteria table it
queries the configured listing source, filters to that row's requirements,
runs the flip/BRRRR math, and upserts qualifying houses into the Houses table.

Listing source (LISTINGS_API_TYPE):

  reso  -- a RESO Web API (OData) feed, the standard interface MLSs expose for
           IDX. This is the real answer: full MLS inventory, licensed for this
           use. Requires credentials from the local MLS (FMLS/GAMLS for Atlanta,
           CRMLS for LA).

  rentcast -- RentCast's /listings/sale endpoint. Works today with the existing
           key, but coverage is thinner than an MLS feed and the free tier is
           capped, so it's a stopgap rather than the destination.

Deliberately NOT supported: scraping Zillow/Redfin/Realtor. Their robots.txt
disallows the search paths and they actively block automated clients (verified
in scripts/probe_sources.py). Building around that would be fragile and against
their terms.

Without a source configured the worker exits cleanly having done nothing,
rather than failing the workflow.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date

from airtable import Airtable, TABLE_CRITERIA, TABLE_HOUSES, parse_list_field
import deals

TIMEOUT = 30
MAX_PER_SEARCH = 50


def _get_json(url, headers):
    req = urllib.request.Request(url)
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- RESO / IDX

def _odata_escape(value):
    return str(value).replace("'", "''")


def fetch_reso(criteria, base_url, api_key):
    filters = ["StandardStatus eq 'Active'"]
    if criteria.get("City"):
        filters.append(f"City eq '{_odata_escape(criteria['City'])}'")
    if criteria.get("State"):
        filters.append(f"StateOrProvince eq '{_odata_escape(criteria['State'])}'")
    if criteria.get("Min Price") is not None:
        filters.append(f"ListPrice ge {int(criteria['Min Price'])}")
    if criteria.get("Max Price") is not None:
        filters.append(f"ListPrice le {int(criteria['Max Price'])}")
    if criteria.get("Min Beds") is not None:
        filters.append(f"BedroomsTotal ge {int(criteria['Min Beds'])}")
    if criteria.get("Min Baths") is not None:
        filters.append(f"BathroomsTotalInteger ge {int(criteria['Min Baths'])}")
    if criteria.get("Min Sqft") is not None:
        filters.append(f"LivingArea ge {int(criteria['Min Sqft'])}")

    query = {"$filter": " and ".join(filters), "$top": str(MAX_PER_SEARCH),
             "$orderby": "ListPrice asc"}
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}{urllib.parse.urlencode(query)}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = _get_json(url, headers)
    records = payload.get("value", []) if isinstance(payload, dict) else payload

    out = []
    for r in records:
        media = r.get("Media") or []
        photo = media[0].get("MediaURL", "") if media and isinstance(media[0], dict) else ""
        out.append({
            "address": r.get("UnparsedAddress") or "",
            "price": _num(r.get("ListPrice")),
            "beds": _num(r.get("BedroomsTotal")),
            "baths": _num(r.get("BathroomsTotalInteger")),
            "sqft": _num(r.get("LivingArea")),
            "propertyType": r.get("PropertySubType") or r.get("PropertyType") or "",
            "url": r.get("ListingURL") or "",
            "photoUrl": photo,
            "description": r.get("PublicRemarks") or "",
        })
    return out


# ------------------------------------------------------------------ RentCast

def fetch_rentcast(criteria, api_key):
    params = {"status": "Active", "limit": str(MAX_PER_SEARCH)}
    if criteria.get("City"):
        params["city"] = criteria["City"]
    if criteria.get("State"):
        params["state"] = criteria["State"]
    if criteria.get("Min Price") is not None:
        params["minPrice"] = int(criteria["Min Price"])
    if criteria.get("Max Price") is not None:
        params["maxPrice"] = int(criteria["Max Price"])
    if criteria.get("Min Beds") is not None:
        params["bedrooms"] = int(criteria["Min Beds"])

    url = "https://api.rentcast.io/v1/listings/sale?" + urllib.parse.urlencode(params)
    payload = _get_json(url, {"X-Api-Key": api_key, "Accept": "application/json"})
    records = payload if isinstance(payload, list) else payload.get("listings", [])

    out = []
    for r in records:
        out.append({
            "address": r.get("formattedAddress") or r.get("addressLine1") or "",
            "price": _num(r.get("price")),
            "beds": _num(r.get("bedrooms")),
            "baths": _num(r.get("bathrooms")),
            "sqft": _num(r.get("squareFootage")),
            "propertyType": r.get("propertyType") or "",
            "url": "",
            "photoUrl": "",
            "description": r.get("description") or "",
        })
    return out


def fetch_listings(criteria):
    source = os.environ.get("LISTINGS_API_TYPE", "").strip().lower()
    if source == "reso":
        base_url = os.environ.get("LISTINGS_API_URL")
        if not base_url:
            raise RuntimeError("LISTINGS_API_TYPE=reso but LISTINGS_API_URL is unset")
        return fetch_reso(criteria, base_url, os.environ.get("LISTINGS_API_KEY"))
    if source == "rentcast":
        api_key = os.environ.get("RENTCAST_API_KEY")
        if not api_key:
            raise RuntimeError("LISTINGS_API_TYPE=rentcast but RENTCAST_API_KEY is unset")
        return fetch_rentcast(criteria, api_key)
    return None  # nothing configured


# ------------------------------------------------------------------ pipeline

def passes_criteria(listing, criteria):
    types = parse_list_field(criteria.get("Property Types"))
    if types and listing.get("propertyType") not in types:
        return False
    if criteria.get("Min Sqft") is not None and (listing.get("sqft") or 0) < criteria["Min Sqft"]:
        return False
    keywords = [k.lower() for k in parse_list_field(criteria.get("Keywords"))]
    if keywords:
        haystack = f"{listing.get('description', '')} {listing.get('address', '')}".lower()
        if not any(k in haystack for k in keywords):
            return False
    return True


def estimate_rehab(listing, criteria):
    """No data source knows a specific house's repair scope, so this is a
    per-sqft placeholder the criteria row controls. It exists so the math has
    something to work with; a human should replace it with a real figure."""
    per_sqft = criteria.get("Rehab Cost Per Sqft") or 20
    sqft = listing.get("sqft")
    return round(sqft * per_sqft) if sqft else None


def build_house_fields(listing, criteria, verdict):
    m = verdict["metrics"]
    return {
        "Address": listing.get("address", ""),
        "Market": criteria.get("Market") or "",
        "Status": "New",
        "Price": listing.get("price"),
        "Beds": listing.get("beds"),
        "Baths": listing.get("baths"),
        "Sqft": listing.get("sqft"),
        "Rehab Cost": listing.get("_rehab"),
        "ARV": listing.get("_arv"),
        "Rent Estimate": listing.get("_rent"),
        "Flip Profit": round(m["flipProfit"]) if m["flipProfit"] is not None else None,
        "Cash on Cash": round(m["cashOnCash"] * 100, 1) if m["cashOnCash"] is not None else None,
        "One Percent": round(m["onePercentRatio"] * 100, 2) if m["onePercentRatio"] is not None else None,
        "Flip Verdict": verdict["flipVerdict"],
        "BRRRR Verdict": verdict["brrrrVerdict"],
        "Best Strategy": verdict["bestStrategy"] or "",
        "Qualified": verdict["qualified"],
        "Listing URL": listing.get("url") or "",
        "Photo URL": listing.get("photoUrl") or "",
        "Source": os.environ.get("LISTINGS_API_TYPE", "search"),
        "Notes": " · ".join(verdict["flipReasons"][:2]),
        "Date Added": date.today().isoformat(),
    }


def run_search(at, criteria_record, existing_keys):
    fields = criteria_record["fields"]
    name = fields.get("Name") or fields.get("Market") or "(unnamed)"

    listings = fetch_listings(fields)
    if listings is None:
        print(f"  {name}: no listing source configured, skipping")
        return []
    print(f"  {name}: fetched {len(listings)}")

    targets = {
        "flipProfit": fields.get("Target Flip Profit"),
        "cashOnCash": (fields["Target Cash on Cash"] / 100) if fields.get("Target Cash on Cash") is not None else None,
        "onePercent": (fields["Target One Percent"] / 100) if fields.get("Target One Percent") is not None else None,
    }

    new_rows = []
    for listing in listings:
        if not passes_criteria(listing, fields):
            continue
        key = (listing.get("address") or listing.get("url") or "").strip().lower()
        if not key or key in existing_keys:
            continue

        # ARV proxy: no source projects after-repair value, so start from the
        # list price and let a human correct it. Flagged via Notes downstream.
        listing["_rehab"] = estimate_rehab(listing, fields)
        listing["_arv"] = listing.get("price")
        listing["_rent"] = None

        verdict = deals.qualify(
            listing.get("price"), listing["_rehab"], listing["_arv"], listing["_rent"], targets
        )
        # Only surface things worth a look; everything else would just be noise.
        if not verdict["qualified"] and verdict["bestRank"] < 1:
            continue

        new_rows.append(build_house_fields(listing, fields, verdict))
        existing_keys.add(key)

    print(f"  {name}: {len(new_rows)} new qualifying")
    return new_rows


def main():
    try:
        at = Airtable()
    except Exception as exc:
        print(f"Airtable not configured: {exc}")
        return 0

    criteria_rows = at.list_records(TABLE_CRITERIA, formula="{Active}")
    if not criteria_rows:
        print("No Active rows in Search Criteria -- nothing to search.")
        return 0

    houses = at.list_records(TABLE_HOUSES)
    existing_keys = set()
    for rec in houses:
        f = rec.get("fields", {})
        for candidate in (f.get("Address"), f.get("Listing URL")):
            if candidate:
                existing_keys.add(str(candidate).strip().lower())

    all_new = []
    for record in criteria_rows:
        try:
            all_new.extend(run_search(at, record, existing_keys))
        except Exception as exc:
            # One bad search must not sink the rest of the run.
            name = record.get("fields", {}).get("Name", "(unnamed)")
            print(f"  {name}: FAILED {exc}")

    if all_new:
        at.create_records(TABLE_HOUSES, all_new)
        print(f"Added {len(all_new)} house(s) to Airtable.")
    else:
        print("No new qualifying houses this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
