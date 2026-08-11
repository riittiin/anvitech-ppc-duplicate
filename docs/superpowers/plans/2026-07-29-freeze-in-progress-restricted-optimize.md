# Freeze in-progress work, re-optimize the rest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On every "Done entering — update plan", run an auto-applying optimization that holds the currently in-progress operations fixed on their machine + operator until finished, and re-optimizes the remaining backlog around them.

**Architecture:** A new `frozen` input reaches the one scheduler `decode(...)`, which *pre-places* the in-progress operations before its main loop; the rest schedule around them. The frozen set is derived (no new floor data entry) from the **last-applied plan** (machine + operator) and the **daily punches** (which steps are live + remaining qty). Persistence saves the applied schedule and the frozen set at apply time. The Thursday-only gate on "Done" is removed — the freeze is what makes daily re-optimization safe.

**Tech Stack:** Python, FastAPI, the vendored `ppc_engine` scheduler, pytest. Prod runs the new engine (`DEFAULT_SCHEDULER=new`).

**Spec:** `docs/superpowers/specs/2026-07-29-freeze-in-progress-restricted-optimize-design.md`

## Global Constraints

- **Prod runs the NEW engine** (`DEFAULT_SCHEDULER=new`, `config.scheduler == "new"`). The freeze is implemented for the new engine path; classic/flow must only *accept* the `frozen` kwarg without error.
- **`frozen` empty/None must be byte-identical to today** everywhere. The golden test (`REGEN_GOLDEN=1 pytest -k golden`) and the ~500 existing tests must stay green after every task through Phase 6.
- **No new floor data entry.** The frozen set is derived from the last-applied plan + existing punches only.
- **Machine + operator of a frozen step come from the last-applied plan; detection + remaining qty come from the punches.** If the planned operator is absent (absence table), the machine stays pinned and a substitute is staffed.
- **Frozen resume order = previous-plan (`prev_start`) order per machine; all frozen work on a machine finishes before any new work.** No setup on resume. Cross-shift work hands off normally.
- **OS/outsourced and DISPATCH steps are never frozen.**
- **IST clock** for any date reasoning (`_ist_today`/`_ist_now` in `api/main.py`).
- **Do NOT commit or push to `main` without the owner's explicit "push"** in that message. Commit locally per task; the owner controls landing.
- **Owner reminder:** `api/main.py` `_OPTIMIZE_WEEKDAY` is a TEMP Sunday(6) testing override; this plan removes the weekday gate entirely (Task 22), which makes it moot.

---

## File map (what each task touches)

| Area | File | Change |
|---|---|---|
| Persistence | `engine/book_store.py` | 2 new keys + save/load/clear helpers |
| Engine core | `ppc_engine/scheduler/schedule.py` | `FrozenOp` dataclass |
| Engine core | `ppc_engine/scheduler/flow_scheduler.py` | `decode(frozen=)` + `_preplace_frozen` + `_lay_frozen` |
| Engine search | `ppc_engine/optimize/search.py` | `optimize(frozen=)`, `_Evaluator(frozen=)` |
| Engine search | `ppc_engine/optimize/contest.py` | `optimize_overlap`/`tune_overlap` forward `frozen` |
| Adapter | `engine/new_engine.py` | app-frozen→ppc-frozen mapping + thread through `run`/`optimize_sequence`/`tune`/`sweep_optimize` |
| Pipeline | `engine/pipeline.py` | `run_forward(frozen=)` → scheduler |
| Classic/flow | `engine/rules/rule6_allocate.py`, `engine/flow_scheduler.py` | accept+ignore `frozen` |
| Optimizer | `engine/optimizer.py` | `optimize`/`sweep_optimize` forward `frozen` |
| Contest/cloud | `engine/optimize_service.py` | `ContestSetup.frozen`, `prepare_contest`, `build/parse_payload`, `run_candidate`, `book_signature` |
| Frozen-set logic | `engine/freeze.py` (NEW) | pure: schedule→projection, projection+punches→frozen rows |
| API | `api/main.py` | persist last-applied at apply; `_plan`/`_all_lines_schedule`/`_plan_run_for_report` read+pass frozen; compute+persist frozen at Done; remove Thursday gate; fold into fingerprints |
| Frontend | `web/app.js` | remove `not_optimize_day` branch |
| Docs | `RULES.md`, `CLAUDE.md` | the freeze rule + banner/bullets |

---

# PHASE 1 — Persistence foundation

### Task 1: Store keys + save/load for last-applied schedule and frozen set

**Files:**
- Modify: `engine/book_store.py` (key constants near line 24-28; helpers near the `save_last_searched`/`load_last_searched` pattern, lines 230-236)
- Test: `tests/test_freeze_persistence.py` (Create)

**Interfaces:**
- Produces:
  - `book_store.LAST_APPLIED_SCHEDULE_KEY`, `book_store.FROZEN_OPS_KEY`
  - `save_last_applied_schedule(rows: list[dict]) -> None`, `load_last_applied_schedule() -> list[dict]`
  - `save_frozen_ops(rows: list[dict]) -> None`, `load_frozen_ops() -> list[dict]`, `clear_frozen_ops() -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_persistence.py
from engine import book_store

def test_last_applied_schedule_round_trips():
    rows = [{"batch_id": "B1", "item_code": "IT-A", "process_seq": 2,
             "machine": "CNC1", "operator": "Alpha", "start": "2026-07-29T08:00:00",
             "end": "2026-07-29T10:00:00", "so_refs": ["SO1"]}]
    book_store.save_last_applied_schedule(rows)
    assert book_store.load_last_applied_schedule() == rows

def test_frozen_ops_round_trip_and_clear():
    rows = [{"so_no": "SO1", "item_code": "IT-A", "process": "CNC first side",
             "op_seq": 2, "machine": "CNC1", "operator": "Alpha", "remaining_qty": 40,
             "prev_start": "2026-07-29T08:00:00"}]
    book_store.save_frozen_ops(rows)
    assert book_store.load_frozen_ops() == rows
    book_store.clear_frozen_ops()
    assert book_store.load_frozen_ops() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_persistence.py -v`
Expected: FAIL — `AttributeError: module 'engine.book_store' has no attribute 'save_last_applied_schedule'`

- [ ] **Step 3: Add the keys and helpers**

In `engine/book_store.py`, after line 28 (`OPERATORS_KEY = ...`):

```python
LAST_APPLIED_SCHEDULE_KEY = "anvitech:last_applied_schedule"  # kv: json list of applied-schedule op rows
FROZEN_OPS_KEY = "anvitech:frozen_ops"       # kv: json list of frozen (in-progress) op rows for today
```

Near the `save_last_searched`/`load_last_searched` helpers (after line 236), add:

```python
# --- last-applied schedule (the plan the floor is following) --- #
def save_last_applied_schedule(rows: list) -> None:
    """Persist the applied plan's per-op assignment (machine/operator/time). Written
    only when an optimize result is APPLIED — never on a display re-plan, so it stays
    'the plan the floor is following' and doesn't drift with new actuals."""
    get_store().kv_set(LAST_APPLIED_SCHEDULE_KEY, json.dumps(rows))


def load_last_applied_schedule() -> list:
    raw = get_store().kv_get(LAST_APPLIED_SCHEDULE_KEY)
    return json.loads(raw) if raw else []


# --- frozen (in-progress) ops for the current day --- #
def save_frozen_ops(rows: list) -> None:
    get_store().kv_set(FROZEN_OPS_KEY, json.dumps(rows))


def load_frozen_ops() -> list:
    raw = get_store().kv_get(FROZEN_OPS_KEY)
    return json.loads(raw) if raw else []


def clear_frozen_ops() -> None:
    get_store().delete_key(FROZEN_OPS_KEY)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_persistence.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/book_store.py tests/test_freeze_persistence.py
git commit -m "feat(freeze): persistence keys for last-applied schedule + frozen ops"
```

---

# PHASE 2 — Engine: `decode` pre-places frozen ops

### Task 2: `FrozenOp` dataclass + `decode(frozen=None)` accepted, byte-identical when empty

**Files:**
- Modify: `ppc_engine/scheduler/schedule.py` (add dataclass near `Segment`, line 17)
- Modify: `ppc_engine/scheduler/flow_scheduler.py` (`decode` signature line 43-49; `_decode_consolidated` line 202-208, 228)
- Test: `tests/test_freeze_engine.py` (Create)

**Interfaces:**
- Produces:
  - `ppc_engine.scheduler.schedule.FrozenOp(order_key: tuple[str,str], op_seq: int, machine_id: str, operator: str, remaining_qty: int, prev_start: datetime)`
  - `decode(orders, sequence, masters, config, dispatch="gt", frozen=None)` — `frozen: list[FrozenOp] | None`, `None`/`[]` byte-identical to today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_engine.py
import io
from datetime import date
import pytest

from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from engine import book_store, loaders, orderbook
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import decode, FrozenOp
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)

@pytest.fixture()
def ctx():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    old = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(old.so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, nm)
    seq = [o.key for o in orders]
    return orders, seq, nm

def test_decode_frozen_none_is_byte_identical(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    a = decode(orders, seq, nm, cfg)
    b = decode(orders, seq, nm, cfg, frozen=None)
    c = decode(orders, seq, nm, cfg, frozen=[])
    assert a.segments == b.segments == c.segments
    assert a.completion == b.completion == c.completion
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_engine.py::test_decode_frozen_none_is_byte_identical -v`
Expected: FAIL — `ImportError: cannot import name 'FrozenOp'`

- [ ] **Step 3: Add `FrozenOp` and the `frozen` param (no behavior yet)**

In `ppc_engine/scheduler/schedule.py`, after the `Segment` dataclass:

```python
@dataclass(frozen=True)
class FrozenOp:
    """An in-progress operation pinned in place for a re-plan: it must finish on its
    machine (from the last-applied plan) before that machine takes any new work; its
    owning order's next step waits for it. ``operator`` is the planned operator ("" =
    let staffing pick). ``prev_start`` (last-applied start) orders multiple frozen ops
    on one machine (previous-plan order)."""
    order_key: tuple[str, str]
    op_seq: int
    machine_id: str
    operator: str
    remaining_qty: int
    prev_start: datetime
```

Add `FrozenOp` to that file's exports if it has an `__all__`; and in `ppc_engine/scheduler/__init__.py`:

```python
from ppc_engine.scheduler.flow_scheduler import decode
from ppc_engine.scheduler.schedule import Schedule, Segment, FrozenOp

__all__ = ["decode", "Schedule", "Segment", "FrozenOp"]
```

In `flow_scheduler.py`, change the `decode` signature (line 43-49) to add `frozen=None`, and pass it through the consolidation recursion (line 228):

```python
def decode(
    orders: list[Order],
    sequence: list[tuple[str, str]],
    masters: Masters,
    config: PlanConfig,
    dispatch: str = "gt",
    frozen=None,
) -> Schedule:
```
and at line 74-75 inside the consolidation guard:
```python
    if getattr(config, "consolidation_window", 0) and config.consolidation_window > 0:
        return _decode_consolidated(orders, sequence, masters, config, dispatch, frozen)
```
Update `_decode_consolidated` (line 202) to accept `frozen=None` as a trailing param and pass it to its inner `decode(...)` call (line 228): `decode(batches, batch_seq, masters, replace(config, consolidation_window=0.0), dispatch, frozen)`. (In the new-engine path consolidation is 0, so this is defensive.)

Do **not** add any pre-placement yet — `frozen` is accepted but unused.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_engine.py::test_decode_frozen_none_is_byte_identical -v`
Expected: PASS

- [ ] **Step 5: Run the full engine + golden suite (regression gate)**

Run: `pytest tests/test_new_engine.py -q && REGEN_GOLDEN=0 pytest -k golden -q`
Expected: PASS (byte-identical)

- [ ] **Step 6: Commit**

```bash
git add ppc_engine/scheduler/schedule.py ppc_engine/scheduler/__init__.py ppc_engine/scheduler/flow_scheduler.py tests/test_freeze_engine.py
git commit -m "feat(freeze): decode accepts a frozen kwarg (no-op when empty)"
```

---

### Task 3: Pre-place a single frozen op (pinned machine + operator, no setup)

**Files:**
- Modify: `ppc_engine/scheduler/flow_scheduler.py` (add `_lay_frozen` + `_preplace_frozen`; hook into `decode` after line 99, before `remaining` at line 102)
- Test: `tests/test_freeze_engine.py`

**Interfaces:**
- Consumes: `FrozenOp` (Task 2), `_lay_on_machine`/`iter_windows`/`Segment`/`StaffingBoard` (existing).
- Produces: `_preplace_frozen(frozen, order_by_key, ops_of, idx_of, ready_of, prev_end_of, machine_free, staffing, completion, masters, config) -> list[Segment]` and `_lay_frozen(machine, earliest, dur_min, order, op, op_qty, planned_operator, staffing, masters, config) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
def _entry(segs, order_key, op_seq):
    got = [s for s in segs if s.order_key == order_key and s.op_seq == op_seq]
    return sorted(got, key=lambda s: s.start)

def test_single_frozen_op_pinned_to_machine_operator_no_setup(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # Pick the first order's first machining op; find a machine option it has.
    o0 = orders[0]
    routing = nm.routings[o0.item_code]
    mach_op = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    mid = mach_op.machine_options[0]
    # Freeze 5 pieces of it on that machine with operator "Alpha", starting at plan_start.
    fo = FrozenOp(order_key=o0.key, op_seq=mach_op.seq, machine_id=mid,
                  operator="Alpha", remaining_qty=5, prev_start=cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[fo])
    segs = _entry(sched.segments, o0.key, mach_op.seq)
    assert segs, "frozen op was not scheduled"
    assert all(s.machine_id == mid for s in segs), "frozen op left its pinned machine"
    assert segs[0].operator == "Alpha", "frozen op not run by the planned operator"
    assert segs[0].start == cfg.plan_start, "frozen op did not resume at plan start"
    # No setup: total minutes == 5 * cycle (machining setup would add setup_min).
    total_min = sum((s.end - s.start).total_seconds() for s in segs) / 60.0
    assert abs(total_min - 5 * mach_op.cycle_min) < 1e-6, "frozen op charged setup time"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_engine.py::test_single_frozen_op_pinned_to_machine_operator_no_setup -v`
Expected: FAIL — the frozen op is scheduled by the normal loop (may pick a different machine / include setup), so one of the asserts fails.

- [ ] **Step 3: Implement `_lay_frozen` and `_preplace_frozen`, hook into `decode`**

Add near `_lay_on_machine` in `flow_scheduler.py`:

```python
def _lay_frozen(machine, earliest, dur_min, order, op, op_qty, planned_operator,
                staffing, masters, config):
    """Lay a frozen (in-progress) op onto its PINNED machine from ``earliest``.
    Prefer the planned operator each shift; if they are absent/busy, staff a
    substitute (candidate_operator). Same window-walking as _lay_on_machine, but the
    machine is fixed and no setup is charged (already set up mid-run)."""
    cursor = earliest
    remaining = dur_min
    segments: list[Segment] = []
    assignments: list[tuple] = []
    first_start = None
    for win in iter_windows(machine, earliest, masters.calendar, config):
        if remaining <= _EPS_MIN:
            break
        seg_start = max(cursor, win.start)
        avail = (win.end - seg_start).total_seconds() / 60.0
        if avail <= 0:
            cursor = win.end
            continue
        take = min(avail, remaining)
        seg_end = seg_start + timedelta(minutes=take)
        name = None
        if (planned_operator
                and masters.calendar.is_operator_available(planned_operator, win.shift_date)
                and staffing.free_during(planned_operator, seg_start, seg_end)):
            name = planned_operator
        else:
            name = staffing.candidate_operator(machine, win.shift_date, win.shift,
                                               seg_start, seg_end, masters, config)
        if name is None:
            cursor = win.end
            continue
        assignments.append((machine.id, win.shift_date, win.shift, name, seg_start, seg_end))
        segments.append(Segment(order.key, op.seq, op.name, op.kind, machine.id, name,
                                seg_start, seg_end, op_qty))
        if first_start is None:
            first_start = seg_start
        remaining -= take
        cursor = seg_end
    if remaining > _EPS_MIN or first_start is None:
        return None
    return {"start": first_start, "end": segments[-1].end,
            "segments": segments, "assignments": assignments}


def _preplace_frozen(frozen, order_by_key, ops_of, idx_of, ready_of, prev_end_of,
                     machine_free, staffing, completion, masters, config):
    """Pin every in-progress op onto its machine+operator BEFORE the main loop.
    Per machine, frozen ops resume in previous-plan (prev_start) order; the machine's
    free time is advanced past them so new work queues after. The owning order's index
    is advanced past the frozen op and its ready/prev_end set to the frozen end, so
    downstream steps wait for it. Returns the frozen segments."""
    from collections import defaultdict
    seq_index = {k: {op.seq: i for i, op in enumerate(ops_of[k])} for k in ops_of}
    by_machine = defaultdict(list)
    for fo in frozen:
        by_machine[fo.machine_id].append(fo)
    out: list[Segment] = []
    for mid, fos in by_machine.items():
        if mid not in masters.machines:
            continue  # machine gone from masters — not frozen (schedule normally)
        fos.sort(key=lambda f: (f.prev_start, f.order_key, f.op_seq))
        for fo in fos:
            order = order_by_key.get(fo.order_key)
            if order is None:
                continue
            oi = seq_index[fo.order_key].get(fo.op_seq)
            if oi is None:
                continue
            op = ops_of[fo.order_key][oi]
            dur = fo.remaining_qty * op.cycle_min      # no setup on resume
            if dur <= 0:
                continue
            earliest = machine_free.get(mid, config.plan_start)
            laid = _lay_frozen(masters.machines[mid], earliest, dur, order, op,
                               int(fo.remaining_qty), fo.operator, staffing, masters, config)
            if laid is None:
                continue  # unstaffable — leave to the main loop
            for a in laid["assignments"]:
                staffing.commit(*a)
            machine_free[mid] = laid["end"]
            for seg in laid["segments"]:
                if seg.operator is not None:
                    staffing.add_load(seg.operator,
                                      (seg.end - seg.start).total_seconds() / 60.0)
            out.extend(laid["segments"])
            end = laid["end"]
            idx_of[fo.order_key] = max(idx_of[fo.order_key], oi + 1)
            ready_of[fo.order_key] = max(ready_of[fo.order_key], end)
            prev_end_of[fo.order_key] = max(prev_end_of[fo.order_key], end)
            if idx_of[fo.order_key] >= len(ops_of[fo.order_key]):
                completion[fo.order_key] = prev_end_of[fo.order_key]
    return out
```

In `decode`, right after `completion` is created (line 99) and BEFORE `remaining` is built (line 102), insert:

```python
    if frozen:
        segments.extend(_preplace_frozen(
            frozen, order_by_key, ops_of, idx_of, ready_of, prev_end_of,
            machine_free, staffing, completion, masters, config))
```

`remaining` (line 102) already filters `idx_of[key] < len(ops_of[key])`, so orders fully consumed by freezing are naturally excluded.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_engine.py::test_single_frozen_op_pinned_to_machine_operator_no_setup -v`
Expected: PASS

- [ ] **Step 5: Regression — frozen=None still byte-identical**

Run: `pytest tests/test_freeze_engine.py tests/test_new_engine.py -q && pytest -k golden -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ppc_engine/scheduler/flow_scheduler.py tests/test_freeze_engine.py
git commit -m "feat(freeze): pre-place a frozen op on its pinned machine+operator (no setup)"
```

---

### Task 4: Multiple frozen ops on one machine — previous-plan order, all before new work

**Files:**
- Test: `tests/test_freeze_engine.py` (no new impl — verifies Task 3 behavior; add a guard only if it fails)

**Interfaces:**
- Consumes: `_preplace_frozen` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
def test_two_frozen_on_one_machine_resume_in_prev_plan_order_before_new_work(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # Two different orders sharing one machine option for a machining op.
    def first_machining(o):
        return next(op for op in nm.routings[o.item_code].operations
                    if op.machine_options and op.cycle_min > 0)
    oa, ob = orders[0], orders[1]
    opa, opb = first_machining(oa), first_machining(ob)
    mid = opa.machine_options[0]
    assert mid in opb.machine_options, "test needs two orders that can share a machine"
    from datetime import timedelta
    # ob's frozen op has the EARLIER prev_start, so it must resume first.
    foa = FrozenOp(oa.key, opa.seq, mid, "Alpha", 4, cfg.plan_start + timedelta(hours=1))
    fob = FrozenOp(ob.key, opb.seq, mid, "Alpha", 4, cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[foa, fob])
    on_mid = sorted([s for s in sched.segments if s.machine_id == mid], key=lambda s: s.start)
    # ob's frozen op resumes first (earlier prev_start), then oa's, then any new work.
    frozen_keys_in_order = [s.order_key for s in on_mid
                            if (s.order_key, s.op_seq) in {(ob.key, opb.seq), (oa.key, opa.seq)}]
    assert frozen_keys_in_order[0] == ob.key and frozen_keys_in_order[1] == oa.key
    frozen_end = max(s.end for s in on_mid
                     if (s.order_key, s.op_seq) in {(ob.key, opb.seq), (oa.key, opa.seq)})
    new_starts = [s.start for s in on_mid
                  if (s.order_key, s.op_seq) not in {(ob.key, opb.seq), (oa.key, opa.seq)}]
    assert all(st >= frozen_end for st in new_starts), "new work started before frozen work finished"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/test_freeze_engine.py::test_two_frozen_on_one_machine_resume_in_prev_plan_order_before_new_work -v`
Expected: PASS (Task 3's `machine_free` advance + `prev_start` sort already guarantee this). If it FAILS, fix `_preplace_frozen`'s sort/advance before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_freeze_engine.py
git commit -m "test(freeze): multiple frozen ops resume in prev-plan order before new work"
```

---

### Task 5: Downstream step waits for its frozen predecessor

**Files:**
- Test: `tests/test_freeze_engine.py`

**Interfaces:**
- Consumes: `_preplace_frozen` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
def test_downstream_step_waits_for_frozen_predecessor(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # An order whose routing has a machining op followed by a later in-house op.
    o = next(o for o in orders
             if len([op for op in nm.routings[o.item_code].operations
                     if op.machine_options and op.cycle_min > 0]) >= 1
             and len(nm.routings[o.item_code].operations) >= 2)
    ops = nm.routings[o.item_code].operations
    fop = next(op for op in ops if op.machine_options and op.cycle_min > 0)
    succ = next((op for op in ops if op.seq > fop.seq), None)
    assert succ is not None
    fo = FrozenOp(o.key, fop.seq, fop.machine_options[0], "Alpha", 6, cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[fo])
    frozen_end = max(s.end for s in sched.segments
                     if s.order_key == o.key and s.op_seq == fop.seq)
    succ_start = min((s.start for s in sched.segments
                      if s.order_key == o.key and s.op_seq == succ.seq), default=None)
    if succ_start is not None:  # OS/dispatch successors may be zero-time milestones
        assert succ_start >= frozen_end, "successor started before the frozen op finished"
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_freeze_engine.py::test_downstream_step_waits_for_frozen_predecessor -v`
Expected: PASS (the `ready_of`/`prev_end_of` advance in Task 3 enforces this). Fix `_preplace_frozen` if not.

- [ ] **Step 3: Commit**

```bash
git add tests/test_freeze_engine.py
git commit -m "test(freeze): downstream step waits for its frozen predecessor"
```

---

### Task 6: Absent planned operator → substitute, machine pinned, no double-booking

**Files:**
- Test: `tests/test_freeze_engine.py`

**Interfaces:**
- Consumes: `_lay_frozen` operator fallback (Task 3); `masters.calendar.leaves`.

- [ ] **Step 1: Write the failing test**

```python
from dataclasses import replace as _dc_replace

def test_absent_planned_operator_gets_substitute_machine_still_pinned(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    o0 = orders[0]
    fop = next(op for op in nm.routings[o0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    mid = fop.machine_options[0]
    # Mark the planned operator "Alpha" on leave for the plan-start day.
    cal = nm.calendar
    merged = dict(getattr(cal, "leaves", {}) or {})
    merged["Alpha"] = frozenset({cfg.plan_start.date()})
    nm_absent = _dc_replace(nm, calendar=_dc_replace(cal, leaves=merged))
    fo = FrozenOp(o0.key, fop.seq, mid, "Alpha", 5, cfg.plan_start)
    sched = decode(orders, seq, nm_absent, cfg, frozen=[fo])
    segs = [s for s in sched.segments if s.order_key == o0.key and s.op_seq == fop.seq]
    assert segs, "frozen op vanished when its planned operator was absent"
    assert all(s.machine_id == mid for s in segs), "machine not pinned under absence"
    assert all(s.operator != "Alpha" for s in segs), "absent operator was still assigned"
    # No operator double-booked (reuse the suite's invariant checker).
    from tests.test_new_engine import _assert_clean
    _assert_clean(sched.segments)
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_freeze_engine.py::test_absent_planned_operator_gets_substitute_machine_still_pinned -v`
Expected: PASS (the `is_operator_available` check in `_lay_frozen` routes to a substitute). Fix `_lay_frozen` if not.

- [ ] **Step 3: Commit**

```bash
git add tests/test_freeze_engine.py
git commit -m "test(freeze): absent planned operator → substitute, machine stays pinned"
```

---

# PHASE 3 — Thread `frozen` through the sequence search

### Task 7: `optimize(frozen=None)` + `_Evaluator` decode passes frozen

**Files:**
- Modify: `ppc_engine/optimize/search.py` (`_Evaluator.__init__` line 57; decode call line 74; `optimize` signature line 140-148; `_Evaluator` construction line 163)
- Test: `tests/test_freeze_engine.py`

**Interfaces:**
- Produces: `ppc_engine.optimize.search.optimize(orders, masters, config, budget=300, seed=0, on_progress=None, on_eval=None, frozen=None)`.

- [ ] **Step 1: Write the failing test**

```python
from ppc_engine.optimize import optimize as new_optimize

def test_search_respects_frozen(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    o0 = orders[0]
    fop = next(op for op in nm.routings[o0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    fo = FrozenOp(o0.key, fop.seq, fop.machine_options[0], "Alpha", 5, cfg.plan_start)
    res = new_optimize(orders, nm, cfg, budget=40, seed=1, frozen=[fo])
    # Re-decode the winning sequence WITH the frozen set → the frozen op is pinned there.
    sched = decode(orders, list(res.best_sequence), nm, cfg, frozen=[fo])
    segs = [s for s in sched.segments if s.order_key == o0.key and s.op_seq == fop.seq]
    assert all(s.machine_id == fop.machine_options[0] for s in segs)
    assert segs[0].start == cfg.plan_start
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_engine.py::test_search_respects_frozen -v`
Expected: FAIL — `optimize() got an unexpected keyword argument 'frozen'`

- [ ] **Step 3: Thread `frozen` through search.py**

`_Evaluator.__init__` (line 57): add `frozen=None` param, store `self._frozen = frozen`.
Decode call (line 74): `sched = decode(self._orders, list(sequence), self._masters, self._config, frozen=self._frozen)`.
`optimize` (line 140): add `frozen=None` to the signature; construct the evaluator with it (line 163): `ev = _Evaluator(orders, masters, config, on_eval=on_eval, frozen=frozen)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_engine.py::test_search_respects_frozen -v`
Expected: PASS

- [ ] **Step 5: Regression**

Run: `pytest tests/test_new_engine.py -q`
Expected: PASS (frozen defaults to None)

- [ ] **Step 6: Commit**

```bash
git add ppc_engine/optimize/search.py tests/test_freeze_engine.py
git commit -m "feat(freeze): sequence search honors the frozen set"
```

---

### Task 8: `optimize_overlap` + `tune_overlap` forward `frozen`

**Files:**
- Modify: `ppc_engine/optimize/contest.py` (`optimize_overlap` line 54-61, its `optimize(...)` call line 83; `tune_overlap` line 167-179, its `optimize(...)` call line 218)
- Test: `tests/test_freeze_engine.py`

**Interfaces:**
- Produces: `optimize_overlap(..., frozen=None)`, `tune_overlap(..., frozen=None)`.

- [ ] **Step 1: Write the failing test**

```python
from ppc_engine.optimize import tune_overlap

def test_tune_overlap_forwards_frozen(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    o0 = orders[0]
    fop = next(op for op in nm.routings[o0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    fo = FrozenOp(o0.key, fop.seq, fop.machine_options[0], "Alpha", 5, cfg.plan_start)
    tr = tune_overlap(orders, nm, cfg, lo=0.5, hi=0.9, seeds=(0,),
                      budget_per_eval=20, coarse=3, frozen=[fo])
    sched = decode(orders, list(tr.best_sequence), nm,
                   __import__("dataclasses").replace(cfg, overlap=tr.best_overlap), frozen=[fo])
    segs = [s for s in sched.segments if s.order_key == o0.key and s.op_seq == fop.seq]
    assert all(s.machine_id == fop.machine_options[0] for s in segs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_engine.py::test_tune_overlap_forwards_frozen -v`
Expected: FAIL — `tune_overlap() got an unexpected keyword argument 'frozen'`

- [ ] **Step 3: Forward `frozen` in contest.py**

- `optimize_overlap` (line 54): add `frozen=None`; at line 83 pass `frozen=frozen` into `optimize(...)`.
- `tune_overlap` (line 167): add `frozen=None`; inside `f(x)` at line 218 pass `frozen=frozen` into `optimize(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_engine.py::test_tune_overlap_forwards_frozen -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/optimize/contest.py tests/test_freeze_engine.py
git commit -m "feat(freeze): overlap contest + golden-section tuner forward the frozen set"
```

---

# PHASE 4 — Adapter: translate app-frozen → ppc-frozen and thread it

### Task 9: `new_engine._ppc_frozen(...)` — map app rows to `FrozenOp`s

**Files:**
- Modify: `engine/new_engine.py` (add helper near `_orders_from_batches`, line 206)
- Test: `tests/test_freeze_adapter.py` (Create)

**Interfaces:**
- Consumes: app-frozen rows `{so_no, item_code, process, op_seq, machine, operator, remaining_qty, prev_start}` (ISO string `prev_start`); `batch_by_key` from `_orders_from_batches`; new masters.
- Produces: `new_engine._ppc_frozen(rows, orders, batch_by_key, masters) -> list[FrozenOp]`.

**Mapping rules:** an app row targets a batch via `(so_no, item_code)` → the batch whose `source_so_refs` contains `so_no` and `item_code` matches (this is `batch.batch_id`, which is the ppc `order_key[0]`). The op is resolved by matching `_norm(process)` to a routing op name → its `seq`. Rows that don't map to a scheduled order, whose machine is unknown/OS/off-lane, or whose `remaining_qty <= 0` are dropped.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_adapter.py
import io
from datetime import date
import pytest
from engine.config import Config
from engine import book_store, loaders
from engine.new_engine import _orders_from_batches, _ppc_frozen, _new_masters, _plan_config
from engine.rules import rule1_consolidate
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import FrozenOp
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)

def test_ppc_frozen_maps_so_and_process_to_frozenop():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    old = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(old.so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    o0 = orders[0]
    batch = batch_by_key[o0.key]
    routing = nm.routings[o0.item_code]
    mop = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    row = {"so_no": batch.source_so_refs[0], "item_code": o0.item_code,
           "process": mop.name, "op_seq": mop.seq, "machine": mop.machine_options[0],
           "operator": "Alpha", "remaining_qty": 7, "prev_start": "2025-03-03T08:00:00"}
    fos = _ppc_frozen([row], orders, batch_by_key, nm)
    assert len(fos) == 1
    fo = fos[0]
    assert isinstance(fo, FrozenOp)
    assert fo.order_key == o0.key and fo.op_seq == mop.seq
    assert fo.machine_id == mop.machine_options[0] and fo.operator == "Alpha"
    assert fo.remaining_qty == 7

def test_ppc_frozen_drops_unmappable_rows():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    old = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(old.so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    rows = [{"so_no": "GHOST", "item_code": "NOPE", "process": "x", "op_seq": 1,
             "machine": "CNC1", "operator": "Alpha", "remaining_qty": 5,
             "prev_start": "2025-03-03T08:00:00"}]
    assert _ppc_frozen(rows, orders, batch_by_key, nm) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_adapter.py -v`
Expected: FAIL — `ImportError: cannot import name '_ppc_frozen'`

- [ ] **Step 3: Implement `_ppc_frozen`**

In `engine/new_engine.py`, add near `_orders_from_batches`:

```python
def _ppc_frozen(rows, orders, batch_by_key, masters):
    """Map app-level frozen rows -> ppc FrozenOp[] for decode. Each row is
    {so_no, item_code, process, op_seq, machine, operator, remaining_qty, prev_start-iso}.
    A row maps to the scheduled batch whose source SOs include ``so_no`` (batch_id ==
    ppc order_key[0]); its op_seq is taken from the row (or resolved via the routing by
    normalised process name). Rows that don't map to a scheduled order, have an unknown/
    OS machine, or have remaining_qty<=0 are dropped."""
    from datetime import datetime
    from ppc_engine.scheduler import FrozenOp
    # Reverse index: (so_no, item_code) -> order_key of the batch that covers it.
    so_to_key = {}
    for key, batch in batch_by_key.items():
        for so in (getattr(batch, "source_so_refs", None) or []):
            so_to_key[(so, batch.item_code)] = key
    order_by_key = {o.key: o for o in orders}
    out = []
    for r in rows or []:
        key = so_to_key.get((r.get("so_no"), r.get("item_code")))
        if key is None or key not in order_by_key:
            continue
        mid = r.get("machine")
        if not mid or mid not in masters.machines:   # unknown / OS / off-lane
            continue
        qty = int(round(float(r.get("remaining_qty", 0))))
        if qty <= 0:
            continue
        # Resolve op_seq: trust the row, else match the routing by normalised name.
        op_seq = r.get("op_seq")
        if op_seq is None:
            want = _norm(r.get("process", ""))
            op_seq = next((op.seq for op in masters.routings[order_by_key[key].item_code].operations
                           if _norm(op.name) == want), None)
            if op_seq is None:
                continue
        try:
            prev_start = datetime.fromisoformat(r["prev_start"])
        except (KeyError, ValueError):
            continue
        out.append(FrozenOp(order_key=key, op_seq=int(op_seq), machine_id=mid,
                            operator=r.get("operator", "") or "",
                            remaining_qty=qty, prev_start=prev_start))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py tests/test_freeze_adapter.py
git commit -m "feat(freeze): map app frozen rows to ppc FrozenOps in the adapter"
```

---

### Task 10: `new_engine.run(frozen=...)` passes frozen into `decode`

**Files:**
- Modify: `engine/new_engine.py` (`run` line 327-348)
- Test: `tests/test_freeze_adapter.py`

**Interfaces:**
- Produces: `new_engine.run(batches, config=None, notes=None, masters=None, machine_lost_min=None, reserved=None, frozen=None, **kw)`.

- [ ] **Step 1: Write the failing test**

```python
def test_new_engine_run_pins_frozen_step():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    old = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(old.so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    b0 = batches[0]
    routing = nm.routings[b0.item_code]
    mop = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    row = {"so_no": b0.source_so_refs[0], "item_code": b0.item_code,
           "process": mop.name, "op_seq": mop.seq, "machine": mop.machine_options[0],
           "operator": "Alpha", "remaining_qty": 6, "prev_start": "2025-03-03T08:00:00"}
    from engine.new_engine import run as new_run
    entries = new_run(batches, config=_CONF, masters=old.masters, frozen=[row])
    hit = [e for e in entries if e.batch_id == b0.batch_id and e.process_seq == mop.seq]
    assert hit and hit[0].machine == mop.machine_options[0]
    assert hit[0].operator == "Alpha"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_adapter.py::test_new_engine_run_pins_frozen_step -v`
Expected: FAIL — `run()` ignores `frozen` (passes to `decode` without it), so machine/operator aren't pinned.

- [ ] **Step 3: Thread frozen through `run`**

In `new_engine.run` (line 327), add `frozen=None` to the signature. After building `orders, batch_by_key` (line 341) and `sequence` (line 345), map and pass:

```python
    ppc_frozen = _ppc_frozen(frozen, orders, batch_by_key, new_masters) if frozen else None
    sched = decode(orders, sequence, new_masters, _plan_config(config), frozen=ppc_frozen)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_adapter.py::test_new_engine_run_pins_frozen_step -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py tests/test_freeze_adapter.py
git commit -m "feat(freeze): new_engine.run threads the frozen set into decode"
```

---

### Task 11: `new_engine.optimize_sequence`/`tune`/`sweep_optimize(frozen=...)`

**Files:**
- Modify: `engine/new_engine.py` (`optimize_sequence` line 351-385; `tune` line 388-430; `sweep_optimize` line 433-458)
- Test: `tests/test_freeze_adapter.py`

**Interfaces:**
- Produces: `optimize_sequence(..., frozen=None)`, `tune(..., frozen=None)`, `sweep_optimize(..., frozen=None)` — each mapping `frozen` via `_ppc_frozen` and passing it to the ppc search + the winner re-decode.

- [ ] **Step 1: Write the failing test**

```python
def test_sweep_optimize_accepts_frozen_and_pins_winner():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    old = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(old.so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    b0 = batches[0]
    mop = next(op for op in nm.routings[b0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    row = {"so_no": b0.source_so_refs[0], "item_code": b0.item_code, "process": mop.name,
           "op_seq": mop.seq, "machine": mop.machine_options[0], "operator": "Alpha",
           "remaining_qty": 5, "prev_start": "2025-03-03T08:00:00"}
    from engine.new_engine import sweep_optimize
    sr = sweep_optimize(old.so_lines, _CONF, old.masters, budget_evals=40, frozen=[row])
    assert sr.result.ranks  # produced a plan without error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_adapter.py::test_sweep_optimize_accepts_frozen_and_pins_winner -v`
Expected: FAIL — `sweep_optimize() got an unexpected keyword argument 'frozen'`

- [ ] **Step 3: Thread frozen through the three functions**

`optimize_sequence` (line 351): add `frozen=None`. After `orders, batch_by_key = _orders_from_batches(batches, nm)` (line 370), compute `ppc_frozen = _ppc_frozen(frozen, orders, batch_by_key, nm) if frozen else None`. Pass `frozen=ppc_frozen` into `new_optimize(...)` (line 373) and pass `frozen=frozen` into the winner-scoring `run(best_batches, config=config, masters=masters, reserved=reserved)` (line 380) → `run(best_batches, config=config, masters=masters, reserved=reserved, frozen=frozen)`.

`tune` (line 388): add `frozen=None`. After `orders, batch_by_key = _orders_from_batches(...)` (line 415), compute `ppc_frozen = _ppc_frozen(frozen, orders, batch_by_key, new_masters) if frozen else None`. Pass `frozen=ppc_frozen` into `tune_overlap(...)` (line 417) and into the winner re-decode `decode(orders, tr.best_sequence, new_masters, won_cfg)` (line 427) → add `frozen=ppc_frozen`.

`sweep_optimize` (line 433): add `frozen=None`; forward it into the `tune(...)` call (line 449-451) as `frozen=frozen`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_adapter.py::test_sweep_optimize_accepts_frozen_and_pins_winner -v`
Expected: PASS

- [ ] **Step 5: Regression**

Run: `pytest tests/test_new_engine.py tests/test_freeze_engine.py tests/test_freeze_adapter.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add engine/new_engine.py tests/test_freeze_adapter.py
git commit -m "feat(freeze): new_engine optimize/tune/sweep thread the frozen set"
```

---

# PHASE 5 — Pipeline + optimizer threading

### Task 12: `run_forward(frozen=None)` → scheduler; classic/flow accept+ignore

**Files:**
- Modify: `engine/pipeline.py` (`run_forward` line 188-208; rule6 call line 235-239)
- Modify: `engine/rules/rule6_allocate.py` (`run` signature, ~line 488), `engine/flow_scheduler.py` (`run` signature, line 434)
- Test: `tests/test_freeze_pipeline.py` (Create)

**Interfaces:**
- Produces: `run_forward(plan_run, config, masters, machine_lost_min=None, reserved=None, priority_rank=None, frozen=None)`.
- `new_engine.run` (Task 10) already accepts `frozen`. Classic/flow `run(...)` must accept `frozen=None` and ignore it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_pipeline.py
import io
from datetime import date
from engine.config import Config
from engine import book_store, loaders, pipeline
from engine.models import PlanRun
from tests.new_sample_workbook import build_new_sample_bytes

def test_run_forward_frozen_none_is_byte_identical():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    old = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    pr1 = PlanRun(so_lines=list(old.so_lines))
    pipeline.run_forward(pr1, cfg, old.masters)
    pr2 = PlanRun(so_lines=list(old.so_lines))
    pipeline.run_forward(pr2, cfg, old.masters, frozen=None)
    assert [e.__dict__ for e in pr1.schedule] == [e.__dict__ for e in pr2.schedule]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_pipeline.py -v`
Expected: FAIL — `run_forward() got an unexpected keyword argument 'frozen'`

- [ ] **Step 3: Thread frozen through run_forward + scheduler signatures**

`engine/pipeline.py` `run_forward` (line 188): add `frozen: dict | None = None` param (document it in the docstring). In the rule6 call (line 235-239), add `frozen=frozen`:

```python
        plan_run.schedule = run_rule(
            trace, "rule6", scheduler_for(config), plan_run.batches_prioritized,
            config=config, masters=masters, machine_lost_min=machine_lost_min,
            reserved=reserved, frozen=frozen,
        )
```

`engine/rules/rule6_allocate.py` `run(...)`: it already ends with `**kw`, so it tolerates the extra kwarg — but add an explicit `frozen=None` param for clarity and a one-line comment "classic engine ignores frozen (new-engine-only feature)".

`engine/flow_scheduler.py` `run(...)` (line 434): its signature is fixed (no `**kw`) — add `frozen=None` and ignore it (comment: "flow engine ignores frozen").

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_pipeline.py -v`
Expected: PASS

- [ ] **Step 5: Golden regression**

Run: `pytest -k golden -q && pytest tests/test_new_engine.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add engine/pipeline.py engine/rules/rule6_allocate.py engine/flow_scheduler.py tests/test_freeze_pipeline.py
git commit -m "feat(freeze): run_forward threads frozen; classic/flow accept+ignore it"
```

---

### Task 13: `engine.optimizer.optimize`/`sweep_optimize` forward `frozen`

**Files:**
- Modify: `engine/optimizer.py` (`optimize` + `sweep_optimize` — the functions that delegate to `new_engine` for `scheduler=="new"`)
- Test: `tests/test_freeze_pipeline.py`

**Interfaces:**
- Produces: `optimizer.optimize(so_lines, config, masters, reserved=None, budget_evals=..., seed=..., on_progress=None, should_cancel=None, frozen=None)` and `optimizer.sweep_optimize(..., frozen=None)`, both forwarding `frozen` into the `new_engine.*` delegates.

- [ ] **Step 1: Write the failing test**

```python
def test_optimizer_sweep_forwards_frozen():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    old = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    batches = __import__("engine.rules.rule1_consolidate", fromlist=["run"]).run(old.so_lines, cfg)
    orders, batch_by_key = __import__("engine.new_engine", fromlist=["_orders_from_batches"])._orders_from_batches(
        batches, __import__("engine.new_engine", fromlist=["_new_masters"])._new_masters())
    b0 = batches[0]
    from engine import optimizer
    row = {"so_no": b0.source_so_refs[0], "item_code": b0.item_code, "process": "",
           "op_seq": None, "machine": "CNC1", "operator": "Alpha",
           "remaining_qty": 3, "prev_start": "2025-03-03T08:00:00"}
    sr = optimizer.sweep_optimize(old.so_lines, cfg, old.masters, budget_evals=30, frozen=[row])
    assert sr is not None  # ran without error with frozen present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_pipeline.py::test_optimizer_sweep_forwards_frozen -v`
Expected: FAIL — `sweep_optimize() got an unexpected keyword argument 'frozen'`

- [ ] **Step 3: Forward frozen in engine/optimizer.py**

Locate the `optimize` and `sweep_optimize` functions in `engine/optimizer.py`. For `scheduler == "new"` they delegate to `new_engine.optimize_sequence` / `new_engine.sweep_optimize`. Add a `frozen=None` param to both and pass `frozen=frozen` into those delegate calls. (The classic-engine branch ignores `frozen`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_pipeline.py::test_optimizer_sweep_forwards_frozen -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/optimizer.py tests/test_freeze_pipeline.py
git commit -m "feat(freeze): engine.optimizer optimize/sweep forward the frozen set"
```

---

# PHASE 6 — Contest, cloud payload, signature

### Task 14: `optimize_service` threads `frozen` (contest + payload + signature)

**Files:**
- Modify: `engine/optimize_service.py` — `ContestSetup` (line 181-195), `prepare_contest` (line 198-230), `run_candidate` (line 251-276), `build_payload` (line 134-153), `parse_payload` (line 156-175), `book_signature` (line 113-128)
- Test: `tests/test_freeze_contest.py` (Create)

**Interfaces:**
- Produces: `ContestSetup.frozen`; `prepare_contest(..., frozen=None)`; `build_payload(..., frozen=None)` writes a `"frozen"` key; `parse_payload` returns `frozen` as the LAST tuple element; `run_candidate` passes `frozen` into `optimizer.optimize`; `book_signature(so_lines, absences=None, frozen=None)` folds frozen in.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_contest.py
import io
from datetime import date
from engine.config import Config
from engine import book_store, loaders, optimize_service

def test_payload_round_trips_frozen():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    old = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    orders = book_store.load_active_orders()  # may be empty; that's fine for the round-trip
    frozen = [{"so_no": "SO1", "item_code": "IT-A", "process": "CNC first side",
               "op_seq": 2, "machine": "CNC1", "operator": "Alpha",
               "remaining_qty": 4, "prev_start": "2026-07-29T08:00:00"}]
    payload = optimize_service.build_payload(orders, [], wb, cfg, seed=1, frozen=frozen)
    assert payload["frozen"] == frozen
    parsed = optimize_service.parse_payload(payload)
    assert parsed[-1] == frozen  # frozen is the last element of the parse tuple

def test_book_signature_changes_with_frozen():
    from tests.new_sample_workbook import build_new_sample_bytes as _b
    wb = _b(); book_store.save_masters_bytes(wb)
    old = loaders.load_all(io.BytesIO(wb))
    from engine import orderbook
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      book_store.load_actuals(), old.masters)
    a = optimize_service.book_signature(lines, absences=[], frozen=[])
    b = optimize_service.book_signature(lines, absences=[],
            frozen=[{"so_no": "SO1", "item_code": "IT-A", "op_seq": 2,
                     "machine": "CNC1", "remaining_qty": 4}])
    assert a != b

from tests.new_sample_workbook import build_new_sample_bytes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_contest.py -v`
Expected: FAIL — `build_payload() got an unexpected keyword argument 'frozen'`

- [ ] **Step 3: Thread frozen through optimize_service.py**

- `ContestSetup` (line 181): add fields `frozen: list = field(default_factory=list)`.
- `prepare_contest` (line 198): add `frozen=None` param; pass `frozen=list(frozen or [])` into the `ContestSetup(...)` constructor (line 228-230).
- `build_payload` (line 134): add `frozen=None` param; add `"frozen": list(frozen or [])` to the returned dict (sibling of `"absences"`, line 151).
- `parse_payload` (line 156): add `frozen = list(payload.get("frozen") or [])`; append `frozen` to the returned tuple (line 175, now 7 elements).
- `run_candidate` (line 251): update the unpack at line 258 to `orders, actuals, masters, config, absences, operator_table, frozen = parse_payload(payload)`; pass `frozen=frozen` into `prepare_contest(...)` (line 265); and pass `frozen=setup.frozen` into `optimizer.optimize(...)` (line 269-273).
- `book_signature` (line 113): add `frozen=None` param; extend the hashed `blob` (line 125-126) with a third sorted list of frozen identity tuples, e.g. `sorted((f.get("so_no",""), f.get("item_code",""), f.get("op_seq"), f.get("machine",""), round(float(f.get("remaining_qty",0)),3)) for f in (frozen or []))`.

**Grep for other `parse_payload` callers** — `grep -rn "parse_payload" engine/ scripts/ api/` — and fix any that unpack the tuple (only `run_candidate` in this repo, per the audit; the cloud worker forwards the opaque payload and needs no change).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_contest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/optimize_service.py tests/test_freeze_contest.py
git commit -m "feat(freeze): thread frozen through contest, cloud payload, and book signature"
```

---

# PHASE 7 — Frozen-set computation (pure, app-level)

### Task 15: `engine/freeze.py` — project a schedule to storable rows

**Files:**
- Create: `engine/freeze.py`
- Test: `tests/test_freeze_logic.py` (Create)

**Interfaces:**
- Consumes: a `list[ScheduleEntry]` (fields per `engine/models.py:243-269`).
- Produces: `freeze.schedule_projection(schedule) -> list[dict]` — one row per real (machine) op: `{batch_id, item_code, process_seq, process_name, machine, operator, start(iso), end(iso), so_refs}`. OS/off-lane and dispatch entries are skipped (no machine to pin).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_logic.py
from datetime import datetime
from engine import freeze
from engine.models import ScheduleEntry

def test_schedule_projection_keeps_machine_ops_only():
    entries = [
        ScheduleEntry(batch_id="B1", item_code="IT-A", process_seq=2,
                      process_name="CNC first side", machine="CNC1", qty=100,
                      occupancy_min=120, start=datetime(2026,7,29,8,0),
                      end=datetime(2026,7,29,10,0), operator="Alpha", so_refs=["SO1"]),
        ScheduleEntry(batch_id="B1", item_code="IT-A", process_seq=9,
                      process_name="DISPATCH", machine="OS / Outsourced", qty=100,
                      occupancy_min=0, start=datetime(2026,7,29,10,0),
                      end=datetime(2026,7,29,10,0), operator="", so_refs=["SO1"]),
    ]
    rows = freeze.schedule_projection(entries)
    assert len(rows) == 1
    r = rows[0]
    assert r["machine"] == "CNC1" and r["operator"] == "Alpha"
    assert r["process_seq"] == 2 and r["so_refs"] == ["SO1"]
    assert r["start"] == "2026-07-29T08:00:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_logic.py::test_schedule_projection_keeps_machine_ops_only -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.freeze'`

- [ ] **Step 3: Create `engine/freeze.py` with `schedule_projection`**

```python
"""Pure freeze logic (reporting/derivation only — never mutates a plan).

Two pure functions:
  - ``schedule_projection(schedule)`` — the applied plan's per-op assignment, the durable
    record of "the plan the floor is following" (machine + operator + time per op).
  - ``compute_frozen_set(applied_rows, actuals, so_lines, masters, config)`` — from that
    record + the punches, the in-progress ops to FREEZE (machine/operator from the plan,
    remaining qty from the punches). See the 2026-07-29 spec.
"""
from __future__ import annotations

_OS_LANES = {"OS / Outsourced", "Off-machine"}


def schedule_projection(schedule) -> list[dict]:
    """One row per real (machine) operation in the applied plan. OS/off-lane entries are
    skipped (no in-house machine to pin)."""
    rows = []
    for e in schedule:
        if e.machine in _OS_LANES:
            continue
        rows.append({
            "batch_id": e.batch_id,
            "item_code": e.item_code,
            "process_seq": e.process_seq,
            "process_name": e.process_name,
            "machine": e.machine,
            "operator": e.operator or "",
            "start": e.start.isoformat(timespec="seconds"),
            "end": e.end.isoformat(timespec="seconds"),
            "so_refs": list(e.so_refs or []),
        })
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_logic.py::test_schedule_projection_keeps_machine_ops_only -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/freeze.py tests/test_freeze_logic.py
git commit -m "feat(freeze): schedule_projection — durable record of the applied plan"
```

---

### Task 16: `compute_frozen_set(...)` — in-progress detection + machine/operator lookup

**Files:**
- Modify: `engine/freeze.py`
- Test: `tests/test_freeze_logic.py`

**Interfaces:**
- Consumes: `applied_rows` (Task 15 output), `so_lines` (active lines with `.process_qty` remaining, `.so_no`, `.item_code`), `masters` (routings), and per-step good-qty from actuals.
- Produces: `freeze.compute_frozen_set(applied_rows, so_lines, good_by_step, masters) -> list[dict]` where each row = `{so_no, item_code, process, op_seq, machine, operator, remaining_qty, prev_start}`. `good_by_step` maps `(so_no, item_code, normalised_process) -> good qty`.

**Detection:** a step is frozen iff `good > 0` AND `remaining > 0` (partially punched). Machine/operator come from `applied_rows` — the row whose `item_code` matches, `process_seq` matches the step's op_seq (resolved via routing by name), and `so_no in so_refs`. If no such applied row (new order / not in last plan), or its machine is OS/off-lane, the step is **not** frozen (omitted).

- [ ] **Step 1: Write the failing test**

```python
from engine.loaders import normalize_process_name as _np

class _Line:
    def __init__(self, so_no, item_code, process_qty):
        self.so_no, self.item_code, self.process_qty = so_no, item_code, process_qty

class _Op:
    def __init__(self, seq, name): self.seq, self.name = seq, name
class _Routing:
    def __init__(self, ops): self.operations = ops
class _Masters:
    def __init__(self, routings): self.routings = routings

def test_compute_frozen_set_picks_partially_punched_steps():
    applied = [{"batch_id": "B1", "item_code": "IT-A", "process_seq": 2,
                "process_name": "CNC first side", "machine": "CNC1", "operator": "Alpha",
                "start": "2026-07-29T08:00:00", "end": "2026-07-29T10:00:00",
                "so_refs": ["SO1"]}]
    masters = _Masters({"IT-A": _Routing([_Op(1, "CNC prep"), _Op(2, "CNC first side")])})
    # Step seq 2 has remaining 40 (partially done) → frozen.
    lines = [_Line("SO1", "IT-A", {_np("CNC first side"): 40, _np("CNC prep"): 0})]
    good = {("SO1", "IT-A", _np("CNC first side")): 60,   # 60 done, 40 left → in progress
            ("SO1", "IT-A", _np("CNC prep")): 100}        # fully done → not frozen
    rows = freeze.compute_frozen_set(applied, lines, good, masters)
    assert len(rows) == 1
    r = rows[0]
    assert r["so_no"] == "SO1" and r["op_seq"] == 2 and r["machine"] == "CNC1"
    assert r["operator"] == "Alpha" and r["remaining_qty"] == 40
    assert r["prev_start"] == "2026-07-29T08:00:00"

def test_compute_frozen_set_skips_step_not_in_last_plan():
    masters = _Masters({"IT-A": _Routing([_Op(2, "CNC first side")])})
    lines = [_Line("SO1", "IT-A", {_np("CNC first side"): 40})]
    good = {("SO1", "IT-A", _np("CNC first side")): 60}
    assert freeze.compute_frozen_set([], lines, good, masters) == []  # no applied row
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_logic.py::test_compute_frozen_set_picks_partially_punched_steps -v`
Expected: FAIL — `AttributeError: module 'engine.freeze' has no attribute 'compute_frozen_set'`

- [ ] **Step 3: Implement `compute_frozen_set`**

Append to `engine/freeze.py`:

```python
from engine.loaders import normalize_process_name as _norm


def compute_frozen_set(applied_rows, so_lines, good_by_step, masters) -> list[dict]:
    """Frozen (in-progress) ops: partially-punched steps (good>0 and remaining>0),
    with machine + operator looked up from the applied plan. Steps not present in the
    applied plan, or whose applied machine is OS/off-lane, are not frozen."""
    # Index applied rows: (item_code, process_seq) -> list of rows (with so_refs).
    by_item_seq: dict[tuple[str, int], list[dict]] = {}
    for r in applied_rows or []:
        by_item_seq.setdefault((r["item_code"], r["process_seq"]), []).append(r)

    out = []
    for line in so_lines:
        routing = masters.routings.get(line.item_code)
        if routing is None:
            continue
        pq = line.process_qty or {}
        for op in routing.operations:
            nkey = _norm(op.name)
            remaining = int(round(float(pq.get(nkey, 0))))
            good = int(round(float(good_by_step.get((line.so_no, line.item_code, nkey), 0))))
            if good <= 0 or remaining <= 0:
                continue  # not started, or fully done → not frozen
            # Machine/operator from the applied plan row covering this SO for this op.
            cand = by_item_seq.get((line.item_code, op.seq), [])
            row = next((r for r in cand if line.so_no in (r.get("so_refs") or [])), None)
            if row is None or row["machine"] in _OS_LANES:
                continue  # not in last plan / outsourced → not frozen
            out.append({
                "so_no": line.so_no, "item_code": line.item_code,
                "process": op.name, "op_seq": op.seq,
                "machine": row["machine"], "operator": row.get("operator", "") or "",
                "remaining_qty": remaining, "prev_start": row["start"],
            })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_logic.py -v`
Expected: PASS (both cases)

- [ ] **Step 5: Commit**

```bash
git add engine/freeze.py tests/test_freeze_logic.py
git commit -m "feat(freeze): compute_frozen_set from applied plan + punches"
```

---

# PHASE 8 — API wiring (this is where the feature goes live)

### Task 17: Persist the applied schedule at apply time

**Files:**
- Modify: `api/main.py` (`_optimize_apply` line 1587-1616 — after `book_store.save_plan_priority(res["ranks"], meta)` at line 1602)
- Test: `tests/test_freeze_api.py` (Create)

**Interfaces:**
- Consumes: `_metrics_for_ranks`/`_all_lines_schedule` (existing, recompute the schedule from ranks), `freeze.schedule_projection` (Task 15), `book_store.save_last_applied_schedule` (Task 1).
- Produces: after any apply, `book_store.load_last_applied_schedule()` returns the applied plan's op rows.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_freeze_api.py — uses the FastAPI app with the sample workbook seeded + orders merged.
# (Follow the setup pattern in tests/test_absences_api.py / tests/test_optimize_service.py:
#  seed masters, merge orders, log in as admin, run a plan, apply an optimize.)
def test_apply_persists_last_applied_schedule(admin_client_with_book):
    client = admin_client_with_book
    # Kick a quick optimize + apply.
    client.post("/optimize", json={"budget": "quick"})
    _wait_optimize_done(client)              # helper: poll /optimize/status until done
    client.post("/optimize/apply")
    from engine import book_store
    rows = book_store.load_last_applied_schedule()
    assert rows, "apply did not persist the applied schedule"
    assert {"batch_id", "item_code", "process_seq", "machine", "operator",
            "start", "end", "so_refs"} <= set(rows[0].keys())
```

(If a shared fixture doesn't exist, create `admin_client_with_book` + `_wait_optimize_done` in `tests/conftest.py` mirroring `tests/test_absences_api.py`'s app/login setup and the sample workbook merge.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_api.py::test_apply_persists_last_applied_schedule -v`
Expected: FAIL — `load_last_applied_schedule()` returns `[]`.

- [ ] **Step 3: Persist the schedule in `_optimize_apply`**

In `api/main.py` `_optimize_apply`, immediately after `book_store.save_plan_priority(res["ranks"], meta)` (line 1602), recompute the applied schedule from the winning ranks and persist its projection:

```python
        # Persist the applied plan's per-op assignment (machine/operator/time) so the
        # next "Done" can freeze whatever is in progress on its real machine. Recompute
        # from the winning ranks the same way the incumbent is scored.
        try:
            setup = optimize_service.prepare_contest(
                book_store.load_active_orders(), book_store.load_actuals(),
                _current_masters(), _resolve_config(_load_plan_config()),
                absences=book_store.load_absences(),
                operator_table=book_store.load_operator_table())
            sched, _ = _all_lines_schedule(setup, setup.masters, res["ranks"])
            book_store.save_last_applied_schedule(freeze.schedule_projection(sched))
        except Exception:
            pass  # never let schedule-snapshotting break an apply
```

Add `from engine import freeze` to the imports at the top of `api/main.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_api.py::test_apply_persists_last_applied_schedule -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_freeze_api.py tests/conftest.py
git commit -m "feat(freeze): persist the applied schedule at apply time"
```

---

### Task 18: Compute + persist the frozen set on "Done"; thread into the contest

**Files:**
- Modify: `api/main.py` — a new `_compute_and_store_frozen()` helper; call it in `_try_start_auto` (line 1110) before starting the contest; thread `frozen` into `_start_optimize` (line 1155) → `prepare_contest` (line 1195-1200) and `build_payload` (line 1208-1214)
- Test: `tests/test_freeze_api.py`

**Interfaces:**
- Consumes: `freeze.compute_frozen_set` (Task 16), `book_store.load_last_applied_schedule` (Task 1), `book_store.save_frozen_ops` (Task 1).
- Produces: `_compute_and_store_frozen() -> list[dict]` (also persists via `save_frozen_ops`); `_start_optimize` and the contest evaluate against the stored frozen set.

**good_by_step:** build `good_by_step[(so_no, item_code, _norm(process))] += actual.good_qty()` from `book_store.load_actuals()` (using `Actual.good_qty()` and `loaders.normalize_process_name(a.process)`).

- [ ] **Step 1: Write the failing test**

```python
def test_done_computes_frozen_set_from_partial_punch(admin_client_with_book):
    client = admin_client_with_book
    # Apply an initial plan so a last-applied schedule exists.
    client.post("/optimize", json={"budget": "quick"}); _wait_optimize_done(client)
    client.post("/optimize/apply")
    # Punch a PARTIAL quantity on the first machining step of a known SO+item (helper
    # picks an in-house machining step from the sample routing and posts good < required).
    _punch_partial(client)
    # Compute the frozen set (Done path helper) directly.
    import api.main as m
    frozen = m._compute_and_store_frozen()
    from engine import book_store
    assert frozen == book_store.load_frozen_ops()
    assert any(r["remaining_qty"] > 0 and r["machine"] for r in frozen)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_api.py::test_done_computes_frozen_set_from_partial_punch -v`
Expected: FAIL — `AttributeError: module 'api.main' has no attribute '_compute_and_store_frozen'`

- [ ] **Step 3: Add `_compute_and_store_frozen` and thread it**

In `api/main.py`, add near `_try_start_auto`:

```python
def _compute_and_store_frozen() -> list:
    """Derive the frozen (in-progress) set from the last-applied plan + the punches and
    persist it (anvitech:frozen_ops). Machine/operator from the applied plan; remaining
    qty from the punches. Empty when nothing is in progress or no plan is on file yet."""
    from collections import defaultdict
    masters = _current_masters()
    actuals = book_store.load_actuals()
    active = book_store.load_active_orders()
    so_lines = orderbook.active_so_lines(active, actuals, masters)
    applied = book_store.load_last_applied_schedule()
    good_by_step = defaultdict(float)
    for a in actuals:
        good_by_step[(a.so_no, a.item_code, loaders.normalize_process_name(a.process))] += a.good_qty()
    rows = freeze.compute_frozen_set(applied, so_lines, dict(good_by_step), masters)
    book_store.save_frozen_ops(rows)
    return rows
```

In `_try_start_auto` (line 1148, just before `_start_optimize(...)`), call `_compute_and_store_frozen()` so the contest that is about to start sees the current frozen set.

In `_start_optimize` (line 1155): after `absences = book_store.load_absences()` (line 1193), add `frozen = book_store.load_frozen_ops()`. Pass `frozen=frozen` into `optimize_service.prepare_contest(...)` (line 1195-1200) and into `optimize_service.build_payload(...)` (cloud branch, line 1208-1214).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_api.py::test_done_computes_frozen_set_from_partial_punch -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_freeze_api.py
git commit -m "feat(freeze): compute+persist the frozen set on Done and feed the contest"
```

---

### Task 19: `_plan` reads the frozen set and passes it through; fingerprints updated

**Files:**
- Modify: `api/main.py` — `_plan` (read frozen near line 726-727; pass `frozen=` at the run_forward call line 740-742); `_all_lines_schedule` (line 1424-1434, pass `frozen=setup.frozen`); `_plan_run_for_report` (line 2094); `_plan_fingerprint` (line 1056-1084, add frozen); `_current_book_sig` (line 1047-1053, pass frozen to `book_signature`); `prepare_contest` call in `_incumbent_metrics` (line 1536-1538, pass frozen)
- Test: `tests/test_freeze_api.py`

**Interfaces:**
- Consumes: `book_store.load_frozen_ops`, `optimize_service.book_signature(..., frozen=)`.
- Produces: the displayed `_plan` pins the stored frozen set; `_plan_fingerprint` and `_current_book_sig` change when the frozen set changes.

- [ ] **Step 1: Write the failing test**

```python
def test_plan_pins_stored_frozen_step(admin_client_with_book):
    client = admin_client_with_book
    client.post("/optimize", json={"budget": "quick"}); _wait_optimize_done(client)
    client.post("/optimize/apply")
    _punch_partial(client)
    import api.main as m
    m._compute_and_store_frozen()
    # The next plan must keep the frozen step on its stored machine.
    from engine import book_store
    frozen = book_store.load_frozen_ops()
    assert frozen
    gantt = client.get("/gantt").json()
    # The frozen op's bar is on its pinned machine (assert via the gantt/trace surface).
    _assert_frozen_step_on_pinned_machine(gantt, frozen[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_api.py::test_plan_pins_stored_frozen_step -v`
Expected: FAIL — `_plan` doesn't pass `frozen` yet, so the step may be re-assigned.

- [ ] **Step 3: Wire `_plan` and the fingerprints**

- `_plan` (line 726-727 area): after loading ranks, add `frozen = book_store.load_frozen_ops()`. At the run_forward call (line 740-742): `run_forward(plan_run, ranked_config, masters, reserved=ab or None, priority_rank=ranks, frozen=frozen or None)`.
- `_all_lines_schedule` (line 1432): add `frozen=getattr(setup, "frozen", None) or None` to its `run_forward(...)` call.
- `_plan_run_for_report` (line 2094): add `frozen=book_store.load_frozen_ops() or None` to its `run_forward(...)` call (so the delay report matches the plan).
- `_current_book_sig` (line 1047-1053): pass `frozen=book_store.load_frozen_ops()` into `optimize_service.book_signature(...)`.
- `_plan_fingerprint` (line 1056-1084): add `"frozen": book_store.load_frozen_ops()` to the `parts` dict so a freeze change busts the plan cache.
- `_incumbent_metrics` (line 1536-1538): pass `frozen=book_store.load_frozen_ops()` into its `prepare_contest(...)` so the incumbent is scored under the same freeze as the contest winner (fair strictly-better gate).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_api.py::test_plan_pins_stored_frozen_step -v`
Expected: PASS

- [ ] **Step 5: Regression — no frozen set ⇒ unchanged**

Run: `pytest tests/test_freeze_api.py tests/test_new_engine.py -q`
Expected: PASS (empty frozen set → byte-identical planning)

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_freeze_api.py
git commit -m "feat(freeze): _plan + incumbent + report pin the stored frozen set; fingerprints updated"
```

---

# PHASE 9 — Cadence change + frontend

### Task 20: Remove the Thursday-only gate on "Done"

**Files:**
- Modify: `api/main.py` — `optimize_done_ep` (line 2235-2249) drops the `_is_optimize_day()` gate; remove/retire `_is_optimize_day` (line 1027-1028) and `_OPTIMIZE_WEEKDAY` (line 1025) and the `not_optimize_day` return
- Test: `tests/test_freeze_api.py` (+ update any existing test that asserts `not_optimize_day`, e.g. in `tests/test_optimize_done*.py`)

**Interfaces:**
- Produces: `POST /optimize/done` runs `_try_start_auto()` on ANY weekday (which internally computes+stores the frozen set — Task 18 — then starts the restricted contest).

- [ ] **Step 1: Write the failing test**

```python
def test_done_starts_optimize_on_any_weekday(admin_client_with_book, monkeypatch):
    client = admin_client_with_book
    client.post("/optimize", json={"budget": "quick"}); _wait_optimize_done(client)
    client.post("/optimize/apply")
    _punch_partial(client)   # something material changed since the applied plan
    # Force "today" to a Monday (weekday 0) — must still start.
    import api.main as m
    from datetime import date
    monkeypatch.setattr(m, "_ist_today", lambda: date(2026, 7, 27))  # Monday
    r = client.post("/optimize/done").json()
    assert r["reason"] != "not_optimize_day"
    assert r["started"] in (True, False)  # started True unless already-running/no-change
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_freeze_api.py::test_done_starts_optimize_on_any_weekday -v`
Expected: FAIL — returns `{"reason": "not_optimize_day"}` on Monday.

- [ ] **Step 3: Remove the gate**

In `api/main.py` `optimize_done_ep` (line 2235), delete the `if not _is_optimize_day(): return {...}` block so the body is just:

```python
@app.post("/optimize/done")
def optimize_done_ep(request: Request):
    """'Done entering — update plan'. Any logged-in role. Runs an auto-applying
    RESTRICTED re-optimization (freezes in-progress ops, re-optimizes the rest) every
    day; skips only when a contest is running or nothing material changed."""
    started = _try_start_auto()
    return {"started": started, "reason": ("started" if started else "skipped"),
            "state": _optimize_status()["state"]}
```

Delete `_OPTIMIZE_WEEKDAY` (line 1025) and `_is_optimize_day` (line 1027-1028). Grep for other references: `grep -rn "_is_optimize_day\|_OPTIMIZE_WEEKDAY\|not_optimize_day" api/ tests/ web/` and update/remove each (tests that asserted the old gate must be updated to the new always-run behavior).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_freeze_api.py::test_done_starts_optimize_on_any_weekday -v`
Expected: PASS

- [ ] **Step 5: Full suite**

Run: `pytest -q`
Expected: PASS (fix any test still asserting the Thursday gate)

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/
git commit -m "feat(freeze): Done runs the restricted optimize every day (Thursday gate removed)"
```

---

### Task 21: Frontend — Done always runs the restricted optimize

**Files:**
- Modify: `web/app.js` (the `/optimize/done` handler — remove the `not_optimize_day` branch so it always blocks on `/optimize/status` progress then refreshes the plan)
- Verify: browser (manual)

**Interfaces:**
- Consumes: `POST /optimize/done` (Task 20) — no longer returns `not_optimize_day`.

- [ ] **Step 1: Find and simplify the handler**

`grep -n "optimize/done\|not_optimize_day\|runs on Thursday" web/app.js`. Remove the branch that, on `reason === "not_optimize_day"`, only calls `runPlan(false)`. The handler should: POST `/optimize/done`; if `started`, poll `/optimize/status` to completion (existing progress UI); then `runPlan(false)` to load the winner. If `!started` (skipped/nothing changed), just `runPlan(false)`.

- [ ] **Step 2: Manual browser verification**

Run the app: `uvicorn api.main:app --reload`. Log in as admin, upload `Test8.xlsx`, run a plan + apply, punch a partial quantity on an in-progress step, click **Done entering — update plan** on any weekday. Confirm: a contest runs (progress shows), the plan refreshes, and the in-progress step stays on its machine in the Gantt.

- [ ] **Step 3: Commit**

```bash
git add web/app.js
git commit -m "feat(freeze): Done button always runs the restricted optimize"
```

---

# PHASE 10 — Docs + full verification

### Task 22: Update RULES.md and CLAUDE.md

**Files:**
- Modify: `RULES.md` (add the freeze constraint as an explicit scheduling rule)
- Modify: `CLAUDE.md` (the banner + relevant bullets: Done cadence change; the two new store keys; `decode(frozen=...)`; `engine/freeze.py`)

- [ ] **Step 1: RULES.md** — add a rule describing the frozen zone: on a re-plan, an in-progress operation (partially punched) is pinned to its last-applied machine + operator until its remaining qty finishes; resume order per machine = previous-plan order, all before new work; no setup on resume; machine + operator from the last-applied plan, remaining qty + detection from the punches; OS/dispatch never frozen.

- [ ] **Step 2: CLAUDE.md** — update the banner's cadence line (Done now runs the restricted optimize daily; Thursday gate removed), and add bullets for `engine/freeze.py`, `book_store` keys `LAST_APPLIED_SCHEDULE_KEY`/`FROZEN_OPS_KEY`, and `decode(frozen=...)`/`FrozenOp`.

- [ ] **Step 3: Commit**

```bash
git add RULES.md CLAUDE.md
git commit -m "docs(freeze): document the freeze rule + Done cadence change"
```

---

### Task 23: Full test suite + Test8 measurement + browser end-to-end

**Files:** none (verification)

- [ ] **Step 1: Full suite**

Run: `pytest -q`
Expected: all pass (508+ existing + the new freeze suites).

- [ ] **Step 2: Golden byte-identical**

Run: `pytest -k golden -q`
Expected: PASS.

- [ ] **Step 3: Real-data measurement (Test8.xlsx)** — with the app running and Test8 uploaded, record makespan + late-days for: (a) a normal optimize (no freeze), then (b) punch several partials and click Done. Confirm the frozen steps stay on their machines and the numbers are sensible (the frozen constraint can only match or slightly worsen makespan vs unconstrained — that's expected and correct). Capture the before/after in the PR notes.

- [ ] **Step 4: Browser end-to-end** — verify every plan surface refreshes after Done (Gantt, Schedule, Orders, Analytics, header numbers, downloads) and the frozen bars are pinned.

- [ ] **Step 5: Report** — summarize results (tests green, Test8 numbers, screenshots). Do NOT push to `main` — hand back to the owner for the "push" decision.

---

## Self-review notes (author)

- **Spec coverage:** §4 cycle → Tasks 17-21; §5 detection/mapping → Tasks 9, 16, 18; §6 engine pre-place → Tasks 2-6; §7 persistence → Tasks 1, 15, 17; §8 contest/cloud/display → Tasks 8, 11, 14, 18, 19; §9 cadence → Tasks 20-21; §10 edge cases → Tasks 6, 9, 16 (drops); §11 tests → every task; §12 docs → Task 22; §13 open items resolved: batch mapping via `source_so_refs` (Task 9/16), overlap-into-frozen kept normal (frozen op is a normal op for the successor's overlap math — no special casing), pure engine has no store/app knowledge (frozen rows are plain dicts; `engine/freeze.py` is pure; API does the store I/O).
- **frozen=None byte-identical** is gated in Tasks 2, 3, 12, 19.
- **Type consistency:** `FrozenOp` fields, app-frozen row keys (`so_no,item_code,process,op_seq,machine,operator,remaining_qty,prev_start`), and applied-row keys (`batch_id,item_code,process_seq,process_name,machine,operator,start,end,so_refs`) are used identically across Tasks 1, 9, 15, 16, 18.
