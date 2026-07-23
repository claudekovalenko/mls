#!/usr/bin/env python3
"""Fetch listings matching criteria.json and email them daily.

Listing data source is pluggable via LISTINGS_API_URL / LISTINGS_API_KEY env vars
(expects a JSON array of listings with price/beds/baths/address/url/photo fields).
Email is sent over SMTP using GMAIL_USER / GMAIL_APP_PASSWORD secrets.
"""
import json
import os
import smtplib
import sys
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

CRITERIA_PATH = Path(__file__).resolve().parent.parent / "criteria.json"


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
        and matches_keywords(listing)
    ]


def build_email(criteria, listings):
    if listings:
        body_lines = [f"{len(listings)} listing(s) matching your criteria today:\n"]
        for listing in listings:
            body_lines.append(
                f"- {listing.get('address', 'Unknown address')} — "
                f"${listing.get('price', 0):,} — "
                f"{listing.get('beds', '?')}bd/{listing.get('baths', '?')}ba\n"
                f"  {listing.get('url', '')}"
            )
        body = "\n".join(body_lines)
    else:
        body = "No new listings matched your criteria today."

    msg = MIMEMultipart()
    msg["Subject"] = f"Daily listing alert ({len(listings)} match{'es' if len(listings) != 1 else ''})"
    msg["From"] = os.environ.get("GMAIL_USER", "")
    msg["To"] = criteria["recipientEmail"]
    if criteria.get("ccEmail"):
        msg["Cc"] = criteria["ccEmail"]
    msg.attach(MIMEText(body, "plain"))
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
    msg = build_email(criteria, listings)
    send_email(msg)


if __name__ == "__main__":
    sys.exit(main())
