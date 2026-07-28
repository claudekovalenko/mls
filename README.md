# MLS House Tracker

A site for tracking Atlanta-area houses we're considering buying — status, ratings, notes — plus
a daily email of new listings matching our filter criteria, with the ability to "like" listings
straight from a reply email.

This currently covers the **purchasing/shopping phase**. Once we own a house, this can expand
into ownership/management (maintenance, docs, expenses, etc.) — but `houses.json` and the
tracker UI are intentionally simple for now.

## How it works

- `houses.json` — every house we're tracking: address, price, beds/baths, listing URL, status
  (Interested / Touring Scheduled / Toured / Offer Made / Under Contract / Purchased / Rejected),
  star rating, notes, `liked` flag, `source` (`manual` or `email`), date added.
- `docs/index.html` — the House Tracker (see below), published via GitHub Pages. Two tabs:
  **All Houses** (everything) and **Highlights** (liked houses, plus new ones added via email
  reply). Add, edit, heart, filter, and sort houses from the browser.
- `criteria.json` — the daily email alert's search filter (location, price range, beds/baths,
  property types, recipient email).
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
3. Without secrets configured, the alert workflow still runs and prints the would-be email to the
   Action log instead of sending it, and the reply-processing workflow just skips checking.

## Using the House Tracker

Open the site (`docs/index.html`), paste a GitHub token once (instructions are on the page), and:

- **+ Add House** — save a new house with address, price, beds/baths, listing URL, status,
  rating, and notes.
- Click the heart (♡/♥) on any row to highlight it — highlighted houses show up on the
  **Highlights** tab.
- Click **Edit** on any row to update its status/rating/notes as you go through the process, or
  delete it.
- Filter by status and sort by date added / price / rating / address.

All changes commit straight to `houses.json` on `main`. You can also edit that file directly in
GitHub if you prefer.

## Liking listings from the daily alert email

Each alert email numbers its listings. Reply to the email with:

```
LIKE 1,3
```

to add listings #1 and #3 from that day's alert straight into the House Tracker (marked
highlighted, status "Interested"). `process-email-replies.yml` checks for these replies every 30
minutes and commits the additions — no need to touch the tracker UI. Houses added this way show a
**NEW** badge and appear on the **Highlights** tab automatically.

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
