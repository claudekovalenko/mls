# MLS House Tracker

A site for tracking houses we're considering buying across two markets (Atlanta and Los Angeles)
— status, ratings, notes — plus a daily email of new Atlanta listings matching our filter
criteria, with the ability to "like" listings straight from a reply email.

This currently covers the **purchasing/shopping phase**. Once we own a house, this can expand
into ownership/management (maintenance, docs, expenses, etc.) — but `houses.json` and the
tracker UI are intentionally simple for now.

## Installing as an app on your phone

The tracker is a Progressive Web App (PWA) — it can be installed to your home screen and opens
full-screen like a native app, no App Store needed:

- **iPhone (Safari)**: open the site, tap the Share icon, tap **Add to Home Screen**.
- **Android (Chrome)**: open the site, tap the ⋮ menu, tap **Add to Home screen** (or **Install app**).

## How it works

- `houses.json` — every house we're tracking: address, price, `priceHistory` (logged
  automatically whenever the price changes), beds/baths/sqft, listing URL, photo URL, status
  (Interested / Touring Scheduled / Toured / Offer Made / Under Contract / Purchased / Rejected),
  star rating, notes, `liked` flag, `source` (`manual` or `email`), `market` (Atlanta or Los
  Angeles — defaults to Atlanta if absent, for houses added before this field existed), `addedBy`
  (Ryan or Ivan), date added, and flip/BRRRR analysis inputs: `rehabCost`, `arv`, `rentEstimate`.
- `docs/index.html` — the House Tracker (see below), published via GitHub Pages. A floating
  bottom nav switches between two full screens: **Houses** (All Houses / Highlights tabs — add,
  edit, heart, filter, sort) and **Calculator** (see below).
- `scripts/refresh_listings.py` — best-effort re-check (every 2 hours) of each house's listing URL
  for a new price or photo (via Open Graph tags). Every house is refreshed independently, so one
  blocked/broken listing never stops the rest or fails the workflow. Anti-scraping sites (Zillow
  in particular) reliably block this — confirmed via 403 responses even from GitHub's own
  servers — so it usually won't do anything for Zillow listings specifically; price/photo can
  always be edited by hand instead.
- `.github/workflows/refresh-listings.yml` — runs `refresh_listings.py` every 2 hours and commits
  any price/photo updates it manages to find.
- `criteria.json` — the daily email alert's search filter (location, price range, beds/baths,
  property types, keywords, recipient email). Keywords are matched (case-insensitive) against
  each listing's description/remarks/title/address — a listing only needs to contain one of them.
- `scripts/send_alert.py` — reads `criteria.json`, fetches listings from a data source, filters
  them, numbers them in the email body, saves that day's list to `last_alert_listings.json`, and
  emails the matches.
- `scripts/process_replies.py` — polls the Gmail inbox (IMAP) for replies to alert emails
  containing a line like `LIKE 1,3`, matches those numbers against `last_alert_listings.json`,
  and appends them to `houses.json` as highlighted houses (`liked: true`, `source: "email"`).
- `.github/workflows/daily-alert.yml` — runs `send_alert.py` daily at 14:00 UTC and commits the
  day's listing snapshot.
- `.github/workflows/process-email-replies.yml` — runs `process_replies.py` every 30 minutes and
  commits any newly liked houses.
- `docs/alert.html` — the daily alert filter settings page, linked from the tracker.

## One-time setup

1. **Enable GitHub Pages**: Settings → Pages → Source: Deploy from a branch → Branch `main`,
   folder `/docs`. The tracker will be live at `https://claudekovalenko.github.io/mls/`.
2. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `GMAIL_USER` / `GMAIL_APP_PASSWORD` — a Gmail address + an
     [app password](https://myaccount.google.com/apppasswords). Used both to send the daily
     alert (SMTP) and to check for "LIKE" replies (IMAP) — make sure
     [IMAP access](https://support.google.com/mail/answer/7126229) is enabled on the account.
   - `LISTINGS_API_URL` / `LISTINGS_API_KEY` — your listings data source. The script expects a
     JSON array of objects like `{price, beds, baths, address, url, propertyType}`. Point this
     at whatever feed you're using (an MLS/IDX API, a broker feed, etc.) — without it, the script
     runs but skips fetching and sends "no matches" each day.
   - `RENTCAST_API_KEY` (optional) — a free API key from [rentcast.io](https://rentcast.io) used
     to fill in beds/baths/sqft/estimated value for tracked houses by address, as a fallback for
     whatever the listing-page scrape couldn't get (which is most of the time for sites like
     Zillow that block scraping outright). Hard-capped at 45 calls/month (5 below RentCast's free
     50-call limit) via `rentcast_usage.json`, and only queried once per house ever. Price filled
     in this way is a valuation estimate, not the real listing price — shown with a `~` prefix in
     the tracker.
   - `GOOGLE_MAPS_API_KEY` (optional) — a Google Maps Platform API key with the **Street View
     Static API** enabled, used as a photo fallback when a listing has no photo (again, mainly
     for Zillow). Requires a Google Cloud project with billing enabled — new accounts get a
     one-time $300/90-day trial credit (not a recurring monthly amount). Actual cost here is
     small regardless: the metadata check is free, and only the image fetch is billed
     (~$0.007/call), so the 100-call/month hard cap tops out around **$0.35/month** even with
     zero trial credit remaining. Only queried once per house ever, tracked in
     `streetview_usage.json`. Fetched photos are saved into `docs/photos/` and committed — the
     API key itself is never exposed in `houses.json` or anywhere public.
3. Without secrets configured, the alert workflow still runs and prints the would-be email to the
   Action log instead of sending it, and the reply-processing workflow just skips checking.
4. **Create your GitHub token** at
   [github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta), scoped to
   only this repo (`Repository access` → `Only select repositories` → `mls`). Under
   `Permissions`, click **+ Add permissions** and add:
   - **Contents** → Read and write (required — lets it save houses/criteria)
   - **Actions** → Read and write (lets it instantly trigger the price/beds/baths/photo auto-fill
     right after you add a house; without it, houses still get filled in by the next scheduled
     run, within 2 hours)

   Paste the generated token into the tracker's "GitHub access token" section and click
   **Save Token**, then **Test Token** to confirm it actually has write access before relying on
   it.

## Using the House Tracker

Open the site (`docs/index.html`), paste a GitHub token once (instructions are on the page), and:

- **Paste a listing URL and hit Add** — the quickest way to add a house. It's saved instantly;
  the address is parsed straight out of the URL text itself (works for Zillow, Redfin, etc, with
  no network request involved), so it usually shows the real street address right away. If your
  token has the Actions permission, it also immediately kicks off the price/beds/baths/photo
  lookup (RentCast + Street View) instead of waiting for the next scheduled run — and if either
  service's monthly limit has already been hit, the status message tells you so instead of
  silently doing nothing. Fill in price/status/notes later via **Edit** if you want.
- **+ Add with details** — the full form, for adding price/beds/baths/status/rating/notes up
  front instead of a bare URL.
- Click the heart (♡/♥) on any row to highlight it — highlighted houses show up on the
  **Highlights** tab.
- Click **Edit** on any row to update its status/rating/notes as you go through the process, or
  delete it.
- Filter by market (Atlanta / Los Angeles / All), status, and sort by date added / price /
  rating / address. Pick the market when adding a house (quick-add or the full form) — it's
  remembered per-browser as the default for next time.
- **Calculator screen** (bottom nav) — a live, editable flip/BRRRR calculator. Type in
  Price/Rehab Cost/ARV/Rent Estimate directly (a blank scratch-pad calculator), or pick a saved
  house from the dropdown — or just tap any card below — to load its numbers in; edit them and
  hit **Save these numbers to the loaded house** to write changes back. Shows the 70% rule max
  offer, estimated flip profit (assumes 8% selling costs), BRRRR cash left in the deal after a
  75%-LTV refinance, BRRRR cash-on-cash return (assumes 7%/30yr on the refi loan and the 50% rule
  for operating expenses), and the 1% rule ratio, all updating live as you type. Below the
  calculator, every house with Rehab Cost or ARV already filled in shows as its own card. Rent
  Estimate auto-fills from RentCast when available, same as price/beds/baths. These are quick
  screening heuristics, not underwriting — always verify real numbers before making an offer.
  Note: true Rehab Cost and true ARV can't be scraped — no data source knows what a specific
  house needs in repairs or what it'll actually be worth after renovation. That said,
  `refresh_listings.py` now auto-fills **placeholder estimates** for both whenever they're
  empty, so the calculator isn't blank by default: ARV defaults to RentCast's current-value
  estimate (a proxy, not a true after-repair projection), and Rehab Cost defaults to
  $20/sqft (a generic "light cosmetic rehab" assumption). Both show with a `~` prefix and an
  explanatory tooltip in the tracker so they're clearly marked as placeholders, not real
  numbers — editing either through the Edit dialog or the Calculator's save button replaces the
  estimate with your real number and clears the "estimated" flag.
- **Filter criteria + Best Deal banner** (Calculator screen) — set a minimum Flip Profit, minimum
  BRRRR Cash-on-Cash %, and/or minimum 1% Rule %, saved per-browser. Any house that clears every
  filter you've set gets a **PASSES** badge and a highlighted border on its card; the single
  best-scoring one (by flip profit) is called out in a large banner at the top so it's impossible
  to miss. If nothing currently clears your bar, the banner says so plainly instead of going
  silent. A separate Market dropdown (Atlanta / Los Angeles / All markets) narrows the
  candidates list and Best Deal pick to one market at a time, also saved per-browser.

All changes commit straight to `houses.json` on `main`. You can also edit that file directly in
GitHub if you prefer.

## Adding listings from the daily alert email

Each listing in the alert email has an **"+ Add to Tracker" button**. Click it and it opens
`docs/add.html`, which adds that listing straight into `houses.json` via the GitHub API — using
the same browser-saved GitHub token as the tracker (first click on a new device/browser asks for
the token once, then remembers it). Houses added this way are marked highlighted and show a
**NEW** badge on the Highlights tab.

If you'd rather not click through, you can also reply to the email with:

```
LIKE 1,3
```

to add listings #1 and #3 from that day's alert. `process-email-replies.yml` checks for these
replies every 30 minutes (via IMAP) and commits the additions.

## Updating the alert filter

Open `docs/alert.html` (linked from the tracker), paste the same GitHub token, and edit/save the
form — it commits straight to `criteria.json` on `main`.

## Running things manually

Actions tab → "MLS Daily Listing Alert" or "Process Alert Email Replies" → Run workflow.

## Future: Airtable

If `houses.json`-in-Git ever becomes limiting (e.g. more collaborators, richer views), the data
model here is simple enough to swap for an Airtable base with minimal changes — `houses.json`'s
shape maps directly to an Airtable table, and `process_replies.py` could write to Airtable's API
instead of committing JSON. Not needed yet.
