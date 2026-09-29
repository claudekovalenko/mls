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
    # Round 2. Round 1 found Cobb's own ArcGIS server at
    # gis.cobbcounty.gov/gisserver -- keyless, public. Walk it: every folder,
    # every service, every layer, and the fields of anything that looks like
    # a parcel/tax layer. Owner name, mailing address, last sale, year built
    # and assessed value are the off-market signals; this finds where they sit.
    base = "https://gis.cobbcounty.gov/gisserver/rest/services"
    root = show("Cobb GIS root", base + "?f=json", peek=1500)
    folders = (root or {}).get("folders", []) if isinstance(root, dict) else []
    services = list((root or {}).get("services", [])) if isinstance(root, dict) else []
    for folder in folders:
        doc = show(f"folder {folder}", f"{base}/{folder}?f=json", peek=200)
        if isinstance(doc, dict):
            services.extend(doc.get("services", []))
    print("=" * 70)
    print(f"{len(services)} service(s):")
    for svc in services:
        print(f"  {svc.get('name')} ({svc.get('type')})")
    # Layers of anything parcel-, tax-, owner- or property-shaped.
    hits = [s for s in services if any(k in (s.get("name") or "").lower()
                                       for k in ("parcel", "tax", "owner", "property",
                                                 "assess", "land", "base"))]
    for svc in hits[:12]:
        url = f"{base}/{svc['name']}/{svc['type']}"
        doc = show(f"service {svc['name']}", url + "?f=json", peek=300)
        if not isinstance(doc, dict):
            continue
        for layer in (doc.get("layers") or [])[:40]:
            lid, lname = layer.get("id"), layer.get("name")
            ldoc = show(f"  layer {lname}", f"{url}/{lid}?f=json", peek=100)
            if isinstance(ldoc, dict):
                fields = [f.get("name") for f in (ldoc.get("fields") or [])]
                print(f"    fields({len(fields)}): {fields[:60]}")
                if any("own" in (f or "").lower() for f in fields):
                    # One real row, so the adapter is written against truth.
                    show(f"  sample row {lname}",
                         f"{url}/{lid}/query?" + urllib.parse.urlencode({
                             "where": "1=1", "outFields": "*", "resultRecordCount": "1",
                             "returnGeometry": "false", "f": "json"}), peek=2500)

    # Cobb Tax Commissioner and Assessor: find the real delinquent / tax sale
    # / bulk-data pages from their front pages.
    for label, url in (("cobbtax front", "https://www.cobbtax.org/"),
                       ("cobbassessor front", "https://www.cobbassessor.org/"),
                       ("cobbassessor robots", "https://www.cobbassessor.org/robots.txt")):
        text = show(label, url, peek=200)
        if isinstance(text, str):
            links = sorted(set(re.findall(r'href="([^"]*(?:delinq|tax-sale|taxsale|sale|download|data|digest|search)[^"]*)"', text, re.I)))[:25]
            print("  interesting links:", links)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
