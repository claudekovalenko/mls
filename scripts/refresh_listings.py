#!/usr/bin/env python3
"""Refresh price/photo/beds/baths/address for tracked houses from their listing URL.

Best-effort only: every house is refreshed independently inside its own
try/except, so one broken/blocked listing (anti-scraping, layout change,
timeout) never stops the rest from updating or fails the workflow. Sites
like Zillow actively block scrapers, so this may often do nothing for a
given house — that's expected and fine, every field can always be edited
by hand in the tracker.

Looks for Open Graph meta tags (og:image, og:title) and common JSON
fields (price/bedrooms/bathrooms) embedded in the listing page's HTML.
Appends a {date, price} entry to priceHistory whenever the detected price
differs from what's stored. Only overwrites the address if it was a
guessed placeholder (quick-add sets addressIsGuessed: true) so a manually
entered address is never clobbered.
"""
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

HOUSES_PATH = Path(__file__).resolve().parent.parent / "houses.json"
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


def refresh_house(house):
    """Mutates house in place. Returns True if anything changed."""
    url = house.get("url")
    if not url:
        return False

    changed = False
    try:
        html = fetch_html(url)
    except Exception as exc:
        print(f"  skip ({house.get('address', url)}): fetch failed: {exc}")
        return False

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

    return changed


def main():
    houses = load_houses()
    any_changed = False
    for house in houses:
        try:
            if refresh_house(house):
                any_changed = True
        except Exception as exc:
            print(f"  unexpected error on {house.get('address', house.get('url'))}: {exc}")

    if any_changed:
        save_houses(houses)
        print("houses.json updated.")
    else:
        print("No changes.")


if __name__ == "__main__":
    sys.exit(main())
