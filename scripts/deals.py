#!/usr/bin/env python3
"""Flip / BRRRR deal math and qualification.

Single source of truth for the numbers, shared by the search worker and
mirrored by the PWA. Assumptions are named constants rather than inline
magic numbers so they can be argued with and tuned.

These are conventional screening heuristics, not underwriting. Rehab Cost
and ARV in particular are estimates unless a human has entered real ones.
"""

FLIP_SELLING_COST_PCT = 0.08   # agent commission + closing costs
FLIP_MAX_OFFER_PCT = 0.70      # the classic "70% rule"
BRRRR_REFI_LTV = 0.75          # typical cash-out refi loan-to-value on ARV
BRRRR_INTEREST_RATE = 0.07     # annual, 30yr fixed
BRRRR_LOAN_TERM_YEARS = 30
BRRRR_EXPENSE_RATIO = 0.50     # "50% rule": taxes/insurance/maint/vacancy/capex/PM

# Qualification thresholds (defaults; a criteria row can override per search).
FLIP_STRONG_PROFIT = 50000
FLIP_OK_PROFIT = 25000
FLIP_STRONG_ROI = 0.15
FLIP_OK_ROI = 0.10
BRRRR_STRONG_COC = 0.12
BRRRR_OK_COC = 0.08

TIER_RANK = {"STRONG": 3, "GOOD": 2, "MARGINAL": 1, "PASS": 0, "NO DATA": -1}


def monthly_mortgage_payment(loan_amount, annual_rate, years):
    if not loan_amount or loan_amount <= 0:
        return 0.0
    monthly_rate = annual_rate / 12
    n = years * 12
    if monthly_rate == 0:
        return loan_amount / n
    return loan_amount * (monthly_rate * (1 + monthly_rate) ** n) / ((1 + monthly_rate) ** n - 1)


def compute_metrics(price, rehab_cost, arv, rent_estimate):
    """All the derived numbers. Any input may be None; anything that can't be
    computed comes back as None rather than a misleading zero.

    An ARV equal to the list price is treated as no ARV at all. Nobody
    estimates that a house will resell for exactly what it is listed at --
    that value only ever arrives as a placeholder, and left alone it makes
    flip profit negative by construction: price minus price minus rehab minus
    selling costs. The result looks like a projection about the house and is
    really just its rehab budget with a minus sign, which is worse than
    having no number at all.
    """
    if arv is not None and price is not None and arv == price:
        arv = None

    m = {
        "maxOffer70": None, "flipProfit": None, "flipRoi": None,
        "cashLeftInDeal": None, "brrrrCashflow": None, "cashOnCash": None,
        "onePercentRatio": None,
    }

    if arv is not None:
        m["maxOffer70"] = arv * FLIP_MAX_OFFER_PCT - (rehab_cost or 0)

    if price is not None and rehab_cost is not None and arv is not None:
        total_in = price + rehab_cost
        m["flipProfit"] = arv - total_in - (arv * FLIP_SELLING_COST_PCT)
        if total_in > 0:
            m["flipRoi"] = m["flipProfit"] / total_in

        refi_loan = arv * BRRRR_REFI_LTV
        m["cashLeftInDeal"] = total_in - refi_loan
        if rent_estimate is not None:
            payment = monthly_mortgage_payment(refi_loan, BRRRR_INTEREST_RATE, BRRRR_LOAN_TERM_YEARS)
            m["brrrrCashflow"] = rent_estimate * BRRRR_EXPENSE_RATIO - payment
            if m["cashLeftInDeal"] > 0:
                m["cashOnCash"] = (m["brrrrCashflow"] * 12) / m["cashLeftInDeal"]
            else:
                # Nothing left in the deal: return is unbounded. Represent it as
                # a large finite number so it sorts/serializes sanely instead of
                # becoming Infinity (which isn't valid JSON).
                m["cashOnCash"] = 99.0 if m["brrrrCashflow"] > 0 else None

    if rent_estimate is not None and price:
        m["onePercentRatio"] = rent_estimate / price

    return m


def qualify_flip(price, m):
    if m["flipProfit"] is None:
        return "NO DATA", ["Needs price, rehab cost, and ARV"]
    reasons = []
    meets70 = price is not None and m["maxOffer70"] is not None and price <= m["maxOffer70"]
    reasons.append("Clears the 70% rule" if meets70 else "Over the 70%-rule max offer")
    reasons.append(f"${m['flipProfit']:,.0f} projected profit")
    if m["flipRoi"] is not None:
        reasons.append(f"{m['flipRoi'] * 100:.1f}% return on cash in")

    if m["flipProfit"] <= 0:
        tier = "PASS"
    elif meets70 and m["flipProfit"] >= FLIP_STRONG_PROFIT and (m["flipRoi"] or 0) >= FLIP_STRONG_ROI:
        tier = "STRONG"
    elif m["flipProfit"] >= FLIP_OK_PROFIT and (m["flipRoi"] or 0) >= FLIP_OK_ROI:
        tier = "GOOD"
    else:
        tier = "MARGINAL"
    return tier, reasons


def qualify_brrrr(m):
    if m["cashLeftInDeal"] is None:
        return "NO DATA", ["Needs price, rehab cost, and ARV"]
    if m["brrrrCashflow"] is None:
        return "NO DATA", ["Needs a rent estimate to judge cashflow"]
    reasons = []
    if m["cashLeftInDeal"] <= 0:
        reasons.append("All capital recycled on refi")
    else:
        reasons.append(f"${m['cashLeftInDeal']:,.0f} left in the deal")
    reasons.append(f"${m['brrrrCashflow']:,.0f}/mo cashflow")

    # Negative cashflow is disqualifying regardless of how good the refi looks --
    # a property that bleeds every month isn't a hold.
    if m["brrrrCashflow"] <= 0:
        tier = "PASS"
    elif m["cashLeftInDeal"] <= 0 or (m["cashOnCash"] or 0) >= BRRRR_STRONG_COC:
        tier = "STRONG"
    elif (m["cashOnCash"] or 0) >= BRRRR_OK_COC:
        tier = "GOOD"
    else:
        tier = "MARGINAL"
    return tier, reasons


def qualify(price, rehab_cost, arv, rent_estimate, targets=None):
    """Full verdict. `targets` optionally carries per-search minimums from the
    criteria row: {flipProfit, cashOnCash, onePercent} -- a house only counts as
    Qualified if it clears every target that was actually set."""
    m = compute_metrics(price, rehab_cost, arv, rent_estimate)
    flip_tier, flip_reasons = qualify_flip(price, m)
    brrrr_tier, brrrr_reasons = qualify_brrrr(m)

    flip_rank, brrrr_rank = TIER_RANK[flip_tier], TIER_RANK[brrrr_tier]
    best_strategy = None
    if max(flip_rank, brrrr_rank) > 0:
        best_strategy = "Flip" if flip_rank >= brrrr_rank else "BRRRR"

    qualified = max(flip_rank, brrrr_rank) > 0
    if targets:
        if targets.get("flipProfit") is not None:
            qualified = qualified and (m["flipProfit"] or 0) >= targets["flipProfit"]
        if targets.get("cashOnCash") is not None:
            qualified = qualified and (m["cashOnCash"] or 0) >= targets["cashOnCash"]
        if targets.get("onePercent") is not None:
            qualified = qualified and (m["onePercentRatio"] or 0) >= targets["onePercent"]

    return {
        "metrics": m,
        "flipVerdict": flip_tier, "flipReasons": flip_reasons,
        "brrrrVerdict": brrrr_tier, "brrrrReasons": brrrr_reasons,
        "bestStrategy": best_strategy,
        "bestRank": max(flip_rank, brrrr_rank),
        "qualified": qualified,
    }
