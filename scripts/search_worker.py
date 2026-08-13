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
import rentcast_budget

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
    zips = parse_list_field(criteria.get("Zip Codes"))
    if zips:
        # A zip list is how "in 30068 or within ~10 miles" is expressed --
        # plain OData feeds don't do radius queries, but a ring of zips does
        # the same job and is inspectable in the criteria row.
        ors = " or ".join(f"PostalCode eq '{_odata_escape(z)}'" for z in zips)
        filters.append(f"({ors})")
    elif criteria.get("City"):
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
        lot = _num(r.get("LotSizeSquareFeet"))
        if lot is None and _num(r.get("LotSizeAcres")) is not None:
            lot = round(_num(r.get("LotSizeAcres")) * 43560)
        out.append({
            "address": r.get("UnparsedAddress") or "",
            "price": _num(r.get("ListPrice")),
            "beds": _num(r.get("BedroomsTotal")),
            "baths": _num(r.get("BathroomsTotalInteger")),
            "sqft": _num(r.get("LivingArea")),
            "lotSqft": lot,
            "propertyType": r.get("PropertySubType") or r.get("PropertyType") or "",
            "url": r.get("ListingURL") or "",
            "photoUrl": photo,
            "description": r.get("PublicRemarks") or "",
        })
    return out


# ------------------------------------------------------------------ RentCast

def fetch_rentcast(criteria, api_key, budget):
    # RentCast takes one zip per request, so a zip ring costs one call each --
    # the 16-zip "30068 + 10 mi" row is 16 billed requests every single run.
    # Every one of them goes through the budget gate before it is issued.
    zips = parse_list_field(criteria.get("Zip Codes")) or [None]
    out = []
    for zip_code in zips:
        if not budget.can_spend():
            print(f"    budget gate: stopping after {len(out)} listing(s); "
                  f"remaining zips deferred to the next run")
            break
        params = {"status": "Active", "limit": str(MAX_PER_SEARCH)}
        if zip_code:
            params["zipCode"] = zip_code
        elif criteria.get("City"):
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
        # Counted before the request: a request that errors after reaching
        # RentCast is still billed, so counting on success would overspend.
        budget.spend(note=f"{criteria.get('Name', '?')} / {zip_code or criteria.get('City', '?')}")
        payload = _get_json(url, {"X-Api-Key": api_key, "Accept": "application/json"})
        records = payload if isinstance(payload, list) else payload.get("listings", [])

        for r in records:
            out.append({
                "address": r.get("formattedAddress") or r.get("addressLine1") or "",
                "price": _num(r.get("price")),
                "beds": _num(r.get("bedrooms")),
                "baths": _num(r.get("bathrooms")),
                "sqft": _num(r.get("squareFootage")),
                "lotSqft": _num(r.get("lotSize")),
                "propertyType": r.get("propertyType") or "",
                "url": "",
                "photoUrl": "",
                "description": r.get("description") or "",
            })
    return out


def resolve_source():
    """Which listing source to use, inferred rather than declared.

    LISTINGS_API_TYPE still wins when set, so an explicit choice is always
    possible -- including forcing "reso" while a RentCast key happens to also be
    present. But requiring it meant a repo could hold a perfectly good RentCast
    key and still search nothing, which is exactly what happened here: the key
    was configured, the variable was not, and three scheduled runs did nothing.
    A credential that is present is a credential that is meant to be used.

    RESO wins the tie when both are configured: it's real MLS data, and the
    RentCast key is a stopgap.
    """
    declared = os.environ.get("LISTINGS_API_TYPE", "").strip().lower()
    if declared in ("reso", "rentcast"):
        return declared
    if os.environ.get("LISTINGS_API_URL"):
        return "reso"
    if os.environ.get("RENTCAST_API_KEY"):
        return "rentcast"
    return None


def fetch_listings(criteria, budget):
    source = resolve_source()
    if source == "reso":
        base_url = os.environ.get("LISTINGS_API_URL")
        if not base_url:
            raise RuntimeError("LISTINGS_API_TYPE=reso but LISTINGS_API_URL is unset")
        return fetch_reso(criteria, base_url, os.environ.get("LISTINGS_API_KEY"))
    if source == "rentcast":
        api_key = os.environ.get("RENTCAST_API_KEY")
        if not api_key:
            raise RuntimeError("LISTINGS_API_TYPE=rentcast but RENTCAST_API_KEY is unset")
        return fetch_rentcast(criteria, api_key, budget)
    return None  # nothing configured


# ------------------------------------------------------------------ pipeline

# The thesis this whole search runs on: value a normal buyer overlooks.
# Each signal is a hint the listing is mispriced or mismarketed -- ugly,
# dated, badly listed, or hiding expandable space.
SIGNAL_RULES = [
    ("Basement", ("basement",)),
    ("ADU potential", ("adu", "in-law", "in law", "guest house", "guesthouse",
                       "carriage house", "kitchenette", "separate entrance",
                       "second kitchen", "detached garage")),
    ("FSBO", ("fsbo", "for sale by owner")),
    ("Fixer", ("fixer", "as-is", "as is", "tlc", "needs work", "handyman",
               "investor special", "cash only", "estate sale", "sold as-is",
               "bring your vision", "dated", "original condition")),
]
OVERSIZED_LOT_SQFT = 15000  # ~0.34 acre; room for an ADU


def value_signals(listing):
    hay = (listing.get("description") or "").lower()
    signals = [name for name, needles in SIGNAL_RULES
               if any(n in hay for n in needles)]
    if not listing.get("sqft"):
        # Missing sqft is an opportunity, not a defect: comps undervalue what
        # they can't measure, so these listings get flagged, never filtered.
        signals.append("No sqft listed")
    if (listing.get("lotSqft") or 0) >= OVERSIZED_LOT_SQFT:
        signals.append("Oversized lot")
    return signals


def passes_criteria(listing, criteria):
    types = parse_list_field(criteria.get("Property Types"))
    if types and listing.get("propertyType") not in types:
        return False
    # Sqft floors and $/sqft caps only apply when sqft is actually listed;
    # a missing figure passes both, deliberately (see value_signals).
    sqft = listing.get("sqft")
    if criteria.get("Min Sqft") is not None and sqft and sqft < criteria["Min Sqft"]:
        return False
    max_ppsf = criteria.get("Max Price Per Sqft")
    if max_ppsf is not None and sqft and listing.get("price"):
        if listing["price"] / sqft > max_ppsf:
            return False
    keywords = [k.lower() for k in parse_list_field(criteria.get("Keywords"))]
    if keywords:
        haystack = f"{listing.get('description', '')} {listing.get('address', '')}".lower()
        if not any(k in haystack for k in keywords):
            return False
    # Must Haves: every comma-separated entry is required; "/" inside an entry
    # lists alternatives ("adu/oversized lot" = either satisfies it). Matched
    # against detected signals first, raw description as fallback.
    must = parse_list_field(criteria.get("Must Haves"))
    if must:
        signals = [s.lower() for s in listing.get("_signals", [])]
        hay = (listing.get("description") or "").lower()
        for requirement in must:
            alts = [a.strip().lower() for a in requirement.split("/") if a.strip()]
            met = any(a in s for a in alts for s in signals) or any(a in hay for a in alts)
            if not met:
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
        "Lot Sqft": listing.get("lotSqft"),
        "Price Per Sqft": round(listing["price"] / listing["sqft"])
            if listing.get("price") and listing.get("sqft") else None,
        "Value Signals": ", ".join(listing.get("_signals", [])),
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


def run_search(at, criteria_record, existing_keys, budget):
    fields = criteria_record["fields"]
    name = fields.get("Name") or fields.get("Market") or "(unnamed)"

    listings = fetch_listings(fields, budget)
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
        listing["_signals"] = value_signals(listing)
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

        # All-in cap (price + rehab): how "300k + 50k reno, 350k max" is
        # enforced. Only bites when both numbers exist.
        cap = fields.get("Max All In")
        if cap and listing.get("price") and listing["_rehab"] \
                and listing["price"] + listing["_rehab"] > cap:
            continue

        verdict = deals.qualify(
            listing.get("price"), listing["_rehab"], listing["_arv"], listing["_rent"], targets
        )
        # Surface it if the math says so -- or if it's the kind of listing the
        # whole search exists to catch: two or more value signals means ugly/
        # dated/mismarketed/expandable, where the placeholder math (ARV = list
        # price) is exactly what you'd expect to be wrong. Everything else is
        # noise and stays out.
        if not verdict["qualified"] and verdict["bestRank"] < 1 \
                and len(listing["_signals"]) < 2:
            continue

        new_rows.append(build_house_fields(listing, fields, verdict))
        existing_keys.add(key)

    print(f"  {name}: {len(new_rows)} new qualifying")
    return new_rows


def main():
    try:
        at = Airtable()
    except Exception as exc:
        # Fail loudly. This used to return 0, which painted the workflow green
        # while it did nothing at all -- three scheduled runs "succeeded"
        # without ever reaching a listing source, and the only way to notice
        # was to open the logs. An unconfigured worker is a broken worker.
        print(f"::error::Airtable not configured: {exc}")
        print("::error::Set the AIRTABLE_TOKEN and AIRTABLE_BASE_ID repository secrets.")
        return 1

    source = resolve_source()
    if not source:
        print("::error::No listing source available: neither RENTCAST_API_KEY nor "
              "LISTINGS_API_URL is set, so there is nothing to search.")
        return 1
    print(f"Listing source: {source}")

    criteria_rows = at.list_records(TABLE_CRITERIA, formula="{Active}")
    if not criteria_rows:
        print("::warning::No Active rows in Search Criteria -- nothing to search.")
        return 0

    budget = rentcast_budget.load()
    print(budget.summary())

    houses = at.list_records(TABLE_HOUSES)
    existing_keys = set()
    for rec in houses:
        f = rec.get("fields", {})
        for candidate in (f.get("Address"), f.get("Listing URL")):
            if candidate:
                existing_keys.add(str(candidate).strip().lower())

    all_new = []
    try:
        for record in criteria_rows:
            try:
                all_new.extend(run_search(at, record, existing_keys, budget))
            except rentcast_budget.BudgetExhausted:
                # Propagate: this is not a per-search failure, it means every
                # remaining search would be refused too. Stop cleanly and keep
                # whatever was already found.
                raise
            except Exception as exc:
                # One bad search must not sink the rest of the run.
                name = record.get("fields", {}).get("Name", "(unnamed)")
                print(f"  {name}: FAILED {exc}")
    except rentcast_budget.BudgetExhausted as exc:
        print(f"  STOPPING: {exc}")
    finally:
        # Always persist, including on the exhausted path -- an uncommitted
        # counter after real spend is exactly how a balance gets drained twice.
        rentcast_budget.save(budget)
        print(budget.summary())

    if all_new:
        at.create_records(TABLE_HOUSES, all_new)
        print(f"Added {len(all_new)} house(s) to Airtable.")
    else:
        print("No new qualifying houses this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
