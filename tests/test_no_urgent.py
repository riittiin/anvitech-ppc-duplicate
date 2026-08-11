"""The Urgent lane is removed (2026-07-29). Orders become open|committed only;
legacy stored "urgent" rows normalize to "committed" on load.

Covers:
  * `Order.from_json` migrates a stored "urgent" commitment to "committed"
    (promised_date preserved).
  * `POST /orders/urgent` no longer exists (404/405 — the route is gone).
  * `orderbook.split_committed_open` treats "committed" as protected; anything
    else (including a defensively-constructed "urgent") is NOT protected,
    since a live line can never actually be "urgent" post-migration.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import orderbook
from engine.models import Order, SOLine


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _admin_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


# --------------------------------------------------------------------------- #
# Migration: Order.from_json
# --------------------------------------------------------------------------- #
def test_from_json_migrates_urgent_to_committed():
    o = Order.from_json({
        "so_no": "SO1",
        "item_code": "A",
        "item_name": "A",
        "ordered_qty": 10,
        "delivery_date": "2026-07-20",
        "completed": False,
        "first_seen": "",
        "commitment": "urgent",
        "promised_date": "2026-08-05",
        "committed_at": "2026-07-13T09:00:00",
    })
    assert o.commitment == "committed"
    assert o.promised_date == date(2026, 8, 5)


def test_from_json_leaves_open_and_committed_untouched():
    o_open = Order.from_json({
        "so_no": "SO1", "item_code": "A", "item_name": "A", "ordered_qty": 10,
        "delivery_date": "2026-07-20",
    })
    assert o_open.commitment == "open"

    o_committed = Order.from_json({
        "so_no": "SO1", "item_code": "A", "item_name": "A", "ordered_qty": 10,
        "delivery_date": "2026-07-20", "commitment": "committed",
        "promised_date": "2026-07-22",
    })
    assert o_committed.commitment == "committed"
    assert o_committed.promised_date == date(2026, 7, 22)


# --------------------------------------------------------------------------- #
# Endpoint removed
# --------------------------------------------------------------------------- #
def test_orders_urgent_endpoint_is_gone():
    m = _api()
    c = _admin_client(m)
    r = c.post("/orders/urgent", json={"so": "SO1", "item": "A"})
    assert r.status_code in (404, 405)


def test_urgent_request_model_no_longer_exists():
    m = _api()
    assert not hasattr(m, "UrgentRequest")
    assert not hasattr(m, "urgent_order_ep")


# --------------------------------------------------------------------------- #
# orderbook.split_committed_open — two lanes only
# --------------------------------------------------------------------------- #
def test_split_committed_open_two_lanes_only():
    lines = [
        SOLine(so_no="SO1", item_code="A", item_name="A", qty=10,
               delivery_date=date(2026, 7, 20), commitment="committed"),
        SOLine(so_no="SO2", item_code="B", item_name="B", qty=10,
               delivery_date=date(2026, 7, 25), commitment="open"),
    ]
    protected, open_lines = orderbook.split_committed_open(lines)
    assert [l.item_code for l in protected] == ["A"]
    assert [l.item_code for l in open_lines] == ["B"]


def test_split_committed_open_never_protects_a_stray_urgent_value():
    # A live line can never actually carry "urgent" (Order.from_json migrates
    # it on load), but split_committed_open's contract is now a strict
    # equality check against "committed" — anything else, including a
    # defensively-constructed "urgent" string, lands in the open bucket.
    lines = [
        SOLine(so_no="SO1", item_code="A", item_name="A", qty=10,
               delivery_date=date(2026, 7, 20), commitment="urgent"),
    ]
    protected, open_lines = orderbook.split_committed_open(lines)
    assert protected == []
    assert [l.item_code for l in open_lines] == ["A"]
