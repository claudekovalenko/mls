#!/usr/bin/env python3
"""Poll Gmail for two kinds of email and turn them into tracked houses.

1. Replies to the daily listing alert containing a line like "LIKE 1,3" — the
   numbered listings from that day's alert (last_alert_listings.json) get added.
2. ANY unread email (to yourself, forwarded, whatever) whose subject or body
   contains a real-estate listing URL (Zillow/Redfin/Realtor/Trulia) — the URL
   gets added as a new house automatically, address best-effort guessed from the
   URL's slug (no page fetch needed, so this works even for sites that block
   scraping). This is the easiest way to add a house: just email or share a
   listing link to the inbox this script polls.

Every matched message is marked read so it isn't processed twice. Requires
GMAIL_USER / GMAIL_APP_PASSWORD (same secrets as send_alert.py) with IMAP access
enabled on the account.
"""
import imaplib
import email
import json
import os
import re
import sys
import uuid
from datetime import date
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAST_ALERT_PATH = ROOT / "last_alert_listings.json"
HOUSES_PATH = ROOT / "houses.json"

LIKE_RE = re.compile(r"\bLIKE\b[^\d]*([\d,\s]+)", re.IGNORECASE)
LISTING_URL_RE = re.compile(
    r"https?://(?:www\.)?(zillow|redfin|realtor|trulia|homes)\.com/\S+",
    re.IGNORECASE,
)


def load_json(path):
    if not path.exists():
        return []
    return json.loads(path.read_text() or "[]")


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def decode_subject(raw_subject):
    if not raw_subject:
        return ""
    parts = decode_header(raw_subject)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="ignore")
        else:
            decoded += text
    return decoded


def get_body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="ignore")
        return ""
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="ignore") if payload else ""


def parse_liked_numbers(body):
    match = LIKE_RE.search(body)
    if not match:
        return []
    numbers = set()
    for chunk in match.group(1).split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            numbers.add(int(chunk))
    return sorted(numbers)


def guess_address_from_url(url):
    """Best-effort address from the URL slug alone — no page fetch, so it
    works even on sites that block scraping (e.g. Zillow)."""
    match = re.search(r"/homedetails/([^/]+)/\d+_zpid", url, re.IGNORECASE)
    if match:
        return match.group(1).replace("-", " ")
    parts = [
        p for p in url.split("/")
        if "-" in p and any(c.isdigit() for c in p) and any(c.isalpha() for c in p)
    ]
    if parts:
        return parts[0].replace("-", " ")
    return None


def add_house_from_url(url, houses):
    existing_urls = {h.get("url") for h in houses if h.get("url")}
    clean_url = url.split("?")[0]
    if clean_url in existing_urls:
        return None

    address = guess_address_from_url(clean_url)
    house = {
        "id": str(uuid.uuid4()),
        "address": address or (clean_url.split("//")[-1].split("/")[0] + " listing"),
        "addressIsGuessed": address is None,
        "url": clean_url,
        "photoUrl": "",
        "price": None,
        "priceHistory": [],
        "beds": None,
        "baths": None,
        "status": "Interested",
        "rating": 0,
        "notes": "",
        "liked": True,
        "source": "email",
        "addedBy": "",
        "dateAdded": date.today().isoformat(),
    }
    houses.append(house)
    return house


def add_liked_houses(numbers, last_listings, houses):
    existing_urls = {h.get("url") for h in houses if h.get("url")}
    existing_addrs = {h.get("address", "").strip().lower() for h in houses}
    added = []

    for n in numbers:
        idx = n - 1
        if idx < 0 or idx >= len(last_listings):
            continue
        listing = last_listings[idx]
        url = listing.get("url")
        addr = listing.get("address", "").strip().lower()
        if (url and url in existing_urls) or (addr and addr in existing_addrs):
            continue

        house = {
            "id": str(uuid.uuid4()),
            "address": listing.get("address", "Unknown address"),
            "url": url or "",
            "photoUrl": listing.get("photoUrl", ""),
            "price": listing.get("price"),
            "priceHistory": [],
            "beds": listing.get("beds"),
            "baths": listing.get("baths"),
            "status": "Interested",
            "rating": 0,
            "notes": "",
            "liked": True,
            "source": "email",
            "addedBy": "",
            "dateAdded": date.today().isoformat(),
        }
        houses.append(house)
        added.append(house)
        if url:
            existing_urls.add(url)
        existing_addrs.add(addr)

    return added


def process_mailbox():
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_pass:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set; skipping reply check.")
        return []

    last_listings = load_json(LAST_ALERT_PATH)
    houses = load_json(HOUSES_PATH)
    all_added = []

    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(gmail_user, gmail_pass)
        imap.select("INBOX")

        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            print("IMAP search failed.")
            return []

        message_ids = data[0].split()
        if not message_ids:
            print("No new mail.")
            return []

        for msg_id in message_ids:
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_subject(msg.get("Subject"))
            body = get_body_text(msg)

            handled = False

            if subject.lower().startswith("re:") and "daily listing alert" in subject.lower():
                numbers = parse_liked_numbers(body)
                if numbers:
                    added = add_liked_houses(numbers, last_listings, houses)
                    all_added.extend(added)
                    print(f"Message {msg_id.decode()}: liked listings {numbers} -> added {len(added)} house(s).")
                    handled = True

            if not handled:
                for m in LISTING_URL_RE.finditer(subject + " " + body):
                    house = add_house_from_url(m.group(0), houses)
                    if house:
                        all_added.append(house)
                        print(f"Message {msg_id.decode()}: added house from link -> {house['address']}")
                    handled = True

            if not handled:
                print(f"Message {msg_id.decode()}: no LIKE pattern or listing link found, skipping.")

            imap.store(msg_id, "+FLAGS", "\\Seen")

    if all_added:
        save_json(HOUSES_PATH, houses)

    return all_added


def main():
    added = process_mailbox()
    print(f"Added {len(added)} house(s) from email replies.")


if __name__ == "__main__":
    sys.exit(main())
