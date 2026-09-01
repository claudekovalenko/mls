#!/usr/bin/env python3
"""One-off diagnostic: what listing data sources are actually reachable, and
what do their robots.txt files permit? Run manually via workflow_dispatch.

This exists to replace guesswork with evidence before committing to a data
source. It only fetches robots.txt (which is explicitly published for exactly
this purpose) plus a single homepage HEAD-equivalent per host to check
reachability -- it does not scrape any listing data.
"""
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The big portals, probed first and all found closed -- kept so the finding
# stays reproducible rather than becoming folklore.
HOSTS = [
    "https://www.zillow.com",
    "https://www.redfin.com",
    "https://www.realtor.com",
    "https://www.trulia.com",
    "https://www.homes.com",
    # Second round: the places a portal cannot show you.
    #
    # Deliberately NOT probed: Facebook Marketplace and Craigslist. Both
    # require an authenticated session and both prohibit automated access in
    # terms, so the answer is no regardless of what a robots.txt says, and
    # probing them would only manufacture a number to argue with.
    #
    # County records are the real prize here. Tax-delinquent, code-violation
    # and probate properties are motivated sellers who never list anywhere,
    # so they are invisible to every portal AND to IDX -- which makes them
    # the closest thing to the brief's "value others overlook".
    "https://www.cobbtax.org",
    "https://cobbcounty.org",
    # Foreclosure and auction inventory, published to be found.
    "https://www.auction.com",
    "https://www.xome.com",
    "https://www.hubzu.com",
    # FSBO, which is explicitly in the brief and absent from MLS feeds.
    "https://www.forsalebyowner.com",
    "https://fsbo.com",
    # Third round: government and lender-owned inventory. These sites exist
    # to move houses -- their listings are published to be found, several
    # publish open data outright, and none of them appear in MLS feeds.
    "https://www.hudhomestore.gov",
    "https://www.homepath.com",
    "https://www.homesteps.com",
    "https://www.treasury.gov",
    "https://data.cobbcountyga.gov",
]

# Paths we'd need if we were to pull search results from each site.
PROBE_PATHS = {
    "https://www.zillow.com": ["/homes/", "/async-create-search-page-state"],
    "https://www.redfin.com": ["/stingray/api/gis-csv", "/city/"],
    "https://www.realtor.com": ["/realestateandhomes-search/", "/api/"],
    "https://www.trulia.com": ["/for_sale/"],
    "https://www.homes.com": ["/for-sale/"],
    "https://www.cobbtax.org": ["/", "/property-tax", "/delinquent-tax"],
    "https://cobbcounty.org": ["/", "/tax-commissioner"],
    "https://www.auction.com": ["/residential/", "/search"],
    "https://www.xome.com": ["/auctions", "/search"],
    "https://www.hubzu.com": ["/search", "/property"],
    "https://www.forsalebyowner.com": ["/search", "/listing"],
    "https://fsbo.com": ["/listings", "/search"],
    "https://www.hudhomestore.gov": ["/Listing/PropertySearch", "/api"],
    "https://www.homepath.com": ["/homes-for-sale", "/api"],
    "https://www.homesteps.com": ["/homesteps/homesearch", "/search"],
    "https://www.treasury.gov": ["/auctions/treasury/rp"],
    "https://data.cobbcountyga.gov": ["/", "/browse"],
}


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def parse_robots(text):
    """Return disallow rules that apply to a generic crawler (User-agent: *)."""
    rules, applies = [], False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            applies = (value == "*")
        elif applies and field in ("disallow", "allow"):
            rules.append((field, value))
    return rules


def path_verdict(rules, path):
    """Longest-match wins, per the robots.txt convention."""
    best = None
    for field, value in rules:
        if value and path.startswith(value):
            if best is None or len(value) > len(best[1]):
                best = (field, value)
    if best is None:
        return "ALLOWED (no matching rule)"
    return f"{'DISALLOWED' if best[0] == 'disallow' else 'ALLOWED'} by rule '{best[1]}'"


def main():
    for host in HOSTS:
        print(f"\n{'=' * 60}\n{host}\n{'=' * 60}")
        try:
            status, body = fetch(host + "/robots.txt")
            print(f"robots.txt: HTTP {status}, {len(body)} bytes")
            rules = parse_robots(body)
            print(f"  parsed {len(rules)} rules for User-agent: *")
            for path in PROBE_PATHS.get(host, []):
                print(f"  {path} -> {path_verdict(rules, path)}")
        except urllib.error.HTTPError as e:
            print(f"robots.txt: HTTP ERROR {e.code}")
        except Exception as e:
            print(f"robots.txt: FAILED {e}")

        try:
            status, body = fetch(host + "/")
            print(f"homepage reachable: HTTP {status}, {len(body)} bytes")
        except urllib.error.HTTPError as e:
            print(f"homepage: HTTP ERROR {e.code} (blocked to automated clients)")
        except Exception as e:
            print(f"homepage: FAILED {e}")


if __name__ == "__main__":
    main()
