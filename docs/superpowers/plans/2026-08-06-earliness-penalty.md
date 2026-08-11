# Earliness Penalty Implementation Plan

> **SUPERSEDED (2026-08-06) by `2026-08-06-symmetric-ontime-objective-design.md`.**
> The branch this described was deleted, not merged. Do not execute this plan.

> *(Historical, do not act on: this plan originally carried an agentic-worker
> directive to execute it task-by-task. It has been struck because the phrasing is
> an execution trigger for this repo's tooling, and this plan must not be run.)*

**Goal:** Make the optimizer stop scheduling work that finishes more than 4 days before its delivery date, without ever increasing total late-days.

**Architecture:** A linear penalty term is added to both mirrored scorers (`engine/optimizer.score` and `ppc_engine/objective/objective.score`), reading signed lateness that both already compute. A no-regression gate on `total_late_days` is added to both apply paths so the penalty can never buy tidiness with lateness. Weights are then set by measurement, not by choice.

**Tech Stack:** Python 3, pytest, openpyxl. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-06-earliness-penalty-design.md`

## Global Constraints

- **Grace period is exactly 4 days.** Finishing 4 days early or less costs nothing.
- **Penalty is LINEAR, never squared.** Unlike `_severity`, `_ceiling_breach` and `_committed_promise_breach`, which all square their overage. Reason in spec §3.1.
- **The two scorers must agree numerically.** `EARLINESS_WEIGHT` in `engine/optimizer.py` and `earliness_weight` in `ppc_engine/config.py` must hold identical values, as `SEVERITY_*` already do.
- **Never worsen lateness.** No applied plan may have a higher `total_late_days` than the incumbent.
- **Additive and inert by default.** With no order beyond the grace, the term is 0.0 and every plan must be byte-identical to today.
- **Do not touch:** the scheduler, any rule, any UI, Settings, `engine/new_engine.py`, `engine/config.py`, freeze, absences, or the contest payload.
- **All 508 existing tests must stay green.**
- Constants carry the house comment: measured, must stay equal, re-measure before moving.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `engine/optimizer.py` | Constants + `earliness_breach` in `plan_metrics` + term in `score` | 1 |
| `tests/test_earliness_metric.py` | Engine-side metric behaviour | 1 |
| `ppc_engine/config.py` | `PlanConfig` earliness defaults | 2 |
| `ppc_engine/objective/objective.py` | `_earliness_breach` + term in `score` | 2 |
| `tests/test_ppc_earliness_metric.py` | ppc-side metric behaviour | 2 |
| `tests/test_earliness_mirror.py` | The two scorers agree; weight constants pinned | 3 |
| `api/main.py` | `lateness_ok` gate on both apply paths | 4 |
| `tests/test_earliness_backstop.py` | Both apply paths refuse a lateness regression | 4 |
| `scripts/measure_earliness.py` | Measurement harness (dev tool, not runtime) | 5 |
| constants + pinned test | Final measured values | 6 |

---

### Task 1: Engine-side metric and score term

**Files:**
- Modify: `engine/optimizer.py` (constants after line 64; `plan_metrics` around lines 136-172; `score` lines 85-94)
- Test: `tests/test_earliness_metric.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: module constants `EARLINESS_GRACE_DAYS: float` and `EARLINESS_WEIGHT: float`; a new key `"earliness_breach": float` in the dict returned by `optimizer.plan_metrics(...)`; `optimizer.score(metrics: dict) -> float` now reads that key via `.get`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_earliness_metric.py`:

```python
"""Earliness penalty, engine side (spec 2026-08-06 §4.2).

The optimizer could not tell "delivered on the due date" from "delivered 37 days
early" — every scoring term was one-sided. This adds the missing term.

LINEAR, not squared (spec §3.1): earliness harm scales with the contended machine
time an order eats, not with how early it is. On the live book a 37-days-early
11-piece order caused 38h of others' waiting while a 10-days-early 400-piece order
caused 558h — squaring would chase the wrong one 30x harder.
"""
from datetime import date, datetime

from engine import optimizer
from engine.models import SOLine, ScheduleEntry


def _line(so, item, due, qty=10):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=qty,
                  delivery_date=due)


def _entry(so, item, end):
    return ScheduleEntry(batch_id=so, item_code=item, process_seq=1,
                         process_name="CNC", machine="CNC1", qty=10,
                         occupancy_min=60, start=datetime(2026, 8, 6, 8, 0),
                         end=end, so_refs=[so])


PS = date(2026, 8, 6)


def test_exactly_at_grace_costs_nothing():
    """4 days early is the owner's stated acceptable limit — zero breach."""
    lines = [_line("SO1", "IT-A", date(2026, 8, 20))]
    sched = [_entry("SO1", "IT-A", datetime(2026, 8, 16, 17, 0))]   # 4d early
    m = optimizer.plan_metrics(sched, lines, PS)
    assert m["earliness_breach"] == 0.0


def test_one_day_past_grace_costs_one():
    """5 days early -> 1 breach-day. Linear, so the value equals the overage."""
    lines = [_line("SO1", "IT-A", date(2026, 8, 20))]
    sched = [_entry("SO1", "IT-A", datetime(2026, 8, 15, 17, 0))]   # 5d early
    m = optimizer.plan_metrics(sched, lines, PS)
    assert m["earliness_breach"] == 1.0


def test_linear_not_squared():
    """The design decision, pinned. 37 days early -> 33, NOT 33^2 = 1089."""
    lines = [_line("SO1", "IT-A", date(2026, 9, 14))]
    sched = [_entry("SO1", "IT-A", datetime(2026, 8, 8, 17, 0))]    # 37d early
    m = optimizer.plan_metrics(sched, lines, PS)
    assert m["earliness_breach"] == 33.0


def test_late_and_on_the_day_contribute_nothing():
    lines = [_line("SO1", "IT-A", date(2026, 8, 10)),
             _line("SO2", "IT-B", date(2026, 8, 20))]
    sched = [_entry("SO1", "IT-A", datetime(2026, 8, 25, 17, 0)),   # 15d LATE
             _entry("SO2", "IT-B", datetime(2026, 8, 20, 17, 0))]   # on the day
    m = optimizer.plan_metrics(sched, lines, PS)
    assert m["earliness_breach"] == 0.0
    assert m["total_late_days"] == 15


def test_several_orders_sum():
    """The live book's 7 offenders: 37,10,10,10,8,8,6 days early
    -> 33+6+6+6+4+4+2 = 61 breach-days (spec §2)."""
    days_early = [37, 10, 10, 10, 8, 8, 6]
    due = date(2026, 10, 1)
    lines, sched = [], []
    for n, de in enumerate(days_early):
        so = f"SO{n}"
        lines.append(_line(so, f"IT-{n}", due))
        end = datetime(2026, 10, 1, 17, 0) - __import__("datetime").timedelta(days=de)
        sched.append(_entry(so, f"IT-{n}", end))
    m = optimizer.plan_metrics(sched, lines, PS)
    assert m["earliness_breach"] == 61.0


def test_inert_case_is_byte_identical():
    """Global constraint: nothing beyond the grace -> term 0 -> score unchanged."""
    lines = [_line("SO1", "IT-A", date(2026, 8, 20))]
    sched = [_entry("SO1", "IT-A", datetime(2026, 8, 25, 17, 0))]   # late, not early
    m = optimizer.plan_metrics(sched, lines, PS)
    assert m["earliness_breach"] == 0.0
    stripped = {k: v for k, v in m.items() if k != "earliness_breach"}
    assert optimizer.score(m) == optimizer.score(stripped)


def test_score_includes_the_weighted_term():
    base = {"total_late_days": 100, "makespan_days": 50.0}
    with_breach = dict(base, earliness_breach=10.0)
    delta = optimizer.score(with_breach) - optimizer.score(base)
    assert abs(delta - optimizer.EARLINESS_WEIGHT * 10.0) < 1e-9
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/Users/ritinwadekar/Desktop/Anvitech Rebuilt"
python -m pytest tests/test_earliness_metric.py -v
```

Expected: FAIL. `KeyError: 'earliness_breach'` on the metric tests, and
`AttributeError: module 'engine.optimizer' has no attribute 'EARLINESS_WEIGHT'`
on the last one.

- [ ] **Step 3: Add the constants**

In `engine/optimizer.py`, immediately after the `COMMITTED_PROMISE_WEIGHT = 5000.0`
block (line 64), add:

```python
# Earliness penalty (2026-08-06 spec). Anvitech does not want stock built weeks
# before the customer will take it: it ties up cash and floor space, and it eats
# contended machine time that late orders are queued for. Measured on the live book
# 2026-08-06: 7 orders finished >4d early and 751.4h of other orders' waiting was
# attributable to them (4.3% of all machine-contention waiting).
#
# LINEAR, deliberately NOT squared like the four guards above. Earliness harm scales
# with the contended machine time the order consumes, not with how early it is: a
# 37-days-early 11-piece order caused 38h of others' waiting while a 10-days-early
# 400-piece order caused 558h. A convex curve would chase the first 30x harder than
# the second. Spec §3.1.
#
# Must stay numerically EQUAL to ppc_engine/config.py earliness_* — the contest
# winner-pick and the sequence search must judge earliness the same way.
# Re-measure before moving (spec §6).
EARLINESS_GRACE_DAYS = 4.0    # == ppc_engine earliness_grace_days
EARLINESS_WEIGHT = 20.0       # == ppc_engine earliness_weight; PLACEHOLDER until Task 6
```

- [ ] **Step 4: Add the metric**

In `engine/optimizer.py:plan_metrics`, after the `committed_promise_breach` loop and
before the `result = {` dict (currently around line 162), add:

```python
    # Earliness beyond the grace, LINEAR (spec §3.1). `gaps` is signed: a negative
    # gap is an order finishing early, which every other term ignores entirely.
    earliness_breach = 0.0
    for g in gaps:
        over = -g - EARLINESS_GRACE_DAYS
        if over > 0:
            earliness_breach += float(over)
```

Then add one key to the `result` dict, after `"committed_promise_breach"`:

```python
        "earliness_breach": round(earliness_breach, 2),
```

- [ ] **Step 5: Add the term to score**

In `engine/optimizer.py:score`, add a final line to the returned expression:

```python
def score(metrics: dict) -> float:
    """Lower is better: lateness + makespan + convex slip guard + worst-order ceiling
    barrier + committed-promise ceiling + earliness penalty. Each added term reads a
    field plan_metrics supplies; ``.get`` keeps legacy metrics dicts safe
    (byte-identical when the field is absent/zero)."""
    return (metrics["total_late_days"]
            + MAKESPAN_WEIGHT * metrics["makespan_days"]
            + SEVERITY_WEIGHT * metrics.get("slip_severity", 0.0)
            + CEILING_WEIGHT * metrics.get("ceiling_breach", 0.0)
            + COMMITTED_PROMISE_WEIGHT * metrics.get("committed_promise_breach", 0.0)
            + EARLINESS_WEIGHT * metrics.get("earliness_breach", 0.0))
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
python -m pytest tests/test_earliness_metric.py -v
```

Expected: 7 passed.

- [ ] **Step 7: Run the full suite**

```bash
python -m pytest -q
```

Expected: all pass. If anything fails, the term is not inert somewhere — investigate
before continuing; do not adjust the failing test to match.

- [ ] **Step 8: Commit**

```bash
git add engine/optimizer.py tests/test_earliness_metric.py
git commit -m "feat: earliness breach metric and score term (engine side)

Linear penalty on days an order finishes more than 4 early. Deliberately not
squared like the other four guards: earliness harm scales with contended machine
time consumed, not with how early the order is (spec 2026-08-06 section 3.1).

Weight is a placeholder until the measurement stage sets it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: ppc_engine-side metric and score term

**Files:**
- Modify: `ppc_engine/config.py` (after the `committed_promise_weight` block, ~line 127)
- Modify: `ppc_engine/objective/objective.py` (new function after `_committed_promise_breach`; `score` at the end)
- Test: `tests/test_ppc_earliness_metric.py` (create)

**Interfaces:**
- Consumes: `EARLINESS_GRACE_DAYS` / `EARLINESS_WEIGHT` values from Task 1 (mirrored, not imported — the packages stay independent).
- Produces: `PlanConfig.earliness_grace_days: float`, `PlanConfig.earliness_weight: float`; `ppc_engine.objective.objective._earliness_breach(metrics: PlanMetrics, config: PlanConfig) -> float`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ppc_earliness_metric.py`:

```python
"""Earliness penalty, ppc_engine side (spec 2026-08-06 §4.1).

Mirror of tests/test_earliness_metric.py. The two scorers must agree — see
tests/test_earliness_mirror.py.
"""
from datetime import datetime

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _earliness_breach, score


def _pm(lateness_by_order):
    """Minimal PlanMetrics — only lateness_by_order varies. Values are SIGNED
    days: negative means the order finished early."""
    return PlanMetrics(
        total_tardiness_days=0.0,
        max_tardiness_days=0.0,
        late_order_count=0,
        makespan_days=0.0,
        lateness_by_order=lateness_by_order,
        promise_slip_by_order={},
    )


CFG = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0),
                 earliness_grace_days=4.0, earliness_weight=20.0)


def test_exactly_at_grace_costs_nothing():
    assert _earliness_breach(_pm({("A", "x"): -4.0}), CFG) == 0.0


def test_one_day_past_grace_costs_one():
    assert _earliness_breach(_pm({("A", "x"): -5.0}), CFG) == 1.0


def test_linear_not_squared():
    """37 days early -> 33, NOT 33^2. Spec §3.1."""
    assert _earliness_breach(_pm({("A", "x"): -37.0}), CFG) == 33.0


def test_late_and_on_the_day_contribute_nothing():
    assert _earliness_breach(_pm({("A", "x"): 15.0, ("B", "y"): 0.0}), CFG) == 0.0


def test_several_orders_sum():
    """The live book's 7 offenders -> 61 breach-days (spec §2)."""
    lat = {(f"SO{n}", "x"): -float(d)
           for n, d in enumerate([37, 10, 10, 10, 8, 8, 6])}
    assert _earliness_breach(_pm(lat), CFG) == 61.0


def test_term_in_score():
    within = score(_pm({("A", "x"): -4.0}), CFG)    # at grace -> no breach
    over = score(_pm({("A", "x"): -14.0}), CFG)     # 10 over grace -> +20*10
    assert over > within
    assert abs((over - within) - 20.0 * 10.0) < 1e-6


def test_no_early_order_contributes_zero():
    """Inert case: an all-late book scores as if the term did not exist."""
    lat = {("A", "x"): 5.0, ("B", "y"): 12.0}
    cfg_off = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0), earliness_weight=0.0)
    assert score(_pm(lat), CFG) == score(_pm(lat), cfg_off)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest tests/test_ppc_earliness_metric.py -v
```

Expected: FAIL with `ImportError: cannot import name '_earliness_breach'`.

- [ ] **Step 3: Add the PlanConfig fields**

In `ppc_engine/config.py`, after the `committed_promise_weight: float = 5000.0` line,
add:

```python
    # Earliness penalty (2026-08-06 spec). Orders finishing more than
    # earliness_grace_days before their due date are penalized LINEARLY per day
    # beyond the grace. Deliberately NOT squared like severity/ceiling/promise above:
    # earliness harm scales with the contended machine time the order eats, not with
    # how early it is, so a convex curve would chase a tiny 37-days-early order
    # 30x harder than the big 10-days-early one doing 15x more damage (spec §3.1).
    # 0 contribution when nothing breaches — additive, byte-identical otherwise.
    # Must equal engine/optimizer.py EARLINESS_* . Re-measure before moving.
    earliness_grace_days: float = 4.0
    earliness_weight: float = 20.0      # PLACEHOLDER until the measurement stage
```

- [ ] **Step 4: Add the breach function**

In `ppc_engine/objective/objective.py`, after `_committed_promise_breach`, add:

```python
def _earliness_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Sum of days each order finishes MORE than the grace before its due date.

    LINEAR, unlike every other guard in this file, which square their overage.
    Earliness harm scales with the contended machine time the order consumes, not
    with how early it is — measured on the live book, a 37-days-early 11-piece order
    blocked 38h of other orders while a 10-days-early 400-piece order blocked 558h.
    Squaring would point the search at the harmless one. See the 2026-08-06 spec §3.1.

    0 when nothing breaches — additive, byte-identical otherwise.
    """
    grace = config.earliness_grace_days
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = -late - grace          # `late` is signed; early -> negative
        if over > 0:
            total += over
    return total
```

- [ ] **Step 5: Add the term to score**

Update the docstring line and the returned expression in `ppc_engine/objective/objective.py:score`:

```python
def score(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Score a plan from its metrics. Lower is better."""
    return (
        metrics.total_tardiness_days
        + config.severity_weight * _severity(metrics, config)
        + config.ceiling_weight * _ceiling_breach(metrics, config)
        + config.committed_promise_weight * _committed_promise_breach(metrics, config)
        + config.earliness_weight * _earliness_breach(metrics, config)
        + config.fairness_weight * metrics.max_tardiness_days
        + config.makespan_weight * metrics.makespan_days
    )
```

Also add one line to the module docstring's formula block, after the `w · makespan` line:

```
          + e · earliness_breach     # linear: don't build weeks before it's wanted
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
python -m pytest tests/test_ppc_earliness_metric.py -v
```

Expected: 7 passed.

- [ ] **Step 7: Run the full suite**

```bash
python -m pytest -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add ppc_engine/config.py ppc_engine/objective/objective.py tests/test_ppc_earliness_metric.py
git commit -m "feat: earliness breach term in the ppc_engine objective

Mirrors the engine-side term added in the previous commit. PlanConfig defaults
carry the values, so engine/new_engine._plan_config needs no change (same as the
severity_* constants).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Mirror guard

The 400x makespan divergence (`engine/optimizer.py:40` is `40.0`, `ppc_engine/config.py:87`
is `0.1`, both documented as mirrors) exists because nothing asserts the scorers agree.
This task installs that guard.

**Files:**
- Test: `tests/test_earliness_mirror.py` (create)

**Interfaces:**
- Consumes: `optimizer.EARLINESS_GRACE_DAYS`, `optimizer.EARLINESS_WEIGHT` (Task 1);
  `PlanConfig.earliness_grace_days`, `PlanConfig.earliness_weight` (Task 2).
- Produces: nothing consumed downstream.

- [ ] **Step 1: Write the test**

Create `tests/test_earliness_mirror.py`:

```python
"""The two scorers must judge a plan the same way.

engine/optimizer.py scores the CONTEST winner-pick and the apply gate;
ppc_engine/objective scores the inner SEQUENCE search. Both files claim their
weights are kept numerically equal. For severity/ceiling/promise they are. For
makespan they diverge 400x (40.0 vs 0.1) and nothing caught it, because no test
ever asserted it. This is that test.
"""
from datetime import date, datetime

from engine import optimizer
from engine.models import SOLine, ScheduleEntry
from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _earliness_breach


def test_earliness_constants_are_mirrored():
    """The invariant both files' comments promise."""
    cfg = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))
    assert optimizer.EARLINESS_GRACE_DAYS == cfg.earliness_grace_days
    assert optimizer.EARLINESS_WEIGHT == cfg.earliness_weight


def test_severity_constants_are_mirrored():
    """Regression guard on the existing mirrored trio."""
    cfg = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))
    assert optimizer.SEVERITY_TOLERANCE_DAYS == cfg.severity_tolerance_days
    assert optimizer.SEVERITY_WEIGHT == cfg.severity_weight
    assert optimizer.SEVERITY_CAP_DAYS == cfg.severity_cap_days
    assert optimizer.CEILING_WEIGHT == cfg.ceiling_weight
    assert optimizer.COMMITTED_PROMISE_WEIGHT == cfg.committed_promise_weight


def test_makespan_weights_are_pinned_and_known_to_diverge():
    """NOT an equality assertion — they genuinely differ today.

    engine/optimizer.py MAKESPAN_WEIGHT was measured 2026-07-19 under the crew-smart
    CLASSIC scheduler, before the current engine went live; ppc_engine has always
    used 0.1. Which is correct is being measured (spec §6.1). Until that lands, pin
    both so neither drifts silently, and so the measurement task must consciously
    update this test.
    """
    cfg = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))
    assert optimizer.MAKESPAN_WEIGHT == 40.0
    assert cfg.makespan_weight == 0.1


def test_both_implementations_compute_the_same_breach():
    """Same orders, same due dates, same completions -> same breach number."""
    days_early = [37, 10, 10, 10, 8, 8, 6, 4, 0, -12]   # last two: at grace, on day, late
    due = date(2026, 10, 1)
    ps = date(2026, 8, 6)
    lines, sched, lateness = [], [], {}
    for n, de in enumerate(days_early):
        so, item = f"SO{n}", f"IT-{n}"
        lines.append(SOLine(so_no=so, item_code=item, item_name=item, qty=10,
                            delivery_date=due))
        end = datetime(2026, 10, 1, 17, 0) - __import__("datetime").timedelta(days=de)
        sched.append(ScheduleEntry(batch_id=so, item_code=item, process_seq=1,
                                   process_name="CNC", machine="CNC1", qty=10,
                                   occupancy_min=60,
                                   start=datetime(2026, 8, 6, 8, 0), end=end,
                                   so_refs=[so]))
        lateness[(so, item)] = float(-de)      # signed: early is negative

    engine_breach = optimizer.plan_metrics(sched, lines, ps)["earliness_breach"]
    ppc_breach = _earliness_breach(
        PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=0.0,
                    lateness_by_order=lateness, promise_slip_by_order={}),
        PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0)))
    assert engine_breach == ppc_breach
```

- [ ] **Step 2: Run the tests**

```bash
python -m pytest tests/test_earliness_mirror.py -v
```

Expected: 4 passed. If `test_earliness_constants_are_mirrored` fails, Task 1 and
Task 2 used different numbers — fix the constants, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_earliness_mirror.py
git commit -m "test: guard that the two scorers stay mirrored

The 400x makespan divergence (40.0 vs 0.1) survived because nothing asserted the
mirror invariant both files' comments promise. Pins the earliness and severity
constants as equal, and pins the makespan pair at their known-divergent values so
the measurement task has to update this deliberately.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Lateness no-regression gate on both apply paths

**Files:**
- Modify: `api/main.py:1779-1802` (`_auto_apply_result`)
- Modify: `api/main.py:2416-2421` (`optimize_apply_ep`)
- Test: `tests/test_earliness_backstop.py` (create)

**Interfaces:**
- Consumes: `optimizer.score` from Task 1; the existing `_incumbent_metrics() -> dict`
  which returns at least `total_late_days`, `makespan_days`, `max_late_days`,
  `max_committed_slip`.
- Produces: no new callable. `_optimize_apply()` stays a pure persist function with
  no gate inside it, so its other caller is unaffected.

- [ ] **Step 1: Write the failing test**

Create `tests/test_earliness_backstop.py`:

```python
"""Lateness no-regression gate (spec 2026-08-06 §5.2).

Owner's rule: the earliness penalty may reorder work freely, but a plan that
reduces earliness at the cost of MORE total late-days must never be applied.

Weight ratios cannot enforce this — the raw total_late_days term is only 3.4% of
the live score, the rest being the convex severity curve. So it is enforced
structurally, at the apply gate, on BOTH paths.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store, optimizer
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _stage(monkeypatch, inc_late, best_late, best_breach):
    """Seed a book, stub the incumbent, stage a contest result whose SCORE wins
    purely because of a big earliness improvement, and return the api module."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    book_store.save_plan_priority({"k": 1}, {"saved_at": "t"})
    monkeypatch.setattr(m, "_incumbent_metrics",
                        lambda: {"total_late_days": inc_late, "makespan_days": 50.0,
                                 "max_late_days": 46, "max_committed_slip": 0,
                                 "earliness_breach": 200.0})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": best_late,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46,
                                          "max_committed_slip": 0,
                                          "earliness_breach": best_breach},
                                 "ranks": {"k": 2},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}
        m._OPTIMIZE["auto"] = True
    return m


def test_auto_apply_rejects_a_lateness_regression(monkeypatch):
    """Earliness collapses 200 -> 0 but late-days rise 500 -> 520. Score wins;
    the gate must still refuse."""
    m = _stage(monkeypatch, inc_late=500, best_late=520, best_breach=0.0)
    assert (optimizer.score({"total_late_days": 520, "makespan_days": 50.0,
                             "earliness_breach": 0.0}) <
            optimizer.score({"total_late_days": 500, "makespan_days": 50.0,
                             "earliness_breach": 200.0})), "premise: score prefers it"

    m._auto_apply_result()

    assert book_store.load_plan_priority()["ranks"] == {"k": 1}     # untouched
    assert "late" in book_store.load_auto_note()["text"].lower()


def test_auto_apply_accepts_equal_lateness(monkeypatch):
    """Same late-days, less earliness -> the win we actually want."""
    m = _stage(monkeypatch, inc_late=500, best_late=500, best_breach=0.0)
    m._auto_apply_result()
    assert book_store.load_plan_priority()["ranks"] == {"k": 2}


def test_auto_apply_accepts_lateness_improvement(monkeypatch):
    m = _stage(monkeypatch, inc_late=500, best_late=460, best_breach=0.0)
    m._auto_apply_result()
    assert book_store.load_plan_priority()["ranks"] == {"k": 2}


def test_manual_apply_rejects_a_lateness_regression(monkeypatch):
    """The manual Apply button applied unconditionally before this change
    (a gap recorded in CLAUDE.md). It must now honour the same rule."""
    from fastapi import HTTPException
    m = _stage(monkeypatch, inc_late=500, best_late=520, best_breach=0.0)

    class _Req:
        pass
    req = _Req()
    monkeypatch.setattr(m, "require_admin", lambda r: None)

    with pytest.raises(HTTPException) as e:
        m.optimize_apply_ep(req)
    assert e.value.status_code == 409
    assert "late" in str(e.value.detail).lower()
    assert book_store.load_plan_priority()["ranks"] == {"k": 1}     # untouched


def test_manual_apply_allows_a_clean_win(monkeypatch):
    m = _stage(monkeypatch, inc_late=500, best_late=460, best_breach=0.0)

    class _Req:
        pass
    monkeypatch.setattr(m, "require_admin", lambda r: None)
    m.optimize_apply_ep(_Req())
    assert book_store.load_plan_priority()["ranks"] == {"k": 2}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest tests/test_earliness_backstop.py -v
```

Expected: FAIL. The auto regression test applies when it should refuse; the manual
regression test raises no `HTTPException`.

- [ ] **Step 3: Add the gate to the auto path**

In `api/main.py:_auto_apply_result`, after the `promise_ok` line, add:

```python
    # Earliness must never be bought with lateness (2026-08-06 spec §5.2). The
    # in-search weight is only a hint: total_late_days is 3.4% of the live score,
    # so a plan CAN win on score while delivering more days late. This is the
    # structural guarantee.
    lateness_ok = best.get("total_late_days", 0) <= inc.get("total_late_days", 0)
```

Change the apply condition to include it:

```python
    if optimizer.score(best) < optimizer.score(inc) and worst_ok and promise_ok and lateness_ok:
```

Add a matching branch, immediately before the final `else:`:

```python
    elif not lateness_ok:
        regress = best.get("total_late_days", 0) - inc.get("total_late_days", 0)
        _auto_note_write(f"Checked {stamp}: kept the current plan — the best "
                         f"alternative finished less work early but delivered "
                         f"{regress} more late-days.")
```

- [ ] **Step 4: Add the gate to the manual path**

Replace `api/main.py:optimize_apply_ep` with:

```python
@app.post("/optimize/apply")
def optimize_apply_ep(request: Request):
    """Persist the last completed run's optimized order — every Plan replays it.
    Admin only.

    Gated on the same no-regression rule as the auto path (2026-08-06 spec §5.2):
    a plan that delivers more total late-days than the current one is refused, even
    when an admin presses Apply deliberately. Before that spec this endpoint applied
    unconditionally, so the worst-order and committed-promise guards were also
    bypassed here; the lateness rule now holds on both paths.
    """
    require_admin(request)
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result") or {}
        best = res.get("best")
    if best:
        try:
            inc = _incumbent_metrics()
        except Exception:  # noqa: BLE001 — never block Apply on a comparison failure
            inc = None
        if inc and best.get("total_late_days", 0) > inc.get("total_late_days", 0):
            regress = best.get("total_late_days", 0) - inc.get("total_late_days", 0)
            raise HTTPException(
                status_code=409,
                detail=(f"Not applied: this plan delivers {regress} more late-days "
                        f"than the current one "
                        f"({best.get('total_late_days')} vs {inc.get('total_late_days')})."))
    return _optimize_apply()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
python -m pytest tests/test_earliness_backstop.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Run the neighbouring gate tests, then the full suite**

```bash
python -m pytest tests/test_promise_backstop.py tests/test_auto_optimize.py tests/test_optimize_endpoints.py -v
python -m pytest -q
```

Expected: all pass. `test_optimize_endpoints.py` exercises the manual Apply path and
is the most likely place to surface an unintended behaviour change — if it fails,
read the failure carefully before touching it.

- [ ] **Step 7: Commit**

```bash
git add api/main.py tests/test_earliness_backstop.py
git commit -m "feat: refuse any applied plan that increases total late-days

The earliness penalty must never be paid for with lateness. Weight ratios cannot
guarantee that (raw late-days is 3.4% of the live score; the convex severity term
is 81%), so the rule is enforced at the apply gate instead.

Applied to the manual Apply button as well, which previously applied
unconditionally with no guards at all.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Measurement harness

**Files:**
- Create: `scripts/measure_earliness.py`

**Interfaces:**
- Consumes: `optimizer.EARLINESS_WEIGHT`, `optimizer.MAKESPAN_WEIGHT` (Task 1);
  `PlanConfig.earliness_weight`, `PlanConfig.makespan_weight` (Task 2).
- Produces: a CLI reporting `late_days`, `makespan_days` and `earliness_breach` per
  weight combination. Consumed by a human in Task 6, not by code.

**Measurement basis and its limitation.** The harness runs on a workbook
(`Test9.xlsx`), which carries the real masters and SO list but **not** the recorded
production actuals the live book has. Absolute numbers will therefore differ from
live. The comparison between weight settings on one identical book is what this
measures, and that is valid. Final confirmation is a live deep-search after deploy.

- [ ] **Step 1: Write the harness**

Create `scripts/measure_earliness.py`:

```python
"""Measure the earliness penalty and the makespan weight (spec 2026-08-06 §6).

Runs the optimizer over a workbook at several weight settings and reports the three
numbers the ship criterion needs. Read-only: touches no store, no live site.

    python scripts/measure_earliness.py Test9.xlsx --budget 150

Ship criterion (spec §6): earliness_breach DOWN and late_days NOT UP versus stage 1.
"""
import argparse
import sys
from datetime import date

sys.path.insert(0, ".")

from engine import loaders, new_engine, optimizer
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward
from ppc_engine import config as ppc_config


def _apply_weights(earliness, makespan):
    """Set BOTH mirrors. Editing only one is the bug tests/test_earliness_mirror.py
    exists to catch."""
    optimizer.EARLINESS_WEIGHT = earliness
    optimizer.MAKESPAN_WEIGHT = makespan
    ppc_config.PlanConfig.earliness_weight = earliness
    ppc_config.PlanConfig.makespan_weight = makespan


def _measure(so_lines, masters, cfg, budget, label):
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=budget, seed=42)
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, cfg, masters, priority_rank=res.ranks)
    m = optimizer.plan_metrics(pr.schedule, so_lines, cfg.plan_start_date)
    print(f"  {label:34s} late={m['total_late_days']:5d}  "
          f"makespan={m['makespan_days']:6.2f}  "
          f"earliness_breach={m['earliness_breach']:7.1f}")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--budget", type=int, default=150)
    ap.add_argument("--start", default="2026-08-06")
    args = ap.parse_args()

    new_engine.set_masters_bytes(open(args.workbook, "rb").read())
    so_lines, masters = loaders.load_all(args.workbook)
    cfg = Config(plan_start_date=date.fromisoformat(args.start), scheduler="new",
                 overlap_percent=84, flexible_machines=True,
                 apply_operator_logic=True, consolidation_window_days=10)
    print(f"{len(so_lines)} SO lines, budget {args.budget}/run\n")

    print("STAGE 1 — baseline (earliness off, makespan as shipped)")
    _apply_weights(0.0, 40.0)
    base = _measure(so_lines, masters, cfg, args.budget, "earliness=0 makespan=40")

    print("\nSTAGE 2 — earliness sweep (makespan held at 40)")
    for w in (5.0, 20.0, 50.0):
        _apply_weights(w, 40.0)
        _measure(so_lines, masters, cfg, args.budget, f"earliness={w} makespan=40")

    print("\nSTAGE 3 — makespan sweep (earliness held at 0)")
    for mk in (0.1, 1.0, 10.0, 40.0):
        _apply_weights(0.0, mk)
        _measure(so_lines, masters, cfg, args.budget, f"earliness=0 makespan={mk}")

    print("\nSTAGE 4 — combine the best of each (edit the pair below, then re-run)")
    _apply_weights(20.0, 40.0)
    _measure(so_lines, masters, cfg, args.budget, "earliness=20 makespan=40")

    print(f"\nSHIP CRITERION vs stage 1 (late={base['total_late_days']}, "
          f"breach={base['earliness_breach']}): a candidate ships only if "
          f"breach is DOWN and late is NOT UP.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the harness runs**

```bash
python scripts/measure_earliness.py Test9.xlsx --budget 20
```

Expected: prints four stages of numbers without error. Budget 20 is a smoke test, far
too small to decide anything.

- [ ] **Step 3: Commit**

```bash
git add scripts/measure_earliness.py
git commit -m "chore: harness for the earliness and makespan weight measurement

Reports late-days, makespan and earliness_breach per weight setting so the ship
criterion in spec section 6 can be evaluated rather than guessed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Run the measurement and set the final weights

This task has no predetermined outcome. **It may end in shipping nothing**, which the
owner accepted explicitly (spec §6).

**Files:**
- Modify: `engine/optimizer.py` (`EARLINESS_WEIGHT`, possibly `MAKESPAN_WEIGHT`)
- Modify: `ppc_engine/config.py` (`earliness_weight`, possibly `makespan_weight`)
- Modify: `tests/test_earliness_mirror.py` (the pinned makespan values, if changed)

**Interfaces:**
- Consumes: `scripts/measure_earliness.py` from Task 5.
- Produces: final constant values; no new callable.

- [ ] **Step 1: Run the full measurement**

```bash
python scripts/measure_earliness.py Test9.xlsx --budget 400 2>&1 | tee /tmp/earliness-measurement.txt
```

This takes a while — roughly 400 evals per line, twelve lines. Record the output.

- [ ] **Step 2: Apply the ship criterion**

For each stage-2 candidate, check both conditions against stage 1:

- `earliness_breach` strictly DOWN, and
- `total_late_days` NOT UP.

Pick the candidate with the largest breach reduction that satisfies both. If none
does, `EARLINESS_WEIGHT` stays `0.0` and the feature ships inert — report that
plainly rather than relaxing the criterion.

- [ ] **Step 3: Report the stage-3 makespan numbers to the owner and stop**

Do **not** choose the makespan weight. Present the four rows (late-days and makespan
for 0.1 / 1 / 10 / 40) and let the owner decide, because it is a business trade
between a shorter schedule and fewer late deliveries (spec §6.1).

Named risk to state alongside the numbers: makespan is 15.8% of the score, so
dropping to 0.1 leaves severity at ~97% and may stretch the schedule badly. A large
makespan increase in that row is a signal to stay near 40.

- [ ] **Step 4: Set the constants**

Replace both placeholder weights with the measured values, keeping them equal:

- `engine/optimizer.py`: `EARLINESS_WEIGHT = <measured>`
- `ppc_engine/config.py`: `earliness_weight: float = <measured>`

Replace the `PLACEHOLDER until ...` comments with the measured evidence, in the house
style. Example shape, with the real numbers substituted:

```python
# MEASURED on Test9.xlsx (2026-08-06, budget 400): weight 20 cut earliness_breach
# 61 -> 12 with total_late_days unchanged at NNN; weight 50 cut breach further but
# added N late-days (rejected by the ship criterion); weight 5 barely moved it.
# Re-measure before moving.
```

If the makespan weight changed, update both files **and** the pinned values in
`tests/test_earliness_mirror.py::test_makespan_weights_are_pinned_and_known_to_diverge`,
renaming that test to reflect the new state.

- [ ] **Step 5: Run the full suite**

```bash
python -m pytest -q
```

Expected: all pass, including the mirror test with the new values.

- [ ] **Step 6: Confirm the golden trace is unaffected**

```bash
python -m pytest -k golden -v
```

Expected: pass without regeneration. The golden trace covers Rules 1-6 and does not
run the optimizer, so the score change should not reach it. **If it fails, stop and
investigate** — do not run `REGEN_GOLDEN=1`, because a changed golden here would mean
the term leaked into the planning path, which the spec forbids.

- [ ] **Step 7: Commit**

```bash
git add engine/optimizer.py ppc_engine/config.py tests/test_earliness_mirror.py
git commit -m "feat: set the measured earliness weight

<paste the measurement summary: baseline, chosen weight, breach and late-day deltas>

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Deployment note

Render auto-deploys every push to `main`, so **do not push until Task 6 is complete
and the owner has approved the measured weights**. Landing Tasks 1-5 on `main` would
put a placeholder weight of 20.0 into production scheduling.

Work on a branch and merge once the measurement is signed off.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3 grace = 4 days | 1, 2 (constants) |
| §3.1 linear not convex | 1, 2 (implementation + `test_linear_not_squared`) |
| §4 architecture, four files | 1, 2, 4 |
| §4.1 ppc `_earliness_breach` | 2 |
| §4.2 engine `plan_metrics` | 1 |
| §4.3 per SO-line measurement | 1 (`plan_metrics` is per SO-line by construction) |
| §5.1 weight ratios insufficient | 4 (rationale in the gate comment) |
| §5.2 gate on both apply paths | 4 |
| §6 four-stage measurement | 5, 6 |
| §6.1 makespan is a measurement | 5 (stage 3), 6 (step 3: owner decides) |
| §7 five test files | 1, 2, 3, 4 (inert case folded into 1 and 2) |
| §8 scope boundary | Global Constraints |
| §9 placeholder weight | 1, 2 land it; 6 replaces it |

No gaps.

**Placeholder scan:** The only `<measured>` / `<paste ...>` markers are in Task 6,
where the value is the deliverable of the task itself and the criterion for choosing
it is fully specified. No "TBD", no "add error handling", no "similar to Task N".

**Type consistency:** `earliness_breach` is the dict key in Tasks 1, 3, 4 and 5.
`_earliness_breach` is the ppc function name in Tasks 2 and 3.
`earliness_grace_days` / `earliness_weight` are the `PlanConfig` fields in Tasks 2, 3
and 5. `EARLINESS_GRACE_DAYS` / `EARLINESS_WEIGHT` are the module constants in Tasks
1, 3 and 5. `lateness_ok` appears only in Task 4. Checked and consistent.
