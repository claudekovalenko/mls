#!/usr/bin/env python3
"""Diagnostic: which off-market data sources for Cobb County are actually
reachable, keyless, and machine-readable?

The off-market thesis: owners who have not listed but have a reason to
sell -- absentee, long tenure (equity), out of state, tax delinquent, in
probate, pre-foreclosure. All of that is public record. This probes the
places it is published and prints response shapes, so the adapters get
written from evidence. Reads a few pages, stores nothing, spends no
RentCast calls.
"""
import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return resp.status, resp.headers.get("Content-Type", ""), raw


def show(label, url, peek=600):
    print("=" * 70)
    print(f"{label}: {url}")
    try:
        status, ctype, raw = fetch(url)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED {e}")
        return None
    text = raw.decode("utf-8", errors="ignore")
    print(f"  HTTP {status}, {len(raw)} bytes, {ctype}")
    if "json" in ctype or text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
            print("  JSON:", json.dumps(doc, indent=1)[:peek])
            return doc
        except ValueError:
            pass
    print("  head:", text[:peek].replace("\n", " "))
    return text


def main():
    # 1. ArcGIS Hub: the open-data catalog most counties publish parcels to.
    #    Keyless JSON. Find Cobb's parcel layer and its FeatureServer URL.
    hub = show("ArcGIS Hub search: Cobb parcels",
               "https://hub.arcgis.com/api/v3/datasets?"
               + urllib.parse.urlencode({"q": "Cobb County Georgia parcels",
                                         "page[size]": "8"}))
    if isinstance(hub, dict):
        for d in hub.get("data", [])[:8]:
            a = d.get("attributes", {})
            print(f"  - {a.get('name')!r} owner={a.get('owner')} "
                  f"url={a.get('url')} fields={[f.get('name') for f in (a.get('fields') or [])][:25]}")

    # 2. Cobb County's own GIS: the usual ArcGIS REST roots.
    for root in ("https://gis.cobbcounty.org/arcgis/rest/services?f=json",
                 "https://gis.cobbcountyga.gov/arcgis/rest/services?f=json",
                 "https://cobbgis.cobbcounty.org/arcgis/rest/services?f=json",
                 "https://services.arcgis.com/oXNMbSEnjjVzlt8k/arcgis/rest/services?f=json"):
        show("Cobb ArcGIS REST root", root, peek=800)

    # 3. Tax delinquency: the Cobb Tax Commissioner publishes delinquent
    #    lists and tax-sale notices.
    show("Cobb Tax Commissioner robots", "https://www.cobbtax.org/robots.txt")
    show("Cobb tax sale page", "https://www.cobbtax.org/property/tax-sale")
    show("Cobb delinquent page", "https://www.cobbtax.org/property/delinquent-taxes")

    # 4. Georgia Public Notice: foreclosure and probate legal ads, statewide,
    #    published to be found.
    show("GA public notice robots", "https://www.georgiapublicnotice.com/robots.txt")
    show("GA public notice search",
         "https://www.georgiapublicnotice.com/Search.aspx?"
         + urllib.parse.urlencode({"County": "Cobb", "Category": "Foreclosure"}))

    # 5. Cobb Superior Court / probate.
    show("Cobb probate court robots", "https://www.cobbcounty.org/robots.txt")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
