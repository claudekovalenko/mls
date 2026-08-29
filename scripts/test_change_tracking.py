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
