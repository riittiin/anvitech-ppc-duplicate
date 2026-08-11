"""Optimize with committed + open orders all present, honouring the
daily-actuals feedback loop.

Post-pivot (2026-07-16) the contract is INFORMATIONAL: lanes (open/committed)
are pure status labels with no scheduling effect (the Urgent lane was removed
2026-07-29 — its orders migrate to committed). So:
  1. LANES: an applied optimization reorders the whole book as one pool; committing
     orders does not change the plan (proved in
     test_replay_single_pass). Here we assert the optimization goes active and the
     feedback loop still works alongside it.
  2. FEEDBACK: after Optimize is applied, punching a Rule-7 actual on any order
     still reduces that order's remaining qty on the next Plan (the saved artifact
     is an ORDERING, not a frozen schedule — it replays against current reality).
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order, Actual
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_two_lanes():
    """Two committed, two open orders (all real, routable items)."""
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("C1", ITEM_A, "SAMPLE RING A", 20, date(2025, 3, 20)),   # -> committed
        Order("U1", ITEM_B, "SAMPLE PIN B", 30, date(2025, 3, 18)),    # -> committed
        Order("O1", ITEM_A, "SAMPLE RING A", 40, date(2025, 3, 25)),   # open
        Order("O2", ITEM_B, "SAMPLE PIN B", 50, date(2025, 3, 26)),    # open
    ])
    # Labels only — no scheduling effect.
    book_store.set_commitment("C1", ITEM_A, "committed", date(2025, 3, 22), "t0")
    book_store.set_commitment("U1", ITEM_B, "committed", date(2025, 3, 18), "t0")


def test_optimize_goes_active_over_the_whole_book():
    m = _api()
    _seed_two_lanes()

    m._start_optimize(budget_evals=40, label="quick", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority() is not None

    meta = m._plan(m._load_plan_config())["optimize_meta"]
    assert meta["active"] is True
    # Every active line is covered by the one-pool contest (nothing left out).
    assert meta["uncovered"] == 0


def _remaining(m, so, item):
    out = m._plan(m._load_plan_config())["trace"]["rule8"]["output"]
    cols = out["columns"]
    si, ii, ri = cols.index("SO No"), cols.index("Item Code"), cols.index("Remaining Qty")
    for r in out["rows"]:
        if r[si] == so and r[ii] == item:
            return r[ri]
    return None


def test_feedback_reduces_remaining_even_with_optimize_applied():
    # The saved artifact is an ORDERING (ranks), not a frozen schedule — so the daily
    # feedback loop still drives quantities. After Optimize is applied, punching an
    # actual still reduces that order's remaining on the next Plan.
    m = _api()
    _seed_two_lanes()
    m._start_optimize(budget_evals=40, label="quick", background=False)
    m._optimize_apply()

    assert _remaining(m, "O1", ITEM_A) == 40
    book_store.append_actual(Actual(
        so_no="O1", item_code=ITEM_A, entry_date=date(2025, 3, 6), shift="First shift",
        item_name="SAMPLE RING A", process="INSP", qty_produced=15, qty_rejected=0))
    assert _remaining(m, "O1", ITEM_A) == 25       # 40 − 15 good at the gate
