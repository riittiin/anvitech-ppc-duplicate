"""Manual "Apply this plan" committed-promise backstop (owner decision,
committed-date-stability branch): `_optimize_apply` — the endpoint behind the
admin "Apply this plan" button — must enforce the SAME committed-promise
threshold the daily auto path enforces in `_auto_apply_result`.

Owner's rule (2026-07-29): a committed order may drift up to
`committed_promise_slack_days` (default 3) worse than its promise WITHOUT
that alone blocking an apply — only reject if the new plan pushes a
committed order PAST the +slack cap, or makes an ALREADY-breaching order
even worse. The threshold is `max(slack, inc.max_committed_slip)`:

  inc=0, best=2  -> threshold max(3,0)=3 -> 2<=3            -> APPLIES
  inc=0, best=5  -> threshold max(3,0)=3 -> 5>3              -> REJECTS (409)
  inc=6, best=6  -> threshold max(3,6)=6 -> 6<=6 (not worse)  -> APPLIES
  inc=6, best=8  -> threshold max(3,6)=6 -> 8>6 (worse)       -> REJECTS (409)

Mirrors the fixture pattern in tests/test_promise_backstop.py."""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store
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


def _stage(monkeypatch, inc_slip, best_slip):
    m = _api()
    _seed_book()
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "max_late_days": 46, "max_committed_slip": inc_slip})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46,
                                          "max_committed_slip": best_slip},
                                 "ranks": {"k": 2},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}
    return m, saved_ranks


def test_manual_apply_applies_plan_within_cap_despite_drift(monkeypatch):
    """The bug this branch fixes: a committed order drifting from 0 to +2
    days is well within the +3 cap and must APPLY, not 409."""
    m, saved_ranks = _stage(monkeypatch, inc_slip=0, best_slip=2)
    meta = m._optimize_apply()
    assert meta["best"]["max_committed_slip"] == 2
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == {"k": 2}


def test_manual_apply_rejects_plan_that_pushes_committed_past_cap(monkeypatch):
    """A plan that pushes a committed order to +5 (past the +3 cap, with the
    incumbent at 0) must be rejected (409), and the previously-applied ranks
    must remain untouched."""
    m, saved_ranks = _stage(monkeypatch, inc_slip=0, best_slip=5)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._optimize_apply()
    assert e.value.status_code == 409
    assert "committed" in e.value.detail.lower()

    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks
    assert m._OPTIMIZE["state"] == "done"


def test_manual_apply_applies_when_already_breaching_and_not_worse(monkeypatch):
    """The incumbent is already past cap (+6). A candidate that holds at the
    same +6 (not worse) must APPLY."""
    m, saved_ranks = _stage(monkeypatch, inc_slip=6, best_slip=6)
    meta = m._optimize_apply()
    assert meta["best"]["max_committed_slip"] == 6
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == {"k": 2}


def test_manual_apply_rejects_when_already_breaching_and_worse(monkeypatch):
    """The incumbent is already past cap (+6). A candidate that pushes it
    further to +8 must be rejected (409)."""
    m, saved_ranks = _stage(monkeypatch, inc_slip=6, best_slip=8)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._optimize_apply()
    assert e.value.status_code == 409
    assert "committed" in e.value.detail.lower()

    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks
    assert m._OPTIMIZE["state"] == "done"


def test_manual_apply_all_open_book_still_applies(monkeypatch):
    """Nothing committed: both sides' max_committed_slip default to 0, so
    0 <= max(3, 0) is True and the plan applies freely (owner's
    Apply-before-Commit flow is unaffected)."""
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "max_late_days": 46})  # no max_committed_slip key
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46},
                                 "ranks": {"k": 9},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}

    meta = m._optimize_apply()
    assert meta is not None

    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == {"k": 9}
