#!/usr/bin/env python3
"""Email digest: the criteria we're hunting with, and what turned up.

Sends one HTML email listing every Active search (so the recipient always
sees exactly what's being hunted) followed by the houses added in the last
DIGEST_DAYS days, best-first, with their value signals and verdicts.

By default it stays silent when there's nothing new -- a daily "no houses"
email trains people to ignore the ones that matter. Set FORCE_SEND=1 to send
anyway (used for the kickoff email that announces the criteria).

SMTP config, via repo secrets:
  SMTP_USER   the sending address (for Gmail: the account, with an App
              Password -- normal passwords won't work over SMTP)
  SMTP_PASS   the app password
  EMAIL_TO    comma-separated recipients
  SMTP_HOST   default smtp.gmail.com
  SMTP_PORT   default 465 (SSL)

Unset config exits cleanly: the digest is an add-on, not something that
should fail the pipeline.
"""
import html
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.text import MIMEText

from airtable import Airtable, TABLE_CRITERIA, TABLE_HOUSES


def _money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def criteria_block(rows):
    items = []
    for rec in rows:
        f = rec.get("fields", {})
        bits = []
        if f.get("Zip Codes"):
            bits.append(f"zips {f['Zip Codes']}")
        elif f.get("City"):
            bits.append(f.get("City"))
        price = f"{_money(f.get('Min Price'))}–{_money(f.get('Max Price'))}"
        bits.append(price)
        if f.get("Max Price Per Sqft"):
            bits.append(f"≤${f['Max Price Per Sqft']:.0f}/sqft")
        if f.get("Max All In"):
            bits.append(f"≤{_money(f['Max All In'])} all-in")
        if f.get("Must Haves"):
            bits.append(f"must have: {f['Must Haves']}")
        if f.get("Target Total Sqft"):
            bits.append(f"goal {f['Target Total Sqft']:.0f}+ sqft after reno")
        if f.get("Min Baths After Reno"):
            bits.append(f"{f['Min Baths After Reno']:g}+ baths after reno")
        items.append(
            f"<li><strong>{html.escape(f.get('Name') or 'Search')}</strong>"
            f" ({html.escape(f.get('Strategy') or 'Either')}) — "
            f"{html.escape(' · '.join(str(b) for b in bits))}</li>"
        )
    return "<ul>" + "".join(items) + "</ul>"


def house_rows(houses):
    def sort_key(rec):
        f = rec.get("fields", {})
        return (not f.get("Qualified"), -(f.get("Flip Profit") or -10**9))
    rows = []
    for rec in sorted(houses, key=sort_key):
        f = rec.get("fields", {})
        addr = html.escape(f.get("Address") or "?")
        if f.get("Listing URL"):
            addr = f'<a href="{html.escape(f["Listing URL"])}">{addr}</a>'
        badge = " ⭐" if f.get("Qualified") else ""
        ppsf = f" · ${f['Price Per Sqft']:.0f}/sqft" if f.get("Price Per Sqft") else ""
        signals = html.escape(f.get("Value Signals") or "")
        rows.append(
            f"<tr><td style='padding:6px 10px'>{addr}{badge}<br>"
            f"<small>{html.escape(f.get('Market') or '')} · {_money(f.get('Price'))}{ppsf}"
            f"{(' · <b>' + signals + '</b>') if signals else ''}</small></td>"
            f"<td style='padding:6px 10px'>Flip: {html.escape(f.get('Flip Verdict') or '—')}<br>"
            f"BRRRR: {html.escape(f.get('BRRRR Verdict') or '—')}</td></tr>"
        )
    return "".join(rows)


def build_email(criteria_rows, new_houses):
    app_url = "https://claudekovalenko.github.io/mls/"
    if new_houses:
        subject = f"House Finder: {len(new_houses)} new match{'es' if len(new_houses) != 1 else ''}"
        houses_html = (
            f"<h3>New in the last run</h3>"
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
      <p><a href="{app_url}">Open the app</a> — tap any house to put in real rehab/ARV numbers
      and the verdicts recompute live.</p>
      <p style="color:#888"><small>Flip/BRRRR verdicts on new finds use placeholder rehab and ARV
      until someone enters real numbers. ⭐ = cleared its search's targets.</small></p>
    </div>"""
    return subject, body


def main():
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not (user and password and to):
        # Fail loudly, for the same reason the search worker does: returning 0
        # here paints the workflow green while no email is ever sent, and the
        # absence of an email looks identical to "nothing new today".
        missing = [name for name, value in
                   (("SMTP_USER", user), ("SMTP_PASS", password), ("EMAIL_TO", to))
                   if not value]
        print(f"::error::Email not configured; missing: {', '.join(missing)}")
        print("::error::Set these as repository secrets. For Gmail, SMTP_PASS must be "
              "an App Password (myaccount.google.com/apppasswords), not the account password.")
        return 1

    at = Airtable()
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
