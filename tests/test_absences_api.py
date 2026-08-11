"""GET/POST /absences + DELETE /absences/{id}: role-gated CRUD over operator
absences (Task 12's book_store.load/save/delete_absence), plus orphan
reporting when an absence names an operator no longer in the masters
(ABSENT_OPERATOR_UNKNOWN, non-blocking, surfaces in _report_for_book)."""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, orderbook
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


def _admin_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _user_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech_user",
                           "password": "anvitech12345678"})
    return c


# --- role gating ------------------------------------------------------ #
def test_user_cannot_post_but_can_get(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    user = _user_client(m)

    r = user.post("/absences", json={"operator": "Operator One",
                                     "from_date": "2025-03-10",
                                     "to_date": "2025-03-12"})
    assert r.status_code == 403

    r = user.get("/absences")
    assert r.status_code == 200
    body = r.json()
    assert body["absences"] == []
    assert body["orphans"] == []
    assert "Operator One" in body["operators"]


# --- admin CRUD happy path --------------------------------------------- #
def test_admin_crud_happy_path(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)

    r = admin.post("/absences", json={"operator": "Operator One",
                                      "from_date": "2025-03-10",
                                      "to_date": "2025-03-12"})
    assert r.status_code == 200
    absence = r.json()["absence"]
    assert absence["operator"] == "Operator One"
    assert absence["from_date"] == "2025-03-10"
    assert absence["to_date"] == "2025-03-12"
    assert "id" in absence

    r = admin.get("/absences")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["absences"]]
    assert absence["id"] in ids

    r = admin.delete(f"/absences/{absence['id']}")
    assert r.status_code == 200 and r.json() == {"deleted": True}

    r = admin.get("/absences")
    assert r.json()["absences"] == []

    # deleting again (unknown id) → 404
    r = admin.delete(f"/absences/{absence['id']}")
    assert r.status_code == 404


def test_delete_unknown_id_is_404(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.delete("/absences/not-a-real-id")
    assert r.status_code == 404


# --- validation --------------------------------------------------------- #
def test_unknown_operator_is_400(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.post("/absences", json={"operator": "Nobody Real",
                                      "from_date": "2025-03-10",
                                      "to_date": "2025-03-12"})
    assert r.status_code == 400


def test_bad_date_is_400(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.post("/absences", json={"operator": "Operator One",
                                      "from_date": "not-a-date",
                                      "to_date": "2025-03-12"})
    assert r.status_code == 400


def test_swapped_dates_are_accepted_and_normalized(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.post("/absences", json={"operator": "Operator One",
                                      "from_date": "2025-03-12",
                                      "to_date": "2025-03-10"})
    assert r.status_code == 200
    absence = r.json()["absence"]
    assert absence["from_date"] == "2025-03-10"
    assert absence["to_date"] == "2025-03-12"


# --- orphan reporting ---------------------------------------------------- #
def test_orphan_absence_lists_and_reports(monkeypatch):
    m = _api(); _seed_book()
    book_store.save_absence({"operator": "Ghost Operator",
                             "from_date": "2025-03-10",
                             "to_date": "2025-03-12"})
    admin = _admin_client(m)

    r = admin.get("/absences")
    assert r.status_code == 200
    assert "Ghost Operator" in r.json()["orphans"]

    masters = m._current_masters()
    so_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                         book_store.load_actuals(), masters)
    report = m._report_for_book(masters, so_lines)
    cols = report["columns"]
    kinds_refs = [(dict(zip(cols, row))["Kind"], dict(zip(cols, row))["Reference"])
                 for row in report["rows"]]
    assert ("ABSENT_OPERATOR_UNKNOWN", "Ghost Operator") in kinds_refs


# --- no event trigger (scheduled-optimize design, 2026-07-18) ------------ #
def test_post_and_delete_do_not_start_a_contest(monkeypatch):
    """Absence changes no longer start an auto contest — the twice-weekly
    scheduled run picks them up via the book fingerprint instead."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api(); _seed_book()
    admin = _admin_client(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))

    r = admin.post("/absences", json={"operator": "Operator One",
                                      "from_date": "2025-03-10",
                                      "to_date": "2025-03-12"})
    absence_id = r.json()["absence"]["id"]
    assert not starts

    admin.delete(f"/absences/{absence_id}")
    assert not starts
