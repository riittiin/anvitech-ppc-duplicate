# Earliness penalty in the optimizer objective — design

> **SUPERSEDED (2026-08-06) by `2026-08-06-symmetric-ontime-objective-design.md`.**
> The branch this described was deleted, not merged. Do not execute this plan.

**Date:** 2026-08-06
**Status:** design approved, not implemented
**Scope:** optimizer objective only. No scheduler, rule, UI, or `new_engine` change.

---

## 1. The problem

The optimizer treats "finished on the delivery date" and "finished 37 days before the
delivery date" as **identical**. Every one of the five terms in the score is one-sided:

| Term | Location | Early completion contributes |
|---|---|---|
| `total_late_days` | `engine/optimizer.py:135` (`late = [g for g in gaps if g > 0]`) | 0 |
| `slip_severity` | `engine/optimizer.py:138` (`over = g - T`, skips `<= 0`) | 0 |
| `ceiling_breach` | `engine/optimizer.py:146` | 0 |
| `max_tardiness` (fairness) | `ppc_engine/objective/metrics.py:66` (`max(0.0, late)`) | 0 |
| `committed_promise_breach` | `ppc_engine/objective/objective.py:62` | 0 |

Nothing in the objective can distinguish an order delivered on its due date from one
delivered three weeks early. The search is therefore free to run long-dated work now,
and it does.

Anvitech does not want stock built weeks before the customer will take it: it ties up
cash and floor space, and — the reason this became urgent — it consumes contended
machine time that late orders are queued for.

## 2. Evidence (live plan, 2026-08-06)

Measured on the production plan pulled from `POST /run` (68 orders, applied
optimization 2026-08-06T10:30, overlap 84, makespan 55.6 d, 483 late-days), and on
the app's own delay-justification report for the same day.

**Delay attribution, from the app's own report:**

| Cause | Days | Share of all time | Share of waiting |
|---|---|---|---|
| Working | 285.3 | 22.2% | — |
| Waiting: machine busy with another job | 724.8 | 56.4% | 72.5% |
| Waiting: off-hours | 168.2 | 13.1% | 16.8% |
| Waiting: crew | 106.4 | 8.3% | 10.6% |

Machine contention dominates, and contention is decided by the sequence — the
optimizer's lever.

**Orders finishing more than 4 days early:** 7, totalling 89 early-days.

Two numbers are used throughout and must not be confused:

- **early-days** — total days early, ignoring the grace: 37+10+10+10+8+8+6 = **89**.
- **earliness-breach-days** — days *beyond* the 4-day grace, which is what the term
  scores: 33+6+6+6+4+4+2 = **61**. This is the quantity in §5.1 and §6.

| Order | Due | Finishes | Early | Qty | Hours of others' waiting it causes |
|---|---|---|---|---|---|
| `26-27SO143 / 61249291-01` | 14 Sep | 8 Aug | 37 d | 11 | 38.4 |
| `26-27SO135 / 9612220704-P` | 22 Aug | 12 Aug | 10 d | 400 | **558.6** |
| `26-27SO136 / 9612220704-P` | 22 Aug | 12 Aug | 10 d | 23 | — |
| `26-27SO134 / 9612220708 P` | 22 Aug | 12 Aug | 10 d | 135 | 146.1 |
| `26-27SO144 / 9612358901` | 19 Aug | 11 Aug | 8 d | 250 | 8.3 |
| `26-27SO88 / 61249330-80` | 8 Oct | 30 Sep | 8 d | 6 | — |
| `26-27SO142 / 8010009209` | 25 Sep | 19 Sep | 6 d | 10 | — |

`SO135` is the ninth largest blocker in the entire plan, ahead of several genuinely
late orders, and it finishes ten days early. A dash means the delay report attributed
no blocking hours to that order: it finishes early but delays nobody, so it is a
working-capital cost only, not a delivery cost. Attributed blocking totals **751.4 h**
across the four orders that have it.

**Is the blocking avoidable?** Splitting all 17,398 hours of machine-contention
waiting by the blocking order's own outcome:

| Blocker's outcome | Hours | Share | |
|---|---|---|---|
| Itself late | 14,840.6 | 85.3% | unavoidable |
| On time or within the 4-day grace | 1,806.2 | 10.4% | unavoidable |
| **More than 4 days early** | **751.4** | **4.3%** | **avoidable** |

**Honest ceiling: 4.3% of machine-contention waiting.** Expected benefit is roughly
2–6% of the 483 late-days, plus the working-capital gain. This is not a fix for the
book being over-committed.

**Ruled out — Rule 1 consolidation is not the cause.** Batch `B049` holds only
`SO135` and `SO136`, both due 22 Aug, in their own batch; it is not glued to an
earlier-due order. The earliness comes from the sequence.

## 3. Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Grace period | **4 days** | Owner's stated policy: up to 4 days early is acceptable, beyond is not. |
| Penalty shape | **Linear** beyond the grace | See §3.1. |
| Knob location | **Hardcoded constants**, mirrored | Follows the `severity_*` precedent; no Settings control. |
| Safety rule | **Never worsen lateness** | Earliness is an inefficiency; lateness is a customer failure. |
| Makespan weight | **Same spec, measured separately** | The two terms interact, but must be attributable. |

### 3.1 Why linear, not convex

Every other guard in this codebase (`_severity`, `_ceiling_breach`,
`_committed_promise_breach`) squares the overage. **Earliness deliberately does not**,
and the live data is the reason:

| Order | Early by | Squared penalty would be | Hours of waiting it actually causes |
|---|---|---|---|
| `SO143 / 61249291-01` | 37 d | (37−4)² = **1089** | 38.4 |
| `SO135 / 9612220704-P` | 10 d | (10−4)² = **36** | **558.6** |

A convex curve would chase the 37-day order thirty times harder than the 10-day one,
while the 10-day one does fifteen times more damage. **Earliness harm scales with the
contended machine time the order consumes, not with how early it is.** A linear term
keeps the units comparable to `total_late_days`, which is what makes the safety rule
statable and testable.

The term does not need to model the damage. The damage — other orders running late —
is *already* in the objective. The term's only job is to break the optimizer's current
indifference; the existing lateness terms then supply the gradient toward the
reorderings that actually pay.

Accepted cost: an absurd case like `SO143` at 37 days early receives only mild
pressure. At 11 pieces and 38 hours of blocking, that is the correct priority.

## 4. Architecture

Two mirrored edits plus one gate.

| File | Change |
|---|---|
| `ppc_engine/config.py` | `earliness_grace_days: float = 4.0`, `earliness_weight: float = <measured>` as `PlanConfig` defaults |
| `ppc_engine/objective/objective.py` | new `_earliness_breach()`, added to `score()` |
| `engine/optimizer.py` | `EARLINESS_GRACE_DAYS = 4.0`, `EARLINESS_WEIGHT = <measured>`; `earliness_breach` in `plan_metrics()`; term in `score()` |
| `api/main.py` | `lateness_ok` condition in `_auto_apply_result` **and** in the manual `POST /optimize/apply` path |

**`engine/new_engine.py` needs no change.** `_plan_config` does not pass the
`severity_*` values either — `PlanConfig` defaults carry them. Earliness works the
same way.

### 4.1 ppc_engine side

```python
def _earliness_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Days an order finishes MORE than the grace early. Linear, NOT squared:
    earliness harm scales with the contended machine time the order eats, not with
    how early it is, so a convex curve would chase the wrong orders (2026-08-06
    spec §3.1). 0 when nothing breaches — additive, byte-identical otherwise."""
    grace = config.earliness_grace_days
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = -late - grace          # `late` is signed; early -> negative
        if over > 0:
            total += over
    return total
```

Added to `score()` as `+ config.earliness_weight * _earliness_breach(metrics, config)`.

### 4.2 engine/optimizer side

`gaps` already exists at `engine/optimizer.py:134` and already holds negatives:

```python
earliness_breach = 0.0
for g in gaps:
    over = -g - EARLINESS_GRACE_DAYS
    if over > 0:
        earliness_breach += float(over)
```

Returned as `earliness_breach`, read in `score()` via
`metrics.get("earliness_breach", 0.0)` so legacy metric dicts stay byte-identical —
the same defensive `.get` the other four terms use.

### 4.3 Measurement basis

Earliness is measured **per SO-line, not per batch**, matching how lateness is already
judged (`plan_metrics` scores each order against its own delivery date). For a
consolidated batch mixing an early-due and a late-due member, the later member is
unavoidably early; pushing the batch back would make the earlier member late, which
the safety rule (§5) blocks automatically. No special-casing.

## 5. Safety enforcement

### 5.1 Why weight ratios are not enough

Live score decomposition:

| Term | Raw | Weight | Points | Share |
|---|---|---|---|---|
| `total_late_days` | 483 | ×1 | 483 | **3.4%** |
| `makespan_days` | 55.6 | ×40 | 2,224 | 15.8% |
| `slip_severity` | 5,683 | ×2 | 11,366 | **80.8%** |
| **Total** | | | **14,073** | |

The raw late-days term is 3.4% of the score; the marginal cost of lateness is set by
the convex `slip_severity` curve, not by the ×1 term. So "keep the earliness weight
below 1.0" would be both wrong and useless — at weight 1 the whole 61-day breach is
0.43% of the score, i.e. noise.

**The rule is therefore enforced structurally, not by tuning.**

### 5.2 Two layers

**Soft, in-search.** Weight high enough to break indifference. Candidate range 5–50;
at 20 the term is ~8.7% of the score. Measured, not chosen.

**Hard, at apply.** Add to `_auto_apply_result`, alongside the existing `worst_ok`
and `promise_ok`:

```python
lateness_ok = best["total_late_days"] <= inc["total_late_days"]
```

This is the actual guarantee. A plan that reduces earliness while raising total
late-days can win on score and still be refused at the gate, which makes the in-search
weight a search hint rather than a promise.

**The manual Apply path gets the same gate.** `POST /optimize/apply` applies
unconditionally today — no `worst_ok`, no `promise_ok` (a known gap recorded in
`CLAUDE.md`, verified still present 2026-08-06). Owner decision 2026-08-06: add
`lateness_ok` there too, so the safety rule holds on both paths. This is a deliberate
behaviour change to an existing endpoint, beyond the earliness feature itself.

## 6. Measurement protocol

Four stages on the live book, each against the same baseline.

| Stage | Change | Reports |
|---|---|---|
| 1 | none | baseline: 483 late-days, 55.6 d makespan, 61 earliness-breach-days |
| 2 | earliness only, weight ∈ {5, 20, 50} | gain from earliness alone |
| 3 | makespan weight only, ∈ {0.1, 1, 10, 40} | gain from closing the 40-vs-0.1 gap |
| 4 | best of each, combined | interaction |

**Ship criterion: earliness-breach-days down AND `total_late_days` not up.**
If no configuration achieves both, ship nothing and report that the feature does not
pay on this book. Explicitly accepted by the owner, 2026-08-06.

### 6.1 The makespan weight is a measurement, not a fix

`engine/optimizer.py:40` is `40.0`; `ppc_engine/config.py:87` is `0.1`. The file
comments claim the mirrors are kept numerically equal; for `severity_*`, `ceiling_*`
and `committed_promise_*` they are. Makespan diverges by 400×.

The `40` was measured 2026-07-19 under the crew-smart **classic** scheduler, before
the current engine went live, and its own comment records that raising 10 → 40
deliberately bought a shorter makespan at the cost of ~200 extra late-days.

Neither value is assumed correct. Stage 3 reports late-days and makespan for each
candidate and the owner chooses, because it is a real business trade: a shorter
overall schedule against fewer late deliveries.

**Named risk.** Makespan is 15.8% of the score. Dropping it to 0.1 removes ~2,200
points and leaves severity at ~97% of the total, which could let the schedule stretch
a long way to shave severity. The sweep will surface this as a large makespan
increase; that is a signal to stay near 40, not to push on.

## 7. Testing

| Test file | Covers |
|---|---|
| `tests/test_earliness_metric.py` | `plan_metrics`: exactly 4 d early → 0, 5 d → 1, late → 0, on the day → 0, multi-order sum |
| `tests/test_ppc_earliness_metric.py` | `_earliness_breach` against `PlanMetrics`, same crafted cases |
| `tests/test_earliness_mirror.py` | the two implementations agree numerically on identical input |
| `tests/test_earliness_backstop.py` | auto-apply **and** manual apply both refuse a plan whose `total_late_days` rose |
| inert-case regression | no order beyond the grace → term is 0 → plans byte-identical to today |

**The mirror test is the most important one.** The 400× makespan divergence exists
precisely because nothing asserts the two scorers agree. The same assertion is added
for `makespan_weight` while implementing, so neither term can drift silently again.

The inert-case regression proves the feature is purely additive: if the measurement
fails and we ship with weight 0, we have provably changed nothing.

Existing suite (508 tests) must stay green. The golden trace is a Rules 1–6 trace and
does not run the optimizer, so it should be unaffected; verify rather than assume.

## 8. Scope boundary

**Not touched:** the scheduler (`ppc_engine/scheduler/flow_scheduler.py`), any rule,
any UI, Settings, `engine/new_engine.py`, `engine/config.py`, the freeze machinery,
absences, and the contest/cloud payload.

`optimize_service.py:285` and `new_engine.py:545` both pick winners via
`optimizer.score(...)` and inherit the new term automatically — no edit needed there.

## 9. Open items for implementation

- `EARLINESS_WEIGHT` is `<measured>` until stage 2 completes. Implementation lands the
  term with a placeholder weight and the measurement sets the final value before merge.
- Confirm the golden trace is genuinely unaffected (§7).
- Both constants carry the standard "must stay numerically equal, re-measure before
  moving" comment, matching `SEVERITY_WEIGHT`.
