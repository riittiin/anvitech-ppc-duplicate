"""The Optimize feature's API layer: job lifecycle (start → status → apply/clear),
role gating, single-pass rank replay, and the optimize_meta staleness info on /run.

Job logic is driven through the internal helpers (like the commit-endpoint tests);
HTTP role gating is exercised with the TestClient."""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order
from engine.pipeline import KEY_SEP
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book(n_orders=2):
    book_store.save_masters_bytes(build_sample_bytes())
    items = [ITEM_A, ITEM_B]
    book_store.add_orders([
        Order(f"SO{i+1}", items[i % 2], items[i % 2], 10 + 5 * i, date(2025, 3, 20 + i))
        for i in range(n_orders)])


# --------------------------------------------------------------------------- #
# Job lifecycle (inline execution — background=False keeps tests deterministic)
# --------------------------------------------------------------------------- #
def test_optimize_then_apply_persists_ranks():
    m = _api()
    _seed_book()
    st = m._start_optimize(budget_evals=15, label="quick", background=False)
    assert st["state"] == "done"
    # The advertised budget is the current setting's full-depth floor; the
    # overlap probes + challenger are bounded extras on top (sweep contract).
    # The contest lineup depends on the CURRENT overlap (an off-list setting
    # joins as an extra contender), so mirror the API and pass the NUMERIC
    # current overlap (review-caught: engine.config.OVERLAP_PERCENT is the
    # overlap-MODE string, not a number).
    from engine import optimizer
    from engine.config import Config as _Cfg
    assert st["evals"] <= optimizer.sweep_total_evals(
        15, current_overlap=_Cfg().overlap_percent)
    assert st["baseline"] and st["best"]

    meta = m._optimize_apply()
    saved = book_store.load_plan_priority()
    assert saved is not None
    assert saved["meta"]["saved_at"] == meta["saved_at"]
    # Every planned order got a rank.
    assert f"SO1{KEY_SEP}{ITEM_A}" in saved["ranks"]
    assert f"SO2{KEY_SEP}{ITEM_B}" in saved["ranks"]


def test_apply_without_completed_run_is_409():
    m = _api()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._optimize_apply()
    assert e.value.status_code == 409


def test_double_start_is_409():
    m = _api()
    _seed_book()
    from fastapi import HTTPException
    m._OPTIMIZE["state"] = "running"
    with pytest.raises(HTTPException) as e:
        m._start_optimize(budget_evals=5, label="quick", background=False)
    assert e.value.status_code == 409


def test_empty_book_is_400():
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())      # masters, no orders
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._start_optimize(budget_evals=5, label="quick", background=False)
    assert e.value.status_code == 400


def test_all_committed_book_optimizes_like_open():
    """Post-pivot (2026-07-15): commitment lanes are pure status labels, so an
    all-committed book is optimized exactly like an all-open one — every active
    line competes in the same pool (no 'all promise-protected' rejection)."""
    m = _api()
    _seed_book()
    book_store.set_commitment("SO1", ITEM_A, "committed", date(2025, 3, 25), "t")
    book_store.set_commitment("SO2", ITEM_B, "committed", date(2025, 3, 26), "t")
    st = m._start_optimize(budget_evals=5, label="quick", background=False)
    assert st["state"] == "done"


def test_clear_removes_applied_optimization():
    m = _api()
    _seed_book()
    m._start_optimize(budget_evals=10, label="quick", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority() is not None
    m._optimize_clear()
    assert book_store.load_plan_priority() is None


def test_apply_clears_in_memory_job_so_refresh_does_not_reoffer():
    m = _api()
    _seed_book()
    m._start_optimize(budget_evals=10, label="quick", background=False)
    m._optimize_apply()
    st = m._optimize_status()
    assert st["state"] == "idle" and st["best"] is None   # nothing left to re-offer


def test_cancel_is_noop_when_idle():
    m = _api()
    st = m._optimize_cancel()
    assert st["state"] == "idle"


# --------------------------------------------------------------------------- #
# _plan integration: replay + optimize_meta
# --------------------------------------------------------------------------- #
def test_plan_reports_inactive_meta_by_default():
    m = _api()
    _seed_book()
    result = m._plan(m._load_plan_config())
    assert result["optimize_meta"] == {"active": False}


def test_plan_replays_applied_ranks_and_reports_coverage():
    m = _api()
    _seed_book()
    # Save a rank map by hand that reverses the two orders.
    book_store.save_plan_priority(
        {f"SO2{KEY_SEP}{ITEM_B}": 1, f"SO1{KEY_SEP}{ITEM_A}": 2},
        {"saved_at": "2026-07-13T10:00:00"})
    result = m._plan(m._load_plan_config())
    meta = result["optimize_meta"]
    assert meta["active"] is True
    assert meta["saved_at"] == "2026-07-13T10:00:00"
    assert meta["covered"] == 2 and meta["uncovered"] == 0
    assert any("Optimized sequence" in n for n in result["trace"]["rule3"]["notes"])


def test_plan_counts_new_orders_as_uncovered():
    m = _api()
    _seed_book()
    book_store.save_plan_priority({f"SO1{KEY_SEP}{ITEM_A}": 1}, {"saved_at": "t"})
    book_store.add_orders([Order("SO9", ITEM_A, ITEM_A, 5, date(2025, 3, 28))])
    result = m._plan(m._load_plan_config())
    meta = result["optimize_meta"]
    assert meta["covered"] == 1 and meta["uncovered"] == 2   # SO2 + the new SO9


def test_ranks_resequence_the_whole_book_including_committed():
    # Post-pivot (2026-07-16): lanes are status labels. A rank map naming a
    # committed order replays over the WHOLE book in one pass — the committed
    # order is sequenced by its rank like any other. It still schedules.
    m = _api()
    _seed_book()
    book_store.set_commitment("SO1", ITEM_A, "committed", date(2025, 3, 25), "t")
    book_store.save_plan_priority(
        {f"SO1{KEY_SEP}{ITEM_A}": 2, f"SO2{KEY_SEP}{ITEM_B}": 1}, {"saved_at": "t"})
    result = m._plan(m._load_plan_config())   # must not raise; single pass
    assert result["optimize_meta"]["active"] is True
    # The committed order still schedules (it is in the one-pool plan).
    r8_rows = result["trace"]["rule8"]["output"]["rows"]
    assert any("SO1" in str(r) for r in r8_rows)


# --------------------------------------------------------------------------- #
# HTTP role gating
# --------------------------------------------------------------------------- #
def test_http_role_gating():
    from fastapi.testclient import TestClient
    from api.main import app
    from api import auth

    accts = auth._accounts()
    admin = next(u for u, a in accts.items() if a["role"] == auth.ADMIN)
    user = next(u for u, a in accts.items() if a["role"] == auth.USER)

    def client_as(u):
        c = TestClient(app)
        r = c.post("/login", data={"username": u, "password": accts[u]["password"]})
        assert r.status_code == 200
        return c

    cu = client_as(user)
    assert cu.post("/optimize", json={"budget": "quick"}).status_code == 403
    assert cu.post("/optimize/apply").status_code == 403
    assert cu.post("/optimize/clear").status_code == 403
    assert cu.post("/optimize/cancel").status_code == 403
    assert cu.get("/optimize/status").status_code == 200     # read-only is fine

    ca = client_as(admin)
    assert ca.get("/optimize/status").status_code == 200
    assert ca.post("/optimize/cancel").status_code == 200    # idle no-op, admin allowed
    assert ca.post("/optimize", json={"budget": "nonsense"}).status_code == 400
