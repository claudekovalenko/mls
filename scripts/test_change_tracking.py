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
    check("a held-back house is labelled Next in line, not Watch",
          all(r["action"] == recommend.NEXT_UP for r in held), True)
    # The number decides the word: a stronger house never carries a weaker
    # verdict than a weaker house does.
    rank = {recommend.SEE_IT: 4, recommend.NEXT_UP: 4, recommend.NEGOTIATE: 3,
            recommend.WATCH: 2, recommend.SKIP: 1}
    mixed = recommend.triage([mk(30, 200, 20, 1.0), mk(10, 95, 6, 0.5),
                              mk(0, 30, 0, 0.5), mk(0, 0, 0, 0)])
    ordered = sorted(mixed, key=lambda r: -r["strength"])
    check("verdicts never get better as the score gets worse",
          all(rank[a["action"]] >= rank[b["action"]]
              for a, b in zip(ordered, ordered[1:])), True)
    check("the score bands are the ones the legend promises",
          [recommend.band_action(x) for x in (75, 50, 30, 5)],
          [recommend.SEE_IT, recommend.NEGOTIATE, recommend.WATCH, recommend.SKIP])

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

    print("\nHomeSteps cards parse into listings:")
    import search_worker as sw
    fix = ('<a id="node-1" href="/listingdetails/6295-phillips-pl-lithonia-ga-30058">'
           '<span class="property-status-value">Active</span>'
           '<div class="property-price">$184,900</div>'
           '<div class="property-details">4 beds, 2 baths, 1,950 sq. ft.</div></a>'
           '<script type="application/ld+json">{"name": "6295 Phillips Pl, Lithonia, GA 30058",'
           ' "offers": {"price": "$184,900", "itemOffered": {"numberOfBedrooms": "4",'
           ' "numberOfBathroomsTotal": "2"}}}</script>')
    parsed = sw._homesteps_parse(fix)
    check("one card parses to one listing", len(parsed), 1)
    check("the price is a number, not a string", parsed[0]["price"], 184900.0)
    check("sqft comes from the details line", parsed[0]["sqft"], 1950.0)
    check("the feed status word travels", parsed[0]["feedStatus"], "Active")
    geo_fix = ('<div data-lat="33.69195" data-lng="-84.537156" typeof="Place">'
               '<div class="location-content">'
               '<a id="node-1" href="/listingdetails/6295-phillips-pl-lithonia-ga-30058">'
               '<div class="property-price">$184,900</div></a></div></div>' + fix)
    with_geo = sw._homesteps_parse(geo_fix)
    check("the map pane's coordinates attach to the listing",
          (with_geo[0]["latitude"], with_geo[0]["longitude"]),
          (33.69195, -84.537156))
    sw._homesteps_cache = parsed
    check("a Marietta search does not receive a Lithonia foreclosure",
          len(sw.fetch_homesteps({"City": "Marietta"})), 0)
    check("its own zip ring does receive it",
          len(sw.fetch_homesteps({"Zip Codes": "30058, 30062"})), 1)
    sw._homesteps_cache = None

    print("\nA richer feed fills the blanks a sparse source left:")
    from search_worker import enrich_gaps
    stored = {"Address": "1 Foreclosure Way", "Price": 200000,
              "Source": "homesteps", "Beds": 4, "Sqft": None, "Lot Sqft": None}
    rich = {"price": 205000, "beds": 4, "baths": 2, "sqft": 1800,
            "lotSqft": 9000, "yearBuilt": 1972}
    gaps = enrich_gaps(rich, stored)
    check("missing sqft is filled in", gaps.get("Sqft"), 1800)
    check("missing lot is filled in", gaps.get("Lot Sqft"), 9000)
    check("a bed count already on file is left alone", "Beds" in gaps, False)
    check("price is never enrichment", "Price" in gaps, False)
    check("provenance is never enrichment", "Source" in gaps, False)
    check("price per sqft is derived once sqft exists",
          gaps.get("Price Per Sqft"), round(205000 / 1800))
    check("a complete row needs nothing",
          enrich_gaps(rich, {**stored, **{"Sqft": 1800, "Lot Sqft": 9000,
              "Baths": 2, "Year Built": 1972, "Price Per Sqft": 114}}),
          {})

    print("\nA search only judges its own kind of building:")
    from send_digest import fit_summary as fs
    mf_row = {"fields": {"Name": "Multifamily 5+", "Active": True,
                         "Property Class": "Multifamily", "Min Units": 5,
                         "Max Price": 5000000}}
    flip_row = {"fields": {"Name": "Flip", "Active": True, "Strategy": "Flip",
                           "Max Price": 500000}}
    sfh = {"Property Type": "Single Family", "Price": 400000}
    block = {"Property Type": "Multi-Family", "Units": 12, "Price": 2000000}
    fits, best = fs(sfh, [mf_row, flip_row])
    check("a single-family house is never scored by the multifamily search",
          all(x["name"] != "Multifamily 5+" for x in fits), True)
    check("...its best fit comes from a house search", best["name"], "Flip")
    fits, best = fs(block, [mf_row, flip_row])
    check("a 12-unit block is never scored by the flip search",
          all(x["name"] != "Flip" for x in fits), True)
    check("...and does fit the multifamily search", best["name"], "Multifamily 5+")
    small = {"Property Type": "Multi-Family", "Units": 3, "Price": 800000}
    fits, _ = fs(small, [mf_row, flip_row])
    unit_checks = [s for x in fits for lab, s in x["checks"] if "units" in lab]
    check("a triplex fails the 5+ unit check rather than passing silently",
          unit_checks, [False])
    clones = [{"fields": {"Name": f"Flip — {city} (Ryan)", "Active": True,
                          "Strategy": "Flip", "Max Price": 500000,
                          "Max Price Per Sqft": 175}}
              for city in ("Marietta", "Smyrna", "Kennesaw", "Acworth")]
    fits, _ = fs(sfh, clones)
    check("four identical city searches collapse to one fit row",
          len(fits), 1)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
