/* House Finder — PWA over Supabase (Postgres).
 *
 * Supabase is the database of record. This app talks to PostgREST directly
 * with the project's publishable anon key, which ships in this file;
 * row-level security in supabase/schema.sql decides what it can touch.
 *
 * Deal math here mirrors scripts/deals.py. If you change one, change both --
 * the worker scores listings server-side, this scores what you type live.
 */

// Keep in lockstep with CACHE in sw.js -- check_version_sync guards it.
const APP_VERSION = "v46";
const TABLE_CRITERIA = "Search Criteria";
const TABLE_HOUSES = "Houses";

// ---- deal math (mirror of scripts/deals.py) ----
const FLIP_SELLING_COST_PCT = 0.08;
const FLIP_MAX_OFFER_PCT = 0.70;
const BRRRR_REFI_LTV = 0.75;
const BRRRR_INTEREST_RATE = 0.07;
const BRRRR_LOAN_TERM_YEARS = 30;
const BRRRR_EXPENSE_RATIO = 0.50;
const FLIP_STRONG_PROFIT = 50000, FLIP_OK_PROFIT = 25000;
const FLIP_STRONG_ROI = 0.15, FLIP_OK_ROI = 0.10;
const BRRRR_STRONG_COC = 0.12, BRRRR_OK_COC = 0.08;
const TIER_RANK = { STRONG: 3, GOOD: 2, MARGINAL: 1, PASS: 0, "NO DATA": -1 };

function monthlyPayment(loan, rate, years) {
  if (!loan || loan <= 0) return 0;
  const r = rate / 12, n = years * 12;
  if (r === 0) return loan / n;
  return loan * (r * Math.pow(1 + r, n)) / (Math.pow(1 + r, n) - 1);
}

function computeMetrics(price, rehab, arv, rent) {
  // An ARV equal to the list price is a placeholder, not an estimate, and
  // left alone it makes flip profit negative by construction. Mirrors
  // deals.compute_metrics.
  if (arv != null && price != null && arv === price) arv = null;
  const m = {
    maxOffer70: null, flipProfit: null, flipRoi: null,
    cashLeftInDeal: null, brrrrCashflow: null, cashOnCash: null, onePercentRatio: null,
  };
  if (arv != null) m.maxOffer70 = arv * FLIP_MAX_OFFER_PCT - (rehab || 0);
  if (price != null && rehab != null && arv != null) {
    const totalIn = price + rehab;
    m.flipProfit = arv - totalIn - arv * FLIP_SELLING_COST_PCT;
    if (totalIn > 0) m.flipRoi = m.flipProfit / totalIn;
    const refi = arv * BRRRR_REFI_LTV;
    m.cashLeftInDeal = totalIn - refi;
    if (rent != null) {
      m.brrrrCashflow = rent * BRRRR_EXPENSE_RATIO - monthlyPayment(refi, BRRRR_INTEREST_RATE, BRRRR_LOAN_TERM_YEARS);
      m.cashOnCash = m.cashLeftInDeal > 0
        ? (m.brrrrCashflow * 12) / m.cashLeftInDeal
        : (m.brrrrCashflow > 0 ? 99 : null);
    }
  }
  if (rent != null && price) m.onePercentRatio = rent / price;
  return m;
}

// Name only what is actually missing. "Needs price, rehab cost, and ARV"
// under a card that plainly shows a price reads as the app not looking.
function missingInputs(price, rehab, arv) {
  const gaps = [];
  if (price == null) gaps.push("a price");
  if (rehab == null) gaps.push("a rehab cost");
  if (arv == null) gaps.push("a resale value (ARV)");
  return "Needs " + (gaps.join(" and ") || "nothing") + " — type it in below and this recalculates";
}

function qualify(price, rehab, arv, rent) {
  const m = computeMetrics(price, rehab, arv, rent);
  let flipTier, flipReasons = [];
  if (m.flipProfit == null) {
    flipTier = "NO DATA"; flipReasons = [missingInputs(price, rehab, arv)];
  } else {
    const meets70 = price != null && m.maxOffer70 != null && price <= m.maxOffer70;
    flipReasons.push(meets70 ? "Clears the 70% rule" : "Over the 70%-rule max offer");
    flipReasons.push(`${money(m.flipProfit)} projected profit`);
    if (m.flipRoi != null) flipReasons.push(`${pct(m.flipRoi)} return on cash in`);
    if (m.flipProfit <= 0) flipTier = "PASS";
    else if (meets70 && m.flipProfit >= FLIP_STRONG_PROFIT && (m.flipRoi || 0) >= FLIP_STRONG_ROI) flipTier = "STRONG";
    else if (m.flipProfit >= FLIP_OK_PROFIT && (m.flipRoi || 0) >= FLIP_OK_ROI) flipTier = "GOOD";
    else flipTier = "MARGINAL";
  }

  let brrrrTier, brrrrReasons = [];
  if (m.cashLeftInDeal == null) { brrrrTier = "NO DATA"; brrrrReasons = [missingInputs(price, rehab, arv)]; }
  else if (m.brrrrCashflow == null) { brrrrTier = "NO DATA"; brrrrReasons = ["Needs a rent estimate"]; }
  else {
    brrrrReasons.push(m.cashLeftInDeal <= 0 ? "All capital recycled on refi" : `${money(m.cashLeftInDeal)} left in the deal`);
    brrrrReasons.push(`${money(m.brrrrCashflow)}/mo cashflow`);
    if (m.brrrrCashflow <= 0) brrrrTier = "PASS";
    else if (m.cashLeftInDeal <= 0 || (m.cashOnCash || 0) >= BRRRR_STRONG_COC) brrrrTier = "STRONG";
    else if ((m.cashOnCash || 0) >= BRRRR_OK_COC) brrrrTier = "GOOD";
    else brrrrTier = "MARGINAL";
  }

  const fr = TIER_RANK[flipTier], br = TIER_RANK[brrrrTier];
  return {
    metrics: m, flipTier, flipReasons, brrrrTier, brrrrReasons,
    bestStrategy: Math.max(fr, br) > 0 ? (fr >= br ? "Flip" : "BRRRR") : null,
    bestRank: Math.max(fr, br),
  };
}

// ---- formatting ----
const money = n => n == null || Number.isNaN(n)
  ? "—" : (n < 0 ? "-" : "") + "$" + Math.abs(Math.round(n)).toLocaleString();
const pct = n => n == null || Number.isNaN(n) ? "—" : (n >= 99 ? "∞" : (n * 100).toFixed(1) + "%");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- storage ----
// The project this app belongs to, shipped in the code rather than typed in
// on every device.
//
// This is safe, and it is the design Supabase intends. The anon key is a
// *publishable* key: it is meant to sit in client JavaScript where anyone
// can read it, and it grants nothing on its own. What it can touch is
// decided server-side by the row-level security policies in
// supabase/schema.sql -- read and write the two tables this app edits, and
// read-only on recipients. The key that bypasses those policies is the
// service_role key, and that one lives only in GitHub secrets and never
// reaches a browser.
//
// The alternative -- a setup screen on every phone, every laptop, every
// reinstall, and after every cache clear -- asks somebody to paste
// credentials to use their own app. That is friction with no security
// benefit, since the key it protects is public by design.
const BUILT_IN = {
  url: "https://dmiysgmhwpkrunmswtrn.supabase.co",
  key: "sb_publishable_9NfxdWLrFdExD-_6HwPs8A_uUtlTH3C",
};

// localStorage still wins when set, so a second project (a test copy, a
// fork, someone else's data) is possible without editing code.
const store = {
  get sbUrl() {
    return (localStorage.getItem("supabase_url") || BUILT_IN.url).replace(/\/+$/, "");
  },
  set sbUrl(v) {
    if (v) localStorage.setItem("supabase_url", v);
    else localStorage.removeItem("supabase_url");
  },
  get sbKey() { return localStorage.getItem("supabase_key") || BUILT_IN.key; },
  set sbKey(v) {
    if (v) localStorage.setItem("supabase_key", v);
    else localStorage.removeItem("supabase_key");
  },
  get isCustom() {
    return !!(localStorage.getItem("supabase_url") || localStorage.getItem("supabase_key"));
  },
};

// Postgres columns are snake_case; the rest of this app speaks the field
// names the schema declares. Listed rather than derived, because neither
// direction is mechanical -- "Days on Market" title-cases to "Days On
// Market", and ARV and BRRRR survive no casing rule at all. Mirrors
// scripts/schema.py SCHEMA; a name missing here is a value silently
// dropped, which is exactly the bug this shape is meant to make obvious.
const FIELD_NAMES = [
  "Name", "Active", "Market", "City", "State", "Min Price", "Max Price",
  "Min Beds", "Min Baths", "Min Sqft", "Zip Codes", "Property Types",
  "Keywords", "Must Haves", "Strategy", "Property Class", "Min Units",
  "Max Price Per Sqft", "Max All In", "Target Total Sqft",
  "Min Baths After Reno", "Target Flip Profit", "Target Cash on Cash",
  "Target One Percent", "Rehab Cost Per Sqft", "Notes",
  "Address", "Status", "Price", "Beds", "Baths", "Sqft", "Lot Sqft",
  "Price Per Sqft", "Value Signals", "Rehab Cost", "ARV", "Rent Estimate",
  "Flip Profit", "Cash on Cash", "One Percent", "Flip Verdict",
  "BRRRR Verdict", "Best Strategy", "Qualified", "Listing URL", "Photo URL",
  "Property Type", "Units", "Found By", "Year Built", "Days on Market",
  "Price Cut", "Previous Price", "Price Change Date", "Listing Status",
  "Latitude", "Longitude",
  "Last Seen", "Source", "Date Added",
  "Email",
];
const toColumn = name => name.trim().toLowerCase().replace(/[ -]/g, "_");
const COLUMN_TO_FIELD = Object.fromEntries(FIELD_NAMES.map(n => [toColumn(n), n]));
const SB_TABLE = { [TABLE_CRITERIA]: "search_criteria", [TABLE_HOUSES]: "houses" };

const rowToRecord = row => {
  const fields = {};
  for (const [col, val] of Object.entries(row)) {
    if (col === "id" || col === "updated_at" || val === null) continue;
    fields[COLUMN_TO_FIELD[col] || col] = val;
  }
  return { id: row.id, fields };
};
const fieldsToRow = fields =>
  Object.fromEntries(Object.entries(fields).map(([k, v]) => [toColumn(k), v]));

async function supabase(method, table, { body, query } = {}) {
  let url = `${store.sbUrl}/rest/v1/${SB_TABLE[table] || table}`;
  if (query) url += "?" + query;
  const res = await fetch(url, {
    method,
    headers: {
      apikey: store.sbKey,
      Authorization: `Bearer ${store.sbKey}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    if (res.status === 401 || res.status === 403) {
      throw new Error("Supabase rejected the key. Check it's the anon public key, "
        + "and that the schema's row-level security policies were applied.");
    }
    if (res.status === 404) {
      throw new Error(`No "${SB_TABLE[table] || table}" table in this project yet. `
        + "Run supabase/schema.sql in the SQL editor.");
    }
    throw new Error(detail.message || detail.hint || `Supabase ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

async function listAll(table) {
  const rows = await supabase("GET", table, { query: "select=*" });
  return rows.map(rowToRecord);
}

// One row in, one row out. Every write in this app is a single record, so
// this plus listAll is the whole database surface.
async function saveRecord(table, id, fields) {
  return id
    ? supabase("PATCH", table, { body: fieldsToRow(fields), query: `id=eq.${encodeURIComponent(id)}` })
    : supabase("POST", table, { body: fieldsToRow(fields) });
}

// ---- state ----
let criteria = [];
let houses = [];
// The three lanes. Declared here rather than beside laneOf() further down,
// because currentLane below reads them while this file is still executing --
// a `const` in the wrong order is a load-time crash, not a late failure.
// Two things are being hunted, and they are different businesses: a block of
// twenty-plus doors bought whole, and a single detached house to flip. There
// is no condo lane because there is no condo search -- every criteria row is
// Single Family or Multifamily, so a third tab would have been permanently
// empty and would have implied a search that does not exist.
const LANES = [
  { id: "multifamily", icon: "🏢", label: "Complex", plural: "complexes",
    hint: "multifamily complexes, 20 units and up" },
  { id: "house",       icon: "🏠", label: "Home", plural: "homes",
    hint: "detached single family" },
];
const DEFAULT_LANE = "house";
// Which lane the Matches screen is showing. Remembered across reloads,
// because whichever one you were working in is the one you want back.
let currentLane = localStorage.getItem("lane") || DEFAULT_LANE;
if (!LANES.some(l => l.id === currentLane)) currentLane = DEFAULT_LANE;
let screen = "matches";

const $ = id => document.getElementById(id);
function setStatus(msg, ok = true) {
  // Mirrored into both screens -- the setup card and the app shell each have
  // their own slot, and a save error raised while the app is up must be visible
  // there rather than in the hidden setup card.
  ["status", "app-status"].forEach(id => {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || "";
    el.className = msg ? (ok ? "ok" : "err") : "";
  });
}

// ---- rendering: matches ----
function houseVerdict(f) {
  return qualify(f.Price ?? null, f["Rehab Cost"] ?? null, f.ARV ?? null, f["Rent Estimate"] ?? null);
}

// Single source of truth for "what the Matches screen is showing", so the
// copy button can never disagree with what's on screen.
// How far under the area median the scorer measured, parsed from the signal
// it wrote ("38% under area $/sqft"). The one number the feed itself supports.
function discountPct(f) {
  const m = String(f["Value Signals"] || "").match(/(\d+)% under area/);
  return m ? Number(m[1]) : 0;
}

// Every way the list can be ordered. Each strategy in Search Criteria gets
// its own entry, sorted by that strategy's fit score -- so "sort by BRRRR A"
// literally reorders the list by how well each house fits that plan.
function sortOptions() {
  const opts = [["best", "Hottest first"]];
  criteria.filter(r => (r.fields || {}).Active).forEach(r => {
    const name = (r.fields || {}).Name || "Search";
    opts.push(["fit:" + name, "Fit: " + name.split("—")[0].trim()]);
  });
  opts.push(
    ["discount", "Most under area $/sqft"],
    ["dom", "Longest on market"],
    ["cut", "Biggest price cut"],
    ["price", "Price: low to high"],
    ["ppsf", "$/sqft: low to high"],
    ["lot", "Largest lot"],
    ["rent", "Rental: cash-on-cash"],
  );
  return opts;
}

// Is this house still buyable? The worker marks anything a complete search
// stopped seeing as Off Market -- under contract, sold, or withdrawn. Rows
// written before that field existed have no value, and absence of evidence
// is not evidence of absence, so they count as live.
const isLive = f => !["Off Market", "Under Contract"].includes(f["Listing Status"]);

// Your own decision about a house, as opposed to what the market did.
// Under Contract, Purchased and Rejected are settled -- there is nothing
// left to do about any of them. Interested and Touring are not: those are
// live, and a price drop on one is the most useful alert this app can send.
const DECIDED_STATUSES = ["Under Contract", "Purchased", "Rejected"];
const isDecided = f => DECIDED_STATUSES.includes(f.Status);

// The short list. Everything that matches criteria is worth having; this is
// the subset worth looking at today -- it qualifies on the numbers, or a
// human already flagged it, or its price just moved, or it lands in enough
// of the brief's categories to be more than a near-miss.
function isHighlighted(f, v) {
  if (isDecided(f)) return false;
  if (f.Qualified || v.bestRank >= 2) return true;
  if (f.Status && f.Status !== "New") return true;
  if (f["Previous Price"] && f.Price && f.Price < f["Previous Price"]) return true;
  return String(f["Value Signals"] || "").split(",").filter(s => s.trim()).length >= 3;
}

function visibleMatches() {
  const marketFilter = $("filter-market").value;
  const view = ($("filter-view") || {}).value || "highlights";
  const sortBy = ($("sort-by") || {}).value || "best";
  const catCount = f => String(f["Value Signals"] || "").split(",").filter(s => s.trim()).length;

  let rows = houses.map(r => ({ id: r.id, f: r.fields || {}, v: houseVerdict(r.fields || {}) }));
  rows = rows.filter(r => laneOf(r.f) === currentLane);
  if (marketFilter) rows = rows.filter(r => (r.f.Market || "") === marketFilter);
  // Off market is its own view rather than a hidden state: the houses are
  // still there when you want to look back at what a street actually sold
  // for, but they never sit in a list of things to go and buy.
  if (view === "off") rows = rows.filter(r => !isLive(r.f));
  else {
    rows = rows.filter(r => isLive(r.f));
    if (view === "highlights") rows = rows.filter(r =>
      isHighlighted(r.f, r.v) && inSearchArea(r.f) !== false);
  }

  const desc = get => (a, b) => (get(b.f) ?? -Infinity) - (get(a.f) ?? -Infinity);
  const asc = get => (a, b) => (get(a.f) ?? Infinity) - (get(b.f) ?? Infinity);

  if (sortBy.startsWith("fit:")) {
    const name = sortBy.slice(4);
    const score = f => {
      const fit = fitSummary(f).fits.find(x => x.name === name);
      return fit ? fit.score * 1000 + fit.met : -1;
    };
    rows.sort(desc(score));
  } else if (sortBy === "discount") rows.sort(desc(discountPct));
  else if (sortBy === "dom") rows.sort(desc(f => f["Days on Market"]));
  else if (sortBy === "cut") rows.sort(desc(f => f["Price Cut"]));
  else if (sortBy === "price") rows.sort(asc(f => f.Price));
  else if (sortBy === "ppsf") rows.sort(asc(f => f["Price Per Sqft"]));
  else if (sortBy === "lot") rows.sort(desc(f => f["Lot Sqft"]));
  else if (sortBy === "rent")
    rows.sort((a, b) => (b.v.metrics.cashOnCash ?? -Infinity) - (a.v.metrics.cashOnCash ?? -Infinity));
  else {
    // Hottest first: the same strength number shown on every card, so the
    // order of the list and the badge on the card cannot disagree.
    const t = triageMap();
    const heat = r => (t.get(r.id) || {}).strength ?? strengthOf(r.f, fitSummary(r.f).best);
    rows.sort((a, b) => heat(b) - heat(a) || (catCount(b.f) - catCount(a.f)));
  }
  return rows;
}

// The lane switcher: a sliding indicator behind three labels, so which lane
// you're in is legible without reading, and moving between them feels like
// one control rather than three buttons.
function renderLaneSwitch() {
  const el = $("lane-switch");
  if (!el) return;
  const counts = laneCounts();
  const index = LANES.findIndex(l => l.id === currentLane);
  el.style.setProperty("--lane-index", index);
  el.style.setProperty("--lane-count", LANES.length);
  el.innerHTML =
    `<div class="lane-thumb" aria-hidden="true"></div>` +
    LANES.map(l => `
      <button type="button" class="lane-btn${l.id === currentLane ? " on" : ""}"
              data-lane="${esc(l.id)}" role="tab"
              aria-selected="${l.id === currentLane}"
              aria-label="${esc(l.label)} — ${counts[l.id]} ${esc(l.hint)}"
              title="${esc(l.label)} — ${counts[l.id]}">
        <span class="lane-icon" aria-hidden="true">${l.icon}</span>
      </button>`).join("");
  el.querySelectorAll("[data-lane]").forEach(b =>
    b.addEventListener("click", () => goToLane(b.dataset.lane)));
}

// Switching lanes, from a tap or a swipe.
//
// The scroll reset is not a flourish. Lanes hold wildly different numbers of
// houses -- 45 against 0 -- so moving from a long one to a short one while
// scrolled down collapses the page under you and the browser drops you
// wherever the new, shorter document ends. That reads as the whole screen
// lurching upward. Going back to the top makes the change deliberate
// instead, and the top is where a fresh list starts anyway.
function goToLane(id) {
  if (!LANES.some(l => l.id === id)) return;
  const changed = id !== currentLane;
  currentLane = id;
  localStorage.setItem("lane", currentLane);
  // Picking a match type means "show me those", so it also brings the
  // list back if you were off on the Searches screen.
  if (screen !== "matches") setScreen("matches");
  renderLaneSwitch();
  renderMatches();
  // Instant, not smooth. Animating a long scroll means half a second of the
  // whole list sliding past, which reads as the screen lurching -- the thing
  // this was meant to stop.
  const sc = $("scroller");
  if (changed && sc && sc.scrollTop) sc.scrollTop = 0;
}

function shiftLane(step) {
  const i = LANES.findIndex(l => l.id === currentLane);
  const next = LANES[i + step];
  if (next) goToLane(next.id);
}

// Drag left and right across the list to move between lanes.
//
// The list follows the finger rather than waiting for release, because a
// switch that only responds after you let go gives you nothing to judge the
// gesture by -- you cannot tell whether it took until it is over. Following
// the finger makes the state of the gesture visible the whole way through,
// and a drag that will not commit springs back so the refusal is legible too.
const SWIPE_COMMIT = 0.28;   // fraction of the width that commits the change
const SWIPE_MIN_X = 12;      // px before we decide this is a drag, not a tap
const SWIPE_SLOPE = 1.2;     // horizontal must beat vertical by this much

function enableLaneSwipe(el) {
  const pane = () => $("matches-list");
  let x0 = null, y0 = null, dragging = false, locked = null, width = 1;

  const setShift = (px, animate) => {
    const p = pane();
    if (!p) return;
    p.style.transition = animate
      ? "transform .28s cubic-bezier(0.32, 0.72, 0, 1), opacity .28s ease"
      : "none";
    p.style.transform = px ? `translate3d(${px}px, 0, 0)` : "";
    // Fading as it goes stops the drag reading as the list itself sliding
    // off somewhere it could be scrolled back from.
    p.style.opacity = px ? String(Math.max(0.35, 1 - Math.abs(px) / (width * 0.9))) : "";
  };

  const neighbour = step => LANES[LANES.findIndex(l => l.id === currentLane) + step];

  el.addEventListener("touchstart", e => {
    if (e.touches.length !== 1) { x0 = null; return; }
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
    dragging = false; locked = null;
    width = el.clientWidth || 1;
  }, { passive: true });

  el.addEventListener("touchmove", e => {
    if (x0 === null) return;
    const dx = e.touches[0].clientX - x0;
    const dy = e.touches[0].clientY - y0;
    if (locked === null) {
      if (Math.abs(dx) < SWIPE_MIN_X && Math.abs(dy) < SWIPE_MIN_X) return;
      // Decide once, at the start, whether this is a scroll or a lane drag.
      // Re-deciding mid-gesture is how a list ends up fighting the finger.
      locked = Math.abs(dx) > Math.abs(dy) * SWIPE_SLOPE ? "lane" : "scroll";
    }
    if (locked !== "lane") return;
    dragging = true;
    // Nothing to move to on that side: let it stretch a little and stop, so
    // the edge of the set is something you feel rather than guess at.
    const resist = neighbour(dx < 0 ? 1 : -1) ? 1 : 0.25;
    setShift(dx * resist, false);
  }, { passive: true });

  el.addEventListener("touchend", e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    x0 = null;
    if (!dragging) return;
    const step = dx < 0 ? 1 : -1;
    const committed = Math.abs(dx) > width * SWIPE_COMMIT && neighbour(step);
    setShift(0, true);
    if (committed) shiftLane(step);
  }, { passive: true });
}

// What the filters are doing right now, in a sentence. Collapsing the
// controls behind a button is only safe if their effect stays visible --
// otherwise an empty list looks like "no houses" when it means "you filtered
// them out three days ago and forgot".
function renderFilterSummary(shown) {
  const el = $("filter-summary");
  if (!el) return;
  const view = ($("filter-view") || {}).value || "highlights";
  const market = ($("filter-market") || {}).value || "";
  const sort = $("sort-by");
  const sortLabel = sort && sort.selectedIndex >= 0
    ? sort.options[sort.selectedIndex].text : "";
  const laneDef = LANES.find(l => l.id === currentLane) || {};
  const viewWord = { highlights: "Highlights", live: "Everything live",
                     off: "Off market" }[view];
  // Named rather than derived: "complex" does not pluralise by adding an s,
  // and "0 complexs" is the kind of thing that makes everything near it look
  // unfinished.
  const noun = shown === 1 ? (laneDef.label || "").toLowerCase()
                           : (laneDef.plural || "");
  const bits = [`${shown} ${noun}`, viewWord.toLowerCase()];
  if (market) bits.push(`in ${market}`);
  if (sortLabel) bits.push(`by ${sortLabel.toLowerCase()}`);
  const pulled = lastPulled();
  if (pulled) bits.push(`data pulled ${pulled}`);
  // The version on screen, so "am I on the new build?" is a glance at this
  // line rather than a guessing game about caches.
  bits.push(APP_VERSION);
  el.textContent = bits.join(" · ");
}

// When the listing feed was last actually read: the newest Last Seen the
// worker stamped (Date Added as a fallback for rows that predate the stamp).
// The email fires straight after every search run and this app reads the
// same live table, so this one date is the freshness of both.
function lastPulled() {
  let latest = "";
  for (const r of houses) {
    const f = r.fields || {};
    const d = String(f["Last Seen"] || f["Date Added"] || "").slice(0, 10);
    if (d > latest) latest = d;
  }
  if (!latest) return "";
  const days = Math.floor((Date.now() - new Date(latest + "T12:00:00")) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

// ---- map view ----
// Leaflet over OpenStreetMap tiles: free, keyless, and every pinned match on
// one screen -- the question a list can't answer is where they sit relative
// to each other. Pins are colored by the triage verdict and open the house.
let viewMode = "list";
try { viewMode = localStorage.getItem("hf-view-mode") || "list"; } catch {}
let map = null, markerLayer = null, areaLayer = null;

// Ryan's two hand-drawn hunting grounds (Aug 2026), traced from the notes on
// the criteria rows. Approximate on purpose -- these are shaded reminders of
// where the searches point, not survey lines.
//
// FLIP: Marietta and East Cobb -- north to Blackwells, east along 120 to the
// Chattahoochee, south to the I-75/I-285 Cumberland interchange, west back
// through Marietta.
// BRRRR (rentals): the same shape but tighter -- its southern line stops at
// Hwy 3/41 just below Marietta instead of running down to Cumberland.
const SEARCH_AREAS = [
  {
    name: "Flips", color: "#a2500c",
    ring: [[34.030, -84.560], [34.035, -84.495], [33.985, -84.435],
           [33.930, -84.425], [33.875, -84.450], [33.860, -84.510],
           [33.905, -84.580], [33.985, -84.585]],
  },
  {
    name: "Rentals (BRRRR)", color: "#0f766e",
    ring: [[34.030, -84.560], [34.035, -84.495], [33.985, -84.435],
           [33.930, -84.440], [33.905, -84.500], [33.905, -84.580],
           [33.985, -84.585]],
  },
];

const PIN_COLORS = { go: "#0f766e", offer: "#a2500c", watch: "#5c6b69", skip: "#b3261e" };

// Ray-cast point-in-polygon: does this point sit inside the ring?
function pointInRing(lat, lon, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [yi, xi] = ring[i], [yj, xj] = ring[j];
    if ((yi > lat) !== (yj > lat)
        && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// true = inside a drawn hunting ground, false = provably outside,
// null = no coordinates yet, so nobody can say.
function inSearchArea(f) {
  const lat = Number(f.Latitude), lon = Number(f.Longitude);
  if (!lat || !lon) return null;
  return SEARCH_AREAS.some(a => pointInRing(lat, lon, a.ring));
}

function pinColor(t) {
  if (!t) return PIN_COLORS.watch;
  return t.action === SEE_IT ? PIN_COLORS.go
       : t.action === NEGOTIATE ? PIN_COLORS.offer
       : t.action === SKIP ? PIN_COLORS.skip : PIN_COLORS.watch;
}

function ensureMap() {
  if (map || typeof L === "undefined") return map;
  map = L.map("map", { zoomControl: true, attributionControl: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
  areaLayer = L.layerGroup().addTo(map);
  for (const a of SEARCH_AREAS) {
    L.polygon(a.ring, {
      color: a.color, weight: 2, dashArray: "6 5",
      fillColor: a.color, fillOpacity: 0.07, interactive: false,
    }).addTo(areaLayer);
  }
  // A small legend so the two shaded shapes explain themselves.
  const legend = L.control({ position: "bottomleft" });
  legend.onAdd = () => {
    const div = document.createElement("div");
    div.className = "map-legend";
    div.innerHTML = SEARCH_AREAS.map(a =>
      `<span><i style="background:${a.color}"></i>${a.name} area</span>`).join("");
    return div;
  };
  legend.addTo(map);
  map.setView([33.9526, -84.5499], 11);  // Marietta until pins say otherwise
  return map;
}

function renderMap(rows) {
  const wrapEl = $("map-wrap");
  if (!wrapEl || wrapEl.hidden) return;
  if (!ensureMap()) {
    $("map-note").textContent = "The map library didn't load — check the connection and reopen.";
    return;
  }
  markerLayer.clearLayers();
  const t = triageMap();
  const pins = [];
  let outside = 0;
  for (const r of rows) {
    const f = r.f, lat = Number(f.Latitude), lon = Number(f.Longitude);
    if (!lat || !lon) continue;
    // The map only shows the hunting grounds. A find outside the drawn
    // areas stays in "Everything live" but does not earn a pin.
    if (inSearchArea(f) === false) { outside++; continue; }
    const tri = t.get(r.id);
    const marker = L.circleMarker([lat, lon], {
      radius: 9, color: "#ffffff", weight: 2,
      fillColor: pinColor(tri), fillOpacity: 0.92,
    });
    // The popup is a miniature verdict: what to do, how strong the
    // evidence is (0-100), and which strategy this house is for -- enough
    // to decide whether the full card is worth opening.
    const best = tri ? tri.best : fitSummary(f).best;
    const action = tri ? tri.action : null;
    const power = tri ? tri.strength : strengthOf(f, best);
    const fitLine = best && best.score > 0
      ? `Best fit: <b>${esc(best.name.split("\u2014")[0].trim())}</b>` +
        ` \u00b7 ${best.met}/${best.known} checks`
      : "Fits no search yet";
    marker.bindPopup(
      `<div class="pin-pop">` +
      `<b>${esc(f.Address || "")}</b>` +
      `<div>${money(f.Price)}${f.Sqft ? ` \u00b7 ${Number(f.Sqft).toLocaleString()} sqft` : ""}</div>` +
      (action ? `<div class="pin-action" style="color:${pinColor(tri)}">${esc(action)}` +
                `<span class="pin-strength">${power}</span></div>` : "") +
      `<div class="pin-fit">${fitLine}</div>` +
      `<a href="#" data-map-open="${esc(r.id)}">Open the card &rarr;</a>` +
      `</div>`);
    marker.addTo(markerLayer);
    pins.push([lat, lon]);
  }
  if (pins.length) {
    map.fitBounds(pins.concat(SEARCH_AREAS[0].ring), { padding: [24, 24], maxZoom: 14 });
  }
  const missing = rows.length - pins.length - outside;
  const bits = [`${pins.length} pinned`];
  if (outside) bits.push(`${outside} outside the drawn areas`);
  if (missing > 0) bits.push(`${missing} waiting on coordinates`);
  $("map-note").textContent = pins.length
    ? bits.join(" · ")
    : "No pins inside the drawn areas yet — the weekly search fills coordinates in.";
  // The map was possibly resized by a mode switch while hidden.
  setTimeout(() => map.invalidateSize(), 60);
}

function setViewMode(mode) {
  viewMode = mode;
  try { localStorage.setItem("hf-view-mode", mode); } catch {}
  document.querySelectorAll("[data-view-mode]").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.viewMode === mode)));
  const mapWrap = $("map-wrap"), list = $("matches-list");
  mapWrap.hidden = mode === "list";
  mapWrap.classList.toggle("map-full", mode === "map");
  list.hidden = mode === "map";
  renderMatches();
}

document.querySelectorAll("[data-view-mode]").forEach(b =>
  b.addEventListener("click", () => setViewMode(b.dataset.viewMode)));

// A popup link opens the same dialog a card tap does.
document.addEventListener("click", ev => {
  const a = ev.target.closest("[data-map-open]");
  if (!a) return;
  ev.preventDefault();
  openHouse(a.dataset.mapOpen);
});

function renderMatches() {
  renderLaneSwitch();
  const wrap = $("matches-list");
  const rows = visibleMatches();

  const empty = $("matches-empty");
  // "flex", not "block": the stylesheet centres this in its min-height,
  // and an inline display would quietly override that.
  empty.style.display = rows.length ? "none" : "flex";
  if (!rows.length) {
    // An empty Highlights lane means something different from an empty
    // database, and telling someone to go connect a feed they already
    // connected is how an app loses their trust.
    const view = ($("filter-view") || {}).value || "highlights";
    empty.textContent = houses.length
      ? (view === "highlights"
          ? "Nothing stands out in this lane right now. Switch to “Everything live” for the full set that matches your criteria."
          : view === "off"
            ? "Nothing has come off the market in this lane yet."
            : "No live listings in this lane. Check “Off market” for ones that have gone under contract.")
      : "Nothing yet. Add a search on the Searches tab — the worker fills this in once a listing feed is connected.";
  }

  const markets = [...new Set(rows.map(r => r.f.Market || "Unspecified"))];
  wrap.innerHTML = markets.map(market => {
    const inMarket = rows.filter(r => (r.f.Market || "Unspecified") === market);
    const best = inMarket[0];
    const bestBanner = best && best.v.bestRank >= 2 ? `
      <div class="banner">
        <div class="banner-label">🏆 Best in ${esc(market)}</div>
        <div class="banner-address">${esc(best.f.Address || "")}</div>
        <div class="banner-metrics">
          ${money(best.v.metrics.flipProfit)} flip profit ·
          ${pct(best.v.metrics.cashOnCash)} cash-on-cash ·
          ${pct(best.v.metrics.onePercentRatio)} rent ratio
        </div>
      </div>` : "";

    return `
      <section class="market-block">
        <h3 class="market-heading">${esc(market)}<span class="count">${inMarket.length}</span></h3>
        ${bestBanner}
        ${inMarket.map(r => houseCard(r)).join("")}
      </section>`;
  }).join("");

  // Staggered entrance: each card rises in a beat after the one before it,
  // capped so a long list doesn't keep the bottom half invisible for seconds.
  wrap.querySelectorAll(".house-card").forEach((el, i) =>
    el.style.setProperty("--enter", `${Math.min(i, 10) * 60}ms`));

  wrap.querySelectorAll("[data-house-id]").forEach(el =>
    el.addEventListener("click", ev => {
      // A link inside the card is its own destination; without this, tapping
      // Zillow would also open the detail dialog behind the new tab.
      if (ev.target.closest("a, details")) return;
      openHouse(el.dataset.houseId);
    }));

  renderFilterSummary(rows.length);
  renderMap(rows);

  // Photos arrive after the list does, so nothing waits on them.
  hydratePhotos(rows);
}

// A Zillow search deep-link built from the address. Nothing is fetched --
// this is the same as typing the address into Zillow's search box, and it is
// how you get to the photos and remarks the listing feed doesn't carry.
function zillowUrl(address) {
  if (!address) return "";
  // Zillow resolves an address slug to the listing itself when the string is
  // clean: commas out, punctuation out, single hyphens between words. A
  // stray comma or double space lands you on a search page instead of the
  // house, which is the difference between one tap and five.
  const slug = String(address)
    .replace(/[.,#]/g, " ")
    .trim()
    .split(/\s+/)
    .join("-");
  return `https://www.zillow.com/homes/${encodeURIComponent(slug)}_rb/`;
}

function listingLink(f) {
  return f["Listing URL"] || zillowUrl(f.Address);
}

// One house as plain text, shaped for pasting into a message. No markdown --
// iMessage renders none of it, and the link needs its own line to stay tappable.
function houseAsText(f) {
  const cats = String(f["Value Signals"] || "").split(",").map(s => s.trim()).filter(Boolean);
  const bits = [money(f.Price)];
  if (f.Beds || f.Baths) bits.push(`${f.Beds ?? "?"}bd/${f.Baths ?? "?"}ba`);
  bits.push(f.Sqft ? `${Number(f.Sqft).toLocaleString()} sqft` : "sqft not listed");
  if (f["Price Per Sqft"]) bits.push(`$${Number(f["Price Per Sqft"]).toLocaleString()}/sqft`);
  const { best } = fitSummary(f);
  const bestLine = best && best.score > 0
    ? `  Best fit: ${best.name.split("—")[0].trim()} (${best.met}/${best.known} checks)` : "";
  return [
    `${f.Address || "?"}${f.Qualified ? " *" : ""}`,
    "  " + bits.join(" · "),
    bestLine,
    cats.length ? `  Why: ${cats.join(", ")}` : "",
    "  " + listingLink(f),
  ].filter(Boolean).join("\n");
}

// ---- property lanes ----
// Three kinds of thing being hunted, each with its own economics: a detached
// house to flip or convert, a complex bought whole, a single unit inside a
// building somebody else owns. Mixing them in one list means every filter and
// every sort has to mean three things at once, so they get their own lane.
//
// Derived from the house rather than stored, so the lanes are right for the
// rows that predate any of this. Units and Property Type are the evidence;
// Found By breaks a tie when the feed said nothing useful.
// LANES and DEFAULT_LANE are declared up in the state block, because the
// initial value of currentLane reads them at load time and `const` is not
// hoisted the way a function declaration is.

function laneOf(f) {
  const type = String(f["Property Type"] || "").toLowerCase();
  const foundBy = String(f["Found By"] || "").toLowerCase();
  const units = Number(f.Units) || 0;
  if (units >= 5 || /multi|residential income|apartment|plex/.test(type)
      || foundBy.includes("multifamily")) return "multifamily";
  // Everything else is the houses lane. Attached stock does not get filtered
  // out here -- the searches already refuse it -- so anything of that kind
  // still in the sheet is a legacy row, and hiding it in a lane that no
  // longer exists would be worse than showing it.
  return "house";
}

function laneCounts() {
  const counts = Object.fromEntries(LANES.map(l => [l.id, 0]));
  // Counted against the view you are actually in, so the number on the tab
  // matches the number of cards under it rather than contradicting them.
  const view = ($("filter-view") || {}).value || "highlights";
  houses.forEach(r => {
    const f = r.fields || {};
    if (view === "off" ? isLive(f) : !isLive(f)) return;
    if (view === "highlights" && !isHighlighted(f, houseVerdict(f))) return;
    counts[laneOf(f)] += 1;
  });
  return counts;
}

// Two ways to see a house from the road, and the free one is the default.
//
// streetViewLink builds a plain Google Maps URL that opens the curb view in
// the Maps app. No API key, no Google account, no billing -- which is the
// point: embedding a Street View *image* requires a key, and getting a key
// requires enabling billing and attaching a card even when usage stays
// inside the free allowance. The link gives the same answer ("what does it
// actually look like?") for nothing.
function streetViewLink(address) {
  if (!address) return "";
  const q = encodeURIComponent(String(address));
  return `https://www.google.com/maps/search/?api=1&query=${q}&layer=c`;
}

// The embedded still, only if someone has chosen to paste a key in. Absent a
// key this returns "" and the card falls back to the link tile.
function streetView(address, w = 640, h = 200) {
  const key = localStorage.getItem("mapsKey");
  if (!key || !address) return "";
  const q = encodeURIComponent(String(address));
  return `https://maps.googleapis.com/maps/api/streetview?size=${w}x${h}`
       + `&location=${q}&fov=75&key=${encodeURIComponent(key)}`;
}

// ---- Mapillary ----
// Crowdsourced street-level photos. Free key, no billing account, no card --
// which is why it goes first. The trade is coverage: a through-road usually
// has imagery, a cul-de-sac often does not, so every lookup can legitimately
// come back with nothing and the card falls through to the next option.
//
// Looked up by coordinate rather than address, because that is what the API
// takes. Houses recorded before the worker stored coordinates have none and
// simply skip this step.
const MAPILLARY_BOX = 0.0012;   // ~130m; near enough to be the same house
const mapillaryCache = new Map();   // address -> url or null, per session

async function mapillaryThumb(f) {
  const key = localStorage.getItem("mapillaryKey");
  const lat = Number(f.Latitude), lon = Number(f.Longitude);
  if (!key || !lat || !lon) return null;
  const id = `${lat},${lon}`;
  if (mapillaryCache.has(id)) return mapillaryCache.get(id);

  const bbox = [lon - MAPILLARY_BOX, lat - MAPILLARY_BOX,
                lon + MAPILLARY_BOX, lat + MAPILLARY_BOX].join(",");
  const url = `https://graph.mapillary.com/images?access_token=${encodeURIComponent(key)}`
            + `&fields=thumb_1024_url&bbox=${bbox}&limit=1`;
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Mapillary ${res.status}`);
    const data = await res.json();
    const thumb = data?.data?.[0]?.thumb_1024_url || null;
    mapillaryCache.set(id, thumb);
    return thumb;
  } catch {
    // A failed lookup must not take the card down with it.
    mapillaryCache.set(id, null);
    return null;
  }
}

// Fill in Mapillary photos after the list is on screen. Deliberately not
// awaited during render: the cards should appear immediately and gain
// pictures as they arrive, rather than the whole list waiting on the
// slowest lookup.
async function hydratePhotos(rows) {
  for (const { id, f } of rows) {
    const thumb = await mapillaryThumb(f);
    if (!thumb) continue;
    const slot = document.querySelector(`[data-house-id="${CSS.escape(id)}"] .card-photo`);
    if (!slot) continue;
    const img = document.createElement("img");
    img.className = "card-photo";
    img.src = thumb;
    img.alt = "";
    img.loading = "lazy";
    slot.replaceWith(img);
  }
}

// ---- fit by strategy ----
// The same engine the email uses (send_digest.assess_fit): each house is
// measured against every Active criteria row, check by check. Unknowns are
// honest -- a basement this feed can't see reads "verify", never pass/fail.
const OVERSIZED_LOT_SQFT_FIT = 15000;
const DATED_BUILD_YEAR_FIT = 1985;

function assessFit(f, c) {
  const cats = String(f["Value Signals"] || "").toLowerCase();
  const checks = []; // [label, true|false|null]
  const price = f.Price, sqft = f.Sqft, lot = f["Lot Sqft"];
  const ppsf = f["Price Per Sqft"], baths = f.Baths, year = f["Year Built"];

  if (c["Max Price"] && price != null)
    checks.push([`price ${money(price)} vs ${money(c["Max Price"])} cap`, price <= c["Max Price"]]);
  if (c["Max Price Per Sqft"]) {
    if (!sqft) checks.push(["sqft unlisted (counts as under cap)", true]);
    else if (ppsf) checks.push([`$${ppsf}/sqft vs $${c["Max Price Per Sqft"]} cap`, ppsf <= c["Max Price Per Sqft"]]);
  }
  if (c["Min Units"]) {
    const units = f.Units;
    checks.push(units
      ? [`${units} units vs ${c["Min Units"]}+ goal`, units >= c["Min Units"]]
      : ["unit count unlisted", null]);
  }
  const musts = String(c["Must Haves"] || "").toLowerCase();
  if (musts.includes("basement"))
    checks.push(["basement", cats.includes("basement") ? true : null]);
  if (musts.includes("adu") || musts.includes("lot") || musts.includes("acre"))
    checks.push(lot
      ? [`lot room for ADU (${(lot / 43560).toFixed(2)} acre)`, lot >= OVERSIZED_LOT_SQFT_FIT]
      : ["lot room for ADU", null]);
  if (c["Target Total Sqft"] && sqft)
    checks.push([`${Number(sqft).toLocaleString()} sqft vs ${Number(c["Target Total Sqft"]).toLocaleString()}+ goal`, sqft >= c["Target Total Sqft"]]);
  if (c["Min Baths After Reno"] && baths != null)
    checks.push([`${baths} baths now vs ${c["Min Baths After Reno"]}+ after reno`, baths >= c["Min Baths After Reno"]]);
  if (c.Strategy === "Flip") {
    const fixer = (year && year <= DATED_BUILD_YEAR_FIT) || cats.includes("days on market")
      || cats.includes("price cut") || cats.includes("fixer");
    checks.push(["fixer evidence (age / sitting / price cut)", fixer ? true : null]);
  }
  return checks;
}

function fitSummary(f) {
  // A search is only ever judged against its own kind of building: a
  // multifamily row must not call a single-family house a fit however many
  // of its other boxes the house ticks, and vice versa.
  const laneOfCrit = c => (c["Property Class"] === "Multifamily" || c["Min Units"])
    ? "multifamily" : "house";
  const fits = criteria.filter(r => (r.fields || {}).Active
                                 && laneOfCrit(r.fields || {}) === laneOf(f)).map(r => {
    const c = r.fields || {};
    const checks = assessFit(f, c);
    const known = checks.filter(([, s]) => s !== null);
    const met = known.filter(([, s]) => s).length;
    // A blown price cap zeroes the score: a strategy you cannot afford is
    // not your best fit, however many other boxes the house ticks.
    const overCap = checks.some(([l, s]) => l.startsWith("price ") && s === false);
    return { name: c.Name || "Search", checks, met, known: known.length,
             score: overCap ? 0 : (known.length ? met / known.length : 0) };
  });
  // The five city-wide flip searches share one spec and differ only in
  // geography, which a fit check cannot see -- so they score every house
  // identically and would print as five identical rows. Collapse rows whose
  // shown name and checks match; one verdict per spec, not per city.
  const seen = new Set();
  const unique = fits.filter(x => {
    const key = x.name.split("—")[0].trim() + "|" + JSON.stringify(x.checks);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const best = unique.length
    ? unique.reduce((a, b) => (b.score > a.score || (b.score === a.score && b.met > a.met)) ? b : a)
    : null;
  return { fits: unique, best };
}

// bestOnly is the card: one line saying which plan this house is for and
// what it misses. The full per-strategy breakdown is four to twelve lines
// and belongs in the detail view, where you have gone looking for it --
// printed on every card it buries the house under its own analysis.
function fitBlock(f, bestOnly = false) {
  const { fits, best } = fitSummary(f);
  if (!fits.length) return "";
  const shown = bestOnly ? (best && best.score > 0 ? [best] : []) : fits;
  if (!shown.length) return "";
  return `<div class="fits">${shown.map(fit => {
    const isBest = best && fit.name === best.name && fit.score > 0;
    const misses = fit.checks.filter(([, s]) => s === false).map(([l]) => l);
    const unknowns = fit.checks.filter(([, s]) => s === null).map(([l]) => l);
    // On the card, one miss is the useful one -- the rest is a list nobody
    // reads standing in a driveway.
    const shownMisses = bestOnly ? misses.slice(0, 1) : misses;
    const detail = [
      shownMisses.length
        ? `misses: ${shownMisses.join("; ")}${bestOnly && misses.length > 1
            ? ` +${misses.length - 1} more` : ""}`
        : "",
      bestOnly || !unknowns.length ? "" : `verify: ${unknowns.join("; ")}`,
    ].filter(Boolean).join(" · ") || "meets everything we can measure";
    const cls = fit.score >= 0.75 ? "fit-good" : fit.score >= 0.4 ? "fit-mid" : "fit-low";
    return `<div class="fit-row">
      <span class="fit-name">${esc(fit.name.split("—")[0].trim())}</span>
      <span class="fit-score ${cls}">${fit.met}/${fit.known}</span>
      ${isBest ? '<span class="fit-best">BEST FIT</span>' : ""}
      <div class="fit-detail">${esc(detail)}</div>
    </div>`;
  }).join("")}</div>`;
}

// Value signals are why a house is here at all -- basement, ADU potential,
// FSBO, missing sqft, oversized lot. They get chips, not a buried field.
function signalChips(raw, limit = 0) {
  const signals = String(raw || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!signals.length) return "";
  const shown = limit ? signals.slice(0, limit) : signals;
  const rest = signals.length - shown.length;
  return `<div class="signals">${shown.map(s =>
    `<span class="signal">${esc(s)}</span>`).join("")}${
    rest ? `<span class="signal signal-more">+${rest}</span>` : ""}</div>`;
}

// The three most informative numbers this house actually has. The old tiles
// were always Flip profit / Cash-on-cash / 1% rule -- all of which need an
// ARV and rent a human hasn't typed yet, so every card led with three dashes
// while the evidence the feed does carry sat below. Financial tiles appear
// the moment real numbers exist; until then the card leads with what's known.
function cardStats(f, v) {
  const tiles = [];
  if (v.metrics.flipProfit != null)
    tiles.push(`<div><span class="stat-num ${v.metrics.flipProfit > 0 ? "pos" : "neg"}">${money(v.metrics.flipProfit)}</span><span class="stat-lbl">Flip profit</span></div>`);
  if (v.metrics.cashOnCash != null)
    tiles.push(`<div><span class="stat-num">${pct(v.metrics.cashOnCash)}</span><span class="stat-lbl">Cash-on-cash</span></div>`);
  if (v.metrics.onePercentRatio != null)
    tiles.push(`<div><span class="stat-num">${pct(v.metrics.onePercentRatio)}</span><span class="stat-lbl">1% rule</span></div>`);
  const disc = discountPct(f);
  if (tiles.length < 3 && disc)
    tiles.push(`<div><span class="stat-num pos">${disc}%</span><span class="stat-lbl">Under area $/sqft</span></div>`);
  if (tiles.length < 3 && f["Days on Market"])
    tiles.push(`<div><span class="stat-num">${f["Days on Market"]}</span><span class="stat-lbl">Days on market</span></div>`);
  if (tiles.length < 3 && f["Price Cut"])
    tiles.push(`<div><span class="stat-num pos">${f["Price Cut"]}%</span><span class="stat-lbl">Price cut</span></div>`);
  if (tiles.length < 3 && f["Lot Sqft"])
    tiles.push(`<div><span class="stat-num">${(f["Lot Sqft"] / 43560).toFixed(2)}</span><span class="stat-lbl">Acres</span></div>`);
  if (tiles.length < 3 && f["Year Built"])
    tiles.push(`<div><span class="stat-num">${f["Year Built"]}</span><span class="stat-lbl">Built</span></div>`);
  return tiles.length ? `<div class="card-stats">${tiles.slice(0, 3).join("")}</div>` : "";
}

// Where a house came from, in words a person recognises. Zillow is the hub --
// every house gets a Zillow link whatever found it -- so this answers the
// other half: which net caught this one. A hand-added house says so, because
// that is the one whose numbers nobody has verified against a feed.
const SOURCE_LABELS = {
  rentcast: "RentCast", reso: "MLS / IDX", search: "Search",
  homesteps: "HomeSteps foreclosure (Freddie Mac)",
};
function daysSince(d) {
  const ds = String(d || "").slice(0, 10);
  if (!ds) return null;
  const n = Math.floor((Date.now() - new Date(ds + "T12:00:00")) / 86400000);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

// The full trail: which feed, which search, how long ago it surfaced, and
// when the feed last confirmed it. One line, because provenance you have to
// go looking for is provenance nobody reads.
function provenanceLine(f) {
  const bits = [];
  const src = String(f.Source || "").trim();
  const finder = String(f["Found By"] || "").trim();
  if (src === "manual") bits.push("typed in by hand");
  else if (src) bits.push(`found via ${SOURCE_LABELS[src] || src}`
                        + (finder ? ` — “${finder.split("—")[0].trim()}”` : ""));
  else if (finder) bits.push(`found by “${finder.split("—")[0].trim()}”`);
  const added = daysSince(f["Date Added"]);
  if (added != null) bits.push(added === 0 ? "found today"
    : `found ${added} day${added === 1 ? "" : "s"} ago`);
  const seen = daysSince(f["Last Seen"]);
  if (seen != null && seen !== added) bits.push(seen === 0 ? "checked today"
    : `checked ${seen} day${seen === 1 ? "" : "s"} ago`);
  return bits.length ? `<div class="prov">${esc(bits.join(" · "))}</div>` : "";
}

// The move since the last run, in dollars. Mirrors send_digest._price_change_html:
// "Price Cut" is the listing's whole life, this is what changed on our watch.
function priceChangeChip(f) {
  const old = f["Previous Price"], now = f.Price;
  if (!old || !now || old === now) return "";
  const delta = now - old;
  const pct = Math.abs(delta) / old * 100;
  const dir = delta < 0 ? "drop" : "rise";
  const label = delta < 0
    ? `↓ ${money(Math.abs(delta))} off · ${pct.toFixed(1)}%`
    : `↑ ${money(delta)} up · ${pct.toFixed(1)}%`;
  return `<div class="price-change price-${dir}">${label}
            <span class="was">was ${money(old)}</span></div>`;
}

// ---- recommendations (mirror of scripts/recommend.py) ----
// A ranked list still leaves the deciding to you. This says what the
// evidence supports and what it rests on, so you can disagree with the
// specifics rather than taking or leaving a score.
//
// It only ever recommends a next step, never a purchase: nothing here knows
// what the work really costs or what the house really resells for, and a
// tool that said "buy this" while missing both would be worth less than none.
const REC_BELOW_MARKET = 15, REC_STALE_DAYS = 90, REC_CUT = 5, REC_DATED = 1985;
const SEE_IT = "Go and see it";
const NEGOTIATE = "Worth an offer under asking";
const WATCH = "Watch it";
const SKIP = "Skip unless you know something the data doesn't";

function breakevenResale(f) {
  const price = Number(f.Price), rehab = Number(f["Rehab Cost"]);
  if (!price || !rehab) return null;
  return (price + rehab) / (1 - 0.08);
}

function recommend(f, best) {
  const reasons = [], caveats = [];
  const dom = Number(f["Days on Market"]) || null;
  const cut = Number(f["Price Cut"]) || null;
  const year = Number(f["Year Built"]) || null;
  const discount = discountPct(f);
  const fitScore = (best && best.score) || 0;
  const fitName = ((best && best.name) || "").split("\u2014")[0].trim();

  const underpriced = discount != null && discount >= REC_BELOW_MARKET;
  const stale = dom != null && dom >= REC_STALE_DAYS;
  const motivated = cut != null && cut >= REC_CUT;
  const dated = year != null && year <= REC_DATED;

  if (underpriced) reasons.push(`${discount}% under the going rate per square foot round there`);
  if (stale) reasons.push(`sat ${dom} days when the area typically goes under contract in about three weeks`);
  if (motivated) reasons.push(`the seller has already cut ${cut}%`);
  if (dated && !underpriced && !motivated) reasons.push(`built ${year}, so likely original condition`);
  if (fitScore >= 1 && fitName) reasons.push(`meets every measurable part of ${fitName}`);
  else if (fitScore >= 0.75 && fitName) reasons.push(`meets most of ${fitName}`);

  let action;
  if ((underpriced || motivated) && fitScore >= 0.75) action = SEE_IT;
  else if (stale && motivated) {
    action = NEGOTIATE;
    reasons.push("both together say the asking price is not holding");
  }
  else if (underpriced || (fitScore >= 1 && (dated || stale))) action = SEE_IT;
  else if (fitScore >= 0.5 || dated || stale) action = WATCH;
  else action = SKIP;

  // Never a recommendation without a reason -- see recommend.py.
  if (!reasons.length) {
    reasons.push(fitScore && fitName
      ? `clears part of ${fitName} but nothing else marks it out`
      : "nothing in the data marks this out from the rest of the street");
  }

  const be = breakevenResale(f);
  caveats.push(be
    ? `needs to resell above ${money(be)} to break even, on a rehab estimate nobody has verified`
    : "no rehab estimate yet, so there is no profit figure here");
  if (f.ARV == null || f.ARV === "")
    caveats.push("resale value is the one number this cannot see");

  return { action, reasons, caveats };
}

// Which play this house is for, and the numbers that define it.
// Mirrors recommend.approach. The play follows the strategy the house fits,
// not the shape of the lot -- picking it from the lot produced a house whose
// best fit was Flip described as an ADU project.
function approach(f, best) {
  const name = ((best && best.name) || "").split("\u2014")[0].trim();
  const detail = (best && best.name) || "";
  const price = Number(f.Price), rehab = Number(f["Rehab Cost"]), sqft = Number(f.Sqft);
  const lot = Number(f["Lot Sqft"]);
  const acres = lot ? lot / 43560 : null;
  const cats = String(f["Value Signals"] || "").toLowerCase();

  const numbers = [];
  if (price) numbers.push(`buy around ${money(price)}`);
  if (rehab) numbers.push(`budget about ${money(rehab)} of work`
                          + (sqft ? ` (${money(rehab / sqft)}/sqft)` : ""));
  const be = breakevenResale(f);
  if (be) numbers.push(`exit above ${money(be)} to make anything`);

  const lower = (name + " " + detail).toLowerCase();
  let play;
  if (lower.includes("adu") || lower.includes("brrrr a"))
    play = "Add a second dwelling. The value is in the land rather than the house, "
         + "so the condition of the existing building matters less than what the lot will take.";
  else if (lower.includes("basement"))
    play = "Convert the basement into a separate unit. Two rents from one roof, "
         + "and the square footage is already built and paid for.";
  else if (lower.includes("flip"))
    play = "Cosmetic renovation and resale. The margin comes from buying under the "
         + "street's going rate, so the entry price is the whole game.";
  else
    play = "Hold and rent. Whether it works turns on rent against the financing "
         + "after the work, and no rent estimate has been entered yet.";

  if (lower.includes("flip") && acres && acres >= 0.34)
    play += ` The ${acres.toFixed(2)}-acre lot would also take a second dwelling `
          + `if you would rather hold it than sell it.`;
  else if (lower.includes("flip") && cats.includes("basement"))
    play += " There is a basement, so a conversion is an alternative to selling it on.";

  return { name: name || "No matching strategy", play, numbers };
}

// The specific things to do, in order. Written to be finishable: "do more
// research" is not a step; "pull three comps and check they clear $500,565" is.
function nextSteps(f, action, best) {
  const steps = [];
  const price = Number(f.Price), dom = Number(f["Days on Market"]);
  const cut = Number(f["Price Cut"]);
  const detail = ((best && best.name) || "").toLowerCase();
  const cats = String(f["Value Signals"] || "").toLowerCase();
  const lot = Number(f["Lot Sqft"]);
  const acres = lot ? lot / 43560 : null;
  const be = breakevenResale(f);

  steps.push(be
    ? `Pull the last three comparable sales within half a mile and check they clear `
      + `${money(be)}. That single number decides whether the rest of this is worth doing.`
    : "Enter a rehab estimate here so there is a breakeven figure to test against.");

  if (!Number(f.Sqft))
    steps.push("Ask the listing agent for the square footage \u2014 it is missing from the "
             + "feed, which is why this passed the price-per-foot test by default.");

  if (dom >= REC_STALE_DAYS && price)
    steps.push(`It has sat ${dom} days${cut ? ` and already come down ${cut}%` : ""}. `
             + `An opening offer near ${money(price * 0.9)} (10% under asking) is `
             + `defensible on the time alone.`);

  if (detail.includes("adu") || (acres && acres >= 0.34))
    steps.push("Check Cobb County zoning for an accessory dwelling on a lot this size "
             + "before anything else \u2014 if it is not permitted, the plan for this house is void.");
  if (detail.includes("basement") || cats.includes("basement"))
    steps.push("Confirm on the visit that the basement is unfinished, has ceiling height, "
             + "and can take a legal egress window. Without egress it is storage, not a unit.");

  if (action === SEE_IT)
    steps.push("Book the viewing this week. Underpriced and dated does not stay on the market.");
  else if (action === NEGOTIATE)
    steps.push("Ask the agent why it has not sold before offering. The answer is usually "
             + "either the price or something you would want to know about the house.");
  else if (action === WATCH)
    steps.push("No action yet. It reappears the moment the price moves.");

  return steps;
}

// 0-100: how much evidence has piled up. Mirrors recommend.strength -- the
// same four facts, the same caps, so the app and the email always show the
// same number for the same house.
function strengthOf(f, best) {
  const dom = Number(f["Days on Market"]) || 0;
  const cut = Number(f["Price Cut"]) || 0;
  const discount = discountPct(f) || 0;
  const fit = Math.max(0, Math.min(1, (best && best.score) || 0));
  return Math.round(35 * Math.min(discount, 30) / 30
                  + 25 * fit
                  + 20 * Math.min(dom, 180) / 180
                  + 20 * Math.min(cut, 20) / 20);
}

// Final actions across the whole list, not per house alone: only the three
// strongest hold "go and see it" at once, because there is one Saturday.
// Recomputed per render and cached by house id. Mirrors recommend.triage.
const SEE_LIMIT = 3;
let triageCache = null;

function triageMap() {
  if (triageCache) return triageCache;
  const rows = houses.map(r => {
    const f = r.fields || {};
    const { best } = fitSummary(f);
    const rec = recommend(f, best);
    let action = rec.action, reasons = rec.reasons, heldBack = false;
    if (["Off Market", "Under Contract"].includes(f["Listing Status"])
        || DECIDED_STATUSES.includes(f.Status)) {
      action = SKIP;
      reasons = ["no longer available, whatever the numbers said"];
    }
    return { id: r.id, f, best, action, reasons, caveats: rec.caveats,
             strength: strengthOf(f, best), heldBack };
  });
  const sees = rows.filter(r => r.action === SEE_IT)
                   .sort((a, b) => b.strength - a.strength);
  for (const r of sees.slice(SEE_LIMIT)) {
    r.action = WATCH;
    r.heldBack = true;
    r.reasons.push(`strong on its own, but only the ${SEE_LIMIT} strongest earn `
                 + `a viewing in any one week — this one is next in line`);
  }
  triageCache = new Map(rows.map(r => [r.id, r]));
  return triageCache;
}

function recommendationBlock(f, id) {
  const t = id != null ? triageMap().get(id) : null;
  const best = t ? t.best : fitSummary(f).best;
  const action = t ? t.action : recommend(f, best).action;
  const reasons = t ? t.reasons : recommend(f, best).reasons;
  const caveats = t ? t.caveats : recommend(f, best).caveats;
  const power = t ? t.strength : strengthOf(f, best);
  const cls = action === SEE_IT ? "rec-go"
            : action === NEGOTIATE ? "rec-offer"
            : action === WATCH ? "rec-watch" : "rec-skip";
  // The card shows one line: verdict, strength, the reason that matters.
  // Everything else -- the full reasoning, the play, the steps, the caveats
  // -- is a tap away behind a native <details>, so the list stays scannable
  // and the depth is there for whoever wants it.
  const headlineReason = (t && t.heldBack) ? reasons[reasons.length - 1]
                                           : (reasons[0] || "");
  const plan = approach(f, best);
  const steps = nextSteps(f, action, best);
  return `<details class="rec ${cls}">
    <summary>
      <span class="rec-action">${esc(action)}</span>
      <span class="rec-strength" title="Strength of the evidence, 0–100">${power}</span>
      <span class="rec-first">${esc(headlineReason)}</span>
    </summary>
    <ul class="rec-why">${reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
    <div class="rec-plan"><b>${esc(plan.name)}.</b> ${esc(plan.play)}</div>
    <div class="rec-numbers">${plan.numbers.map(esc).join(" \u00b7 ")}</div>
    <div class="rec-blind">Next steps</div>
    <ol class="rec-steps">${steps.map(t2 => `<li>${esc(t2)}</li>`).join("")}</ol>
    <div class="rec-blind">What this can't see</div>
    <ul class="rec-caveats">${caveats.map(c => `<li>${esc(c)}</li>`).join("")}</ul>
  </details>`;
}

function houseCard({ id, f, v }) {
  // The feed's own photo when it has one, a Street View still otherwise --
  // for a fixer hunt the kerb shot is close to the point, since a tired roof
  // and an overgrown yard are visible from the road.
  const src = f["Photo URL"] || streetView(f.Address);
  const link = streetViewLink(f.Address);
  let photo;
  if (src) {
    photo = `<img class="card-photo" src="${esc(src)}" alt="" loading="lazy">`;
  } else if (link) {
    // No key and no feed photo: a tappable tile that opens the curb view in
    // Google Maps. Costs nothing and needs no account.
    photo = `<a class="card-photo card-photo-link" href="${esc(link)}" data-photo-slot
                target="_blank" rel="noopener">
               <span class="card-photo-icon">🛣️</span>
               <span>See it from the street</span>
             </a>`;
  } else {
    photo = `<div class="card-photo card-photo-empty"></div>`;
  }
  const tier = t => `<span class="tier tier-${t.replace(" ", "-")}">${t}</span>`;
  return `
    <article class="card house-card" data-house-id="${esc(id)}">
      ${photo}
      <div class="card-body">
        <div class="card-title">
          ${esc(f.Address || "Untitled")}
          ${f.Qualified ? '<span class="badge">QUALIFIED</span>' : ""}
          ${isLive(f) ? "" : (f["Listing Status"] === "Under Contract"
            ? '<span class="badge badge-pending" title="The listing feed reports this as pending or contingent">UNDER CONTRACT</span>'
            : '<span class="badge badge-off" title="No longer an active listing — sold, withdrawn, or stopped appearing in the feed">OFF MARKET</span>')}
        </div>
        <div class="card-sub">
          ${money(f.Price)} · ${f.Beds ?? "?"}bd/${f.Baths ?? "?"}ba
          ${f.Sqft ? ` · ${Number(f.Sqft).toLocaleString()} sqft` : ""}
          ${f["Price Per Sqft"] ? ` · $${Number(f["Price Per Sqft"]).toLocaleString()}/sqft` : ""}
          ${f.Status && f.Status !== "New" ? ` · ${esc(f.Status)}` : ""}
        </div>
        ${priceChangeChip(f)}
        ${provenanceLine(f)}
        ${signalChips(f["Value Signals"], 3)}
        ${recommendationBlock(f, id)}
        ${fitBlock(f, true)}
        ${cardStats(f, v)}
        <div class="card-verdicts">
          Flip ${tier(v.flipTier)} &nbsp; BRRRR ${tier(v.brrrrTier)}
          ${v.bestStrategy ? `<span class="best-strategy">→ better as ${v.bestStrategy}</span>` : ""}
        </div>
        <div class="card-links">
          <a class="card-link primary-link" href="${esc(listingLink(f))}"
             target="_blank" rel="noopener">Zillow — photos &amp; remarks →</a>
          ${link ? `<a class="card-link" href="${esc(link)}" target="_blank"
             rel="noopener">Street view →</a>` : ""}
          <a class="card-link" href="https://www.google.com/search?q=${encodeURIComponent(f.Address || "")}"
             target="_blank" rel="noopener">Google it →</a>
        </div>
      </div>
    </article>`;
}

// ---- rendering: criteria ----
const CRITERIA_FIELDS = [
  ["Name", "text", "Name this search"],
  ["Market", "text", "Atlanta"],
  ["City", "text", "Atlanta"],
  ["State", "text", "GA"],
  ["Min Price", "number", ""],
  ["Max Price", "number", ""],
  ["Min Beds", "number", ""],
  ["Min Baths", "number", ""],
  ["Min Sqft", "number", ""],
  ["Zip Codes", "text", "30068, 30067, 30062"],
  // Searches are single family only; this column can narrow within that but
  // never widen it, so suggesting "Townhouse" here only invites a value the
  // worker will ignore. Leaving it blank is the normal case.
  ["Property Types", "text", "Single Family (leave blank for all)"],
  ["Keywords", "text", "fixer, as-is, TLC"],
  ["Must Haves", "text", "basement, adu/oversized lot"],
  ["Max Price Per Sqft", "number", "175"],
  ["Max All In", "number", "350000"],
  ["Target Total Sqft", "number", "2000"],
  ["Min Baths After Reno", "number", "3"],
  ["Target Flip Profit", "number", "50000"],
  ["Target Cash on Cash", "number", "8"],
  ["Target One Percent", "number", "1"],
  ["Rehab Cost Per Sqft", "number", "20"],
];

function renderCriteria() {
  const wrap = $("criteria-list");
  $("criteria-empty").style.display = criteria.length ? "none" : "block";
  wrap.innerHTML = criteria.map(rec => {
    const f = rec.fields || {};
    return `
      <article class="card criteria-card">
        <div class="card-title">
          ${esc(f.Name || f.Market || "Untitled search")}
          <span class="badge ${f.Active ? "" : "badge-off"}">${f.Active ? "ACTIVE" : "PAUSED"}</span>
        </div>
        <div class="card-sub">
          ${f["Zip Codes"] ? esc(String(f["Zip Codes"]).split(",")[0].trim()) + " +" + (String(f["Zip Codes"]).split(",").length - 1) + " zips" : esc(f.City || "")}${!f["Zip Codes"] && f.State ? ", " + esc(f.State) : ""} ·
          ${money(f["Min Price"])}–${money(f["Max Price"])}
          ${f["Max Price Per Sqft"] ? ` · ≤$${f["Max Price Per Sqft"]}/sqft` : ""}
          ${f["Max All In"] ? ` · ≤${money(f["Max All In"])} all-in` : ""}
        </div>
        ${f["Must Haves"] ? `<div class="card-sub">Must have: ${esc(f["Must Haves"])}</div>` : ""}
        <div class="card-sub">
          Targets: ${money(f["Target Flip Profit"])} profit ·
          ${f["Target Cash on Cash"] ?? "—"}% CoC ·
          ${f["Target One Percent"] ?? "—"}% rent ratio
        </div>
        <div class="card-actions">
          <button class="secondary" data-edit-criteria="${esc(rec.id)}">Edit</button>
          <button class="secondary" data-toggle-criteria="${esc(rec.id)}">${f.Active ? "Pause" : "Activate"}</button>
        </div>
      </article>`;
  }).join("");

  wrap.querySelectorAll("[data-edit-criteria]").forEach(b =>
    b.addEventListener("click", () => openCriteria(b.dataset.editCriteria)));
  wrap.querySelectorAll("[data-toggle-criteria]").forEach(b =>
    b.addEventListener("click", () => toggleCriteria(b.dataset.toggleCriteria)));
}

function openCriteria(id) {
  const rec = criteria.find(r => r.id === id);
  const f = rec ? rec.fields : {};
  $("criteria-dialog-title").textContent = rec ? "Edit search" : "New search";
  $("criteria-id").value = rec ? rec.id : "";
  $("criteria-fields").innerHTML = CRITERIA_FIELDS.map(([key, type, ph]) => `
    <label>${key}
      <input type="${type}" data-field="${esc(key)}" placeholder="${esc(ph)}"
             value="${esc(f[key] ?? "")}">
    </label>`).join("");
  $("criteria-active").checked = rec ? !!f.Active : true;
  $("criteria-dialog").showModal();
}

async function saveCriteria(e) {
  e.preventDefault();
  const id = $("criteria-id").value;
  const fields = { Active: $("criteria-active").checked };
  $("criteria-fields").querySelectorAll("[data-field]").forEach(input => {
    const key = input.dataset.field;
    const raw = input.value.trim();
    if (raw === "") { fields[key] = null; return; }
    fields[key] = input.type === "number" ? Number(raw) : raw;
  });
  try {
    await saveRecord(TABLE_CRITERIA, id, fields);
    $("criteria-dialog").close();
    setStatus("Saved.");
    await refresh();
  } catch (err) { setStatus("Save failed: " + err.message, false); }
}

async function toggleCriteria(id) {
  const rec = criteria.find(r => r.id === id);
  if (!rec) return;
  try {
    await saveRecord(TABLE_CRITERIA, id, { Active: !rec.fields.Active });
    await refresh();
  } catch (err) { setStatus("Update failed: " + err.message, false); }
}

// ---- house detail ----
function openHouse(id) {
  // Ids travel through the DOM as strings; the database hands out numbers.
  const rec = houses.find(r => String(r.id) === String(id));
  if (!rec) return;
  const f = rec.fields || {};
  const v = houseVerdict(f);
  $("house-id").value = id;
  $("house-dialog-title").textContent = f.Address || "House";
  $("house-detail").innerHTML = `
    <div class="card-sub">${money(f.Price)} · ${f.Beds ?? "?"}bd/${f.Baths ?? "?"}ba${f.Sqft ? ` · ${Number(f.Sqft).toLocaleString()} sqft` : ""}</div>
    ${signalChips(f["Value Signals"])}
    ${fitBlock(f)}
    ${(v.flipTier === "NO DATA" && v.brrrrTier === "NO DATA"
       && v.flipReasons.join() === v.brrrrReasons.join())
      ? `<div class="verdict-block">
      <strong>Flip &amp; BRRRR — NO DATA</strong>
      <ul>${v.flipReasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
    </div>`
      : `<div class="verdict-block">
      <strong>Flip — ${v.flipTier}</strong>
      <ul>${v.flipReasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      <strong>BRRRR — ${v.brrrrTier}</strong>
      <ul>${v.brrrrReasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
    </div>`}
    ${listingLink(f) ? `<p class="card-links"><a class="card-link primary-link" href="${esc(listingLink(f))}" target="_blank" rel="noopener">Open on Zillow — photos &amp; remarks →</a></p>` : ""}`;
  ["Price", "Rehab Cost", "ARV", "Rent Estimate"].forEach(k => {
    $("h-" + k.replace(/ /g, "-")).value = f[k] ?? "";
  });
  $("h-status").value = f.Status || "New";
  $("house-dialog").showModal();
}

function recomputeHouseDialog() {
  const num = id => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
  const v = qualify(num("h-Price"), num("h-Rehab-Cost"), num("h-ARV"), num("h-Rent-Estimate"));
  $("house-live").innerHTML = `
    <div class="card-stats">
      <div><span class="stat-num ${(v.metrics.flipProfit ?? 0) > 0 ? "pos" : "neg"}">${money(v.metrics.flipProfit)}</span><span class="stat-lbl">Flip profit</span></div>
      <div><span class="stat-num">${pct(v.metrics.cashOnCash)}</span><span class="stat-lbl">Cash-on-cash</span></div>
      <div><span class="stat-num">${pct(v.metrics.onePercentRatio)}</span><span class="stat-lbl">1% rule</span></div>
    </div>
    <div class="card-verdicts">Flip <span class="tier tier-${v.flipTier.replace(" ", "-")}">${v.flipTier}</span>
      &nbsp; BRRRR <span class="tier tier-${v.brrrrTier.replace(" ", "-")}">${v.brrrrTier}</span></div>`;
}

async function saveHouse(e) {
  e.preventDefault();
  const id = $("house-id").value;
  const num = elId => { const v = $(elId).value.trim(); return v === "" ? null : Number(v); };
  const price = num("h-Price"), rehab = num("h-Rehab-Cost"), arv = num("h-ARV"), rent = num("h-Rent-Estimate");
  const v = qualify(price, rehab, arv, rent);
  const fields = {
    Price: price, "Rehab Cost": rehab, ARV: arv, "Rent Estimate": rent,
    Status: $("h-status").value,
    "Flip Profit": v.metrics.flipProfit != null ? Math.round(v.metrics.flipProfit) : null,
    "Cash on Cash": v.metrics.cashOnCash != null ? Number((v.metrics.cashOnCash * 100).toFixed(1)) : null,
    "One Percent": v.metrics.onePercentRatio != null ? Number((v.metrics.onePercentRatio * 100).toFixed(2)) : null,
    "Flip Verdict": v.flipTier, "BRRRR Verdict": v.brrrrTier,
    "Best Strategy": v.bestStrategy || "",
  };
  try {
    await saveRecord(TABLE_HOUSES, id, fields);
    $("house-dialog").close();
    setStatus("Saved.");
    await refresh();
  } catch (err) { setStatus("Save failed: " + err.message, false); }
}

// ---- load / navigation ----
async function refresh() {
  try {
    setStatus("Loading…");
    [criteria, houses] = await Promise.all([listAll(TABLE_CRITERIA), listAll(TABLE_HOUSES)]);
    triageCache = null;   // new data, new triage
    const markets = [...new Set(houses.map(r => r.fields?.Market).filter(Boolean))];
    $("filter-market").innerHTML = '<option value="">All markets</option>' +
      markets.map(m => `<option>${esc(m)}</option>`).join("");
    // Rebuilt on every load so a strategy added to a search becomes a sort
    // option on the next refresh. The current choice survives the rebuild.
    const sortSel = $("sort-by");
    const keep = sortSel.value;
    sortSel.innerHTML = sortOptions().map(([v, label]) =>
      `<option value="${esc(v)}">${esc(label)}</option>`).join("");
    if ([...sortSel.options].some(o => o.value === keep)) sortSel.value = keep;
    renderCriteria();
    renderMatches();
    setStatus("");
  } catch (err) {
    // Say what went wrong and stay put. There is no screen to fall back to
    // and nothing for anyone to re-enter -- the database is built in, so a
    // failure here is a network or a server problem, never a credentials
    // one, and the only useful response is to try again.
    setStatus(`${err.message} — pull down to retry.`, false);
  }
}

function setScreen(next) {
  screen = next;
  $("screen-matches").style.display = next === "matches" ? "block" : "none";
  $("screen-criteria").style.display = next === "criteria" ? "block" : "none";
  // The selector stays visible on both screens -- it is how you get back --
  // but it reads as unselected while you are off the list, so the highlight
  // never claims you are looking at something you are not.
  $("lane-switch").classList.toggle("idle", next !== "matches");
  $("nav-criteria").classList.toggle("active", next === "criteria");
}

document.addEventListener("DOMContentLoaded", () => {
  // Both photo keys live in this browser only. Neither can read the
  // database, so neither belongs in the repo secrets.
  const photoKeys = [["maps-key", "mapsKey"], ["mapillary-key", "mapillaryKey"]];
  photoKeys.forEach(([id, slot]) => {
    const el = $(id);
    if (el) el.value = localStorage.getItem(slot) || "";
  });
  $("save-photo-keys")?.addEventListener("click", () => {
    let saved = 0;
    photoKeys.forEach(([id, slot]) => {
      const value = ($(id).value || "").trim();
      if (value) { localStorage.setItem(slot, value); saved += 1; }
      else localStorage.removeItem(slot);
    });
    setStatus(saved ? "Saved. Photos will appear on the Matches tab."
                    : "Both keys cleared; cards keep their street-view link.");
    renderMatches();
  });
  // Tests the keys from inside the app, which is the only place a test is
  // meaningful: a key restricted to this site (as it should be) fails from a
  // browser address bar no matter how correct it is, because there is no
  // referrer. Google's metadata endpoint is used rather than the image one --
  // it reports the same errors and is explicitly free, so checking costs
  // nothing against the quota.
  $("test-photo-keys")?.addEventListener("click", async () => {
    const out = $("key-test");
    out.textContent = "Testing…";
    const lines = [];

    const mapsKey = (($("maps-key") || {}).value || "").trim()
                    || localStorage.getItem("mapsKey");
    if (!mapsKey) lines.push("Google: no key saved.");
    else {
      const url = "https://maps.googleapis.com/maps/api/streetview/metadata"
                + "?location=" + encodeURIComponent("3601 Longfellow Trl, Marietta, GA 30062")
                + "&key=" + encodeURIComponent(mapsKey);
      try {
        const d = await (await fetch(url)).json();
        if (d.status === "OK") lines.push("Google: working — imagery found for a test address.");
        else if (d.status === "ZERO_RESULTS")
          lines.push("Google: key accepted, but no imagery at the test address.");
        else if (d.status === "REQUEST_DENIED")
          lines.push(`Google: rejected — ${d.error_message || "check the key and its restrictions."}`);
        else lines.push(`Google: ${d.status}${d.error_message ? " — " + d.error_message : ""}`);
      } catch (err) {
        lines.push(`Google: request failed (${err.message}).`);
      }
    }

    const mlyKey = (($("mapillary-key") || {}).value || "").trim()
                   || localStorage.getItem("mapillaryKey");
    if (!mlyKey) lines.push("Mapillary: no token saved.");
    else {
      const url = `https://graph.mapillary.com/images?access_token=${encodeURIComponent(mlyKey)}`
                + "&fields=id&bbox=-84.5612,33.9388,-84.5412,33.9588&limit=1";
      try {
        const res = await fetch(url);
        const d = await res.json();
        if (!res.ok) lines.push(`Mapillary: rejected — ${d?.error?.message || res.status}.`);
        else if (d?.data?.length) lines.push("Mapillary: working — imagery found near Marietta.");
        else lines.push("Mapillary: token accepted, but no imagery in the test area.");
      } catch (err) {
        lines.push(`Mapillary: request failed (${err.message}).`);
      }
    }

    // How many houses each source can actually serve, which is the question
    // behind the question.
    // Only worth saying once there are houses to count. "0 of 0" reads as a
    // failure when it only means the list has not loaded yet.
    if (houses.length) {
      const withCoords = houses.filter(r => r.fields?.Latitude && r.fields?.Longitude).length;
      lines.push(`${withCoords} of ${houses.length} houses have coordinates `
               + "(Mapillary needs them; Google works from the address).");
    }
    out.innerHTML = lines.map(l => `<div>${esc(l)}</div>`).join("");
  });

  $("clear-photo-keys")?.addEventListener("click", () => {
    photoKeys.forEach(([id, slot]) => {
      localStorage.removeItem(slot);
      $(id).value = "";
    });
    setStatus("Keys removed; cards keep their street-view link.");
    renderMatches();
  });

  $("nav-criteria").addEventListener("click", () =>
    setScreen(screen === "criteria" ? "matches" : "criteria"));
  $("new-criteria").addEventListener("click", () => openCriteria(null));
  $("criteria-form").addEventListener("submit", saveCriteria);
  $("criteria-cancel").addEventListener("click", () => $("criteria-dialog").close());
  $("house-form").addEventListener("submit", saveHouse);
  $("house-cancel").addEventListener("click", () => $("house-dialog").close());
  ["h-Price", "h-Rehab-Cost", "h-ARV", "h-Rent-Estimate"].forEach(id =>
    $(id).addEventListener("input", recomputeHouseDialog));
  // Copies exactly what's on screen -- whatever the filters are showing --
  // as plain text, so it can go straight into a message.
  $("copy-matches").addEventListener("click", async () => {
    const text = visibleMatches().map(r => houseAsText(r.f)).join("\n\n");
    if (!text) { setStatus("Nothing to copy.", false); return; }
    try {
      await navigator.clipboard.writeText(text);
      setStatus(`Copied ${visibleMatches().length} house(s) — paste into a message.`);
    } catch { window.prompt("Copy this:", text); }
  });

  enableLaneSwipe($("screen-matches"));

  // Arrow keys do the same thing on a laptop.
  document.addEventListener("keydown", e => {
    if (screen !== "matches" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowRight") shiftLane(1);
    if (e.key === "ArrowLeft") shiftLane(-1);
  });

  $("filters-toggle").addEventListener("click", () => {
    const panel = $("filters");
    const open = panel.hidden;
    panel.hidden = !open;
    $("filters-toggle").setAttribute("aria-expanded", String(open));
  });

  $("filter-market").addEventListener("change", renderMatches);
  $("filter-view").addEventListener("change", renderMatches);
  $("sort-by").addEventListener("change", renderMatches);

  setScreen("matches");
  refresh();

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
});

// Restore the last view mode (List is default and needs no work).
if (viewMode !== "list") setViewMode(viewMode);
