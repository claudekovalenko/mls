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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from airtable import Airtable, TABLE_CRITERIA, TABLE_HOUSES, TABLE_RECIPIENTS
from search_worker import zillow_url


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
        note_html = (f'<div style="color:{MUTED};font-size:12px;line-height:1.5;'
                     f'margin:4px 0 0;font-style:italic;">{html.escape(note)}</div>'
                     ) if note else ""
        items.append(
            f'<tr><td style="padding:0 0 12px;">'
            f'<div style="font-size:14px;font-weight:700;color:{INK};">'
            f'{html.escape(f.get("Name") or "Search")}'
            f'<span style="font-size:11px;font-weight:600;color:#0b5d56;background:{SOFT};'
            f'border-radius:10px;padding:2px 8px;margin-left:6px;">'
            f'{html.escape(f.get("Strategy") or "Either")}</span></div>'
            f'<div style="font-size:12px;color:{MUTED};line-height:1.55;margin-top:3px;">'
            f'{html.escape(" · ".join(str(b) for b in bits))}</div>'
            f'{note_html}</td></tr>'
        )
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'border="0">' + "".join(items) + "</table>")


# Restated in the email because the whole thesis is "value a normal buyer
# misses" -- a recipient seeing only price bands would think this is an
# ordinary MLS filter. Mirrors SIGNAL_RULES in search_worker.py.
# Email HTML is not web HTML: Gmail strips <style> blocks and Outlook renders
# through Word, ignoring flexbox, grid and most positioning. So everything here
# is tables with inline styles, one 600px column, and no external assets.
# The palette and type mirror the deal-sheet artifact: warm-grey ground, teal
# accent, ochre for the discount signal, and a serif for addresses and prices.
# Georgia stands in for Fraunces because web fonts don't survive email clients.
BRAND = "#0f766e"
INK = "#16211f"
MUTED = "#5c6b69"
LINE = "#e0e6e3"
SOFT = "#e4f1ee"
GROUND = "#f4f6f4"
SIGNAL = "#a2500c"       # the discount/motivation hue, same as the deal sheet
SIGNAL_SOFT = "#f5e6d5"
TRACK = "#e8ece9"
SERIF = "Georgia,'Times New Roman',serif"
SANS = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif")


def _chip(text, warm=False):
    bg, fg = (SIGNAL_SOFT, SIGNAL) if warm else (SOFT, "#0b5d56")
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'border-radius:12px;padding:3px 9px;margin:0 4px 4px 0;font-size:12px;'
            f'font-weight:600;line-height:1.4;white-space:nowrap;">{html.escape(text)}</span>')


# Chips describing the seller's situation rather than the house get the warm
# hue, so motivation reads differently from geometry at a glance.
WARM_MARKERS = ("price cut", "days on market", "fsbo", "built ")


def _discount_pct(cats):
    """The 'N% under area $/sqft' figure, if the scorer wrote one."""
    for c in cats:
        if "% under area" in c:
            try:
                return int(c.split("%")[0].strip())
            except ValueError:
                return None
    return None


def _discount_bar(pct):
    """The deal sheet's discount bar, rebuilt as two table cells.

    Outlook can't draw a styled div, but it can colour two <td>s whose widths
    split at the percentage. Doubled so a strong 40% discount reads as a
    mostly-full bar rather than a mostly-empty one.
    """
    fill = max(2, min(96, pct * 2))
    return f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="margin:9px 0 2px;">
          <tr>
            <td style="padding:0 0 4px;font-size:11px;font-weight:700;color:{SIGNAL};
                       letter-spacing:0.4px;">UNDER AREA MEDIAN</td>
            <td align="right" style="padding:0 0 4px;font-size:11px;font-weight:700;
                       color:{SIGNAL};">{pct}%</td>
          </tr>
        </table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td width="{fill}%" bgcolor="{SIGNAL}" style="font-size:0;line-height:5px;height:5px;">&nbsp;</td>
            <td bgcolor="{TRACK}" style="font-size:0;line-height:5px;height:5px;">&nbsp;</td>
          </tr>
        </table>"""


def _house_card(f):
    """One house, as a self-contained table so it survives every client."""
    cats = [c.strip() for c in str(f.get("Value Signals") or "").split(",") if c.strip()]
    link = f.get("Listing URL") or zillow_url(f.get("Address"))
    addr = html.escape(f.get("Address") or "?")

    stats = []
    if f.get("Beds") or f.get("Baths"):
        stats.append(f"{f.get('Beds') or '?'} bd / {f.get('Baths') or '?'} ba")
    stats.append(f"{f['Sqft']:,.0f} sqft" if f.get("Sqft") else "sqft not listed")
    if f.get("Price Per Sqft"):
        stats.append(f"${f['Price Per Sqft']:,.0f}/sqft")
    if f.get("Year Built"):
        stats.append(f"built {f['Year Built']:.0f}")
    if f.get("Days on Market"):
        stats.append(f"{f['Days on Market']:.0f} days on market")

    star = ('<span style="background:#fef3c7;color:#92400e;border-radius:10px;'
            'padding:2px 8px;font-size:11px;font-weight:700;margin-left:6px;">'
            'MEETS TARGETS</span>') if f.get("Qualified") else ""

    pct = _discount_pct(cats)
    bar = _discount_bar(pct) if pct else ""
    chips = "".join(
        _chip(c, warm=any(m in c.lower() for m in WARM_MARKERS)) for c in cats)

    # A bordered table cell, not a CSS button: Outlook drops padding on <a>.
    button = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="border:1px solid {BRAND};border-radius:6px;">'
        f'<a href="{html.escape(link)}" style="display:inline-block;padding:9px 18px;'
        f'color:{BRAND};font-size:13px;font-weight:700;text-decoration:none;'
        f'letter-spacing:0.3px;">View on Zillow &rarr;</a></td></tr></table>') if link else ""

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="margin:0 0 14px;border:1px solid {LINE};background:#ffffff;">
      <tr><td style="padding:18px 20px;">
        <div style="font-family:{SERIF};font-size:18px;font-weight:700;color:{INK};
                    line-height:1.3;">{addr}{star}</div>
        <div style="margin:7px 0 2px;">
          <span style="font-family:{SERIF};font-size:26px;font-weight:700;
                       color:{INK};">{_money(f.get('Price'))}</span>
        </div>
        <div style="font-size:13px;color:{MUTED};line-height:1.5;">{html.escape(' · '.join(stats))}</div>
        {bar}
        <div style="margin:11px 0 13px;">{chips}</div>
        {button}
      </td></tr>
    </table>"""


def house_rows(houses):
    return "".join(_house_card(rec.get("fields", {}))
                   for rec in sorted(houses, key=_house_sort_key))


SIGNALS_HTML = f"""
      <h3 style="font-size:15px;color:{INK};margin:26px 0 6px;">What the tags mean</h3>
      <p style="font-size:13px;color:{MUTED};margin:0 0 10px;line-height:1.5;">
        Each house is tagged with the criteria it provably falls into. Two or more
        surfaces it &mdash; they don't need to hit all of them.</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="font-size:13px;color:{MUTED};line-height:1.55;">
        <tr><td style="padding:2px 0;"><b style="color:{INK};">under $X/sqft</b> &mdash; beats that search's price-per-foot ceiling</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{INK};">N% under area $/sqft</b> &mdash; cheaper per foot than the median of everything else that search pulled right now</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{INK};">oversized lot</b> &mdash; room to build an ADU</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{INK};">no sqft listed</b> &mdash; missing data other buyers skip past</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{INK};">$Xk all-in</b> &mdash; purchase plus budgeted rehab clears the cap</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{SIGNAL};">built 19XX</b> &mdash; dated, original-condition stock (1985 or earlier)</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{SIGNAL};">N days on market</b> &mdash; being passed over; the area's typical time to contract is ~3 weeks</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{SIGNAL};">price cut N%</b> &mdash; the seller's own statement about motivation</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{SIGNAL};">possible FSBO</b> &mdash; no listing agent or office on the record</td></tr>
        <tr><td style="padding:2px 0;"><b style="color:{INK};">fixer / unfinished basement / ADU potential</b> &mdash; read from the listing remarks when the feed carries them</td></tr>
      </table>"""


def text_summary(new_houses):
    """Plain text, shaped to be pasted into a text message.

    One house per short block, no table, no markdown -- iMessage renders
    neither, and a wrapped table is unreadable on a phone. The Zillow link
    goes on its own line so it stays tappable instead of being swallowed by
    surrounding punctuation.
    """
    lines = []
    for rec in sorted(new_houses, key=_house_sort_key):
        f = rec.get("fields", {})
        cats = [c.strip() for c in str(f.get("Value Signals") or "").split(",") if c.strip()]
        bits = [_money(f.get("Price"))]
        if f.get("Beds") or f.get("Baths"):
            bits.append(f"{f.get('Beds') or '?':g}bd/{f.get('Baths') or '?':g}ba"
                        if isinstance(f.get("Beds"), (int, float)) else "")
        if f.get("Sqft"):
            bits.append(f"{f['Sqft']:,.0f} sqft")
        else:
            bits.append("sqft not listed")
        if f.get("Price Per Sqft"):
            bits.append(f"${f['Price Per Sqft']:.0f}/sqft")
        star = " *" if f.get("Qualified") else ""
        lines.append(f"{f.get('Address') or '?'}{star}")
        lines.append("  " + " · ".join(b for b in bits if b))
        if cats:
            lines.append(f"  Why: {', '.join(cats)}")
        lines.append("  " + (f.get("Listing URL") or zillow_url(f.get("Address"))))
        lines.append("")
    return "\n".join(lines).rstrip()


def _house_sort_key(rec):
    # Category count leads, because it is the part the data can actually
    # evidence. Flip profit is computed off a placeholder ARV until a human
    # types a real one, so sorting by it first would rank the list by a
    # number nobody has checked yet.
    f = rec.get("fields", {})
    cats = len([c for c in str(f.get("Value Signals") or "").split(",") if c.strip()])
    return (not f.get("Qualified"), -cats, -(f.get("Flip Profit") or -10**9))


def build_email(criteria_rows, new_houses):
    app_url = "https://claudekovalenko.github.io/mls/"
    today = date.today().strftime("%b %-d")

    if new_houses:
        n = len(new_houses)
        subject = f"House Finder: {n} new match{'es' if n != 1 else ''}"
        headline = f"{n} new match{'es' if n != 1 else ''}"
        sub = ("Ranked by how many of your criteria each one provably falls into. "
               "Tap through for photos and the full listing.")
        content = house_rows(new_houses)
    else:
        subject = "House Finder: search criteria are live"
        headline = "No new matches today"
        sub = "The searches below ran and found nothing new. Here's what they're hunting for."
        content = ""

    return subject, f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:{GROUND};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{GROUND};padding:24px 10px;">
 <tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
         style="width:100%;max-width:600px;background:#ffffff;
                font-family:{SANS};">

    <tr><td style="padding:26px 24px 20px;border-bottom:2px solid {INK};">
      <div style="color:{BRAND};font-size:11px;font-weight:700;letter-spacing:1.6px;
                  text-transform:uppercase;">House Finder &middot; Deal Sheet &middot; {today}</div>
      <div style="font-family:{SERIF};color:{INK};font-size:30px;font-weight:700;
                  margin-top:8px;line-height:1.1;">{headline}</div>
      <div style="color:{MUTED};font-size:13px;margin-top:9px;line-height:1.55;">{sub}</div>
    </td></tr>

    <tr><td style="padding:20px 18px 4px;">
      {content}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td align="center" style="padding:8px 0 18px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="border:1.5px solid {BRAND};border-radius:6px;">
              <a href="{app_url}" style="display:inline-block;padding:10px 22px;color:{BRAND};
                 font-size:13px;font-weight:700;text-decoration:none;">Open the app</a>
            </td></tr>
          </table>
          <div style="font-size:12px;color:{MUTED};padding-top:8px;">
            Enter real rehab and resale numbers there and every verdict recalculates live.
          </div>
        </td></tr>
      </table>
    </td></tr>

    <tr><td style="padding:0 24px;border-top:1px solid {LINE};">
      <h3 style="font-size:15px;color:{INK};margin:22px 0 8px;">What we're looking for</h3>
      {criteria_block(criteria_rows)}
      {SIGNALS_HTML}
      <h3 style="font-size:15px;color:{INK};margin:26px 0 6px;">What this can't see</h3>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="font-size:13px;color:{MUTED};line-height:1.55;">
        <tr><td style="padding:3px 0;"><b style="color:{INK};">FSBO and off-market</b> never reach a
          data feed, so they can't be searched. Anything spotted in the wild gets added by hand.</td></tr>
        <tr><td style="padding:3px 0;"><b style="color:{INK};">Renovation cost and resale value</b>
          aren't in any listing. Until someone enters real figures, no profit number here means
          anything &mdash; which is why houses arrive uncosted rather than pre-judged.</td></tr>
      </table>
      <div style="height:22px;"></div>
    </td></tr>

    <tr><td style="background:{GROUND};border-top:1px solid {LINE};padding:14px 24px;
                   font-size:11px;color:{MUTED};line-height:1.5;text-align:center;">
      Sent by House Finder &middot; searches run weekly &middot;
      change recipients or criteria in Airtable
    </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


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
    def worth_sending(rec):
        f = rec.get("fields", {})
        if (f.get("Date Added") or "") < cutoff:
            return False
        # A house someone costed and rejected stays out of the email. NO DATA
        # is not a rejection -- it means nobody has put real numbers in yet.
        return not (f.get("Flip Verdict") == "PASS" and f.get("BRRRR Verdict") == "PASS")

    new_houses = [rec for rec in at.list_records(TABLE_HOUSES) if worth_sending(rec)]

    if not new_houses and os.environ.get("FORCE_SEND") != "1":
        print("Nothing new and FORCE_SEND unset -- staying quiet.")
        return 0

    subject, body = build_email(criteria_rows, new_houses)
    # Both parts, HTML preferred. The text half is what survives being
    # forwarded into a text message, and multipart/alternative is also what
    # keeps a plain-HTML blast out of spam filters.
    msg = MIMEMultipart("alternative")
    text = text_summary(new_houses) if new_houses else \
        "No new houses yet -- the searches are running. " \
        "https://claudekovalenko.github.io/mls/"
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))
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
