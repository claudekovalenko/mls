#!/usr/bin/env python3
"""Email digest: what turned up, short enough to read on a phone.

Each recipient gets one email for the markets they follow (the Markets
column on their Recipients row), holding the houses added or re-priced in
the last DIGEST_DAYS days, best first: a photo, the price, one line of facts,
one line of verdict, and links. Everything else lives in the app.

By default it stays silent when there's nothing new -- a daily "no houses"
email trains people to ignore the ones that matter. Set FORCE_SEND=1 to send
anyway (used for the kickoff email that announces the criteria).

Recipients live in the "Recipients" table (Email + Active), not in
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
  DIGEST_MARKETS  with EMAIL_TO: which markets that send covers
              (default: the non-private ones)
  DIGEST_DRY_RUN  "1" writes each email to DIGEST_OUT (default
              ./digest-preview) instead of sending; needs no SMTP login
"""
import html
import os
import recommend
import smtplib
import sys
import urllib.parse
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from db import connect, TABLE_CRITERIA, TABLE_HOUSES, TABLE_RECIPIENTS
from search_worker import zillow_url


def _money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


# Email HTML is not web HTML: Gmail strips <style> blocks and Outlook renders
# through Word, ignoring flexbox, grid and most positioning. So everything here
# is tables with inline styles in one 600px column. The only external asset
# is each card's photo, which Gmail fetches through its own image proxy.
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

# Statuses that mean the question is settled. Interested and Touring are
# deliberately absent: those are live and a price drop on one is exactly the
# email worth getting.
DECIDED_STATUSES = {"Under Contract", "Purchased", "Rejected"}

# Markets that belong to someone else's hunt. They live in the same table
# and run through the same worker, but only reach recipients whose Markets
# column names them, and never show in the app until unlocked there.
# Mirrors PRIVATE_MARKETS in docs/app.js.
PRIVATE_MARKETS = {"Orange County", "Los Angeles"}

# The app code that opens each private market, so the email's "open the
# app" button can land its reader straight on their own areas. Only people
# who follow a private market ever receive its email, so the code in the
# link goes to someone who already has it.
PRIVATE_MARKET_CODES = {"Orange County": "ivan", "Los Angeles": "ivan"}


def app_link(markets=()):
    codes = sorted({PRIVATE_MARKET_CODES[m] for m in markets if m in PRIVATE_MARKET_CODES})
    return APP_URL + (f"?open={urllib.parse.quote(codes[0])}" if len(codes) == 1 else "")


def is_private(f):
    return (f.get("Market") or "") in PRIVATE_MARKETS


# Verdict colours.
GOOD_FIT = "#166534"
GOOD_FIT_SOFT = "#dcf2e3"


OVERSIZED_LOT_SQFT = 15000   # mirrors search_worker; a lot with ADU room
DATED_BUILD_YEAR = 1985


def assess_fit(f, crit):
    """How this house measures against ONE criteria row.

    Returns (name, strategy, checks) where checks is a list of
    (label, status) and status is True (met), False (missed), or None
    (cannot be known from the data -- a basement, say). Unknowns are shown
    as unknowns rather than silently passed or failed, because "we can't
    see the basement from here" is honest and "no basement" is a lie.
    """
    cats = str(f.get("Value Signals") or "").lower()
    price, sqft, lot = f.get("Price"), f.get("Sqft"), f.get("Lot Sqft")
    ppsf, baths = f.get("Price Per Sqft"), f.get("Baths")
    year = f.get("Year Built")
    checks = []

    cap = crit.get("Max Price")
    if cap and price:
        checks.append((f"price {_money(price)} vs {_money(cap)} cap", price <= cap))

    ppsf_cap = crit.get("Max Price Per Sqft")
    if ppsf_cap:
        if not sqft:
            checks.append(("sqft unlisted (counts as under cap)", True))
        elif ppsf:
            checks.append((f"${ppsf:.0f}/sqft vs ${ppsf_cap:.0f} cap", ppsf <= ppsf_cap))

    min_units = crit.get("Min Units")
    if min_units:
        units = f.get("Units")
        if units:
            checks.append((f"{units:g} units vs {min_units:g}+ goal",
                           units >= min_units))
        else:
            checks.append(("unit count unlisted", None))

    musts = str(crit.get("Must Haves") or "").lower()
    if "basement" in musts:
        checks.append(("basement", True if "basement" in cats else None))
    if "adu" in musts or "lot" in musts or "acre" in musts:
        if lot:
            checks.append((f"lot room for ADU ({lot / 43560:.2f} acre)",
                           lot >= OVERSIZED_LOT_SQFT))
        else:
            checks.append(("lot room for ADU", None))

    target_sqft = crit.get("Target Total Sqft")
    if target_sqft and sqft:
        checks.append((f"{sqft:,.0f} sqft vs {target_sqft:,.0f}+ goal",
                       sqft >= target_sqft))

    target_baths = crit.get("Min Baths After Reno")
    if target_baths and baths is not None:
        # A house already at the target needs no bath added; short of it is
        # a reno line item, not a rejection -- but it is worth seeing.
        checks.append((f"{baths:g} baths now vs {target_baths:g}+ after reno",
                       baths >= target_baths))

    if crit.get("Strategy") == "Flip":
        fixer = (year and year <= DATED_BUILD_YEAR) or "days on market" in cats \
            or "price cut" in cats or "fixer" in cats
        checks.append(("fixer evidence (age / sitting / price cut)",
                       True if fixer else None))

    return crit.get("Name") or "Search", crit.get("Strategy") or "Either", checks


def fit_summary(f, criteria_rows):
    """All three assessments plus which one this house fits best.

    Best = highest share of KNOWN checks met, ties broken by more checks
    met, so a clean 3-of-3 beats a 4-of-6.
    """
    fits = []
    for rec in criteria_rows:
        crit = rec.get("fields", {})
        # A search is only ever judged against its own kind of building: a
        # multifamily row must not call a single-family house a fit however
        # many of its other boxes the house ticks, and vice versa.
        crit_lane = ("multifamily"
                     if (crit.get("Property Class") == "Multifamily"
                         or crit.get("Min Units"))
                     else "house")
        if crit_lane != lane_of(f):
            continue
        name, strategy, checks = assess_fit(f, crit)
        known = [s for _, s in checks if s is not None]
        met = sum(1 for s in known if s)
        # A blown price cap zeroes the score: a strategy you cannot afford is
        # not your best fit, however many other boxes the house ticks.
        over_cap = any(lab.startswith("price ") and s is False for lab, s in checks)
        score = 0 if over_cap else ((met / len(known)) if known else 0)
        fits.append({"name": name, "strategy": strategy, "checks": checks,
                     "met": met, "known": len(known), "score": score})
    # The five city-wide flip searches share one spec and differ only in
    # geography, which a fit check cannot see -- so they score every house
    # identically and would print as five identical rows. Collapse rows
    # whose shown name and checks match; one verdict per spec, not per city.
    seen, unique = set(), []
    for x in fits:
        key = (x["name"].split("—")[0].strip(), repr(x["checks"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(x)
    fits = unique
    best = max(fits, key=lambda x: (x["score"], x["met"])) if fits else None
    return fits, best


DROP = "#a2500c"        # a cut is the interesting direction, so it gets the hue
DROP_SOFT = "#f5e6d5"
RISE = "#5c6b69"        # a raise is worth knowing and worth not shouting about


def _triage_rows(houses, criteria_rows):
    """recommend.triage over the whole email's houses, strongest first.

    Computed once and shared, because the cap only means something if every
    part of the email agrees on who holds it -- a house called a top pick in
    one section and held back in another would read as a broken promise.
    """
    scored = []
    for rec in houses:
        f = rec.get("fields", {})
        _, best = fit_summary(f, criteria_rows)
        scored.append((f, best))
    rows = recommend.triage(scored)
    rows.sort(key=lambda r: -r["strength"])
    return rows


def lane_of(f):
    """Which of the two digests a house belongs in.

    Mirrors laneOf() in docs/app.js, and reads the house rather than the
    search that found it. "Found By" would be the obvious key and is the
    wrong one: it is a recently added field, so every house migrated from
    the old database has it empty, and a split on an empty field silently
    routes everything into one email. What a building *is* cannot go stale
    that way.
    """
    ptype = str(f.get("Property Type") or "").lower()
    found_by = str(f.get("Found By") or "").lower()
    units = f.get("Units") or 0
    if units >= 5 or "multifamily" in found_by or any(
            m in ptype for m in ("multi", "residential income", "apartment", "plex")):
        return "multifamily"
    return "house"


def _n(value):
    """A number from a field, or None. Rows arrive from Postgres as numbers,
    from hand edits as strings, and sometimes as "" -- the Sept 17 digest
    crashed formatting a house whose baths were missing, and the email
    never went out."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).replace(",", "").replace("$", "").strip()
        return float(text) if text else None
    except ValueError:
        return None


def _numeric_fields():
    from schema import SCHEMA
    return {name for name, kind in SCHEMA[TABLE_HOUSES] if kind == "number"}


NUMERIC_FIELDS = _numeric_fields()


def clean(rec):
    """A copy of a house record whose number fields are numbers or None.

    Everything downstream -- the fit checks, the triage score, the card --
    compares and formats these, and each one was its own chance to crash on
    a "1,200" or a "" that a hand edit or an old import left behind.
    Cleaning once at the door is what makes the rest safe to trust.
    """
    f = dict(rec.get("fields") or {})
    for name in NUMERIC_FIELDS & f.keys():
        f[name] = _n(f[name])
    return {**rec, "fields": f}


def _fmt(value):
    """3 -> "3", 2.5 -> "2.5", None -> "?"."""
    v = _n(value)
    return "?" if v is None else f"{v:g}"


def stats_line(f):
    """3 bd · 2 ba · 1,420 sqft · $457/sqft · built 1958 -- the facts a
    person scans before deciding to tap."""
    bits = []
    units = _n(f.get("Units"))
    if units:
        bits.append(f"{units:g} units")
    if _n(f.get("Beds")) is not None or _n(f.get("Baths")) is not None:
        bits.append(f"{_fmt(f.get('Beds'))} bd · {_fmt(f.get('Baths'))} ba")
    sqft = _n(f.get("Sqft"))
    bits.append(f"{sqft:,.0f} sqft" if sqft else "sqft not listed")
    ppsf = _n(f.get("Price Per Sqft"))
    if ppsf:
        bits.append(f"${ppsf:,.0f}/sqft")
    year = _n(f.get("Year Built"))
    if year:
        bits.append(f"built {year:.0f}")
    return " · ".join(bits)


def price_move(f):
    """(delta, pct) since the last run, or None when the price held."""
    old, new = _n(f.get("Previous Price")), _n(f.get("Price"))
    if not old or not new or old == new:
        return None
    return new - old, abs(new - old) / old * 100


def text_summary(new_houses, criteria_rows=()):
    """Plain text, shaped to be pasted into a text message.

    One house per short block, no table, no markdown -- iMessage renders
    neither. The link goes on its own line so it stays tappable.
    """
    lines = []
    new_houses = [clean(r) for r in new_houses]
    for rec in sorted(new_houses, key=_house_sort_key):
        f = rec.get("fields", {})
        lines.append(f"{f.get('Address') or '?'}")
        lines.append(f"  {_money(_n(f.get('Price')))} · {stats_line(f)}")
        move = price_move(f)
        if move:
            delta, pct = move
            lines.append(f"  PRICE {'DROP' if delta < 0 else 'RAISE'}: "
                         f"{_money(abs(delta))} ({pct:.1f}%)")
        lines.append("  Zillow: " + zillow_url(f.get("Address")))
        lines.append("")
    return "\n".join(lines).rstrip()


def _house_sort_key(rec):
    # Category count leads, because it is the part the data can actually
    # evidence. Flip profit is computed off a placeholder ARV until a human
    # types a real one, so sorting by it first would rank the list by a
    # number nobody has checked yet.
    f = rec.get("fields", {})
    cats = len([c for c in str(f.get("Value Signals") or "").split(",") if c.strip()])
    return (not f.get("Qualified"), -cats, -(_n(f.get("Flip Profit")) or -10**9))


# ------------------------------------------------------------------ photos

def aerial_url(f, w=560, h=220):
    """An overhead photo of the lot, from coordinates the feed already gives.

    RentCast carries no listing photos, which is why every card used to be a
    grey "see it from the street" button. Esri's public World Imagery export
    needs no key, no account and no card, and a roof-and-yard view is most
    of what a fixer or ADU hunt wants from a first glance anyway. Mirrors
    aerialPhoto() in docs/app.js.
    """
    import math
    lat, lng = _n(f.get("Latitude")), _n(f.get("Longitude"))
    if not lat or not lng:
        return ""
    d_lng = 0.0011
    d_lat = d_lng * (h / w) * math.cos(math.radians(lat))
    bbox = ",".join(f"{v:.6f}" for v in (lng - d_lng / 2, lat - d_lat / 2,
                                          lng + d_lng / 2, lat + d_lat / 2))
    return ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
            f"MapServer/export?bbox={bbox}&bboxSR=4326&imageSR=3857"
            f"&size={w},{h}&format=jpg&f=image")


def photo_url(f):
    """The best picture we can put on a card, and what it is.

    The feed's own photo first; a Street View still if someone has paid for
    a Google key; otherwise the free aerial. ("", None) only when a house
    has neither a photo nor a position.
    """
    if f.get("Photo URL"):
        return f["Photo URL"], "photo"
    key = os.environ.get("GOOGLE_MAPS_KEY")
    if key and f.get("Address"):
        q = urllib.parse.quote(str(f["Address"]))
        return (f"https://maps.googleapis.com/maps/api/streetview"
                f"?size=560x220&location={q}&fov=75&key={key}"), "street"
    aerial = aerial_url(f)
    return (aerial, "aerial") if aerial else ("", None)


def street_link(address):
    q = urllib.parse.quote(str(address or ""))
    return f"https://www.google.com/maps/search/?api=1&query={q}&layer=c"


# ------------------------------------------------------------------- cards

# How many houses one email shows. Past a dozen nobody reads on; the rest
# are one tap away in the app, sorted the same way.
MAX_CARDS = 12
MAX_CHIPS = 3
APP_URL = "https://claudekovalenko.github.io/mls/"

VERDICT_TONE = {
    recommend.SEE_IT: (GOOD_FIT, GOOD_FIT_SOFT),
    recommend.NEGOTIATE: (SIGNAL, SIGNAL_SOFT),
}


def _card(row):
    """One house: photo, address, price, facts, verdict, links. That's all."""
    f = row["fields"]
    addr = str(f.get("Address") or "?")
    # Zillow for every house, whatever found it: that is where the photos,
    # remarks and price history are. A source with its own page (a
    # HomeSteps foreclosure) gets a second link to that page.
    zillow = zillow_url(addr) if f.get("Address") else ""
    own = str(f.get("Listing URL") or "")
    own = "" if (not own or "zillow.com" in own) else own
    listing = zillow or own
    src, _kind = photo_url(f)

    photo = ""
    if src:
        photo = (f'<a href="{html.escape(listing)}"><img src="{html.escape(src)}" '
                 f'width="560" alt="Photo" style="display:block;'
                 f'width:100%;max-width:560px;height:auto;border:0;'
                 f'background:{TRACK};"></a>')

    move = price_move(f)
    move_html = ""
    if move:
        delta, pct = move
        arrow, fg = ("&darr;", DROP) if delta < 0 else ("&uarr;", RISE)
        move_html = (f'<span style="font-size:13px;font-weight:700;color:{fg};'
                     f'padding-left:8px;">{arrow} {_money(abs(delta))} '
                     f'({pct:.1f}%)</span>')

    action = row.get("action") or ""
    reasons = row.get("reasons") or []
    why = (reasons[-1] if row.get("held_back") else reasons[0]) if reasons else ""
    fg, bg = VERDICT_TONE.get(action, (MUTED, GROUND))
    verdict = ""
    if action:
        verdict = (f'<div style="margin-top:8px;font-size:12px;line-height:1.45;'
                   f'color:{MUTED};"><span style="background:{bg};color:{fg};'
                   f'border-radius:10px;padding:2px 8px;font-weight:700;">'
                   f'{html.escape(action)}</span>&nbsp; {html.escape(why)}</div>')

    cats = [c.strip() for c in str(f.get("Value Signals") or "").split(",") if c.strip()]
    chips = "".join(_chip(c, warm=any(m in c.lower() for m in WARM_MARKERS))
                    for c in cats[:MAX_CHIPS])
    chips = f'<div style="margin-top:8px;">{chips}</div>' if chips else ""

    link = f'color:{BRAND};font-weight:700;text-decoration:none;'
    button = (f'<td style="border:1.5px solid {BRAND};border-radius:6px;">'
              f'<a href="{html.escape(zillow)}" style="display:inline-block;'
              f'padding:7px 14px;{link}font-size:13px;">Zillow &rarr;</a></td>'
              ) if zillow else ""
    extra = (f'<a href="{html.escape(own)}" style="{link}">'
             f'{html.escape(_source_name(f))} &rarr;</a>&nbsp;&nbsp;&nbsp;') if own else ""
    links = (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
             f'style="margin-top:12px;"><tr>{button}'
             f'<td style="padding-left:14px;font-size:13px;">{extra}'
             f'<a href="{html.escape(street_link(addr))}" style="{link}">'
             f'Street view &rarr;</a></td></tr></table>')

    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:0 0 16px;border:1px solid {LINE};'
            f'background:#ffffff;"><tr><td>{photo}</td></tr>'
            f'<tr><td style="padding:12px 16px 14px;">'
            f'<div style="font-size:15px;font-weight:700;color:{INK};'
            f'line-height:1.3;">{html.escape(addr)}</div>'
            f'<div style="margin-top:4px;"><span style="font-family:{SERIF};'
            f'font-size:22px;font-weight:700;color:{INK};">'
            f'{_money(_n(f.get("Price")))}</span>{move_html}</div>'
            f'<div style="font-size:13px;color:{MUTED};margin-top:3px;">'
            f'{html.escape(stats_line(f))}</div>'
            f'{verdict}{chips}{links}</td></tr></table>')


def _source_name(f):
    return {"homesteps": "HomeSteps", "reso": "MLS"}.get(
        str(f.get("Source") or "").lower(), "Listing")


def _headline(houses, lane):
    noun = ("building", "buildings") if lane == "multifamily" else ("home", "homes")
    drops = sum(1 for r in houses if (price_move(r.get("fields", {})) or (0,))[0] < 0)
    fresh = len(houses) - drops
    parts = []
    if fresh:
        parts.append(f"{fresh} new {noun[fresh != 1]}")
    if drops:
        parts.append(f"{drops} price drop{'s' if drops != 1 else ''}")
    return " · ".join(parts) or f"No new {noun[1]}"


def build_email(criteria_rows, new_houses, markets=(), lane=None):
    """(subject, html). `markets` names the email; `lane` picks the noun."""
    new_houses = [clean(r) for r in new_houses]
    today = date.today().strftime("%b %-d")
    label = " + ".join(markets) if markets else "All markets"
    headline = _headline(new_houses, lane)
    subject = f"{label}: {headline}" if new_houses else f"{label}: searches are live"

    rows = _triage_rows(new_houses, criteria_rows) if new_houses else []
    shown, rest = rows[:MAX_CARDS], rows[MAX_CARDS:]
    cards = "".join(_card(r) for r in shown)
    if not rows:
        cards = (f'<div style="font-size:14px;color:{MUTED};padding:8px 0 16px;">'
                 f'Nothing new since the last email. The searches below are '
                 f'still running.</div>')
    more = (f"See all {len(rows)} in the app" if rest else "Open the app")
    more_note = (f'<div style="font-size:12px;color:{MUTED};padding-top:6px;">'
                 f'{len(rest)} more, same order, in the app.</div>') if rest else ""

    searches = sorted({str((r.get("fields") or {}).get("Name") or "")
                       for r in criteria_rows} - {""})
    aerial_used = any(photo_url(r["fields"])[1] == "aerial" for r in shown)
    credit = " &middot; Aerial imagery &copy; Esri" if aerial_used else ""

    body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:{GROUND};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{GROUND};padding:16px 8px;">
 <tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
         style="width:100%;max-width:600px;font-family:{SANS};">
    <tr><td style="padding:8px 4px 14px;">
      <div style="color:{BRAND};font-size:11px;font-weight:700;letter-spacing:1.4px;
                  text-transform:uppercase;">{html.escape(label)} &middot; {today}</div>
      <div style="font-family:{SERIF};color:{INK};font-size:26px;font-weight:700;
                  margin-top:6px;line-height:1.15;">{html.escape(headline)}</div>
      <div style="color:{MUTED};font-size:13px;margin-top:6px;">Best first.
        Tap a photo to open it on Zillow.</div>
    </td></tr>
    <tr><td>{cards}</td></tr>
    <tr><td align="center" style="padding:4px 0 18px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0">
        <tr><td style="background:{BRAND};border-radius:6px;">
          <a href="{html.escape(app_link(markets))}" style="display:inline-block;padding:11px 24px;color:#ffffff;
             font-size:14px;font-weight:700;text-decoration:none;">{more}</a>
        </td></tr>
      </table>{more_note}
    </td></tr>
    <tr><td style="padding:12px 4px 4px;border-top:1px solid {LINE};font-size:11px;
                   color:{MUTED};line-height:1.6;">
      Searching: {html.escape(" · ".join(searches) or "—")}<br>
      Searches run weekly &middot; change them in the app{credit}
    </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""
    return subject, body


# ---------------------------------------------------------------- routing

def recipient_markets(value):
    """A Markets cell -> set of market names; empty set means "the public ones"."""
    return {m.strip() for m in str(value or "").split(",") if m.strip()}


def resolve_recipients(at):
    """[(email, markets)] from the table, or from EMAIL_TO when set.

    Recipients come from the database, so they can be changed from a phone.
    EMAIL_TO still wins when set -- for a one-off send or a preview -- and
    covers DIGEST_MARKETS, defaulting to the non-private markets so an
    override can never leak a private market to someone by accident.
    """
    override = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if override:
        markets = recipient_markets(os.environ.get("DIGEST_MARKETS"))
        print(f"Recipients: {len(override)} from EMAIL_TO override, markets: "
              f"{', '.join(sorted(markets)) or 'public'}")
        return [(a, markets) for a in override]

    try:
        rows = at.list_records(TABLE_RECIPIENTS, formula="{Active}")
    except Exception as exc:
        print(f"::warning::Could not read the {TABLE_RECIPIENTS} table ({exc}).")
        return []

    good = []
    for rec in rows:
        f = rec.get("fields", {})
        email = (f.get("Email") or "").strip()
        if not _looks_like_email(email):
            print(f"::warning::Skipping {email or '(blank)'!r} in {TABLE_RECIPIENTS} "
                  f"-- not a valid address.")
            continue
        good.append((email, recipient_markets(f.get("Markets"))))
    print(f"Recipients: {len(good)} active")
    return good


def in_markets(f, markets):
    """Does this house or search belong in an email for `markets`?"""
    market = (f.get("Market") or "").strip()
    if markets:
        return market in markets
    return market not in PRIVATE_MARKETS


def worth_sending(f, cutoff, lane=None):
    """Newly listed or newly re-priced, still buyable, not already decided."""
    if lane and lane_of(f) != lane:
        return False
    # A house you can no longer buy is not news, including one the feed
    # itself reported as pending.
    if f.get("Listing Status") in ("Off Market", "Under Contract"):
        return False
    # Two ways in: newly listed, or its price moved. Anything else we
    # already emailed about, unchanged.
    is_new = str(f.get("Date Added") or "") >= cutoff
    moved = str(f.get("Price Change Date") or "") >= cutoff
    if not (is_new or moved):
        return False
    # Your own decisions (Under Contract, Purchased, Rejected) are not news.
    if f.get("Status") in DECIDED_STATUSES:
        return False
    # Costed by a human and failed both ways. NO DATA is not a rejection.
    return not (f.get("Flip Verdict") == "PASS" and f.get("BRRRR Verdict") == "PASS")


def plan_emails(recipients, criteria_rows, houses, cutoff, lane=None):
    """Group recipients who follow the same markets into one email each.

    Returns [(markets_label_list, [emails], criteria, houses)]. A group with
    no houses is still returned; main() decides whether to stay quiet.
    """
    groups = {}
    for email, markets in recipients:
        groups.setdefault(frozenset(markets), []).append(email)
    plans = []
    for markets, emails in groups.items():
        crit = [r for r in criteria_rows if in_markets(r.get("fields", {}), markets)]
        picked = [r for r in houses
                  if in_markets(r.get("fields", {}), markets)
                  and worth_sending(r.get("fields", {}), cutoff, lane)]
        names = sorted(markets) or sorted(
            {(r.get("fields", {}).get("Market") or "").strip() for r in crit} - {""})
        plans.append((names, emails, crit, picked))
    return plans


def _looks_like_email(value):
    """Cheap sanity check. A typo'd row shouldn't abort the whole send, but it
    also shouldn't be handed to the SMTP server as a recipient."""
    value = (value or "").strip()
    return "@" in value and "." in value.split("@")[-1] and " " not in value


def lane_from_env():
    """Which lane this workflow sends. DIGEST_SEARCH=Multifamily is the
    buildings email; DIGEST_EXCLUDE=Multifamily is the homes email."""
    only = os.environ.get("DIGEST_SEARCH", "").strip().lower()
    skip = os.environ.get("DIGEST_EXCLUDE", "").strip().lower()
    if "multifamily" in only:
        return "multifamily"
    if "multifamily" in skip:
        return "house"
    return None


def criteria_for_lane(rows, lane):
    if not lane:
        return rows
    def crit_lane(f):
        return ("multifamily" if (f.get("Property Class") == "Multifamily"
                                  or f.get("Min Units")) else "house")
    return [r for r in rows if crit_lane(r.get("fields", {})) == lane]


def main():
    dry = os.environ.get("DIGEST_DRY_RUN") == "1"
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not dry and not (user and password):
        # Fail loudly: a green run that sent nothing looks identical to
        # "nothing new today".
        missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASS", password)) if not v]
        print(f"::error::Email not configured; missing: {', '.join(missing)}")
        print("::error::Set these as repository secrets. For Gmail, SMTP_PASS must be "
              "an App Password (myaccount.google.com/apppasswords), not the account password.")
        return 1

    lane = lane_from_env()
    at = connect()
    recipients = resolve_recipients(at)
    if not recipients:
        print(f"::error::No recipients. Add a row to the {TABLE_RECIPIENTS} table "
              "with an Email and Active checked, or set EMAIL_TO for a one-off.")
        return 1
    criteria_rows = criteria_for_lane(
        at.list_records(TABLE_CRITERIA, formula="{Active}"), lane)
    days = int(os.environ.get("DIGEST_DAYS", "1") or "1")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    houses = at.list_records(TABLE_HOUSES)

    force = os.environ.get("FORCE_SEND") == "1"
    out_dir = os.environ.get("DIGEST_OUT", "digest-preview")
    failures = 0
    for names, emails, crit, picked in plan_emails(recipients, criteria_rows,
                                                   houses, cutoff, lane):
        label = " + ".join(names) or "public markets"
        if not picked and not force:
            print(f"{label}: nothing new -- staying quiet for {len(emails)} recipient(s).")
            continue
        if not crit and not picked:
            print(f"{label}: no active searches -- nothing to announce.")
            continue
        subject, body = build_email(crit, picked, names, lane)
        text = (text_summary(picked, crit) if picked else
                "Nothing new since the last email. " + APP_URL)
        if dry:
            os.makedirs(out_dir, exist_ok=True)
            slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
            path = os.path.join(out_dir, f"{lane or 'all'}-{slug}.html")
            with open(path, "w") as fh:
                fh.write(body)
            print(f"DRY RUN {subject!r} -> {path} ({len(picked)} house(s), "
                  f"{len(body) // 1024} KB) for {len(emails)} recipient(s)")
            continue
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(body, "html", "utf-8"))
        msg["Subject"] = subject
        msg["From"] = os.environ.get("EMAIL_FROM", user)
        msg["To"] = ", ".join(emails)
        try:
            host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
            port = int(os.environ.get("SMTP_PORT", "465"))
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
            print(f"Sent {subject!r} to {len(emails)} recipient(s).")
        except Exception as exc:  # noqa: BLE001 -- one group must not sink the rest
            failures += 1
            print(f"::error::{label}: send failed ({exc})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
