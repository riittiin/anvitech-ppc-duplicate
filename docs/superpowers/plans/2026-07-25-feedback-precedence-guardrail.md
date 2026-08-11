# Feedback precedence guardrail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject feedback punches (and rollbacks) that violate routing precedence — a process's recorded qty can never exceed the good qty that cleared the process before it.

**Architecture:** Two pure validators in `engine/orderbook.py` (no store I/O), wired into `POST /actuals` (before append) and `POST /actuals/rollback` (before delete) in `api/main.py`. The UI surfaces the 400 via its existing error path. Full design + wiring map: `docs/superpowers/specs/2026-07-25-feedback-precedence-guardrail-design.md`.

**Tech Stack:** Python 3, FastAPI, pytest. No new dependencies.

## Global Constraints

- Validators are **pure** (no `book_store`/store access); the API passes data in — mirrors every other `engine/` rule (project law #1).
- Reuse `orderbook._norm` and the `completed_by_process` accounting (produced / good) — do NOT define a second normalization or accounting, or capture and planning will disagree.
- "Recorded at a process" = cumulative `qty_produced`; "cleared a process" = cumulative `qty_produced − qty_rejected` clamped ≥ 0.
- Hard block (HTTP 400), never warn-and-allow. Error messages name the blocking step.
- Run `pytest` (full suite, ~571 tests) green before each commit; golden trace must stay byte-identical (this feature touches no scheduling path).

---

## File Structure

- `engine/orderbook.py` — add `precedence_cap_error(...)` and `rollback_cap_error(...)` (pure).
- `api/main.py` — call the validators in `post_actuals` and `rollback_actual`.
- `tests/test_orderbook.py` — unit tests for the two validators.
- `tests/test_actuals_api.py` (new) — endpoint 400 tests for capture + rollback.
- `docs/…/specs/2026-07-25-feedback-precedence-guardrail-design.md` — already written.
- `CLAUDE.md` — one-line note under the `engine/orderbook.py` map entry.

---

### Task 1: Pure capture validator `precedence_cap_error`

**Files:**
- Modify: `engine/orderbook.py`
- Test: `tests/test_orderbook.py`

**Interfaces:**
- Produces: `precedence_cap_error(actuals, so_no, item_code, punched_process, routing, ordered_qty) -> str | None`
- Consumes: existing `orderbook._norm`, `Process.seq`, `Routing.processes`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_orderbook.py
from engine import orderbook
from engine.models import Actual, Process, Routing

def _routing():
    return Routing(item_code="X", description="", customer="", rm_type="", moq=None,
                   processes=[Process(1, "CNC FIRST SIDE", 5, 5, "CNC1", "CNC1"),
                              Process(2, "VMC FIRST SIDE", 6, 6, "VMC1", "VMC1")])

def _act(proc, prod, rej=0):
    return Actual(so_no="S1", item_code="X", entry_date=__import__("datetime").date(2025,3,1),
                  qty_produced=prod, qty_rejected=rej, process=proc, item_name="X", operator="o")

def test_cannot_punch_downstream_before_upstream():
    # No CNC recorded; a VMC punch must be rejected (cap 0).
    err = orderbook.precedence_cap_error([_act("VMC FIRST SIDE", 10)],
                                         "S1", "X", "VMC FIRST SIDE", _routing(), 40)
    assert err and "CNC FIRST SIDE" in err

def test_downstream_capped_at_upstream_good():
    # CNC good = 20; VMC of 20 ok, VMC of 21 rejected.
    acts_ok = [_act("CNC FIRST SIDE", 20), _act("VMC FIRST SIDE", 20)]
    assert orderbook.precedence_cap_error(acts_ok, "S1", "X", "VMC FIRST SIDE", _routing(), 40) is None
    acts_bad = [_act("CNC FIRST SIDE", 20), _act("VMC FIRST SIDE", 21)]
    assert orderbook.precedence_cap_error(acts_bad, "S1", "X", "VMC FIRST SIDE", _routing(), 40) is not None

def test_first_process_capped_at_ordered_qty():
    assert orderbook.precedence_cap_error([_act("CNC FIRST SIDE", 41)],
                                          "S1", "X", "CNC FIRST SIDE", _routing(), 40) is not None
    assert orderbook.precedence_cap_error([_act("CNC FIRST SIDE", 40)],
                                          "S1", "X", "CNC FIRST SIDE", _routing(), 40) is None

def test_rejects_count_as_consumed_upstream_good_only():
    # CNC produced 25 rejected 5 -> good 20. VMC produced 20 ok; 21 rejected.
    base = [_act("CNC FIRST SIDE", 25, 5)]
    assert orderbook.precedence_cap_error(base + [_act("VMC FIRST SIDE", 20)],
                                          "S1", "X", "VMC FIRST SIDE", _routing(), 40) is None
    assert orderbook.precedence_cap_error(base + [_act("VMC FIRST SIDE", 21)],
                                          "S1", "X", "VMC FIRST SIDE", _routing(), 40) is not None

def test_no_routing_or_unknown_process_allows():
    assert orderbook.precedence_cap_error([_act("CNC FIRST SIDE", 99)], "S1", "X", "CNC FIRST SIDE", None, 40) is None
    assert orderbook.precedence_cap_error([_act("MYSTERY", 99)], "S1", "X", "MYSTERY", _routing(), 40) is None

def test_other_orders_do_not_interfere():
    # An actual for a different SO/item is ignored.
    other = Actual(so_no="S2", item_code="X", entry_date=__import__("datetime").date(2025,3,1),
                   qty_produced=100, qty_rejected=0, process="CNC FIRST SIDE", item_name="X", operator="o")
    assert orderbook.precedence_cap_error([other, _act("CNC FIRST SIDE", 40)],
                                          "S1", "X", "CNC FIRST SIDE", _routing(), 40) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_orderbook.py -k "precedence or cap or downstream or upstream or first_process or rejects or no_routing or other_orders" -q`
Expected: FAIL — `AttributeError: module 'engine.orderbook' has no attribute 'precedence_cap_error'`

- [ ] **Step 3: Implement `precedence_cap_error`**

```python
# engine/orderbook.py
def precedence_cap_error(actuals, so_no, item_code, punched_process,
                         routing, ordered_qty):
    """Piece-flow guard: the cumulative produced at `punched_process` must not exceed
    the good qty that cleared the process before it (or `ordered_qty` for the first
    process). Returns an error message, or None if allowed. Pure — `actuals` is the
    full list; only this order's entries are considered."""
    if routing is None:
        return None
    procs = sorted(routing.processes, key=lambda p: p.seq)
    names = [_norm(p.name) for p in procs]
    tgt = _norm(punched_process)
    if tgt not in names:
        return None
    idx = names.index(tgt)
    produced, good = {}, {}
    for a in actuals:
        if a.so_no == so_no and a.item_code == item_code:
            n = _norm(a.process)
            produced[n] = produced.get(n, 0.0) + (a.qty_produced or 0.0)
            good[n] = good.get(n, 0.0) + ((a.qty_produced or 0.0) - (a.qty_rejected or 0.0))
    got = produced.get(tgt, 0.0)
    if idx == 0:
        if got > ordered_qty:
            return (f"Can't record {got:g} at '{procs[0].name}' — the order is only for "
                    f"{ordered_qty:g} pieces.")
        return None
    prev = procs[idx - 1]
    cap = max(good.get(names[idx - 1], 0.0), 0.0)
    if got > cap:
        return (f"Can't record {got:g} at '{procs[idx].name}' — only {cap:g} pieces have "
                f"cleared the previous step '{prev.name}'. Record '{prev.name}' first.")
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_orderbook.py -q`
Expected: PASS (all, incl. existing).

- [ ] **Step 5: Commit**

```bash
git add engine/orderbook.py tests/test_orderbook.py
git commit -m "feat(feedback): pure precedence cap validator for per-process punches"
```

---

### Task 2: Pure rollback validator `rollback_cap_error`

**Files:**
- Modify: `engine/orderbook.py`
- Test: `tests/test_orderbook.py`

**Interfaces:**
- Produces: `rollback_cap_error(actuals_after_removal, removed, routing) -> str | None`

- [ ] **Step 1: Write failing tests**

```python
def test_rollback_blocked_when_downstream_depends_on_it():
    # CNC good 20, VMC produced 20. Rolling back a CNC-10 entry -> CNC good 10 < VMC 20 -> block.
    after = [_act("CNC FIRST SIDE", 10), _act("VMC FIRST SIDE", 20)]   # the other CNC-10 already removed
    removed = _act("CNC FIRST SIDE", 10)
    err = orderbook.rollback_cap_error(after, removed, _routing())
    assert err and "VMC FIRST SIDE" in err

def test_rollback_allowed_when_no_downstream_dependency():
    after = [_act("CNC FIRST SIDE", 10)]           # VMC has nothing
    removed = _act("CNC FIRST SIDE", 10)
    assert orderbook.rollback_cap_error(after, removed, _routing()) is None

def test_rollback_of_last_process_always_allowed():
    after = [_act("CNC FIRST SIDE", 20), _act("VMC FIRST SIDE", 10)]
    removed = _act("VMC FIRST SIDE", 10)           # VMC is terminal-ish; nothing after it
    assert orderbook.rollback_cap_error(after, removed, _routing()) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_orderbook.py -k rollback -q`
Expected: FAIL — `precedence`... `rollback_cap_error` undefined.

- [ ] **Step 3: Implement `rollback_cap_error`**

```python
def rollback_cap_error(actuals_after_removal, removed, routing):
    """After removing `removed`, its process's good qty must still be >= the produced
    qty already recorded at the immediately-following process. Returns a message or
    None. Pure."""
    if routing is None:
        return None
    procs = sorted(routing.processes, key=lambda p: p.seq)
    names = [_norm(p.name) for p in procs]
    tgt = _norm(removed.process)
    if tgt not in names:
        return None
    idx = names.index(tgt)
    if idx + 1 >= len(procs):
        return None
    produced, good = {}, {}
    for a in actuals_after_removal:
        if a.so_no == removed.so_no and a.item_code == removed.item_code:
            n = _norm(a.process)
            produced[n] = produced.get(n, 0.0) + (a.qty_produced or 0.0)
            good[n] = good.get(n, 0.0) + ((a.qty_produced or 0.0) - (a.qty_rejected or 0.0))
    succ = procs[idx + 1]
    if produced.get(names[idx + 1], 0.0) > max(good.get(tgt, 0.0), 0.0):
        return (f"Can't roll back this '{procs[idx].name}' entry — "
                f"{produced.get(names[idx + 1], 0.0):g} pieces are already recorded at the later "
                f"step '{succ.name}'. Roll back '{succ.name}' first.")
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_orderbook.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/orderbook.py tests/test_orderbook.py
git commit -m "feat(feedback): pure rollback precedence validator"
```

---

### Task 3: Wire the capture guard into `POST /actuals`

**Files:**
- Modify: `api/main.py` (`post_actuals`, immediately before `all_actuals = r7.run(actual)`)
- Test: `tests/test_actuals_api.py` (create)

**Interfaces:**
- Consumes: `orderbook.precedence_cap_error`, `_current_masters().routings`, `book_store.load_active_orders()`, `book_store.load_actuals()`.

- [ ] **Step 1: Write failing endpoint test**

```python
# tests/test_actuals_api.py
import importlib
from datetime import date
import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A   # ITEM_A routing: multi-step

def _api():
    import api.main as m; importlib.reload(m); return m

def _client(m):
    c = TestClient(m.app); c.post("/login", data={"username": "anvitech", "password": "1930rail"}); return c

def _seed(m):
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()

def _procs(m):
    return [p.name for p in m._current_masters().routings[ITEM_A].processes]

def test_downstream_before_upstream_is_400(monkeypatch):
    m = _api(); _seed(m); c = _client(m)
    procs = _procs(m)                       # procs[0] upstream, procs[1] downstream
    r = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 10, "qty_rejected": 0,
        "shift": "1st shift", "process": procs[1], "operator": "Operator One"})
    assert r.status_code == 400 and procs[0] in r.json()["detail"]

def test_upstream_then_downstream_within_cap_ok(monkeypatch):
    m = _api(); _seed(m); c = _client(m)
    procs = _procs(m)
    ok1 = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 20, "qty_rejected": 0,
        "shift": "1st shift", "process": procs[0], "operator": "Operator One"})
    assert ok1.status_code == 200
    ok2 = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 20, "qty_rejected": 0,
        "shift": "1st shift", "process": procs[1], "operator": "Operator One"})
    assert ok2.status_code == 200
    bad = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 1, "qty_rejected": 0,
        "shift": "1st shift", "process": procs[1], "operator": "Operator One"})
    assert bad.status_code == 400              # VMC would exceed CNC's 20
```

*(If `ITEM_A` in `tests/sample_workbook` has <2 processes, use the item that does, or the new-engine sample — check `masters.routings[ITEM_A].processes` first.)*

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_actuals_api.py -q`
Expected: FAIL — the out-of-order punch returns 200 (no guard yet).

- [ ] **Step 3: Wire the guard in `post_actuals`**

Insert immediately before `all_actuals = r7.run(actual)`:

```python
    _routing = _current_masters().routings.get(req.item_code)
    _order = book_store.load_active_orders().get((req.so_no, req.item_code))
    _ordered = _order.ordered_qty if _order else float("inf")
    _err = orderbook.precedence_cap_error(
        book_store.load_actuals() + [actual], req.so_no, req.item_code,
        req.process, _routing, _ordered)
    if _err:
        raise HTTPException(status_code=400, detail=_err)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_actuals_api.py -q && pytest -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_actuals_api.py
git commit -m "feat(feedback): reject out-of-order / over-cap punches at POST /actuals"
```

---

### Task 4: Wire the rollback guard + docs + permanent invariant

**Files:**
- Modify: `api/main.py` (`rollback_actual`, before `book_store.delete_actual`)
- Modify: `tests/test_actuals_api.py`
- Modify: `CLAUDE.md` (orderbook map entry) + confirm the spec is committed
- Create: `tests/test_feedback_precedence.py` (promote the dogfood invariant)

- [ ] **Step 1: Write failing rollback endpoint test**

```python
def test_rollback_blocked_when_downstream_recorded(monkeypatch):
    m = _api(); _seed(m); c = _client(m)
    procs = _procs(m)
    up = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 20, "qty_rejected": 0,
        "shift": "1st shift", "process": procs[0], "operator": "Operator One"}).json()
    c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 20, "qty_rejected": 0,
        "shift": "1st shift", "process": procs[1], "operator": "Operator One"})
    up_id = up["visible"][0]["id"] if "visible" in up else book_store.load_actuals()[0].id
    # roll back the CNC entry while VMC=20 depends on it -> 400
    r = c.post("/actuals/rollback", json={"id": book_store.load_actuals()[0].id})
    assert r.status_code == 400 and procs[1] in r.json()["detail"]
```

*(Adjust id-selection to whichever entry is the upstream one; the point is: rolling back upstream while downstream recorded → 400.)*

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_actuals_api.py -k rollback -q`
Expected: FAIL — rollback returns 200.

- [ ] **Step 3: Wire the guard in `rollback_actual`**

Insert after `target` is found and the "latest day" check, before `book_store.delete_actual`:

```python
    _routing = _current_masters().routings.get(target.item_code)
    _err = orderbook.rollback_cap_error(
        [a for a in before if a.id != req.id], target, _routing)
    if _err:
        raise HTTPException(status_code=400, detail=_err)
```

- [ ] **Step 4: Promote the permanent invariant test**

```python
# tests/test_feedback_precedence.py — an order can never hold downstream > upstream.
# Drive POST /actuals through a legal sequence, then assert every attempt to break the
# chain is 400 and completed_by_process stays monotonic non-increasing along seq order.
```
(Mirror the harness check: after a series of legal punches, assert for every order that
`completed_by_process` is non-increasing along `Routing.processes` seq order.)

- [ ] **Step 5: Docs**

- Add to `CLAUDE.md` under the `engine/orderbook.py` bullet:
  `precedence_cap_error`/`rollback_cap_error` — piece-flow guard: a process's recorded
  qty can't exceed the previous step's good (first step capped at ordered); enforced at
  `POST /actuals` + `/actuals/rollback` (see the 2026-07-25 spec).

- [ ] **Step 6: Run full suite + commit**

Run: `pytest -q`
Expected: PASS (all, golden byte-identical).

```bash
git add api/main.py tests/test_actuals_api.py tests/test_feedback_precedence.py CLAUDE.md docs/superpowers/
git commit -m "feat(feedback): rollback guard + permanent precedence invariant + docs"
```

---

## Self-Review

- **Spec coverage:** order-enforce (Task 1 test 1), cap (Task 1 test 2), first-process cap (Task 1 test 3), rejects (Task 1 test 4), no-routing/unknown (Task 1 test 5), rollback (Task 2 + Task 4), capture wiring (Task 3), rollback wiring (Task 4), UI 400 surfacing (existing path — no code, noted), cache (no change — noted), permanent invariant (Task 4). ✅
- **Types:** `precedence_cap_error(actuals, so_no, item_code, punched_process, routing, ordered_qty)` and `rollback_cap_error(actuals_after_removal, removed, routing)` used identically in tasks and call sites. ✅
- **No placeholders:** all steps contain real code/commands. The only judgement call flagged is "pick the item whose routing has ≥2 processes" in Task 3 — verify against `sample_workbook` at execution time. ✅
