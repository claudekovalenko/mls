# MLS House Tracker

A site for tracking Atlanta-area houses we're considering buying — status, ratings, notes — plus
a daily email of new listings matching our filter criteria.

This currently covers the **purchasing/shopping phase**. Once we own a house, this can expand
into ownership/management (maintenance, docs, expenses, etc.) — but `houses.json` and the
tracker UI are intentionally simple for now.

## How it works

- `houses.json` — every house we're tracking: address, price, beds/baths, listing URL, status
  (Interested / Touring Scheduled / Toured / Offer Made / Under Contract / Purchased / Rejected),
  star rating, notes, date added.
- `docs/index.html` — the House Tracker (see below), published via GitHub Pages. Add, edit,
  filter, and sort houses from the browser.
- `criteria.json` — the daily email alert's search filter (location, price range, beds/baths,
  property types, recipient email).
- `scripts/send_alert.py` — reads `criteria.json`, fetches listings from a data source, filters
  them, and emails the matches.
- `.github/workflows/daily-alert.yml` — runs `send_alert.py` automatically every day at 14:00 UTC
  (adjust the cron schedule as needed), and can also be triggered manually from the Actions tab.
- `docs/alert.html` — the daily alert filter settings page, linked from the tracker.

## One-time setup

1. **Enable GitHub Pages**: Settings → Pages → Source: Deploy from a branch → Branch `main`,
   folder `/docs`. The tracker will be live at `https://claudekovalenko.github.io/mls/`.
2. **Add repo secrets** (Settings → Secrets and variables → Actions), only needed for the daily
   email alert:
   - `GMAIL_USER` / `GMAIL_APP_PASSWORD` — a Gmail address + an
     [app password](https://myaccount.google.com/apppasswords) to send from.
   - `LISTINGS_API_URL` / `LISTINGS_API_KEY` — your listings data source. The script expects a
     JSON array of objects like `{price, beds, baths, address, url, propertyType}`. Point this
     at whatever feed you're using (an MLS/IDX API, a broker feed, etc.) — without it, the script
     runs but skips fetching and sends "no matches" each day.
3. Without secrets configured, the workflow still runs and prints the would-be email to the
   Action log instead of sending it, so you can verify it end-to-end before wiring up real
   credentials.

## Using the House Tracker

Open the site (`docs/index.html`), paste a GitHub token once (instructions are on the page), and:

- **+ Add House** — save a new house with address, price, beds/baths, listing URL, status,
  rating, and notes.
- Click **Edit** on any row to update its status/rating/notes as you go through the process, or
  delete it.
- Filter by status and sort by date added / price / rating / address.

All changes commit straight to `houses.json` on `main`. You can also edit that file directly in
GitHub if you prefer.

## Updating the alert filter

Open `docs/alert.html` (linked from the tracker), paste the same GitHub token, and edit/save the
form — it commits straight to `criteria.json` on `main`.

## Running the alert manually

Actions tab → "MLS Daily Listing Alert" → Run workflow.
