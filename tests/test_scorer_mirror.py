"""The two scorers must judge a plan identically.

engine/optimizer.py scores the contest winner-pick and the apply comparison;
ppc_engine/objective scores the inner sequence search. They are documented as
mirrors. Before 2026-08-06 they were not: the search weighed makespan 0.1 against
the winner-pick's 40, and carried a 30x worst-order fairness term the winner-pick
did not have at all. Nothing caught either, because nothing compared them.

SINCE 2026-08-13 they diverge in EXACTLY ONE PLACE, on purpose: the SHAPE of the
on-time overage. ``engine/optimizer.py`` is linear (the owner's request — see
that module); ``ppc_engine/`` is vendored, drives only the retired ``new``
engine, and was deliberately left squaring. Every other constant — band, cap,
symmetry, all the weights — is still pinned identical below, and the on-time test
is rescaled rather than deleted so a SECOND divergence would still be caught.
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


DAYS_OFF = [30, -30, 10, -10, 5, -5, 4, -4, 0, 100, -100, 61]


def _both_breaches():
    """The same misses — both directions, on both sides of the band and the cap —
    put through both implementations. Returns (engine, ppc_engine)."""
    lines, sched, lateness = [], [], {}
    for n, d in enumerate(DAYS_OFF):
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
    return engine_breach, ppc_breach


def test_the_two_agree_on_band_cap_and_symmetry_and_differ_only_in_SHAPE():
    """REBASED 2026-08-13 — a deliberate behaviour change, not a fudge.

    ``engine/optimizer.py`` went LINEAR at the owner's request (see that module).
    ``ppc_engine/`` is vendored, drives only the retired ``new`` engine, and was
    deliberately NOT changed — so these two no longer compute the same number.

    Deleting the mirror would have thrown away everything it was guarding. The
    check is rescaled instead: square the engine's per-order overage back up and
    the two must agree EXACTLY. That still pins the band (4), the cap (60),
    ``abs()`` symmetry and which orders are counted at all — it would fail if
    anyone diverged a second thing — while recording that the shape is the one
    intended difference.
    """
    engine_breach, ppc_breach = _both_breaches()
    band, cap = optimizer.ONTIME_BAND_DAYS, optimizer.ONTIME_CAP_DAYS
    resquared = sum(min(abs(d) - band, cap) ** 2
                    for d in DAYS_OFF if abs(d) - band > 0)
    linear = sum(min(abs(d) - band, cap) for d in DAYS_OFF if abs(d) - band > 0)
    assert engine_breach == linear
    assert ppc_breach == resquared
    assert engine_breach > 0        # the fixture must actually exercise the term


def test_the_divergence_is_real_and_recorded():
    """Non-vacuity for the rescaling above: without it the two disagree, and by a
    lot. If someone reverts the engine to squaring, this fails and forces the
    revert to be a conscious decision rather than a silent one."""
    engine_breach, ppc_breach = _both_breaches()
    assert engine_breach != ppc_breach
    assert engine_breach == 243.0 and ppc_breach == 11875.0
