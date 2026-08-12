# House Finder

A flip / BRRRR deal finder. You describe what you're looking for, a scheduled
worker searches listing feeds and scores every result, and anything that clears
your targets shows up in a phone app — best deal in each market first.

- **App:** https://claudekovalenko.github.io/mls/ (installable — Share → Add to Home Screen)
- **Database:** Airtable
- **Worker:** GitHub Actions, every 6 hours

---

## How it fits together

```
Airtable "Search Criteria"  ──►  search_worker.py  ──►  Airtable "Houses"  ──►  PWA
   (what you want)               (fetch, filter,          (scored results)      (browse,
                                  score, dedupe)                                 edit, decide)
```

Airtable is the database of record. The app talks to Airtable's REST API
directly from your browser; there is no backend of ours in the middle.

## Setup

### 1. Get a token

airtable.com/create/tokens → scopes `data.records:read`, `data.records:write`,
`schema.bases:read`, `schema.bases:write`. Grant it on your whole workspace if
you want the next step to create the base for you, or on one existing base
otherwise.

### 2. Build the base

Don't create the forty-odd fields by hand — `bootstrap_base.py` builds both
tables from `SCHEMA` in [`scripts/airtable.py`](scripts/airtable.py), with the
right number precision and select options:

```sh
cd scripts

# make a blank base called "House Finder" in Airtable, grant the token on it,
# then just:
AIRTABLE_TOKEN=pat… python bootstrap_base.py

# or let it create the base (wsp… is in the Airtable URL)
AIRTABLE_TOKEN=pat… AIRTABLE_WORKSPACE_ID=wsp… python bootstrap_base.py
```

You don't need to hunt down a base ID — it finds the base from the token and
prints the ID when it's done. If it can't identify one it lists every base the
token can see, with IDs, so you can re-run with `AIRTABLE_BASE_ID=app…`. Re-running is safe: it only adds fields
that are missing and never renames, retypes, or deletes anything, so columns you
add yourself survive.

### 3. Connect the app

Open the site, paste the token, and hit **Find my base** to fill the ID in (or
paste it yourself). They're kept in `localStorage` on
that device and sent only to `api.airtable.com`. Nothing is committed to this
repo and nothing is shared between devices — each phone/laptop connects once.

### 4. Connect the worker

Repo → Settings → Secrets and variables → Actions:

| Name | Kind | Value |
|---|---|---|
| `AIRTABLE_TOKEN` | secret | same token as above |
| `AIRTABLE_BASE_ID` | secret | `app…` |
| `LISTINGS_API_TYPE` | variable | `reso` or `rentcast` |
| `LISTINGS_API_URL` | secret | RESO OData endpoint (reso only) |
| `LISTINGS_API_KEY` | secret | bearer token for that feed |
| `RENTCAST_API_KEY` | secret | rentcast only |

Then run **Actions → Search Listings → Run workflow** to test it.

## Where listings come from

The worker has two adapters:

**RESO Web API (`reso`)** — the standard MLS/IDX interface, and the one worth
having. It gives you the real, complete, current listing set for a market with
photos and full remarks. Access is per-MLS and normally requires a licensed
agent to sponsor the feed:

- Atlanta → FMLS and/or GAMLS
- Los Angeles → CRMLS

Bridge Interactive (Zillow-owned) resells several of these for free once the MLS
approves you. Once you have a URL and key, it's a two-secret change — no code.

**RentCast (`rentcast`)** — a stopgap. Broad coverage, no MLS approval needed,
but a metered free tier and thinner data. Fine for proving the pipeline out.

### Why there's no scraper

Zillow, Redfin, Realtor.com, Trulia and Homes.com were all probed directly (see
[`scripts/probe_sources.py`](scripts/probe_sources.py), run from a GitHub Actions
runner). Every one of them either disallows the search paths in its own
`robots.txt` or blocks automated clients outright:

| Site | robots.txt on the paths we'd need | Response to automation |
|---|---|---|
| Zillow | `/homes/` disallowed | 200 on homepage only |
| Redfin | `/stingray/` disallowed | 405 |
| Realtor.com | allowed | 429 |
| Trulia | allowed | 403 |
| Homes.com | robots.txt itself 403s | 403 |

Building something to get around that would be against those sites' terms, on a
repo in your name, and would break constantly anyway. IDX is the path that
actually works.

## How deals are scored

`scripts/deals.py` is the reference implementation; `docs/app.js` mirrors it so
the numbers you see while editing match what the worker wrote.

**Flip**
- 70% rule max offer: `ARV × 0.70 − rehab`
- Profit: `ARV − price − rehab − (ARV × 8%)` selling costs
- ROI: profit ÷ cash in
- STRONG = clears the 70% rule, ≥$50k profit, ≥15% ROI · GOOD = ≥$25k, ≥10% · PASS = no profit

**BRRRR**
- Refi at 75% of ARV, 7% over 30 years
- Operating expenses at 50% of rent
- Cash left in deal = `price + rehab − refi`
- STRONG = all capital out, or ≥12% cash-on-cash · GOOD = ≥8% · PASS = negative cashflow

A house shows **QUALIFIED** when it also clears the per-search targets on its
`Search Criteria` row, so a strict search and a loose one can run side by side.

**Rehab cost and ARV cannot be known from a listing feed.** The worker seeds
rehab from your search's `Rehab Cost Per Sqft` and uses list price as an ARV
placeholder. Both are meant to be overwritten — open a house in the app, type
real numbers, and every verdict updates live before you save.

## Repo layout

```
docs/            the PWA (GitHub Pages)
  index.html     screens: setup, matches, searches
  app.js         Airtable client, deal math, rendering
  style.css
  sw.js          caches the app shell only, never API responses
  manifest.json
scripts/
  airtable.py    REST client + SCHEMA (source of truth)
  bootstrap_base.py  builds the base/tables from SCHEMA
  deals.py       flip/BRRRR math and qualification
  search_worker.py   the scheduled search
  migrate_to_airtable.py
  probe_sources.py   the scraping-feasibility probe
.github/workflows/search.yml
```

## Local use

```sh
cd scripts
export AIRTABLE_TOKEN=pat… AIRTABLE_BASE_ID=app…
python search_worker.py
```
