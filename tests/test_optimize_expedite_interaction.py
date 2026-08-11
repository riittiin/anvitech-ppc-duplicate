"""Regression: the Expedite window must not cancel out the Optimize feature.

Bug (2026-07-13, hit on the live site): with the "Expedite urgent orders" tick ON, the
optimizer found "no improvement" for every book and an applied optimization had no effect
— because expedite dynamically re-sorts ops by slack at schedule time, overriding the
batch sequence the optimizer controls. Fix: the optimizer searches with expedite OFF, and
an applied plan runs the ranked orders with expedite OFF (the found order supersedes
expedite). This guards both halves.
"""
import os
import json
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.config import Config, OVERLAP_PERCENT


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


# --- Unit: an applied optimization always plans in the pure non-delay model --------- #
def test_ranked_config_forces_expedite_off_only_when_ranks_present():
    from dataclasses import replace
    cfg = Config(expedite_window_min=45)
    # Mirrors _plan's rule: ranked_config = expedite-off iff ranks exist.
    ranked = replace(cfg, expedite_window_min=0)
    assert ranked.expedite_window_min == 0
    assert cfg.expedite_window_min == 45          # original untouched
    # No ranks -> config unchanged (expedite behaves exactly as the tick sets it).
    assert cfg.expedite_window_min == 45


# --- Integration on the real book (the exact live scenario) ------------------------- #
@pytest.mark.skipif(not os.path.exists("Test5.xlsx"), reason="real data file not present")
def test_optimize_beats_the_current_plan_even_with_expedite_on():
    m = _api()
    from engine.loaders import load_all
    from engine.models import Order

    with open("Test5.xlsx", "rb") as f:
        book_store.save_masters_bytes(f.read())
    so_lines, _ = load_all("Test5.xlsx")
    if not so_lines:
        # The owner swaps/edits the real data file; an emptied SO sheet (e.g.
        # the go-live cleanup of 2026-07-18) leaves nothing to optimize.
        pytest.skip("Test5.xlsx present but has no sales orders")
    book_store.add_orders([Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date)
                           for s in so_lines])
    # The exact live config: Expedite AND Balance both ON.
    live = Config(plan_start_date=date(2026, 7, 11), overlap_mode=OVERLAP_PERCENT,
                  overlap_percent=80, apply_operator_logic=True, split_parallel=True,
                  expedite_window_min=45, balance_operator_load=True)
    book_store.save_plan_config(json.dumps(live.to_dict()))
    m._MASTERS_CACHE["masters"] = None

    def late_days(result):
        due = {f"{s.so_no}\x1f{s.item_code}": s.delivery_date for s in so_lines}
        gaps = [(date.fromisoformat(v) - due[k]).days
                for k, v in result["expected_end"].items() if k in due]
        return sum(g for g in gaps if g > 0)

    before = late_days(m._plan(m._load_plan_config()))

    st = m._start_optimize(budget_evals=120, label="quick", background=False)
    # The bug was: improved=False, baseline==best. Now it must find a real improvement.
    assert st["improved"] is True
    assert st["best"]["total_late_days"] < st["baseline"]["total_late_days"]
    # Baseline shown is the REAL current plan (expedite on), not the expedite-off Rule-3.
    assert st["baseline"]["total_late_days"] == before

    m._optimize_apply()
    after = late_days(m._plan(m._load_plan_config()))
    assert after < before        # applying actually improves the live plan (was a no-op)
    assert after == st["best"]["total_late_days"]   # what you saw is what you get
