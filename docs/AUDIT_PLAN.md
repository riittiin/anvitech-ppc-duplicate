# Audit plan — every feature, what it feeds, what could fail silently

Working document. One row per auditable feature: where it lives, what it derives from,
and the specific way it could be wrong **without anyone noticing**. We work through it
in order; status is updated as each is done.

The bug classes found so far, which tell us what to look for:

1. **Two derivations of one fact** — a feature computes its own answer instead of
   reading the shared one (Gantt 07-Sep vs delay report 04-Sep).
2. **Two moments** — a feature plans at a different time than the one on screen, and the
   plan is not stable across time (the plan clock).
3. **A different model** — a feature models the shop differently from the engine that
   built the plan (158 h of work outside the working window it believed in).
4. **Silent omission** — a list built from what *happened* rather than what *exists*, so
   an idle or unused thing disappears (20 staff shown as 19).
5. **Wrong source of truth** — reading the Excel where the app owns the data, or vice
   versa (operators are Settings-owned; machines are Excel-owned).

Status: `DONE` verified with evidence · `PART` partly covered · `TODO` not yet audited.

---

## A. Inputs — everything downstream inherits these

| # | Feature | Where | Derives from / feeds | Silent-failure risk | Status |
|---|---------|-------|----------------------|---------------------|--------|
| A1 | **Excel upload + merge into the order book** | Settings → Upload; `POST /upload`; `orderbook.merge_upload` | Excel SO sheet → `anvitech:orders`, keyed (SO#, item). Feeds *everything*. | A re-import silently changing (or failing to change) a field; intra-upload duplicates; an order merged twice under a whitespace-different key; delivery-date edits not landing | TODO |
| A2 | **Masters parsing** (machines, routings, calendar, holidays) | `engine/loaders.py`; `anvitech:masters` | Excel → machine list, routings, cycle times, working calendar. Feeds the plan and every capacity number. | Sheet-name/format drift silently dropping rows; time-unit coercion; a routing step silently skipped; provisional machines mis-registered | TODO |
| A3 | **Operator & shift table** | Settings → Operators & shifts; `GET/POST/PATCH/DELETE /operators`; `anvitech:operators` | **App-owned (Settings only).** Seeded once from the workbook, fossil after. Feeds Rule 6 crewing, Analytics, shift-wise, efficiency. | Seeding when it shouldn't; a machine token that no parser matches disqualifying someone silently; shift string not recognised | **PART** — Settings-is-the-source proven; machine-picker parity and seed-once not re-verified |
| A4 | **Operator absences** | Settings → Absences; `GET/POST/DELETE /absences`; `anvitech:absences` | Blocks operator time in every plan pass and contest; reduces Analytics capacity. | An absence silently ignored by the plan but shown in the UI; orphaned absence after an operator rename; date-boundary off-by-one | TODO |
| A5 | **Plan settings** | Settings; `anvitech:plan_config` | Overlap %, shifts, setup, split, flexibility → the plan and the optimizer. | A saved knob silently ignored by the new engine; UI showing a value the engine doesn't use; optimizer-owned knobs being hand-edited | TODO |

## B. The plan — the one centerline everything must read

| # | Feature | Where | Derives from / feeds | Silent-failure risk | Status |
|---|---------|-------|----------------------|---------------------|--------|
| B1 | **Rules 1–3** (consolidate → sort → priority) | `engine/rules/rule1..3` | Active SO lines → batch order. Feeds Rule 6. | Consolidation merging orders with different due dates; slack computed on a clock that doesn't match the machine (known simplification — quantify it) | TODO |
| B2 | **Scheduling engine** (Rule 6 / `ppc_engine`) | `engine/new_engine.py`, `ppc_engine/` | Batches + masters + operators → the schedule. **The single source every view reads.** | One-operator-per-machine-per-shift violations; an operator booked on a machine they can't run; double-booking; work scheduled outside working hours | **PART** — invariants exist in tests; not re-verified on the live book since the engine changes |
| B3 | **Plan clock + plan cache** | `api._resolve_config`, `_plan`, `anvitech:plan_start_floor` | Fixes *when* the plan starts; the cache serves one plan to all features. | The clock moving without a visible event; the cache serving a stale plan; the fingerprint missing an input | **DONE** (2026-08-07) |
| B4 | **Optimize** — deep search, apply, auto-trigger, freeze | `optimizer`, `optimize_service`, `POST /optimize*`, `anvitech:plan_priority`, `anvitech:frozen_ops` | Searches sequence × overlap × machine-set; applied ranks replay in every later plan. | Panel promising a plan the apply doesn't reproduce; freeze not actually pinning in-progress work; ranks replaying differently than searched; auto-apply gate letting a worse plan through | **PART** — before/after numbers unified; freeze + cloud parity not verified |
| B5 | **Cloud / Oracle contest workers** | `.github/workflows/optimize.yml`, `scripts/*_worker.py` | Same contest code, run off-box; result posted back. | A cloud result computed on different inputs than the app replays; shard merge losing candidates; a stopped run being presented as complete | TODO |

## C. Views of the plan — none may derive its own answer

| # | Feature | Where | Derives from / feeds | Silent-failure risk | Status |
|---|---------|-------|----------------------|---------------------|--------|
| C1 | **Orders tab** | View `orders`; `/run` `orders` + `expected_end` | Order book + plan → status, remaining qty, expected completion, late flag. | Status derived differently from the book; an order present in the book but absent from the plan with no explanation | **PART** — dates + qty verified; status derivation not |
| C2 | **Schedule table** (Rule 6 output) | View `schedule`; CSV download | The schedule, one row per op. | A row whose operator/time disagrees with the Gantt | **DONE** (422 ops verified) |
| C3 | **Machine-wise timeline + utilization** | View `schedule` tables; CSV | The schedule + machine clock. | Idle machines missing; idle-time maths using the wrong clock | **DONE** (2026-08-07) |
| C4 | **Shift-wise schedule** | `trace.rule6.shiftwise`; CSV | The schedule's per-shift segments. | Re-deriving operators instead of reading them; per-shift qty not summing; a shift label that contradicts its own times | **DONE** |
| C5 | **Gantt** | View `gantt`; `/gantt` | The schedule → per-order bars. | Bar times/operators disagreeing with the schedule; an order with no bars vanishing | **DONE** |
| C6 | **Analytics** | View `analytics`; `trace.analytics` | The schedule + capacity model. | Idle resources missing; capacity model differing from the engine; operator hours exceeding a shift | **DONE** (2026-08-07) |
| C7 | **Delay justification report** | `GET /delay-report.xlsx` | The plan → per-order wait attribution. | Building its own plan; its own working-hours model; dropping orders | **DONE** (2026-08-07) |
| C8 | **Validation report banner** | `/run` `report`; `_report_for_book` | Loader issues + book cross-checks + staffing gaps. | A real problem not surfaced; a ghost problem surfaced; rows nobody acts on | **PART** — staffing gaps added; the rest not re-verified |

## D. The feedback loop — what the floor puts back in

| # | Feature | Where | Derives from / feeds | Silent-failure risk | Status |
|---|---------|-------|----------------------|---------------------|--------|
| D1 | **Capture actuals** (Daily Entry) | View `entry`; `POST /actuals` | Punches → `anvitech:actuals` → remaining qty → the next plan. | A punch accepted but not reducing remaining qty; operator/process name not matching the plan's; precedence guard blocking a legitimate entry (or missing an illegitimate one) | TODO |
| D2 | **Rollback** | `POST /actuals/rollback` | Removes one punch, un-completes an order. | A rolled-back punch still counted somewhere; completion state left inconsistent | TODO |
| D3 | **"Done entering — update plan"** | `POST /optimize/done` | Recomputes the frozen set, runs an auto-applying contest daily. | Freeze not pinning what's physically running; the trigger silently skipping; the plan moving work already on a machine | TODO |
| D4 | **Operator efficiency report** | Settings (admin); `/efficiency`, `/efficiency.csv` | Punches + absences + cycle-time standards → monthly efficiency %. | **Prime suspect for silent omission** — an operator with no punches vanishing; no-standard punches skewing a person; shift text unmatched; attended time double-counted | TODO |
| D5 | **Order completion / archive** | `POST /orders/complete`, mark-complete on a punch | Moves an order out of planning. | A completed order still consuming capacity, or an active one silently excluded | TODO |

## E. Cross-cutting

| # | Feature | Where | Derives from / feeds | Silent-failure risk | Status |
|---|---------|-------|----------------------|---------------------|--------|
| E1 | **Login, roles, permissions** | `api/auth.py`, `gatekeeper` | Admin vs user. | A user-role account reaching an admin action; a control hidden in the UI but open on the API | TODO |
| E2 | **Persistence / store** | `engine/storage.py` (Mongo/Upstash/local) | Every durable key. | A key silently failing to write; field-name encoding; a partial write leaving inconsistent state | TODO |
| E3 | **Order deletion / clear** | `POST /orders/delete`, `/orders/clear` | Removes orders permanently. | Deleting the wrong (SO#, item); leaving actuals orphaned | TODO |
| E4 | **Commitment lanes (hidden)** | `COMMITMENT_FEATURE_ENABLED = False` | Dormant; engine machinery still live. | The flag off in the UI but the machinery still steering the optimizer | TODO |

---

## Suggested order

Follow the data: if an input is wrong, every audit downstream is measuring the wrong
thing.

1. **D4 efficiency report** — the class we just fixed almost certainly lives here too
2. **A3/A4/A5 inputs** — operators, absences, settings actually reaching the plan
3. **D1/D2/D3 the feedback loop** — punches, rollback, freeze: the daily path
4. **A1/A2 upload and masters** — the foundation everything inherits
5. **B2/B4/B5 engine and optimize** — invariants and cloud parity
6. **C1/C8 remaining views** — order status, validation banner
7. **E cross-cutting** — auth, store, deletes, the dormant lane machinery

## Method (what "audited" means here)

Not reading the code and pronouncing it fine. For each feature:

- drive the **real app** on the **real workbook** with the **production config**;
- compare against an **independent** calculation, not the code's own helper;
- compare **sets, not intersections**, so an omission cannot hide;
- prove the check is real by running it against the **pre-fix code** and watching it fail;
- state plainly what the audit did **not** cover.
