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
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import date

from db import connect, TABLE_CRITERIA, TABLE_HOUSES, parse_list_field
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
            "source": "reso",
            "units": _num(r.get("NumberOfUnitsTotal")),
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
                "source": "rentcast",
                "units": _num(r.get("unitCount")) or _num(r.get("units")),
                # The brief's qualitative half -- dated, poorly marketed,
                # motivated seller, FSBO -- has no listing remarks to read in
                # this feed, but these four fields stand in for all of it and
                # were being thrown away.
                "yearBuilt": _num(r.get("yearBuilt")),
                "daysOnMarket": _num(r.get("daysOnMarket")),
                "priceCut": _price_cut(r.get("history")),
                "hasAgent": bool(r.get("listingAgent") or r.get("listingOffice")),
            })
    # "No agent" only means FSBO if this feed names agents for anyone. If the
    # response carries none at all, the field is simply unpopulated and every
    # house would otherwise be flagged FSBO.
    agents_seen = any(item["hasAgent"] for item in out)
    for item in out:
        item["_agentsSeen"] = agents_seen
    return out


def _price_cut(history):
    """How far below its first asking price a listing has been marked down.

    RentCast keys history by date, each entry carrying that day's price. A
    seller who has cut twice is telling you more about their motivation than
    any adjective in a listing description would.
    """
    if not isinstance(history, dict):
        return None
    # Keyed by ISO date, so sorting the keys puts them in chronological order.
    # Relying on dict order would read whatever order the JSON happened to
    # arrive in and could report a cut as a rise.
    prices = [_num(history[k].get("price")) for k in sorted(history)
              if isinstance(history[k], dict) and _num(history[k].get("price"))]
    if len(prices) < 2:
        return None
    first, last = prices[0], prices[-1]
    return round((first - last) / first, 3) if first and last < first else None


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
OVERSIZED_LOT_SQFT = 15000

# A house built this long ago and never renovated is the "dated, original
# condition" the brief is hunting. 1985 is where Atlanta's postwar and
# early-suburban stock sits, before the 90s build-out reset finishes.
DATED_BUILD_YEAR = 1985

# Cobb County's median time to contract runs about three weeks. At two months
# a listing is being passed over, which is where the brief's "poorly marketed"
# houses live.
STALE_DAYS_ON_MARKET = 60

# Below this a cut is a rounding adjustment; at or above it the seller is
# telling you something.
MEANINGFUL_PRICE_CUT = 0.03  # ~0.34 acre; room for an ADU
BELOW_MARKET_PCT = 0.15     # this much under the area median $/sqft counts
MIN_CATEGORIES = 2          # surface a house that falls into at least this many


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


def zillow_url(address):
    """A Zillow search deep-link for an address.

    Constructing a URL is not scraping -- nothing is fetched or parsed. It is
    the same thing as typing the address into Zillow's search box, which is
    the fastest way to see photos and remarks that this feed does not carry.
    It lands on Zillow's results for that address rather than a guaranteed
    listing page, because the listing id is not knowable from here.
    """
    if not address:
        return ""
    slug = "-".join(str(address).replace(",", " ").split())
    return f"https://www.zillow.com/homes/{urllib.parse.quote(slug)}_rb/"


# Condos, townhouse units and apartments are cheap per square foot because of
# what they are, not because they are mispriced -- they flooded the first real
# results and none of them is a flip or an ADU play. Excluded unless a search
# explicitly asks for them by Property Type.
ATTACHED_TYPES = ("condo", "townhouse", "apartment", "co-op", "coop", "multi")
UNIT_MARKERS = (" apt ", " unit ", " #", " ste ")

# What counts as single family, across both feeds. RentCast says "Single Family";
# RESO's PropertySubType says "Single Family Residence" or "Single Family
# Detached", so this matches on the phrase rather than the whole string.
SINGLE_FAMILY_TYPES = ("single family", "singlefamily", "detached")


def looks_attached(listing):
    kind = (listing.get("propertyType") or "").lower()
    if any(t in kind for t in ATTACHED_TYPES):
        return True
    addr = f" {(listing.get('address') or '').lower()} "
    return any(m in addr for m in UNIT_MARKERS)


def is_single_family(listing):
    """True only for a detached single-family house.

    This is a whitelist, not the absence of the attached blacklist, and the
    difference is the point: manufactured homes, duplexes and anything with a
    property type nobody anticipated all slipped through "not a condo". Every
    strategy in the brief -- flip, house plus ADU, basement conversion -- needs
    a detached house on its own lot, so the feed has to say so affirmatively.

    A listing with no property type at all is rejected. That is the opposite of
    how missing square footage is treated, deliberately: missing sqft is the
    opportunity the brief goes hunting for, while a missing property type is
    just an unidentified building, and there is no upside in guessing.

    The address still gets a look, because a feed will happily label a stacked
    unit "Single Family" when the address carries an apartment number.
    """
    kind = (listing.get("propertyType") or "").strip().lower()
    if not kind:
        return False
    if any(t in kind for t in ATTACHED_TYPES):
        return False
    if not any(t in kind for t in SINGLE_FAMILY_TYPES):
        return False
    addr = f" {(listing.get('address') or '').lower()} "
    return not any(m in addr for m in UNIT_MARKERS)


# What a feed calls a building with many units. RentCast says "Multi-Family";
# RESO's PropertySubType is usually "Residential Income" or "Apartment", and
# larger stock often arrives as plain "Multi Family" with a space.
MULTIFAMILY_TYPES = ("multi-family", "multi family", "multifamily",
                     "residential income", "apartment", "duplex", "triplex",
                     "fourplex", "quadruplex")


def is_multifamily(listing):
    """True for a building of several dwellings, false for a house.

    The mirror of is_single_family: the feed has to say so affirmatively, and
    an untyped listing is rejected rather than guessed at. A condo is NOT
    multifamily for this purpose -- it is one unit inside a building somebody
    else owns, which is the opposite of buying the building.
    """
    kind = (listing.get("propertyType") or "").strip().lower()
    if not kind:
        return False
    if "condo" in kind or "townhouse" in kind or "co-op" in kind:
        return False
    return any(t in kind for t in MULTIFAMILY_TYPES)


def looks_like_land(listing):
    """Vacant lots masquerade as two-signal houses: no sqft (signal) plus a
    big lot (signal) is exactly what raw land looks like, and the first real
    run filled Matches with $84k dirt. A land listing has no bedrooms, no
    bathrooms, and no living area -- a fixer house always has at least one of
    those on the listing, however dated it is."""
    if "land" in (listing.get("propertyType") or "").lower():
        return True
    return not (listing.get("beds") or listing.get("baths") or listing.get("sqft"))


def passes_criteria(listing, criteria):
    """Hard exclusions only: things that are not the kind of property we buy.

    Everything else the brief asks for -- price per sqft, basement, ADU room,
    fixer language -- is scored by categories() rather than filtered here.
    Requiring all of them at once produced zero results across four runs and
    450 listings, because this feed carries no listing remarks and so can
    never evidence the word-based ones. A house that provably falls into some
    of the categories is worth surfacing; demanding every one guarantees an
    empty inbox.

    Price is the exception, and it is not a category. The brief caps a flip at
    $500k and a BRRRR at $300k, and a house above the cap is not a worse deal,
    it is not a deal -- no amount of being cheap per square foot makes it one.
    Treating it as just another signal put a $1,575,000 house on the sheet
    because it was 17% under the area median. Max Price was being handed to the
    API and then trusted, which is why nothing caught it here.
    """
    if looks_like_land(listing):
        return False
    price = listing.get("price")
    if criteria.get("Max Price") is not None and price and price > criteria["Max Price"]:
        return False
    if criteria.get("Min Price") is not None and price and price < criteria["Min Price"]:
        return False
    # What kind of building this row is hunting. Blank means Single Family,
    # so every row written before this column existed behaves unchanged.
    #
    # This is a gate turned around, not a gate removed. A Single Family search
    # still admits only detached houses -- Property Types can narrow within
    # that but never widen it, which is what stopped the Atlanta row's condo
    # types readmitting four Atlanta condos. A Multifamily search inverts it:
    # a 20-unit complex is admitted and a detached house is not.
    klass = (criteria.get("Property Class") or "Single Family").strip().lower()
    if klass.startswith("single"):
        if not is_single_family(listing):
            return False
    elif klass.startswith("condo"):
        # The one class defined by what it is rather than what it is not: a
        # condo or townhouse unit, which every other search here excludes.
        if not looks_attached(listing) or is_multifamily(listing):
            return False
    elif klass.startswith("multi"):
        if not is_multifamily(listing):
            return False
        units = listing.get("units")
        floor = criteria.get("Min Units")
        # No unit count is not "fewer than twenty". Most residential feeds
        # simply do not carry one, and rejecting on its absence would empty
        # the search; the digest flags it as a to-verify instead.
        if floor and units and units < floor:
            return False
    # "Any" falls through: no class gate at all, land and attached alike.
    types = parse_list_field(criteria.get("Property Types"))
    if types and listing.get("propertyType") not in types:
        return False
    sqft = listing.get("sqft")
    if criteria.get("Min Sqft") is not None and sqft and sqft < criteria["Min Sqft"]:
        return False
    return True


def median_price_per_sqft(listings):
    """Median $/sqft of what this search is looking at right now.

    The comparison has to be against current comparable inventory, not a
    fixed number, or "cheap" means something different in 30067 than 30060.
    """
    ratios = [l["price"] / l["sqft"] for l in listings
              if l.get("price") and l.get("sqft")]
    return statistics.median(ratios) if ratios else None


def categories(listing, criteria, median_ppsf=None):
    """Which of the brief's categories this listing provably falls into.

    Deliberately excludes the price cap: the API already enforces it, so every
    listing would score it and the count would stop discriminating. Only
    things that distinguish one listing from another are counted.
    """
    hits = []
    price, sqft, lot = listing.get("price"), listing.get("sqft"), listing.get("lotSqft")

    if price and sqft:
        ppsf = price / sqft
        cap = criteria.get("Max Price Per Sqft")
        if cap and ppsf <= cap:
            hits.append(f"under ${cap:.0f}/sqft")
        if median_ppsf and ppsf <= median_ppsf * (1 - BELOW_MARKET_PCT):
            hits.append(f"{(1 - ppsf / median_ppsf) * 100:.0f}% under area $/sqft")
    elif not sqft:
        hits.append("no sqft listed")

    if lot and lot >= OVERSIZED_LOT_SQFT:
        # Acres, and no thousands separator anywhere in a category name:
        # these are stored comma-separated, so a comma inside one splits it
        # into two and corrupts both the count and the chips downstream.
        hits.append(f"oversized lot ({lot / 43560:.2f} acre)")

    all_in_cap = criteria.get("Max All In")
    rehab = estimate_rehab(listing, criteria)
    if all_in_cap and price and rehab and price + rehab <= all_in_cap:
        hits.append(f"${(price + rehab) / 1000:.0f}k all-in")

    # The brief's qualitative half, evidenced without listing remarks. A house
    # is not "ugly" or "poorly marketed" in any field, but age, time on market
    # and a seller's own price cuts are the observable shadow of all three, and
    # they are the difference between a list of cheap houses and a list of
    # houses with a reason to be cheap.
    year = listing.get("yearBuilt")
    if year and year <= DATED_BUILD_YEAR:
        hits.append(f"built {year:.0f}")

    dom = listing.get("daysOnMarket")
    if dom and dom >= STALE_DAYS_ON_MARKET:
        hits.append(f"{dom:.0f} days on market")

    cut = listing.get("priceCut")
    if cut and cut >= MEANINGFUL_PRICE_CUT:
        hits.append(f"price cut {cut * 100:.0f}%")

    # No agent and no office on a listing is the closest this feed comes to
    # naming an FSBO. Only counted when the feed populates agents at all, so a
    # response that carries none for anyone doesn't mark every house FSBO.
    if listing.get("hasAgent") is False and listing.get("_agentsSeen"):
        hits.append("possible FSBO")

    # Word-based categories. Silent on a feed without remarks -- that absence
    # is a fact about the data source, not about the house.
    hits += [s.lower() for s in value_signals(listing)
             if s not in ("No sqft listed", "Oversized lot")]
    return hits


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
        "Listing URL": listing.get("url") or zillow_url(listing.get("address")),
        "Photo URL": listing.get("photoUrl") or "",
        "Property Type": listing.get("propertyType") or "",
        "Units": listing.get("units"),
        "Found By": criteria.get("Name") or "",
        "Year Built": listing.get("yearBuilt"),
        "Days on Market": listing.get("daysOnMarket"),
        "Price Cut": round(listing["priceCut"] * 100, 1) if listing.get("priceCut") else None,
        # Where this house came from. Resolved rather than read from the env
        # so it names the adapter that actually ran, and so a house added by
        # hand or by a future source is distinguishable from a feed result.
        # The digest and the app both show it, because "we found this on X"
        # changes how much the rest of the row should be trusted.
        "Source": listing.get("source") or resolve_source(),
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

    # The comparison baseline has to come from this search's own current
    # inventory, so it is computed once over everything that survived the
    # hard exclusions rather than per listing.
    eligible = [l for l in listings if passes_criteria(l, fields)]
    median_ppsf = median_price_per_sqft(eligible)
    if median_ppsf:
        print(f"  {name}: {len(eligible)} eligible, median ${median_ppsf:,.0f}/sqft")

    new_rows = []
    for listing in eligible:
        listing["_signals"] = categories(listing, fields, median_ppsf)
        key = (listing.get("address") or listing.get("url") or "").strip().lower()
        if not key or key in existing_keys:
            continue

        # ARV is left unknown rather than proxied by the list price. Setting
        # ARV = price makes flip profit negative by construction -- price minus
        # price minus rehab minus selling costs -- so every house was written
        # with a PASS verdict that says nothing about the house and everything
        # about the placeholder. Unknown reads as NO DATA, which is true, and
        # keeps PASS meaning "a human entered real numbers and it failed".
        listing["_rehab"] = estimate_rehab(listing, fields)
        listing["_arv"] = None
        listing["_rent"] = None

        verdict = deals.qualify(
            listing.get("price"), listing["_rehab"], listing["_arv"], listing["_rent"], targets
        )
        # Surface it if it falls into enough of the brief's categories, or if
        # the math says so on its own. The categories carry the weight here:
        # ARV is a placeholder equal to list price, so the math cannot yet be
        # right about a mispriced house -- which is exactly the house we want.
        if len(listing["_signals"]) < MIN_CATEGORIES \
                and not verdict["qualified"] and verdict["bestRank"] < 1:
            continue
        # A real PASS -- one computed from numbers a human actually entered --
        # means the deal was examined and failed. Those do not belong in the
        # app. NO DATA is not a PASS; it just means nobody has costed it yet.
        if verdict["flipVerdict"] == "PASS" and verdict["brrrrVerdict"] == "PASS":
            continue

        new_rows.append(build_house_fields(listing, fields, verdict))
        existing_keys.add(key)

    new_rows.sort(key=lambda r: -len(str(r["Value Signals"]).split(", ")))
    print(f"  {name}: {len(new_rows)} new in {MIN_CATEGORIES}+ categories")
    return new_rows


def main():
    try:
        at = connect()
    except Exception as exc:
        # Fail loudly. This used to return 0, which painted the workflow green
        # while it did nothing at all -- three scheduled runs "succeeded"
        # without ever reaching a listing source, and the only way to notice
        # was to open the logs. An unconfigured worker is a broken worker.
        print(f"::error::Airtable not configured: {exc}")
        print("::error::Set the AIRTABLE_TOKEN repository secret.")
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
