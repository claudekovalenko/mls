#!/usr/bin/env python3
"""Fetch listings matching criteria.json and email them daily.

Listing data source is pluggable via LISTINGS_API_URL / LISTINGS_API_KEY env vars
(expects a JSON array of listings with price/beds/baths/address/url/photo fields).
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


def fetch_listings(criteria):
    api_url = os.environ.get("LISTINGS_API_URL")
    api_key = os.environ.get("LISTINGS_API_KEY")
    if not api_url:
        print("LISTINGS_API_URL not set; skipping fetch (no data source configured).")
        return []

    req = urllib.request.Request(api_url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        listings = json.loads(resp.read())

    location = criteria.get("location", "").strip().lower()
    keywords = [k.lower() for k in criteria.get("keywords", [])]

    def matches_keywords(listing):
        if not keywords:
            return True
        haystack = " ".join(
            str(listing.get(field, ""))
            for field in ("description", "remarks", "title", "address")
        ).lower()
        return any(keyword in haystack for keyword in keywords)

    return [
        listing
        for listing in listings
        if criteria["minPrice"] <= listing.get("price", 0) <= criteria["maxPrice"]
        and listing.get("beds", 0) >= criteria["minBeds"]
        and listing.get("baths", 0) >= criteria["minBaths"]
        and (not criteria.get("propertyTypes") or listing.get("propertyType") in criteria["propertyTypes"])
        and (not location or location in listing.get("address", "").strip().lower())
        and matches_keywords(listing)
    ]


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
    return f"{TRACKER_ADD_URL}?{urllib.parse.urlencode(params)}"


def build_plain_body(listings):
    if not listings:
        return "No new listings matched your criteria today."
    lines = [f"{len(listings)} listing(s) matching your criteria today:\n"]
    for i, listing in enumerate(listings, start=1):
        lines.append(
            f"{i}. {listing.get('address', 'Unknown address')} — "
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

    rows = []
    for i, listing in enumerate(listings, start=1):
        address = html.escape(listing.get("address", "Unknown address"))
        price = f"${listing.get('price', 0):,}"
        beds = listing.get("beds", "?")
        baths = listing.get("baths", "?")
        url = listing.get("url", "")
        add_link = tracker_add_link(listing)
        rows.append(f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #e2e8e4;">
            <div style="font-weight:700;font-size:15px;color:#16211b;">{i}. {address}</div>
            <div style="color:#5b6a62;font-size:13px;margin-top:2px;">{price} — {beds}bd/{baths}ba</div>
            <div style="margin-top:10px;">
              <a href="{add_link}" style="display:inline-block;background:#1f6f4a;color:#fff;text-decoration:none;
                 padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;">+ Add to Tracker</a>
              {f'<a href="{html.escape(url)}" style="display:inline-block;margin-left:8px;color:#1f6f4a;text-decoration:none;font-size:13px;font-weight:600;">View Listing &rarr;</a>' if url else ''}
            </div>
          </td>
        </tr>""")

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#16211b;max-width:600px;">
      <p style="font-size:15px;">{len(listings)} listing(s) matching your criteria today:</p>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
      <p style="color:#5b6a62;font-size:12px;margin-top:20px;">
        Or reply to this email with "LIKE 1,3" (listing numbers) to add them instead.
      </p>
    </div>
    """


def build_email(criteria, listings):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily listing alert ({len(listings)} match{'es' if len(listings) != 1 else ''})"
    msg["From"] = os.environ.get("GMAIL_USER", "")
    msg["To"] = criteria["recipientEmail"]
    if criteria.get("ccEmail"):
        msg["Cc"] = criteria["ccEmail"]
    msg.attach(MIMEText(build_plain_body(listings), "plain"))
    msg.attach(MIMEText(build_html_body(listings), "html"))
    return msg


def send_email(msg):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pass:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set; printing email instead of sending.")
        print(msg.as_string())
        return

    recipients = [msg["To"]] + ([msg["Cc"]] if msg.get("Cc") else [])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, recipients, msg.as_string())
    print(f"Sent alert to {recipients}")


def main():
    criteria = load_criteria()
    listings = fetch_listings(criteria)
    save_last_alert_listings(listings)
    msg = build_email(criteria, listings)
    send_email(msg)


if __name__ == "__main__":
    sys.exit(main())
