"use strict";

// Per-rule tab metadata.
const RULES = {
  rule1: { n: 1, title: "Consolidate" },
  rule2: { n: 2, title: "Sort by delivery date" },
  rule3: { n: 3, title: "Smart priority (slack)" },
  rule4: { n: 4, title: "Setup time" },
  rule5: { n: 5, title: "Overlap mode" },
  rule6: { n: 6, title: "Machine schedule" },
  rule7: { n: 7, title: "Daily entry" },
  rule8: { n: 8, title: "Plan (book → remaining qty)" },
};
const ORDER = ["rule1","rule2","rule3","rule4","rule5","rule6","rule7","rule8"];
// Only two per-rule tabs are shown: Rule 6 (Allocate to machines — where the user
// downloads the schedule) and Rule 7 (Capture actuals — the feedback entry). Rules
// 1–5 and Rule 8 still run in the backend (the trace computes them; Rule 8 = the Plan
// over the order book, also shown on the Orders tab + triggered by the Plan button)
// but are HIDDEN from the UI. Add a key back here to re-show its tab. Frontend-only.
const VISIBLE_TABS = ["rule6", "rule7"];

let currentTrace = null;
let currentGantt = null;
let currentOrders = null;     // {columns, rows} from /run or /orders
let currentExpected = null;   // {"SO\x1fITEM": "YYYY-MM-DD"} from /run's expected_end (Task 9)
let optimizeMeta = null;      // /run's optimize_meta: applied optimization + staleness
let autoNote = null;          // /run's auto_note: scheduled optimization's latest note ({text, at} or null)
let optimizePollTimer = null; // /optimize/status polling handle
let ITEMS = null;
let ganttDayWidth = 200;   // px per day column (Gantt is day-level, no hour detail)
let currentRole = "user";   // set from /me; default to the least-privileged role
let currentConfig = null;   // /run's config (both roles) — feeds the status strip's plan basis
let planEverLoaded = false; // true once the first /run response (success or failure) lands —
                             // distinguishes "still loading" from a genuinely empty plan
let bootLoadingTimer = null;
let optimizePollFailures = 0;   // consecutive failed /optimize/status polls
let commitmentEnabled = false;  // from /me — are the Committed/Open lanes shown at all?

// ---- View router ----
// Six destinations, one visible at a time. Each maps to an existing render call
// (the old per-rule tab machinery, absorbed into a fixed nav). `mountFor` returns
// the per-view content div so the unchanged render functions write to the right spot.
const VIEWS = ["orders", "schedule", "gantt", "entry", "analytics", "settings"];
let activeView = "orders";

const $ = (id) => document.getElementById(id);
const setStatus = (m, isError = false) => {
  const el = $("status");
  el.textContent = m;
  el.title = m;   // full message on hover — the line itself is truncated for long text
  el.classList.toggle("status-error", !!isError);
};
const setDatasetStatus = (m, isError = false) => {
  const el = $("dataset-status");
  el.innerHTML = m;
  el.classList.toggle("status-error", !!isError);
};

// ---- Boot-loading banner (fixed in index.html; removed/updated once the first
// plan lands) — distinguishes "still loading" (a cold Render instance waking
// up) from "broken". See boot() below for the 5s "still loading" bump.
function bootLoadingSuccess() {
  const el = $("boot-loading");
  if (el) el.remove();
  if (bootLoadingTimer) { clearTimeout(bootLoadingTimer); bootLoadingTimer = null; }
}
function bootLoadingError(msg) {
  const el = $("boot-loading");
  if (el) { el.textContent = msg; el.classList.add("boot-error"); }
  if (bootLoadingTimer) { clearTimeout(bootLoadingTimer); bootLoadingTimer = null; }
}

function mountFor(key) {
  if (key === "orders") return $("orders-mount");
  if (key === "gantt") return $("gantt-mount");
  if (key === "analytics") return $("analytics-mount");
  if (key === "rule6" || key === "schedule") return $("schedule-mount");
  if (key === "rule7" || key === "entry") return $("entry-mount");
  return $("tab-content");
}

// Render the content for a view using the existing render functions.
function renderView(v) {
  if (v === "orders") renderOrders();
  else if (v === "schedule") renderTab("rule6");
  else if (v === "gantt") renderGantt();
  else if (v === "entry") renderTab("rule7");
  else if (v === "analytics") renderAnalytics();
  // "settings" is static markup (cards wired once at boot) — nothing to render.
}

function showView(v, push) {
  if (!VIEWS.includes(v)) v = "orders";
  // Every tab is open to every role (2026-08-09 role-parity fix). Analytics used
  // to be admin-only (owner rule, 2026-07-27) — nav link CSS-hidden AND a redirect
  // here — but a director compared the two logins and asked for them to match. It
  // is a read-only view of the plan both roles already hold, so nothing new is
  // exposed by showing it. Settings likewise shows Operators & shifts / Absences
  // read-only to the user role (the truly admin-only sections inside it — Plan
  // settings, the efficiency report — are their own admin-only cards).
  activeView = v;
  try { localStorage.setItem("anvitech-view", v); } catch (e) { /* private mode */ }
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === v));
  renderView(v);
  if (push && location.hash.replace(/^#/, "") !== v) location.hash = v;
}

function lastView() {
  let v;
  try { v = localStorage.getItem("anvitech-view"); } catch (e) { v = null; }
  return VIEWS.includes(v) ? v : "orders";
}

function onHashChange() {
  const h = location.hash.replace(/^#/, "");
  showView(VIEWS.includes(h) ? h : lastView());
}

function readConfig() {
  // GUARD — Overlap % is owned by Optimize (the sweep contest tunes it and Apply
  // persists the winner). There is no form field for it; its ONLY source is
  // currentConfig, populated by the first /run response. Until then there is NO
  // complete config to send — return null so no caller (present or future) can
  // POST a config that would silently reset the tuned overlap to a default.
  // There is deliberately no fallback value here.
  if (!currentConfig || currentConfig.overlap_percent == null) return null;
  const cfgObj = {
    setup_time_min: Number($("cfg-setup").value),
    overlap_mode: "overlap",   // sequential mode retired — always overlap
    // A settings Save must PRESERVE the optimizer-chosen overlap: echo back the
    // last-loaded config's value (never a form field, never a hardcoded default).
    overlap_percent: currentConfig.overlap_percent,
    apply_operator_logic: $("cfg-operator-logic").checked,
    // split_parallel and balance_operator_load left Settings on 2026-08-05: the LIVE
    // planner never reads either (only the retired classic rule6 does), so a tick box
    // that changes nothing only misleads. Echo the STORED values the same way
    // overlap_percent does above — reading the removed elements would throw and break
    // every Save, and hardcoding false would silently overwrite what is on file.
    split_parallel: currentConfig.split_parallel,
    // Expedite was removed from Settings (measured harmful, and always forced off
    // once optimized ranks are applied): always send 0.
    expedite_window_min: 0,
    balance_operator_load: currentConfig.balance_operator_load,
  };
  // Plan start date (the day scheduling begins). "Start from today" (auto) sends
  // null — the server resolves it to the real current date (IST) on every plan,
  // so a live shop never drifts to a stale date. Unchecked → send the picked ISO
  // date (omitted if blank so the saved/default date is kept). Daily punches still
  // advance the clock past it either way.
  const auto = $("cfg-start-auto");
  if (auto && auto.checked) {
    cfgObj.plan_start_date = null;
  } else {
    const ps = $("cfg-plan-start") && $("cfg-plan-start").value;
    if (ps) cfgObj.plan_start_date = ps;
  }
  return cfgObj;
}

// Today's date as ISO (browser local) — a FALLBACK for the disabled auto-mode
// display only; the authoritative start is the server-resolved IST date carried
// in the /run response (resolved_plan_start, cached below).
function todayIso() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
let lastResolvedStart = null;   // resolved_plan_start from the latest /run response

function updatePlanStartEcho() {
  const auto = $("cfg-start-auto"), inp = $("cfg-plan-start"), e = $("cfg-plan-start-echo");
  const isAuto = !!(auto && auto.checked);
  if (inp) {
    inp.disabled = isAuto;             // auto mode: the picker is display-only
    if (isAuto) inp.value = lastResolvedStart || todayIso();  // server IST today
  }
  const v = inp && inp.value;
  if (e) e.textContent = v ? "= " + isoToDdmmyyyy(v) : "";
}

// ---- Session / role ----
// Learn who we are. Default to the least-privileged role on any failure, and
// send the browser to the login page if the session is gone.
async function initSession() {
  try {
    const res = await fetch("/me");
    if (res.status === 401) { window.location = "/login"; return; }
    const me = await res.json();
    currentRole = me.role === "admin" ? "admin" : "user";
    // Commit/uncommit lanes are hidden for now (2026-08-04 — the directors don't
    // want them yet). The server owns the switch and reports it here, so the
    // buttons/columns and the endpoints can never disagree about whether the
    // feature exists. Flipping COMMITMENT_FEATURE_ENABLED in api/main.py brings
    // the whole thing back with no frontend change.
    commitmentEnabled = me.commitment_enabled === true;
    renderSessionInfo(me.username, currentRole);
  } catch (e) {
    currentRole = "user";
    commitmentEnabled = false;   // fail closed: never show a control we can't confirm
  }
  // Fail closed: drop the default "role-pending" gate (CSS hides .admin-only for
  // it too) and apply the real one — an admin control never flashes visible to
  // a user account while the role is still in flight.
  document.body.classList.remove("role-pending");
  document.body.classList.toggle("role-user", currentRole !== "admin");
}

function renderSessionInfo(username, role) {
  const el = $("session-info");
  if (!el) return;
  const label = role === "admin" ? "Admin" : "User";
  el.innerHTML = `Signed in as <strong>${escapeHtml(username || "")}</strong> · ${escapeHtml(label)} `
    + `<form method="post" action="/logout" class="logout-form"><button type="submit">Logout</button></form>`;
}

// Reflect the (server) SAVED plan config back into the admin's config bar so it
// shows the last-saved plan settings. `resolvedStart` (response key
// resolved_plan_start) = the ISO date the server's plan clock actually started
// from — shown in the disabled picker when auto ("start from today") is on.
function applyConfig(cfg, resolvedStart) {
  if (!cfg) return;
  const setVal = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = v; };
  const setSel = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = v; };
  // null plan_start_date = auto ("start from today"): tick the box, show the
  // server-resolved start (IST today; browser-local today as a fallback).
  const _auto = $("cfg-start-auto");
  const _isAuto = (cfg.plan_start_date === null || cfg.plan_start_date === undefined);
  if (_auto) _auto.checked = _isAuto;
  if (resolvedStart) lastResolvedStart = resolvedStart;
  setVal("cfg-plan-start", _isAuto ? (resolvedStart || todayIso()) : cfg.plan_start_date);
  updatePlanStartEcho();
  setVal("cfg-setup", cfg.setup_time_min);
  // Overlap % is read-only (Optimize owns it): reflect the saved value into the
  // info line rather than an editable input.
  const ov = $("cfg-overlap-info");
  if (ov && cfg.overlap_percent != null) ov.textContent = cfg.overlap_percent;
  const ms = $("cfg-machineset-info");
  if (ms) ms.textContent = cfg.flexible_machines ? "Allotted + Suggested" : "Allotted only";
  const ol = $("cfg-operator-logic");
  if (ol) ol.checked = !!cfg.apply_operator_logic;
  // No cfg-split-parallel / cfg-balance-ops elements any more (removed 2026-08-05,
  // see readConfig) — their stored values ride along untouched, nothing to paint.
}

// ---- Plan (Run + Rerun unified) ----
// persist=true only on the admin's explicit Plan click (saves the config so every
// login sees the same plan). Auto-load and the user role pass persist=false.
async function runPlan(persist = false) {
  // readConfig() is null until boot's first /run has populated currentConfig
  // (the only source of the optimizer-tuned overlap). A save in that window
  // would persist an incomplete config — block it; the admin just retries.
  const cfg = readConfig();
  if (persist && cfg === null) {
    setStatus("Plan is still loading, try again in a moment.", true);
    return;
  }
  const runBtn = $("run-btn");
  const runBtnLabel = runBtn ? runBtn.textContent : null;
  if (runBtn) { runBtn.disabled = true; runBtn.textContent = "Saving & re-planning…"; }
  setStatus("Planning…");
  try {
    // With no complete config yet (pre-boot refresh), send none: the server
    // plans with its saved config (or defaults) and nothing is persisted.
    const body = { persist: !!persist };
    if (cfg !== null) body.config = cfg;
    const res = await fetch("/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 401) { window.location = "/login"; return; }
    if (!res.ok) {
      setStatus("Error: " + (await res.text()), true);
      bootLoadingError("Could not reach the server. Check your connection and reload the page.");
      return;
    }
    const data = await res.json();
    currentTrace = data.trace;
    currentGantt = data.gantt || null;
    currentOrders = data.orders || null;
    currentExpected = data.expected_end || null;
    optimizeMeta = data.optimize_meta || null;
    autoNote = data.auto_note || null;
    currentConfig = data.config || null;
    if (data.resolved_plan_start) lastResolvedStart = data.resolved_plan_start;
    if (currentRole === "admin" && data.config) applyConfig(data.config, data.resolved_plan_start);
    renderReport(data.report);
    renderOptimizeBanner();
    renderAutoNote();
    renderStatusStrip();
    renderView(activeView);
    setStatus("Plan " + data.run_id + " complete.");
    bootLoadingSuccess();
  } catch (e) {
    setStatus("Request failed: " + e.message, true);
    bootLoadingError("Could not reach the server. Check your connection and reload the page.");
  } finally {
    planEverLoaded = true;
    if (runBtn) { runBtn.disabled = false; runBtn.textContent = runBtnLabel; }
  }
}

// ---- Upload & merge into the order book ----
async function uploadExcel() {
  const f = $("xlsx-file").files[0];
  if (!f) { setDatasetStatus("Choose an Excel file (.xlsx) first.", true); return; }
  if (!window.confirm(
    `Add "${f.name}" to the order list?\n\nNew and changed orders are added right away. `
    + `You cannot undo this in one click, you would have to delete each order by hand `
    + `afterwards. Continue?`)) {
    return;
  }
  const fd = new FormData(); fd.append("file", f);
  setDatasetStatus("Uploading…");
  const btn = $("upload-btn");
  const btnLabel = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = "Uploading…"; }
  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    if (!res.ok) { setDatasetStatus("Upload failed: " + (await res.text()), true); return; }
    const d = await res.json();
    ITEMS = null;  // item metadata may have changed
    let msg = `<strong>${escapeHtml(d.name)}</strong>: ${d.added} new order(s) added`;
    if (d.updated) msg += ` · ${d.updated} delivery date(s) updated`;
    if (d.flagged && d.flagged.length) {
      // Delivery-date updates are the ones a director is most likely to be
      // checking for — surface them first so they aren't buried among routine
      // "duplicate: already in the book" flags. Nothing is dropped, only reordered.
      const isDateUpdate = (f) => (f.reason || "").includes("delivery date updated");
      const ordered = d.flagged.filter(isDateUpdate).concat(d.flagged.filter((f) => !isDateUpdate(f)));
      const detail = ordered.map((f) => `${escapeHtml(f.so_no)}/${escapeHtml(f.item_code || "")} (${escapeHtml(f.reason)})`).join("; ");
      msg += ` · <span class="pill-pending">${d.flagged.length} flagged</span>: ${detail}`;
    }
    if (d.masters_updated) msg += " · uploaded machine/operator/item data updated";
    setDatasetStatus(msg);
    // The upload's OWN (loader-scoped) report drives the always-visible
    // missing-routings note — it still shows any item codes the file dropped
    // for having no routing, which the runPlan() call below never can (its
    // /run report is book-scoped and those codes never reached the book).
    renderMissingRoutings(d.report);
    await runPlan();           // refresh the book + schedule
    msg += ' · Schedule is ready. See the <a href="#schedule">Machine schedule</a>'
      + ' or <a href="#gantt">the Gantt</a>.';
    setDatasetStatus(msg);
    showView("orders", true);
  } catch (e) { setDatasetStatus("Upload error: " + e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = btnLabel; } }
}

// ---- Optimize (admin: search for a better job sequence; banner for everyone) ----
function fmtMetrics(m) {
  if (!m) return "-";
  return `${m.makespan_days} days · ${m.late_orders}/${m.orders} late · ${m.total_late_days} late-days`;
}

function isoToDisplayDateTime(iso) {       // "2026-07-13T10:04:00" -> "13-07-2026 10:04"
  const s = String(iso || "");
  const [d, t] = s.split("T");
  return d ? isoToDdmmyyyy(d) + (t ? " " + t.slice(0, 5) : "") : s;
}

// Shared wording for the "delivery dates moved since the applied optimization"
// warning — rendered in both the Optimize panel (next to the Start deep search
// button) and the status strip, so the two can never drift.
function datesChangedWarningText(meta) {
  if (!meta || !meta.dates_changed) return null;
  const n = meta.dates_changed_count || 0;
  return `${n} order${n === 1 ? "" : "s"} ${n === 1 ? "has" : "have"} a delivery `
    + "date that changed since the last search. The job order does not know about it "
    + "reflects the change. Run Start deep search.";
}

function renderOptimizeBanner() {
  const b = $("optimize-banner");
  if (!b) return;
  if (!optimizeMeta || !optimizeMeta.active) { b.classList.add("hidden"); b.innerHTML = ""; return; }
  b.classList.remove("hidden");
  let h = `Using a saved better job order (found ${escapeHtml(isoToDisplayDateTime(optimizeMeta.saved_at))})`;
  if (optimizeMeta.uncovered > 0) {
    h += ` · <span class="pill-pending">${optimizeMeta.uncovered} new order(s) came in since. ` +
         `run Optimize again for the best plan</span>`;
  }
  if (optimizeMeta.inputs_changed) {
    h += ` · <span class="pill-pending">Your Settings or your uploaded machine/operator/item ` +
         `data changed since this search ran. The numbers shown may be old. Press ` +
         `Optimize again to refresh them</span>`;
  }
  const datesWarn = datesChangedWarningText(optimizeMeta);
  if (datesWarn) {
    h += ` · <span class="pill-pending">${datesWarn}</span>`;
  }
  // The live plan is worse than the number the Optimize panel showed (a change the
  // inputs check can't see — e.g. a code update or a cloud/local gap). Say so plainly.
  if (optimizeMeta.diverged && optimizeMeta.target && optimizeMeta.achieved) {
    const t = optimizeMeta.target, a = optimizeMeta.achieved;
    const bits = [];
    if (a.makespan_days != null && t.makespan_days != null && a.makespan_days - t.makespan_days >= 2)
      bits.push(`${Math.round(a.makespan_days)} days now vs ${Math.round(t.makespan_days)} targeted`);
    if (a.total_late_days != null && t.total_late_days != null && a.total_late_days - t.total_late_days >= 5)
      bits.push(`${Math.round(a.total_late_days)} late-days now vs ${Math.round(t.total_late_days)} targeted`);
    h += ` · <span class="pill-pending">This plan has drifted from the search it was based on` +
         (bits.length ? ` (${bits.join("; ")})` : "") + `. Press Start deep search again.</span>`;
  }
  if (currentRole === "admin") {
    h += ` <button id="optimize-clear-btn" class="ghost-btn small">Go back to standard plan</button>`;
  }
  b.innerHTML = h;
  const clr = $("optimize-clear-btn");
  if (clr) clr.onclick = async () => {
    if (!window.confirm("Go back to the standard job order? This undoes the search result you applied.")) return;
    const res = await fetch("/optimize/clear", { method: "POST" });
    if (res.ok) { setStatus("Back to the standard plan."); await runPlan(false); }
    else setStatus("Could not undo this: " + (await res.text()), true);
  };
}

// Self-tuning contest's latest note (auto-triggered runs only) — informational,
// no control.
function renderAutoNote() {
  const el = $("auto-note");
  if (!el) return;
  const n = autoNote;
  if (n && n.text) { el.classList.remove("hidden"); el.textContent = n.text; }
  else { el.classList.add("hidden"); el.textContent = ""; }
}

// ---- Status strip (both roles) ----
// One calm line, entirely from data already fetched: order/late counts, the plan
// basis, next shift rotation (admin only), the latest auto-note (collapsible), and
// a colored warning chip for staleness / unstaffed hrs.
function countLateOrders() {
  if (!currentOrders || !currentOrders.rows || !currentExpected) return 0;
  const cols = currentOrders.columns;
  const soI = cols.indexOf("SO No"), itI = cols.indexOf("Item Code");
  const ddI = cols.indexOf("SO Delivery Date"), stI = cols.indexOf("Status");
  if (soI < 0 || ddI < 0) return 0;
  let late = 0;
  currentOrders.rows.forEach((r) => {
    if (stI >= 0 && String(r[stI]) === "Complete") return;   // archived — not "late"
    const key = String(r[soI]) + "\x1f" + String(itI >= 0 ? r[itI] : "");
    const exp = isoToDate(currentExpected[key]);
    const due = ddmmyyyyToDate(r[ddI]);
    if (exp && due && exp > due) late++;
  });
  return late;
}


function renderStatusStrip() {
  const el = $("status-strip");
  if (!el) return;
  const nOrders = currentOrders && currentOrders.rows ? currentOrders.rows.length : 0;
  const nLate = countLateOrders();

  const segs = [];
  segs.push(`<span class="ss-seg"><span class="ss-dot"></span>${nOrders} order${nOrders === 1 ? "" : "s"}</span>`);
  segs.push(`<span class="ss-seg${nLate ? " ss-late" : ""}">${nLate} late</span>`);

  // Plan basis: null start date = "follows today" (server-resolved); else a fixed date.
  const isAuto = currentConfig ? (currentConfig.plan_start_date === null || currentConfig.plan_start_date === undefined) : true;
  const startDisp = isoToDdmmyyyy(lastResolvedStart || todayIso());
  segs.push(isAuto
    ? `<span class="ss-seg">Plan follows today (${escapeHtml(startDisp)})</span>`
    : `<span class="ss-seg">Plan starts ${escapeHtml(isoToDdmmyyyy(currentConfig.plan_start_date))}</span>`);

  // Latest auto-note — collapsible.
  if (autoNote && autoNote.text) {
    segs.push(`<button type="button" class="ss-note" id="ss-note-toggle">▸ note</button>`
      + `<span class="ss-note-text hidden" id="ss-note-text">${escapeHtml(autoNote.text)}</span>`);
  }

  // Warning chip: staleness and/or unstaffed hours (both from the /run response).
  const warns = [];
  if (optimizeMeta && optimizeMeta.inputs_changed) {
    warns.push("Your settings or your uploaded machine, operator or item data changed since the last search. The numbers shown may be old. Press Start deep search again.");
  }
  const datesChangedWarn = datesChangedWarningText(optimizeMeta);
  if (datesChangedWarn) warns.push(datesChangedWarn);
  const unstaffed = currentTrace && currentTrace.analytics && currentTrace.analytics.headline
    ? currentTrace.analytics.headline.unstaffed_hrs : 0;
  if (unstaffed && unstaffed > 0) {
    warns.push(`${Math.round(unstaffed)} hrs of scheduled work fall in shifts with no free qualified operator. Those shifts need more crew.`);
  }
  let chip = "";
  if (warns.length) {
    chip = `<button type="button" class="ss-chip" id="ss-chip">⚠ ${warns.length} warning${warns.length === 1 ? "" : "s"}</button>`
      + `<div class="ss-chip-detail hidden" id="ss-chip-detail"><ul>${warns.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>`;
  }

  el.innerHTML = `<div class="ss-line">${segs.join("")}${chip}</div>`;

  const nt = $("ss-note-toggle");
  if (nt) nt.onclick = () => {
    const t = $("ss-note-text");
    if (t) { const open = t.classList.toggle("hidden") === false; nt.textContent = (open ? "▾ " : "▸ ") + "note"; }
  };
  const cb = $("ss-chip");
  if (cb) cb.onclick = () => { const d = $("ss-chip-detail"); if (d) d.classList.toggle("hidden"); };
}

function optimizeProgressLine(st) {
  const where = st.mode === "cloud" ? " in the cloud" : "";
  // Before the first plan lands (cloud runner still spinning up ~1-2 min), say so plainly
  // rather than showing a frozen "tried 0".
  if (st.mode === "cloud" && (!st.evals || st.evals === 0)) {
    return "starting on GitHub Actions (spinning up the runner, ~1-2 min)…";
  }
  // A TIME-BOXED search has no plan budget to count against. The CP engine runs
  // ONE solve and its `evals` counts IMPROVED SOLUTIONS, not plans tried, so the
  // server sends budget_evals = 0 and a fraction would be a fiction — "tried 3 of
  // 0 plans", a bar that can never fill. Every other engine sends a real budget
  // and reads exactly as before.
  const tried = st.budget_evals
    ? `tried ${st.evals} of ${st.budget_evals} plans${where}`
    : `searching${where}… ${st.evals} better plan${st.evals === 1 ? "" : "s"} found so far`;
  let best = "";
  if (st.best && st.best.makespan_days !== undefined) best = `, best so far: ${fmtMetrics(st.best)}`;
  else if (st.best && typeof st.best.score === "number") best = `, best score so far: ${st.best.score}`;
  return tried + best;
}

// Poll the server for progress. CRUCIAL: this must survive a failed poll — Render's
// free tier drops the odd request, and if we stopped polling on the first blip the
// counter would freeze while the search kept running underneath. So on ANY failure we
// simply schedule another attempt (a slightly longer gap) instead of giving up.
function _schedulePoll(ms) {
  clearTimeout(optimizePollTimer);
  optimizePollTimer = setTimeout(pollOptimizeStatus, ms);
}

function _showRunningControls(running) {
  const startBtn = $("optimize-start"), stopBtn = $("optimize-stop");
  if (startBtn) startBtn.disabled = running;
  if (stopBtn) stopBtn.classList.toggle("hidden", !running);
}

// After 3 consecutive failed polls (~15s), say so on the progress line instead
// of silently freezing — the search itself may still be running server-side.
function _pollFailure() {
  optimizePollFailures++;
  if (optimizePollFailures >= 3) {
    const prog = $("optimize-progress");
    if (prog && !/connection hiccup/.test(prog.textContent)) {
      prog.textContent += " (connection is slow, still trying. The search is still running.)";
    }
  }
  _schedulePoll(5000);
}

async function pollOptimizeStatus() {
  let st;
  try {
    const res = await fetch("/optimize/status");
    if (!res.ok) { _pollFailure(); return; }   // transient — try again, don't die
    st = await res.json();
  } catch (e) {
    _pollFailure();                                   // network blip — try again, don't die
    return;
  }
  optimizePollFailures = 0;
  const prog = $("optimize-progress");
  if (st.state === "running") {
    _showRunningControls(true);
    const tail = st.stopping ? ", stopping…" : "…";
    if (prog) prog.textContent = optimizeProgressLine(st) + tail;
    _schedulePoll(3000);
    return;
  }
  clearTimeout(optimizePollTimer); optimizePollTimer = null;
  _showRunningControls(false);
  if (st.state === "failed") {
    if (prog) prog.textContent = "Optimization failed: " + (st.error || "unknown error");
    return;
  }
  if (st.state === "done") {
    const how = st.cancelled ? "stopped early" : "done";
    if (prog) prog.textContent = `${how}: ${st.evals} plans tried in ${st.elapsed_s}s`;
    renderOptimizeResult(st);
  }
}

function renderOptimizeResult(st) {
  const box = $("optimize-result");
  if (!box) return;
  if (st.auto) {
    // An auto-triggered contest already applied itself (or found nothing better) —
    // never show a stale Apply/Discard panel for a decision that's already made.
    box.classList.remove("hidden");
    box.innerHTML = "<p>Auto-optimize finished. The plan updated itself if a better one was found.</p>";
    runPlan(false);
    return;
  }
  box.classList.remove("hidden");
  const rows = [
    ["Total days to finish", st.baseline ? st.baseline.makespan_days : "-",
     st.best ? st.best.makespan_days : "-"],
    ["Late orders", st.baseline ? `${st.baseline.late_orders} / ${st.baseline.orders}` : "-",
     st.best ? `${st.best.late_orders} / ${st.best.orders}` : "-"],
    ["Total days late, added up across all orders", st.baseline ? st.baseline.total_late_days : "-",
     st.best ? st.best.total_late_days : "-"],
    ["The single most late order (days)", st.baseline ? st.baseline.max_late_days : "-",
     st.best ? st.best.max_late_days : "-"],
  ];
  // Lateness distribution — what actually matters (how MANY orders, and how far late),
  // not just the raw count. A good plan reads as good here even if "late orders" looks high.
  const band = (label, key) => [label,
    st.baseline && st.baseline.bands ? st.baseline.bands[key] : "-",
    st.best && st.best.bands ? st.best.bands[key] : "-"];
  if (st.best && st.best.bands) {
    rows.push(
      band("• On-time orders", "on_time"),
      band("• Late 1–4 days (fine)", "d1_4"),
      band("• Late 5–9 days", "d5_9"),
      band("• Late 10–19 days", "d10_19"),
      band("• Late 20+ days (very bad)", "d20_plus"),
    );
  }
  const stoppedNote = st.cancelled ? " (stopped early: this is the best of the plans tried so far)" : "";
  let h = st.improved
    ? `<p><strong>A better job order was found${stoppedNote}.</strong> Press Apply below to start using it. Nothing changes until you press Apply.</p>`
    : `<p><strong>No better job order found.</strong> Your current plan is already the best one tried.</p>`;
  // Settings sweep: Optimize also tuned the mode's setting (overlap % under the
  // classic scheduler, transfer chunks under the flow scheduler).
  if (st.best_overlap != null && st.current_overlap != null) {
    const isChunks = st.knob === "flow_chunks";
    const name = isChunks ? "Batch flow (chunks)" : "Overlap";
    const unit = isChunks ? "" : "%";
    const nameLabel = isChunks ? name : `${name} (how early the next step can start)`;
    h += st.best_overlap !== st.current_overlap
      ? `<p>Best setting found: <strong>${nameLabel} ${st.best_overlap}${unit}</strong> (currently ${st.current_overlap}${unit}). Applying this plan also updates it automatically.</p>`
      : `<p>Your ${nameLabel} setting (${st.current_overlap}${unit}) was also tested against the alternatives. It is already the best.</p>`;
  }
  h += '<div class="table-wrap"><table><thead><tr><th></th><th>Standard plan</th><th>Optimized</th></tr></thead><tbody>';
  rows.forEach((r) => {
    h += `<tr><td>${escapeHtml(String(r[0]))}</td><td>${escapeHtml(String(r[1]))}</td><td><strong>${escapeHtml(String(r[2]))}</strong></td></tr>`;
  });
  h += "</tbody></table></div>";
  // The orders that are physically impossible for the current crew (20+ days late) — the
  // ones to outsource, add a shift for, or renegotiate. The engine surfaces them instead
  // of silently juggling them; no software setting can make them on-time.
  const worst = (st.best && st.best.worst_orders) || [];
  if (worst.length) {
    h += `<div class="optimize-impossible"><p><strong>${worst.length} order${worst.length === 1 ? " is" : "s are"} 20+ days late, no matter how the jobs are ordered.</strong> There is not enough crew capacity for this much work. You will need to send work outside, add a shift, or ask the customer for a new date:</p><ul>`;
    worst.forEach((w) => {
      h += `<li>${escapeHtml(String(w.so))}-${escapeHtml(String(w.item))}: <strong>${w.days} days late</strong></li>`;
    });
    h += "</ul></div>";
  }
  // Apply / Discard are built here rather than in index.html, so the CSS
  // .admin-only rule can never reach them — the role gate has to be in JS
  // (2026-08-09 role-parity fix). The user role sees the SAME result table above
  // and is simply told whose decision it is; the server 403s both endpoints too.
  if (currentRole === "admin") {
    h += `<div class="optimize-actions">
            <button id="optimize-apply-btn" class="primary">Apply this job order</button>
            <button id="optimize-discard-btn" class="ghost-btn">Discard</button>
          </div>`;
  } else {
    h += `<p class="muted">Your admin decides whether to use this job order.</p>`;
  }
  box.innerHTML = h;
  if (currentRole !== "admin") return;   // nothing below to wire for the user role
  $("optimize-apply-btn").onclick = async () => {
    const applyBtn = $("optimize-apply-btn");
    const discardBtn = $("optimize-discard-btn");
    // Immediate feedback: apply + the follow-up re-plan take a few seconds, so make the
    // click obviously registered (disable both, show "Applying…") — matches Save/Stop.
    const _label = applyBtn.textContent;
    applyBtn.disabled = true;
    if (discardBtn) discardBtn.disabled = true;
    applyBtn.textContent = "Applying…";
    setStatus("Applying this plan…");
    try {
      const res = await fetch("/optimize/apply", { method: "POST" });
      if (!res.ok) {
        setStatus("Apply failed: " + (await res.text()), true);
        applyBtn.disabled = false; applyBtn.textContent = _label;
        if (discardBtn) discardBtn.disabled = false;
        return;
      }
      box.classList.add("hidden"); box.innerHTML = "";
      setStatus("✓ Applied. Rebuilding your plan…");
      await runPlan(false);
    } catch (e) {
      setStatus("Apply error: " + e.message, true);
      applyBtn.disabled = false; applyBtn.textContent = _label;
      if (discardBtn) discardBtn.disabled = false;
    }
  };
  $("optimize-discard-btn").onclick = () => { box.classList.add("hidden"); box.innerHTML = ""; };
}

const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// "Done entering — update plan": fire a feedback-driven re-optimization, then
// block on live progress until it lands and refresh the whole plan to the winner.
// The contest auto-applies server-side if it's strictly better (both roles).
async function doneOptimize() {
  // Two-step guard: a stray tap on "Done entering: update plan" must not kick off a
  // re-optimize on half-entered feedback. Ask first (same confirm style as Mark
  // complete / Rollback); Cancel does nothing.
  if (!window.confirm(
    "Have you finished entering ALL of today's production updates?\n\n" +
    "This rebuilds the schedule using everything entered so far, and can take 15 to " +
    "30 minutes. If you still have more to enter, click Cancel and finish first."
  )) return;
  const st = $("optimize-done-status");
  const doneBtn = $("optimize-done");
  if (doneBtn) doneBtn.disabled = true;
  if (st) st.textContent = "Starting optimization…";
  let started = false;
  let state = null;
  try {
    const res = await fetch("/optimize/done", { method: "POST" });
    if (!res.ok) {
      if (st) st.textContent = "Could not start optimization: " + (await res.text());
      if (doneBtn) doneBtn.disabled = false;
      return;
    }
    const body = await res.json();
    started = body.started;
    state = body.state;
  } catch (e) {
    if (st) st.textContent = "Could not start optimization: " + e.message;
    if (doneBtn) doneBtn.disabled = false;
    return;
  }
  if (!started && state === "running") {
    // A contest is already in flight (e.g. another click/tab) — attach to it
    // instead of reporting a no-op "nothing to re-optimize".
    await pollDoneOptimize(st);
    if (doneBtn) doneBtn.disabled = false;
    return;
  }
  if (!started) {
    // Nothing changed since the last run (or auto disabled) — just refresh facts.
    await runPlan(false);
    if (st) st.textContent = "Plan updated. Nothing new to re-plan since last time.";
    if (doneBtn) doneBtn.disabled = false;
    return;
  }
  await pollDoneOptimize(st);
  if (doneBtn) doneBtn.disabled = false;
}

// Poll the shared contest to completion, showing progress next to the Done
// button. The contest auto-applies itself; on completion we refresh everything.
async function pollDoneOptimize(st) {
  for (;;) {
    let status;
    try {
      const r = await fetch("/optimize/status");
      if (!r.ok) { await _sleep(3000); continue; }
      status = await r.json();
    } catch (e) { await _sleep(3000); continue; }
    if (status.state === "running") {
      if (st) st.textContent = "Optimizing… " + optimizeProgressLine(status)
        + " (this can take several minutes)";
      await _sleep(3000);
      continue;
    }
    await runPlan(false);   // pick up the auto-applied winner + new facts
    if (st) {
      st.textContent = status.state === "failed"
        ? "Optimization could not finish: " + (status.error || "unknown error")
          + ". Plan updated with the latest feedback."
        : "Plan re-optimized and updated.";
    }
    return;
  }
}

async function startOptimize() {
  const budget = "deep";   // one option (owner decision): ~1,800 plans in the cloud (~200 local)
  const prog = $("optimize-progress");
  const box = $("optimize-result");
  if (box) { box.classList.add("hidden"); box.innerHTML = ""; }
  _showRunningControls(true);
  if (prog) prog.textContent = "starting…";
  try {
    const res = await fetch("/optimize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ budget }),
    });
    if (!res.ok) {
      if (prog) prog.textContent = "Could not start: " + (await res.text());
      _showRunningControls(false);
      return;
    }
    pollOptimizeStatus();
  } catch (e) {
    if (prog) prog.textContent = "Request failed: " + e.message;
    _showRunningControls(false);
  }
}

async function stopOptimize() {
  const stopBtn = $("optimize-stop");
  if (stopBtn) stopBtn.disabled = true;
  try {
    await fetch("/optimize/cancel", { method: "POST" });   // keeps best-so-far
  } catch (e) { /* the poll will still pick up the done state */ }
  if (stopBtn) stopBtn.disabled = false;
  pollOptimizeStatus();   // reflect "stopping…" / result promptly
}

// ---- Loader report (collapsible) ----
const REPORT_LABELS = {
  PENDING_MASTER_DATA: "machines not yet in your Machine master (used anyway)",
  NO_ROUTING: "orders without routing",
  TIME_COERCION: "cycle times auto-converted to minutes",
  BAD_DELIVERY_DATE: "bad delivery dates", MISSING_SHEET: "missing sheets",
  BAD_QTY: "unreadable SO quantities", DUPLICATE_PROCESS: "repeated process names in a routing",
};
// The data-gaps card wraps BOTH warning panels and lives outside the admin-only
// "Add orders" card, so the user role sees warnings about its own data too
// (2026-08-09 role-parity fix). Show the card only while one of them has content,
// or every plan would render an empty box on the Orders tab.
function syncDataGapsCard() {
  const card = $("data-gaps-card");
  if (!card) return;
  const shown = ["report-panel", "report-noroute"]
    .some((id) => { const el = $(id); return el && !el.classList.contains("hidden"); });
  card.classList.toggle("hidden", !shown);
}

function renderReport(report) {
  const panel = $("report-panel"), toggle = $("report-toggle"), detail = $("report-detail");
  if (!report || !report.rows || report.rows.length === 0) {
    panel.classList.add("hidden"); syncDataGapsCard(); return;
  }
  panel.classList.remove("hidden");
  syncDataGapsCard();
  const counts = {};
  report.rows.forEach((r) => { counts[r[0]] = (counts[r[0]] || 0) + 1; });
  const parts = Object.entries(counts).map(([k, v]) => `${v} ${REPORT_LABELS[k] || k}`).join(", ");
  toggle.innerHTML = `⚠ ${parts} (data gaps): click to view <span class="chev">▸</span>`;
  detail.innerHTML = tableHtml(report, true);
  detail.classList.add("hidden"); toggle.classList.remove("open");
  toggle.onclick = () => { const open = !detail.classList.toggle("hidden"); toggle.classList.toggle("open", open); };
}

// Always-visible missing-routings note (no click needed): the item codes the
// uploaded file couldn't schedule because the Item's process Master has no
// recipe for them. Deliberately called ONLY from uploadExcel with the
// upload's OWN (loader-scoped) report — never from runPlan()'s book-scoped
// /run report, which can never see these codes (they're dropped before they
// ever reach the order book) and would otherwise wipe this note the instant
// the automatic post-upload Plan runs.
function renderMissingRoutings(report) {
  const noroute = $("report-noroute");
  const rows = (report && report.rows) || [];
  const codes = [...new Set(rows.filter((r) => r[0] === "NO_ROUTING").map((r) => r[1]))];
  if (!codes.length) {
    noroute.classList.add("hidden"); noroute.textContent = ""; syncDataGapsCard(); return;
  }
  noroute.textContent = "Missing routings. Add these item codes to the Item's "
    + "process Master so they can be scheduled: " + codes.join(", ") + ".";
  noroute.classList.remove("hidden");
  syncDataGapsCard();
}

// ---- Per-view content (rule6 = Schedule, rule7 = Daily Entry) ----
// The fixed nav (renderView/showView) drives which view is visible; this renders
// the dynamic content for the rule-backed views into their per-view mount.
function renderTab(key) {
  if (key === "orders") { renderOrders(); return; }
  if (key === "gantt") { renderGantt(); return; }
  if (key === "analytics") { renderAnalytics(); return; }
  const entry = currentTrace ? currentTrace[key] : null;
  const meta = RULES[key];
  const root = mountFor(key);
  if (!entry) {
    root.innerHTML = planEverLoaded
      ? '<p class="placeholder">No plan yet. Upload a sales-order Excel on the Orders tab to get started.</p>'
      : '<p class="placeholder">Loading your plan…</p>';
    return;
  }

  // Machine schedule: don't fall through to an empty table shell — guide the
  // owner back to Orders, same pattern already used by Gantt/Analytics.
  if (key === "rule6" && (!entry.output || !entry.output.rows || !entry.output.rows.length)) {
    root.innerHTML = '<p class="placeholder">No schedule yet. Add orders on the Orders tab first.</p>';
    return;
  }

  const noActiveOrders = !currentOrders || !currentOrders.rows || !currentOrders.rows.length;

  // "Machine schedule" and "Daily entry" already carry a heading on their card
  // (index.html) — don't repeat it here with an internal "Rule N:" label.
  let html = (key === "rule6" || key === "rule7")
    ? ""
    : `<div class="rule-header"><h2>Rule ${meta.n}: ${meta.title}</h2></div>`;
  if (entry.reached === false) {
    html += '<div class="not-reached-box">This step didn’t run because an earlier step hit a '
      + 'problem. Check the warning above, fix the data, then Save &amp; re-plan.</div>';
    root.innerHTML = html; return;
  }
  if (entry.error) {
    html += `<div class="error-box"><strong>Planning stopped</strong> on order `
      + `<code>${escapeHtml(String(entry.error.record_id))}</code>: ${escapeHtml(entry.error.message)}. `
      + `Fix this order's data, then Save &amp; re-plan.</div>`;
  }
  if (key === "rule7") {
    html += noActiveOrders
      ? '<p class="placeholder">No orders to enter yet. Upload the order Excel on the '
        + '<strong>Orders</strong> tab first, then come back here.</p>'
      : actualsFormHtml();
  }

  // Task 3: let operators download the machine schedule to print and follow.
  if (key === "rule6" && entry.output && entry.output.rows && entry.output.rows.length) {
    html += '<div class="dl-toolbar">'
      + '<button id="dl-schedule" class="primary" title="Every operation, one row each">⬇ Download schedule (CSV)</button>'
      + '<button id="dl-machine" title="Same schedule, grouped by machine. Post one at each machine.">⬇ Download machine-wise view</button>'
      + '<button id="dl-shiftwise" title="Grouped by shift. Hand this to the shift supervisors.">⬇ Download shift-wise schedule</button>'
      + '<button id="dl-delay" title="Per-order justification: why each order is delayed and by how much">⬇ Download delay justification</button>'
      + '<span class="muted"> (opens in Excel; print it for the floor)</span></div>';
  }

  html += '<div class="io">';
  html += `<div class="panel"><h3>Input</h3>${tableHtml(entry.input)}</div>`;
  if (key === "rule7") {
    html += `<div class="panel"><h3>Saved entries</h3>${actualsOutputHtml(entry.output, entry.actuals_ids)}</div>`;
  } else {
    html += `<div class="panel"><h3>Output</h3>${tableHtml(entry.output)}</div>`;
  }
  html += "</div>";
  if (entry.tables && entry.tables.length) {
    entry.tables.forEach((t) => {
      html += `<div class="panel extra-table"><h3>${escapeHtml(t.title)}</h3>${tableHtml(t.table)}</div>`;
    });
  }
  if (entry.notes && entry.notes.length) {
    html += '<div class="notes"><h3>Notes</h3><ul>';
    entry.notes.forEach((n) => (html += `<li>${escapeHtml(n)}</li>`));
    html += "</ul></div>";
  }
  root.innerHTML = html;
  if (key === "rule7") { if (!noActiveOrders) wireActualsForm(); wireRollback(); }
  if (key === "rule6") {
    const sched = $("dl-schedule");
    if (sched) sched.onclick = () => downloadCsv(`anvitech-schedule-${todayStamp()}.csv`, entry.output);
    // Delay justification: a server-built 2-sheet .xlsx (both roles since the
    // 2026-08-09 role-parity fix); a plain file download.
    const dly = $("dl-delay");
    if (dly) dly.onclick = () => { window.location.href = "/delay-report.xlsx"; };
    const mach = $("dl-machine");
    const mView = entry.tables && entry.tables[0] && entry.tables[0].table;
    if (mach && mView) mach.onclick = () => downloadCsv(`anvitech-machine-schedule-${todayStamp()}.csv`, mView);
    else if (mach) mach.style.display = "none";
    const shift = $("dl-shiftwise");
    if (shift && entry.shiftwise && entry.shiftwise.rows && entry.shiftwise.rows.length) {
      shift.onclick = () => downloadCsv(`anvitech-shiftwise-schedule-${todayStamp()}.csv`, entry.shiftwise);
    } else if (shift) shift.style.display = "none";   // hidden when operator logic is off
  }
}

// ---- CSV download (for printing schedules) ----
function formatDdmmyyyy(d) {
  return `${String(d.getDate()).padStart(2, "0")}-${String(d.getMonth() + 1).padStart(2, "0")}-${d.getFullYear()}`;
}
function todayStamp() {
  return formatDdmmyyyy(new Date());
}

function tableToCsv(table) {
  const esc = (v) => {
    let s = v === null || v === undefined ? "" : String(v);
    // Neutralize CSV formula injection: a leading =, +, -, @, tab, or CR
    // would be executed as a formula by spreadsheet apps that open the file.
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [table.columns.map(esc).join(",")];
  table.rows.forEach((r) => lines.push(r.map(esc).join(",")));
  return lines.join("\r\n");
}
function downloadCsv(filename, table) {
  if (!table || !table.columns) return;
  const blob = new Blob(["﻿" + tableToCsv(table)], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// ---- Orders dashboard ----
async function renderOrders() {
  const root = mountFor("orders");
  if (!currentOrders) {
    try { currentOrders = (await (await fetch("/orders")).json()).orders; } catch (e) { currentOrders = null; }
  }
  const isAdmin = currentRole === "admin";
  let html = "";
  if (!currentOrders || !currentOrders.rows.length) {
    if (!planEverLoaded) {
      html += '<p class="placeholder">Loading your plan…</p>';
    } else {
      html += isAdmin
        ? '<p class="placeholder">No orders yet. Upload your Excel file to begin.</p>'
        : '<p class="placeholder">No orders yet. Ask an admin to upload the order Excel file.</p>';
    }
    root.innerHTML = html; return;
  }
  // Commitment controls are admin-only (server enforces this too). Danger actions
  // (delete) live in a separate strip at the bottom, visually set apart.
  if (isAdmin && commitmentEnabled) {
    html += '<div class="ord-toolbar">'
      + '<button id="ord-commit-sel" class="ghost-btn" title="A label only. It does not change the machine schedule.">Commit selected</button> '
      + '<button id="ord-uncommit-sel" class="ghost-btn" title="A label only. It does not change the machine schedule.">Uncommit selected</button>'
      + '</div>';
  }
  html += orderTableHtml(currentOrders, isAdmin);
  html += '<p class="g-note">Pending = not started. Running = some production entered. Complete = closed on Daily Entry. '
    + (commitmentEnabled
       ? 'Lane: <strong>open</strong> = newly arrived · <strong>committed</strong> = promised to the customer. The plan keeps its finish date within 3 days of the promise. Commit an order once you\'ve told the customer a date. '
         + '<strong>Red</strong> "Current expected" = later than the Promised date shown in the same row (the order has slipped).'
       : '"Current expected" is when the plan expects this order to finish.')
    + '</p>';
  if (isAdmin) {
    html += '<div class="danger-strip">'
      + '<span class="danger-label">Danger zone</span>'
      + '<button id="ord-del-sel" class="danger">Delete selected</button> '
      + '<button id="ord-del-all" class="danger">Delete ALL data</button>'
      + '<span class="muted"> · deletes permanently from the database (and their production entries)</span></div>';
  }
  root.innerHTML = html;
  if (isAdmin) { wireOrdersDelete(); wireOrdersCommit(); }
}

// "DD-MM-YYYY" -> Date, or null if unparseable (used to compare Promised vs
// Current expected for the slip highlight).
function ddmmyyyyToDate(s) {
  const p = String(s || "").split("-");
  if (p.length !== 3) return null;
  const d = new Date(+p[2], +p[1] - 1, +p[0]);
  return isNaN(d.getTime()) ? null : d;
}
// ISO "YYYY-MM-DD" -> Date, or null.
function isoToDate(s) {
  if (!s) return null;
  const d = new Date(String(s) + "T00:00:00");
  return isNaN(d.getTime()) ? null : d;
}

function orderTableHtml(table, showSelect) {
  const sIdx = table.columns.indexOf("Status");
  const soIdx = table.columns.indexOf("SO No");
  const itemIdx = table.columns.indexOf("Item Code");
  const laneIdx = table.columns.indexOf("Lane");
  const promIdx = table.columns.indexOf("Promised");
  // While the commit/uncommit feature is hidden, Lane and Promised are dropped
  // from the table entirely — they only mean anything alongside those controls.
  const hidden = commitmentEnabled ? new Set() : new Set([laneIdx, promIdx].filter((i) => i >= 0));
  let h = '<div class="table-wrap"><table><thead><tr>';
  if (showSelect) h += '<th><input type="checkbox" id="ord-all-check" title="select all"></th>';
  table.columns.forEach((c, i) => { if (!hidden.has(i)) h += `<th>${escapeHtml(c)}</th>`; });
  h += '<th>Current expected</th>';
  h += "</tr></thead><tbody>";
  table.rows.forEach((row) => {
    const so = soIdx >= 0 ? String(row[soIdx]) : "";
    const item = itemIdx >= 0 ? String(row[itemIdx]) : "";
    h += "<tr>";
    // An order is the (SO#, item) pair — carry both so delete targets the exact line.
    if (showSelect) h += `<td><input type="checkbox" class="ordsel" data-so="${escapeHtml(so)}" data-item="${escapeHtml(item)}"></td>`;
    row.forEach((cell, i) => {
      if (hidden.has(i)) return;
      const v = cell === null || cell === undefined ? "" : String(cell);
      if (i === sIdx) h += `<td><span class="status-pill status-${v.toLowerCase()}">${escapeHtml(v)}</span></td>`;
      else if (i === laneIdx) h += `<td><span class="lane-badge lane-${v.toLowerCase()}">${escapeHtml(v)}</span></td>`;
      else h += `<td>${escapeHtml(v)}</td>`;
    });
    // Current expected: look up the plan's expected completion for this (SO, item);
    // only populated after a Plan run (currentExpected comes from /run, not /orders).
    const key = so + "\x1f" + item;
    const expIso = currentExpected ? currentExpected[key] : null;
    const expDisp = expIso ? isoToDdmmyyyy(expIso) : "";
    // The red "slipped" flag compares against Promised, so it goes with the lanes.
    const promised = (commitmentEnabled && promIdx >= 0) ? String(row[promIdx] || "") : "";
    const promD = promised ? ddmmyyyyToDate(promised) : null;
    const expD = expIso ? isoToDate(expIso) : null;
    const slip = promD && expD && expD > promD;
    h += `<td class="${slip ? "slip" : ""}">${escapeHtml(expDisp)}</td>`;
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

// A destructive action guarded by re-entering the admin password. ``doFetch(pw)``
// must return the fetch Response; on 403 the modal stays open with an error and
// lets the user retry; Cancel/Escape aborts. Resolves to true on success.
function deleteWithPassword(message, doFetch) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.innerHTML = `
      <div class="modal">
        <h3>⚠ Confirm deletion</h3>
        <p>${escapeHtml(message)}</p>
        <p class="muted">This cannot be undone. Enter your password to continue.</p>
        <input id="pw-confirm" type="password" placeholder="Password" autocomplete="off" />
        <div class="modal-err" id="pw-err"></div>
        <div class="modal-actions">
          <button id="pw-cancel">Cancel</button>
          <button id="pw-ok" class="danger">Delete</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const input = ov.querySelector("#pw-confirm");
    const err = ov.querySelector("#pw-err");
    const ok = ov.querySelector("#pw-ok");
    const done = (v) => { ov.remove(); resolve(v); };
    setTimeout(() => input.focus(), 0);

    const submit = async () => {
      const pw = input.value;
      if (!pw) { err.textContent = "Enter your password."; return; }
      ok.disabled = true; err.textContent = "Checking…";
      try {
        const res = await doFetch(pw);
        if (res.status === 403) { err.textContent = "Password incorrect. Try again."; ok.disabled = false; input.select(); return; }
        if (!res.ok) { err.textContent = "Failed: " + (await res.text()); ok.disabled = false; return; }
        done(true);
      } catch (e) { err.textContent = "Error: " + e.message; ok.disabled = false; }
    };
    ov.querySelector("#pw-cancel").onclick = () => done(false);
    ok.onclick = submit;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submit();
      if (e.key === "Escape") done(false);
    });
    ov.addEventListener("click", (e) => { if (e.target === ov) done(false); });
  });
}

function wireOrdersDelete() {
  const allCheck = $("ord-all-check");
  if (allCheck) allCheck.onclick = () => {
    document.querySelectorAll(".ordsel").forEach((c) => (c.checked = allCheck.checked));
  };
  const delSel = $("ord-del-sel");
  if (delSel) delSel.onclick = async () => {
    const sel = [...document.querySelectorAll(".ordsel:checked")].map((c) => [c.dataset.so, c.dataset.item]);
    if (!sel.length) { setStatus("Select at least one order to delete.", true); return; }
    const okd = await deleteWithPassword(
      `Permanently delete ${sel.length} order(s) and their production data?`,
      (pw) => fetch("/orders/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orders: sel, password: pw }),
      }));
    if (!okd) { setStatus("Delete cancelled."); return; }
    setStatus(`Deleted ${sel.length} order(s).`);
    currentOrders = null; await runPlan();
  };
  const delAll = $("ord-del-all");
  if (delAll) delAll.onclick = async () => {
    const okd = await deleteWithPassword(
      "Permanently delete ALL orders and production data from the database?",
      (pw) => fetch("/orders/clear", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      }));
    if (!okd) { setStatus("Delete cancelled."); return; }
    setStatus("All orders deleted.");
    currentOrders = null; await runPlan();
  };
}

// Selected (SO, item) pairs from the order-book checkboxes.
function selectedOrderPairs() {
  return [...document.querySelectorAll(".ordsel:checked")].map((c) => [c.dataset.so, c.dataset.item]);
}

// Wire the Commit / Uncommit toolbar buttons (admin only).
function wireOrdersCommit() {
  const commitBtn = $("ord-commit-sel");
  if (commitBtn) commitBtn.onclick = async () => {
    const sel = selectedOrderPairs();
    if (!sel.length) { setStatus("Select orders to commit.", true); return; }
    try {
      const res = await fetch("/orders/commit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orders: sel }),
      });
      if (!res.ok) { setStatus("Commit failed: " + (await res.text()), true); return; }
      setStatus(`Committed ${sel.length} order(s).`);
      currentOrders = null; await runPlan();
    } catch (e) { setStatus("Commit error: " + e.message, true); }
  };

  const uncommitBtn = $("ord-uncommit-sel");
  if (uncommitBtn) uncommitBtn.onclick = async () => {
    const sel = selectedOrderPairs();
    if (!sel.length) { setStatus("Select orders to uncommit.", true); return; }
    try {
      const res = await fetch("/orders/uncommit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orders: sel }),
      });
      if (!res.ok) { setStatus("Uncommit failed: " + (await res.text()), true); return; }
      setStatus(`Uncommitted ${sel.length} order(s).`);
      currentOrders = null; await runPlan();
    } catch (e) { setStatus("Uncommit error: " + e.message, true); }
  };
}

// ---- Gantt ----
function renderGantt() {
  const root = mountFor("gantt");
  if (!currentGantt) {
    root.innerHTML = planEverLoaded
      ? '<p class="placeholder">No schedule yet. Add orders on the Orders tab first.</p>'
      : '<p class="placeholder">Loading your plan…</p>';
    return;
  }
  const g = currentGantt;
  if (!g.rows || !g.rows.length) {
    root.innerHTML = '<p class="placeholder">No schedule yet. Add orders on the Orders tab first.</p>';
    return;
  }
  const DAYW = ganttDayWidth, axisW = g.num_days * DAYW;
  const monthCells = g.months.map((m) => `<div class="g-month" style="width:${m.days * DAYW}px">${escapeHtml(m.label)}</div>`).join("");
  const dayCells = g.days.map((d) => `<div class="g-day${d.working ? "" : " g-off"}" style="width:${DAYW}px">${d.day} ${escapeHtml(d.month.slice(0, 3))}</div>`).join("");
  // Day-level chart: one column per day, no hour ruler. Day gridlines only,
  // with non-working days (Thursdays / holidays) shaded behind the bars.
  const grid = `repeating-linear-gradient(to right, transparent 0, transparent ${DAYW - 1}px, var(--border) ${DAYW - 1}px, var(--border) ${DAYW}px)`;
  const offDays = g.days.map((d, i) => d.working ? "" : `<div class="g-offday" style="left:${i * DAYW}px;width:${DAYW}px"></div>`).join("");
  const dateOnly = (s) => String(s).split(" ")[0];   // "DD-MM-YYYY HH:MM" -> "DD-MM-YYYY"

  const rowsHtml = g.rows.map((r) => {
    const lanes = r.bars.map((b) => {
      const left = b.offset_days * DAYW, w = Math.max(b.duration_days * DAYW, 8);
      const d0 = dateOnly(b.start), d1 = dateOnly(b.end);
      const dates = (d1 === d0) ? d0 : `${d0} → ${d1}`;
      const op = b.operator ? ` · ${b.operator}` : "";
      const tip = `${b.process} · ${b.machine}${op} · ${d0}${d1 !== d0 ? " → " + d1 : ""} · qty ${b.qty}`;
      // Wide bars (multi-day, e.g. OS turnaround) show the dates INSIDE at their start
      // so they stay on-screen; narrow bars show the dates just after the bar.
      const wide = w >= 220;
      const label = wide ? `${b.process} · ${dates}` : b.process;
      const after = wide ? "" : `<div class="g-bar-dates" style="left:${left + w + 6}px">${escapeHtml(dates)}</div>`;
      return `<div class="g-lane">`
        + `<div class="g-bar" style="left:${left}px;width:${w}px;background:${b.color}" title="${escapeHtml(tip)}">${escapeHtml(label)}</div>`
        + after
        + `</div>`;
    }).join("");
    const st = r.status || "";
    // Flag orders whose expected completion falls after the SO delivery date (late).
    const toDate = (s) => { const p = String(s).split("-"); return p.length === 3 ? new Date(+p[2], +p[1] - 1, +p[0]) : null; };
    const dd = toDate(r.so_delivery_date), cc = toDate(r.completion);
    const late = dd && cc && cc > dd;
    return `<tr>
      <td>${escapeHtml(r.item_name)}</td><td>${escapeHtml(r.item_code)}</td>
      <td>${escapeHtml(r.so_no)}</td><td>${r.so_qty}</td><td>${escapeHtml(r.so_delivery_date)}</td>
      <td class="${late ? "g-late" : ""}" title="${late ? "Finishes after the SO delivery date" : "On time"}">${escapeHtml(r.completion || "")}</td>
      <td>${st ? `<span class="status-pill status-${st.toLowerCase()}">${escapeHtml(st)}</span>` : ""}</td>
      <td class="g-timeline"><div class="g-lanes" style="width:${axisW}px;background-image:${grid}">${offDays}${lanes}</div></td>
    </tr>`;
  }).join("");

  const legend = Object.entries(g.machine_colors).map(([m, c]) => `<span class="g-leg"><span class="g-chip" style="background:${c}"></span>${escapeHtml(m)}</span>`).join("");
  root.innerHTML = `
    <div class="g-toolbar">Zoom <button id="g-zoom-out">−</button> <button id="g-zoom-in">+</button>
      <span class="muted">Each column is one day. Grey columns are Thursday or a holiday, when the shop is closed.</span></div>
    <div class="g-scroll"><table class="g-table"><thead>
      <tr><th class="g-corner" colspan="7" rowspan="2">Dates →</th>
          <th class="g-axis"><div class="g-band" style="width:${axisW}px">${monthCells}</div></th></tr>
      <tr><th class="g-axis"><div class="g-band" style="width:${axisW}px">${dayCells}</div></th></tr>
      <tr><th>Item name</th><th>Item Code</th><th>SO No</th><th>SO Qty</th><th>SO Delivery Date</th><th>Expected completion</th><th>Status</th>
          <th class="g-axis"><div class="g-band" style="width:${axisW}px"></div></th></tr>
    </thead><tbody>${rowsHtml}</tbody></table></div>
    <div class="g-legend"><strong>Bar colour = machine:</strong> ${legend}</div>
    <p class="g-note">"OS / Outsourced" and "Off-machine" are not real machines. They show work sent outside the shop, or a paperwork step.</p>
    <p class="g-note">Each bar is one job, shown on the day(s) it runs. Hover a bar to see the machine, operator, time and quantity. If <strong>Expected completion</strong> is red, the order will finish after its SO Delivery Date, so it is late.</p>`;
  $("g-zoom-in").onclick = () => { ganttDayWidth = Math.min(ganttDayWidth + 40, 560); renderGantt(); };
  $("g-zoom-out").onclick = () => { ganttDayWidth = Math.max(ganttDayWidth - 40, 80); renderGantt(); };
  fitGantt();
}

// Size the Gantt scroll area to fill from its top down to near the window's bottom, so its
// HORIZONTAL scrollbar is always pinned to the bottom of the screen — visible no matter how
// far down you scroll the rows (instead of dropping below the fold on a tall chart).
function fitGantt() {
  const gs = document.querySelector("#view-gantt .g-scroll");
  if (!gs) return;
  const top = gs.getBoundingClientRect().top;   // distance from the window top to the chart
  gs.style.maxHeight = Math.max(320, window.innerHeight - top - 24) + "px";
}
window.addEventListener("resize", () => {
  if (document.getElementById("view-gantt")?.classList.contains("active")) fitGantt();
});

// ---- Analytics ----
function renderAnalytics() {
  const root = mountFor("analytics");
  const a = currentTrace && currentTrace.analytics;
  if (!a || !a.machines || !a.machines.length) {
    root.innerHTML = planEverLoaded
      ? '<p class="placeholder">Nothing to show yet. Add orders on the Orders tab, then this page shows how busy your machines and people are.</p>'
      : '<p class="placeholder">Loading your plan…</p>';
    return;
  }
  const h = a.headline || {};
  const pct = (v) => v == null ? "-" : v + "%";
  const cls = (s) => s === "bottleneck" ? "hot" : s === "under-used" ? "cold" : s === "process" ? "proc" : "ok";

  // one utilization row: status dot · name (+ muted sub) · track/fill bar · value
  const row = (label, sub, v, s) => {
    const c = cls(s);
    const w = Math.max(Math.min(v == null ? 0 : v, 100), v ? 2 : 0);
    return `<div class="au-row">
      <span class="au-dot au-${c}"></span>
      <div class="au-name" title="${escapeHtml(label)}">${escapeHtml(label)}${sub ? `<span>${escapeHtml(sub)}</span>` : ""}</div>
      <div class="au-track"><div class="au-fill au-${c}" style="width:${w}%"></div></div>
      <div class="au-val au-${c}">${pct(v)}</div>
    </div>`;
  };

  // collapsible raw-numbers table (declutters — bars are the primary view)
  const table = (rows) => {
    if (!rows.length) return "";
    const cols = Object.keys(rows[0]).filter((c) => c !== "Status");
    const th = cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
    const tr = rows.map((r) => "<tr>" + cols.map((c) =>
      `<td>${escapeHtml(String(r[c] == null ? "-" : r[c]))}</td>`).join("") + "</tr>").join("");
    return `<details class="a-more"><summary>Show numbers</summary>
      <div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div></details>`;
  };

  // --- KPI cards ---
  const b = h.bottleneck;
  const nMach = a.machines.length;
  const kpis = `<div class="a-kpis">
    <div class="a-kpi hot">
      <span class="a-kpi-lab">Busiest machine</span>
      <span class="a-kpi-val">${b ? escapeHtml(b.Machine) : "-"}</span>
      <span class="a-kpi-sub">${b ? pct(b["Utilization %"]) + " busy, it is holding up the rest of the plan" : "no machines yet"}</span>
    </div>
    <div class="a-kpi">
      <span class="a-kpi-lab">Average machine use</span>
      <span class="a-kpi-val">${pct(h.avg_machine_util)}</span>
      <span class="a-kpi-sub">across ${nMach} machine${nMach === 1 ? "" : "s"}</span>
    </div>
    <div class="a-kpi">
      <span class="a-kpi-lab">Scheduled work</span>
      <span class="a-kpi-val">${Math.round(h.total_busy_hrs || 0)}<span class="a-kpi-unit">hrs</span></span>
      <span class="a-kpi-sub">total machine time</span>
    </div>
    <div class="a-kpi">
      <span class="a-kpi-lab">Total plan length</span>
      <span class="a-kpi-val">${h.makespan_days || "?"}<span class="a-kpi-unit">days</span></span>
      <span class="a-kpi-sub">days from the first scheduled job to the last</span>
    </div>
  </div>`;

  const legend = `<span class="a-legend">
    <span class="au-dot au-hot"></span>very busy, 85% or more
    <span class="au-dot au-ok"></span>normal
    <span class="au-dot au-cold"></span>mostly idle, 30% or less</span>`;

  const machineBars = a.machines.map((m) => row(m.Machine, m.Type, m["Utilization %"], m.Status)).join("");
  const groupBars = a.machine_groups.map((g) =>
    row(g.Type, g.Machines + (g.Machines === 1 ? " machine" : " machines"), g["Utilization %"], g.Status)).join("");
  const opBars = a.operators.length
    ? a.operators.map((o) => row(o.Operator, "", o["Utilization %"], o.Status)).join("")
    : '<p class="a-empty">Operator reports are off. Tick <strong>Show operator reports</strong> in Settings to see how busy each person is.</p>';
  // Honest capacity gap: shift segments no qualified operator was free to man
  // (never billed to a person, so nobody can show over 100%).
  const unstaffedNote = (h.unstaffed_hrs || 0) > 0
    ? `<p class="a-empty">⚠ <strong>${Math.round(h.unstaffed_hrs)} hrs</strong> of planned work has nobody free to run it, because every operator who can run that machine is already busy that shift. Those shifts need more people.</p>`
    : "";
  const procBars = a.processes.map((p) => row(p.Process, p.Machines, p["Share %"], "process")).join("");

  root.innerHTML = `
    <div class="a-head">
      <span class="a-window">${a.window ? "Current plan · " + escapeHtml(a.window.start + " → " + a.window.end) : ""}</span>
    </div>
    ${kpis}
    <div class="a-grid">
      <section class="a-card">
        <div class="a-card-head"><h3>How busy your machines are</h3>${legend}</div>
        <div class="au-list">${machineBars}</div>
        <div class="a-subhead">By machine type</div>
        <div class="au-list">${groupBars}</div>
        ${table(a.machines)}
      </section>
      <section class="a-card">
        <div class="a-card-head"><h3>How busy your operators are</h3></div>
        <div class="au-list">${opBars}</div>
        ${unstaffedNote}
        ${a.operators.length ? table(a.operators) : ""}
      </section>
      <section class="a-card a-span">
        <div class="a-card-head"><h3>Where the work goes</h3><span class="muted">how big each job type's share of the total work is. This is not a busy or idle score.</span></div>
        <div class="au-list au-cols">${procBars}</div>
        ${table(a.processes)}
      </section>
    </div>
    <p class="a-foot"><strong>How to read this.</strong> How busy = hours worked, divided by the hours that machine or person could have worked in this plan. It is not the full calendar: Thursdays, holidays and any days an operator was marked absent are already left out. So a one shift hand station and a two shift CNC can both show 60% and it means the same thing. A <span class="au-t hot">very busy</span> machine is slowing the whole plan down and needs attention first. A <span class="au-t cold">mostly idle</span> one has room for more work. The last chart shows how big each job type's share of the work is, not how busy anyone is.</p>`;
}

// ---- Rule 8 daily entry form ----
async function ensureItems() {
  if (ITEMS) return ITEMS;
  try { ITEMS = await (await fetch("/items")).json(); } catch (e) { ITEMS = { items: {} }; }
  return ITEMS;
}
function fy(label, inner) { return `<label class="efield fy">${label}${inner}</label>`; }
function fr(label, inner) { return `<label class="efield fr">${label}${inner}</label>`; }

function actualsFormHtml() {
  const today = new Date().toISOString().slice(0, 10);
  const left =
    fy("Date", `<input id="a-date" type="date" value="${today}" /> <span id="a-date-echo" class="date-echo"></span>`) +
    fy("Shift", `<select id="a-shift"><option value="1st shift">1st shift</option><option value="2nd shift">2nd shift</option></select>`) +
    fr("Operator <span class=auto>(required, except for OS/outside steps)</span>", `<select id="a-operator"><option value="">Select operator</option></select>`) +
    fr("SO No <span class=auto>(step 1: pick from orders)</span>", `<select id="a-so"><option value="">Select SO No</option></select>`) +
    fr("Item Code <span class=auto>(step 2: pick this SO's item)</span>", `<select id="a-item"><option value="">Select SO No first</option></select>`) +
    fr("Item Name <span class=auto>(auto)</span>", `<input id="a-itemname" readonly />`) +
    fr("Process <span class=auto>(dropdown)</span>", `<select id="a-process"></select>`);
  const right =
    fy("Qty Produced (good pieces)", `<input id="a-prod" type="number" min="0" value="" />`) +
    fy("Qty Rejected (bad pieces)", `<input id="a-rej" type="number" min="0" value="" />`) +
    fy("Actual Setting Time (min)", `<input id="a-setup" type="number" min="0" value="" />`) +
    fy("No Power (min)", `<input id="a-nopower" type="number" min="0" value="" />`) +
    fy("No Operator (min)", `<input id="a-noop" type="number" min="0" value="" />`) +
    fy("Tool Problem (min)", `<input id="a-tool" type="number" min="0" value="" />`) +
    fy("Machine Breakdown (min)", `<input id="a-mbd" type="number" min="0" value="" />`) +
    fy("No Load (min)", `<input id="a-noload" type="number" min="0" value="" />`) +
    fy("Other Work (min)", `<input id="a-other" type="number" min="0" value="" />`) +
    fy("Remarks", `<textarea id="a-remarks" rows="2"></textarea>`);
  return `
    <div class="entry-legend"><span class="lg lg-y">Type this yourself</span><span class="lg lg-r">Pick from the list</span></div>
    <div class="entry-grid"><div class="entry-col">${left}</div><div class="entry-col">${right}</div></div>
    <button id="a-save" class="primary">Save daily entry</button>
    <button id="a-mark-complete" class="ghost-btn" title="Close this order for good. You only need the SO No and Item Code, nothing else.">✓ Mark this SO+item complete</button>
    <div class="optimize-done-row">
      <button id="optimize-done" class="primary">Done entering: update plan</button>
      <span id="optimize-done-status" class="status"></span>
    </div>
    <p class="explainer">Save records your entry right away. It does <b>not</b> rebuild the
      schedule, so you can punch quickly without waiting. Click <b>Done entering: update
      plan</b> once you're finished for the day to refresh the plan from everything you
      entered. This can take 15 to 30 minutes. Work that is already part finished stays on the same machine.</p>`;
}

// Populate the Capture Actuals Operator dropdown from the app-owned operator
// table (GET /operators — role-open, same list either role sees on Settings).
async function fillOperatorDropdown() {
  const sel = $("a-operator");
  if (!sel) return;
  try {
    const res = await fetch("/operators");
    if (!res.ok) return;
    const data = await res.json();
    const rows = data.operators || [];
    const keep = sel.value;
    sel.innerHTML = `<option value="">Select operator</option>`
      + rows.map((o) => `<option value="${escapeHtml(o.name)}">${escapeHtml(o.name)}</option>`).join("");
    if (keep) sel.value = keep;
  } catch (e) { /* dropdown stays empty; required-field check + server 400 are the backstop */ }
}

// Populate the Shift dropdown with exactly the values /items advertises (falls
// back to the current two-shift vocabulary if the fetch hasn't landed yet), so
// the operator can no longer free-type shift text the report can't recognize.
function fillShiftDropdown() {
  const sel = $("a-shift");
  if (!sel) return;
  const list = (ITEMS && ITEMS.shifts && ITEMS.shifts.length) ? ITEMS.shifts : ["1st shift", "2nd shift"];
  const keep = sel.value;
  sel.innerHTML = list.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  sel.value = list.includes(keep) ? keep : list[0];
}

// Populate the SO No dropdown with every SO from the orders tab.
function fillSoDropdown() {
  const sel = $("a-so");
  if (!sel) return;
  // Only ACTIVE (open) orders — you don't punch production for a completed/archived
  // order (and a stray punch on one would wrongly advance the plan clock).
  const list = (ITEMS && ITEMS.open_so_nos) ? ITEMS.open_so_nos
             : (ITEMS && ITEMS.so_nos) ? ITEMS.so_nos : [];
  sel.innerHTML = `<option value="">Select SO No</option>`
    + list.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
}

// Step 2 of the picker: selecting an SO No fills the Item Code dropdown with THAT
// SO's open item lines (an SO number can carry several items, so the operator picks
// which one). A lone item auto-selects; then item name + process dropdown follow.
function fillItemFromSO() {
  const so = $("a-so").value.trim();
  const map = ITEMS && ITEMS.so_to_items ? ITEMS.so_to_items : {};
  const lines = map[so] || [];
  const sel = $("a-item");
  if (!lines.length) {
    sel.innerHTML = `<option value="">Select SO No first</option>`;
  } else {
    const head = lines.length > 1 ? `<option value="">Select item</option>` : "";
    sel.innerHTML = head + lines.map((l) =>
      `<option value="${escapeHtml(l.item_code)}">${escapeHtml(l.item_code)}${l.item_name ? " - " + escapeHtml(l.item_name) : ""}</option>`
    ).join("");
    if (lines.length === 1) sel.value = lines[0].item_code;   // only one → pick it
  }
  fillItemMeta();
}

function fillItemMeta() {
  const code = $("a-item").value.trim();
  if (!code) {
    $("a-itemname").value = "";
    $("a-process").innerHTML = `<option value="">-</option>`;
    return;
  }
  const meta = ITEMS && ITEMS.items ? ITEMS.items[code] : null;
  if (meta) {
    $("a-itemname").value = meta.item_name || "";
    $("a-process").innerHTML = meta.processes.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
  } else {
    $("a-itemname").value = "(unknown item code)";
    $("a-process").innerHTML = `<option value="">-</option>`;
  }
}

// True if an identical entry (same date / SO / item / process / produced / rejected)
// is already on record — used to warn before saving an accidental duplicate.
function isoToDdmmyyyy(iso) {            // "2025-08-01" -> "01-08-2025" (display format)
  const p = String(iso).split("-");
  return p.length === 3 ? `${p[2]}-${p[1]}-${p[0]}` : String(iso);
}
function actualIsDuplicate(body) {
  const t = currentTrace && currentTrace.rule7 && currentTrace.rule7.output;
  if (!t || !t.rows || !t.rows.length) return false;
  const ix = (n) => t.columns.indexOf(n);
  const [di, si, ii, pi, qp, qr] =
    ["Date", "SO No", "Item Code", "Process", "Qty Produced", "Qty Rejected"].map(ix);
  const dateCell = isoToDdmmyyyy(body.entry_date);   // stored Date column is DD-MM-YYYY
  return t.rows.some((r) =>
    String(r[di]) === dateCell && String(r[si]) === String(body.so_no) &&
    String(r[ii]) === String(body.item_code) && String(r[pi]) === String(body.process) &&
    Number(r[qp]) === Number(body.qty_produced) && Number(r[qr]) === Number(body.qty_rejected));
}

async function wireActualsForm() {
  ITEMS = null;                 // refetch so the SO dropdown reflects the latest orders
  await ensureItems();
  fillShiftDropdown();
  fillSoDropdown();
  await fillOperatorDropdown();
  $("a-so").addEventListener("change", fillItemFromSO);   // step 1: pick SO No -> fill Item dropdown
  $("a-item").addEventListener("change", fillItemMeta);   // step 2: pick Item -> name + processes
  fillItemMeta();
  // The native date picker shows the browser locale (MM/DD/YYYY on US machines);
  // echo the chosen date in DD-MM-YYYY so it always matches the rest of the app.
  const dateEcho = () => {
    const e = $("a-date-echo"), v = $("a-date") && $("a-date").value;
    if (e) e.textContent = v ? "= " + isoToDdmmyyyy(v) : "";
  };
  dateEcho();
  $("a-date").addEventListener("change", dateEcho);
  $("a-date").addEventListener("input", dateEcho);

  // Standalone "Mark this SO+item complete" (owner, 2026-07-28): needs ONLY the SO No +
  // Item Code — no operator, no punch. Archives the order; its recorded production stays
  // for reports. Like Save, it does NOT re-plan (only "Done entering" does).
  const mc = $("a-mark-complete");
  if (mc) mc.onclick = async () => {
    const so = $("a-so").value.trim(), item = $("a-item").value.trim();
    if (!so || !item) {
      setStatus("⚠ Pick the SO No and Item Code, then Mark complete.", true);
      $(!so ? "a-so" : "a-item").focus();
      return;
    }
    if (!confirm(`Mark ${so} / ${item} complete and archive it?\nIt will move out of the daily list. What you already entered for it is kept.`)) return;
    mc.disabled = true; setStatus("Marking complete…");
    try {
      const res = await fetch("/orders/complete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ so_no: so, item_code: item }),
      });
      if (!res.ok) { setStatus("Mark complete failed: " + (await res.text()), true); mc.disabled = false; return; }
      // Refresh the order list (the archived line drops off) — do NOT null it, or the
      // Daily Entry form would treat it as "no orders" and blank out (regression, 2026-07-28).
      try { currentOrders = (await (await fetch("/orders")).json()).orders; } catch (e) { /* keep existing */ }
      setStatus(`✓ ${so} / ${item} marked complete. Click "Done entering: update plan" to update the schedule.`);
      renderTab("rule7");     // reload the SO/Item pickers (the archived order drops off) — no re-plan
    } catch (e) { setStatus("Mark complete error: " + e.message, true); mc.disabled = false; }
  };

  $("a-save").onclick = async () => {
    const btn = $("a-save");
    const num = (id) => Number($(id).value) || 0;
    const body = {
      entry_date: $("a-date").value, shift: $("a-shift").value,
      so_no: $("a-so").value.trim(), item_code: $("a-item").value.trim(),
      item_name: $("a-itemname").value, process: $("a-process").value,
      operator: $("a-operator").value.trim(),
      qty_produced: num("a-prod"), qty_rejected: num("a-rej"),
      actual_setup_min: num("a-setup"), no_power_min: num("a-nopower"),
      no_operator_min: num("a-noop"), tool_problem_min: num("a-tool"),
      machine_breakdown_min: num("a-mbd"), no_load_min: num("a-noload"),
      other_work_min: num("a-other"), remarks: $("a-remarks").value,
    };
    // Outsourced (OS) steps run off-site — no in-house operator runs them — so the
    // operator is NOT required for them (owner rule, 2026-07-26). Every other step
    // still requires one. The server enforces the same rule; this is the UX mirror.
    const _meta = ITEMS && ITEMS.items ? ITEMS.items[body.item_code] : null;
    const _osStep = !!(_meta && _meta.os_processes && _meta.os_processes.includes(body.process));
    const _needOperator = !_osStep;
    if ((_needOperator && !body.operator) || !body.so_no || !body.item_code) {
      setStatus("⚠ Select SO No, Item Code" + (_needOperator ? ", and Operator" : "") + " before saving.", true);
      $(!body.so_no ? "a-so" : !body.item_code ? "a-item" : "a-operator").focus();
      return;
    }
    // Quantity/downtime fields carry min="0" but Save is a plain button (not a
    // form submit), so the browser never enforces it — check for negatives here.
    const negFields = [
      ["a-prod", "Qty Produced"], ["a-rej", "Qty Rejected"], ["a-setup", "Actual Setting Time"],
      ["a-nopower", "No Power"], ["a-noop", "No Operator"], ["a-tool", "Tool Problem"],
      ["a-mbd", "Machine Breakdown"], ["a-noload", "No Load"], ["a-other", "Other Work"],
    ].filter(([id]) => Number($(id).value) < 0);
    if (negFields.length) {
      setStatus("⚠ " + negFields.map((f) => f[1]).join(", ") + " cannot be negative.", true);
      return;
    }
    // Guard against re-saving the exact same entry (the #1 cause of duplicates).
    if (actualIsDuplicate(body) && !confirm(
        `An identical entry is already saved (SO ${body.so_no}, ${body.process || "-"}, `
        + `${body.qty_produced} produced on ${body.entry_date}). Save another?`)) {
      return;
    }
    // Immediate feedback so you know the click registered.
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Saving…";
    setStatus("Saving…");
    try {
      const res = await fetch("/actuals", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) {
        setStatus("Save failed: " + (await res.text()), true);
        btn.disabled = false; btn.textContent = label; return;
      }
      const d = await res.json();
      // SAVE IS INSTANT — no re-plan here (owner rule, 2026-07-26). Re-planning the whole
      // schedule is slow, and the owner wants that to happen ONLY on "Done entering —
      // update plan", not on every punch. Update the Daily Entry saved-entries list +
      // rollup DIRECTLY from the /actuals response (the same no-re-plan pattern the
      // rollback handler uses), so the punch shows immediately without the pause.
      if (currentTrace && currentTrace.rule7) {
        currentTrace.rule7.output = d.actuals;
        currentTrace.rule7.actuals_ids = d.actuals_ids;
        currentTrace.rule7.tables = [{
          title: "Per item code: output & downtime rollup (minutes summed across entries)",
          table: d.by_item,
        }];
      }
      const doneHint = ' Click "Done entering: update plan" when you have finished for the day.';
      setStatus("✓ Saved." + doneHint);
      renderTab("rule7");   // instant: fresh blank form + updated saved-entries list
    } catch (e) {
      setStatus("Save error: " + e.message, true); btn.disabled = false; btn.textContent = label;
    }
  };
  // "Done entering — update plan": re-optimizes the job order from the day's
  // feedback (both roles), waits for the contest, then refreshes the whole plan.
  const doneBtn = $("optimize-done");
  if (doneBtn) doneBtn.onclick = doneOptimize;
}

// Capture-actuals table with a per-row Rollback button (uses the parallel ids).
function actualsOutputHtml(table, ids) {
  if (!table || !table.columns || !table.columns.length) return '<div class="empty">No entries</div>';
  // Rollback is the FIRST column so it's always visible (the table is wide).
  let h = '<div class="table-wrap"><table><thead><tr><th>Rollback</th>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += "</tr></thead><tbody>";
  table.rows.forEach((row, i) => {
    const id = ids && ids[i] ? ids[i] : "";
    h += "<tr>";
    h += id
      ? `<td><button class="rollback-btn danger" data-id="${escapeHtml(id)}" title="Undo this entry">Undo</button></td>`
      : "<td></td>";
    row.forEach((cell) => (h += `<td>${cell === null || cell === undefined ? "" : escapeHtml(String(cell))}</td>`));
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

// Roll back one saved actual: delete it and return that order to normal.
function wireRollback() {
  document.querySelectorAll(".rollback-btn").forEach((b) => {
    b.onclick = async () => {
      const id = b.getAttribute("data-id");
      if (!id) return;
      if (!confirm("Undo this entry? It will be deleted for good. If this entry is the one that marked the order complete, the order will open again.")) return;
      b.disabled = true; setStatus("Rolling back…");
      try {
        const res = await fetch("/actuals/rollback", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
        });
        if (!res.ok) { setStatus("Could not undo this entry: " + (await res.text()), true); b.disabled = false; return; }
        const d = await res.json();
        if (currentTrace && currentTrace.rule7) {
          currentTrace.rule7.output = d.actuals;
          currentTrace.rule7.actuals_ids = d.actuals_ids;
          currentTrace.rule7.tables = [{
            title: "Per item code: output & downtime rollup (minutes summed across entries)",
            table: d.by_item,
          }];
        }
        if (d.orders) currentOrders = d.orders;
        setStatus("✓ Entry undone." + (d.uncompleted_order ? " Order reopened." : "") + " Updating the plan…");
        renderTab("rule7");
        await runPlan(false);                    // refreshes currentTrace/gantt/orders/report
        setStatus("✓ Entry undone." + (d.uncompleted_order ? " Order reopened." : "") + " Plan updated.");
      } catch (e) { setStatus("Could not undo this entry: " + e.message, true); b.disabled = false; }
    };
  });
}

function tableHtml(table, withClasses) {
  if (!table || !table.columns || table.columns.length === 0) return '<div class="empty">No rows</div>';
  let h = '<div class="table-wrap"><table><thead><tr>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += "</tr></thead><tbody>";
  table.rows.forEach((row) => {
    h += "<tr>";
    row.forEach((cell) => {
      let cls = "";
      if (withClasses && String(cell).includes("PENDING")) cls = ' class="pill-pending"';
      h += `<td${cls}>${cell === null || cell === undefined ? "" : escapeHtml(String(cell))}</td>`;
    });
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- Operator efficiency (Settings-area block; admin-only — pure reporting,
// never touches the plan). GET /efficiency for the on-screen preview,
// GET /efficiency.csv for the download; both admin-only server-side. ----
function effDefaultMonth() {
  // Previous calendar month, "YYYY-MM" (the value a <input type="month"> wants).
  const d = new Date();
  d.setDate(1);          // avoid rolling into the wrong month on short months
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function effYearMonth() {
  const el = $("eff-month");
  const v = el && el.value;
  if (!v) return null;
  const [y, m] = v.split("-").map(Number);
  return { year: y, month: m };
}

async function previewEfficiency() {
  const ym = effYearMonth();
  const el = $("eff-table");
  if (!ym) { setStatus("Pick a month first.", true); return; }
  try {
    const res = await fetch(`/efficiency?year=${ym.year}&month=${ym.month}`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { setStatus("Efficiency error: " + (body.detail || res.status), true); return; }
    renderEfficiencyTable(body.rows, el);
  } catch (e) { setStatus("Efficiency error: " + e.message, true); }
}

function renderEfficiencyTable(rows, el) {
  if (!el) return;
  if (!rows || rows.length === 0) { el.innerHTML = '<div class="empty">No rows</div>'; return; }
  const columns = Object.keys(rows[0]);
  const tableRows = rows.map((r) => columns.map((c) => (r[c] === null || r[c] === undefined ? "-" : r[c])));
  el.innerHTML = tableHtml({ columns, rows: tableRows });
}

function downloadEfficiencyCsv() {
  const ym = effYearMonth();
  if (!ym) { setStatus("Pick a month first.", true); return; }
  window.location.href = `/efficiency.csv?year=${ym.year}&month=${ym.month}`;
}

// ---- Operator absences (Settings-area block; list is visible to both roles,
// add/remove controls are admin-only via CSS). GET /absences is role-open. ----
function renderAbsenceList(absences) {
  const ul = $("absence-list");
  if (!ul) return;
  if (!absences || absences.length === 0) {
    ul.innerHTML = '<li class="muted">No operators currently marked absent.</li>';
    return;
  }
  ul.innerHTML = absences.map((a) => `
    <li>
      <span>${escapeHtml(a.operator)}: ${isoToDdmmyyyy(a.from_date)} to ${isoToDdmmyyyy(a.to_date)}</span>
      <button type="button" class="ghost-btn absence-remove admin-only" data-id="${escapeHtml(a.id)}">✕</button>
    </li>`).join("");
  ul.querySelectorAll(".absence-remove").forEach((btn) => {
    btn.onclick = () => removeAbsence(btn.dataset.id);
  });
}

async function loadAbsences() {
  try {
    const res = await fetch("/absences");
    if (!res.ok) return;
    const data = await res.json();
    const sel = $("absence-operator");
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = (data.operators || []).map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
      if (prev && (data.operators || []).includes(prev)) sel.value = prev;
    }
    renderAbsenceList(data.absences || []);
  } catch (e) { /* the list is a convenience view — a fetch hiccup shouldn't block the page */ }
}

async function addAbsence() {
  const op = $("absence-operator"), from = $("absence-from"), to = $("absence-to");
  if (!op || !op.value) { setStatus("Pick an operator to mark absent.", true); return; }
  if (!from.value || !to.value) { setStatus("Pick both absence dates.", true); return; }
  if (new Date(to.value) < new Date(from.value)) {
    setStatus("'To' date must be on or after 'From' date.", true);
    return;
  }
  try {
    const res = await fetch("/absences", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator: op.value, from_date: from.value, to_date: to.value }),
    });
    if (!res.ok) { setStatus("Mark absent failed: " + (await res.text()), true); return; }
    setStatus(`Marked ${op.value} absent.`);
    from.value = ""; to.value = "";
    await loadAbsences();
    await runPlan(false);
  } catch (e) { setStatus("Mark absent error: " + e.message, true); }
}

async function removeAbsence(id) {
  if (!window.confirm("Remove this absence? The operator will be treated as available for scheduling again.")) return;
  try {
    const res = await fetch(`/absences/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { setStatus("Remove absence failed: " + (await res.text()), true); return; }
    setStatus("Absence removed.");
    await loadAbsences();
    await runPlan(false);
  } catch (e) { setStatus("Remove absence error: " + e.message, true); }
}

// ---- Operators & shifts (Settings-area block). The list is visible to both
// roles, but rows render two different markups per role (rather than dual-DOM
// + CSS-hiding as the absence row's single delete button does): admins get
// editable inputs/select/checkbox/remove; users get plain text — same
// per-role conditional-render approach already used for the order book table
// (`renderOrders`'s `isAdmin` branch). Simplest fit here since every cell but
// the name needs an admin-only edit affordance, not just one trailing button. ----
// The machines an operator may be qualified for, straight off the uploaded Excel's
// "Machine master" sheet (served read-only by GET /operators). Kept in module state
// so the picker and the add-operator row share one list.
let machineOptions = [];     // [{id, name, type, provisional}]
let operatorRows = [];       // last /operators rows — re-rendered locally as chips change
let newOpMachines = [];      // the Add-operator row's pending selection (canonical ids)

// Split an operator's stored machines string EXACTLY the way the LIVE engine does
// (ppc_engine/loaders/normalize.py `parse_machine_options`: split on "/" and ","
// only, then uppercase and drop whitespace). Mirroring it — rather than being more
// forgiving — is the whole point: a token this panel draws as "unknown" is exactly a
// token the scheduler cannot match to a machine. So a row broken by a typo (a full
// stop instead of a separator, say) shows up as a red chip instead of silently
// disqualifying that operator from every machine, which is what happens today.
function parseMachinesRaw(raw) {
  const out = [];
  String(raw || "").split(/[/,]/).forEach((tok) => {
    const id = tok.replace(/\s+/g, "").toUpperCase();
    if (!id || out.some((t) => t.id === id)) return;
    const known = machineOptions.find((mo) => mo.id === id);
    out.push({ id, name: known ? known.name : tok.trim(), known: !!known });
  });
  return out;
}

// Selected machines as chips. `editable` adds the ✕; unknown tokens are flagged
// rather than hidden or silently dropped — removing one is always a human's call.
function machineChipsHtml(tokens, rowId, editable, emptyMsg) {
  if (!tokens.length) return `<span class="mach-none">${escapeHtml(emptyMsg)}</span>`;
  return tokens.map((t) => {
    const x = editable
      ? `<button type="button" class="mach-x" data-id="${escapeHtml(rowId)}" ` +
        `data-token="${escapeHtml(t.id)}" title="Remove ${escapeHtml(t.name)}">✕</button>`
      : "";
    if (t.known) return `<span class="mach-chip">${escapeHtml(t.name)}${x}</span>`;
    return `<span class="mach-chip unknown" title="&quot;${escapeHtml(t.id)}&quot; is not a `
      + `machine in your Machine master, so the planner ignores it. This operator gets no `
      + `credit for it. Remove it and pick the real machines.">⚠ ${escapeHtml(t.id)}${x}</span>`;
  }).join("");
}

// The "+ Add machine" dropdown: every machine not already picked, grouped by the
// master's machine type, with any machine the routings use but Machine master does
// not list yet in a trailing group of its own.
function machineSelectHtml(selectedIds, cls, rowId) {
  if (!machineOptions.length) {
    return `<span class="mach-none">Upload your Excel to list machines</span>`;
  }
  const taken = new Set(selectedIds);
  const groups = new Map();       // insertion order = the API's (type, provisional) sort
  machineOptions.forEach((mo) => {
    if (taken.has(mo.id)) return;
    const label = mo.provisional ? "Not in Machine master yet" : (mo.type || "Other");
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(mo);
  });
  if (!groups.size) return `<span class="mach-none">All machines selected</span>`;
  const opts = Array.from(groups).map(([label, rows]) =>
    `<optgroup label="${escapeHtml(label)}">` +
    rows.map((mo) => `<option value="${escapeHtml(mo.id)}">${escapeHtml(mo.name)}</option>`).join("") +
    `</optgroup>`).join("");
  return `<select class="${cls}" data-id="${escapeHtml(rowId)}">`
    + `<option value="">＋ Add machine</option>${opts}</select>`;
}

// Add/remove one machine on an existing operator. The row is updated in the local
// cache and the table re-rendered from it (a full reload would yank the dropdown out
// from under the click), the PATCH goes out immediately so an edit is never lost, and
// the re-plan is debounced so adding four machines costs one re-plan, not four.
function changeOperatorMachines(id, mutate) {
  const row = operatorRows.find((o) => o.id === id);
  if (!row) return;
  const next = [];
  mutate(parseMachinesRaw(row.machines_raw).map((t) => t.id))
    .forEach((x) => { if (x && !next.includes(x)) next.push(x); });
  row.machines_raw = next.join("/");   // "/" — the one separator both parsers agree on
  renderOperatorsTable(operatorRows);
  patchOperator(id, { machines_raw: row.machines_raw }, { reload: false, debounceReplan: true });
}

function wireMachinePicker(scope, onAdd, onRemove) {
  scope.querySelectorAll(".mach-add").forEach((sel) => {
    sel.addEventListener("change", () => { if (sel.value) onAdd(sel.dataset.id, sel.value); });
  });
  scope.querySelectorAll(".mach-x").forEach((btn) => {
    btn.onclick = () => onRemove(btn.dataset.id, btn.dataset.token);
  });
}

function renderOperatorsTable(operators) {
  const tbody = $("operators-tbody");
  if (!tbody) return;
  const isAdmin = currentRole === "admin";
  if (!operators || operators.length === 0) {
    const msg = "No operators yet. The first Excel you upload fills this list automatically "
      + "from its \"Operator &amp; shift Master\" sheet"
      + (isAdmin ? ", or add one below." : ".");
    tbody.innerHTML = `<tr><td colspan="${isAdmin ? 4 : 3}" class="empty">${msg}</td></tr>`;
    return;
  }
  tbody.innerHTML = operators.map((o) => {
    const id = escapeHtml(o.id);
    const tokens = parseMachinesRaw(o.machines_raw);
    if (!isAdmin) {
      const shiftLabel = o.shift ? escapeHtml(o.shift) : "Day (9–18)";
      return `<tr>
        <td>${escapeHtml(o.name)}</td>
        <td class="mach-cell">${machineChipsHtml(tokens, o.id, false, "No machines")}</td>
        <td>${shiftLabel}</td>
      </tr>`;
    }
    const shiftOpts = ["", "First shift", "Second shift"].map((v) =>
      `<option value="${escapeHtml(v)}"${o.shift === v ? " selected" : ""}>${v === "" ? "Day (9–18)" : escapeHtml(v)}</option>`
    ).join("");
    return `<tr data-id="${id}">
      <td>${escapeHtml(o.name)}</td>
      <td class="mach-cell">${machineChipsHtml(tokens, o.id, true, "No machines, so this operator is never scheduled")}`
      + `${machineSelectHtml(tokens.map((t) => t.id), "mach-add", o.id)}</td>
      <td><select class="op-shift" data-id="${id}">${shiftOpts}</select></td>
      <td class="admin-only"><button type="button" class="ghost-btn op-remove" data-id="${id}">✕</button></td>
    </tr>`;
  }).join("");
  if (isAdmin) {
    wireMachinePicker(tbody,
      (id, machineId) => changeOperatorMachines(id, (ids) => ids.concat([machineId])),
      (id, token) => changeOperatorMachines(id, (ids) => ids.filter((x) => x !== token)));
    tbody.querySelectorAll(".op-shift").forEach((sel) => {
      sel.addEventListener("change", () => patchOperator(sel.dataset.id, { shift: sel.value }));
    });
    tbody.querySelectorAll(".op-remove").forEach((btn) => {
      btn.onclick = () => removeOperator(btn.dataset.id);
    });
  }
}

async function loadOperators() {
  try {
    const res = await fetch("/operators");
    if (!res.ok) return;
    const data = await res.json();
    const header = $("operators-header");
    if (header) {
      header.textContent = "The shift you set here is used every week, until you change it.";
    }
    machineOptions = data.machines || [];      // set BEFORE rendering — the chips need it
    operatorRows = data.operators || [];
    renderOperatorsTable(operatorRows);
    renderNewOperatorMachines();
    renderStatusStrip();
  } catch (e) { /* the panel is a convenience view — a fetch hiccup shouldn't block the page */ }
}

// One re-plan after a burst of edits, not one per click.
let _replanTimer = null;
function replanSoon(delayMs) {
  if (_replanTimer) clearTimeout(_replanTimer);
  _replanTimer = setTimeout(() => { _replanTimer = null; runPlan(false); }, delayMs || 1200);
}

async function patchOperator(id, body, opts) {
  const { reload = true, debounceReplan = false } = opts || {};
  try {
    const res = await fetch(`/operators/${encodeURIComponent(id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // A failed PATCH always resyncs from the server, even when the caller rendered
    // the change optimistically — the screen must never keep an edit the store rejected.
    if (!res.ok) { setStatus("Update operator failed: " + (await res.text()), true); await loadOperators(); return; }
    setStatus("Operator updated.");
    if (reload) await loadOperators();
    if (debounceReplan) replanSoon();
    else await runPlan(false);
  } catch (e) { setStatus("Update operator error: " + e.message, true); await loadOperators(); }
}

// The Add-operator row's machine picker. Its selection lives in `newOpMachines`
// until the row is submitted, so nothing is stored for an operator who is never added.
function renderNewOperatorMachines() {
  const wrap = $("op-add-machines");
  if (!wrap) return;
  const tokens = newOpMachines.map((id) => {
    const mo = machineOptions.find((m) => m.id === id);
    return { id, name: mo ? mo.name : id, known: !!mo };
  });
  wrap.innerHTML = machineChipsHtml(tokens, "__new__", true, "")
    + machineSelectHtml(newOpMachines, "mach-add", "__new__");
  wireMachinePicker(wrap,
    (_id, machineId) => {
      if (!newOpMachines.includes(machineId)) newOpMachines.push(machineId);
      renderNewOperatorMachines();
    },
    (_id, token) => {
      newOpMachines = newOpMachines.filter((x) => x !== token);
      renderNewOperatorMachines();
    });
}

async function addOperator() {
  const name = $("op-add-name"), shift = $("op-add-shift");
  if (!name || !name.value.trim()) { setStatus("Enter an operator name.", true); return; }
  try {
    const res = await fetch("/operators", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name.value.trim(),
        machines_raw: newOpMachines.join("/"),
        shift: shift ? shift.value : "",
      }),
    });
    if (!res.ok) { setStatus("Add operator failed: " + (await res.text()), true); return; }
    setStatus(`Added operator ${name.value.trim()}.`);
    name.value = ""; newOpMachines = []; if (shift) shift.value = "";
    await loadOperators();
    await runPlan(false);
  } catch (e) { setStatus("Add operator error: " + e.message, true); }
}

async function removeOperator(id) {
  if (!confirm("Remove this operator? Any absences recorded against them are kept but will show as unknown.")) return;
  try {
    const res = await fetch(`/operators/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { setStatus("Remove operator failed: " + (await res.text()), true); return; }
    setStatus("Operator removed.");
    await loadOperators();
    await runPlan(false);
  } catch (e) { setStatus("Remove operator error: " + e.message, true); }
}

// Wire the admin controls (null-guarded — they're absent/hidden for the user role).
const _runBtn = $("run-btn");
if (_runBtn) _runBtn.onclick = () => runPlan(true);   // explicit admin Plan → persist
const _upBtn = $("upload-btn");
if (_upBtn) _upBtn.onclick = uploadExcel;
// Plan-start date: keep the DD-MM-YYYY echo in sync as the admin changes it.
// The "Start from today" checkbox disables the picker and echoes today (auto).
const _psField = $("cfg-plan-start");
if (_psField) { _psField.addEventListener("change", updatePlanStartEcho); }
const _autoField = $("cfg-start-auto");
if (_autoField) { _autoField.addEventListener("change", updatePlanStartEcho); }
updatePlanStartEcho();
// Settings & Optimize are now their own nav destinations (Settings view / Schedule
// view) — no disclosure toggles. Optimize Start/Stop wire directly.
const _optStart = $("optimize-start");
if (_optStart) _optStart.onclick = startOptimize;
const _optStop = $("optimize-stop");
if (_optStop) _optStop.onclick = stopOptimize;
// Operator absences: add is admin-only (button is CSS-hidden for the user role,
// but the handler is harmless to wire either way — the server enforces the role).
const _absAdd = $("absence-add");
if (_absAdd) _absAdd.onclick = addAbsence;
// Operators & shifts: add is admin-only (row wrapped in admin-only, so it's
// CSS-hidden for the user role too; the handler is harmless to wire either
// way — the server enforces the role).
const _opAdd = $("op-add-btn");
if (_opAdd) _opAdd.onclick = addOperator;
// Operator efficiency: month input defaults to last month; Preview/Download are
// admin-only (server enforces it; the fieldset is inside the admin-only Settings
// panel so the user role never sees it at all).
const _effMonthEl = $("eff-month");
if (_effMonthEl) _effMonthEl.value = effDefaultMonth();
const _effPreviewBtn = $("eff-preview-btn");
if (_effPreviewBtn) _effPreviewBtn.onclick = previewEfficiency;
const _effDownloadBtn = $("eff-download-btn");
if (_effDownloadBtn) _effDownloadBtn.onclick = downloadEfficiencyCsv;

// ---- Live site mirror ----
// This deployment can be configured to read its data from the live Anvitech site
// (UPSTREAM_MONGODB_URI). When it is, say so loudly — an operator must never
// mistake this tab for production — and let an admin re-attach data they have
// detached by wiping it here.
async function loadMirrorStatus() {
  let status;
  try {
    const res = await fetch("/mirror/status");
    if (!res.ok) return;
    status = await res.json();
  } catch (e) { return; }        // cosmetic: never block boot on it
  if (!status.enabled) return;

  const badge = $("mirror-badge");
  if (badge) badge.hidden = false;
  const card = $("mirror-card");
  if (card) card.hidden = false;
  renderMirrorDetached(status.detached || []);
}

function renderMirrorDetached(detached) {
  const el = $("mirror-detached");
  if (!el) return;
  if (!detached.length) {
    el.innerHTML = `<p class="explainer">Everything is currently mirroring the live site.</p>`;
    return;
  }
  el.innerHTML = `<p class="explainer">These are no longer mirroring the live site, because they
      were deleted here. Re-attaching discards the local copy and shows the live data again.</p>`
    + `<ul class="absence-list">`
    + detached.map((d) => `<li><span>${escapeHtml(d.label)}</span>`
        + `<button class="ghost-btn" type="button" data-mirror-key="${escapeHtml(d.key)}">`
        + `Re-attach to live</button></li>`).join("")
    + `</ul>`;
  el.querySelectorAll("[data-mirror-key]").forEach((b) => {
    b.onclick = () => reattachMirror(b.dataset.mirrorKey, b.previousElementSibling.textContent);
  });
}

async function reattachMirror(key, label) {
  if (!confirm(`Re-attach ${label} to the live site?\n\n`
    + `Everything this duplicate holds for it is deleted and replaced by the live data. `
    + `This cannot be undone.`)) return;
  const password = prompt("Confirm your admin password to re-attach:");
  if (!password) return;
  try {
    const res = await fetch("/mirror/reattach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, password }),
    });
    if (!res.ok) {
      alert(res.status === 403 ? "Password confirmation failed." : "Could not re-attach.");
      return;
    }
  } catch (e) {
    alert("Could not re-attach.");
    return;
  }
  await loadMirrorStatus();
  await runPlan(false);          // the data underneath the plan just changed
}

// Boot: learn the role, restore the last view, then auto-load the current plan (no
// persist) so the schedule/Gantt/views populate without a Plan click.
(async function boot() {
  // "Still loading" bump for a cold Render instance waking up — cleared by
  // bootLoadingSuccess()/bootLoadingError() the moment the first /run response
  // (in runPlan()) lands, success or failure.
  bootLoadingTimer = setTimeout(() => {
    const el = $("boot-loading");
    if (el) el.textContent = "Still loading. The server may be waking up after a quiet period, "
      + "of inactivity, this can take up to a minute…";
  }, 5000);
  await initSession();
  loadMirrorStatus();
  window.addEventListener("hashchange", onHashChange);
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.addEventListener("click", (e) => { e.preventDefault(); showView(n.dataset.view, true); });
  });
  // Land on the hash's view, else the last view visited (defaults to Orders).
  const initial = VIEWS.includes(location.hash.replace(/^#/, "")) ? location.hash.replace(/^#/, "") : lastView();
  showView(initial, true);
  // Operators & shifts / Absences are read-only for the user role but still
  // visible (only their edit controls are admin-only) — load for both roles.
  loadAbsences();
  loadOperators();
  await runPlan(false);
  // A search may still be running (or have finished) from before a page reload.
  // Resume the progress display so a refresh mid-run never loses the work — the
  // Optimize card lives on the Schedule view and updates in place. Both roles:
  // the panel is read-only for a user but they still need to see a search is
  // running (2026-08-09 role-parity fix), and GET /optimize/status is role-open.
  try {
    const st = await (await fetch("/optimize/status")).json();
    if (st.state === "running" || (st.state === "done" && st.best)) pollOptimizeStatus();
  } catch (e) { /* status is cosmetic at boot */ }
})();
