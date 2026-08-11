"""Stop & keep best — the NEW engine's search must honor should_cancel promptly.

Bug (2026-08-03): the live new engine dropped `should_cancel` — `optimize`/`tune_overlap`
in ppc_engine had no cancel hook, so a Deep Search ran its whole budget before reporting
instead of stopping when the admin pressed "Stop & keep best". These tests pin the fix:
the search polls the cancel callback per-evaluation, stops promptly, keeps the best plan
found so far, and reports cancelled=True.
"""
from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.optimize import optimize as ppc_optimize, tune_overlap


def _scenario():
    machines = {
        "CNC1": Machine("CNC1", "CNC lathe", MachineKind.MACHINING, 19.5),
        "CNC2": Machine("CNC2", "CNC lathe", MachineKind.MACHINING, 19.5),
    }
    operators = (
        Operator("Op1", Role.OPERATOR, frozenset({"CNC1", "CNC2"}), Shift.FIRST),
        Operator("Op2", Role.OPERATOR, frozenset({"CNC1", "CNC2"}), Shift.FIRST),
    )

    def routing(code):
        return Routing(code, "", operations=(
            Operation(1, "CNC", OperationKind.MACHINING,
                      machine_options=("CNC1", "CNC2"), cycle_min=5.0),
            Operation(2, "DISPATCH", OperationKind.DISPATCH),
        ))

    items = ["A", "B", "C", "D", "E", "F"]
    masters = Masters(machines=machines, operators=operators,
                      routings={c: routing(c) for c in items}, calendar=ShopCalendar())
    orders = [Order(f"SO{i}", c, f"item{c}", qty=50 + 10 * i, due_date=date(2025, 3, 10 + i))
              for i, c in enumerate(items)]
    cfg = PlanConfig(plan_start=datetime(2025, 3, 3, 8, 0), week_anchor=None)
    return orders, masters, cfg


def test_optimize_without_cancel_runs_the_search():
    orders, masters, cfg = _scenario()
    res = ppc_optimize(orders, masters, cfg, budget=60, seed=0)
    assert res.cancelled is False
    assert res.evaluations > 4          # past the 4 dispatch-rule seeds -> real search ran
    assert set(res.best_sequence) == {o.key for o in orders}


def test_optimize_stops_promptly_on_cancel_and_keeps_best():
    orders, masters, cfg = _scenario()
    res = ppc_optimize(orders, masters, cfg, budget=200, seed=0, should_cancel=lambda: True)
    assert res.cancelled is True
    assert res.evaluations <= 4          # broke out during the seed pass, nowhere near 200
    assert set(res.best_sequence) == {o.key for o in orders}   # still a valid best-so-far


def test_optimize_polls_cancel_per_eval_and_stops_midway():
    orders, masters, cfg = _scenario()
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 8           # allow a few polls, then trip -> proves per-eval polling

    res = ppc_optimize(orders, masters, cfg, budget=300, seed=0, should_cancel=cancel)
    assert res.cancelled is True
    assert res.evaluations < 300        # stopped well before the budget


def test_tune_overlap_honors_cancel():
    orders, masters, cfg = _scenario()
    tr = tune_overlap(orders, masters, cfg, budget_per_eval=20, seeds=(0,),
                      should_cancel=lambda: True)
    assert tr.cancelled is True
    assert tr.evaluations <= 20         # stopped in the first probe, didn't run all coarse probes
    assert set(tr.best_sequence) == {o.key for o in orders}
