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
    """Same misses, on both sides of the band and the cap.

    **LATE-ONLY misses since 2026-08-13, deliberately.** `engine/optimizer.py` was
    made one-sided (earliness free) at the owner's request; `ppc_engine/` is a
    vendored package and its mirror of this term was left symmetric on purpose,
    because it is reachable only from the retired `new` engine's internal search.
    The two therefore genuinely disagree on EARLY orders now — that divergence is
    asserted head-on in `test_earliness_is_where_the_two_deliberately_diverge`
    rather than swept under this fixture.
    """
    days_off = [30, 10, 5, 4, 0, 100, 61, 64, 63]
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


def test_earliness_is_where_the_two_deliberately_diverge():
    """The known, intended asymmetry (2026-08-13). Pinned so it cannot be
    "repaired" by silently re-symmetrising `engine/optimizer.py`.

    The owner reversed the 2026-08-06 symmetric rule for the scorers that pick and
    report the live contest winner. `ppc_engine/` is vendored and untouched, so its
    copy still charges for early orders. The engine under test does not.
    """
    line = SOLine(so_no="SO1", item_code="IT-A", item_name="IT-A", qty=10,
                  delivery_date=date(2026, 9, 1))
    entry = ScheduleEntry(
        batch_id="SO1", item_code="IT-A", process_seq=1, process_name="CNC",
        machine="CNC1", qty=10, occupancy_min=60,
        start=datetime(2026, 8, 6, 8, 0),
        end=datetime(2026, 9, 1, 17, 0) - timedelta(days=30), so_refs=["SO1"])

    engine_breach = optimizer.plan_metrics([entry], [line], date(2026, 8, 6))["ontime_breach"]
    ppc_breach = _ontime_breach(
        PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=0.0,
                    lateness_by_order={("SO1", "IT-A"): -30.0},
                    promise_slip_by_order={}), CFG)
    assert engine_breach == 0.0, "the live scorer must charge nothing for 30 days early"
    assert ppc_breach == 676.0, "ppc_engine's vendored mirror stays symmetric"
