"""Symmetric on-time objective, ppc_engine side (spec 2026-08-06).

Mirror of tests/test_ontime_objective.py. The two scorers must agree exactly — see
tests/test_scorer_mirror.py.
"""
from datetime import datetime

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _ontime_breach, score

CFG = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))


def _pm(lateness, makespan=0.0):
    """lateness_by_order values are SIGNED days: negative means early."""
    return PlanMetrics(
        total_tardiness_days=0.0,
        max_tardiness_days=0.0,
        late_order_count=0,
        makespan_days=makespan,
        lateness_by_order={(f"SO{n}", "x"): float(v) for n, v in enumerate(lateness)},
        promise_slip_by_order={},
    )


def test_early_and_late_are_penalised_identically():
    assert _ontime_breach(_pm([30]), CFG) == _ontime_breach(_pm([-30]), CFG)
    assert _ontime_breach(_pm([30]), CFG) > 0


def test_inside_the_band_costs_nothing_either_direction():
    for d in (0, 4, -4, 3, -1):
        assert _ontime_breach(_pm([d]), CFG) == 0.0, f"{d} days off should be free"


def test_one_day_past_the_band_costs_one():
    assert _ontime_breach(_pm([5]), CFG) == 1.0
    assert _ontime_breach(_pm([-5]), CFG) == 1.0


def test_squaring_spreads_the_misses():
    assert _ontime_breach(_pm([30]), CFG) == 676.0
    assert _ontime_breach(_pm([6] * 10), CFG) == 40.0


def test_cap_stops_one_hopeless_order_dominating():
    assert _ontime_breach(_pm([100]), CFG) == _ontime_breach(_pm([64]), CFG) == 3600.0


def test_score_is_the_ontime_term_plus_a_makespan_tiebreak():
    """With both guards dormant, the score is exactly these two terms."""
    m = _pm([10], makespan=50.0)
    expected = CFG.ontime_weight * 36.0 + CFG.makespan_weight * 50.0
    assert abs(score(m, CFG) - expected) < 1e-9


def test_makespan_cannot_outrank_the_ontime_term():
    shorter_but_worse = _pm([8], makespan=10.0)     # (8-4)^2 = 16
    longer_but_better = _pm([0], makespan=110.0)    # inside the band -> 0
    assert score(longer_but_better, CFG) < score(shorter_but_worse, CFG)
