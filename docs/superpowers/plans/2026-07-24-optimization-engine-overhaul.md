# Optimization Engine Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship the research-converged engine: consolidation fixed, the Judge as the single
arbiter (replacing the crude worst-order ceiling), the lateness DISTRIBUTION + doomed-order set
surfaced so the owner trusts every trade, and the auto-decided knobs (Order Priority removed).

**Architecture:** Production runs `scheduler="new"` (ppc_engine). Research (docs spec
2026-07-24-optimization-engine-overhaul-design.md) established: objective is at its search
ceiling; the real lever is structural (consolidation 10→1 ≈ 6% Judge win); the worst-vs-
distribution "conflict" was a broken metric — 24/57 orders are structurally DOOMED and the
raw-worst ceiling sacrificed 7 SAVABLE orders to hold one doomed order. Fix: let the Judge
(convex per-order by 4/10/20 bands + makespan) arbitrate, protect SAVABLE (on-time) orders
explicitly, surface the doomed set.

## Global Constraints
- Production engine = ppc_engine; the app maps its output to old `ScheduleEntry`. Keep that seam.
- The Judge (owner's values): per-order cost ≈0 up to 4 days late, convex ramp with a step at
  10 ("bad") and 20 ("very bad"); + modest makespan; utilization is a reported goal.
- SAVABLE = an order deliverable on time (or nearly); DOOMED = ≥10 days late under any sequence.
- Golden trace must stay green (classic engine untouched).
- Changes land on branch `optimization-engine-overhaul` (NOT main).
- The raw-worst ceiling (`worst_ceiling_days`, ppc `ceiling_days`/`ceiling_weight`, the
  `_auto_apply_result` max-late backstop) shipped to main 2026-07-24 is REPLACED here.

---

### Task 1: Consolidation is engine-decided (10 → 1)
**Files:** `engine/config.py` (default), `api/main.py` (force at planning boundary),
`web/index.html` + `web/app.js` (remove the control), `tests/` (new test).
- [ ] Change `consolidation_window_days` default 10 → 1 in `engine/config.py`.
- [ ] At the planning boundary (`api/main.py:_resolve_config`), force `consolidation_window_days=1`
      (engine-decided; a stale saved 10 is ignored) — mirror how overlap is engine-owned.
- [ ] Remove the "Consolidation window (days)" control (`web/index.html:155`) and its
      `web/app.js` read/set (`cfg-window`).
- [ ] Test: a Config built from a dict with `consolidation_window_days=10` resolves to 1 at the
      boundary; measured on the sample book, consolidation 1 is used.
- [ ] Full suite + golden green. Commit.

### Task 2: Judge-shaped objective (explicit 4/10/20 bands)
**Files:** `ppc_engine/config.py`, `ppc_engine/objective/objective.py`, `engine/optimizer.py`
(mirror), `tests/`.
- [ ] Reshape the per-order cost to the Judge: tolerance 4, convex ramp, + a step at 10 and 20.
      Parametrize via config (defaults = the measured-neutral values). Keep it numerically
      consistent between ppc and the optimizer mirror.
- [ ] Validate on the harness it is Judge-neutral-or-better vs current (research showed ≈neutral).
- [ ] Tests: per-order cost is 0 ≤4 days, jumps at 10 and 20; both score functions agree.
- [ ] Full suite + golden. Commit.

### Task 3: Replace the raw-worst ceiling with the Judge + savable protection
**Files:** `ppc_engine/config.py`/`objective.py` (remove `ceiling_days`/`ceiling_weight` barrier),
`engine/optimizer.py` (remove `CEILING_*`/`ceiling_breach`), `engine/config.py` (remove
`worst_ceiling_days`), `api/main.py` (`_start_optimize` stop injecting the ceiling;
`_auto_apply_result` replace the max-late backstop with a SAVABLE-order guard), `tests/`.
- [ ] Remove the ceiling barrier terms + `worst_ceiling_days` plumbing (revert the 2026-07-24
      ceiling machinery).
- [ ] SAVABLE-order apply guard in `_auto_apply_result`: apply the winner iff it improves AND no
      order that is ON-TIME in the incumbent becomes badly-late (≥10) in the new plan. (The Aug-8
      guarantee, correctly scoped to savable orders; doomed orders may drift.)
- [ ] Tests: (a) a plan that pushes an on-time order to ≥10 is BLOCKED; (b) a plan that lets a
      doomed order drift while reducing bad orders is ALLOWED; (c) the removed ceiling fields no
      longer appear in `_inputs_signature`.
- [ ] Full suite + golden. Commit.

### Task 4: Doomed-order detection + distribution surfaced in the panel
**Files:** `engine/optimizer.py` or a small new module (doomed detection + distribution),
`api/main.py` (attach to the optimize result + `/run`), `web/index.html`/`web/app.js` (render),
`tests/`.
- [ ] `distribution(lateness)` → counts per band (on-time/1-4/5-9/10-19/20+), worst, bad(10+).
- [ ] Doomed detection: an order is doomed if its best-achievable completion (critical-path from
      the plan start, ignoring contention) is already ≥10 days past due — a cheap, deterministic
      floor. (Surfacing, not used to gate the search.)
- [ ] Optimize panel: show the distribution for standard vs optimized (bands, not a raw count) +
      a "N orders cannot be delivered on time regardless — [list]" doomed callout.
- [ ] Tests: distribution buckets correct; a hand-built impossible order is flagged doomed.
- [ ] Full suite. Commit.

### Task 5: Remove the "Order priority" setting
**Files:** `web/index.html` (remove the fieldset lines 170-193), `web/app.js` (remove the reads),
`engine/config.py` (keep fields, set engine defaults slack + no window; drop from user surface).
- [ ] Remove the fieldset + app.js reads; the engine uses the recommended defaults.
- [ ] Test: a saved config missing these fields plans fine with the defaults.
- [ ] Full suite. Commit.

### Task 6: End-to-end validation + before/after
- [ ] Harness: current engine vs the overhauled engine across all fabricated books + the real
      book — report the distribution + Judge + doomed counts; confirm fewer bad(10+) orders and no
      savable order regressed.
- [ ] Full suite + golden green. Update the spec's progress log with the final numbers. Commit.
