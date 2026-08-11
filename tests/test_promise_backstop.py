"""Committed-promise apply backstop (mirrors the worst-order backstop in
tests/test_auto_optimize.py): `_auto_apply_result`'s `promise_ok` gate.

Owner's rule (committed-date-stability, 2026-07-29): a committed order may
drift up to `committed_promise_slack_days` (default 3) worse than its
promise WITHOUT that alone blocking an apply — only reject if the new plan
pushes a committed order PAST the +slack cap, or makes an ALREADY-breaching
order even worse. The threshold is `max(slack, inc.max_committed_slip)`:

  inc=0, best=2  -> threshold max(3,0)=3 -> 2<=3            -> APPLIES
  inc=0, best=5  -> threshold max(3,0)=3 -> 5>3              -> REJECTS
  inc=6, best=6  -> threshold max(3,6)=6 -> 6<=6 (not worse)  -> APPLIES
  inc=6, best=8  -> threshold max(3,6)=6 -> 8>6 (worse)       -> REJECTS
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store, optimizer
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


def _run(monkeypatch, inc_slip, best_slip):
    """Shared harness: seed a book, stub the incumbent at `inc_slip`, stage a
    contest result (strictly better score, same worst order) at `best_slip`,
    run `_auto_apply_result`, and return (module, note_text, loaded_ranks)."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    # `ontime_breach` is what score() reads since 2026-08-06; `total_late_days` is
    # still reported and is what the auto-note text asserts on. Both are needed:
    # the first makes the score premise true, the second makes the note assertion true.
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "ontime_breach": 500.0,
                                  "max_late_days": 46, "max_committed_slip": inc_slip})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "ontime_breach": 100.0,
                                          "max_late_days": 46,
                                          "max_committed_slip": best_slip},
                                 "ranks": {"k": 2},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}
        m._OPTIMIZE["auto"] = True
    # Sanity check on the premise every case isolates: score is strictly
    # better on its own — the backstop (or lack of one) is the only reason
    # apply/reject differs from a plain score comparison.
    assert (optimizer.score({"total_late_days": 100, "makespan_days": 50.0, "ontime_breach": 100.0}) <
            optimizer.score({"total_late_days": 500, "makespan_days": 50.0, "ontime_breach": 500.0}))

    m._auto_apply_result()

    note = book_store.load_auto_note()
    assert note is not None
    loaded = book_store.load_plan_priority()
    return m, note["text"], loaded


def test_auto_apply_applies_plan_within_cap_despite_drift(monkeypatch):
    """The bug this branch fixes: a committed order drifting from 0 to +2
    days is well within the +3 cap and must APPLY, not be rejected as a
    'regression'."""
    m, text, loaded = _run(monkeypatch, inc_slip=0, best_slip=2)
    assert "auto-re-optimized" in text
    assert loaded["ranks"] == {"k": 2}


def test_auto_apply_rejects_plan_that_pushes_committed_past_cap(monkeypatch):
    """A plan that pushes a committed order to +5 (past the +3 cap, with the
    incumbent at 0) must still be REJECTED."""
    m, text, loaded = _run(monkeypatch, inc_slip=0, best_slip=5)
    assert "protect a committed promise" in text
    assert "5" in text  # regression amount: 5 - 0
    assert loaded["ranks"] == {"k": 1}  # untouched


def test_auto_apply_applies_when_already_breaching_and_not_worse(monkeypatch):
    """The incumbent is already past cap (+6). A candidate that holds at the
    same +6 (not worse) must APPLY — the cap doesn't retroactively veto an
    already-bad situation the new plan doesn't make worse."""
    m, text, loaded = _run(monkeypatch, inc_slip=6, best_slip=6)
    assert "auto-re-optimized" in text
    assert loaded["ranks"] == {"k": 2}


def test_auto_apply_rejects_when_already_breaching_and_worse(monkeypatch):
    """The incumbent is already past cap (+6). A candidate that pushes it
    further to +8 must still be REJECTED."""
    m, text, loaded = _run(monkeypatch, inc_slip=6, best_slip=8)
    assert "protect a committed promise" in text
    assert "2" in text  # regression amount: 8 - 6
    assert loaded["ranks"] == {"k": 1}  # untouched
