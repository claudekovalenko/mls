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
    throw new Error(detail?.error?.message || detail?.error?.type || `Airtable ${res.status}`);
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

function renderMatches() {
  const wrap = $("matches-list");
  const marketFilter = $("filter-market").value;
  const onlyQualified = $("filter-qualified").checked;

  let rows = houses.map(r => ({ id: r.id, f: r.fields || {}, v: houseVerdict(r.fields || {}) }));
  if (marketFilter) rows = rows.filter(r => (r.f.Market || "") === marketFilter);
  if (onlyQualified) rows = rows.filter(r => r.f.Qualified || r.v.bestRank >= 2);
  rows.sort((a, b) => (b.v.bestRank - a.v.bestRank)
    || ((b.v.metrics.flipProfit ?? -Infinity) - (a.v.metrics.flipProfit ?? -Infinity)));

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

function houseCard({ id, f, v }) {
  const photo = f["Photo URL"]
    ? `<img class="card-photo" src="${esc(f["Photo URL"])}" alt="" loading="lazy">`
    : `<div class="card-photo card-photo-empty"></div>`;
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
          ${f.Status ? ` · ${esc(f.Status)}` : ""}
        </div>
        <div class="card-stats">
          <div><span class="stat-num ${(v.metrics.flipProfit ?? 0) > 0 ? "pos" : "neg"}">${money(v.metrics.flipProfit)}</span><span class="stat-lbl">Flip profit</span></div>
          <div><span class="stat-num">${pct(v.metrics.cashOnCash)}</span><span class="stat-lbl">Cash-on-cash</span></div>
          <div><span class="stat-num">${pct(v.metrics.onePercentRatio)}</span><span class="stat-lbl">1% rule</span></div>
        </div>
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
  ["Property Types", "text", "Single Family, Townhouse"],
  ["Keywords", "text", "pool, basement"],
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
          ${esc(f.City || "")}${f.State ? ", " + esc(f.State) : ""} ·
          ${money(f["Min Price"])}–${money(f["Max Price"])} ·
          ${f["Min Beds"] ?? "?"}+ bd
        </div>
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
    ${f["Listing URL"] ? `<p><a href="${esc(f["Listing URL"])}" target="_blank" rel="noopener">View listing →</a></p>` : ""}`;
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

document.addEventListener("DOMContentLoaded", () => {
  $("setup-token").value = store.token;
  $("setup-base").value = store.baseId;
  $("setup-save").addEventListener("click", async () => {
    store.token = $("setup-token").value.trim();
    store.baseId = $("setup-base").value.trim();
    await refresh();
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
  $("filter-market").addEventListener("change", renderMatches);
  $("filter-qualified").addEventListener("change", renderMatches);

  setScreen("matches");
  refresh();

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
});
