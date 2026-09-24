#!/usr/bin/env python3
"""Stress test for the email digest. No network, no SMTP, no database.

The digest failed in production on Sept 17 because one house had beds but
no baths, and the plain-text formatter crashed on it -- nothing was sent and
nothing said so except a red run nobody opened. This throws the ugly data a
feed actually produces at every part of the email, checks who receives
what, and renders real previews into digest-preview/ to look at.

Run: python test_digest.py
"""
import itertools
import os
import random
import sys
import tempfile
from datetime import date, timedelta

import send_digest as sd

failures = []
TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=30)).isoformat()


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}\n        got      {got!r}")
        failures.append(label)


def house(**kw):
    f = {"Address": "123 Main St, Anaheim, CA 92805", "Market": "Orange County",
         "Price": 850000, "Beds": 3, "Baths": 2, "Sqft": 1400,
         "Price Per Sqft": 607, "Year Built": 1958, "Date Added": TODAY,
         "Listing Status": "Active", "Status": "New",
         "Property Type": "Single Family", "Latitude": 33.8366,
         "Longitude": -117.9143, "Value Signals": "built 1958, 72 days on market",
         "Flip Verdict": "NO DATA", "BRRRR Verdict": "NO DATA",
         "Found By": "Ivan — North OC houses"}
    f.update(kw)
    return {"id": kw.get("Address", "x"), "fields": f}


def crit(name, market, **kw):
    return {"id": name, "fields": {"Name": name, "Market": market, "Active": True,
                                   "Strategy": "Flip", "Max Price": 1100000, **kw}}


CRITERIA = [
    crit("Flip — Marietta (Ryan)", "Atlanta", **{"Max Price": 500000}),
    crit("Multifamily — Marietta (Ryan)", "Atlanta", **{"Property Class": "Multifamily",
                                                         "Min Units": 5}),
    crit("Ivan — North OC houses", "Orange County"),
    crit("Ivan — North OC multifamily", "Orange County",
         **{"Property Class": "Multifamily", "Min Units": 2}),
    crit("Ivan — LA County houses", "Los Angeles"),
]


def main():
    print("The Sept 17 crash, and everything shaped like it:")
    crash = house(Beds=3.0, Baths=None)
    try:
        sd.text_summary([crash], CRITERIA)
        check("beds without baths no longer crashes the text part", True, True)
    except Exception as exc:  # noqa: BLE001
        check(f"beds without baths no longer crashes ({exc})", False, True)

    # Every field a card reads, crossed with every bad value a row can hold.
    bad_values = [None, "", "abc", "1,200", "$450,000", 0, -5, True, 2.5, "  "]
    fields = ["Price", "Beds", "Baths", "Sqft", "Price Per Sqft", "Year Built",
              "Units", "Previous Price", "Latitude", "Longitude", "Days on Market"]
    crashes = []
    for field, value in itertools.product(fields, bad_values):
        h = house(**{field: value})
        try:
            sd.text_summary([h], CRITERIA)
            sd.build_email(CRITERIA, [h], ["Orange County"], "house")
        except Exception as exc:  # noqa: BLE001
            crashes.append(f"{field}={value!r}: {exc}")
    check(f"{len(fields) * len(bad_values)} single-field corruptions render",
          crashes, [])

    rng = random.Random(7)
    crashes = []
    for _ in range(300):
        h = house(**{f: rng.choice(bad_values + [123, 4.0])
                     for f in rng.sample(fields, rng.randint(1, len(fields)))})
        try:
            sd.build_email(CRITERIA, [h], ["Orange County"], None)
            sd.text_summary([h])
        except Exception as exc:  # noqa: BLE001
            crashes.append(str(exc))
    check("300 randomly corrupted houses render", crashes[:3], [])

    empty = {"id": "e", "fields": {}}
    try:
        sd.build_email(CRITERIA, [empty], [], None)
        sd.text_summary([empty])
        check("a completely empty row renders", True, True)
    except Exception as exc:  # noqa: BLE001
        check(f"a completely empty row renders ({exc})", False, True)

    print("\nThe email is short:")
    many = [house(Address=f"{100 + i} Elm St, Buena Park, CA 90620", Price=700000 + i * 1000)
            for i in range(40)]
    subject, body = sd.build_email(CRITERIA, many, ["Orange County", "Los Angeles"], "house")
    check("no more than MAX_CARDS houses are shown", body.count("Zillow &rarr;"),
          sd.MAX_CARDS)
    check("the rest are pointed at the app", "See all 40 in the app" in body, True)
    # Gmail clips anything over ~102 KB behind a "[Message clipped]" link,
    # which hides the bottom of the email -- the old long-form digest could
    # get there. Keep well clear.
    check(f"a full email stays under Gmail's clipping size ({len(body) // 1024} KB)",
          len(body) < 90_000, True)
    check("the subject names the markets and the count",
          subject, "Orange County + Los Angeles: 40 new homes")

    print("\nEvery card has a picture:")
    src, kind = sd.photo_url(house()["fields"])
    check("a house with coordinates gets a free aerial", kind, "aerial")
    check("...which needs no key", "key=" in src, False)
    check("...and is centred on the house",
          "bbox=-117.914850,33.836" in src, True)
    check("a feed photo wins when there is one",
          sd.photo_url(house(**{"Photo URL": "https://x/p.jpg"})["fields"])[1], "photo")
    check("no coordinates and no photo: no broken image",
          sd.photo_url(house(Latitude=None)["fields"]), ("", None))
    check("the aerial credit is shown when aerials are used",
          "Aerial imagery &copy; Esri" in body, True)

    print("\nEvery house links to Zillow:")
    _, b = sd.build_email(CRITERIA, many[:5], ["Orange County"], "house")
    check("one Zillow button per card", b.count("Zillow &rarr;"), 5)
    fc = house(**{"Source": "homesteps",
                  "Listing URL": "https://www.homesteps.com/listingdetails/x"})
    _, b = sd.build_email(CRITERIA, [fc], ["Orange County"], "house")
    check("a foreclosure keeps Zillow and adds its own page",
          ("zillow.com" in b, "HomeSteps &rarr;" in b), (True, True))
    check("the text version carries the Zillow link",
          "Zillow: https://www.zillow.com/homes/" in sd.text_summary([fc]), True)
    check("Ivan's app button opens his areas directly",
          "mls/?open=ivan" in sd.build_email(CRITERIA, [fc], ["Orange County",
                                                              "Los Angeles"])[1], True)
    check("Ryan's app button carries no code",
          "?open=" in sd.build_email(CRITERIA, [house(Market="Atlanta")], [])[1], False)

    print("\nNothing unescaped reaches the HTML:")
    evil = house(Address='<script>alert(1)</script> 1 "Q" St', **{
        "Value Signals": "<b>x</b>, built 1950"})
    _, b = sd.build_email(CRITERIA, [evil], ["Orange County"], None)
    check("an address with markup is escaped", "<script>" in b, False)
    check("a signal with markup is escaped", "<b>x</b>" in b, False)

    print("\nPrice drops read as drops:")
    drop = house(**{"Previous Price": 900000, "Price": 850000,
                    "Price Change Date": TODAY, "Date Added": OLD})
    subject, b = sd.build_email(CRITERIA, [drop], ["Orange County"], "house")
    check("the headline counts it as a drop", subject, "Orange County: 1 price drop")
    check("the card shows the dollars off", "$50,000 (5.6%)" in b, True)

    print("\nWho gets what:")
    houses = [
        house(Address="1 Atl Rd, Marietta, GA", Market="Atlanta"),
        house(Address="2 OC Ave, Anaheim, CA", Market="Orange County"),
        house(Address="3 LA Blvd, Downey, CA", Market="Los Angeles"),
        house(Address="4 Plex Way, Anaheim, CA", Market="Orange County",
              **{"Property Type": "Multi-Family", "Units": 4}),
        house(Address="5 Old St, Anaheim, CA", **{"Date Added": OLD}),
        house(Address="6 Gone St, Anaheim, CA", **{"Listing Status": "Under Contract"}),
        house(Address="7 Nope St, Anaheim, CA", Status="Rejected"),
        house(Address="8 Legacy Ln, Marietta, GA", Market=None),
    ]
    recipients = [("ryan@example.com", set()),
                  ("ivan@example.com", {"Orange County", "Los Angeles"})]
    cutoff = (date.today() - timedelta(days=1)).isoformat()

    def sent(lane):
        return {tuple(emails): sorted(r["fields"]["Address"][:1] for r in picked)
                for _, emails, _, picked in sd.plan_emails(
                    recipients, CRITERIA, houses, cutoff, lane)}

    check("homes: Ryan gets Atlanta only, Ivan gets OC + LA only",
          sent("house"), {("ryan@example.com",): ["1", "8"],
                          ("ivan@example.com",): ["2", "3"]})
    check("buildings: the fourplex goes to Ivan's buildings email, nobody else's",
          sent("multifamily"), {("ryan@example.com",): [],
                                ("ivan@example.com",): ["4"]})
    for _, emails, crit_rows, _ in sd.plan_emails(recipients, CRITERIA, houses, cutoff):
        names = [r["fields"]["Name"] for r in crit_rows]
        if emails == ["ryan@example.com"]:
            check("Ryan's footer never names Ivan's searches",
                  any("Ivan" in n for n in names), False)
        else:
            check("Ivan's footer never names Ryan's searches",
                  any("Ryan" in n for n in names), False)
    os.environ["EMAIL_TO"] = "someone@example.com"
    os.environ.pop("DIGEST_MARKETS", None)
    check("an EMAIL_TO override defaults to public markets, never private",
          sd.resolve_recipients(None), [("someone@example.com", set())])
    os.environ["DIGEST_MARKETS"] = "Orange County, Los Angeles"
    check("...and DIGEST_MARKETS opts a preview into private ones",
          sd.resolve_recipients(None),
          [("someone@example.com", {"Orange County", "Los Angeles"})])
    del os.environ["EMAIL_TO"], os.environ["DIGEST_MARKETS"]

    print("\nThe lane comes from the workflow's settings:")
    for env, want in (({"DIGEST_SEARCH": "Multifamily"}, "multifamily"),
                      ({"DIGEST_EXCLUDE": "Multifamily"}, "house"), ({}, None)):
        old = {k: os.environ.pop(k, None) for k in ("DIGEST_SEARCH", "DIGEST_EXCLUDE")}
        os.environ.update(env)
        check(f"{env or 'no scope'} -> {want}", sd.lane_from_env(), want)
        for k in ("DIGEST_SEARCH", "DIGEST_EXCLUDE"):
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in old.items() if v})

    print("\nA dry run renders every email and sends nothing:")

    class FakeDB:
        def list_records(self, table, formula=None):
            if table == sd.TABLE_RECIPIENTS:
                return [{"fields": {"Email": e, "Active": True, "Markets": ",".join(m)}}
                        for e, m in recipients]
            if table == sd.TABLE_CRITERIA:
                return CRITERIA
            return houses + many

    out = os.environ.get("DIGEST_OUT") or tempfile.mkdtemp()
    real_connect = sd.connect
    sd.connect = lambda: FakeDB()
    os.environ.update({"DIGEST_DRY_RUN": "1", "DIGEST_OUT": out,
                       "DIGEST_EXCLUDE": "Multifamily"})
    for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO", "DIGEST_SEARCH"):
        os.environ.pop(k, None)
    try:
        code = sd.main()
    finally:
        sd.connect = real_connect
        for k in ("DIGEST_DRY_RUN", "DIGEST_EXCLUDE"):
            os.environ.pop(k, None)
    check("dry run exits cleanly without SMTP credentials", code, 0)
    written = sorted(os.listdir(out))
    check("one preview per market group", len([w for w in written if w.startswith("house-")]), 2)
    print(f"        previews: {out}/")

    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        return 1
    print("\nAll digest checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
