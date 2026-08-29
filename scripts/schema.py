#!/usr/bin/env python3
"""The shape of the data, in one place.

Table and field names live here rather than in the database client, so the
worker, the digest, the SQL and the app all agree without importing a
particular backend. supabase/schema.sql is the physical mirror of this file
and scripts/check_schema_sync.py fails the build if the two drift.
"""

# Table names, matched by name so the base can be created by hand or by API.
TABLE_CRITERIA = "Search Criteria"
TABLE_HOUSES = "Houses"
TABLE_RECIPIENTS = "Recipients"

# The schema, kept here so setup docs and code can't drift apart.
SCHEMA = {
    TABLE_CRITERIA: [
        ("Name", "singleLineText"),          # e.g. "Atlanta flips"
        ("Active", "checkbox"),              # only Active rows are searched
        ("Market", "singleLineText"),        # Atlanta / Los Angeles / ...
        ("City", "singleLineText"),
        ("State", "singleLineText"),
        ("Min Price", "number"),
        ("Max Price", "number"),
        ("Min Beds", "number"),
        ("Min Baths", "number"),
        ("Min Sqft", "number"),
        ("Zip Codes", "singleLineText"),       # comma-separated; how "within 10 mi" is expressed
        ("Property Types", "singleLineText"),  # comma-separated
        ("Keywords", "singleLineText"),        # comma-separated, any-match
        ("Must Haves", "singleLineText"),      # comma-separated, ALL required; "/" = alternatives
        ("Strategy", "singleSelect"),          # Flip / BRRRR / Either
        # What kind of building this search is for. Single Family is the
        # default and the rule for every house search; Multifamily turns that
        # gate around rather than off, so a 20-unit complex is admitted and a
        # detached house is not. Blank reads as Single Family, which keeps
        # every existing row behaving exactly as it did.
        ("Property Class", "singleSelect"),
        ("Min Units", "number"),               # multifamily only
        ("Max Price Per Sqft", "number"),      # skip anything above; missing sqft passes
        ("Max All In", "number"),              # price + rehab cap
        ("Target Total Sqft", "number"),       # post-reno goal, informational
        ("Min Baths After Reno", "number"),    # post-reno goal, informational
        ("Target Flip Profit", "number"),      # qualification thresholds
        ("Target Cash on Cash", "number"),     # percent, e.g. 8
        ("Target One Percent", "number"),      # percent, e.g. 1
        ("Rehab Cost Per Sqft", "number"),     # used to estimate rehab
        ("Notes", "multilineText"),
    ],
    TABLE_HOUSES: [
        ("Address", "singleLineText"),
        ("Market", "singleLineText"),
        ("Status", "singleSelect"),   # New / Interested / Touring / Offer / Rejected ...
        ("Price", "number"),
        ("Beds", "number"),
        ("Baths", "number"),
        ("Sqft", "number"),
        ("Lot Sqft", "number"),
        ("Price Per Sqft", "number"),
        ("Value Signals", "singleLineText"),  # Basement, ADU potential, FSBO, ...
        ("Rehab Cost", "number"),
        ("ARV", "number"),
        ("Rent Estimate", "number"),
        ("Flip Profit", "number"),
        ("Cash on Cash", "number"),
        ("One Percent", "number"),
        ("Flip Verdict", "singleSelect"),
        ("BRRRR Verdict", "singleSelect"),
        ("Best Strategy", "singleLineText"),
        ("Qualified", "checkbox"),    # cleared every target on its criteria row
        ("Listing URL", "url"),
        ("Photo URL", "url"),
        ("Property Type", "singleLineText"),  # as the feed reported it
        ("Units", "number"),                  # multifamily: unit count
        # Which criteria row found this house. Lets a digest be scoped to one
        # search, which is how the multifamily email stays separate from the
        # house email without a second table.
        ("Found By", "singleLineText"),
        # The observable shadow of "dated, poorly marketed, motivated seller",
        # which no feed states outright but all three of these imply.
        ("Year Built", "number"),
        ("Days on Market", "number"),
        ("Price Cut", "number"),              # percent off the first asking price
        # What the house cost the last time we looked, and when that changed.
        # This is the whole reason houses are stored rather than re-fetched:
        # a listing we already know about is only news again if its price
        # moved, and nothing in the feed tells us that -- only a comparison
        # against what we recorded last run can.
        ("Previous Price", "number"),
        ("Price Change Date", "date"),
        # Whether the listing is still on the market. The feed is queried for
        # Active listings only, so a house we already track that stops coming
        # back from a complete search has gone under contract, sold, or been
        # withdrawn -- and a house you can no longer buy should not be sitting
        # in a list of houses to buy. Kept as a flag rather than a delete, so
        # the history survives and a relisting can flip back to Active.
        ("Listing Status", "singleSelect"),
        ("Last Seen", "date"),
        ("Source", "singleLineText"),
        ("Notes", "multilineText"),
        ("Date Added", "date"),
    ],
    # Who gets the digest. This is a table rather than an EMAIL_TO
    # repo secret because the recipient list is the one piece of config that
    # genuinely changes -- adding a partner, an agent, a lender for one deal --
    # and editing a GitHub secret from a phone to do it is the wrong shape.
    # Credentials (SMTP_USER/SMTP_PASS) stay secrets; addresses are not secrets.
    TABLE_RECIPIENTS: [
        ("Email", "singleLineText"),
        ("Name", "singleLineText"),
        ("Active", "checkbox"),
        ("Notes", "multilineText"),
    ],
}


# The allowed values for the constrained columns. These have to match the
# strings deals.py and app.js actually produce, and supabase/schema.sql
# repeats them as check constraints.
SELECT_OPTIONS = {
    "Strategy": ["Flip", "BRRRR", "Either"],
    "Property Class": ["Single Family", "Multifamily", "Condo", "Any"],
    "Listing Status": ["Active", "Off Market"],
    "Status": ["New", "Interested", "Touring", "Toured", "Offer",
               "Under Contract", "Purchased", "Rejected"],
    "Flip Verdict": ["STRONG", "GOOD", "MARGINAL", "PASS", "NO DATA"],
    "BRRRR Verdict": ["STRONG", "GOOD", "MARGINAL", "PASS", "NO DATA"],
}

# Decimal places per number field. Dollars are whole; ratios and bath counts
# are not, and rounding them to integers would quietly destroy the
# distinction between a 7.9% and an 8.4% cash-on-cash.
NUMBER_PRECISION = {
    "Baths": 1, "Min Baths": 1, "Min Baths After Reno": 1,
    "Cash on Cash": 1, "One Percent": 2, "Price Cut": 1,
    "Target Cash on Cash": 1, "Target One Percent": 2,
}



# The allowed values for the constrained columns. These have to match the
# strings deals.py and app.js actually produce, and supabase/schema.sql
# repeats them as check constraints.
SELECT_OPTIONS = {
    "Strategy": ["Flip", "BRRRR", "Either"],
    "Property Class": ["Single Family", "Multifamily", "Condo", "Any"],
    "Listing Status": ["Active", "Off Market"],
    "Status": ["New", "Interested", "Touring", "Toured", "Offer",
               "Under Contract", "Purchased", "Rejected"],
    "Flip Verdict": ["STRONG", "GOOD", "MARGINAL", "PASS", "NO DATA"],
    "BRRRR Verdict": ["STRONG", "GOOD", "MARGINAL", "PASS", "NO DATA"],
}

# Decimal places per number field. Dollars are whole; ratios and bath counts
# are not, and rounding them to integers would quietly destroy the
# distinction between a 7.9% and an 8.4% cash-on-cash.
NUMBER_PRECISION = {
    "Baths": 1, "Min Baths": 1, "Min Baths After Reno": 1,
    "Cash on Cash": 1, "One Percent": 2, "Price Cut": 1,
    "Target Cash on Cash": 1, "Target One Percent": 2,
}


def parse_list_field(value):
    """Comma-separated text -> list of trimmed strings."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]
