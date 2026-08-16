#!/usr/bin/env python3
"""Email digest: the criteria we're hunting with, and what turned up.

Sends one HTML email listing every Active search (so the recipient always
sees exactly what's being hunted) followed by the houses added in the last
DIGEST_DAYS days, best-first, with their value signals and verdicts.

By default it stays silent when there's nothing new -- a daily "no houses"
email trains people to ignore the ones that matter. Set FORCE_SEND=1 to send
anyway (used for the kickoff email that announces the criteria).

Recipients live in the Airtable "Recipients" table (Email + Active), not in
config, so the list can be changed from a phone without touching repo
settings -- adding a partner, an agent, or a lender for one deal is a normal
thing to do and shouldn't require editing a GitHub secret.

Credentials still have to be secrets, and only these two:
  SMTP_USER   the sending address (for Gmail: the account, with an App
              Password -- normal passwords won't work over SMTP)
  SMTP_PASS   the app password
  SMTP_HOST   default smtp.gmail.com
  SMTP_PORT   default 465 (SSL)
  EMAIL_TO    optional override, comma-separated; wins over the table when
              set, for a one-off send or a local test
"""
import html
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.text import MIMEText

from airtable import Airtable, TABLE_CRITERIA, TABLE_HOUSES, TABLE_RECIPIENTS


def _money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def _price_range(f):
    """A max-only search reads as "up to $500,000", not "—–$500,000"."""
    lo, hi = f.get("Min Price"), f.get("Max Price")
    if lo and hi:
        return f"{_money(lo)}–{_money(hi)}"
    if hi:
        return f"up to {_money(hi)}"
    if lo:
        return f"{_money(lo)}+"
    return "any price"


def criteria_block(rows):
    """Every constraint on the row, not just the numeric ones.

    The recipient is checking that what we're hunting matches what they
    asked for, so a field they specified and can't find here reads as
    "you dropped it" -- Keywords and the rehab allowance were doing exactly
    that. Notes carries the qualitative half of the brief (the ADU layout,
    the finish-the-basement plan) that no structured field can hold.
    """
    items = []
    for rec in rows:
        f = rec.get("fields", {})
        bits = []
        if f.get("Zip Codes"):
            zips = [z.strip() for z in str(f["Zip Codes"]).split(",") if z.strip()]
            bits.append(f"{len(zips)} zips: {', '.join(zips)}")
        elif f.get("City"):
            bits.append(f.get("City"))
        bits.append(_price_range(f))
        if f.get("Max Price Per Sqft"):
            bits.append(f"≤${f['Max Price Per Sqft']:.0f}/sqft (or sqft not listed)")
        if f.get("Max All In"):
            bits.append(f"≤{_money(f['Max All In'])} all-in (purchase + rehab)")
        if f.get("Rehab Cost Per Sqft"):
            bits.append(f"rehab budgeted at ${f['Rehab Cost Per Sqft']:.0f}/sqft")
        if f.get("Min Beds"):
            bits.append(f"{f['Min Beds']:g}+ bd")
        if f.get("Min Baths"):
            bits.append(f"{f['Min Baths']:g}+ ba")
        if f.get("Min Sqft"):
            bits.append(f"{f['Min Sqft']:,.0f}+ sqft")
        if f.get("Must Haves"):
            bits.append(f"must have: {f['Must Haves']}")
        if f.get("Keywords"):
            bits.append(f"listing must read like: {f['Keywords']}")
        if f.get("Target Total Sqft"):
            bits.append(f"goal {f['Target Total Sqft']:,.0f}+ sqft after reno")
        if f.get("Min Baths After Reno"):
            bits.append(f"{f['Min Baths After Reno']:g}+ baths after reno")
        if f.get("Target Flip Profit"):
            bits.append(f"target {_money(f['Target Flip Profit'])}+ flip profit")
        if f.get("Target Cash on Cash"):
            bits.append(f"{f['Target Cash on Cash']:g}%+ cash-on-cash")

        note = f.get("Notes")
        note_html = (f"<div style='color:#555;font-size:13px;margin:4px 0 0'>"
                     f"{html.escape(note)}</div>") if note else ""
        items.append(
            f"<li style='margin-bottom:10px'><strong>{html.escape(f.get('Name') or 'Search')}</strong>"
            f" ({html.escape(f.get('Strategy') or 'Either')})<br>"
            f"<span style='font-size:13px'>{html.escape(' · '.join(str(b) for b in bits))}</span>"
            f"{note_html}</li>"
        )
    return "<ul style='padding-left:18px'>" + "".join(items) + "</ul>"


# Restated in the email because the whole thesis is "value a normal buyer
# misses" -- a recipient seeing only price bands would think this is an
# ordinary MLS filter. Mirrors SIGNAL_RULES in search_worker.py.
SIGNALS_HTML = """
      <h3>What we flag as overlooked value</h3>
      <p style="font-size:13px;margin-top:4px">Every listing is tagged with any of these that apply, and
      two or more will surface a house even when the headline numbers look ordinary:</p>
      <ul style="font-size:13px;padding-left:18px">
        <li><strong>Fixer</strong> — as-is, TLC, needs work, investor special, estate sale, dated, original condition</li>
        <li><strong>Basement</strong> — especially unfinished, where usable sq. ft. can be added</li>
        <li><strong>ADU potential</strong> — in-law, guest house, kitchenette, separate entrance, detached garage</li>
        <li><strong>FSBO</strong> — for sale by owner</li>
        <li><strong>No sqft listed</strong> — missing square footage is an opportunity, not a disqualifier; these
        deliberately pass the $/sqft cap</li>
        <li><strong>Oversized lot</strong> — 15,000+ sq. ft., room to build</li>
      </ul>"""


def house_rows(houses):
    def sort_key(rec):
        # Category count leads, because it is the part the data can actually
        # evidence. Flip profit is computed off a placeholder ARV until a
        # human types a real one, so sorting by it first would rank the list
        # by a number nobody has checked yet.
        f = rec.get("fields", {})
        cats = len([c for c in str(f.get("Value Signals") or "").split(",") if c.strip()])
        return (not f.get("Qualified"), -cats, -(f.get("Flip Profit") or -10**9))
    rows = []
    for rec in sorted(houses, key=sort_key):
        f = rec.get("fields", {})
        addr = html.escape(f.get("Address") or "?")
        if f.get("Listing URL"):
            addr = f'<a href="{html.escape(f["Listing URL"])}">{addr}</a>'
        badge = " ⭐" if f.get("Qualified") else ""
        ppsf = f" · ${f['Price Per Sqft']:.0f}/sqft" if f.get("Price Per Sqft") else ""
        cats = [c.strip() for c in str(f.get("Value Signals") or "").split(",") if c.strip()]
        chips = "".join(
            f"<span style='background:#e0f2f0;color:#0b5d56;border-radius:10px;"
            f"padding:1px 7px;margin-right:4px;font-size:11px;white-space:nowrap'>"
            f"{html.escape(c)}</span>" for c in cats)
        beds = f"{f['Beds']:g}bd" if f.get("Beds") else "?bd"
        baths = f"{f['Baths']:g}ba" if f.get("Baths") else "?ba"
        sqft = f" · {f['Sqft']:,.0f} sqft" if f.get("Sqft") else " · no sqft listed"
        count_label = "category" if len(cats) == 1 else "categories"
        rows.append(
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:8px 10px'>{addr}{badge}<br>"
            f"<small>{html.escape(f.get('Market') or '')} · {_money(f.get('Price'))} · "
            f"{beds}/{baths}{sqft}{ppsf}</small>"
            + (f"<div style='margin-top:4px'>{chips}</div>" if chips else "")
            + f"</td><td style='padding:8px 10px;white-space:nowrap'>"
            f"<b>{len(cats)}</b> {count_label}<br>"
            f"<small>Flip: {html.escape(f.get('Flip Verdict') or '—')} · "
            f"BRRRR: {html.escape(f.get('BRRRR Verdict') or '—')}</small></td></tr>"
        )
    return "".join(rows)


def build_email(criteria_rows, new_houses):
    app_url = "https://claudekovalenko.github.io/mls/"
    if new_houses:
        subject = f"House Finder: {len(new_houses)} new match{'es' if len(new_houses) != 1 else ''}"
        houses_html = (
            f"<h3>New in the last run</h3>"
            f"<p style='font-size:13px;margin:4px 0 8px'>Ranked by how many of the "
            f"criteria below each one provably falls into. They don't need to hit "
            f"all of them &mdash; each chip is one the data actually evidences.</p>"
            f"<table style='border-collapse:collapse'>{house_rows(new_houses)}</table>"
        )
    else:
        subject = "House Finder: search criteria are live"
        houses_html = "<p>No new houses yet — the searches below are what the worker is hunting with.</p>"
    body = f"""
    <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px">
      <h2 style="color:#0f766e">House Finder</h2>
      {houses_html}
      <h3>What we're looking for</h3>
      {criteria_block(criteria_rows)}
      {SIGNALS_HTML}
      <h3>Known gaps</h3>
      <ul style="font-size:13px;padding-left:18px">
        <li><strong>FSBO / off-market</strong> can't be searched automatically — those listings never
        reach an MLS or data feed. Anything spotted in the wild gets added to the app by hand.</li>
        <li><strong>Resale value (ARV) and true rehab cost</strong> aren't knowable from a listing, so
        every new find starts with a placeholder and the profit figure means nothing until a human
        types real numbers in. That's the one judgement the system can't make for you.</li>
      </ul>
      <p><a href="{app_url}">Open the app</a> — tap any house to put in real rehab/ARV numbers
      and the verdicts recompute live.</p>
      <p style="color:#888"><small>⭐ = cleared its search's targets.</small></p>
    </div>"""
    return subject, body


def _looks_like_email(value):
    """Cheap sanity check. A typo'd row shouldn't abort the whole send, but it
    also shouldn't be handed to the SMTP server as a recipient."""
    value = (value or "").strip()
    return "@" in value and "." in value.split("@")[-1] and " " not in value


def resolve_recipients(at):
    """Recipients come from Airtable, so they can be changed from a phone.

    EMAIL_TO still works and wins when set -- useful for a one-off send to
    someone who shouldn't join the standing list, and for running this
    locally without touching the shared table.
    """
    override = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if override:
        print(f"Recipients: {len(override)} from EMAIL_TO override")
        return override

    try:
        rows = at.list_records(TABLE_RECIPIENTS, formula="{Active}")
    except Exception as exc:
        # A missing table is the expected first-run state, not a crash.
        print(f"::warning::Could not read the {TABLE_RECIPIENTS} table ({exc}).")
        return []

    good, bad = [], []
    for rec in rows:
        email = (rec.get("fields", {}).get("Email") or "").strip()
        (good if _looks_like_email(email) else bad).append(email or "(blank)")
    for entry in bad:
        print(f"::warning::Skipping {entry!r} in {TABLE_RECIPIENTS} -- not a valid address.")
    print(f"Recipients: {len(good)} active from Airtable")
    return good


def main():
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not (user and password):
        # Fail loudly, for the same reason the search worker does: returning 0
        # here paints the workflow green while no email is ever sent, and the
        # absence of an email looks identical to "nothing new today".
        missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASS", password)) if not v]
        print(f"::error::Email not configured; missing: {', '.join(missing)}")
        print("::error::Set these as repository secrets. For Gmail, SMTP_PASS must be "
              "an App Password (myaccount.google.com/apppasswords), not the account password.")
        return 1

    at = Airtable()
    to = resolve_recipients(at)
    if not to:
        print(f"::error::No recipients. Add a row to the {TABLE_RECIPIENTS} table in "
              "Airtable with an Email and Active checked, or set EMAIL_TO for a one-off.")
        return 1
    criteria_rows = at.list_records(TABLE_CRITERIA, formula="{Active}")

    days = int(os.environ.get("DIGEST_DAYS", "1"))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    new_houses = [
        rec for rec in at.list_records(TABLE_HOUSES)
        if (rec.get("fields", {}).get("Date Added") or "") >= cutoff
    ]

    if not new_houses and os.environ.get("FORCE_SEND") != "1":
        print("Nothing new and FORCE_SEND unset -- staying quiet.")
        return 0

    subject, body = build_email(criteria_rows, new_houses)
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM", user)
    msg["To"] = ", ".join(to)

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"Sent {subject!r} to {len(to)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
