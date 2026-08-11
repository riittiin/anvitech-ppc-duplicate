# Committed-order date stability (+3-day promise cap) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A committed order's expected completion never slips more than +3 days past its promised date because of re-optimizing; open orders absorb the slack and wait. Remove the Urgent lane.

**Architecture:** Mirror the proven **worst-order ceiling** (2026-07-24) — a convex penalty in the objective (soft, in-search) + a hard no-regression backstop at apply. Unlike the scalar worst-order ceiling, the promise ceiling is **per committed order** (its own `promised_date + slack`), so it threads `promised_date` onto the ppc `Order`. Enforced authoritatively in old-space (`engine/optimizer.plan_metrics` + apply backstop) and mirrored in the ppc objective so the new-engine search actively protects committed orders.

**Tech Stack:** Python, the vendored `ppc_engine`, FastAPI, pytest. Prod runs the new engine (`DEFAULT_SCHEDULER=new`).

**Spec:** `docs/superpowers/specs/2026-07-29-committed-date-stability-design.md`

## Global Constraints

- **No committed order / all-open book must be byte-identical to today.** When nothing is committed (or no `promised_date`), the new metric terms are 0 and every plan/score is unchanged. Golden test + the existing suite stay green after every task.
- **This mirrors the worst-order ceiling — follow that code exactly.** Old-space: `engine/optimizer.py` `CEILING_WEIGHT` / `plan_metrics`'s `ceiling_breach` / `score`. ppc: `ppc_engine/objective/objective.py` `_ceiling_breach` / `score`, `ppc_engine/config.py` `ceiling_days`/`ceiling_weight`. Read those first for each task.
- **Anchor = `promised_date` (snapshotted at commit), never floats.** Ceiling = `promised_date + committed_promise_slack_days` (default 3). Open orders have no ceiling. Days are **calendar days** (`completion.date() − promised_date).days`), matching the existing lateness metric.
- **Soft-in-search + hard-at-apply.** NO hard in-decoder veto (that is the July collapse). The apply backstop is no-regression on the worst committed slip.
- **Prod = new engine.** Old-space classic path may carry the term but is not the shipping path.
- **Efficiency cost is a SHIP GATE.** Task 9 measures the late-days cost on Test8; if unacceptable, retune the weight or escalate before shipping.
- **Do NOT commit/push to `main` without the owner's explicit "push."** Work on a fresh branch off `main` (which now contains the freeze feature).

## Setup

Create a branch off `main`: `git checkout main && git checkout -b committed-date-stability`.

## File map

| Area | File | Change |
|---|---|---|
| Old-space metric+score | `engine/optimizer.py` | `plan_metrics`: `committed_promise_breach`,`max_committed_slip`; `score`: weighted term; `COMMITTED_PROMISE_WEIGHT` |
| Old config | `engine/config.py` | `committed_promise_slack_days=3` + validation; fold into `_inputs_signature` (api) |
| ppc metric | `ppc_engine/objective/metrics.py` | `PlanMetrics.promise_slip_by_order`; `compute_metrics` fills it from `Order.promise_date` |
| ppc objective | `ppc_engine/objective/objective.py` | `_committed_promise_breach` + score term |
| ppc config | `ppc_engine/config.py` | `committed_promise_slack_days`,`committed_promise_weight` |
| ppc order | `ppc_engine/domain/order.py` | `promise_date: date | None = None` |
| adapter | `engine/new_engine.py` | `_orders_from_batches` sets `promise_date`; `_plan_config` passes slack+weight |
| apply backstop | `api/main.py` | `_auto_apply_result`/`/optimize/apply`: `promise_ok`; thread `max_committed_slip`+slack through `_incumbent_metrics`/`_metrics_for_ranks`/`_start_optimize` |
| remove urgent | `api/main.py`,`engine/orderbook.py`,`engine/book_store.py` | delete `/orders/urgent`, urgent branch; migrate urgent→committed on load |
| remove urgent UI | `web/app.js`,`web/index.html` | drop urgent button/lane; two-lane |
| docs | `CLAUDE.md`,`RULES.md` | two lanes; committed = soft-protected; promise ceiling |

---

# PHASE 1 — Old-space metric + score (authoritative)

### Task 1: `plan_metrics` computes committed-promise breach + max slip; `score` penalizes it

**Files:**
- Modify: `engine/optimizer.py` (`plan_metrics` ~line 101-144; `score` ~line 77-84; constants ~line 56)
- Test: `tests/test_committed_promise_metric.py` (Create)

**Interfaces:**
- Consumes: `so_lines` (each `SOLine` has `.commitment` (`open`/`committed`), `.promised_date` (date|None) — `engine/models.py:58-59`), `expected[k]` completion date (already computed in `plan_metrics`).
- Produces: `plan_metrics(..., promise_slack_days: int | None = None)` returns extra keys `committed_promise_breach` (Σ squared days past promise+slack, committed only) and `max_committed_slip` (max committed `(expected − promised).days`, else 0). `score` adds `COMMITTED_PROMISE_WEIGHT * committed_promise_breach`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_committed_promise_metric.py
from datetime import date, datetime
from engine import optimizer
from engine.models import SOLine, ScheduleEntry

def _line(so, item, due, commitment="open", promised=None, qty=10):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=qty,
                  delivery_date=due, commitment=commitment, promised_date=promised)

def _entry(so, item, end):
    return ScheduleEntry(batch_id=so, item_code=item, process_seq=1, process_name="CNC",
                         machine="CNC1", qty=10, occupancy_min=60,
                         start=datetime(2026,7,29,8,0), end=end, so_refs=[so])

def test_committed_promise_breach_and_max_slip():
    ps = date(2026,7,29)
    # committed, promised 05-Aug, finishes 10-Aug -> slip 5 days; slack 3 -> over 2 -> breach 4
    lines = [_line("SO1","IT-A",date(2026,8,20),"committed",date(2026,8,5)),
             _line("SO2","IT-B",date(2026,8,20),"open")]                 # open -> ignored
    sched = [_entry("SO1","IT-A",datetime(2026,8,10,17,0)),
             _entry("SO2","IT-B",datetime(2026,8,30,17,0))]             # open late, irrelevant
    m = optimizer.plan_metrics(sched, lines, ps, promise_slack_days=3)
    assert m["max_committed_slip"] == 5           # (10-Aug − 5-Aug)
    assert m["committed_promise_breach"] == 4.0    # (5-3)^2

def test_committed_within_slack_is_zero():
    ps = date(2026,7,29)
    lines = [_line("SO1","IT-A",date(2026,8,20),"committed",date(2026,8,5))]
    sched = [_entry("SO1","IT-A",datetime(2026,8,7,17,0))]              # slip 2 <= slack 3
    m = optimizer.plan_metrics(sched, lines, ps, promise_slack_days=3)
    assert m["committed_promise_breach"] == 0.0
    assert m["max_committed_slip"] == 2

def test_no_committed_is_byte_identical():
    ps = date(2026,7,29)
    lines = [_line("SO1","IT-A",date(2026,8,20),"open")]
    sched = [_entry("SO1","IT-A",datetime(2026,8,30,17,0))]
    base = optimizer.plan_metrics(sched, lines, ps)                     # no promise_slack_days
    withp = optimizer.plan_metrics(sched, lines, ps, promise_slack_days=3)
    assert withp["committed_promise_breach"] == 0.0
    assert withp["max_committed_slip"] == 0
    assert optimizer.score(base) == optimizer.score(withp)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_committed_promise_metric.py -v`
Expected: FAIL — `plan_metrics()` has no `promise_slack_days` kwarg / missing keys.

- [ ] **Step 3: Implement — mirror `ceiling_breach` for committed promises**

Read `engine/optimizer.py` `plan_metrics` (the `expected`/`gaps`/`ceiling_breach` block) and `score` first. Add near the constants (~line 56):

```python
# Committed-promise ceiling (2026-07-29) — mirror of the worst-order ceiling, but
# per-order: each committed order's ceiling is ITS promised_date + slack. MEASURED
# on Test8 (see the committed-date-stability plan §Task 9); re-measure before moving.
COMMITTED_PROMISE_WEIGHT = 100.0
```

Change the signature to `plan_metrics(schedule, so_lines, plan_start, ceiling_days=None, with_distribution=False, promise_slack_days=None)`. Inside, after `expected` is built and alongside the `ceiling_breach` loop, add (using each line's commitment/promised_date):

```python
    committed_promise_breach = 0.0
    max_committed_slip = 0
    if promise_slack_days is not None:
        promised = {(l.so_no, l.item_code): l.promised_date for l in so_lines
                    if l.commitment == "committed" and l.promised_date is not None}
        for k, pdate in promised.items():
            if k in expected:
                slip = (expected[k] - pdate).days       # calendar days past promise
                if slip > max_committed_slip:
                    max_committed_slip = slip
                over = slip - promise_slack_days
                if over > 0:
                    committed_promise_breach += float(over * over)
```

Add to the `result` dict: `"committed_promise_breach": round(committed_promise_breach, 2), "max_committed_slip": int(max_committed_slip),`.

In `score`, add the term (with `.get` for legacy safety, exactly like `ceiling_breach`):
```python
            + COMMITTED_PROMISE_WEIGHT * metrics.get("committed_promise_breach", 0.0))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_committed_promise_metric.py -v`
Expected: PASS

- [ ] **Step 5: Regression**

Run: `python3 -m pytest tests/test_optimizer*.py -q && python3 -m pytest -k golden -q`
Expected: PASS (no committed/no slack → unchanged)

- [ ] **Step 6: Commit**

```bash
git add engine/optimizer.py tests/test_committed_promise_metric.py
git commit -m "feat(promise): committed-promise breach + max-slip metric; score penalizes it"
```

---

# PHASE 2 — Config knob (old Config)

### Task 2: `Config.committed_promise_slack_days` (default 3) + inputs signature

**Files:**
- Modify: `engine/config.py` (add field + validation — mirror `worst_ceiling_days` ~line 120-124/178-179; note `worst_ceiling_days` is EXCLUDED from `_inputs_signature`, but slack is a plan-shaping knob so it IS included)
- Modify: `api/main.py` (`_inputs_signature` ~line 323-361 — the field is in `config.to_dict()`, so confirm it's NOT popped; it should be hashed)
- Test: `tests/test_committed_promise_metric.py` (add a config test)

**Interfaces:**
- Produces: `Config.committed_promise_slack_days: int = 3`, validated `>= 0`.

- [ ] **Step 1: Write the failing test**

```python
def test_config_slack_default_and_validation():
    from engine.config import Config
    assert Config().committed_promise_slack_days == 3
    import pytest
    with pytest.raises(Exception):
        Config(committed_promise_slack_days=-1).validate()
```

- [ ] **Step 2: Run test → fails** (`AttributeError`). `python3 -m pytest tests/test_committed_promise_metric.py::test_config_slack_default_and_validation -v`

- [ ] **Step 3: Implement** — in `engine/config.py`, add `committed_promise_slack_days: int = 3` to the dataclass; in `validate()` add `if self.committed_promise_slack_days < 0: errs.append("committed_promise_slack_days must be >= 0")`. Confirm `to_dict`/`from_dict` round-trip it (dataclass asdict usually covers it — verify). In `api/main.py` `_inputs_signature`, it rides in `config.to_dict()` and is NOT popped, so it is hashed automatically — verify no code pops it.

- [ ] **Step 4: Run test → passes.** Regression: `python3 -m pytest tests/test_config*.py -q`.

- [ ] **Step 5: Commit**
```bash
git add engine/config.py tests/test_committed_promise_metric.py
git commit -m "feat(promise): committed_promise_slack_days config (default 3)"
```

---

# PHASE 3 — ppc objective mirror (so the SEARCH actively protects committed)

### Task 3: ppc `Order.promise_date` + `PlanMetrics.promise_slip_by_order`

**Files:**
- Modify: `ppc_engine/domain/order.py` (add field), `ppc_engine/objective/metrics.py` (`PlanMetrics` field + `compute_metrics`)
- Test: `tests/test_ppc_promise_metric.py` (Create)

**Interfaces:**
- Produces: `ppc_engine.domain.order.Order.promise_date: date | None = None` (excluded from equality/hash like `process_remaining`); `PlanMetrics.promise_slip_by_order: dict[key, float]` (signed days vs promise, only for orders with a `promise_date`); `compute_metrics` fills it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ppc_promise_metric.py
from datetime import date, datetime
from ppc_engine.domain.order import Order
from ppc_engine.objective.metrics import compute_metrics
from ppc_engine.scheduler.schedule import Schedule

def test_promise_slip_by_order():
    o = Order(so_no="SO1", item_code="IT-A", item_name="A", qty=10,
              due_date=date(2026,8,20), promise_date=date(2026,8,5))
    sched = Schedule(segments=tuple(),
                     completion={("SO1","IT-A"): datetime(2026,8,10,17,0)})
    m = compute_metrics(sched, [o], datetime(2026,7,29,8,0))
    assert m.promise_slip_by_order[("SO1","IT-A")] == 5   # 10-Aug - 5-Aug
    # order with no promise_date is absent from the map
    o2 = Order(so_no="SO2", item_code="IT-B", item_name="B", qty=10, due_date=date(2026,8,20))
    sched2 = Schedule(segments=tuple(), completion={("SO2","IT-B"): datetime(2026,8,30,17,0)})
    m2 = compute_metrics(sched2, [o2], datetime(2026,7,29,8,0))
    assert ("SO2","IT-B") not in m2.promise_slip_by_order
```

- [ ] **Step 2: Run → fails** (`Order` has no `promise_date`). `python3 -m pytest tests/test_ppc_promise_metric.py -v`

- [ ] **Step 3: Implement**
- `ppc_engine/domain/order.py`: add `promise_date: date | None = field(default=None, compare=False)` after `process_remaining` (import `date` if needed).
- `ppc_engine/objective/metrics.py`: add `promise_slip_by_order: dict[tuple[str, str], float]` to `PlanMetrics`. In `compute_metrics`, build `promise_by_key = {o.key: o.promise_date for o in orders if o.promise_date is not None}` and, in the completion loop, `if key in promise_by_key: promise_slip_by_order[key] = (completion.date() - promise_by_key[key]).days`. Add the field to the `PlanMetrics(...)` return. (Every existing `PlanMetrics(...)` construction must pass the new field — grep for `PlanMetrics(` and default it to `{}` where constructed elsewhere.)

- [ ] **Step 4: Run → passes.** Regression: `python3 -m pytest tests/test_new_engine.py -q`.

- [ ] **Step 5: Commit**
```bash
git add ppc_engine/domain/order.py ppc_engine/objective/metrics.py tests/test_ppc_promise_metric.py
git commit -m "feat(promise): ppc Order.promise_date + PlanMetrics.promise_slip_by_order"
```

---

### Task 4: ppc objective `_committed_promise_breach` + score term + PlanConfig knobs

**Files:**
- Modify: `ppc_engine/config.py` (add `committed_promise_slack_days`,`committed_promise_weight`), `ppc_engine/objective/objective.py` (`_committed_promise_breach` + score)
- Test: `tests/test_ppc_promise_metric.py`

**Interfaces:**
- Consumes: `PlanMetrics.promise_slip_by_order` (Task 3), `PlanConfig.committed_promise_slack_days`/`committed_promise_weight`.
- Produces: `objective.score(metrics, config)` adds `config.committed_promise_weight * _committed_promise_breach(metrics, config)`.

- [ ] **Step 1: Write the failing test**

```python
from ppc_engine.config import PlanConfig
from ppc_engine.objective.objective import score
from ppc_engine.objective.metrics import PlanMetrics

def _pm(promise_slip):
    return PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0, late_order_count=0,
                       makespan_days=0.0, lateness_by_order={}, promise_slip_by_order=promise_slip)

def test_committed_promise_term_in_score():
    cfg = PlanConfig(plan_start=None) if False else PlanConfig.__dataclass_fields__ and None
    # build a real minimal PlanConfig (fill required fields per ppc_engine/config.py)
    from tests.new_sample_workbook import build_new_sample_bytes  # only to import config defaults path
    # Simpler: construct PlanConfig with its required args; slack=3, weight=100.
    cfg = _make_plan_config(slack=3, weight=100.0)   # helper: see below
    within = score(_pm({("A","x"): 2.0}), cfg)   # slip 2 <= slack 3 -> no breach
    over    = score(_pm({("A","x"): 6.0}), cfg)   # slip 6, over 3 -> breach 9 -> +900
    assert over > within
    assert abs((over - within) - 100.0 * 9.0) < 1e-6
```

(Provide a `_make_plan_config` helper in the test that constructs a valid `PlanConfig` — read `ppc_engine/config.py` for the required positional fields, e.g. `plan_start`, `week_anchor`, shift times — and sets `committed_promise_slack_days=3`, `committed_promise_weight=100.0`. Mirror how `tests/test_new_engine.py` or `engine/new_engine._plan_config` builds one.)

- [ ] **Step 2: Run → fails** (no such config fields / score term). `python3 -m pytest tests/test_ppc_promise_metric.py::test_committed_promise_term_in_score -v`

- [ ] **Step 3: Implement** — mirror `_ceiling_breach`/`ceiling_weight`:
- `ppc_engine/config.py`: add `committed_promise_slack_days: float = 3.0` and `committed_promise_weight: float = 100.0` (near `ceiling_days`/`ceiling_weight`).
- `ppc_engine/objective/objective.py`:
```python
def _committed_promise_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Sum of squared committed-order lateness beyond (promised_date + slack). 0 when no
    committed order breaches its promise. Per-order mirror of _ceiling_breach."""
    slack = config.committed_promise_slack_days
    total = 0.0
    for slip in metrics.promise_slip_by_order.values():
        over = slip - slack
        if over > 0:
            total += over * over
    return total
```
Add to `score`: `+ config.committed_promise_weight * _committed_promise_breach(metrics, config)`.

- [ ] **Step 4: Run → passes.** Regression: `python3 -m pytest tests/test_new_engine.py -q && python3 -m pytest -k golden -q`.

- [ ] **Step 5: Commit**
```bash
git add ppc_engine/config.py ppc_engine/objective/objective.py tests/test_ppc_promise_metric.py
git commit -m "feat(promise): ppc objective committed-promise breach term"
```

---

### Task 5: adapter — `_orders_from_batches` sets `promise_date`; `_plan_config` passes slack+weight

**Files:**
- Modify: `engine/new_engine.py` (`_orders_from_batches` ~line 206-249; `_plan_config` ~line 163-183)
- Test: `tests/test_promise_adapter.py` (Create)

**Interfaces:**
- Consumes: each old `Batch`'s source SO-lines carry `.commitment`/`.promised_date` — the batch must expose the tightest committed promise. **Read `engine/rules/rule1_consolidate.py` + `engine/models.py` `Batch` to find how per-SO-line commitment/promised_date reach the batch** (via `source_so_refs` or a batch field). Produce a helper `_batch_promise_date(batch, so_lines_by_key) -> date | None` = the **earliest** `promised_date` among the batch's committed members (None if none committed).
- Produces: ppc `Order.promise_date` set from that; `_plan_config` sets `committed_promise_slack_days`/`committed_promise_weight` from the old Config (slack) + `optimizer.COMMITTED_PROMISE_WEIGHT` (weight).

- [ ] **Step 1: Write the failing test** — build a book via the sample workbook where one SO-line is committed with a promised_date (set it via `book_store.set_commitment` or directly on the SOLine), run `_orders_from_batches`, and assert the resulting ppc `Order.promise_date` equals that promised_date; an all-open batch → `promise_date is None`. (Use `so_lines, masters = loaders.load_all(io.BytesIO(wb))` — load_all returns a 2-TUPLE.)

- [ ] **Step 2: Run → fails** (promise_date always None).

- [ ] **Step 3: Implement** — in `_orders_from_batches`, when constructing each `Order(...)`, add `promise_date=_batch_promise_date(b, ...)`. In `_plan_config`, add `committed_promise_slack_days=float(getattr(config, "committed_promise_slack_days", 3))` and `committed_promise_weight=<the old-space COMMITTED_PROMISE_WEIGHT>` to the `PlanConfig(...)`. Import the weight from `engine.optimizer` (single source) or hardcode-with-comment mirroring `ceiling_weight`.

- [ ] **Step 4: Run → passes.** Regression: `python3 -m pytest tests/test_new_engine.py tests/test_promise_adapter.py -q && python3 -m pytest -k golden -q`.

- [ ] **Step 5: Commit**
```bash
git add engine/new_engine.py tests/test_promise_adapter.py
git commit -m "feat(promise): adapter threads committed promise into the ppc search"
```

---

# PHASE 4 — Apply backstop (hard no-regression)

### Task 6: `promise_ok` backstop at apply + thread `max_committed_slip`/slack

**Files:**
- Modify: `api/main.py` — `_auto_apply_result` (the `worst_ok` gate ~line 1595-1616), `/optimize/apply`, and thread the slack into every `plan_metrics(...)` call and `max_committed_slip` through `_incumbent_metrics`/`_metrics_for_ranks`/`_start_optimize`/`_all_lines_schedule` scoring.
- Test: `tests/test_promise_backstop.py` (Create)

**Interfaces:**
- Consumes: `plan_metrics(..., promise_slack_days=config.committed_promise_slack_days)` now returns `max_committed_slip`; `optimizer.score` includes the promise term.
- Produces: an applied plan must satisfy `best["max_committed_slip"] <= inc["max_committed_slip"]` (in addition to `worst_ok` and score-better).

- [ ] **Step 1: Write the failing test** — mirror `tests/` worst-order backstop test: build an incumbent with a committed order at slip S; a candidate that scores better overall but raises that committed order's slip to S+2; assert `_auto_apply_result` does NOT apply it (the plan is kept). (Use the FastAPI TestClient new-engine fixture pattern from `tests/test_freeze_api.py` — isolated store, `DEFAULT_SCHEDULER=new` via monkeypatch, small `_OPT_BUDGETS`.)

- [ ] **Step 2: Run → fails** (candidate applied — no promise gate).

- [ ] **Step 3: Implement**
- Everywhere `plan_metrics(...)` is called for scoring in `api/main.py` (grep `plan_metrics(`), pass `promise_slack_days=<resolved config>.committed_promise_slack_days`. Same in `engine/new_engine.py` `optimize_sequence`/`tune`'s `plan_metrics` calls (the winner scoring) — pass the slack from config.
- In `_auto_apply_result`, add: `promise_ok = best.get("max_committed_slip", 0) <= inc.get("max_committed_slip", 0)` and require it in the apply condition: `if optimizer.score(best) < optimizer.score(inc) and worst_ok and promise_ok:`. Write the "kept current plan to protect a committed promise" auto-note when `promise_ok` fails.
- Ensure `_incumbent_metrics` and `_metrics_for_ranks` compute `max_committed_slip` (they call `plan_metrics`/`optimizer.plan_metrics` — pass the slack).

- [ ] **Step 4: Run → passes.** Regression: `python3 -m pytest tests/test_auto_optimize.py tests/test_promise_backstop.py -q`.

- [ ] **Step 5: Commit**
```bash
git add api/main.py engine/new_engine.py tests/test_promise_backstop.py
git commit -m "feat(promise): apply backstop — never worsen a committed order's promise slip"
```

---

# PHASE 5 — Remove Urgent

### Task 7: backend — delete `/orders/urgent`, urgent branch, migrate urgent→committed

**Files:**
- Modify: `api/main.py` (delete `POST /orders/urgent` ~line 1819-1832 + the urgent request model), `engine/orderbook.py` (`split_committed_open` ~line 309-312 — drop "urgent"), `engine/book_store.py` / `engine/orderbook.py` load path (migrate `commitment=="urgent"` → `"committed"`), `engine/models.py` (comment `commitment` = `open | committed`)
- Test: `tests/test_orderbook_commitment.py` (update), `tests/test_promise_backstop.py` or a new `tests/test_no_urgent.py`

**Interfaces:**
- Produces: `commitment ∈ {open, committed}`; any stored `"urgent"` normalizes to `"committed"` on load (`Order.from_json` or the book load path); `POST /orders/urgent` removed (404/405).

- [ ] **Step 1: Write the failing test** — a stored order with `commitment="urgent"` loads as `"committed"` (via `Order.from_json({..., "commitment":"urgent"})` → `.commitment == "committed"`); `client.post("/orders/urgent", ...)` returns 404/405.

- [ ] **Step 2: Run → fails.**

- [ ] **Step 3: Implement** — `Order.from_json` (`engine/models.py` ~line 482): map `d.get("commitment","open")` through `"urgent" → "committed"`. Same for `SOLine` if it deserializes. `orderbook.split_committed_open`: `protected = [l for l in so_lines if l.commitment == "committed"]` (drop urgent) — or delete the function if now unused (it was already "kept but unused"). Delete the `/orders/urgent` endpoint + its Pydantic model + `set_commitment(..., "urgent")` call site. Grep `"urgent"` across `api/`, `engine/` and remove/normalize each.

- [ ] **Step 4: Run → passes.** Full suite: `python3 -m pytest -q`.

- [ ] **Step 5: Commit**
```bash
git add api/main.py engine/orderbook.py engine/models.py tests/
git commit -m "feat(promise): remove the Urgent lane; migrate existing urgent -> committed"
```

---

### Task 8: frontend — remove urgent button/lane, two-lane Orders tab

**Files:**
- Modify: `web/app.js` (urgent button ~line 916, 1105-1139; lane legend ~line 922), `web/index.html`
- Verify: browser (manual)

- [ ] **Step 1: Implement** — remove the `ord-urgent-sel` button, its handler (`web/app.js` ~1105-1139), and the `/orders/urgent` fetch. Update the lane legend text (~line 922) to two lanes and to say **committed = soft-protected (dates held within +3 days), open = newly arrived**. Remove any urgent badge rendering. `node --check web/app.js`.

- [ ] **Step 2: Browser verify** — run the app (isolated store, `DEFAULT_SCHEDULER=new`, `_OPT_BUDGETS` small — see the freeze plan's `run_app.py` pattern), upload Test8, open the Orders tab, confirm only Committed + Open, commit an order, confirm no Urgent control anywhere.

- [ ] **Step 3: Commit**
```bash
git add web/app.js web/index.html
git commit -m "feat(promise): Orders tab shows Committed + Open only (Urgent removed)"
```

---

# PHASE 6 — Measurement gate

### Task 9: tune `COMMITTED_PROMISE_WEIGHT` on Test8 + measure the efficiency cost (SHIP GATE)

**Files:** none (measurement); may adjust the two weight constants.

- [ ] **Step 1: Real-data script** (scratchpad, like `freeze_test2.py`): isolated store, `DEFAULT_SCHEDULER=new`, upload Test8. Apply a plan; **commit a representative set of orders** (snapshot their promised dates). Add a batch of new **open** orders (or re-upload a superset). Re-optimize.
- [ ] **Step 2: Measure** — for weights ∈ {50, 100, 200, 400}: (a) the **max committed promise slip** and how many committed orders exceed +3 (target: 0 where feasible); (b) the **late-days delta** vs an unconstrained (weight 0) run — the efficiency cost. Record a small table.
- [ ] **Step 3: Lock** the weight that holds committed within +3 at the smallest late-days cost; set `COMMITTED_PROMISE_WEIGHT` (old) and `committed_promise_weight` (ppc) to it, with a measured-on-Test8 comment (mirror `CEILING_WEIGHT`'s comment).
- [ ] **Step 4: GATE** — if no weight holds committed within +3 without an unacceptable late-days blowup, STOP and report to the owner with the numbers (do not ship silently). Otherwise record the accepted cost.
- [ ] **Step 5: Commit** the locked weights.
```bash
git add engine/optimizer.py ppc_engine/config.py
git commit -m "chore(promise): lock committed-promise weight (Test8 measurement)"
```

---

# PHASE 7 — Docs

### Task 10: `RULES.md` + `CLAUDE.md`

- [ ] **Step 1: RULES.md** — add the committed-promise rule (committed orders capped at promised+3 later; open unbounded/waits; soft convex penalty + hard apply backstop; physical slip flagged+prioritized). Update the lanes section to **two lanes**.
- [ ] **Step 2: CLAUDE.md** — update the "lanes are status labels, no scheduling effect" statements: **committed now has a soft scheduling effect** (promise ceiling + apply backstop); **open remains a pure label**; Urgent removed. Add the new config field, the two weight constants, and the ppc `Order.promise_date` / `promise_slip_by_order`.
- [ ] **Step 3: Commit**
```bash
git add RULES.md CLAUDE.md
git commit -m "docs(promise): committed-promise cap rule + two-lane model"
```

---

## Self-review notes (author)

- **Spec coverage:** §4a metric → Task 1,3; §4b objective → Task 1,4; §4c apply backstop → Task 6; §4d physical slip flag → surfaced via `max_committed_slip` + the existing Orders-tab drift flag (UI red flag is Task 8's legend + the existing Promised-vs-Current display — confirm the red flag shows when slip>+3); §5 remove urgent → Task 7,8; §6 lifecycle/reversal → Task 10 docs; §8 config → Task 2,4; §9 testing → each task; §10 map → all tasks; §11 open items → Task 9 (weight), Task 1/3 (calendar-day basis, resolved), Task 6 (scalar backstop chosen).
- **Byte-identical guards** in Tasks 1, 3, 4, 5 (no committed/None → 0 term).
- **Type consistency:** `max_committed_slip` (int days), `committed_promise_breach` (float, squared), `promise_slip_by_order` (dict key→float days), `promise_date` (date|None), `committed_promise_slack_days` (int old / float ppc — intentional, both compared to day counts). Verify `PlanMetrics(` constructions all pass the new field (Task 3 grep).
- **Open risk:** the batch→promise mapping (Task 5) when a consolidated batch mixes committed + open members — the plan takes the earliest committed promise (tightest); the old-space winner re-score + apply backstop are per-SO-line authoritative, so a slightly conservative ppc search is safe. Flag for review during Task 5.
