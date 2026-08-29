/* House Finder — Airtable-backed PWA.
 *
 * Airtable is the database of record. This app talks to its REST API directly
 * with a Personal Access Token kept in localStorage (never committed, never
 * sent anywhere but api.airtable.com).
 *
 * Deal math here mirrors scripts/deals.py. If you change one, change both --
 * the worker scores listings server-side, this scores what you type live.
 */

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

function qualify(price, rehab, arv, rent) {
  const m = computeMetrics(price, rehab, arv, rent);
  let flipTier, flipReasons = [];
  if (m.flipProfit == null) {
    flipTier = "NO DATA"; flipReasons = ["Needs price, rehab cost, and ARV"];
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
  if (m.cashLeftInDeal == null) { brrrrTier = "NO DATA"; brrrrReasons = ["Needs price, rehab cost, and ARV"]; }
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

// ---- Airtable client ----
const store = {
  get token() { return localStorage.getItem("airtable_token") || ""; },
  set token(v) { localStorage.setItem("airtable_token", v); },
  get baseId() { return localStorage.getItem("airtable_base") || ""; },
  set baseId(v) { localStorage.setItem("airtable_base", v); },
};

async function airtable(method, table, { body, query } = {}) {
  if (!store.token || !store.baseId) throw new Error("Airtable not connected yet.");
  let url = `https://api.airtable.com/v0/${store.baseId}/${encodeURIComponent(table)}`;
  if (query) url += "?" + new URLSearchParams(query).toString();
  const res = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${store.token}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    const type = detail?.error?.type || "";
    // Airtable deliberately conflates "no permission" with "no such table" in
    // one 403, and its wording sends people off checking their token when the
    // real answer is almost always that the base is still empty. Say both.
    if (res.status === 403 && type === "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND") {
      throw new Error(
        `Couldn't read the "${table}" table. Either the base doesn't have it yet ` +
        `— run the "Set Up Airtable Base" action to build it — or the token isn't granted on this base.`
      );
    }
    if (res.status === 404) {
      throw new Error(`Base ${store.baseId} has no "${table}" table yet. Run the "Set Up Airtable Base" action to build it.`);
    }
    if (res.status === 401) throw new Error("Airtable rejected the token. Check it was copied whole.");
    throw new Error(detail?.error?.message || type || `Airtable ${res.status}`);
  }
  return res.json();
}

async function listAll(table) {
  // Follow pagination so a growing table isn't silently cut off at 100.
  let records = [], offset;
  do {
    const query = { pageSize: "100" };
    if (offset) query.offset = offset;
    const page = await airtable("GET", table, { query });
    records = records.concat(page.records || []);
    offset = page.offset;
  } while (offset);
  return records;
}

// ---- state ----
let criteria = [];
let houses = [];
// The three lanes. Declared here rather than beside laneOf() further down,
// because currentLane below reads them while this file is still executing --
// a `const` in the wrong order is a load-time crash, not a late failure.
const LANES = [
  { id: "multifamily", label: "Multiplex", hint: "20+ unit complexes" },
  { id: "house",       label: "Houses",    hint: "detached single family" },
  { id: "condo",       label: "Condos",    hint: "condo and townhouse units" },
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
  const opts = [["best", "Sort: best overall"]];
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

function visibleMatches() {
  const marketFilter = $("filter-market").value;
  const onlyQualified = $("filter-qualified").checked;
  const sortBy = ($("sort-by") || {}).value || "best";
  const catCount = f => String(f["Value Signals"] || "").split(",").filter(s => s.trim()).length;

  let rows = houses.map(r => ({ id: r.id, f: r.fields || {}, v: houseVerdict(r.fields || {}) }));
  rows = rows.filter(r => laneOf(r.f) === currentLane);
  if (marketFilter) rows = rows.filter(r => (r.f.Market || "") === marketFilter);
  if (onlyQualified) rows = rows.filter(r => r.f.Qualified || r.v.bestRank >= 2);

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
    // Best overall: category count leads, because flip profit runs off a
    // placeholder ARV until someone types a real one.
    rows.sort((a, b) => (b.v.bestRank - a.v.bestRank)
      || (catCount(b.f) - catCount(a.f))
      || ((b.v.metrics.flipProfit ?? -Infinity) - (a.v.metrics.flipProfit ?? -Infinity)));
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
              aria-selected="${l.id === currentLane}" title="${esc(l.hint)}">
        <span class="lane-label">${esc(l.label)}</span>
        <span class="lane-count">${counts[l.id]}</span>
      </button>`).join("");
  el.querySelectorAll("[data-lane]").forEach(b =>
    b.addEventListener("click", () => {
      currentLane = b.dataset.lane;
      localStorage.setItem("lane", currentLane);
      renderLaneSwitch();
      renderMatches();
    }));
}

function renderMatches() {
  renderLaneSwitch();
  const wrap = $("matches-list");
  const rows = visibleMatches();

  $("matches-empty").style.display = rows.length ? "none" : "block";

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

  wrap.querySelectorAll("[data-house-id]").forEach(el =>
    el.addEventListener("click", () => openHouse(el.dataset.houseId)));
}

// A Zillow search deep-link built from the address. Nothing is fetched --
// this is the same as typing the address into Zillow's search box, and it is
// how you get to the photos and remarks the listing feed doesn't carry.
function zillowUrl(address) {
  if (!address) return "";
  const slug = String(address).replace(/,/g, " ").trim().split(/\s+/).join("-");
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
  if (/condo|townhouse|co-?op/.test(type) || foundBy.includes("condo")) return "condo";
  return "house";
}

function laneCounts() {
  const counts = Object.fromEntries(LANES.map(l => [l.id, 0]));
  houses.forEach(r => { counts[laneOf(r.fields || {})] += 1; });
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
  const fits = criteria.filter(r => (r.fields || {}).Active).map(r => {
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
  const best = fits.length
    ? fits.reduce((a, b) => (b.score > a.score || (b.score === a.score && b.met > a.met)) ? b : a)
    : null;
  return { fits, best };
}

function fitBlock(f) {
  const { fits, best } = fitSummary(f);
  if (!fits.length) return "";
  return `<div class="fits">${fits.map(fit => {
    const isBest = best && fit.name === best.name && fit.score > 0;
    const misses = fit.checks.filter(([, s]) => s === false).map(([l]) => l);
    const unknowns = fit.checks.filter(([, s]) => s === null).map(([l]) => l);
    const detail = [
      misses.length ? `misses: ${misses.join("; ")}` : "",
      unknowns.length ? `verify: ${unknowns.join("; ")}` : "",
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
function signalChips(raw) {
  const signals = String(raw || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!signals.length) return "";
  return `<div class="signals">${signals.map(s =>
    `<span class="signal">${esc(s)}</span>`).join("")}</div>`;
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
};
function sourceBadge(f) {
  const raw = String(f.Source || "").trim();
  // No Source means no adapter wrote this row -- somebody typed it in, so
  // nothing here has been checked against a feed. Worth saying plainly
  // rather than as a bare label nobody can interpret.
  if (!raw) return `<span class="src src-hand" title="Typed in by hand — no feed has verified these numbers">typed in by hand</span>`;
  return `<span class="src">found via ${esc(SOURCE_LABELS[raw] || raw)}</span>`;
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
    photo = `<a class="card-photo card-photo-link" href="${esc(link)}"
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
        </div>
        <div class="card-sub">
          ${money(f.Price)} · ${f.Beds ?? "?"}bd/${f.Baths ?? "?"}ba
          ${f.Sqft ? ` · ${Number(f.Sqft).toLocaleString()} sqft` : ""}
          ${f["Price Per Sqft"] ? ` · $${Number(f["Price Per Sqft"]).toLocaleString()}/sqft` : ""}
          ${f["Year Built"] ? ` · built ${f["Year Built"]}` : ""}
          ${f["Days on Market"] ? ` · ${f["Days on Market"]} days on market` : ""}
          ${f["Price Cut"] ? ` · cut ${f["Price Cut"]}%` : ""}
          ${f.Status ? ` · ${esc(f.Status)}` : ""}
          ${sourceBadge(f)}
        </div>
        ${priceChangeChip(f)}
        ${signalChips(f["Value Signals"])}
        ${fitBlock(f)}
        ${cardStats(f, v)}
        <div class="card-verdicts">
          Flip ${tier(v.flipTier)} &nbsp; BRRRR ${tier(v.brrrrTier)}
          ${v.bestStrategy ? `<span class="best-strategy">→ better as ${v.bestStrategy}</span>` : ""}
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
    if (id) await airtable("PATCH", TABLE_CRITERIA, { body: { records: [{ id, fields }], typecast: true } });
    else await airtable("POST", TABLE_CRITERIA, { body: { records: [{ fields }], typecast: true } });
    $("criteria-dialog").close();
    setStatus("Saved.");
    await refresh();
  } catch (err) { setStatus("Save failed: " + err.message, false); }
}

async function toggleCriteria(id) {
  const rec = criteria.find(r => r.id === id);
  if (!rec) return;
  try {
    await airtable("PATCH", TABLE_CRITERIA, {
      body: { records: [{ id, fields: { Active: !rec.fields.Active } }], typecast: true },
    });
    await refresh();
  } catch (err) { setStatus("Update failed: " + err.message, false); }
}

// ---- house detail ----
function openHouse(id) {
  const rec = houses.find(r => r.id === id);
  if (!rec) return;
  const f = rec.fields || {};
  const v = houseVerdict(f);
  $("house-id").value = id;
  $("house-dialog-title").textContent = f.Address || "House";
  $("house-detail").innerHTML = `
    <div class="card-sub">${money(f.Price)} · ${f.Beds ?? "?"}bd/${f.Baths ?? "?"}ba${f.Sqft ? ` · ${Number(f.Sqft).toLocaleString()} sqft` : ""}</div>
    <div class="verdict-block">
      <strong>Flip — ${v.flipTier}</strong>
      <ul>${v.flipReasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      <strong>BRRRR — ${v.brrrrTier}</strong>
      <ul>${v.brrrrReasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
    </div>
    ${listingLink(f) ? `<p><a href="${esc(listingLink(f))}" target="_blank" rel="noopener">Open on Zillow →</a></p>` : ""}`;
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
    await airtable("PATCH", TABLE_HOUSES, { body: { records: [{ id, fields }], typecast: true } });
    $("house-dialog").close();
    setStatus("Saved.");
    await refresh();
  } catch (err) { setStatus("Save failed: " + err.message, false); }
}

// ---- load / navigation ----
async function refresh() {
  if (!store.token || !store.baseId) { showSetup(true); return; }
  try {
    setStatus("Loading…");
    [criteria, houses] = await Promise.all([listAll(TABLE_CRITERIA), listAll(TABLE_HOUSES)]);
    const markets = [...new Set(houses.map(r => r.fields?.Market).filter(Boolean))];
    $("filter-market").innerHTML = '<option value="">All markets</option>' +
      markets.map(m => `<option>${esc(m)}</option>`).join("");
    // Rebuilt on every load so a strategy added in Airtable becomes a sort
    // option on the next refresh. The current choice survives the rebuild.
    const sortSel = $("sort-by");
    const keep = sortSel.value;
    sortSel.innerHTML = sortOptions().map(([v, label]) =>
      `<option value="${esc(v)}">${esc(label)}</option>`).join("");
    if ([...sortSel.options].some(o => o.value === keep)) sortSel.value = keep;
    renderCriteria();
    renderMatches();
    showSetup(false);
    setStatus("");
  } catch (err) {
    setStatus(err.message, false);
    showSetup(true);
  }
}

function showSetup(show) {
  $("setup").style.display = show ? "block" : "none";
  $("app").style.display = show ? "none" : "block";
}

function setScreen(next) {
  screen = next;
  $("screen-matches").style.display = next === "matches" ? "block" : "none";
  $("screen-criteria").style.display = next === "criteria" ? "block" : "none";
  $("nav-matches").classList.toggle("active", next === "matches");
  $("nav-criteria").classList.toggle("active", next === "criteria");
}

// The base id is buried in an Airtable URL and is the one piece of setup with
// no obvious home, so look it up from the token instead of asking for it.
// Needs schema.bases:read, which a data-only token won't have -- hence the
// fallback message rather than treating the failure as fatal.
async function findBases() {
  const token = $("setup-token").value.trim();
  const out = $("setup-bases");
  out.innerHTML = "";
  if (!token) { setStatus("Paste your token first.", false); return; }
  setStatus("Looking…");
  let bases;
  try {
    const res = await fetch("https://api.airtable.com/v0/meta/bases", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error(res.status === 403
      ? "That token can't list bases — add the schema.bases:read scope, or paste the app… id from your Airtable URL."
      : `Airtable said ${res.status}.`);
    bases = (await res.json()).bases || [];
  } catch (err) {
    setStatus(err.message, false);
    return;
  }
  if (!bases.length) {
    setStatus("This token has no bases yet. Create one in Airtable and grant the token access.", false);
    return;
  }
  setStatus(`Found ${bases.length} base${bases.length > 1 ? "es" : ""}. Pick one:`);
  out.innerHTML = bases.map(b =>
    `<button type="button" class="secondary base-pick" data-base-id="${esc(b.id)}">${esc(b.name)}</button>`
  ).join("");
  out.querySelectorAll("[data-base-id]").forEach(btn =>
    btn.addEventListener("click", () => {
      $("setup-base").value = btn.dataset.baseId;
      out.innerHTML = "";
      setStatus(`Using ${btn.textContent} — hit Connect.`);
    }));
}

document.addEventListener("DOMContentLoaded", () => {
  $("setup-token").value = store.token;
  $("setup-base").value = store.baseId;
  $("setup-save").addEventListener("click", async () => {
    store.token = $("setup-token").value.trim();
    store.baseId = $("setup-base").value.trim();
    await refresh();
  });

  $("setup-find").addEventListener("click", findBases);

  // The token lives only in this browser, and the person may have nowhere
  // else to retrieve it from -- Airtable shows a token once at creation and
  // never again. These buttons make this device the way to get it into the
  // repo's GitHub secrets without creating a new token.
  const copyOut = async (label, value) => {
    if (!value) { setStatus(`No ${label} saved on this device.`, false); return; }
    try {
      await navigator.clipboard.writeText(value);
      setStatus(`${label} copied — paste it into the GitHub secret.`);
    } catch {
      // Clipboard API can be blocked (http, permissions); prompt() still
      // gives a selectable string on every mobile browser.
      window.prompt(`Copy this ${label}:`, value);
    }
  };
  // The Maps key lives in this browser only. It is not an Airtable token --
  // it buys map images and nothing else -- so it does not belong in the repo
  // secrets alongside credentials that can read the database.
  const mapsInput = $("maps-key");
  if (mapsInput) mapsInput.value = localStorage.getItem("mapsKey") || "";
  $("save-maps-key")?.addEventListener("click", () => {
    const key = (mapsInput.value || "").trim();
    if (!key) return setStatus("Enter a key first.", false);
    localStorage.setItem("mapsKey", key);
    setStatus("Saved. Photos will appear on the Matches tab.");
    renderMatches();
  });
  $("clear-maps-key")?.addEventListener("click", () => {
    localStorage.removeItem("mapsKey");
    mapsInput.value = "";
    setStatus("Key removed; cards go back to no photo.");
    renderMatches();
  });

  $("copy-token").addEventListener("click", () => copyOut("token", store.token));
  $("copy-base").addEventListener("click", () => copyOut("base ID", store.baseId));
  $("disconnect").addEventListener("click", () => {
    if (!window.confirm("Disconnect this device? The saved token is removed from this browser — make sure it's saved somewhere (like the GitHub secret) first.")) return;
    store.token = "";
    store.baseId = "";
    showSetup(true);
  });

  $("nav-matches").addEventListener("click", () => setScreen("matches"));
  $("nav-criteria").addEventListener("click", () => setScreen("criteria"));
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

  $("filter-market").addEventListener("change", renderMatches);
  $("filter-qualified").addEventListener("change", renderMatches);
  $("sort-by").addEventListener("change", renderMatches);

  setScreen("matches");
  refresh();

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
});
