#!/usr/bin/env python3
"""Refresh price/photo/beds/baths/address/sqft for tracked houses.

Two independent data sources, both best-effort — every house is refreshed
inside its own try/except so one broken/blocked listing never stops the rest
or fails the workflow:

1. Scraping the listing URL's HTML for Open Graph tags and common JSON
   fields. Sites like Zillow actively block this (confirmed via 403 even from
   GitHub's own servers), so it often does nothing — that's expected.
2. RentCast's property data API (https://rentcast.io), looked up by address,
   used as a fallback for whatever scraping couldn't fill in (beds, baths,
   sqft, and a price *estimate* if no real price is known yet — marked with
   priceIsEstimate: true since it's an AVM valuation, not the listing price).
   Only runs if RENTCAST_API_KEY is set, and only once per house
   (rentcastChecked: true) to stay within the free tier's monthly quota.

Every field can always be edited by hand in the tracker regardless of what
either source manages to find.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

HOUSES_PATH = Path(__file__).resolve().parent.parent / "houses.json"
RENTCAST_USAGE_PATH = Path(__file__).resolve().parent.parent / "rentcast_usage.json"
RENTCAST_MONTHLY_LIMIT = 45  # hard stop with a safety margin below the free tier's 50
TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PRICE_RE = re.compile(r'"price"\s*:\s*"?\$?([\d,]+)"?', re.IGNORECASE)
PRICE_FALLBACK_RE = re.compile(r"\$([\d]{2,3}(?:,\d{3})+)")
OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
BEDS_RE = re.compile(r'"bedrooms"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
BEDS_FALLBACK_RE = re.compile(r'(\d+)\s*(?:bd|bed(?:room)?s?)\b', re.IGNORECASE)
BATHS_RE = re.compile(r'"bathrooms"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
BATHS_FALLBACK_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:ba|bath(?:room)?s?)\b', re.IGNORECASE)
TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.IGNORECASE)


def load_houses():
    if not HOUSES_PATH.exists():
        return []
    return json.loads(HOUSES_PATH.read_text() or "[]")


def save_houses(houses):
    HOUSES_PATH.write_text(json.dumps(houses, indent=2) + "\n")


def load_rentcast_usage():
    current_month = date.today().strftime("%Y-%m")
    if RENTCAST_USAGE_PATH.exists():
        try:
            usage = json.loads(RENTCAST_USAGE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            usage = {}
    else:
        usage = {}
    if usage.get("month") != current_month:
        usage = {"month": current_month, "calls": 0}
    usage.setdefault("calls", 0)
    return usage


def save_rentcast_usage(usage):
    RENTCAST_USAGE_PATH.write_text(json.dumps(usage, indent=2) + "\n")


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_price(html):
    match = PRICE_RE.search(html) or PRICE_FALLBACK_RE.search(html)
    if not match:
        return None
    digits = match.group(1).replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return None


def extract_photo(html):
    match = OG_IMAGE_RE.search(html)
    return match.group(1) if match else None


def extract_number(html, precise_re, fallback_re):
    match = precise_re.search(html) or fallback_re.search(html)
    if not match:
        return None
    try:
        value = float(match.group(1))
        return int(value) if value.is_integer() else value
    except ValueError:
        return None


def extract_title_address(html):
    match = TITLE_RE.search(html)
    if not match:
        return None
    title = match.group(1).strip()
    # Strip common site suffixes like " | Zillow", " - Redfin", " | realtor.com®".
    title = re.split(r"\s*[|–—-]\s*(?:zillow|redfin|realtor|trulia)", title, flags=re.IGNORECASE)[0]
    title = title.strip()
    return title or None


def query_rentcast(path, address, api_key):
    url = f"https://api.rentcast.io/v1/{path}?" + urllib.parse.urlencode({"address": address})
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def refresh_from_rentcast(house, usage):
    """RentCast fallback, keyed by address. Only queried once per house ever
    (rentcastChecked), and every actual call is counted against a persisted
    monthly budget (usage["calls"]) with a hard stop well below the free
    tier's 50/month limit — checked before EVERY call, not just once, so a
    partial budget can still make one last call safely instead of two."""
    api_key = os.environ.get("RENTCAST_API_KEY")
    if not api_key or house.get("rentcastChecked"):
        return False
    address = house.get("address")
    if not address or house.get("addressIsGuessed"):
        return False

    house["rentcastChecked"] = True
    changed = False

    record = None
    if usage["calls"] < RENTCAST_MONTHLY_LIMIT:
        usage["calls"] += 1
        try:
            records = query_rentcast("properties", address, api_key)
            record = records[0] if isinstance(records, list) and records else None
        except Exception as exc:
            print(f"  rentcast property lookup failed for {address}: {exc}")
    else:
        print(f"  rentcast monthly call budget ({RENTCAST_MONTHLY_LIMIT}) reached, skipping property lookup")

    if record:
        if house.get("beds") is None and record.get("bedrooms") is not None:
            house["beds"] = record["bedrooms"]
            changed = True
        if house.get("baths") is None and record.get("bathrooms") is not None:
            house["baths"] = record["bathrooms"]
            changed = True
        if record.get("squareFootage") and not house.get("sqft"):
            house["sqft"] = record["squareFootage"]
            changed = True

    if house.get("price") is None:
        price = None
        if usage["calls"] < RENTCAST_MONTHLY_LIMIT:
            usage["calls"] += 1
            try:
                estimate = query_rentcast("avm/value", address, api_key)
                price = estimate.get("price") if isinstance(estimate, dict) else None
            except Exception as exc:
                print(f"  rentcast value estimate failed for {address}: {exc}")
        else:
            print(f"  rentcast monthly call budget ({RENTCAST_MONTHLY_LIMIT}) reached, skipping value estimate")
        if price:
            house["price"] = price
            house["priceIsEstimate"] = True
            changed = True
            print(f"  rentcast price estimate for {address}: ${price:,}")

    if changed:
        print(f"  rentcast filled in details for {address}")
    return changed


def refresh_house(house, rentcast_usage):
    """Mutates house in place. Returns True if anything changed."""
    changed = False
    url = house.get("url")
    html = None

    if url:
        try:
            html = fetch_html(url)
        except Exception as exc:
            print(f"  scrape skip ({house.get('address', url)}): fetch failed: {exc}")

    if html:
        try:
            new_price = extract_price(html)
            if new_price and new_price != house.get("price"):
                house.setdefault("priceHistory", [])
                if house.get("price"):
                    house["priceHistory"].append({
                        "date": date.today().isoformat(),
                        "price": house["price"],
                    })
                house["price"] = new_price
                house["priceIsEstimate"] = False
                changed = True
                print(f"  price updated for {house.get('address', url)}: ${new_price:,}")
        except Exception as exc:
            print(f"  price parse failed for {house.get('address', url)}: {exc}")

        try:
            if not house.get("photoUrl"):
                photo = extract_photo(html)
                if photo:
                    house["photoUrl"] = photo
                    changed = True
                    print(f"  photo set for {house.get('address', url)}")
        except Exception as exc:
            print(f"  photo parse failed for {house.get('address', url)}: {exc}")

        try:
            if house.get("beds") is None:
                beds = extract_number(html, BEDS_RE, BEDS_FALLBACK_RE)
                if beds is not None:
                    house["beds"] = beds
                    changed = True
        except Exception as exc:
            print(f"  beds parse failed for {house.get('address', url)}: {exc}")

        try:
            if house.get("baths") is None:
                baths = extract_number(html, BATHS_RE, BATHS_FALLBACK_RE)
                if baths is not None:
                    house["baths"] = baths
                    changed = True
        except Exception as exc:
            print(f"  baths parse failed for {house.get('address', url)}: {exc}")

        try:
            if house.get("addressIsGuessed"):
                address = extract_title_address(html)
                if address:
                    house["address"] = address
                    house["addressIsGuessed"] = False
                    changed = True
                    print(f"  address filled in: {address}")
        except Exception as exc:
            print(f"  address parse failed for {house.get('address', url)}: {exc}")

    try:
        if refresh_from_rentcast(house, rentcast_usage):
            changed = True
    except Exception as exc:
        print(f"  rentcast lookup failed for {house.get('address', url)}: {exc}")

    return changed


def main():
    houses = load_houses()
    rentcast_usage = load_rentcast_usage()
    starting_calls = rentcast_usage["calls"]
    any_changed = False

    for house in houses:
        try:
            if refresh_house(house, rentcast_usage):
                any_changed = True
        except Exception as exc:
            print(f"  unexpected error on {house.get('address', house.get('url'))}: {exc}")

    if rentcast_usage["calls"] != starting_calls:
        save_rentcast_usage(rentcast_usage)
        print(f"RentCast calls this month: {rentcast_usage['calls']}/{RENTCAST_MONTHLY_LIMIT}")

    if any_changed:
        save_houses(houses)
        print("houses.json updated.")
    else:
        print("No changes.")


if __name__ == "__main__":
    sys.exit(main())
