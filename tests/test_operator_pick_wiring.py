"""`balance_operator_load` must actually reach the new engine.

The setting is read only by engine/rules/rule6_allocate.py — the CLASSIC engine.
ppc_engine has its own `operator_pick`, which new_engine._plan_config() never
set, so it stayed "scarce": least-flexible free operator, ties broken by NAME.

On the live book that put Rohan Chakane at 90.4% and Sidhu Singe at 81.6%
despite identical qualifications and shift, purely because "Rohan" sorts first.
"""
from datetime import date

import pytest

from engine.config import Config
from engine import new_engine


def _cfg(**kw):
    kw.setdefault("plan_start_date", date(2026, 8, 10))
    return Config(**kw)


def test_off_by_default_keeps_the_scarce_policy():
    assert new_engine._plan_config(_cfg()).operator_pick == "scarce"


def test_balance_operator_load_selects_the_balanced_policy():
    assert new_engine._plan_config(
        _cfg(balance_operator_load=True)).operator_pick == "balanced"


def test_explicitly_off_stays_scarce():
    assert new_engine._plan_config(
        _cfg(balance_operator_load=False)).operator_pick == "scarce"


def test_the_engine_understands_the_value_it_is_given():
    """A typo'd policy would silently fall through to 'scarce' inside the board,
    so pin that the wired value is one ppc_engine actually implements."""
    assert new_engine._plan_config(
        _cfg(balance_operator_load=True)).operator_pick in ("scarce", "balanced", "flexible")
