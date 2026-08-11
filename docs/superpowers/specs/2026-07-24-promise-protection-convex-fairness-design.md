# Promise Protection via a Convex Fairness Term — Design

**Date:** 2026-07-24
**Status:** Approved (brainstorming) — pending spec review
**Author:** Claude + owner
**Supersedes / relates to:** the discarded 2026-07-16 "promise ceiling" hard veto
(Phase 2R); the current `ppc_engine` `λ·max_tardiness` fairness guard.

## The incident

On a live Thursday auto-optimize, an order **due and promised for 8 August** came out
of the re-optimization finishing **23 August** — a ~15-day slip the optimizer *chose*,
not one the floor forced. The owner discovered it only by hand-comparing before/after
expected dates. A self-inflicted 15-day miss on a promised order damages Anvitech's
reputation and must not happen when it is avoidable.

## Root cause

Production runs `scheduler="new"` (`ppc_engine`). Its sequence search
(`ppc_engine/optimize/memetic.py`) scores every candidate job order with the one
objective in `ppc_engine/objective/objective.py`:

```
score = total_tardiness_days            # Σ over orders of max(0, days late)
      + λ · max_tardiness_days          # λ = 30, the "fairness" guard
      + w · makespan_days               # w = 0.1
```

The `λ·max_tardiness` term was added *specifically* to stop a savable order being
pushed dramatically late. **But it only protects the single worst order.**
`max_tardiness` is the lateness of the one most-late order in the whole plan. When some
*other* order is already later than our Aug-8 order (very common — the book routinely
carries structurally-impossible orders that are 20-40 days late from a mid-cycle start),
pushing Aug-8 from on-time to 15-days-late **does not raise `max_tardiness`** (15 < 20),
so the guard is blind to it. Meanwhile `total_tardiness` is a flat sum: +15 days on one
order scores identically to +1 day on fifteen orders. Nothing in the objective objected
to sacrificing Aug-8 for an aggregate gain.

**In one sentence: the fairness term protects rank-1 worst and leaves rank-2, rank-3, …
fully exposed.**

The same slip-neutral aggregate is mirrored in `engine/optimizer.py`
(`score = total_late_days + 40·makespan_days`), which is what the overlap contest
(`optimize_service.py:246`) and the Thursday auto-apply "strictly better" gate
(`api/main.py:_auto_apply_result`) use to pick and accept plans. So even the acceptance
gate that applied this plan judged it purely on aggregate.

## Goals

1. The optimizer must **not push an order far past its due date when a better sequence
   exists** — protecting *every* order, not just the current worst.
2. When a slip is **genuinely unavoidable** (the floor fell behind and no sequence hits
   the date), the optimizer must still **report the honest new date** — the fix fights
   only *avoidable* damage.
3. Preserve the hard-won aggregate quality (today ≈ 72.7 d makespan / ~1528 late-days on
   the live book) — the fix must not blow up total late-days or makespan.
4. Give the owner **visibility** each Thursday into which orders moved later, so slips
   (avoidable or not) can be managed with customers proactively.

## Non-goals

- **No hard veto / infeasibility gate.** The 2026-07-16 "promise ceiling" made most
  plans illegal (zero-slack promises collapse the feasible region) and measured ~30%
  worse. This design is a *soft* penalty only — the search never becomes infeasible.
- **No manual marking.** Protection applies to every order against its own SO delivery
  date; the owner chose this over Committed/Urgent-only tiers. `commitment` /
  `promised_date` stay informational and untouched.
- **No change to the three basic rules or the scheduler mechanics.** Only the *objective*
  (which sequence is "best") and reporting change.

## Design

### Part 1 — Convex, per-order fairness in the `ppc_engine` objective (primary lever)

Replace the "protect only the worst" term with one that penalizes **each order's**
lateness on an **accelerating (convex) curve** — the "accelerating pain" the owner
chose. Per order, tardiness `t_i = max(0, completion_date − due_date in days)`. Define a
per-order severity:

```
severity_i = min(CAP, ( max(0, t_i − T) )²)
severity   = Σ over orders of severity_i
```

- **`T` (tolerance days):** the first `T` days of lateness cost nothing extra (a small
  slip is normal/recoverable — only `total_tardiness` counts them). Beyond `T`, cost
  accelerates.
- **squared beyond `T`:** a 15-day slip costs vastly more than fifteen 1-day slips, so
  the optimizer strongly prefers spreading small delays over dumping a big one on any
  single order — and this now protects *every* rank, not just the worst.
- **`CAP` (per-order ceiling):** a genuinely-impossible order (can never hit its date
  from any sequence) cannot hijack the search into a lost cause at the expense of the
  savable orders. Past the cap, extra lateness on a doomed order stops dominating.

New objective:

```
score = total_tardiness_days
      + μ · severity                    # NEW: convex, every order, capped
      + w · makespan_days               # unchanged
      + (λ · max_tardiness_days)        # see "λ decision" below
```

**λ decision (settled by measurement):** the convex sum generalizes the max term (max is
its degenerate ∞-norm extreme). Default intent is to **replace** `λ·max_tardiness` with
`μ·severity`. The tuning sweep (below) decides whether retaining a small `λ·max` as an
extra cheap guard on the absolute worst measurably helps; if not, it is removed. Either
way the outcome is a measured dominance point, documented in a code comment like the
existing `MAKESPAN_WEIGHT` note.

**Why this satisfies both goals automatically.** If a big slip is *avoidable*, some other
sequence has a lower `severity`, so the search finds and prefers it. If it is
*unavoidable*, every candidate carries that `severity_i`, the term is a constant the
search cannot escape, and the optimizer simply returns the honest date. The convex
penalty only ever moves the plan away from *avoidable* damage.

**Where it lives.**
- `ppc_engine/objective/metrics.py` — `compute_metrics` already returns
  `lateness_by_order`; add a computed `severity_days` field (needs `T`, `CAP` from
  config). Keep `max_tardiness_days` for reporting regardless of the λ decision.
- `ppc_engine/objective/objective.py` — `score()` adds `μ · metrics.severity_days`.
- `ppc_engine/config.py` — new weights beside `fairness_weight` / `makespan_weight`:
  `severity_weight` (μ), `severity_tolerance_days` (T), `severity_cap` (CAP), each with a
  measured-default comment.

### Part 2 — Mirror the term in `engine/optimizer.py` (acceptance parity)

The overlap contest winner-pick and the Thursday auto-apply gate score plans through
`engine/optimizer.py` `score()` / `plan_metrics()` in the "old space". If only the
`ppc_engine` objective changes, the search would protect Aug-8 but the contest/auto-apply
gate could still wave through an aggregate-better-but-reputation-worse plan. So mirror the
same convex term here:

- `engine/optimizer.py:plan_metrics` — it already computes each order's `gaps`/`late`
  internally; add a `slip_severity` field using the same `T`/`CAP`.
- `engine/optimizer.py:score` — add `μ · metrics["slip_severity"]` alongside the existing
  `total_late_days + 40·makespan_days`.
- The `T`/`μ`/`CAP` constants live beside `MAKESPAN_WEIGHT` (module constants, matching
  the codebase's "measured constant" convention), kept numerically consistent with the
  `ppc_engine` config values.

No new callers, no new machinery, no signature changes to the contest/apply/replay path.
Both surfaces (search + acceptance) now judge plans by the same reputation-aware yardstick.

### Part 3 — Thursday "what moved" transparency note

After each auto-optimize apply, extend the existing one-line auto-note
(`book_store.save_auto_note`, surfaced on `/run`'s `auto_note` and the Orders tab) to
name the orders that now finish **materially later than the plan that was on screen
before this run**.

**Data flow (no new persisted state).** At apply time in
`api/main.py:_auto_apply_result` we already have both plans on today's book:
- the **incumbent** schedule (current applied ranks replayed — `_incumbent_metrics`
  already builds it via `_all_lines_schedule`), and
- the **new** schedule (the winning ranks replayed via `_all_lines_schedule`).

Compute each order's expected completion date from both schedules (the same
`e.end`-per-`so_ref` expected map `plan_metrics` already derives), diff them, and list
orders whose new expected date is later than the incumbent's by more than a small
threshold (e.g. `> N` days — a config/module constant). The note then reads, e.g.:

> "Plan auto-re-optimized 11:03: 1490 late-days (was 1528), overlap 80 → 85.
> ⚠ 2 orders now finish later than before: SO123-01 +6d (→ 14-Aug), SO440-02 +3d."

If nothing moved later, the note keeps its current form (no warning line). This turns a
silent surprise into a heads-up the owner can act on. It is **schedule-neutral** — pure
reporting, computed from schedules that already exist at apply time.

## Verification & tuning plan (first-class, not an afterthought)

The codebase treats objective weights as *measured dominance points, not taste*
(`MAKESPAN_WEIGHT = 40` — "re-measure before moving it"). The new `T` / `μ` / `CAP` get
the same rigor:

1. **Reproduce the failure first.** Load the real book at (or a crafted reproduction of)
   the state that produced Aug 8 → Aug 23, run the *current* optimizer, and confirm the
   code reproduces a plan that avoidably pushes a savable order far past its due date.
   No fix is trusted until the bug is seen failing in a test.
2. **Sweep `T` × `μ` (× `CAP`)** on the real book. For each combination measure: **worst
   single-order slip on savable orders**, **total late-days**, and **makespan**. Choose
   the point that eliminates the avoidable big slips **without** regressing aggregate
   quality (guardrail: total late-days and makespan stay within a small tolerance of
   today's ≈ 1528 / 72.7 d). Settle the λ-retention question here.
3. **Regression test — the Aug-8 pin.** A crafted book where a savable order *can* be
   protected must come out protected; assert the worst-savable-order slip is bounded.
   This makes the blind spot impossible to silently reintroduce.
4. **Consistency test.** `ppc_engine` objective and `engine/optimizer` score agree on
   which of two hand-built plans is better under the convex term.
5. **Transparency test.** Given two schedules where one order moves later, the note lists
   exactly that order with the correct delta and new date; when nothing moves later, no
   warning line appears.
6. **Full suite green** (`pytest`, ~508 tests). **Golden trace unchanged** — the classic
   engine's rule output is untouched; only the optimizer's *choice of sequence* and the
   note text change. Existing `ppc_engine` objective tests updated to the new score shape
   with justified expected values.

## Risks & mitigations

- **Re-collapsing the search (the July failure).** Mitigated by construction: soft
  penalty, never a feasibility gate; `CAP` prevents doomed orders from dominating.
- **Aggregate regression.** Mitigated by the tuning guardrail (step 2) — a combination
  that improves worst-slip but worsens totals beyond tolerance is rejected.
- **Weights drift with data.** Documented as measured constants with re-measure notes; the
  Aug-8 regression test guards the *behavior* even if data shifts.
- **Two score functions diverging over time.** Kept numerically consistent and covered by
  the consistency test (step 4); a comment in each points at the other.

## Measurement result (2026-07-24, real book Test5, 71 orders)

Measured OFF (severity_weight 0 = the old max-only behaviour) vs ON (the shipped
defaults tol 2 / weight 2 / cap 30) on identical inputs, seed, and budget (the
same new-engine sequence search; measured at the engine's default overlap, so the
*absolute* numbers differ from the tuned-overlap production plan, but the OFF-vs-ON
contrast on identical inputs is the valid signal):

| Metric | OFF (old) | ON (default) |
|---|---|---|
| Total late-days | 1596 | **1451** (−9%) |
| Orders >14 days late | 56 | **44** |
| Worst single order | 46 d | 61 d |

Per-order OFF→ON diff (the decisive check):

- **On-time → late (the Aug-8 failure mode): 0 orders.** The guard never pushes a
  savable/on-time order late.
- **Late → on-time (rescued): 12 orders.**
- **Already-late got worse: 22** (the worst 46→61 lives here) — the per-order **cap**
  deliberately lets the optimizer pile *unavoidable* slip onto orders already missing
  their date, to shield the savable ones. This is the intended trade, not a regression.
- **Already-late got better: 33.**

**Decision:** lock the defaults (tol 2 / weight 2 / cap 30) — `weight 2` and `weight 4`
produced byte-identical plans, so μ=2 is sufficient. No constant change from the
behaviour-driven starting values. Owner-facing nuance to accept: the guard concentrates
unavoidable lateness onto already-late orders (a 46 can become a 61) in exchange for
never sacrificing an on-time one.

## Amendment (2026-07-24, after owner review of the measurement) — the worst order must never get later

The measurement above surfaced a behaviour the owner rejected: while the convex guard pushes
**zero on-time orders late** (the original Aug-8 problem — solved), it lets the single *worst*
(already-late) order drift further (46→61 d) because the cap makes lateness beyond ~32 d free.
Owner ruling: **whatever the worst order's date is in the current plan, a re-optimization must
never push it later.** It need not be finished on time — it must simply not regress.

A λ (max-lateness) sweep confirmed this cannot be met by tuning alone: only λ≈1000 held the worst
at 46 d, and that reproduced the un-optimized plan (0 rescues). So protecting the worst is a
*constraint*, not a weight. Owner chose the "smart search + hard backstop" option.

**The worst-order ceiling.** Define `ceiling = the currently-applied plan's max lateness in days`
(from `_incumbent_metrics()["max_late_days"]`; for a first-ever optimize with nothing applied,
this is the naive Rule-3 plan's worst — so even the first optimize can't worsen the worst order).
Two complementary parts, both soft-or-safe (no repeat of the July feasibility collapse):

1. **Search barrier (the "smart" part).** Add a strong convex barrier to BOTH objectives that
   penalizes any order whose lateness exceeds `ceiling`:
   `+ ceiling_weight · Σ max(0, gᵢ − ceiling)²`. Below the ceiling the convex tail guard optimizes
   normally; the barrier only bites when a plan would push some order past the current worst-case,
   so the search prefers win-win plans that help others *without* breaching the ceiling. Soft
   (unavoidable breaches still return the least-breaching plan, never infeasible).
   `ceiling` is threaded per-run; `ceiling=None` (no incumbent / not an optimize) = byte-identical
   to today.

2. **Apply backstop (the guarantee).** In `_auto_apply_result`, apply the winner only if
   `best["max_late_days"] <= inc["max_late_days"]` **in addition to** the strictly-better score
   check. If the only improving plan regresses the worst order, keep the current plan and write a
   note saying so. This makes the guarantee absolute even if the search barrier couldn't hold.

**Honest trade-off (owner-accepted):** on the current book the tail rescues *come from* breaching
the worst, so protecting it means some Thursdays keep the plan unchanged. That is the owner's
stated priority — the worst order's reputation over aggregate churn.

**Plumbing.** `engine/config.py` gains a transient `worst_ceiling_days: float | None` (serialized
by `to_dict`/`from_dict`, so it reaches the cloud worker via the payload; **excluded from
`_inputs_signature`** since it is per-run, not a saved input; **never persisted** into the saved
plan config). `_start_optimize` computes the incumbent ceiling and injects it before
`prepare_contest`/`build_payload`. `ppc_engine/config.py` gains `ceiling_days`/`ceiling_weight`;
`ppc_engine/objective/objective.py` adds the barrier. `engine/optimizer.py` `plan_metrics` gains
an optional `ceiling_days=` (computes `ceiling_breach`; `score` folds in `CEILING_WEIGHT`), kept
numerically consistent with the ppc side. `new_engine._plan_config` maps `worst_ceiling_days →
ceiling_days`, and `optimize_sequence`/`sweep_optimize` pass `ceiling_days` to their `plan_metrics`
calls so the contest winner-pick and reported metrics see the breach. `ceiling_weight` is MEASURED
on the real book (the winner must never exceed the incumbent max).

## Ceiling measurement result (2026-07-24, real book Test5)

With the ceiling wired (`worst_ceiling_days` = the naive incumbent's worst = **46 d**), swept
`ceiling_weight ∈ {100, 300, 1000}` on the real 71-order book:

| ceiling_weight | winner's worst | ≤ ceiling? | total late | rescued | on-time harmed |
|---|---|---|---|---|---|
| 100 | 46 | **YES** | 1596 | 0 | 0 |
| 300 | 46 | YES | 1596 | 0 | 0 |
| 1000 | 46 | YES | 1596 | 0 | 0 |

- **Guarantee proven:** the search winner's worst order stays exactly at the ceiling (46) and never
  exceeds it, at weight 100 already (also verified with a looser ceiling of 61 → winner 57 ≤ 61).
  With the hard apply backstop on top, "the worst order never gets later" is guaranteed. **Lock
  `ceiling_weight = 100`.**
- **Honest cost on this book:** holding the worst at 46 reproduces the naive plan (total 1596, 0
  rescues) — the worst order is on the critical path, so every tail improvement would push it past
  46 and is therefore refused. Consequence: on the current book a Thursday re-optimize *protects*
  (keeps the plan) rather than churns; the winner isn't strictly better than the incumbent, so the
  backstop/strictly-better check keeps the current plan. On a book where a real win-win exists
  (worst ≤ ceiling AND others improve), it still applies. This is the owner's accepted trade:
  the worst order's reputation over aggregate churn.

## Rollout

Single change set, behind the existing engine seam. No env var, no schema change, no UI
change beyond the richer auto-note text. Deploy is the standard Render manual deploy after
`pytest` passes.
