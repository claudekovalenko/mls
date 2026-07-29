#!/usr/bin/env python3
"""Refresh price/photo for tracked houses by re-checking each listing URL.

Best-effort only: every house is refreshed independently inside its own
try/except, so one broken/blocked listing (anti-scraping, layout change,
timeout) never stops the rest from updating or fails the workflow. Sites
like Zillow actively block scrapers, so this may often do nothing for a
given house — that's expected and fine, the price/photo can always be
edited by hand in the tracker.

Looks for Open Graph meta tags (og:image, og:price:amount / a $-prefixed
price in the title/description) in the listing page's HTML. Appends a
{date, price} entry to priceHistory whenever the detected price differs
from what's stored.
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
