"""The two scorers must judge a plan identically.

engine/optimizer.py scores the contest winner-pick and the apply comparison;
ppc_engine/objective scores the inner sequence search. They are documented as
mirrors. Before 2026-08-06 they were not: the search weighed makespan 0.1 against
the winner-pick's 40, and carried a 30x worst-order fairness term the winner-pick
did not have at all. Nothing caught either, because nothing compared them.
"""
from datetime import date, datetime, timedelta

from engine import optimizer
from engine.models import SOLine, ScheduleEntry
from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _ontime_breach

CFG = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))


def test_ontime_constants_are_mirrored():
    assert optimizer.ONTIME_BAND_DAYS == CFG.ontime_band_days
    assert optimizer.ONTIME_CAP_DAYS == CFG.ontime_cap_days
    assert optimizer.ONTIME_WEIGHT == CFG.ontime_weight


def test_makespan_weights_are_now_equal():
    """They diverged 40 vs 0.1 from 2026-07-19 to 2026-08-06. Never again."""
    assert optimizer.MAKESPAN_WEIGHT == CFG.makespan_weight == 0.1


def test_guard_constants_are_mirrored():
    assert optimizer.CEILING_WEIGHT == CFG.ceiling_weight
    assert optimizer.COMMITTED_PROMISE_WEIGHT == CFG.committed_promise_weight


def test_both_implementations_compute_the_same_breach():
    """Same misses, both directions, on both sides of the band and the cap."""
    days_off = [30, -30, 10, -10, 5, -5, 4, -4, 0, 100, -100, 61]
    lines, sched, lateness = [], [], {}
    for n, d in enumerate(days_off):
        so, item = f"SO{n}", f"IT-{n}"
        lines.append(SOLine(so_no=so, item_code=item, item_name=item, qty=10,
                            delivery_date=date(2026, 9, 1)))
        sched.append(ScheduleEntry(
            batch_id=so, item_code=item, process_seq=1, process_name="CNC",
            machine="CNC1", qty=10, occupancy_min=60,
            start=datetime(2026, 8, 6, 8, 0),
            end=datetime(2026, 9, 1, 17, 0) + timedelta(days=d), so_refs=[so]))
        lateness[(so, item)] = float(d)

    engine_breach = optimizer.plan_metrics(sched, lines, date(2026, 8, 6))["ontime_breach"]
    ppc_breach = _ontime_breach(
        PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=0.0,
                    lateness_by_order=lateness, promise_slip_by_order={}), CFG)
    assert engine_breach == ppc_breach
    assert engine_breach > 0        # the fixture must actually exercise the term
