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
    # Round 2. www.homepath.com is only a shell that client-side redirects to
    # homepath.fanniemae.com -- probe the real host, and pull its app bundle
    # to read the API paths the front end actually calls.
    ("homepath real host robots", "https://homepath.fanniemae.com/robots.txt"),
    ("homepath real search page",
     "https://homepath.fanniemae.com/homes-for-sale/GA/Marietta"),
    # HomeSteps advertises /listing/search in its own structured data.
    ("homesteps listing search",
     "https://www.homesteps.com/listing/search?search=Marietta%20GA"),
]

BUNDLE_HOST = "https://homepath.fanniemae.com"


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
        # Pull the SPA's own JS bundles and read the API paths out of them.
        if "fanniemae" in url and "html" in ctype:
            import re
            srcs = re.findall(r'src="(/[^"]+\.js)"', text)[:4]
            for src in srcs:
                try:
                    _, _, js = fetch(BUNDLE_HOST + src)
                except Exception as e:  # noqa: BLE001
                    print(f"  bundle {src}: FAILED {e}")
                    continue
                jtext = js.decode("utf-8", errors="ignore")
                hits = sorted(set(re.findall(
                    r'["\'](/[a-zA-Z0-9_\-]*api[a-zA-Z0-9_/\-]*)["\']', jtext)))[:30]
                print(f"  bundle {src}: {len(js)} bytes, api paths: {hits}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
