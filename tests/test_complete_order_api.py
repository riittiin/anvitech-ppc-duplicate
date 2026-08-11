"""Standalone 'Mark this SO+item complete' (owner, 2026-07-28): needs only SO No + item
code — NO operator, no production punch — archives the order, and KEEPS the production
records so reports/efficiency still count them."""
import importlib
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _seed(m):
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()


def _client(m, user="anvitech", pw="1930rail"):
    c = TestClient(m.app)
    c.post("/login", data={"username": user, "password": pw})
    return c


def test_mark_complete_needs_only_so_and_item_no_operator():
    m = _api(); _seed(m); c = _client(m)
    assert ("SO1", ITEM_A) in book_store.load_active_orders()
    r = c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert r.status_code == 200 and r.json().get("completed") is True
    assert ("SO1", ITEM_A) not in book_store.load_active_orders()
    assert ("SO1", ITEM_A) in book_store.load_completed_orders()


def test_mark_complete_available_to_the_user_role():
    m = _api(); _seed(m); c = _client(m, "anvitech_user", "anvitech12345678")
    r = c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert r.status_code == 200


def test_mark_complete_unknown_order_is_404():
    m = _api(); _seed(m); c = _client(m)
    r = c.post("/orders/complete", json={"so_no": "NOPE", "item_code": "X"})
    assert r.status_code == 404


def test_mark_complete_keeps_the_production_records_for_reporting():
    m = _api(); _seed(m); c = _client(m)
    procs = [p.name for p in m._current_masters().routings[ITEM_A].processes]
    c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
                             "entry_date": "2025-03-10", "qty_produced": 10, "qty_rejected": 0,
                             "shift": "1st shift", "process": procs[0], "operator": "Operator One"})
    n = len(book_store.load_actuals())
    assert n >= 1
    c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert len(book_store.load_actuals()) == n   # marking complete must not delete the records
