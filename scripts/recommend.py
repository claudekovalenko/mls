#!/usr/bin/env python3
"""What to do about a house, and why.

A ranked list still leaves the deciding to you. This turns the evidence into
a recommendation, and -- more importantly -- states what the recommendation
rests on, so you can disagree with it on the specifics rather than having to
take or leave a score.

Two rules govern everything here.

It only ever recommends a *next step*, never a purchase. Nothing in this
project knows the two numbers that decide a flip: what the work really costs
and what the house really resells for. A tool that said "buy this" while
missing both would be worth less than no tool.

And it says what would change its mind. A recommendation you cannot argue
with is one you cannot trust.
"""

BELOW_MARKET_PCT = 15      # % under the area median that counts as underpriced
STALE_DAYS = 90            # past this a listing is being passed over
REAL_PRICE_CUT = 5         # % off asking that indicates a motivated seller
DATED_YEAR = 1985          # original-condition stock

# The four things worth doing about a house, strongest first.
SEE_IT = "Go and see it"
NEGOTIATE = "Worth an offer under asking"
WATCH = "Watch it"
SKIP = "Skip unless you know something the data doesn't"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def discount_pct(fields):
    """The 'N% under area $/sqft' figure the scorer wrote, if any."""
    for signal in str(fields.get("Value Signals") or "").split(","):
        signal = signal.strip()
        if "% under area" in signal:
            try:
                return int(signal.split("%")[0].strip())
            except ValueError:
                return None
    return None


def breakeven_resale(fields, selling_cost_pct=0.08):
    """What it must resell for to break even, given price and rehab.

    The single most decision-useful number available, because it needs no
    guess from anyone: purchase and rehab are both known, so the only
    unknown left is the one you are actually qualified to judge. It turns
    "is this a good flip?" into "can this street carry $X?", which is a
    question you can answer standing on the street.
    """
    price, rehab = _num(fields.get("Price")), _num(fields.get("Rehab Cost"))
    if price is None or rehab is None:
        return None
    # resale - price - rehab - selling_cost_pct * resale = 0
    return (price + rehab) / (1 - selling_cost_pct)


def recommend(fields, best_fit=None):
    """(action, reasons, caveats) for one house.

    best_fit is the winning entry from send_digest.fit_summary, when there is
    one -- how well the house matches a strategy is evidence, but it is not
    the only evidence, and a house can fit a brief perfectly and still be
    priced like everything else on the street.
    """
    reasons, caveats = [], []

    price = _num(fields.get("Price"))
    dom = _num(fields.get("Days on Market"))
    cut = _num(fields.get("Price Cut"))
    year = _num(fields.get("Year Built"))
    discount = discount_pct(fields)
    fit_score = (best_fit or {}).get("score") or 0
    fit_name = ((best_fit or {}).get("name") or "").split("—")[0].strip()

    # Evidence, each with the fact that supports it.
    underpriced = discount is not None and discount >= BELOW_MARKET_PCT
    stale = dom is not None and dom >= STALE_DAYS
    motivated = cut is not None and cut >= REAL_PRICE_CUT
    dated = year is not None and year <= DATED_YEAR

    if underpriced:
        reasons.append(f"{discount}% under the going rate per square foot round there")
    if stale:
        reasons.append(f"sat {dom:.0f} days when the area typically goes under "
                       f"contract in about three weeks")
    if motivated:
        reasons.append(f"the seller has already cut {cut:.0f}%")
    if dated and not (underpriced or motivated):
        reasons.append(f"built {year:.0f}, so likely original condition")
    if fit_score >= 1 and fit_name:
        reasons.append(f"meets every measurable part of {fit_name}")
    elif fit_score >= 0.75 and fit_name:
        reasons.append(f"meets most of {fit_name}")

    # The decision. Deliberately conservative: the strong actions need
    # evidence about *price*, not just a house that matches a description.
    if (underpriced or motivated) and fit_score >= 0.75:
        action = SEE_IT
    elif stale and motivated:
        action = NEGOTIATE
        reasons.append("both together say the asking price is not holding")
    elif underpriced or (fit_score >= 1 and (dated or stale)):
        action = SEE_IT
    elif fit_score >= 0.5 or dated or stale:
        action = WATCH
    else:
        action = SKIP

    # Never a recommendation without a reason. A house can land on Watch
    # purely by half-matching a strategy, and "Watch it" with nothing under
    # it is the kind of empty verdict this whole module exists to avoid.
    if not reasons:
        if fit_score and fit_name:
            reasons.append(f"clears part of {fit_name} but nothing else marks it out")
        else:
            reasons.append("nothing in the data marks this out from the rest of the street")

    # What the recommendation does not know. Never omitted, because the
    # gap is the whole reason this is a next step and not a verdict.
    breakeven = breakeven_resale(fields)
    if breakeven:
        caveats.append(f"needs to resell above ${breakeven:,.0f} to break even, "
                       f"on a rehab estimate nobody has verified")
    else:
        caveats.append("no rehab estimate yet, so there is no profit figure here")
    if price and fields.get("ARV") in (None, ""):
        caveats.append("resale value is the one number this cannot see")

    return action, reasons, caveats


def headline(fields, best_fit=None):
    """One line: the action and its single strongest reason."""
    action, reasons, _ = recommend(fields, best_fit)
    return f"{action} — {reasons[0]}" if reasons else action
