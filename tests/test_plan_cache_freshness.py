"""The plan cache must never serve a stale ORDER BOOK (live bug, 2026-08-08).

A director marked three (SO#, item) lines complete in the office; the owner at home
refreshed 20 times and still saw them as not complete. Root cause: `_plan_fingerprint`
hashed the book via `_current_book_sig()`, which is built from `active_so_lines` — and
that SKIPS any order with nothing left to make (`remaining <= 0`). An order you mark
complete is normally already fully produced, so archiving it changed NOTHING in the
fingerprint, and `_PLAN_CACHE` kept serving the pre-completion response — which carries
the Orders tab table (built from active PLUS completed) and the Rule 8 tab.

The plan itself was right either way (a fully-produced order contributes no work); only
what the two tabs DISPLAYED was stale. So the fingerprint must cover the whole order
book as DISPLAYED, not just the lines the planner schedules.
"""
import importlib
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, orderbook
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _client(m, user="anvitech", pw="1930rail"):
    c = TestClient(m.app)
    c.post("/login", data={"username": user, "password": pw})
    return c


def _seed_with_one_finished_order(m):
    """Two active orders; SO1 punched to its FULL qty at every routing step, so its
    remaining is 0 and it has already dropped out of `active_so_lines` — the exact
    state an order is in when someone marks it complete."""
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20)),
        Order("SO2", ITEM_A, ITEM_A, 25, date(2025, 3, 25)),
    ])
    masters = m._current_masters()
    c = _client(m)
    op = sorted({o.name for o in masters.operators})[0]
    for p in masters.routings[ITEM_A].processes:
        r = c.post("/actuals", json={
            "so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
            "entry_date": "2025-03-10", "process": p.name,
            "qty_produced": 40, "qty_rejected": 0, "operator": op,
            "shift": "1st shift", "machine": "", "downtime_min": 0, "remarks": "",
        })
        assert r.status_code == 200, r.text
    # Precondition: the planner already cannot see SO1 — that is what made the
    # fingerprint blind to it.
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      book_store.load_actuals(), masters)
    assert not any(l.so_no == "SO1" for l in lines)
    return c


def _status(table, so_no):
    cols = table["columns"]
    i_so, i_st = cols.index("SO No"), cols.index("Status")
    return [row[i_st] for row in table["rows"] if row[i_so] == so_no]


def _so_nos(table):
    i_so = table["columns"].index("SO No")
    return [row[i_so] for row in table["rows"]]


def test_archiving_a_finished_order_changes_the_plan_fingerprint():
    m = _api()
    _seed_with_one_finished_order(m)
    cfg = m._load_plan_config()
    before = m._plan_fingerprint(cfg)
    assert book_store.complete_order("SO1", ITEM_A)
    assert m._plan_fingerprint(cfg) != before


def test_a_second_admin_sees_a_completion_on_the_next_plan():
    """The live bug, end to end: one browser completes the order, ANOTHER browser's
    refresh (POST /run, the boot path) must show it as Complete."""
    m = _api()
    director = _seed_with_one_finished_order(m)
    owner = _client(m)

    # The owner has the pre-completion plan loaded (this is what filled the cache).
    assert _status(owner.post("/run", json={"persist": False}).json()["orders"],
                   "SO1") == ["Running"]

    assert director.post("/orders/complete",
                         json={"so_no": "SO1", "item_code": ITEM_A}).status_code == 200

    # Refreshing must show it — every time, not eventually.
    for _ in range(3):
        rows = owner.post("/run", json={"persist": False}).json()["orders"]
        assert _status(rows, "SO1") == ["Complete"]


def test_an_archived_order_drops_off_the_rule_8_tab_on_the_next_plan():
    """Rule 8 lists the ACTIVE book. The cached trace kept showing an archived order."""
    m = _api()
    c = _seed_with_one_finished_order(m)
    assert "SO1" in _so_nos(c.post("/run", json={"persist": False}).json()
                            ["trace"]["rule8"]["output"])

    assert c.post("/orders/complete",
                  json={"so_no": "SO1", "item_code": ITEM_A}).status_code == 200

    r8 = c.post("/run", json={"persist": False}).json()["trace"]["rule8"]["output"]
    assert "SO1" not in _so_nos(r8) and "SO2" in _so_nos(r8)


def test_deleting_a_finished_order_shows_on_the_next_plan():
    """Adjacent case in the same blind spot — this one ALREADY passed before the fix
    (it survives by luck: `delete_orders` also purges the order's actuals, and the
    actuals digest IS in the fingerprint). Pinned so it cannot start failing."""
    m = _api()
    c = _seed_with_one_finished_order(m)
    assert _status(c.post("/run", json={"persist": False}).json()["orders"],
                   "SO1") == ["Running"]

    assert c.post("/orders/delete",
                  json={"orders": [["SO1", ITEM_A]],
                        "password": "1930rail"}).status_code == 200

    rows = c.post("/run", json={"persist": False}).json()["orders"]
    assert "SO1" not in _so_nos(rows) and "SO2" in _so_nos(rows)


def test_the_orders_table_is_rebuilt_even_when_the_plan_is_served_from_cache():
    """Defense in depth for the whole class: whatever the fingerprint covers, the
    Orders table in a cached response must reflect the book as it is NOW. Archive an
    order behind the cache's back (no fingerprint change is possible here, because
    the fingerprint is not consulted again) and the next /run must still be right."""
    m = _api()
    c = _seed_with_one_finished_order(m)
    c.post("/run", json={"persist": False})          # fill the cache

    # Freeze the fingerprint so a cache HIT is guaranteed, then change the book.
    frozen_key = m._PLAN_CACHE["key"]
    m._plan_fingerprint = lambda cfg: frozen_key
    assert book_store.complete_order("SO1", ITEM_A)

    rows = c.post("/run", json={"persist": False}).json()["orders"]
    assert _status(rows, "SO1") == ["Complete"]
