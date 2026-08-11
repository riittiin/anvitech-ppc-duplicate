"""POST /actuals + /actuals/rollback — the feedback-precedence guardrail
(2026-07-25 spec): a process's recorded qty can't exceed the good qty that cleared
the process before it; rollback can't retro-create the illegal state."""
import importlib
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A   # BANDSAW -> CNC OS -> INSP

OP = "Operator One"


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _seed(m):
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()


def _procs(m):
    return [p.name for p in m._current_masters().routings[ITEM_A].processes]


def _punch(c, proc, produced, rejected=0):
    return c.post("/actuals", json={
        "so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": produced, "qty_rejected": rejected,
        "shift": "1st shift", "process": proc, "operator": OP})


def test_downstream_before_upstream_is_rejected():
    m = _api(); _seed(m); c = _client(m)
    p = _procs(m)
    r = _punch(c, p[1], 10)                       # punch step 2 with step 1 unrecorded
    assert r.status_code == 400
    assert p[0] in r.json()["detail"]


def test_upstream_then_downstream_within_cap_ok_but_over_cap_rejected():
    m = _api(); _seed(m); c = _client(m)
    p = _procs(m)
    assert _punch(c, p[0], 20).status_code == 200          # BANDSAW 20 good
    assert _punch(c, p[1], 20).status_code == 200          # CNC OS 20 == cap
    assert _punch(c, p[1], 1).status_code == 400           # 21 > 20 -> rejected


def test_first_process_over_ordered_qty_rejected():
    m = _api(); _seed(m); c = _client(m)
    p = _procs(m)
    assert _punch(c, p[0], 41).status_code == 400          # order is only 40


def test_rollback_upstream_blocked_while_downstream_recorded():
    m = _api(); _seed(m); c = _client(m)
    p = _procs(m)
    _punch(c, p[0], 20)                                     # BANDSAW
    _punch(c, p[1], 20)                                     # CNC OS depends on it
    bandsaw_id = next(a.id for a in book_store.load_actuals() if a.process == p[0])
    r = c.post("/actuals/rollback", json={"id": bandsaw_id})
    assert r.status_code == 400
    assert p[1] in r.json()["detail"]


def test_rollback_downstream_first_is_allowed():
    m = _api(); _seed(m); c = _client(m)
    p = _procs(m)
    _punch(c, p[0], 20)
    _punch(c, p[1], 20)
    cncos_id = next(a.id for a in book_store.load_actuals() if a.process == p[1])
    assert c.post("/actuals/rollback", json={"id": cncos_id}).status_code == 200


def test_completed_by_process_stays_monotonic_along_the_routing():
    """After legal in-order capture the invariant holds: cumulative good is
    non-increasing down the routing (step1 >= step2 >= step3) — no order can hold
    downstream-recorded > upstream-recorded."""
    from engine import orderbook
    m = _api(); _seed(m); c = _client(m)
    p = _procs(m)
    _punch(c, p[0], 30)
    _punch(c, p[1], 25)
    _punch(c, p[2], 20)
    done = orderbook.completed_by_process(book_store.load_actuals())
    vals = [done.get(("SO1", ITEM_A, orderbook._norm(name)), 0.0) for name in p]
    assert vals == sorted(vals, reverse=True), vals
