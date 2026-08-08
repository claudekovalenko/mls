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

HOSTS = [
    "https://www.zillow.com",
    "https://www.redfin.com",
    "https://www.realtor.com",
    "https://www.trulia.com",
    "https://www.homes.com",
]

# Paths we'd need if we were to pull search results from each site.
PROBE_PATHS = {
    "https://www.zillow.com": ["/homes/", "/async-create-search-page-state"],
    "https://www.redfin.com": ["/stingray/api/gis-csv", "/city/"],
    "https://www.realtor.com": ["/realestateandhomes-search/", "/api/"],
    "https://www.trulia.com": ["/for_sale/"],
    "https://www.homes.com": ["/for-sale/"],
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
