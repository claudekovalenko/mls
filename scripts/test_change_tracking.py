#!/usr/bin/env python3
"""Does a second run actually notice what changed since the first?

The whole promise of storing houses is that a digest can say "these are new,
these got cheaper, and nothing else is worth your morning". That promise has
three moving parts -- the worker recording a price move, the worker retiring
a listing that left the market, and the digest selecting on both -- and none
of them is exercised by simply running the search once.

So this replays two runs against the real functions and asserts what the
second one should conclude. No network, no API keys, no cost.

Run: python test_change_tracking.py
"""
import sys
from datetime import date, timedelta

from search_worker import address_key, price_change_update, retire_missing
from send_digest import lane_of

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=30)).isoformat()

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}\n        got      {got!r}")
        failures.append(label)


def digest_picks(house, days=1):
    """The digest's own selection rule, as send_digest.worth_sending applies
    it: newly listed or newly cheaper, still buyable, and not already
    decided."""
    from send_digest import DECIDED_STATUSES
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    if house.get("Listing Status") in ("Off Market", "Under Contract"):
        return False
    if house.get("Status") in DECIDED_STATUSES:
        return False
    is_new = (house.get("Date Added") or "") >= cutoff
    dropped = (house.get("Price Change Date") or "") >= cutoff
    return is_new or dropped


def main():
    print("\nRUN 1 -- four houses recorded, then a week passes.\n")
    # What the first run left in the database.
    stored = {address_key(a): {"id": rid, "price": p} for a, rid, p in (
        ("1 Dropped Ln", "r1", 400000),
        ("2 Unchanged Rd", "r2", 350000),
        ("3 Sold Ct", "r3", 275000),
        ("4 Raised Ave", "r4", 300000),
    )}
    rows = {
        "r1": {"Address": "1 Dropped Ln", "Price": 400000, "Date Added": OLD,
               "Found By": "Flip", "Status": "New", "Listing Status": "Active"},
        "r2": {"Address": "2 Unchanged Rd", "Price": 350000, "Date Added": OLD,
               "Found By": "Flip", "Status": "New", "Listing Status": "Active"},
        "r3": {"Address": "3 Sold Ct", "Price": 275000, "Date Added": OLD,
               "Found By": "Flip", "Status": "New", "Listing Status": "Active"},
        "r4": {"Address": "4 Raised Ave", "Price": 300000, "Date Added": OLD,
               "Found By": "Flip", "Status": "New", "Listing Status": "Active"},
    }

    print("RUN 2 -- the feed comes back: one cut, one raised, one gone, one new.\n")
    feed = [
        {"address": "1 Dropped Ln", "price": 375000, "sqft": 2000},   # -$25,000
        {"address": "2 Unchanged Rd", "price": 350000, "sqft": 1800},  # same
        {"address": "4 Raised Ave", "price": 315000, "sqft": 1500},    # +$15,000
        {"address": "5 Brand New Way", "price": 260000, "sqft": 1400}, # never seen
    ]                                                                  # 3 Sold Ct absent

    print("What the worker concludes:")
    changes = {}
    for listing in feed:
        known = stored.get(address_key(listing["address"]))
        if known:
            upd = price_change_update(listing, known)
            if upd:
                changes[upd["id"]] = upd["fields"]

    check("1 Dropped Ln recorded a change", "r1" in changes, True)
    check("  ...previous price kept", changes.get("r1", {}).get("Previous Price"), 400000)
    check("  ...new price written", changes.get("r1", {}).get("Price"), 375000)
    check("  ...dated today", changes.get("r1", {}).get("Price Change Date"), TODAY)
    check("2 Unchanged Rd wrote nothing", "r2" in changes, False)
    check("4 Raised Ave recorded a change too", "r4" in changes, True)
    check("5 Brand New Way is new, not a change", "r5" in changes, False)

    # Apply the worker's writes, the way the real run would.
    for rid, fields in changes.items():
        rows[rid].update(fields)
    rows["r5"] = {"Address": "5 Brand New Way", "Price": 260000, "Date Added": TODAY,
                  "Found By": "Flip", "Status": "New", "Listing Status": "Active"}

    seen = {address_key(l["address"]) for l in feed}
    records = [{"id": rid, "fields": f} for rid, f in rows.items()]
    for upd in retire_missing(records, {"Flip": seen}):
        rows[upd["id"]].update(upd["fields"])

    check("3 Sold Ct marked off market", rows["r3"].get("Listing Status"), "Off Market")
    check("1 Dropped Ln still active", rows["r1"].get("Listing Status"), "Active")

    print("\nWhat tomorrow's digest sends:")
    picked = sorted(f["Address"] for f in rows.values() if digest_picks(f))
    for name in picked:
        why = []
        f = next(v for v in rows.values() if v["Address"] == name)
        if f.get("Date Added") == TODAY:
            why.append("newly listed")
        if f.get("Price Change Date") == TODAY:
            delta = f["Price"] - f["Previous Price"]
            why.append(f"{'cut' if delta < 0 else 'raised'} ${abs(delta):,}")
        print(f"    {name} -- {', '.join(why)}")
    for name in sorted(f["Address"] for f in rows.values() if not digest_picks(f)):
        f = next(v for v in rows.values() if v["Address"] == name)
        reason = ("no longer on the market" if f.get("Listing Status") == "Off Market"
                  else "unchanged since last time")
        print(f"    (held back) {name} -- {reason}")

    check("digest sends exactly the three that changed",
          picked, ["1 Dropped Ln", "4 Raised Ave", "5 Brand New Way"])

    print("\nHouses whose question is already settled:")
    for status, sends in (("New", True), ("Interested", True), ("Touring", True),
                          ("Under Contract", False), ("Purchased", False),
                          ("Rejected", False)):
        check(f"a {status!r} house with a fresh price cut is emailed: {sends}",
              digest_picks({"Address": "x", "Status": status,
                            "Price": 300000, "Previous Price": 340000,
                            "Price Change Date": TODAY, "Date Added": OLD}), sends)
    check("a house the market put under contract is not emailed",
          digest_picks({"Address": "x", "Status": "New", "Date Added": TODAY,
                        "Listing Status": "Under Contract"}), False)

    print("\nWhen the feed states a status outright:")
    from search_worker import listing_status_from_feed as feed_status
    for word, want in (("Active", "Active"), ("Pending", "Under Contract"),
                       ("Active Under Contract", "Under Contract"),
                       ("Contingent", "Under Contract"), ("Sold", "Off Market"),
                       ("Withdrawn", "Off Market"), ("", None), ("Wibble", None)):
        check(f"feed says {word!r}", feed_status(word), want)
    # A status word alone is worth writing, even with no price move.
    status_only = price_change_update(
        {"price": 400000, "feedStatus": "Pending"},
        {"id": "r9", "price": 400000, "listing_status": "Active"})
    check("pending is recorded even when the price held",
          status_only and status_only["fields"], {"Listing Status": "Under Contract"})
    check("an unchanged, unremarkable house still writes nothing",
          price_change_update({"price": 400000, "feedStatus": "Active"},
                              {"id": "r9", "price": 400000, "listing_status": "Active"}), None)

    print("\nSame house, written two ways:")
    check("typed by hand matches the feed's format",
          address_key("1912 King Arthurs Ct Marietta GA 30062")
          == address_key("1912 King Arthurs Ct, Marietta, GA 30062"), True)
    check("different houses stay different",
          address_key("1912 King Arthurs Ct") == address_key("1913 King Arthurs Ct"), False)

    print("\nRecommendations:")
    import recommend
    from send_digest import fit_summary
    crit = [{"fields": {"Name": "Flip — 30068", "Active": True,
                        "Max Price": 500000, "Max Price Per Sqft": 175,
                        "Strategy": "Flip"}}]
    def advise(f):
        _, best = fit_summary(f, crit)
        return recommend.recommend(f, best)

    underpriced = {"Price": 369000, "Rehab Cost": 91520, "Sqft": 2288,
                   "Price Per Sqft": 161, "Days on Market": 131, "Year Built": 1972,
                   "Value Signals": "under $175/sqft, 23% under area $/sqft"}
    stale_cut = {"Price": 404900, "Rehab Cost": 65040, "Sqft": 1626,
                 "Price Per Sqft": 249, "Days on Market": 157, "Price Cut": 19,
                 "Year Built": 1966, "Value Signals": "built 1966"}
    ordinary = {"Price": 459900, "Rehab Cost": 19780, "Sqft": 989,
                "Price Per Sqft": 465, "Days on Market": 10, "Year Built": 2015,
                "Value Signals": ""}

    check("underpriced and fitting -> go and see it",
          advise(underpriced)[0], recommend.SEE_IT)
    check("stale with a real cut -> worth an offer",
          advise(stale_cut)[0], recommend.NEGOTIATE)
    check("nothing remarkable -> not a strong action",
          advise(ordinary)[0] in (recommend.WATCH, recommend.SKIP), True)
    # The rule that matters more than the actions themselves.
    for label, house in (("underpriced", underpriced), ("stale", stale_cut),
                         ("ordinary", ordinary)):
        action, why, caveats = advise(house)
        check(f"{label}: never a recommendation without a reason", bool(why), True)
        check(f"{label}: always says what it cannot see", bool(caveats), True)
    check("breakeven is price plus rehab over the selling margin",
          round(recommend.breakeven_resale({"Price": 369000, "Rehab Cost": 91520})),
          500565)
    check("no rehab means no breakeven, not a guess",
          recommend.breakeven_resale({"Price": 369000}), None)

    print("\nApproach and next steps:")
    big_lot = dict(underpriced, **{"Lot Sqft": 30492})
    _, best_lot = fit_summary(big_lot, crit)
    name, play, numbers = recommend.approach(big_lot, best_lot)
    # The bug this caught: the play was chosen from the lot, so a house whose
    # best fit was Flip was described as an ADU project.
    check("the play matches the strategy named beside it",
          "resale" in play.lower() if "flip" in name.lower() else True, True)
    check("a big lot is mentioned as an option, not as the plan",
          "would also take a second dwelling" in play, True)
    check("the numbers name entry, work and exit", len(numbers), 3)

    steps = recommend.next_steps(big_lot, recommend.SEE_IT, best_lot)
    check("first step is testing the breakeven against real comps",
          "comparable sales" in steps[0], True)
    check("a stale listing gets a concrete opening offer",
          any("opening offer" in t for t in steps), True)
    check("a big lot triggers the zoning check before anything else",
          any("zoning" in t for t in steps), True)

    print("\nStrength and the viewing cap:")
    mk = lambda disc, dom, cut, fit: (
        {"Price": 350000, "Rehab Cost": 50000, "Sqft": 1800,
         "Days on Market": dom, "Price Cut": cut, "Year Built": 1970,
         "Value Signals": f"{disc}% under area $/sqft" if disc else "",
         "Listing Status": "Active"},
        {"score": fit, "name": "Flip — 30068"})
    check("saturated evidence scores 100", recommend.strength(*mk(30, 200, 20, 1.0)), 100)
    check("no evidence scores near zero", recommend.strength(*mk(0, 0, 0, 0)), 0)
    check("more discount means more strength",
          recommend.strength(*mk(25, 100, 0, 1.0))
          > recommend.strength(*mk(16, 100, 0, 1.0)), True)

    field = [mk(23, 131, 0, 1.0), mk(30, 200, 20, 1.0), mk(15, 95, 6, 1.0),
             mk(16, 100, 5, 1.0), mk(18, 120, 8, 1.0)]
    rows = recommend.triage(field)
    sees = [r for r in rows if r["action"] == recommend.SEE_IT]
    held = [r for r in rows if r["held_back"]]
    check("at most three hold a viewing however strong the field",
          len(sees), 3)
    check("the strongest three are the ones that hold it",
          min(r["strength"] for r in sees) >= max(r["strength"] for r in held), True)
    check("a held-back house says so in its own reasons",
          all("next in line" in r["reasons"][-1] for r in held), True)

    print("\nPicks only include things you can still buy:")
    live = (underpriced, best_lot)
    gone = (dict(underpriced, **{"Listing Status": "Off Market"}), best_lot)
    pending = (dict(underpriced, **{"Listing Status": "Under Contract"}), best_lot)
    chosen = recommend.picks([live, gone, pending])
    check("one live house picked, the sold and pending ones dropped",
          len(chosen), 1)
    watch_only = ({"Price": 459900, "Rehab Cost": 19780, "Sqft": 989,
                   "Price Per Sqft": 465, "Days on Market": 10,
                   "Year Built": 2015, "Value Signals": ""}, None)
    check("a house merely worth watching is not a pick",
          len(recommend.picks([watch_only])), 0)

    print("\nLane routing, so the two emails stay separate:")
    check("a 24-unit block is a complex", lane_of({"Units": 24}), "multifamily")
    check("a detached house is a home", lane_of({"Property Type": "Single Family"}), "house")
    check("an empty row is a home, not nothing", lane_of({}), "house")

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
