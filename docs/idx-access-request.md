# Requesting an IDX / RESO feed

The single change that would most improve this project. Everything else is a
workaround for not having it.

## Why

The search currently runs on RentCast, which resells residential sale data.
It is fine for detached houses and it is the reason the flip and BRRRR
searches work at all. It has two limits that no amount of tuning fixes:

- **No listing remarks.** "Fixer", "as-is", "estate sale", "bring your
  vision", "unfinished basement" — the language that identifies a motivated
  seller lives in the agent's description, and this feed does not carry it.
  The search infers motivation from year built, days on market and price
  cuts instead, which works but is a shadow of the real signal.
- **No commercial listings.** Apartment buildings of 20+ units are commercial
  real estate. They trade on LoopNet, Crexi and CoStar, and a residential
  sale API will never return them however the search is worded.

An IDX feed carries both, direct from the MLS, and covers everything a member
agent can see.

## What to ask for

Georgia has two MLSs covering metro Atlanta, and Cobb County listings appear
in both:

- **FMLS** (First Multiple Listing Service) — fmls.com
- **GAMLS** (Georgia MLS) — gamls.com

Ask either for a **RESO Web API (OData) IDX feed**. Specifically:

- RESO Web API access, not RETS — RETS is the older standard and is being
  retired
- The `Property` resource, with `Media` if photos are included
- Read-only is fine; nothing here writes back

Both normally require a **sponsoring broker** — an MLS member who vouches for
the feed and is accountable for how the data is used. If you or Ryan work
with an agent already, that is the person to ask first. It is a routine
request; agents sponsor IDX feeds for websites all the time.

Expect a data licence agreement, a one-off setup fee at some MLSs, and
sometimes a small monthly charge. Ask for the fee schedule up front.

## Draft message

> Hi [name],
>
> I'm putting together a private tool to track investment properties in Cobb
> County — flips and small multifamily, for my own buying rather than a
> public-facing site.
>
> Would you be willing to sponsor an IDX data feed for it? I'm looking for
> RESO Web API access to the Property resource, read-only. The data stays in
> a private app used by two people and isn't republished anywhere.
>
> Happy to sign whatever data licence agreement is required and to cover the
> setup and monthly fees. Could you let me know what the process looks like
> and roughly what it costs?
>
> Thanks,
> [name]

## What happens once you have it

Nothing needs building. The adapter already exists — `fetch_reso()` in
`scripts/search_worker.py` speaks RESO OData today and is exercised by the
same code path RentCast uses. Two repository secrets switch the project over:

| Secret | Value |
| --- | --- |
| `LISTINGS_API_URL` | the OData endpoint they give you |
| `LISTINGS_API_KEY` | the bearer token |

and one variable, `LISTINGS_API_TYPE`, set to `reso`.

The searches, the scoring, the emails and the app all carry on unchanged —
they would just be reading better data. The keyword matching in the criteria
rows ("fixer, as-is, TLC, needs work, estate sale") starts working the day
the feed carries remarks, because it was written for exactly that and has had
nothing to read.
