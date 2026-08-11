# Symmetric on-time objective — design

**Date:** 2026-08-06
**Status:** design approved, not implemented
**Scope:** the optimizer's scoring formula only. No scheduler, rule, UI, export, report or
data change.
**Supersedes:** `2026-08-06-earliness-penalty-design.md` (that branch is deleted, not merged)

---

## 1. The goal, in the owner's words

> "We care only about the deliveries-on-time thing. It should not be too early, and it
> should not be too late. Everything will have an equal penalty. The relaxing days would
> be ±4 days of the delivery date. If there are ten orders and the plan is such that eight
> are delivered on time and two are delivered very late, like 10, 20 or 30 days, it
> shouldn't happen like that. The lateness should be distributed evenly."

Three requirements, and nothing else:

1. **On time is the only goal.** Not schedule length.
2. **±4 days is on time.** Early and late count the same beyond that.
3. **Spread the misses.** Ten orders slightly off beats two orders badly off.

## 2. Why the current formula does not express this

The score has five terms. Measured on the live plan (2026-08-06, 68 orders, applied
optimization 10:30):

| Term | Live contribution | Share |
|---|---|---|
| `total_late_days` | 483 | 3.4% |
| `slip_severity` (squared lateness beyond 2 d, capped 60) | 11,366 | 80.8% |
| `makespan_days` × 40 | 2,224 | **15.8%** |
| `ceiling_breach` | **0** | dormant *in the saved config* — `worst_ceiling_days` is null there by design (`api/main.py:368` strips it as a transient per-run value); every optimize run itself sets it from the incumbent's `max_late_days` (`api/main.py:1282`), so the term is live during search, not dormant |
| `committed_promise_breach` | **0** | dormant (0 committed orders, feature hidden) |

Two of the five are contributing nothing on today's book. The engine is really running on
three-and-a-half terms — `ceiling_breach` is armed but not currently binding.

**Three concrete mismatches with the goal:**

**(a) 15.8% of the decision is spent on schedule length, which the owner does not value.**
`engine/optimizer.py:32-40` records the reason it is 40: on 2026-07-19 raising it from 10
to 40 was measured at **78.4 d / 1,327 late-days → 72.7 d / 1,528 late-days**. It
deliberately bought 5.7 days of schedule for **201 extra late-days**. That was a correct
trade for the goal stated then ("minimize BOTH"). It is the wrong trade for the goal now.

**(b) Earliness is invisible.** Every term is one-sided; an order delivered 37 days early
scores identically to one delivered on the day.

**(c) The two scorers are not the same formula.** They are documented as mirrors:

| Term | Search (`ppc_engine/objective`) | Winner-pick (`engine/optimizer.score`) |
|---|---|---|
| Days late | 483 (3.7%) | 483 (3.4%) |
| Worst-order protection | 11,366 (87.5%) | 11,366 (80.8%) |
| **Fairness — 30 × worst order** | **1,140 (8.8%)** | **absent** |
| Schedule length | 5.6 (0.0%) | **2,224 (15.8%)** |

The search barely weighs schedule length and carries a fairness term the winner-pick does
not have at all. So the search finds a plan optimising one thing and the contest then
ranks candidates by another. Neither divergence was caught, because nothing compared them.

## 3. The new formula

```
For each order:
    miss    = |completion_date − delivery_date|        # both directions
    breach  = max(0, miss − ONTIME_BAND_DAYS)          # ±4 days is free
    breach  = min(breach, ONTIME_CAP_DAYS)             # one hopeless order can't dominate
    penalty = breach ** 2                              # squaring spreads the misses

score = ONTIME_WEIGHT × Σ penalty  +  MAKESPAN_TIEBREAK × makespan_days
```

Constants:

| Constant | Value | Rationale |
|---|---|---|
| `ONTIME_BAND_DAYS` | **4.0** | Owner's stated policy: ±4 days is on time. |
| `ONTIME_CAP_DAYS` | **60.0** | Unchanged from the existing `severity_cap_days`. The owner's 2026-07-25 "distribute the pain" choice; 60/90/120 gave identical plans, 60 is the plateau. Stops one hopeless order swallowing the score. Does not bind today (worst order is 38 d). |
| `ONTIME_WEIGHT` | **1.0** | With one dominant term, absolute scale is arbitrary — only the ratio to the tie-break matters. 1.0 keeps the number readable. |
| `MAKESPAN_TIEBREAK` | **0.1** | Matches what the search already uses. On the live book that is 0.1% of the score, so it can only separate otherwise-equal plans. Larger stops being a tie-break and starts buying schedule length again. |

### 3.1 Why squaring, and why a band

**Squaring delivers requirement 3 by itself:**

| Situation | Penalty |
|---|---|
| one order 30 days out | (30 − 4)² = **676** |
| ten orders 6 days out | 10 × (6 − 4)² = **40** |

A 17× preference for spreading. This is the mechanism the owner asked for, and it is the
same mechanism `slip_severity` already uses — generalised to both directions with the
tolerance moved 2 → 4.

**The band is flat, deliberately.** Owner decision 2026-08-06: no gentle pull toward the
exact date. Anywhere inside ±4 days scores zero. The accepted consequence is that the
optimizer has no reason to prefer delivering on the date over delivering 4 days out, so
orders currently inside the band may drift toward its edge. The owner was shown this and
chose it: ±4 is the promise, not a target to beat.

### 3.2 What each removed term is replaced by

| Removed | Replaced by | Note |
|---|---|---|
| `total_late_days` | the new term, beyond 4 days | Days 1–4 late become free — intended, per §3.1 |
| `slip_severity` | the new term | Same shape; tolerance 2 → 4, now two-sided |
| `fairness_weight × max_tardiness` (search only) | the new term's squaring | Owner decision 2026-08-06: a second lever at the same target, and present in only one of the two scorers |
| `earliness_breach` | the new term | Does the job properly and symmetrically |
| `makespan × 40` | `makespan × 0.1` | Demoted to a tie-break, not removed |

### 3.3 What is NOT replaced, and stays untouched

- **`ceiling_breach`** — guards against a *re-optimization* pushing an order past where the
  current plan already has it. That is stability between plans, not plan quality; the new
  term cannot express it. **Not dormant** — `api/main.py:1282` sets `worst_ceiling_days` to
  the incumbent's `max_late_days` on every optimize run, and it reaches
  `PlanConfig.ceiling_days` at `engine/new_engine.py:205`. It reads null only in the *saved*
  Config (`api/main.py:368` strips it there on purpose, since it is a per-run transient, not
  a setting) — that is a different thing from it being inactive during search.
- **`committed_promise_breach`** — measures against `promised_date`, a different date from
  the delivery date. Currently dormant (feature hidden, zero committed orders).

Both remain in the code and in the score, exactly as they are. Because the new on-time
term's live contribution is measured smaller than the old three-term total it replaces
(≈2.71× smaller on the live book), `ceiling_weight` (100) and `committed_promise_weight`
(5000) — both tuned against the old, larger scale — are now proportionally ~2.7× more
dominant relative to the main term than the values they were measured against.
`ceiling_breach` is live today, so this is not a purely theoretical interaction;
`committed_promise_breach` stays dormant only as long as the (currently hidden) commitment
feature stays off. Unmeasured — re-measure before either guard next binds.

### 3.4 The side effect worth having

Both scorers end up identical in formula, still different in domain. `ppc_engine`'s search
scores one entry per *consolidated batch*, using the batch's earliest member's delivery
date; `engine/optimizer.plan_metrics` scores one entry per *SO-line*, at that line's own
date — a measured 5.1% disagreement on the same plan. Consolidation (Rule 1) is exactly the
mechanism that produces this: a batch built around its earliest-due member delivers its
later-due members early, and the new symmetric term — with no pull toward the exact date
inside the band — has no reason to see that as a cost, so it is systematically invisible to
the search and only charged at the winner-pick, per-line. The 400× makespan divergence and
the missing fairness term both disappear, because there is now only one rule left to
disagree about at the formula level. The mirror test becomes meaningful for the formula it
checks, but it does not — and was never meant to — cover this domain difference.

## 4. Architecture

| File | Change |
|---|---|
| `engine/optimizer.py` | new constants; `score()` rewritten; `plan_metrics()` **gains** `ontime_breach` |
| `ppc_engine/objective/objective.py` | `score()` rewritten identically; new `_ontime_breach()` |
| `ppc_engine/config.py` | new `PlanConfig` fields; `makespan_weight` 0.1 unchanged |

`engine/new_engine.py` needs no change — `PlanConfig` defaults carry the values, exactly as
`severity_*` already does.

### 4.1 The load-bearing constraint: `plan_metrics` reports, `score` decides

`plan_metrics()` **keeps every field it returns today** and only gains one. This is not
caution, it is necessity. Traced consumers:

| Consumer | Fields read |
|---|---|
| `web/app.js:352` | `makespan_days`, `late_orders`, `orders`, `total_late_days` |
| `web/app.js:398-399` | `total_late_days` |
| `web/app.js:589-594` | `late_orders`, `orders`, `total_late_days`, `max_late_days` |
| `web/app.js:633` | `worst_orders` |
| `api/main.py:892-901` | `total_late_days`, `makespan_days` |
| `api/main.py:1282` | `max_late_days` (feeds `worst_ceiling_days`) |
| `api/main.py:1779` | `max_late_days` (the `worst_ok` apply gate) |
| `api/main.py:1792-1805` | `total_late_days` (the auto-note text) |

Removing any of these blanks the Optimize panel, the Orders tab note, or the apply gate.
`slip_severity` is likewise retained in the returned dict even though `score()` no longer
reads it — cheap to compute, and removing a reported field is an unnecessary risk.

### 4.2 Data flow

`optimize_service.py:285` and `new_engine.py:545` both pick winners through
`optimizer.score(...)`, and the inner search runs on `ppc_engine.objective.score`. Editing
those two functions covers every decision point. No call-site changes.

## 5. Scope boundary

**Unchanged, explicitly:** the scheduler (`ppc_engine/scheduler/`), all nine rules, the
Gantt, the shift-wise Excel, the delay-justification report, the Rule-6 allocation CSV, the
Orders tab, analytics, the efficiency report, the freeze machinery, absences, the operator
table, `engine/config.py`, `engine/new_engine.py`, and the cloud contest payload.

**But "unchanged" means the features, not the output.** A different plan wins, so the Gantt
shows different bars and the Excel different dates. That is the purpose of the change.

## 6. Testing and the ship gate

**Unit tests on the formula:**

- symmetry — 30 days early and 30 days late score identically
- the band — exactly 4 days out scores 0; 5 days out scores 1
- the cap — 100 days out scores the same as 60 days out
- spreading — one order 30 out scores worse than ten orders 6 out
- the tie-break cannot outrank the main term at any realistic magnitude

**Mirror test:** both scorers must return identical numbers for identical input. Meaningful
for the first time, because the formulas are now the same.

**Ship gate — measured on the live book, best-of-three-seeds per configuration.**

Two conditions, both required, and at least one must strictly improve:

| Measure | Today | Required |
|---|---|---|
| Orders inside ±4 days ("on time") | **25 of 68** | not lower |
| Worst single order | **38 days** | not worse |

**Why only two.** An earlier draft also required "orders more than 4 days late must
fall", which can contradict the first condition: the three buckets sum to 68, so a plan
that converts early-beyond-4 orders into on-time ones can raise the on-time count *and*
the late count simultaneously. Requiring both would reject a plan that improved exactly
what the owner asked for. The late/early split is **reported for information, not gated**.

**If it does not clear that gate on the real book, it does not ship.** The same discipline
stopped the earliness feature, correctly.

**Best-of-three-seeds is mandatory, not optional.** The earliness measurement was ruined by
single-run noise: weights 25–45 produced a plan strictly dominated by the weight-50 plan by
~9,700 score points, meaning the search missed a basin worth 295 late-days. A single run
measures the search's luck. Compare distributions, not points.

**Golden trace** must pass without regeneration — it covers Rules 1–6 and does not run the
optimizer. Verify rather than assume.

## 7. Risk

This is a rewrite of the objective that drives a live factory. Unlike the earliness work,
it is **not** inert: it changes which plan wins, therefore what the floor runs.

- Every existing weight was tuned against the old formula. The new formula makes most of
  them irrelevant, but `ceiling_weight` and `committed_promise_weight` still coexist with it
  and their relative scale is now different. Both are dormant today, which limits the
  exposure, but a future user committing an order would meet an untuned interaction.
- The flat band means orders currently landing inside ±4 days may drift to the edge.
  Accepted by the owner (§3.1), but it will be visible on the Gantt.
- Render auto-deploys `main`. Merging is a production release; this one genuinely changes
  the schedule and must be deployed deliberately, not casually.

## 8. Open items for implementation

- `ONTIME_WEIGHT` is fixed at 1.0 and needs no measurement: with a single dominant term,
  only its ratio to `MAKESPAN_TIEBREAK` matters, and that ratio is set by §3.
- Confirm the golden trace is genuinely unaffected (§6).
- Delete `tests/test_earliness_*`, `scripts/measure_earliness.py` and the earliness
  constants along with the superseded branch.
- The measurement harness needs rebuilding around the new ship gate's three measures
  rather than around score. Reuse `scripts/measure_earliness.py`'s corrected weight-patching
  mechanism — the frozen-dataclass trap it documents still applies.
