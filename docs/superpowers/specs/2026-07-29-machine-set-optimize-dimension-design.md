# Machine-set as an Optimize dimension — design

**Date:** 2026-07-29
**Status:** approved (owner), ready for implementation plan
**Scope:** the Optimize contest only (plus the thin persist/replay path the tuned
overlap already uses). No change to the rules, the scheduler, capture, freeze, or the
committed-promise cap.

## Goal

Let the Optimize contest **choose the machine set** for each plan, comparing
**Allotted-only** against **Allotted + Suggested (union)** as a third search dimension,
alongside the two it already sweeps (job sequence and overlap %). The contest keeps the
single best `(machine-set, overlap, sequence)` combination by the current score, and the
winning machine-set persists and flows into the everyday plan exactly like the tuned
overlap does today.

## Background: what the optimizer compares today (verified in code)

The new (production) engine's contest sweeps **two** dimensions:

1. **Job sequence** — a multi-start hill-climb over batch orderings
   (`engine/optimizer.optimize` / `engine/new_engine.optimize_sequence`).
2. **Overlap %** — a grid of candidate values
   (`optimize_service.CLOUD_NEW_OVERLAP_CANDIDATES = (60,65,70,74,78,80,82,84,86,88,90,93)`),
   run as a contest: for each overlap value, a full sequence search; the best
   `(overlap, sequence)` wins by `total_late_days + 10 × makespan_days`.

It does **not** vary the machine set. Every candidate plan uses the **Allotted** machine
for each step. The machine options are baked at *load* time:
`ppc_engine/loaders/masters_loader.load_routings(..., flexible_machines)` builds each
in-house machining/manual/inspection op's `machine_options` as either the Allotted
machine only (`flexible_machines=False`, the default) or the deduped union of Allotted +
Suggested (`flexible_machines=True`). The scheduler
(`ppc_engine/scheduler/flow_scheduler._place_operation`) already picks, among an op's
`machine_options`, the machine that finishes that op earliest — so it *would* load-balance
onto Suggested machines if they were in the options. Today `engine/new_engine._new_masters`
calls `load_all(io.BytesIO(raw))` with no flag → `flexible_machines=False`, and a
full-codebase grep confirms `flexible_machines=True` is never passed anywhere.

### The dedup rule (owner-specified)

The union is `dict.fromkeys(allotted_options + suggested_options)` — Allotted first,
Suggested appended, duplicates dropped. Examples:
- Suggested {CNC5, CNC7}, Allotted CNC7 → {CNC7, CNC5}
- Suggested {CNC7}, Allotted CNC7 → {CNC7}
- Suggested {CNC1, CNC3}, Allotted CNC7 → {CNC7, CNC1, CNC3}

This is exactly what `load_routings(..., flexible_machines=True)` already produces
(`masters_loader.py:184-189`). No new parsing logic is needed.

### Test8 measurement that motivates this (plan start 2026-07-29, new engine)

| | makespan | late-days | worst | late orders |
|---|---|---|---|---|
| Single plan, Allotted only | 54.6 d | 1738 | 69 d | 55 |
| Single plan, Allotted + Suggested | 52.6 d | 1573 | 66 d | 54 |
| Optimized (400 evals), Allotted only | 49.5 d | 1539 | 56 d | 64 |
| Optimized (400 evals), Allotted + Suggested | 55.5 d | 1497 | 54 d | 66 |

At a fixed sequence the union clearly helps (superset of choices; 33 ops re-placed onto a
Suggested machine). Through the optimizer the two trade off (union: fewer late-days and a
tighter worst order, but longer makespan). Because the trade-off depends on the book, the
right answer is to let the **contest decide per book** rather than hard-picking one — which
is what this feature does. Scoring is unchanged (option 1): the owner values the current
"distribute the lateness" behavior, so `total_late_days + 10 × makespan_days` stays.

## Design

### 1. One new config field

`engine/config.py`: add `flexible_machines: bool = False`.

- Default `False` = today's behavior, byte-identical.
- Round-trips in `to_dict`/`from_dict` (default `False` when missing).
- Folded into `_inputs_signature` (plan-shaping, like `overlap_percent` /
  `consolidation_window_days` / `committed_promise_slack_days`) so a change to the applied
  machine-set flags an optimization `inputs_changed` in the staleness banner.
- Validation: it's a bool; no range check.

This field is **owned by the optimizer**, not the user — it is set only by applying an
Optimize result (never hand-edited), exactly like the tuned `overlap_percent`.

### 2. `_new_masters` becomes flexibility-aware

`engine/new_engine._new_masters(flexible: bool)`:
- Load `ppc_load_all(io.BytesIO(raw), flexible_machines=flexible).masters`.
- Cache keyed by `(sha256(workbook), flexible)` so both machine-sets coexist in cache.
- Callers pass the flexibility explicitly:
  - `run(batches, config, masters, ...)` → `config.flexible_machines`.
  - `optimize_sequence(...)`, `tune(...)`, `sweep_optimize(...)` → the flexibility of the
    config copy the contest hands them (see §3).

The everyday plan therefore loads whatever machine-set the applied config carries. This is
the single wiring point that makes the winner "flow into the everyday plan."

### 3. The contest gains an outer machine-set loop

Machine-set is a **second, orthogonal dimension** — it multiplies the existing
`overlap × sequence` contest rather than replacing the overlap knob. The overlap "knob"
abstraction (`optimizer.knob_for`, `SweepResult`) is unchanged.

**Cloud contest** (`optimize_service.run_contest`): wrap the existing overlap-contender
loop in an outer loop over `machine_set ∈ (False, True)`. For each machine-set, build the
masters at that flexibility (`prepare_contest` loads per-flex; the union is a superset, so
loading twice is cheap) and run the full overlap × sequence search. Keep the single global
best across both passes. The outer loop is **hardcoded `(False, True)`** — the contest
always tries both, regardless of the config's current `flexible_machines` value.

**Local fallback** (`new_engine.sweep_optimize` → `tune`): run the golden-section
overlap+sequence `tune` twice, once per machine-set (config copy with
`flexible_machines` set), and keep the better by score.

**Winner record.** `SweepResult` (and the contest result dict) gains a `flexible_machines`
field carrying the winning machine-set. Winner selection compares plans across both passes
by the existing score — no scoring change.

**Per-pass config copies.** Each pass uses `replace(config, flexible_machines=flex)` (and
the existing per-overlap copy inside), mirroring exactly how overlap is already swept via
config copies. Everything downstream (`run`, the search, `_new_masters`) reads the copy.

### 4. Apply persists both knobs; the plan reproduces the winner

`api/main._optimize_apply` (and the auto path via `_finalize_optimize`): when a plan is
applied, persist **both** the winning `overlap_percent` **and** the winning
`flexible_machines` into the saved plan config (`book_store.save_plan_config`). Today it
already persists the overlap at `api/main.py:1675-1683`; this adds one field beside it.

`api/main._plan` reads `config.flexible_machines` and (via `run()` → `_new_masters`) loads
masters at that flexibility, so the schedule, Gantt, and analytics everyone sees reproduce
the optimized winner. `_incumbent_metrics` and `_metrics_for_ranks` go through the same
config, so the auto-apply gate (worst-order + committed-promise) compares best vs incumbent
on a consistent machine-set basis.

### 5. Settings shows it read-only

`web/index.html` + `web/app.js`: a read-only line in the Scheduling settings group,
alongside the read-only overlap line — e.g.
*"Machine set: Allotted + Suggested (chosen by Optimize)"* or *"Allotted only"* — sourced
from the plan response's config echo. The user can see which set the current plan uses but
cannot hand-edit it (the optimizer owns it).

## What stays untouched

- The 3 rules, the scheduler, capture, freeze, the committed-promise cap, operator
  logic/absences/rotation — no behavior change.
- The classic/flow engines and the golden test — the flag is new-engine only. The classic
  engine keeps its own machine selection (`rule6_allocate._resolve_candidates` driven by
  `split_parallel`); it ignores `flexible_machines`.
- With `flexible_machines=False` (the default until the optimizer applies a union winner),
  every plan is byte-identical to today.
- Frozen in-progress ops stay pinned to their real machine in **both** passes — they are
  pre-placed before the scheduling loop, independent of an op's `machine_options` — so the
  floor's running work is never re-routed by this feature.

## Edge cases (each gets a regression test)

1. **Frozen op on a Suggested machine during the Allotted-only pass.** A previous union
   plan may have placed a now-in-progress op on a Suggested-only machine. When the next
   contest runs its Allotted-only pass, that machine is not in the op's options — but frozen
   ops are pre-placed regardless of options, so the pin must still hold. Verify it does.
2. **Blank Allotted step.** With `flexible_machines=False`, options already fall back to
   Suggested; with `True`, options are the union. A step with blank Allotted and multiple
   Suggested therefore already load-balances today — confirm the union pass doesn't regress
   it and the Allotted-only pass matches today.
3. **Cloud == local parity.** Both must sweep both machine-sets identically (the
   byte-identical-contest principle): a cloud run and a local run of the same book choose the
   same winner.

## Testing

- **Byte-identical guard:** `flexible_machines=False` → new-engine plan identical to today;
  golden (classic) green. A book where the union never wins applies an identical result.
- **Config round-trip + `_inputs_signature`:** `flexible_machines` survives to_dict/from_dict
  and changes the inputs signature (staleness flag) when it differs.
- **`_new_masters` cache:** loading `False` then `True` for the same workbook returns
  different option sets (not a stale cache hit).
- **Contest engages the union:** on a crafted/Test8 book, at least one op is scheduled on a
  Suggested machine under the union pass, and the contest is *able* to select the union when
  it wins the score.
- **Apply persists both knobs; `_plan` reproduces the winner** (dates match the applied
  result's expected_end).
- **Auto-apply gate consistency** under a union winner (incumbent and best scored on their
  own machine-sets; committed/worst gates behave).
- **The three edge cases above.**
- **Test8 re-measurement:** Allotted-only contest vs the machine-set-aware contest — report
  makespan / late-days / worst / bands so the owner sees the real before/after.

## Cost

The contest roughly doubles (both machine-sets swept): cloud deep search ~15 → ~30 min;
local fallback ~1,000 → ~2,000 plans. Owner-approved. If time ever becomes a concern, the
overlap grid for the union pass could be pruned later — out of scope here (full grid × 2).
