-- House Finder on Supabase (Postgres).
--
-- Mirrors scripts/airtable.py's SCHEMA one column at a time, so the two can
-- run side by side during the switch and a row means the same thing in both.
-- Run once in the Supabase SQL editor.
--
-- Why the app can hold the key in a browser here when it could not with
-- Airtable: an Airtable token is all-or-nothing -- read it and you can read
-- and write every table in the base. Supabase splits that in two. The anon
-- key is public by design and can do only what the policies below allow;
-- the service_role key, which bypasses them, never leaves GitHub secrets.

-- ---------------------------------------------------------------- criteria
create table if not exists search_criteria (
  id                    uuid primary key default gen_random_uuid(),
  name                  text not null,
  active                boolean not null default false,
  market                text,
  city                  text,
  state                 text,
  min_price             numeric,
  max_price             numeric,
  min_beds              numeric,
  min_baths             numeric,
  min_sqft              numeric,
  zip_codes             text,          -- comma-separated; how "within 10 mi" is expressed
  property_types        text,          -- comma-separated
  keywords              text,
  must_haves            text,
  strategy              text check (strategy in ('Flip', 'BRRRR', 'Either')),
  property_class        text check (property_class in ('Single Family', 'Multifamily', 'Any')),
  min_units             numeric,
  max_price_per_sqft    numeric,
  max_all_in            numeric,
  target_total_sqft     numeric,
  min_baths_after_reno  numeric,
  target_flip_profit    numeric,
  target_cash_on_cash   numeric,
  target_one_percent    numeric,
  rehab_cost_per_sqft   numeric,
  notes                 text,
  created_at            timestamptz not null default now()
);

-- One search per name. Airtable allowed duplicates, and a duplicated row is
-- not a harmless mistake -- it costs a listing API call on every single run.
create unique index if not exists search_criteria_name_key
  on search_criteria (lower(name));

-- The worker only ever reads active rows.
create index if not exists search_criteria_active_idx
  on search_criteria (active) where active;

-- ------------------------------------------------------------------ houses
create table if not exists houses (
  id                 uuid primary key default gen_random_uuid(),
  address            text not null,
  market             text,
  status             text not null default 'New'
                     check (status in ('New', 'Interested', 'Touring', 'Toured',
                                       'Offer', 'Under Contract', 'Purchased',
                                       'Rejected')),
  price              numeric,
  beds               numeric,
  baths              numeric,
  sqft               numeric,
  lot_sqft           numeric,
  price_per_sqft     numeric,
  value_signals      text,
  rehab_cost         numeric,
  arv                numeric,
  rent_estimate      numeric,
  flip_profit        numeric,
  cash_on_cash       numeric,
  one_percent        numeric,
  flip_verdict       text check (flip_verdict in ('STRONG','GOOD','MARGINAL','PASS','NO DATA')),
  brrrr_verdict      text check (brrrr_verdict in ('STRONG','GOOD','MARGINAL','PASS','NO DATA')),
  best_strategy      text,
  qualified          boolean not null default false,
  listing_url        text,
  photo_url          text,
  property_type      text,
  units              numeric,
  found_by           text,          -- the criteria row that found it
  year_built         numeric,
  days_on_market     numeric,
  price_cut          numeric,
  source             text,
  notes              text,
  date_added         date not null default current_date,
  updated_at         timestamptz not null default now()
);

-- The worker upserts by address, so this is what makes a re-run update a
-- house instead of adding a duplicate. Airtable had no such guarantee.
create unique index if not exists houses_address_key on houses (lower(address));

create index if not exists houses_date_added_idx on houses (date_added desc);
create index if not exists houses_found_by_idx   on houses (found_by);

-- Keep updated_at honest without every caller remembering to set it.
create or replace function touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists houses_touch_updated_at on houses;
create trigger houses_touch_updated_at
  before update on houses
  for each row execute function touch_updated_at();

-- -------------------------------------------------------------- recipients
create table if not exists recipients (
  id      uuid primary key default gen_random_uuid(),
  email   text not null,
  name    text,
  active  boolean not null default true,
  notes   text
);

create unique index if not exists recipients_email_key on recipients (lower(email));

-- --------------------------------------------------------------------- RLS
-- Row Level Security is what lets the PWA ship a key at all. Without these
-- policies the anon key can do nothing; with them it can do exactly the
-- app's job and no more.
alter table search_criteria enable row level security;
alter table houses          enable row level security;
alter table recipients      enable row level security;

-- Criteria and houses: the app reads and edits both, which is its whole
-- purpose -- marking a house Interested, adjusting a search from a phone.
drop policy if exists anon_rw_criteria on search_criteria;
create policy anon_rw_criteria on search_criteria
  for all to anon using (true) with check (true);

drop policy if exists anon_rw_houses on houses;
create policy anon_rw_houses on houses
  for all to anon using (true) with check (true);

-- Recipients: read-only to the browser. These are people's email addresses,
-- and nothing in the app needs to change them -- only the digest reads the
-- list, and it runs with the service_role key. A public key that could
-- rewrite the recipient list is a public key that could redirect the mail.
drop policy if exists anon_read_recipients on recipients;
create policy anon_read_recipients on recipients
  for select to anon using (true);
