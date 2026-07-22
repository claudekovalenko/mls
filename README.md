# MLS Listing Alerts

Daily email of real-estate listings matching your criteria, plus a website to edit those
criteria without touching code.

## How it works

- `criteria.json` — the current search criteria (location, price range, beds/baths, property
  types, recipient email).
- `scripts/send_alert.py` — reads `criteria.json`, fetches listings from a data source, filters
  them, and emails the matches.
- `.github/workflows/daily-alert.yml` — runs `send_alert.py` automatically every day at 14:00 UTC
  (adjust the cron schedule as needed), and can also be triggered manually from the Actions tab.
- `docs/index.html` — the criteria dashboard (see below), published via GitHub Pages.

## One-time setup

1. **Enable GitHub Pages**: Settings → Pages → Source: Deploy from a branch → Branch `main`,
   folder `/docs`. The dashboard will be live at `https://claudekovalenko.github.io/mls/`.
2. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `GMAIL_USER` / `GMAIL_APP_PASSWORD` — a Gmail address + an
     [app password](https://myaccount.google.com/apppasswords) to send from.
   - `LISTINGS_API_URL` / `LISTINGS_API_KEY` — your listings data source. The script expects a
     JSON array of objects like `{price, beds, baths, address, url, propertyType}`. Point this
     at whatever feed you're using (an MLS/IDX API, a broker feed, etc.) — without it, the script
     runs but skips fetching and sends "no matches" each day.
3. Without secrets configured, the workflow still runs and prints the would-be email to the
   Action log instead of sending it, so you can verify it end-to-end before wiring up real
   credentials.

## Updating criteria

Open the dashboard (`docs/index.html`), paste a GitHub token once (instructions are on the page),
and edit/save the form — it commits straight to `criteria.json` on `main`. You can also just edit
`criteria.json` directly in GitHub if you prefer.

## Running manually

Actions tab → "MLS Daily Listing Alert" → Run workflow.
