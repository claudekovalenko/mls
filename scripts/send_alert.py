#!/usr/bin/env python3
"""Fetch listings matching criteria.json and email them daily.

criteria.json holds a list of markets (Atlanta, Los Angeles, ...), each with its
own price/beds/baths/keyword filters. Every market is searched separately and its
listings tagged with the market name, so they land in the right section of the
tracker instead of being lumped together.

Two listing-source modes, both driven by LISTINGS_API_URL / LISTINGS_API_KEY:

  LISTINGS_API_TYPE=json (default)
      Expects a JSON array of {price, beds, baths, address, url, propertyType}.
      The URL may contain {city}/{state}/{location} placeholders, substituted
      per market.

  LISTINGS_API_TYPE=reso
      A RESO Web API (OData) feed -- the standard interface MLSs expose for IDX.
      Builds a per-market $filter and maps RESO's standard field names onto the
      shape the rest of this pipeline expects. This is the mode to use once an
      MLS (FMLS/GAMLS for Atlanta, CRMLS for LA) issues IDX credentials.

Without LISTINGS_API_URL set, no listings are fetched and the daily email
reports zero matches -- which is the current state, and why nothing has ever
appeared in the alerts.

Email is sent over SMTP using GMAIL_USER / GMAIL_APP_PASSWORD secrets.
"""
import html
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

CRITERIA_PATH = Path(__file__).resolve().parent.parent / "criteria.json"
LAST_ALERT_PATH = Path(__file__).resolve().parent.parent / "last_alert_listings.json"
TRACKER_ADD_URL = "https://claudekovalenko.github.io/mls/add.html"


def load_criteria():
    return json.loads(CRITERIA_PATH.read_text())


def get_markets(criteria):
    """Markets list, with back-compat for the old single-market schema where
    location/minPrice/etc. lived at the top level."""
    markets = criteria.get("markets")
    if markets:
        return markets
    legacy = {k: v for k, v in criteria.items() if k not in ("recipientEmails", "recipientEmail", "ccEmail", "notes")}
    legacy.setdefault("name", legacy.get("location", "Unknown"))
    return [legacy]


def _http_get_json(url, api_key, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _reso_escape(value):
    """OData string literals escape a single quote by doubling it."""
    return str(value).replace("'", "''")


def build_reso_url(base_url, market):
    """Build a RESO Web API (OData) Property query for one market.

    Uses RESO's standard field names, which is the whole point of the standard --
    any compliant MLS feed answers to these regardless of vendor.
    """
    filters = ["StandardStatus eq 'Active'"]
    if market.get("city"):
        filters.append(f"City eq '{_reso_escape(market['city'])}'")
    if market.get("state"):
        filters.append(f"StateOrProvince eq '{_reso_escape(market['state'])}'")
    if market.get("minPrice") is not None:
        filters.append(f"ListPrice ge {int(market['minPrice'])}")
    if market.get("maxPrice") is not None:
        filters.append(f"ListPrice le {int(market['maxPrice'])}")
    if market.get("minBeds") is not None:
        filters.append(f"BedroomsTotal ge {int(market['minBeds'])}")
    if market.get("minBaths") is not None:
        filters.append(f"BathroomsTotalInteger ge {int(market['minBaths'])}")

    query = {
        "$filter": " and ".join(filters),
        "$top": "50",
        "$orderby": "ListPrice asc",
    }
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{urllib.parse.urlencode(query)}"


def normalize_reso_listing(record):
    """Map RESO standard field names onto this pipeline's listing shape."""
    address = record.get("UnparsedAddress") or " ".join(
        str(record.get(f, "")) for f in ("StreetNumber", "StreetName", "City", "StateOrProvince")
    ).strip()
    media = record.get("Media") or []
    photo = ""
    if isinstance(media, list) and media:
        first = media[0]
        if isinstance(first, dict):
            photo = first.get("MediaURL", "")
    return {
        "address": address,
        "price": record.get("ListPrice"),
        "beds": record.get("BedroomsTotal"),
        "baths": record.get("BathroomsTotalInteger"),
        "sqft": record.get("LivingArea"),
        "propertyType": record.get("PropertySubType") or record.get("PropertyType"),
        "url": record.get("ListingURL") or "",
        "photoUrl": photo,
        "description": record.get("PublicRemarks", ""),
    }


def fetch_raw_listings(market):
    """Pull the candidate listings for one market from whichever source is
    configured. Returns [] (never raises) when nothing is configured, so a
    missing feed degrades to 'no matches' rather than failing the workflow."""
    api_url = os.environ.get("LISTINGS_API_URL")
    api_key = os.environ.get("LISTINGS_API_KEY")
    api_type = os.environ.get("LISTINGS_API_TYPE", "json").strip().lower()
    if not api_url:
        return []

    if api_type == "reso":
        url = build_reso_url(api_url, market)
        payload = _http_get_json(url, api_key, accept="application/json;odata.metadata=minimal")
        records = payload.get("value", []) if isinstance(payload, dict) else payload
        return [normalize_reso_listing(r) for r in records]

    url = api_url.format(
        city=urllib.parse.quote(str(market.get("city", ""))),
        state=urllib.parse.quote(str(market.get("state", ""))),
        location=urllib.parse.quote(str(market.get("location", ""))),
    )
    payload = _http_get_json(url, api_key)
    return payload.get("listings", []) if isinstance(payload, dict) else payload


def filter_listings(listings, market):
    location = market.get("location", "").strip().lower()
    keywords = [k.lower() for k in market.get("keywords", [])]

    def matches_keywords(listing):
        if not keywords:
            return True
        haystack = " ".join(
            str(listing.get(field, ""))
            for field in ("description", "remarks", "title", "address")
        ).lower()
        return any(keyword in haystack for keyword in keywords)

    def in_range(listing):
        price = listing.get("price")
        if price is None:
            return False
        if market.get("minPrice") is not None and price < market["minPrice"]:
            return False
        if market.get("maxPrice") is not None and price > market["maxPrice"]:
            return False
        return True

    return [
        listing
        for listing in listings
        if in_range(listing)
        and (listing.get("beds") or 0) >= (market.get("minBeds") or 0)
        and (listing.get("baths") or 0) >= (market.get("minBaths") or 0)
        and (not market.get("propertyTypes") or listing.get("propertyType") in market["propertyTypes"])
        and (not location or location in listing.get("address", "").strip().lower())
        and matches_keywords(listing)
    ]


def fetch_listings(criteria):
    """All markets, each fetched and filtered on its own terms, tagged with the
    market name so downstream (email grouping, houses.json) keeps them separate."""
    if not os.environ.get("LISTINGS_API_URL"):
        print("LISTINGS_API_URL not set; skipping fetch (no data source configured).")
        print("  -> This is why the daily alert reports no matches. See README for IDX setup.")
        return []

    all_listings = []
    for market in get_markets(criteria):
        name = market.get("name") or market.get("location", "Unknown")
        try:
            raw = fetch_raw_listings(market)
            matched = filter_listings(raw, market)
            for listing in matched:
                listing["market"] = name
            all_listings.extend(matched)
            print(f"  {name}: {len(matched)} match(es) out of {len(raw)} fetched")
        except Exception as exc:
            # One market's feed failing must not sink the others or the email.
            print(f"  {name}: fetch failed: {exc}")
    return all_listings


def save_last_alert_listings(listings):
    LAST_ALERT_PATH.write_text(json.dumps(listings, indent=2) + "\n")


def tracker_add_link(listing):
    params = {"a": listing.get("address", "")}
    if listing.get("url"):
        params["u"] = listing["url"]
    if listing.get("price"):
        params["p"] = listing["price"]
    if listing.get("beds") is not None:
        params["bd"] = listing["beds"]
    if listing.get("baths") is not None:
        params["ba"] = listing["baths"]
    if listing.get("photoUrl"):
        params["photo"] = listing["photoUrl"]
    if listing.get("market"):
        params["mkt"] = listing["market"]
    return f"{TRACKER_ADD_URL}?{urllib.parse.urlencode(params)}"


def group_by_market(listings):
    """Preserve a stable market order and keep each market's listings together,
    while the numbering stays global so "LIKE 1,3" still maps to one flat list."""
    grouped = {}
    for listing in listings:
        grouped.setdefault(listing.get("market") or "Other", []).append(listing)
    return grouped


def build_plain_body(listings):
    if not listings:
        return "No new listings matched your criteria today."
    lines = [f"{len(listings)} listing(s) matching your criteria today:\n"]
    number = 0
    for market, market_listings in group_by_market(listings).items():
        lines.append(f"\n=== {market} ({len(market_listings)}) ===")
        for listing in market_listings:
            number += 1
            lines.append(
                f"{number}. {listing.get('address', 'Unknown address')} — "
                f"${listing.get('price', 0):,} — "
                f"{listing.get('beds', '?')}bd/{listing.get('baths', '?')}ba\n"
                f"   Listing: {listing.get('url', '')}\n"
                f"   Add to tracker: {tracker_add_link(listing)}"
            )
    lines.append(
        "\n---\nOr reply to this email with:\n"
        "  LIKE 1,3\n"
        "(the numbers of the listings you want added to the House Tracker)"
    )
    return "\n".join(lines)


def build_html_body(listings):
    if not listings:
        return "<p>No new listings matched your criteria today.</p>"

    sections = []
    number = 0
    for market, market_listings in group_by_market(listings).items():
        rows = []
        for listing in market_listings:
            number += 1
            address = html.escape(listing.get("address", "Unknown address"))
            price = f"${listing.get('price', 0):,}"
            beds = listing.get("beds", "?")
            baths = listing.get("baths", "?")
            url = listing.get("url", "")
            add_link = tracker_add_link(listing)
            rows.append(f"""
            <tr>
              <td style="padding:12px 0;border-bottom:1px solid #e2e8e4;">
                <div style="font-weight:700;font-size:15px;color:#16211b;">{number}. {address}</div>
                <div style="color:#5b6a62;font-size:13px;margin-top:2px;">{price} — {beds}bd/{baths}ba</div>
                <div style="margin-top:10px;">
                  <a href="{add_link}" style="display:inline-block;background:#1f6f4a;color:#fff;text-decoration:none;
                     padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;">+ Add to Tracker</a>
                  {f'<a href="{html.escape(url)}" style="display:inline-block;margin-left:8px;color:#1f6f4a;text-decoration:none;font-size:13px;font-weight:600;">View Listing &rarr;</a>' if url else ''}
                </div>
              </td>
            </tr>""")
        sections.append(f"""
        <div style="margin-top:24px;">
          <div style="font-size:12px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;
               color:#1f6f4a;border-bottom:2px solid #e6f2ec;padding-bottom:6px;">
            {html.escape(market)} — {len(market_listings)} listing(s)
          </div>
          <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
        </div>""")

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#16211b;max-width:600px;">
      <p style="font-size:15px;">{len(listings)} listing(s) matching your criteria today:</p>
      {''.join(sections)}
      <p style="color:#5b6a62;font-size:12px;margin-top:20px;">
        Or reply to this email with "LIKE 1,3" (listing numbers, counted across all markets)
        to add them instead.
      </p>
    </div>
    """


def get_recipients(criteria):
    recipients = criteria.get("recipientEmails")
    if recipients:
        return [r.strip() for r in recipients if r.strip()]
    # Back-compat with the old single-recipient/cc schema.
    legacy = [criteria.get("recipientEmail", "")]
    if criteria.get("ccEmail"):
        legacy.append(criteria["ccEmail"])
    return [r.strip() for r in legacy if r.strip()]


def build_email(criteria, listings, recipients):
    msg = MIMEMultipart("alternative")
    by_market = group_by_market(listings)
    breakdown = ", ".join(f"{m} {len(v)}" for m, v in by_market.items()) if listings else "none"
    msg["Subject"] = f"Daily listing alert ({len(listings)} match{'es' if len(listings) != 1 else ''} — {breakdown})"
    msg["From"] = os.environ.get("GMAIL_USER", "")
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(build_plain_body(listings), "plain"))
    msg.attach(MIMEText(build_html_body(listings), "html"))
    return msg


def send_email(msg, recipients):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pass:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set; printing email instead of sending.")
        print(msg.as_string())
        return

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, recipients, msg.as_string())
    print(f"Sent alert to {recipients}")


def main():
    criteria = load_criteria()
    listings = fetch_listings(criteria)
    save_last_alert_listings(listings)
    recipients = get_recipients(criteria)
    msg = build_email(criteria, listings, recipients)
    send_email(msg, recipients)


if __name__ == "__main__":
    sys.exit(main())
