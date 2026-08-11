"""Outsourced (OS) steps need no operator at Capture Actuals (owner rule, 2026-07-26).

STRICT signal (owner-chosen): a process is outsourced iff its ALLOTTED machine
resource is the sentinel ``OS``. The Suggested cell and an 'OS' in the process NAME
alone do NOT count — a step named 'CNC OS' but allotted a real machine is in-house.
"""
import importlib
from datetime import date

import pytest

from engine import orderbook
from engine.models import Routing, Process


def _routing(*procs):
    return Routing(item_code="X", description="", customer="", rm_type="",
                   moq=None, processes=list(procs))


# ---- pure helper: orderbook.process_is_outsourced -------------------------- #

def test_allotted_os_is_outsourced():
    r = _routing(Process(1, "ROUGH MACHINING OS", None, None, None, "OS"))
    assert orderbook.process_is_outsourced(r, "ROUGH MACHINING OS") is True


def test_allotted_real_machine_is_not_outsourced():
    r = _routing(Process(1, "CNC FIRST SIDE", 10, 10, "CNC1", "CNC1"))
    assert orderbook.process_is_outsourced(r, "CNC FIRST SIDE") is False


def test_os_only_in_name_but_blank_allotted_is_not_outsourced():
    # The 9 real-data steps named '... OS' with a blank Allotted cell: strict rule
    # does NOT exempt them (owner will fix the Excel).
    r = _routing(Process(1, "ROUGH MACHINING OS", None, None, None, None))
    assert orderbook.process_is_outsourced(r, "ROUGH MACHINING OS") is False


def test_os_only_in_suggested_is_not_outsourced():
    r = _routing(Process(1, "THREAD OS", None, None, "OS", None))
    assert orderbook.process_is_outsourced(r, "THREAD OS") is False


def test_allotted_os_wins_even_when_name_is_not_os():
    # Real case: item 744411101 has a step named 'VMC' with Allotted = OS.
    r = _routing(Process(1, "VMC", None, None, None, "OS"))
    assert orderbook.process_is_outsourced(r, "VMC") is True


def test_name_matched_case_and_whitespace_insensitively():
    r = _routing(Process(1, "CNC OS", None, None, None, "OS"))
    assert orderbook.process_is_outsourced(r, "cnc  os") is True


def test_unknown_routing_or_process_is_not_outsourced():
    assert orderbook.process_is_outsourced(None, "X") is False
    r = _routing(Process(1, "CNC", 10, 10, "CNC1", "CNC1"))
    assert orderbook.process_is_outsourced(r, "NOPE") is False


# ---- endpoint: POST /actuals + GET /items --------------------------------- #

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_workbook, ITEM_A


def _os_variant_bytes():
    """The sample workbook, with ITEM_A's Process 2 ('CNC OS') Allotted M/c set to OS
    so it becomes a true outsourced step (header col 21 = 'Process 2 Allotted M/c',
    0-based -> openpyxl column 22; ITEM_A is data row 3)."""
    import io
    wb = build_workbook()
    wb["Item's process Master"].cell(row=3, column=22, value="OS")
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _seed(m):
    book_store.save_masters_bytes(_os_variant_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()


def _punch(c, proc, operator, produced=10):
    return c.post("/actuals", json={
        "so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": produced, "qty_rejected": 0,
        "shift": "1st shift", "process": proc, "operator": operator})


def test_os_step_saves_with_blank_operator():
    m = _api(); _seed(m); c = _client(m)
    procs = [p.name for p in m._current_masters().routings[ITEM_A].processes]
    assert _punch(c, procs[0], "Operator One").status_code == 200   # BANDSAW (in-house) first
    r = _punch(c, procs[1], "")                                     # CNC OS (Allotted=OS): no operator
    assert r.status_code == 200, r.text


def test_non_os_step_still_requires_operator():
    m = _api(); _seed(m); c = _client(m)
    procs = [p.name for p in m._current_masters().routings[ITEM_A].processes]
    r = _punch(c, procs[0], "")                                     # BANDSAW (in-house): operator required
    assert r.status_code == 400
    assert "please pick an operator" in r.json()["detail"]


def test_items_lists_os_processes():
    m = _api(); _seed(m); c = _client(m)
    data = c.get("/items").json()
    meta = data["items"][ITEM_A]
    assert "CNC OS" in meta["os_processes"]
    assert "BANDSAW" not in meta["os_processes"]
