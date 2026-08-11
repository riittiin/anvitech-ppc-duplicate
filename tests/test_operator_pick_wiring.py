"""`balance_operator_load` is deliberately NOT forwarded to the new engine.

The setting is read only by engine/rules/rule6_allocate.py (the CLASSIC engine),
so under scheduler='new' the staffing board uses its own default, "scarce":
least-flexible free operator, ties broken by NAME. On the live book that runs
Rohan Chakane at 90.4% and Sidhu Singe at 81.6% despite identical qualifications
and shift, purely because "Rohan" sorts first.

Forwarding it to "balanced" was implemented and measured on the live production
book (2026-08-12, applied ranks, single metric, both policies run twice and
deterministic):

    policy      late-days   late lines   objective   span   util spread
    scarce            397           40       3,399   48 d       88 pts
    balanced          398           39       3,500   47 d       74 pts

Balanced spreads the work far more evenly (Swapnil 10.6% -> 43.0%, Narayan Fatak
2.4% -> 16.5%) but the PLAN is worse on both delivery measures — +1 late-day and
+101 on the app's own objective. A more even roster is not worth a worse plan, so
the wiring was reverted. These tests pin that decision so it is not silently
undone.
"""
from datetime import date

import pytest

from engine.config import Config
from engine import new_engine


def _cfg(**kw):
    kw.setdefault("plan_start_date", date(2026, 8, 10))
    return Config(**kw)


def test_the_new_engine_uses_the_scarce_policy():
    assert new_engine._plan_config(_cfg()).operator_pick == "scarce"


def test_balance_operator_load_does_not_change_the_policy():
    """Measured worse on the live book — see the module docstring."""
    assert new_engine._plan_config(
        _cfg(balance_operator_load=True)).operator_pick == "scarce"


def test_turning_it_off_also_leaves_scarce():
    assert new_engine._plan_config(
        _cfg(balance_operator_load=False)).operator_pick == "scarce"
