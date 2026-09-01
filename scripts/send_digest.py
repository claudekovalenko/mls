#!/usr/bin/env python3
"""Email digest: the criteria we're hunting with, and what turned up.

Sends one HTML email listing every Active search (so the recipient always
sees exactly what's being hunted) followed by the houses added in the last
DIGEST_DAYS days, best-first, with their value signals and verdicts.

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

# Feed names in words a person recognises. An empty Source means nobody's
# adapter wrote the row -- a human typed it in.
# Statuses that mean the question is settled. Interested and Touring are
# deliberately absent: those are live and a price drop on one is exactly the
# email worth getting.
DECIDED_STATUSES = {"Under Contract", "Purchased", "Rejected"}

SOURCE_LABELS = {"rentcast": "RentCast", "reso": "MLS / IDX", "search": "search",
                 "manual": "typed in by hand"}

# Verdict colours for the per-strategy fit rows.
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


def _fit_rows_html(fits, best):
    """The per-strategy scorecard on a card: one row per search, met/known,
    and each miss or unknown named -- the point is knowing what to check on
    the walkthrough, not just a number."""
    rows = []
    for fit in fits:
        is_best = best is not None and fit["name"] == best["name"]
        badge = (f'<span style="background:{GOOD_FIT_SOFT};color:{GOOD_FIT};'
                 f'border-radius:9px;padding:1px 7px;font-size:10px;font-weight:700;'
                 f'margin-left:6px;">BEST FIT</span>') if is_best and fit["score"] > 0 else ""
        # Short name: "Flip", "BRRRR A", "BRRRR B" read faster than full row names.
        short = fit["name"].split("—")[0].strip()
        misses = [lab for lab, s in fit["checks"] if s is False]
        unknowns = [lab for lab, s in fit["checks"] if s is None]
        detail = ""
        if misses:
            detail += f'<span style="color:{SIGNAL};">misses: {html.escape("; ".join(misses))}</span>'
        if unknowns:
            if detail:
                detail += " &middot; "
            detail += f'<span style="color:{MUTED};">to verify: {html.escape("; ".join(unknowns))}</span>'
        if not detail:
            detail = f'<span style="color:{GOOD_FIT};">meets everything we can measure</span>'
        pct_color = GOOD_FIT if fit["score"] >= 0.75 else (SIGNAL if fit["score"] >= 0.4 else MUTED)
        rows.append(
            f'<tr><td style="padding:3px 0;font-size:12px;line-height:1.5;'
            f'border-top:1px solid {LINE};">'
            f'<b style="color:{INK};">{html.escape(short)}</b>'
            f'<span style="color:{pct_color};font-weight:700;padding-left:6px;">'
            f'{fit["met"]}/{fit["known"]}</span>{badge}'
            f'<br><span style="font-size:11px;">{detail}</span></td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:4px 0 12px;">'
            f'<tr><td style="padding:0 0 4px;font-size:10px;font-weight:700;'
            f'letter-spacing:0.8px;color:{MUTED};">FIT BY STRATEGY</td></tr>'
            + "".join(rows) + "</table>")


def _street_view(address):
    """How the house looks from the road -- by link for free, by image if paid.

    Embedding a Street View still requires a Google Maps API key, and getting
    a key requires enabling billing on a Google Cloud project, which means a
    card on file even when usage never leaves the free allowance. So the
    default is a plain Maps link: no key, no account, no card, and it opens
    the same curb view one tap away. Set GOOGLE_MAPS_KEY only if you'd rather
    have the picture inline and have accepted that trade.
    """
    if not address:
        return ""
    q = urllib.parse.quote(str(address))
    key = os.environ.get("GOOGLE_MAPS_KEY")
    if key:
        url = (f"https://maps.googleapis.com/maps/api/streetview"
               f"?size=560x240&location={q}&fov=75&key={key}")
        return (f'<img src="{html.escape(url)}" width="560" alt="Street view of '
                f'{html.escape(str(address))}" style="display:block;width:100%;'
                f'max-width:560px;height:auto;border:1px solid {LINE};'
                f'margin:0 0 12px;">')
    link = f"https://www.google.com/maps/search/?api=1&query={q}&layer=c"
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="margin:0 0 12px;">'
            f'<tr><td style="border:1px solid {LINE};background:{SOFT};'
            f'text-align:center;">'
            f'<a href="{html.escape(link)}" style="display:block;padding:12px 16px;'
            f'color:{BRAND};font-size:12px;font-weight:700;text-decoration:none;'
            f'letter-spacing:0.3px;">&#128739; See it from the street &rarr;</a>'
            f'</td></tr></table>')


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


def _strength_chip(value):
    return (f'<span style="background:{INK};color:#ffffff;border-radius:10px;'
            f'padding:2px 8px;font-size:11px;font-weight:700;margin-left:8px;'
            f'vertical-align:middle;">{value}</span>')


def _picks_html(houses, criteria_rows):
    """Where to start: the few houses worth acting on, with the play.

    A digest of forty-five houses ranked by fit answers "which of these
    match". This answers "what do I do on Saturday", which is the question
    somebody actually opens the email with.
    """
    rows = _triage_rows(houses, criteria_rows)
    order = {recommend.SEE_IT: 0, recommend.NEGOTIATE: 1}
    chosen = sorted((r for r in rows if r["action"] in order),
                    key=lambda r: (order[r["action"]], -r["strength"]))[:3]
    if not chosen:
        return ""

    blocks = []
    for i, pick in enumerate(chosen, 1):
        f, best = pick["fields"], pick["best"]
        name, play, numbers = recommend.approach(f, best)
        steps = recommend.next_steps(f, pick["action"], best)
        step_html = "".join(
            f'<tr><td style="padding:2px 0 2px 14px;font-size:12px;color:{MUTED};'
            f'line-height:1.5;">{n}. {html.escape(t)}</td></tr>'
            for n, t in enumerate(steps, 1))
        blocks.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:0 0 12px;border:1px solid {LINE};'
            f'background:#ffffff;"><tr><td style="padding:14px 16px;">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:0.8px;'
            f'color:{BRAND};">{i} &middot; {html.escape(pick["action"]).upper()}</div>'
            f'<div style="font-family:{SERIF};font-size:17px;font-weight:700;'
            f'color:{INK};margin-top:4px;">{html.escape(str(f.get("Address") or ""))}</div>'
            f'<div style="font-size:12px;color:{MUTED};margin-top:5px;line-height:1.55;">'
            f'<b style="color:{INK};">{html.escape(name)}.</b> {html.escape(play)}</div>'
            f'<div style="font-size:12px;color:{SIGNAL};margin-top:6px;font-weight:600;">'
            f'{" &middot; ".join(html.escape(n) for n in numbers)}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin-top:9px;"><tr><td style="padding:0 0 3px;'
            f'font-size:10px;font-weight:700;letter-spacing:0.7px;color:{MUTED};">'
            f'NEXT STEPS</td></tr>{step_html}</table>'
            f'</td></tr></table>')

    return (f'<tr><td style="padding:18px 18px 2px;">'
            f'<div style="font-family:{SERIF};font-size:20px;font-weight:700;'
            f'color:{INK};margin-bottom:3px;">Where to start</div>'
            f'<div style="font-size:12px;color:{MUTED};margin-bottom:12px;'
            f'line-height:1.55;">Still on the market, and the evidence supports '
            f'doing something about them this week.</div>'
            f'{"".join(blocks)}</td></tr>')


def _recommendation_html(f, best, final=None):
    """The recommendation block: what to do, why, and what it cannot see.

    `final` is this house's triage row. Without it the block would re-judge
    the house alone and could contradict the cap -- claiming a viewing the
    triage already gave to somebody stronger.
    """
    if final is not None:
        action, reasons, caveats = final["action"], final["reasons"], final["caveats"]
    else:
        action, reasons, caveats = recommend.recommend(f, best)

    if action in (recommend.WATCH, recommend.SKIP):
        # The full panel is for houses that earn action. Everything else
        # gets its verdict in one line -- clean to scan, and the detail is a
        # tap away in the app.
        # For a held-back house the appended explanation is the whole story
        # -- "23% under" without "next in line" reads as the system ignoring
        # its own evidence.
        if final is not None and final.get("held_back") and reasons:
            first = reasons[-1]
        else:
            first = reasons[0] if reasons else ""
        strength_txt = (f' &middot; strength {final["strength"]}'
                        if final is not None else "")
        return (f'<div style="margin:2px 0 12px;padding:8px 12px;background:{GROUND};'
                f'font-size:12px;color:{MUTED};line-height:1.5;">'
                f'<b style="color:{INK};">{html.escape(action)}</b>'
                f'{strength_txt} &middot; {html.escape(first)}</div>')
    tone = {recommend.SEE_IT: (GOOD_FIT, GOOD_FIT_SOFT),
            recommend.NEGOTIATE: (SIGNAL, SIGNAL_SOFT)}.get(action, (MUTED, GROUND))
    fg, bg = tone
    why = "".join(
        f'<tr><td style="padding:1px 0 1px 12px;font-size:12px;color:{MUTED};'
        f'line-height:1.5;">&bull; {html.escape(r)}</td></tr>' for r in reasons)
    notes = "".join(
        f'<tr><td style="padding:1px 0 1px 12px;font-size:11px;color:{MUTED};'
        f'line-height:1.5;font-style:italic;">{html.escape(c)}</td></tr>'
        for c in caveats)
    name, play, numbers = recommend.approach(f, best)
    plan = (f'<tr><td style="padding:8px 12px 0;font-size:12px;color:{MUTED};'
            f'line-height:1.55;"><b style="color:{INK};">{html.escape(name)}.</b> '
            f'{html.escape(play)}</td></tr>'
            f'<tr><td style="padding:5px 12px 0;font-size:12px;font-weight:600;'
            f'color:{SIGNAL};">{" &middot; ".join(html.escape(n) for n in numbers)}</td></tr>')
    steps = "".join(
        f'<tr><td style="padding:2px 0 2px 12px;font-size:12px;color:{MUTED};'
        f'line-height:1.5;">{n}. {html.escape(t)}</td></tr>'
        for n, t in enumerate(recommend.next_steps(f, action, best), 1))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:2px 0 12px;background:{bg};">'
            f'<tr><td style="padding:10px 12px 4px;font-size:13px;font-weight:700;'
            f'color:{fg};">{html.escape(action)}</td></tr>'
            f'{why}'
            f'{plan}'
            f'<tr><td style="padding:8px 0 3px 12px;font-size:10px;font-weight:700;'
            f'letter-spacing:0.7px;color:{MUTED};">NEXT STEPS</td></tr>'
            f'{steps}'
            f'<tr><td style="padding:8px 0 0 12px;font-size:10px;font-weight:700;'
            f'letter-spacing:0.6px;color:{MUTED};">WHAT THIS CANNOT SEE</td></tr>'
            f'{notes}'
            f'<tr><td style="height:10px;"></td></tr></table>')


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


def _price_change_html(f):
    """The actual dollars off, since the last time we looked.

    "Price cut" from the feed's own history is the whole life of the listing;
    this is the move that happened between two of our runs, which is the one
    that is news today. Shown in dollars first because that is the number
    people react to -- "$25,000 off" lands where "-6.1%" does not.
    """
    old, new = f.get("Previous Price"), f.get("Price")
    if not old or not new or old == new:
        return ""
    delta = new - old
    pct = abs(delta) / old * 100
    when = f.get("Price Change Date") or ""
    if delta < 0:
        label = f"&darr; {_money(abs(delta))} off &nbsp;·&nbsp; {pct:.1f}% cut"
        fg, bg = DROP, DROP_SOFT
    else:
        label = f"&uarr; {_money(delta)} up &nbsp;·&nbsp; {pct:.1f}%"
        fg, bg = RISE, GROUND
    was = f'was {_money(old)}' + (f' &nbsp;·&nbsp; changed {html.escape(when)}' if when else "")
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 8px;"><tr><td style="background:{bg};'
            f'border-radius:4px;padding:6px 10px;font-size:12px;font-weight:700;'
            f'color:{fg};">{label}<span style="font-weight:400;color:{MUTED};">'
            f'&nbsp;&nbsp;{was}</span></td></tr></table>')


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


def _house_card(f, criteria_rows=(), final=None):
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
    if f.get("Units"):
        stats.append(f"{f['Units']:.0f} units")
    if f.get("Year Built"):
        stats.append(f"built {f['Year Built']:.0f}")
    if f.get("Days on Market"):
        stats.append(f"{f['Days on Market']:.0f} days on market")
    # Which net caught this one. Zillow is the hub every house links to, so
    # this is the other half of the provenance -- and a hand-added house says
    # so, since that is the one whose numbers no feed has checked.
    # Only claim a source when one was actually recorded. Rows written
    # before the field existed have none, and labelling those "added by
    # hand" asserts something untrue about where the numbers came from --
    # which is exactly the thing a provenance line is supposed to settle.
    source = str(f.get("Source") or "").strip()
    finder = str(f.get("Found By") or "").split("—")[0].strip()
    if source:
        stats.append("found via " + SOURCE_LABELS.get(source, source)
                     + (f" — {finder}" if finder else ""))
    elif finder:
        stats.append(f"found by {finder}")

    def _days_ago(value, verb):
        ds = str(value or "")[:10]
        if not ds:
            return None
        try:
            n = (date.today() - date.fromisoformat(ds)).days
        except ValueError:
            return None
        if n < 0:
            return None
        return f"{verb} today" if n == 0 else \
            f"{verb} {n} day{'' if n == 1 else 's'} ago"
    added = _days_ago(f.get("Date Added"), "found")
    if added:
        stats.append(added)
    seen = _days_ago(f.get("Last Seen"), "checked")
    if seen and (not added or seen != added.replace("found", "checked", 1)):
        stats.append(seen)

    star = ('<span style="background:#fef3c7;color:#92400e;border-radius:10px;'
            'padding:2px 8px;font-size:11px;font-weight:700;margin-left:6px;">'
            'MEETS TARGETS</span>') if f.get("Qualified") else ""

    pct = _discount_pct(cats)
    bar = _discount_bar(pct) if pct else ""
    chips = "".join(
        _chip(c, warm=any(m in c.lower() for m in WARM_MARKERS)) for c in cats)

    photo = _street_view(f.get("Address"))
    fits, best = fit_summary(f, criteria_rows)
    fit_html = _fit_rows_html(fits, best) if fits else ""
    advice = _recommendation_html(f, best, final)
    best_badge = ""
    if best and best["score"] > 0:
        short = html.escape(best["name"].split("—")[0].strip())
        best_badge = (f'<span style="background:{GOOD_FIT_SOFT};color:{GOOD_FIT};'
                      f'border-radius:10px;padding:2px 9px;font-size:11px;'
                      f'font-weight:700;margin-left:8px;vertical-align:middle;">'
                      f'{short} · {best["met"]}/{best["known"]}</span>')

    change = _price_change_html(f)

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
        {photo}
        <div style="font-family:{SERIF};font-size:18px;font-weight:700;color:{INK};
                    line-height:1.3;">{addr}{star}</div>
        <div style="margin:7px 0 2px;">
          <span style="font-family:{SERIF};font-size:26px;font-weight:700;
                       color:{INK};">{_money(f.get('Price'))}</span>{best_badge}
        </div>
        {change}
        <div style="font-size:13px;color:{MUTED};line-height:1.5;">{html.escape(' · '.join(stats))}</div>
        {bar}
        <div style="margin:11px 0 10px;">{chips}</div>
        {advice}
        {fit_html}
        {button}
      </td></tr>
    </table>"""


def house_rows(houses, criteria_rows=()):
    # Hottest first, judged by the same triage the picks use, so the order
    # of the list and the order of the advice never disagree.
    rows = _triage_rows(houses, criteria_rows)
    return "".join(_house_card(r["fields"], criteria_rows, final=r)
                   for r in rows)


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


def text_summary(new_houses, criteria_rows=()):
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
        if criteria_rows:
            fits, best = fit_summary(f, criteria_rows)
            if best and best["score"] > 0:
                short = best["name"].split("—")[0].strip()
                lines.append(f"  Best fit: {short} ({best['met']}/{best['known']} checks)")
        lines.append("  " + " · ".join(b for b in bits if b))
        old = f.get("Previous Price")
        if old and f.get("Price") and old != f["Price"]:
            delta = f["Price"] - old
            way = "off" if delta < 0 else "up"
            lines.append(f"  PRICE {'DROP' if delta < 0 else 'RAISE'}: "
                         f"{_money(abs(delta))} {way} from {_money(old)} "
                         f"({abs(delta) / old * 100:.1f}%)")
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


def _pulled_stamp(houses):
    """When the feed was last actually read: the newest Last Seen the worker
    stamped (Date Added for rows that predate the stamp). The same date the
    app shows, because both read the same table."""
    latest = ""
    for rec in houses:
        f = rec.get("fields", {})
        d = str(f.get("Last Seen") or f.get("Date Added") or "")[:10]
        latest = max(latest, d)
    return latest or date.today().isoformat()


def build_email(criteria_rows, new_houses):
    app_url = "https://claudekovalenko.github.io/mls/"
    today = date.today().strftime("%b %-d")

    if new_houses:
        n = len(new_houses)
        # Two kinds of news, counted separately, because "3 price drops" is a
        # different email from "3 new listings" and the subject line is the
        # only part most people read.
        drops = sum(1 for r in new_houses
                    if r.get("fields", {}).get("Previous Price")
                    and r.get("fields", {}).get("Price")
                    and r["fields"]["Price"] < r["fields"]["Previous Price"])
        fresh = n - drops
        parts = []
        if fresh:
            parts.append(f"{fresh} new match{'es' if fresh != 1 else ''}")
        if drops:
            parts.append(f"{drops} price drop{'s' if drops != 1 else ''}")
        headline = " · ".join(parts) or f"{n} match{'es' if n != 1 else ''}"
        subject = f"House Finder: {headline}"
        sub = ("Newly listed, or newly cheaper. Ranked by how many of your criteria "
               "each one provably falls into. Tap through for the full listing.")
        content = house_rows(new_houses, criteria_rows)
        picks_html = _picks_html(new_houses, criteria_rows)
    else:
        subject = "House Finder: search criteria are live"
        headline = "No new matches today"
        sub = "The searches below ran and found nothing new. Here's what they're hunting for."
        content = ""
        picks_html = ""

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
      <div style="color:{MUTED};font-size:11px;margin-top:4px;">Data pulled {_pulled_stamp(new_houses)}</div>
      <div style="font-family:{SERIF};color:{INK};font-size:30px;font-weight:700;
                  margin-top:8px;line-height:1.1;">{headline}</div>
      <div style="color:{MUTED};font-size:13px;margin-top:9px;line-height:1.55;">{sub}</div>
    </td></tr>
    {picks_html}
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
      change recipients or criteria in the database
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
    """Recipients come from the database, so they can be changed from a phone.

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
    print(f"Recipients: {len(good)} active")
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

    # DIGEST_SEARCH scopes this send to one criteria row, matched against the
    # Found By column. That is what keeps the multifamily email separate from
    # the house email without a second table or a second script: same code,
    # two workflows, each pointed at its own search. Unset means every search,
    # which is the existing behaviour.
    only = os.environ.get("DIGEST_SEARCH", "").strip().lower()
    # The mirror of DIGEST_SEARCH. Without it the two digests overlap: the
    # house email has no scope of its own, so it would happily list the
    # multifamily complexes the other email exists to carry.
    skip = os.environ.get("DIGEST_EXCLUDE", "").strip().lower()

    at = connect()
    to = resolve_recipients(at)
    if not to:
        print(f"::error::No recipients. Add a row to the {TABLE_RECIPIENTS} table in "
              "the Recipients table with an Email and Active checked, or set "
              "EMAIL_TO for a one-off.")
        return 1
    criteria_rows = at.list_records(TABLE_CRITERIA, formula="{Active}")
    if only:
        # Show only the brief this email is about; a multifamily digest
        # listing the three house searches would just be confusing.
        scoped = [r for r in criteria_rows
                  if only in (r.get("fields", {}).get("Name") or "").lower()]
        criteria_rows = scoped or criteria_rows
    elif skip:
        criteria_rows = [r for r in criteria_rows
                         if skip not in (r.get("fields", {}).get("Name") or "").lower()]

    days = int(os.environ.get("DIGEST_DAYS", "1"))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    def worth_sending(rec):
        f = rec.get("fields", {})
        lane = lane_of(f)
        if only and lane != ("multifamily" if "multifamily" in only else "house"):
            return False
        if skip and lane == ("multifamily" if "multifamily" in skip else "house"):
            return False
        # A house you can no longer buy is not news. Under contract, sold
        # or withdrawn houses drop out of the email entirely -- including
        # the ones the feed itself reported as pending, which is the whole
        # reason for reading its status word rather than waiting a week for
        # the listing to vanish.
        if f.get("Listing Status") in ("Off Market", "Under Contract"):
            return False
        # Two ways in, and only two: it is newly listed, or its price moved.
        # Everything else is a house we already emailed about, unchanged --
        # and re-sending it is how a digest turns into noise nobody opens.
        is_new = (f.get("Date Added") or "") >= cutoff
        dropped = (f.get("Price Change Date") or "") >= cutoff
        if not (is_new or dropped):
            return False
        # A decision already made is not news. Under Contract, Purchased and
        # Rejected are your own words about a house -- there is nothing left
        # to do about any of them, and putting one back in a list of things
        # to go and look at is worse than useless. This is the pipeline
        # Status, which is separate from Listing Status: one is what you
        # decided, the other is what the market did.
        if f.get("Status") in DECIDED_STATUSES:
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
    text = text_summary(new_houses, criteria_rows) if new_houses else \
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
