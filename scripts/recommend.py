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
NEXT_UP = "Next in line"   # strong enough to see, but the week's three are taken

# The score decides the word. One scale everywhere, so a 62 can never read
# as less important than a 38 -- which is exactly what happened when the
# word came from rules and the number from a composite.
GO_BAND, OFFER_BAND, WATCH_BAND = 60, 40, 20


def band_action(score):
    if score >= GO_BAND:
        return SEE_IT
    if score >= OFFER_BAND:
        return NEGOTIATE
    if score >= WATCH_BAND:
        return WATCH
    return SKIP


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


# ---------------------------------------------------------------- approach

def _acres(fields):
    lot = _num(fields.get("Lot Sqft"))
    return lot / 43560 if lot else None


def approach(fields, best_fit=None):
    """Which play this house is for, and the numbers that define it.

    The strategy a house best fits is not the same question as how you would
    actually run it. "BRRRR A" is a label; "the lot is big enough to put a
    second dwelling on, so the value is in the land not the house" is a plan
    you can act on or reject.
    """
    name = ((best_fit or {}).get("name") or "").split("—")[0].strip()
    detail = ((best_fit or {}).get("name") or "")
    price = _num(fields.get("Price"))
    rehab = _num(fields.get("Rehab Cost"))
    sqft = _num(fields.get("Sqft"))
    acres = _acres(fields)
    cats = str(fields.get("Value Signals") or "").lower()

    numbers = []
    if price is not None:
        numbers.append(f"buy around ${price:,.0f}")
    if rehab is not None:
        per_sqft = f" (${rehab / sqft:,.0f}/sqft)" if sqft else ""
        numbers.append(f"budget about ${rehab:,.0f} of work{per_sqft}")
    breakeven = breakeven_resale(fields)
    if breakeven is not None:
        numbers.append(f"exit above ${breakeven:,.0f} to make anything")

    # The play follows the strategy this house actually fits. Choosing it
    # from the lot instead produced the obvious contradiction: a house whose
    # best fit was Flip described as an ADU project, because the garden
    # happened to be large. What the house physically offers becomes an
    # aside, which is what it is -- a second option, not the plan.
    lower = (name + " " + detail).lower()
    if "adu" in lower or "brrrr a" in lower:
        play = ("Add a second dwelling. The value is in the land rather than "
                "the house, so the condition of the existing building matters "
                "less than what the lot will take.")
    elif "basement" in lower:
        play = ("Convert the basement into a separate unit. Two rents from one "
                "roof, and the square footage is already built and paid for.")
    elif "flip" in lower:
        play = ("Cosmetic renovation and resale. The margin comes from buying "
                "under the street's going rate, so the entry price is the whole "
                "game -- overpay at the front and no amount of work recovers it.")
    else:
        play = ("Hold and rent. Whether it works turns on rent against the "
                "financing after the work, and no rent estimate has been "
                "entered yet.")

    # Options the house carries beyond its best-fitting strategy. Worth
    # naming, never worth mistaking for the plan.
    if "flip" in lower and acres and acres >= 0.34:
        play += (f" The {acres:.2f}-acre lot would also take a second dwelling "
                 f"if you would rather hold it than sell it.")
    elif "flip" in lower and "basement" in cats:
        play += (" There is a basement, so a conversion is an alternative to "
                 "selling it on.")

    return name or "No matching strategy", play, numbers


# ------------------------------------------------------------- next steps

def next_steps(fields, action=None, best_fit=None):
    """The specific things to do about this house, in order.

    Written to be finishable. "Do more research" is not a step; "pull the
    last three sold on this street and check they clear $500,565" is.
    """
    steps = []
    price = _num(fields.get("Price"))
    dom = _num(fields.get("Days on Market"))
    cut = _num(fields.get("Price Cut"))
    detail = ((best_fit or {}).get("name") or "").lower()
    cats = str(fields.get("Value Signals") or "").lower()
    breakeven = breakeven_resale(fields)
    acres = _acres(fields)

    # The blind spot always comes first, because every other number depends
    # on it and nothing here can supply it.
    if breakeven is not None:
        steps.append(f"Pull the last three comparable sales within half a mile "
                     f"and check they clear ${breakeven:,.0f}. That single number "
                     f"decides whether the rest of this is worth doing.")
    else:
        steps.append("Enter a rehab estimate in the app so there is a breakeven "
                     "figure to test against.")

    if not _num(fields.get("Sqft")):
        steps.append("Ask the listing agent for the square footage -- it is "
                     "missing from the feed, which is why this passed the "
                     "price-per-foot test by default rather than on merit.")

    if dom is not None and dom >= STALE_DAYS and price:
        opening = price * 0.90
        steps.append(f"It has sat {dom:.0f} days"
                     + (f" and already come down {cut:.0f}%" if cut else "")
                     + f". An opening offer near ${opening:,.0f} (10% under "
                       f"asking) is defensible on the time alone.")

    if "adu" in detail or (acres and acres >= 0.34):
        steps.append("Check Cobb County zoning for an accessory dwelling on a "
                     "lot this size before anything else -- if it is not "
                     "permitted, the whole plan for this house is void.")
    if "basement" in detail or "basement" in cats:
        steps.append("Confirm on the visit that the basement is unfinished, has "
                     "ceiling height, and can take a legal egress window. "
                     "Without egress it is storage, not a unit.")

    if action == SEE_IT:
        steps.append("Book the viewing this week. Underpriced and dated does "
                     "not stay on the market.")
    elif action == NEGOTIATE:
        steps.append("Ask the agent why it has not sold before offering. The "
                     "answer is usually either the price or something you would "
                     "want to know about the house.")
    elif action == WATCH:
        steps.append("No action yet. It reappears in the email the moment the "
                     "price moves.")

    return steps


# ---------------------------------------------------------------- strength

def strength(fields, best_fit=None):
    """0-100: how much evidence has piled up on this house.

    The actions alone stopped meaning anything the moment several houses
    earned the same one -- twenty 'go and see it's is a list, not advice.
    This grades within the verdict, from the same four facts the verdict
    rests on, each capped so no single signal can saturate the score:

      up to 35  discount against the area's going $/sqft (capped at 30%)
      up to 25  how completely it fits its best strategy
      up to 20  time on market (capped at 180 days)
      up to 20  the seller's own price cut (capped at 20%)

    Deterministic and explainable: two houses with the same number have the
    same evidence, and you can see which fact moved the score.
    """
    dom = _num(fields.get("Days on Market")) or 0
    cut = _num(fields.get("Price Cut")) or 0
    discount = discount_pct(fields) or 0
    fit = max(0.0, min(1.0, (best_fit or {}).get("score") or 0))
    return round(35 * min(discount, 30) / 30
                 + 25 * fit
                 + 20 * min(dom, 180) / 180
                 + 20 * min(cut, 20) / 20)


# ------------------------------------------------------------------ triage

# How many houses may hold "go and see it" at once. Somebody has one
# Saturday, not twenty -- a strong verdict that everything qualifies for
# stops being a verdict at all.
SEE_LIMIT = 3


def triage(scored, see_limit=SEE_LIMIT):
    """Final actions across the whole field, not per house in isolation.

    scored: [(fields, best_fit)]. Each house is judged on its own evidence
    first, then ranked against the others: only the see_limit strongest keep
    "go and see it", and the rest are held back to Watch with that said in
    so many words. Held-back houses are next in line by construction -- the
    day a top one goes under contract, the strongest of them is promoted on
    the next run without anyone doing anything.
    """
    rows = []
    for fields, best in scored:
        _, reasons, caveats = recommend(fields, best)
        power = strength(fields, best)
        # The word follows the number. The rule-based recommend() still
        # supplies the reasons -- the *why* -- but the verdict itself comes
        # from the same 0-100 score the pin and the chip show.
        action = band_action(power)
        # A house that can no longer be bought gets no action at all,
        # whatever its evidence says. A recommendation you cannot act on is
        # noise wearing a verdict's clothes.
        if (fields.get("Listing Status") in ("Off Market", "Under Contract")
                or fields.get("Status") in ("Under Contract", "Purchased", "Rejected")):
            action = SKIP
            reasons = ["no longer available, whatever the numbers said"]
        rows.append({"fields": fields, "best": best, "action": action,
                     "reasons": reasons, "caveats": caveats,
                     "strength": power, "held_back": False})

    sees = sorted((r for r in rows if r["action"] == SEE_IT),
                  key=lambda r: -r["strength"])
    for r in sees[see_limit:]:
        r["action"] = NEXT_UP
        r["held_back"] = True
        r["reasons"].append(
            f"strong on its own, but only the {see_limit} strongest earn a "
            f"viewing in any one week -- this one is next in line")
    return rows


def picks(scored, limit=SEE_LIMIT):
    """The few houses worth starting with, strongest first."""
    order = {SEE_IT: 0, NEGOTIATE: 1}
    rows = [r for r in triage(scored, see_limit=limit)
            if r["action"] in order]
    rows.sort(key=lambda r: (order[r["action"]], -r["strength"]))
    return rows[:limit]


def headline(fields, best_fit=None):
    """One line: the action and its single strongest reason."""
    action, reasons, _ = recommend(fields, best_fit)
    return f"{action} — {reasons[0]}" if reasons else action
