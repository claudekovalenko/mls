#!/usr/bin/env python3
"""One-off diagnostic: what do HomePath and HomeSteps actually serve?

The robots probe said both allow crawling. Before writing a parser, this
fetches one search page per site plus the obvious JSON endpoints their own
front ends would use, and prints enough of each response to write the real
adapter from evidence instead of guesswork. Read the output in the workflow
log; nothing is stored.
"""
import gzip
import json
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TARGETS = [
    # HomePath (Fannie Mae REO). The site is an Angular app; these are the
    # API shapes such apps commonly hang off the same host.
    ("homepath search page", "https://www.homepath.com/homes-for-sale/GA/Marietta"),
    ("homepath api guess 1",
     "https://www.homepath.com/listing/api/search?city=Marietta&state=GA"),
    ("homepath api guess 2",
     "https://api.homepath.com/listings?city=Marietta&state=GA"),
    # HomeSteps (Freddie Mac REO).
    ("homesteps search page",
     "https://www.homesteps.com/homesteps/homesearch?state=GA&city=Marietta"),
    ("homesteps root", "https://www.homesteps.com/"),
]


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return resp.status, resp.headers.get("Content-Type", ""), raw


def main():
    for label, url in TARGETS:
        print("=" * 70)
        print(f"{label}: {url}")
        try:
            status, ctype, raw = fetch(url)
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001 -- diagnostic, report and move on
            print(f"  FAILED {e}")
            continue
        text = raw.decode("utf-8", errors="ignore")
        print(f"  HTTP {status}, {len(raw)} bytes, {ctype}")
        if "json" in ctype:
            try:
                doc = json.loads(text)
                print("  JSON keys:", list(doc)[:20] if isinstance(doc, dict)
                      else f"list of {len(doc)}")
            except ValueError:
                pass
        # The interesting parts of an HTML shell: embedded state and the
        # script URLs whose names reveal the API the page calls.
        for marker in ("__NEXT_DATA__", "window.__", "apiUrl", "api/", "/api",
                       "search", "listing"):
            i = text.find(marker)
            if i >= 0:
                print(f"  ...{marker!r} at {i}: {text[max(0, i - 60):i + 240]!r}")
        print("  head:", text[:400].replace("\n", " ")[:400])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
