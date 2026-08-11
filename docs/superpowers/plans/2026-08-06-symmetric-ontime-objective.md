# Symmetric On-Time Objective Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the optimizer's scoring formula with a single symmetric on-time penalty, so the software optimises for the only thing the owner values: orders delivered within ±4 days of their date, with misses spread rather than concentrated.

**Architecture:** One squared penalty on `|completion − due|` beyond a 4-day band, capped at 60 days, replaces four existing terms in both mirrored scorers. Makespan drops from weight 40 to a 0.1 tie-break. `plan_metrics` keeps every field it reports today and gains one — reporting and deciding are deliberately separated.

**Tech Stack:** Python 3, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-06-symmetric-ontime-objective-design.md`

## Global Constraints

- **Band is exactly 4 days, both directions.** `|miss| ≤ 4` scores zero. Early and late count identically beyond it.
- **Squared, capped at 60.** Squaring is what spreads misses across orders; the cap stops one hopeless order dominating.
- **The band is FLAT.** No gentle pull toward the exact date. Owner decision 2026-08-06 — do not add a linear term "to help".
- **`plan_metrics` keeps EVERY field it returns today** and only gains one. Its output is consumed by `web/app.js:352,398,589-594,633` and `api/main.py:892-901,1282,1779,1792-1805`. Removing a field blanks the Optimize panel, the Orders note, or the apply gate.
- **`slip_severity` stays computed and reported** even though `score()` no longer reads it. Three existing tests assert it and it is cheap.
- **The two scorers must produce IDENTICAL numbers** for identical input. This is now genuinely achievable — same formula both sides.
- **Do not touch:** the scheduler, any of the nine rules, any UI, Settings, exports, the Gantt, the delay report, analytics, the efficiency report, freeze, absences, operators, `engine/config.py`, `engine/new_engine.py`, or the cloud payload.
- **`MAKESPAN_WEIGHT` keeps its name.** Only its value changes, 40.0 → 0.1. Renaming is churn the spec does not require.
- **Baseline is 756 passed, 1 skipped.** Every task must leave the suite green.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `engine/optimizer.py` | Constants, `ontime_breach` in `plan_metrics`, new `score()` | 1 |
| `tests/test_ontime_objective.py` | Engine-side formula behaviour | 1 |
| `tests/test_promise_protection.py` | Fix the engine test whose premise the new formula changes | 1 |
| `ppc_engine/config.py` | `PlanConfig` on-time fields | 2 |
| `ppc_engine/objective/objective.py` | `_ontime_breach`, new `score()`, delete `_severity` | 2 |
| `tests/test_ppc_ontime_objective.py` | ppc-side formula behaviour | 2 |
| `tests/test_ppc_promise_metric.py` | Fix the formula reconstruction | 2 |
| `tests/test_scorer_mirror.py` | The two scorers agree exactly | 3 |
| `scripts/measure_ontime.py` | Ship-gate measurement harness | 4 |
| constants | Final decision after measurement | 5 |

---

### Task 1: Engine-side formula

**Files:**
- Modify: `engine/optimizer.py` (constants ~line 40-48; `plan_metrics` ~line 134-172; `score` line 85-94)
- Create: `tests/test_ontime_objective.py`
- Modify: `tests/test_promise_protection.py` (one test, see Step 6)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: module constants `ONTIME_BAND_DAYS: float = 4.0`, `ONTIME_CAP_DAYS: float = 60.0`, `ONTIME_WEIGHT: float = 1.0`; `MAKESPAN_WEIGHT` changed to `0.1`; new key `"ontime_breach": float` in `optimizer.plan_metrics(...)`; `optimizer.score(metrics: dict) -> float` reads it via `.get`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ontime_objective.py`:

```python
"""Symmetric on-time objective, engine side (spec 2026-08-06).

The owner's rule, in full: deliver on time; +/-4 days either side is fine; beyond
that early and late are equally bad; and misses must be SPREAD across orders rather
than concentrated on a few. Squaring the overage is what delivers the spreading.
"""
from datetime import date, datetime, timedelta

from engine import optimizer
from engine.models import SOLine, ScheduleEntry

PS = date(2026, 8, 6)
DUE = date(2026, 9, 1)


def _line(so, item, due=DUE):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=10, delivery_date=due)


def _entry(so, item, end):
    return ScheduleEntry(batch_id=so, item_code=item, process_seq=1,
                         process_name="CNC", machine="CNC1", qty=10,
                         occupancy_min=60, start=datetime(2026, 8, 6, 8, 0),
                         end=end, so_refs=[so])


def _breach_for(days_off):
    """days_off > 0 = late, < 0 = early."""
    lines, sched = [], []
    for n, d in enumerate(days_off):
        so, item = f"SO{n}", f"IT-{n}"
        lines.append(_line(so, item))
        end = datetime(2026, 9, 1, 17, 0) + timedelta(days=d)
        sched.append(_entry(so, item, end))
    return optimizer.plan_metrics(sched, lines, PS)["ontime_breach"]


def test_early_and_late_are_penalised_identically():
    """The core of the owner's rule: 30 days early is exactly as bad as 30 late."""
    assert _breach_for([30]) == _breach_for([-30])
    assert _breach_for([30]) > 0


def test_inside_the_band_costs_nothing_either_direction():
    for d in (0, 4, -4, 3, -1):
        assert _breach_for([d]) == 0.0, f"{d} days off should be free"


def test_one_day_past_the_band_costs_one():
    """5 days off -> overage 1 -> 1 squared -> 1.0. Pins band=4 exactly."""
    assert _breach_for([5]) == 1.0
    assert _breach_for([-5]) == 1.0


def test_squaring_spreads_the_misses():
    """The owner's stated requirement: ten orders slightly off must beat one order
    badly off. 30 days out -> (30-4)^2 = 676; ten at 6 days -> 10 * (6-4)^2 = 40."""
    concentrated = _breach_for([30])
    spread = _breach_for([6] * 10)
    assert concentrated == 676.0
    assert spread == 40.0
    assert spread < concentrated


def test_cap_stops_one_hopeless_order_dominating():
    """Overage is capped at 60 before squaring, so 100 days out scores the same as
    64 days out. Without this a single doomed order swamps the whole plan."""
    assert _breach_for([100]) == _breach_for([64]) == 60.0 ** 2


def test_score_uses_ontime_breach_and_a_makespan_tiebreak():
    base = {"makespan_days": 50.0, "ontime_breach": 0.0}
    worse = {"makespan_days": 50.0, "ontime_breach": 10.0}
    assert optimizer.score(worse) - optimizer.score(base) == optimizer.ONTIME_WEIGHT * 10.0


def test_makespan_cannot_outrank_the_ontime_term():
    """Makespan is a TIE-BREAK. A plan one day shorter must never beat a plan with a
    genuinely better on-time result. At weight 0.1, 100 extra days of schedule are
    worth less than a single order 8 days off ((8-4)^2 = 16)."""
    shorter_but_worse = {"makespan_days": 10.0, "ontime_breach": 16.0}
    longer_but_better = {"makespan_days": 110.0, "ontime_breach": 0.0}
    assert optimizer.score(longer_but_better) < optimizer.score(shorter_but_worse)


def test_makespan_still_breaks_an_exact_tie():
    a = {"makespan_days": 50.0, "ontime_breach": 5.0}
    b = {"makespan_days": 60.0, "ontime_breach": 5.0}
    assert optimizer.score(a) < optimizer.score(b)


def test_plan_metrics_keeps_every_reported_field():
    """Global constraint: the UI and api read these. Losing one blanks a panel."""
    m = optimizer.plan_metrics([_entry("SO1", "IT-A", datetime(2026, 9, 20, 17, 0))],
                               [_line("SO1", "IT-A")], PS)
    for field in ("makespan_days", "late_orders", "total_late_days", "max_late_days",
                  "slip_severity", "ceiling_breach", "committed_promise_breach",
                  "max_committed_slip", "orders", "ontime_breach"):
        assert field in m, f"plan_metrics stopped reporting {field}"
    assert m["total_late_days"] == 19        # still reported even though score ignores it
    assert m["slip_severity"] == (19 - 2) ** 2
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/Users/ritinwadekar/Desktop/Anvitech Rebuilt"
python3 -m pytest tests/test_ontime_objective.py -v
```

Expected: FAIL — `KeyError: 'ontime_breach'` and `AttributeError: ... has no attribute 'ONTIME_WEIGHT'`.

- [ ] **Step 3: Replace the makespan constant and its stale comment**

In `engine/optimizer.py`, replace the whole comment block above `MAKESPAN_WEIGHT` (currently lines 32-40) and the constant with:

```python
# Score = ONTIME_WEIGHT x ontime_breach + MAKESPAN_WEIGHT x makespan_days
#         (+ the two dormant guards: ceiling, committed-promise).
#
# 2026-08-06 (owner: "we care only about the deliveries-on-time thing"): makespan
# demoted 40 -> 0.1, a pure TIE-BREAK. It was 40 because on 2026-07-19 the goal was
# to minimize BOTH late days and schedule length; that note recorded the trade
# honestly — raising 10 -> 40 moved the live book from 78.4 d / 1327 late-days to
# 72.7 d / 1528, i.e. it bought 5.7 schedule days for 201 extra late-days. The goal
# is now on-time delivery only, which makes that trade the wrong way round. 0.1
# matches ppc_engine/config.py makespan_weight, so the two scorers finally agree.
MAKESPAN_WEIGHT = 0.1           # == ppc_engine makespan_weight

# The on-time objective (2026-08-06 spec). ONE symmetric term replacing
# total_late_days + slip_severity + earliness: for each order take how far it misses
# its delivery date in EITHER direction, ignore the first ONTIME_BAND_DAYS, cap the
# rest, and square it.
#
# Squaring is the mechanism the owner asked for by name: ten orders 6 days out
# (10 x 2^2 = 40) must beat one order 30 days out ((30-4)^2 = 676). The cap stops a
# single hopeless order swamping the plan. The band is FLAT by owner decision — no
# pull toward the exact date; anywhere inside +/-4 days is equally on time.
#
# Must stay numerically EQUAL to ppc_engine/config.py ontime_* .
ONTIME_BAND_DAYS = 4.0          # == ppc_engine ontime_band_days
ONTIME_CAP_DAYS = 60.0          # == ppc_engine ontime_cap_days
ONTIME_WEIGHT = 1.0             # == ppc_engine ontime_weight
```

- [ ] **Step 4: Mark the severity constants reporting-only**

Immediately above `SEVERITY_TOLERANCE_DAYS` (currently line ~45), replace the existing comment block with:

```python
# REPORTING ONLY since 2026-08-06. `score()` no longer reads slip_severity — the
# on-time term above subsumes it (same squared shape, tolerance 2 -> 4, now
# two-sided). `plan_metrics` still COMPUTES and RETURNS slip_severity because it is
# a reported field: three tests assert it and removing a reported field is
# needless risk. Kept equal to ppc_engine/config.py severity_* .
```

Leave the three `SEVERITY_*` constants themselves unchanged.

- [ ] **Step 5: Add the metric and rewrite score**

In `plan_metrics`, immediately after the `committed_promise_breach` block and before `result = {`, add:

```python
    # The on-time objective (spec 2026-08-06). `gaps` is SIGNED — negative is early —
    # and abs() is what makes early and late count the same, which is the owner's rule.
    ontime_breach = 0.0
    for g in gaps:
        over = abs(g) - ONTIME_BAND_DAYS
        if over > 0:
            if over > ONTIME_CAP_DAYS:
                over = ONTIME_CAP_DAYS
            ontime_breach += float(over * over)
```

Add one key to the `result` dict, after `"committed_promise_breach"`:

```python
        "ontime_breach": round(ontime_breach, 2),
```

Replace `score()` entirely:

```python
def score(metrics: dict) -> float:
    """Lower is better. ONE on-time term plus a makespan tie-break, and the two
    dormant guards.

    `ontime_breach` is the whole objective: squared distance from the delivery date
    in either direction, beyond a 4-day band, capped. It replaces total_late_days,
    slip_severity and the search's fairness term (2026-08-06 spec).

    Makespan is a TIE-BREAK at 0.1 — it separates plans that are otherwise equal and
    must never outrank a genuine on-time improvement.

    `ceiling_breach` and `committed_promise_breach` are unchanged and both dormant in
    production today; they do jobs the on-time term cannot express (no-regression
    across re-plans, and a different promised date).

    ``.get`` keeps legacy metrics dicts safe.
    """
    return (ONTIME_WEIGHT * metrics.get("ontime_breach", 0.0)
            + MAKESPAN_WEIGHT * metrics["makespan_days"]
            + CEILING_WEIGHT * metrics.get("ceiling_breach", 0.0)
            + COMMITTED_PROMISE_WEIGHT * metrics.get("committed_promise_breach", 0.0))
```

- [ ] **Step 6: Fix the one existing engine test whose premise the new formula changes**

`tests/test_promise_protection.py::test_optimizer_score_convex_protects_second_worst`
builds metrics dicts carrying `slip_severity`, which `score()` no longer reads, so both
plans would now score identically. Its *intent* — the objective must prefer spreading —
is exactly what the new formula does, so keep the intent and update the mechanism.
Replace that test with:

```python
def test_optimizer_score_convex_protects_second_worst():
    # Same scenario, expressed in the on-time term the score now reads (2026-08-06).
    # sacrifice: orders 20 and 15 days late  -> (20-4)^2 + (15-4)^2 = 256 + 121 = 377
    # protect:   orders 22 and  2 days late  -> (22-4)^2 +        0 = 324
    # Makespan equal, so only the on-time term can flip the preference.
    sacrifice = {"makespan_days": 40.0,
                 "ontime_breach": (20 - 4) ** 2 + (15 - 4) ** 2}
    protect = {"makespan_days": 40.0,
               "ontime_breach": (22 - 4) ** 2 + 0}
    assert optimizer.score(protect) < optimizer.score(sacrifice)
```

Leave `test_plan_metrics_slip_severity_is_convex_and_capped` and
`test_plan_metrics_severity_zero_within_tolerance` untouched — `plan_metrics` still
reports `slip_severity`, so they must keep passing. If either fails, you have removed a
reported field: fix the code, not the test.

- [ ] **Step 7: Run the new tests, then the neighbours, then the suite**

```bash
python3 -m pytest tests/test_ontime_objective.py -v
python3 -m pytest tests/test_promise_protection.py tests/test_worst_ceiling.py -v
python3 -m pytest -q
```

Expected: all pass. `test_worst_ceiling.py::test_optimizer_score_uses_ceiling_breach`
should still pass untouched — the ceiling term survives unchanged.

If any *other* test fails, read it before editing: a failure in `tests/test_optimizer.py`
or the golden test means something outside the scoring formula moved, which the spec
forbids.

- [ ] **Step 8: Commit**

```bash
git add engine/optimizer.py tests/test_ontime_objective.py tests/test_promise_protection.py
git -c commit.gpgsign=false commit -m "feat: symmetric on-time objective (engine side)

One squared penalty on distance from the delivery date in either direction,
beyond a 4-day band, capped at 60. Replaces total_late_days + slip_severity.
Makespan demoted 40 -> 0.1, a pure tie-break.

slip_severity stays computed and reported - score() just stops reading it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: ppc_engine-side formula

**Files:**
- Modify: `ppc_engine/config.py` (after `makespan_weight`, ~line 87)
- Modify: `ppc_engine/objective/objective.py` (module docstring; delete `_severity`; rewrite `score`)
- Create: `tests/test_ppc_ontime_objective.py`
- Modify: `tests/test_ppc_promise_metric.py` (formula reconstruction, ~line 64-71)
- Modify: `tests/test_promise_protection.py` (the ppc test at ~line 25-35)

**Interfaces:**
- Consumes: the constant VALUES from Task 1 — `ONTIME_BAND_DAYS = 4.0`, `ONTIME_CAP_DAYS = 60.0`, `ONTIME_WEIGHT = 1.0`, `MAKESPAN_WEIGHT = 0.1`. Mirror them exactly; Task 3 asserts equality and will fail otherwise.
- Produces: `PlanConfig.ontime_band_days`, `.ontime_cap_days`, `.ontime_weight`; `ppc_engine.objective.objective._ontime_breach(metrics: PlanMetrics, config: PlanConfig) -> float`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ppc_ontime_objective.py`:

```python
"""Symmetric on-time objective, ppc_engine side (spec 2026-08-06).

Mirror of tests/test_ontime_objective.py. The two scorers must agree exactly — see
tests/test_scorer_mirror.py.
"""
from datetime import datetime

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _ontime_breach, score

CFG = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))


def _pm(lateness, makespan=0.0):
    """lateness_by_order values are SIGNED days: negative means early."""
    return PlanMetrics(
        total_tardiness_days=0.0,
        max_tardiness_days=0.0,
        late_order_count=0,
        makespan_days=makespan,
        lateness_by_order={(f"SO{n}", "x"): float(v) for n, v in enumerate(lateness)},
        promise_slip_by_order={},
    )


def test_early_and_late_are_penalised_identically():
    assert _ontime_breach(_pm([30]), CFG) == _ontime_breach(_pm([-30]), CFG)
    assert _ontime_breach(_pm([30]), CFG) > 0


def test_inside_the_band_costs_nothing_either_direction():
    for d in (0, 4, -4, 3, -1):
        assert _ontime_breach(_pm([d]), CFG) == 0.0, f"{d} days off should be free"


def test_one_day_past_the_band_costs_one():
    assert _ontime_breach(_pm([5]), CFG) == 1.0
    assert _ontime_breach(_pm([-5]), CFG) == 1.0


def test_squaring_spreads_the_misses():
    assert _ontime_breach(_pm([30]), CFG) == 676.0
    assert _ontime_breach(_pm([6] * 10), CFG) == 40.0


def test_cap_stops_one_hopeless_order_dominating():
    assert _ontime_breach(_pm([100]), CFG) == _ontime_breach(_pm([64]), CFG) == 3600.0


def test_score_is_the_ontime_term_plus_a_makespan_tiebreak():
    """With both guards dormant, the score is exactly these two terms."""
    m = _pm([10], makespan=50.0)
    expected = CFG.ontime_weight * 36.0 + CFG.makespan_weight * 50.0
    assert abs(score(m, CFG) - expected) < 1e-9


def test_makespan_cannot_outrank_the_ontime_term():
    shorter_but_worse = _pm([8], makespan=10.0)     # (8-4)^2 = 16
    longer_but_better = _pm([0], makespan=110.0)    # inside the band -> 0
    assert score(longer_but_better, CFG) < score(shorter_but_worse, CFG)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python3 -m pytest tests/test_ppc_ontime_objective.py -v
```

Expected: FAIL — `ImportError: cannot import name '_ontime_breach'`.

- [ ] **Step 3: Add the PlanConfig fields**

In `ppc_engine/config.py`, immediately after `makespan_weight: float = 0.1`, add:

```python
    # The on-time objective (2026-08-06 spec). ONE symmetric term replacing
    # total_tardiness + severity + the fairness term: for each order, how far it
    # misses its due date in EITHER direction, minus a free band, capped, squared.
    # Squaring spreads misses across orders instead of concentrating them, which is
    # the owner's stated requirement. The band is FLAT — no pull toward the exact
    # date. Must equal engine/optimizer.py ONTIME_* .
    ontime_band_days: float = 4.0
    ontime_cap_days: float = 60.0
    ontime_weight: float = 1.0
```

Then mark the now-unused fields. Above `fairness_weight: float = 30.0` replace the
comment with:

```python
    # RETAINED BUT UNUSED since 2026-08-06. `score()` no longer reads fairness_weight
    # or severity_*: the on-time term subsumes both. Kept (not deleted) so the field
    # names stay stable for anyone reading old plans or notes, matching how `pinned`
    # and `week_anchor` were retained inert when shift rotation was removed.
```

Leave `fairness_weight` and the three `severity_*` fields themselves in place.

- [ ] **Step 4: Rewrite the objective module**

In `ppc_engine/objective/objective.py`:

**(a)** Replace the module docstring's formula block with:

```
    score = w_ontime · Σ (|miss| − band, capped)²     # the whole objective
          + w_makespan · makespan                      # a strict tie-break only
          + ceiling / committed-promise guards         # dormant in production today
```

**(b)** DELETE the `_severity` function entirely. It is now unreachable — nothing else
calls it, and `PlanMetrics` carries no severity field, so unlike the engine side it is
not a reported value.

**(c)** Add, immediately before `score`:

```python
def _ontime_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Squared distance from the delivery date, in EITHER direction, beyond a free
    band and capped. This is the whole objective (2026-08-06 spec).

    `abs()` is the owner's rule that early and late are equally bad. Squaring is the
    owner's rule that misses must be spread: ten orders 6 days out (10 x 2^2 = 40)
    beats one order 30 days out ((30-4)^2 = 676). The cap stops one hopeless order
    swamping the plan.

    `lateness_by_order` is SIGNED — negative means the order finished early.
    """
    band = config.ontime_band_days
    cap = config.ontime_cap_days
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = abs(late) - band
        if over > 0:
            if over > cap:
                over = cap
            total += over * over
    return total
```

**(d)** Replace `score` entirely:

```python
def score(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Score a plan from its metrics. Lower is better."""
    return (
        config.ontime_weight * _ontime_breach(metrics, config)
        + config.ceiling_weight * _ceiling_breach(metrics, config)
        + config.committed_promise_weight * _committed_promise_breach(metrics, config)
        + config.makespan_weight * metrics.makespan_days
    )
```

- [ ] **Step 5: Fix the two existing ppc tests the rewrite breaks**

**(a)** `tests/test_ppc_promise_metric.py` line 6 imports `_severity`, which no longer
exists, and lines ~64-71 reconstruct the old formula. Change the import to:

```python
from ppc_engine.objective.objective import _ceiling_breach, _ontime_breach, score
```

and replace the `pre_existing_terms` expression with:

```python
    pre_existing_terms = (
        cfg.ontime_weight * _ontime_breach(metrics, cfg)
        + cfg.ceiling_weight * _ceiling_breach(metrics, cfg)
        + cfg.makespan_weight * metrics.makespan_days
    )
```

**(b)** `tests/test_promise_protection.py`, the ppc test at the top of the file, uses
`replace(cfg, severity_weight=0.0)` to show the old objective was wrong. `severity_weight`
no longer affects the score, so that contrast is gone. Replace that whole test with one
that pins the same intent against the new formula:

```python
def test_ontime_objective_protects_the_second_worst():
    # sacrifice: orders 20 and 15 days late -> (20-4)^2 + (15-4)^2 = 256 + 121 = 377
    # protect:   orders 22 and  2 days late -> (22-4)^2 +        0 = 324
    # Squaring is what makes spreading the pain the better plan.
    cfg = PlanConfig(plan_start=datetime(2025, 3, 1))
    sacrifice = _metrics([20.0, 15.0])
    protect = _metrics([22.0, 2.0])
    assert score(protect, cfg) < score(sacrifice, cfg)
```

Keep the file's existing `_metrics` helper and imports; drop the now-unused `replace`
import only if nothing else in the file uses it.

- [ ] **Step 6: Run the tests**

```bash
python3 -m pytest tests/test_ppc_ontime_objective.py tests/test_ppc_promise_metric.py tests/test_promise_protection.py -v
python3 -m pytest -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ppc_engine/config.py ppc_engine/objective/objective.py \
        tests/test_ppc_ontime_objective.py tests/test_ppc_promise_metric.py \
        tests/test_promise_protection.py
git -c commit.gpgsign=false commit -m "feat: symmetric on-time objective (ppc_engine side)

Mirrors the engine-side term. Removes _severity and the 30x worst-order
fairness term - the search carried that fairness term and the winner-pick
did not, one of two reasons the two scorers disagreed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Mirror guard

The two scorers were documented as mirrors while actually running different formulas —
the search weighed makespan 400× less and carried a fairness term the winner-pick lacked.
Both are now gone. This test makes the claim enforceable.

**Files:**
- Create: `tests/test_scorer_mirror.py`

**Interfaces:**
- Consumes: Task 1's `optimizer.ONTIME_BAND_DAYS/ONTIME_CAP_DAYS/ONTIME_WEIGHT/MAKESPAN_WEIGHT` and `optimizer.plan_metrics`; Task 2's `PlanConfig.ontime_*` and `_ontime_breach`.
- Produces: nothing consumed downstream.

- [ ] **Step 1: Write the test**

Create `tests/test_scorer_mirror.py`:

```python
"""The two scorers must judge a plan identically.

engine/optimizer.py scores the contest winner-pick and the apply comparison;
ppc_engine/objective scores the inner sequence search. They are documented as
mirrors. Before 2026-08-06 they were not: the search weighed makespan 0.1 against
the winner-pick's 40, and carried a 30x worst-order fairness term the winner-pick
did not have at all. Nothing caught either, because nothing compared them.
"""
from datetime import date, datetime, timedelta

from engine import optimizer
from engine.models import SOLine, ScheduleEntry
from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _ontime_breach

CFG = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))


def test_ontime_constants_are_mirrored():
    assert optimizer.ONTIME_BAND_DAYS == CFG.ontime_band_days
    assert optimizer.ONTIME_CAP_DAYS == CFG.ontime_cap_days
    assert optimizer.ONTIME_WEIGHT == CFG.ontime_weight


def test_makespan_weights_are_now_equal():
    """They diverged 40 vs 0.1 from 2026-07-19 to 2026-08-06. Never again."""
    assert optimizer.MAKESPAN_WEIGHT == CFG.makespan_weight == 0.1


def test_guard_constants_are_mirrored():
    assert optimizer.CEILING_WEIGHT == CFG.ceiling_weight
    assert optimizer.COMMITTED_PROMISE_WEIGHT == CFG.committed_promise_weight


def test_both_implementations_compute_the_same_breach():
    """Same misses, both directions, on both sides of the band and the cap."""
    days_off = [30, -30, 10, -10, 5, -5, 4, -4, 0, 100, -100, 61]
    lines, sched, lateness = [], [], {}
    for n, d in enumerate(days_off):
        so, item = f"SO{n}", f"IT-{n}"
        lines.append(SOLine(so_no=so, item_code=item, item_name=item, qty=10,
                            delivery_date=date(2026, 9, 1)))
        sched.append(ScheduleEntry(
            batch_id=so, item_code=item, process_seq=1, process_name="CNC",
            machine="CNC1", qty=10, occupancy_min=60,
            start=datetime(2026, 8, 6, 8, 0),
            end=datetime(2026, 9, 1, 17, 0) + timedelta(days=d), so_refs=[so]))
        lateness[(so, item)] = float(d)

    engine_breach = optimizer.plan_metrics(sched, lines, date(2026, 8, 6))["ontime_breach"]
    ppc_breach = _ontime_breach(
        PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=0.0,
                    lateness_by_order=lateness, promise_slip_by_order={}), CFG)
    assert engine_breach == ppc_breach
    assert engine_breach > 0        # the fixture must actually exercise the term
```

- [ ] **Step 2: Run it**

```bash
python3 -m pytest tests/test_scorer_mirror.py -v
```

Expected: 4 passed. If `test_ontime_constants_are_mirrored` or
`test_makespan_weights_are_now_equal` fails, Tasks 1 and 2 used different numbers — fix
the constants, never the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scorer_mirror.py
git -c commit.gpgsign=false commit -m "test: the two scorers must agree exactly

Pins every shared constant and asserts both implementations compute the
same breach for the same misses. The 400x makespan divergence survived
weeks because nothing ever compared them.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Ship-gate measurement harness

**Files:**
- Create: `scripts/measure_ontime.py`

**Interfaces:**
- Consumes: `optimizer.plan_metrics`, `optimizer.optimize`, `engine.pipeline.run_forward`.
- Produces: a CLI printing the three ship-gate measures for the old and new objectives. Consumed by a human in Task 5.

**Two traps this harness must avoid, both hit during the superseded earliness work:**

1. **`PlanConfig` is a FROZEN dataclass.** Python bakes dataclass defaults into the
   generated `__init__` at class-creation time, so `PlanConfig.ontime_weight = X` is
   silently ignored by every later `PlanConfig()` call. The only construction site in the
   plan path is `engine/new_engine.py:_plan_config`; override there.
2. **Single runs measure the search's luck, not the objective.** A previous measurement
   found a plan strictly dominated by ~9,700 score points, meaning the search missed a
   basin worth 295 late-days. Best-of-three-seeds is mandatory.

- [ ] **Step 1: Write the harness**

Create `scripts/measure_ontime.py`:

```python
"""Ship gate for the symmetric on-time objective (spec 2026-08-06 §6).

Judged on the OWNER'S measures, not on score — the two objectives are not
comparable by score, so only outcomes count:

    orders inside +/-4 days   must NOT fall
    worst single order        must NOT get worse
    at least one              must strictly improve

    python3 scripts/measure_ontime.py Test9.xlsx --budget 400
"""
import argparse
import dataclasses
import sys
from datetime import date

sys.path.insert(0, ".")

from engine import loaders, new_engine, optimizer
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward

BAND = 4
SEEDS = (42, 7, 2026)

# PlanConfig is a FROZEN dataclass whose defaults are baked into __init__ at class
# creation, so assigning the class attribute is silently ignored. Override at the one
# construction site in the plan path instead.
_ORIG_PLAN_CONFIG = new_engine._plan_config
_OVERRIDE = {}


def _patched_plan_config(config):
    pc = _ORIG_PLAN_CONFIG(config)
    return dataclasses.replace(pc, **_OVERRIDE) if _OVERRIDE else pc


def _install_patch():
    """From main() only — installing at import would redirect any importing process."""
    new_engine._plan_config = _patched_plan_config


def _use_old_objective():
    """Approximate the pre-2026-08-06 objective by switching the on-time term off and
    restoring the old makespan weight, so the baseline is measured on the same code."""
    optimizer.ONTIME_WEIGHT = 0.0
    optimizer.MAKESPAN_WEIGHT = 40.0
    _OVERRIDE.clear()
    _OVERRIDE.update(ontime_weight=0.0, makespan_weight=40.0)


def _use_new_objective():
    optimizer.ONTIME_WEIGHT = 1.0
    optimizer.MAKESPAN_WEIGHT = 0.1
    _OVERRIDE.clear()
    _OVERRIDE.update(ontime_weight=1.0, makespan_weight=0.1)


def _self_check():
    """Prove the knob reaches the search before any number is trusted."""
    _use_new_objective()
    pc = new_engine._plan_config(Config(plan_start_date=date(2026, 8, 6)))
    assert pc.ontime_weight == 1.0, f"ppc knob is dead: {pc.ontime_weight}"
    _use_old_objective()
    pc = new_engine._plan_config(Config(plan_start_date=date(2026, 8, 6)))
    assert pc.ontime_weight == 0.0, f"ppc knob is dead: {pc.ontime_weight}"
    print("self-check OK: the objective switch reaches the search\n")


def _outcomes(so_lines, masters, cfg, budget, seed):
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=budget, seed=seed)
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, cfg, masters, priority_rank=res.ranks)
    m = optimizer.plan_metrics(pr.schedule, so_lines, cfg.plan_start_date)

    due = {(l.so_no, l.item_code): l.delivery_date for l in so_lines}
    expected = {}
    for e in pr.schedule:
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            d = e.end.date()
            if k not in expected or d > expected[k]:
                expected[k] = d
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    return {
        "on_time": sum(1 for g in gaps if abs(g) <= BAND),
        "late_beyond": sum(1 for g in gaps if g > BAND),
        "early_beyond": sum(1 for g in gaps if g < -BAND),
        "worst": max((g for g in gaps if g > 0), default=0),
        "total_late": m["total_late_days"],
        "makespan": m["makespan_days"],
        "orders": len(gaps),
    }


def _best_of_seeds(so_lines, masters, cfg, budget, label):
    runs = [_outcomes(so_lines, masters, cfg, budget, s) for s in SEEDS]
    for s, r in zip(SEEDS, runs):
        print(f"    seed {s:5d}: on-time {r['on_time']:3d}  worst {r['worst']:3d}  "
              f"late>{BAND} {r['late_beyond']:3d}  early>{BAND} {r['early_beyond']:3d}")
    # "best" = most on time, worst order breaking ties
    best = max(runs, key=lambda r: (r["on_time"], -r["worst"]))
    print(f"  {label:22s} BEST-OF-3: on-time {best['on_time']}/{best['orders']}  "
          f"worst {best['worst']}d  late>{BAND} {best['late_beyond']}  "
          f"early>{BAND} {best['early_beyond']}  (total late-days {best['total_late']}, "
          f"makespan {best['makespan']:.1f})\n")
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--start", default="2026-08-06")
    args = ap.parse_args()

    _install_patch()
    with open(args.workbook, "rb") as fh:
        new_engine.set_masters_bytes(fh.read())
    so_lines, masters = loaders.load_all(args.workbook)
    cfg = Config(plan_start_date=date.fromisoformat(args.start), scheduler="new",
                 overlap_percent=84, flexible_machines=True,
                 apply_operator_logic=True, consolidation_window_days=10)
    _self_check()
    print(f"{len(so_lines)} SO lines | budget {args.budget} | seeds {SEEDS}\n")

    print("OLD objective (on-time term off, makespan 40)")
    _use_old_objective()
    old = _best_of_seeds(so_lines, masters, cfg, args.budget, "OLD")

    print("NEW objective (symmetric on-time, makespan 0.1 tie-break)")
    _use_new_objective()
    new = _best_of_seeds(so_lines, masters, cfg, args.budget, "NEW")

    print("=" * 70)
    print("SHIP GATE")
    print("=" * 70)
    on_time_ok = new["on_time"] >= old["on_time"]
    worst_ok = new["worst"] <= old["worst"]
    improved = new["on_time"] > old["on_time"] or new["worst"] < old["worst"]
    print(f"  orders inside +/-{BAND} days : {old['on_time']:3d} -> {new['on_time']:3d}   "
          f"{'OK' if on_time_ok else 'FAIL (fell)'}")
    print(f"  worst single order        : {old['worst']:3d} -> {new['worst']:3d}   "
          f"{'OK' if worst_ok else 'FAIL (worse)'}")
    print(f"  at least one improved     : {'OK' if improved else 'FAIL (no change)'}")
    print(f"\n  VERDICT: {'SHIP' if (on_time_ok and worst_ok and improved) else 'DO NOT SHIP'}")
    print(f"\n  (reported, not gated: late>{BAND} {old['late_beyond']} -> {new['late_beyond']}, "
          f"early>{BAND} {old['early_beyond']} -> {new['early_beyond']}, "
          f"makespan {old['makespan']:.1f} -> {new['makespan']:.1f})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test it**

```bash
python3 scripts/measure_ontime.py Test9.xlsx --budget 20
```

Expected: the self-check line prints, then six seed lines and a verdict, without error.
Budget 20 is a smoke test only — **draw no conclusion from those numbers.**

If the self-check raises, STOP and report. Every number after a failed self-check is
worthless.

- [ ] **Step 3: Commit**

```bash
git add scripts/measure_ontime.py
git -c commit.gpgsign=false commit -m "chore: ship-gate harness for the on-time objective

Judged on the owner's measures (orders inside the band, worst order), not
on score - the two objectives are not score-comparable. Best-of-3-seeds,
because single runs measure the search's luck.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Run the gate and decide

This task has no predetermined outcome. **It may end in not shipping**, which the owner
accepted when approving the gate.

**Files:**
- Modify: `CLAUDE.md` (only if the gate passes)

**Interfaces:**
- Consumes: `scripts/measure_ontime.py` from Task 4.
- Produces: a decision, and the `CLAUDE.md` record of it.

- [ ] **Step 1: Run the full measurement**

```bash
python3 scripts/measure_ontime.py Test9.xlsx --budget 400 2>&1 | tee /tmp/ontime-gate.txt
```

Six optimizations at 400 evaluations. Expect roughly 20 minutes.

- [ ] **Step 2: Apply the gate, and report it honestly**

The harness prints the verdict. Report the numbers to the owner **as printed**, including
the reported-not-gated line. Do not soften a DO NOT SHIP.

Reference point from the live book on 2026-08-06: 25 of 68 orders inside ±4 days, worst
order 38 days, 36 late beyond the band, 7 early beyond it. The workbook baseline will
differ — `Test9.xlsx` carries no recorded production — so compare OLD against NEW within
this run, never against the live figures.

- [ ] **Step 3: If the gate FAILS, stop here**

Report that the objective did not clear its own gate, with the numbers. Do not adjust the
band, the cap or the weights to make it pass — that is fitting the gate to the result.
The branch stays unmerged pending the owner's decision.

- [ ] **Step 4: If the gate PASSES, update `CLAUDE.md`**

Add a bullet in the banner's style recording: the objective is now ONE symmetric squared
on-time term (±4-day band, cap 60) replacing `total_late_days`, `slip_severity` and the
search's fairness term; makespan is a 0.1 tie-break, no longer 40; `slip_severity` is
still computed and reported but no longer scored; `ceiling_breach` and
`committed_promise_breach` are unchanged and dormant; and the measured gate result.

Also correct the stale line in the existing optimizer bullet that describes the score as
`total_late_days + 10×makespan_days` — it has been wrong since the severity term landed
on 2026-07-24 and it is what led a previous session to misremember the formula.

- [ ] **Step 5: Full suite and golden trace**

```bash
python3 -m pytest -q
python3 -m pytest -k golden -v
```

The golden trace must pass **without regeneration** — it covers Rules 1–6 and does not run
the optimizer. If it fails, STOP: the change has leaked into the planning path, which the
spec forbids. Never run `REGEN_GOLDEN=1` to make it pass.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git -c commit.gpgsign=false commit -m "docs: record the on-time objective gate result

<paste the measured OLD -> NEW numbers and the verdict>

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Deployment note

Render auto-deploys `main`, so merging is a production release. **Unlike the superseded
earliness branch, this one is not inert** — it changes which plan wins, therefore what the
floor runs. The Gantt will show different bars and the Excel different dates; every screen,
column and export keeps working, but the content changes.

Do not merge until Task 5's gate has passed and the owner has seen the numbers.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3 formula, band 4, cap 60, weight 1.0 | 1, 2 |
| §3 makespan tie-break 0.1 | 1, 2 (+ mirror assertion in 3) |
| §3.1 squaring spreads; flat band | 1, 2 (`test_squaring_spreads_the_misses`, `test_inside_the_band_costs_nothing_either_direction`) |
| §3.2 what each removed term is replaced by | 1 (engine), 2 (ppc, incl. deleting `_severity` and fairness) |
| §3.3 ceiling + promise untouched | 1, 2 (both retained in `score`); asserted in 3 |
| §3.4 both scorers identical | 3 |
| §4 three files change | 1, 2 |
| §4.1 `plan_metrics` keeps every field | 1 (`test_plan_metrics_keeps_every_reported_field`) |
| §4.2 no call-site changes | Global Constraints |
| §5 scope boundary | Global Constraints + Deployment note |
| §6 unit tests, mirror test, ship gate, best-of-3, golden | 1, 2, 3, 4, 5 |
| §7 risk: not inert | Deployment note |
| §8 delete earliness artefacts | Done before this plan — branch deleted, base rebased onto `main` |

No gaps.

**Placeholder scan:** the only bracketed marker is `<paste the measured … numbers>` in
Task 5 Step 6, where the value is that task's deliverable and the criterion for producing
it is fully specified. No "TBD", no "handle edge cases", no "similar to Task N".

**Type consistency:** `ontime_breach` is the dict key in Tasks 1, 3 and 4. `_ontime_breach`
is the ppc function in Tasks 2 and 3. `ontime_band_days` / `ontime_cap_days` /
`ontime_weight` are the `PlanConfig` fields in Tasks 2, 3 and 4. `ONTIME_BAND_DAYS` /
`ONTIME_CAP_DAYS` / `ONTIME_WEIGHT` / `MAKESPAN_WEIGHT` are the module constants in Tasks
1, 3 and 4. `_use_old_objective` / `_use_new_objective` / `_best_of_seeds` appear only in
Task 4. Checked and consistent.
