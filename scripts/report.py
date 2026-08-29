#!/usr/bin/env python3
"""Score listings against the brief and print the best ones, no filtering.

The scheduled worker answers "does this clear every bar?" and on the current
data source the answer is always no -- RentCast returns no listing remarks, so
every requirement phrased in words (fixer, basement, ADU, FSBO) has nothing to
match and rejects everything. Four runs, 450 listings, zero results.

This asks the useful question instead: "which of the brief's categories does
this listing demonstrably fall into?" Nothing is filtered out for missing a
category. Everything is scored on what the data can actually prove, ranked,
and printed. A house hitting three categories is worth a look even if the
other four are unknowable.

Read-only: prints a report, writes nothing back. Budget-gated like the
worker, and capped tighter -- this is meant to be run on demand.
"""
import os
import sys

from db import connect, TABLE_CRITERIA
import rentcast_budget
import search_worker

TOP_N = 20
MAX_CALLS = 10          # tighter than the worker's 25; this runs on demand


def _fmt_money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def run_one(criteria_record, budget):
    fields = criteria_record["fields"]
    name = fields.get("Name") or "(unnamed)"
    listings = search_worker.fetch_rentcast(
        fields, os.environ["RENTCAST_API_KEY"], budget)

    # Drop land -- no beds, no baths, no living area. It scores well on
    # "no sqft" plus "oversized lot" and is not a house.
    houses = [l for l in listings if not search_worker.looks_like_land(l)]

    median_ppsf = search_worker.median_price_per_sqft(houses)

    scored = []
    for listing in houses:
        hits = search_worker.categories(listing, fields, median_ppsf)
        if hits:
            scored.append((len(hits), listing, hits))
    scored.sort(key=lambda x: (-x[0], x[1].get("price") or 10**9))

    print(f"\n{'=' * 68}\n{name}")
    print(f"  {len(listings)} fetched · {len(houses)} houses (land dropped) · "
          f"median ${median_ppsf:,.0f}/sqft" if median_ppsf else
          f"  {len(listings)} fetched · {len(houses)} houses (land dropped)")
    if not scored:
        print("  Nothing scored in any category.")
        return

    for rank, (count, l, hits) in enumerate(scored[:TOP_N], 1):
        ppsf = (f"${l['price'] / l['sqft']:,.0f}/sqft"
                if l.get("price") and l.get("sqft") else "sqft unlisted")
        beds = f"{l['beds']:g}bd" if l.get("beds") else "?bd"
        baths = f"{l['baths']:g}ba" if l.get("baths") else "?ba"
        sqft = f"{l['sqft']:,.0f} sqft" if l.get("sqft") else "no sqft"
        print(f"\n  {rank}. {l.get('address') or '(no address)'}")
        print(f"     {_fmt_money(l.get('price'))} · {beds}/{baths} · {sqft} · {ppsf}")
        print(f"     [{count}] {' · '.join(hits)}")


def main():
    if not os.environ.get("RENTCAST_API_KEY"):
        print("::error::RENTCAST_API_KEY is not set.")
        return 1

    budget = rentcast_budget.load()
    budget_cap = min(MAX_CALLS, rentcast_budget.PER_RUN_LIMIT)
    rentcast_budget.PER_RUN_LIMIT = budget_cap
    print(budget.summary())

    at = connect()
    rows = at.list_records(TABLE_CRITERIA, formula="{Active}")
    if not rows:
        print("::warning::No Active rows in Search Criteria.")
        return 0

    try:
        for record in rows:
            try:
                run_one(record, budget)
            except rentcast_budget.BudgetExhausted as exc:
                print(f"\n  Stopped: {exc}")
                break
            except Exception as exc:
                name = record.get("fields", {}).get("Name", "(unnamed)")
                print(f"\n  {name}: FAILED {exc}")
    finally:
        # Save even on the exhausted path -- an uncommitted counter after real
        # spend is how a balance gets drained twice.
        rentcast_budget.save(budget)
        print(f"\n{'=' * 68}\n{budget.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
