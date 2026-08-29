# House Finder

A flip / BRRRR deal finder. You describe what you're looking for, a scheduled
worker searches listing feeds and scores every result, and anything that clears
your targets shows up in a phone app — best deal in each market first.

- **App:** https://claudekovalenko.github.io/mls/ (installable — Share → Add to Home Screen)
- **Database:** Supabase (Postgres)
- **Worker:** GitHub Actions, weekly

---

## How it fits together

```
"search_criteria"  ──►  search_worker.py  ──►  "houses"  ──►  PWA
 (what you want)        (fetch, filter,        (scored)      (browse,
                         score, dedupe)                       edit, decide)
```

Supabase is the database of record. The app talks to PostgREST directly from
your browser with the project's anon key; row-level security in the schema
decides what that key can touch. There is no backend of ours in the middle.

## Setup

### 1. Create the project

supabase.com → new project. Then open the **SQL editor** and run
[`supabase/schema.sql`](supabase/schema.sql) — it builds both tables, the
uniqueness rules the worker upserts against, and the access policies.

### 2. Connect the app

Open the app → **Settings → API** in Supabase gives you the **Project URL** and
the **anon public** key. Paste both into the app's setup screen. They are
stored only in that browser.

### 3. Let the worker in

Add two repo secrets at github.com → Settings → Secrets → Actions:

| Secret | Value |
| --- | --- |
| `SUPABASE_URL` | the project URL |
| `SUPABASE_SERVICE_KEY` | the **service role** key, not the anon key — the worker writes rows |

Every workflow picks Supabase up automatically once both exist; there is no
switch to flip.

### 3. Connect the app

Open the site and paste the project URL and anon key into the setup screen.
They are kept in `localStorage` on that device and sent only to your own
Supabase project. Nothing is committed to this repo and nothing is shared
between devices — each phone or laptop connects once.

### 4. Point the worker at a listing feed

Repo → Settings → Secrets and variables → Actions:

| Name | Kind | Value |
|---|---|---|
| `LISTINGS_API_TYPE` | variable | `reso` or `rentcast` |
| `LISTINGS_API_URL` | secret | RESO OData endpoint (reso only) |
| `LISTINGS_API_KEY` | secret | bearer token for that feed |
| `RENTCAST_API_KEY` | secret | rentcast only |

Then run **Actions → Search Listings → Run workflow** to test it.

### 5. Email digests

`send_digest.py` mails anything newly listed or newly cheaper, daily at 8am
Atlanta. There are two: **Email Digest** for houses and **Email Multifamily
Digest** for 20+ door complexes. Both stay quiet when nothing changed.
Secrets: `SMTP_USER` (Gmail address), `SMTP_PASS` (a Gmail **App Password**,
not the account password). Recipients live in the `recipients` table.

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

## Why Postgres

The database was Airtable until August 2026. Postgres fixes two real
weaknesses beyond the connector dropping constantly: it enforces uniqueness
(Airtable could not, which is how a duplicate criteria row went unnoticed for
weeks, costing an API call a run), and Row Level Security lets the app hold a
public key that can only do the app's job, where an Airtable token could read
and write everything in the base.

It is also plain Postgres, so nothing here is locked to Supabase — `pg_dump`
moves the whole thing to any other host with the same schema.

## Upgrade paths, for when they're wanted

Nothing here is set up, and none of it is needed for the pipeline to run. This
is the shortlist as of August 2026 so the decision doesn't have to be researched
again from scratch. Prices drift; check before buying.

The gap all of these close is the same one: RentCast returns no listing
remarks, so "ugly", "dated", "as-is", "unfinished basement" and FSBO can never
be evidenced directly. The worker infers them from age, days on market and
price cuts (see *How deals are scored*), which is a shadow of the real thing.

**Free, and the fastest fix — Redfin / Zillow saved searches.** Both support
keyword matching against listing remarks, which is exactly the text this feed
lacks. A saved alert for Marietta under $500k matching `as-is`, `fixer`,
`unfinished basement`, `estate sale`, `bring your vision` costs nothing and
starts working immediately. It cannot be automated into this repo — that is the
scraping problem above — so it lands in a human's inbox alongside the digest
rather than in the Houses table. Anything worth keeping gets added by hand,
which the app already supports and cleanup already protects.

**~$99/month — PropStream.** The one that covers what neither this pipeline nor
an MLS feed does: distress filters over county records — pre-foreclosure, tax
lien, vacant, absentee owner, high equity. Those properties are usually not
listed anywhere, so they are invisible to any MLS-derived source including IDX.
Worth trialing against a single Marietta zip first; if the Cobb County distress
data is thin, it isn't worth the money. No integration exists; export CSV and
import, or wire an adapter alongside `fetch_rentcast`.

**~$149/month — Privy.** Built for flippers specifically; surfaces what other
investors in the market are buying and rehabbing. A different signal from
everything else here, and the only one that is about competitors rather than
properties.

**MLS fees, typically $30-60/month — FMLS / GAMLS direct (RESO).** Still the
best fit for *this* repo, because the adapter already exists and it is a
two-secret change. Full remarks, basement fields, photos, price history. Needs
a licensed Georgia agent to sponsor the feed, which is the whole obstacle.

**Roughly $300+/month — ATTOM, CoreLogic, Datafiniti.** Descriptions included,
no agent sponsorship needed. Priced for firms, not for one buyer's search.

A reasonable order, if it's ever wanted: free keyword alerts first, then ask
whether an agent can sponsor IDX, and only then pay for a platform.

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

**Value signals** are the other way in. The searches hunt overlooked value —
ugly, dated, mismarketed, expandable — so the worker tags every listing whose
remarks or numbers show: `Basement`, `ADU potential`, `FSBO`, `Fixer`,
`No sqft listed`, `Oversized lot` (15k+ sqft). Two or more signals earns a spot
in Matches even when the placeholder math says PASS, because placeholder math
is exactly what's wrong about a mispriced house. Missing sqft deliberately
passes both the `Min Sqft` floor and the `Max Price Per Sqft` cap.

Criteria rows can also set `Zip Codes` (a ring of zips is how "within 10 miles"
is expressed), `Must Haves` (comma = AND, `/` = alternatives, e.g.
`basement, adu/oversized lot`), and `Max All In` (price + estimated rehab cap).

One honest limitation: **FSBO / off-market houses never appear in MLS or
RentCast feeds.** The FSBO signal catches agent-listed homes whose remarks
mention it, but true off-market finds have to be added to the Houses table by
hand.

**Rehab cost and ARV cannot be known from a listing feed.** The worker seeds
rehab from your search's `Rehab Cost Per Sqft` and uses list price as an ARV
placeholder. Both are meant to be overwritten — open a house in the app, type
real numbers, and every verdict updates live before you save.

## Repo layout

```
docs/            the PWA (GitHub Pages)
  index.html     screens: setup, matches, searches
  app.js         Supabase client, deal math, rendering
  style.css
  sw.js          caches the app shell only, never API responses
  manifest.json
scripts/
  schema.py      field names and allowed values (source of truth)
  db.py          connect() -- the one import every script uses
  supabase_db.py PostgREST client
  deals.py       flip/BRRRR math and qualification
  search_worker.py   the scheduled search + value signals
  send_digest.py     the email digests
  cleanup_houses.py  removes rows the current rules would not write
  check_schema_sync.py  fails the build if schema.py, the SQL and app.js drift
  probe_sources.py   the scraping-feasibility probe
supabase/schema.sql  the physical mirror of schema.py
.github/workflows/
```

## Local use

```sh
cd scripts
export SUPABASE_URL=https://….supabase.co SUPABASE_SERVICE_KEY=…
python search_worker.py
```
