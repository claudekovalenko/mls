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

### Coming from Airtable?

`scripts/migrate_to_supabase.py` copies the rows over. It never writes to
Airtable, and re-running it is safe. Run **Actions → Migrate to Supabase** with
the dry-run box ticked first to see what it would do.

---

## The old Airtable setup

Kept for reference while the migration finishes. Nothing new should be built
against it.

### 1. Get a token

airtable.com/create/tokens → scopes `data.records:read`, `data.records:write`,
`schema.bases:read`, `schema.bases:write`. Grant it on your whole workspace if
you want the next step to create the base for you, or on one existing base
otherwise.

### 2. Build the base

Don't create the forty-odd fields by hand — `bootstrap_base.py` builds both
tables from `SCHEMA` in [`scripts/airtable.py`](scripts/airtable.py), with the
right number precision and select options:

**From a phone**, where there's no shell: add `AIRTABLE_TOKEN` and
`AIRTABLE_BASE_ID` as repo secrets (step 4), then run
**Actions → Set Up Airtable Base → Run workflow**.

**From a terminal:**

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

### 5. Email digest (optional)

`send_digest.py` mails the active criteria plus anything new, daily at 8am ET.
Quiet by default when nothing turned up; run **Actions → Email Digest** with
*force* checked to send a kickoff email announcing the criteria. Secrets:
`SMTP_USER` (Gmail address), `SMTP_PASS` (a Gmail **App Password**, not the
account password), `EMAIL_TO` (comma-separated recipients).

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

## Moving to Supabase

Airtable is still the database of record. Supabase is built, tested and
ready, and the switch is one environment variable — but it needs a project
that only the owner can create, so the last three steps are theirs.

Why move at all: the Airtable connector drops constantly, which is why half
the tooling in `.github/workflows` exists to do from a runner what should be
one tap in a UI. Supabase also fixes two real weaknesses rather than just the
flakiness — Postgres enforces uniqueness (Airtable cannot, which is how a
duplicate criteria row went unnoticed for weeks costing an API call a run),
and Row Level Security lets the PWA hold a public key that can only do the
app's job, where today it holds an Airtable token that can read and write
everything.

1. Create a project at supabase.com (free tier is ample here).
2. Run [`supabase/schema.sql`](supabase/schema.sql) in the SQL editor.
3. Add two repo secrets from Settings → API:
   `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the `service_role` key — it
   bypasses RLS, so it belongs in secrets and never in a browser).
4. Run the **Migrate to Supabase** workflow with dry-run ticked, read the
   counts, then run it again unticked. It reads Airtable and writes Supabase;
   it never modifies Airtable, and it is idempotent, so it can be re-run to
   pick up whatever the searches added since.
5. Set the `DB_BACKEND` repo variable to `supabase`.

Step 5 is the only irreversible-feeling one, and it isn't: set it back to
`airtable` and everything reads from Airtable again, because the migration
never removed anything. Leave both populated for a week before deleting
anything.

`scripts/db.py` picks the backend: `DB_BACKEND` when set, otherwise Supabase
if its credentials exist and Airtable if they don't. Both clients speak the
same `{"id", "fields"}` record shape, so no caller knows which one it got.

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
  app.js         Airtable client, deal math, rendering
  style.css
  sw.js          caches the app shell only, never API responses
  manifest.json
scripts/
  airtable.py    REST client + SCHEMA (source of truth)
  bootstrap_base.py  builds the base/tables from SCHEMA
  deals.py       flip/BRRRR math and qualification
  search_worker.py   the scheduled search + value signals
  send_digest.py     daily email digest
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
