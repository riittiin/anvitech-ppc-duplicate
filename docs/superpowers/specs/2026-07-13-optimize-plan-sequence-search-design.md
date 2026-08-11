# Anvitech PPC Engine — "Optimize Plan" (Sequence Search) Design

**Date:** 2026-07-13
**Status:** Approved & built (2026-07-13)
**Related:** [`RULES.md`](../../../RULES.md) (Rules 3, 6, 8),
the order-commitment design (spec since pruned as superseded),
memory `sequence-optimizer-findings`

---

## 1. Problem

The owner's two goals, verbatim: **(1) reduce the gap between the SO delivery date
and the expected date, (2) reduce the total days to complete the workbook (~43
today)** — using software only: no new machines, operators, or Excel edits.

Rule 6 is a **greedy, single-pass, non-delay scheduler**: it builds exactly one plan,
and at every instant the earliest-ready operation claims the machine. Measured on the
real book (Test5, live settings, plan start 2026-07-11):

- Orders spend **723 order-days queueing between steps** — 74% of it waiting for
  CNC/VMC machines. Tail orders wait 24–45 days holding only 1.5–11 days of work.
- The busiest machine (VMC2) carries only ~34 calendar days of work → the 43-day
  plan has **~8+ days of theoretical headroom**; every other resource is 2–58% used.
- The plan is **chaotic**: config levers (overlap %, expedite, split threshold,
  priority metric) don't stack — combinations often plan *worse* than single levers.
  Even granting every CNC/VMC step full same-type machine alternatives improved
  makespan but **worsened** total lateness. The binding constraint is the
  **sequencing policy**, not capacity and not master data.

**The lever:** the order in which batches claim machines is worth ~12 days of
makespan by itself (random sequences span 40.6–52.6 days). One full plan evaluates
in <1 s, so the software can afford to **search thousands of sequences and keep the
best plan** instead of trusting one greedy pass.

**Measured result** (925 evaluations, ~9 min on a laptop, same book/settings/start):

| | Live plan | Optimized sequence |
|---|---|---|
| Days to finish the workbook | 42.5 ("43 days") | **38.7** |
| Late orders | 53 / 65 | **47 / 65** |
| Total late-days (Σ delivery gaps) | 1,026 | **752 (−27%)** |
| Worst order lateness | 49 d | **41 d** |

**Generality (owner requirement — this is a rolling-book feature, not a Test5
sequence):** on 6 perturbed books (20–30% of orders removed; plan start shifted 1–2
weeks) a 250-eval search beat the Rule-3 baseline on late-days in **6/6** scenarios
(−9%…−33%) and on makespan in 5/6. The *method* generalizes; each found sequence is
disposable and recomputed from whatever the book holds when the button is pressed.

## 2. What ships (owner-visible behaviour)

A new admin-only **Optimize** button beside **Plan**:

1. Admin clicks **Optimize**, picks **Quick** (~150 tries) or **Deep** (~1,000
   tries). The search runs in the background on the **current order book snapshot**;
   the UI shows live progress: *"tried 214 plans — best so far: 39.1 days,
   760 late-days (baseline: 42.5 / 1,026)"*.
2. When done (or any time mid-run), the panel shows a **before/after table**
   (total days, late orders, total late-days, worst order). Admin clicks **Apply**
   or **Discard**.
3. **Apply** saves the winning priority order durably. From then on, **every Plan
   replays it** — for the admin, for read-only users, and for the auto re-plan
   after daily actuals. The schedule, Gantt, Analytics and Orders tabs all just
   reflect the better plan; no other UI changes.
4. As the book rolls forward (new uploads, completions), a banner appears when
   orders exist that the saved optimization has never seen: *"N orders added since
   the last optimization — re-optimize for the best plan."* New orders still plan
   fine meanwhile (they keep their normal Rule-3 priority slot).
5. **Clear optimization** (admin) reverts to the pure Rule-3 order.

**Promises stay sacred.** Committed/Urgent orders are planned exactly as today in
the protected first pass — the optimizer only re-sequences the **Open** pass into
the leftover capacity. A found plan can never move a promised date.

**Doing nothing changes nothing.** With no saved optimization, every plan is
byte-identical to today (golden trace untouched).

## 3. Architecture

```
                       ┌────────────────────────────────────────────┐
 admin clicks Optimize │  POST /optimize  (snapshot book + config)  │
                       │      └─▶ background thread:                │
                       │          engine/optimizer.py  search loop  │
                       │          (permute → replay Rules 1/2/3/6 → │
                       │           score → keep best)               │
                       │  GET /optimize/status   (live progress)    │
                       │  POST /optimize/apply   (persist ranks)    │
                       └────────────────────────────────────────────┘
                                          │ anvitech:plan_priority (durable)
                                          ▼
 every Plan:  run_forward(…, priority_rank=ranks)
              = Rules 1 → 2 → 3 → [reorder ranked batches] → 6
```

- **`engine/optimizer.py` (new, pure).** `optimize(so_lines, config, masters, *,
  reserved=None, budget_evals, seed, on_progress=None) -> OptimizeResult`. It calls
  the **unchanged** Rules 1→2→3 once to get batches, then repeatedly permutes the
  batch order and replays the **unchanged** Rule 6 — no rule logic is duplicated
  (design principle #2). Returns the best sequence (as order-pair ranks), its
  metrics, the baseline metrics, and evals used.
- **`run_forward(…, priority_rank=None)`** gains one optional argument: after Rule 3,
  batches whose member orders carry a rank are reordered among the *slots they
  already occupy*; unranked batches keep their Rule-3 positions. `None` (default and
  all existing callers) is byte-identical to today. The reorder is recorded in the
  Rule-3 trace notes (*"optimized sequence applied — saved <date>, covers X of Y
  batches"*) so the tabs stay honest.
- **`api/main.py`**: three admin endpoints + status; `_plan` loads saved ranks and
  passes them to the open pass (or the single pass when nothing is committed).

## 4. The search (v1 algorithm, all measured on real data)

- **Score** (lower is better): `total_late_days + 10 × makespan_days`, computed from
  the replayed schedule vs each order's SO delivery date. Encodes both owner goals,
  delivery gaps dominant.
- **Seeds:** the Rule-3 order, SPT (least total work first), ATC (due-date pressure ÷
  work — the best single-pass rule found: 927 vs 1,257 late-days), and 3 fixed-seed
  shuffles. Best seed starts the climb.
- **Moves:** random insertion (50%), pairwise swap (30%), 3-batch block move (20%);
  accept improvements and sideways moves; after 400 non-improving tries, restart
  from the best with a small shake ("kick").
- **Deterministic:** fixed RNG seed + eval-count budget (not wall-clock) → the same
  book, config and budget always yield the same result, on any machine. Quick = 150
  evals, Deep = 1,000 (most of the gain lands in the first ~200).
- **Two-pass books:** the protected pass is planned first exactly as today; its
  reservations are fed to the search, which evaluates only Open-pass sequences
  against them (Rule 6's existing `reserved=` argument).

## 5. Persistence & the rolling book

- **Stored artifact** (`anvitech:plan_priority`, via `book_store`): a rank per
  **(SO No, Item Code)** pair — the book's universal key — plus metadata: saved-at,
  the covered key set, baseline/optimized scores, budget, seed. A batch's rank =
  the minimum rank of its member orders (consolidation-safe: batches merge/shrink
  as orders complete, ranks survive).
- **Unranked orders** (uploaded after the last Optimize) keep their natural Rule-3
  slot — a brand-new urgent-delivery order is *not* pushed to the back. Ranked
  batches reorder only among their own positions.
- **Staleness banner:** Plan responses include `optimize_meta` (saved-at, covered
  count, uncovered count); the web UI shows the re-optimize hint when uncovered > 0.
- **Job lifetime:** the search runs in one background thread in the FastAPI process
  (Render free tier = 1 worker). Only one job at a time (409 if one is running).
  If the dyno restarts mid-run the job dies harmlessly — status returns to idle;
  the applied ranks are durable and unaffected. Status polling keeps the dyno awake
  for the duration of a run.
- **Render speed caveat (honest):** the free tier CPU is slower than a laptop —
  Quick ≈ 2–4 min, Deep ≈ 15–25 min there. Quick already captures most of the gain;
  Deep is for a weekly "settle the plan" run. Budgets are eval-counted, so results
  are identical regardless of where they run.

## 6. API

| Endpoint | Role | Behaviour |
|---|---|---|
| `POST /optimize` `{budget: "quick"\|"deep"}` | admin | snapshot book+config, start the thread; 409 if running |
| `GET /optimize/status` | any logged-in | `{state: idle\|running\|done\|failed, evals, budget, baseline: {makespan, late_orders, late_days}, best: {…}, elapsed_s}` |
| `POST /optimize/apply` | admin | persist the last completed run's ranks + metadata; 409 if none completed |
| `POST /optimize/clear` | admin | delete saved ranks (back to pure Rule 3) |

`/run` responses gain `optimize_meta: {active: bool, saved_at, covered, uncovered}`.
All follow the existing `require_admin` / CSRF / session middleware.

## 7. UI

- **Allocate-to-machines tab, next to Plan** (admin only): `Optimize` button →
  inline panel: Quick/Deep radio, Start, progress line (polls status every 3 s),
  before/after comparison table, **Apply** / **Discard**.
- **Banner** (all roles) when an optimized order is active: *"Optimized plan active
  (saved 13-07-2026)"* + *"N new orders since — re-optimize"* when stale; admin
  also sees **Clear**.
- No other tabs change — they render whatever plan `_plan` returns.

## 8. Edge cases

- **Empty book / all completed** → Optimize disabled (nothing to sequence).
- **All orders committed** → nothing in the Open pass; Optimize reports "all orders
  are promise-protected — nothing to optimize" and does not run.
- **Saved ranks cover zero current batches** (book fully rolled over) → ranks are
  inert (everything unranked = pure Rule 3); banner suggests re-optimizing.
- **Optimizer finds nothing better** → status shows best = baseline; Apply is
  allowed (idempotent) but the UI says "no improvement found".
- **Actuals punched between Optimize and Apply** → Apply stores ranks, not the plan;
  the next Plan replays them against the *new* remaining quantities. Ranks are
  advisory ordering, never quantities/dates, so this is safe (and it's exactly the
  weekly rolling case the feature exists for).
- **Config change after Apply** (e.g. overlap %) → ranks still apply; the banner
  shows the optimization's saved-at date so the admin can re-run after big setting
  changes.

## 9. Testing (test-first, per repo convention)

- `tests/test_optimizer.py` — pure-engine: determinism (same inputs+seed+budget →
  identical result), score never worse than the Rule-3 baseline, respects
  `reserved` intervals, tiny budgets for speed (sample workbook).
- `tests/test_priority_rank.py` — `run_forward(priority_rank=…)`: `None` is
  byte-identical (golden safety), ranked reorder among-occupied-slots, unranked
  keep Rule-3 slots, consolidation min-rank behaviour.
- `tests/test_optimize_api.py` — role gating (user 403), 409 double-start, status
  lifecycle, apply/clear persistence round-trip, `optimize_meta` in `/run`,
  two-pass books optimize only the open pass.
- **Golden trace: untouched** (no saved ranks in tests that exercise it).
- Manual verification: drive the real UI in a browser; run Quick on the real
  Test5 book locally and confirm the before/after table matches the harness numbers.

## 10. Out of scope (deliberate)

- Changing Rule 3's default metric (ATC stays a seed inside the search only).
- Optimizing the protected pass or promised dates (promise protection is absolute).
- Machine-assignment search (which machine, not just which order) — a possible v2;
  v1 keeps Rule 6's machine choice untouched.
- Auto-optimize on every upload (owner presses the button; predictable behaviour).
- Parallel/multi-core search (Render free tier is single-core; determinism first).
