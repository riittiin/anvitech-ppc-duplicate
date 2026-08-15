"""Symmetric on-time objective, engine side (spec 2026-08-06).

The owner's rule, in full: deliver on time; +/-4 days either side is fine; beyond
that early and late are equally bad; and misses must be SPREAD across orders rather
than concentrated on a few. Squaring the overage is what delivers the spreading.
"""
from datetime import date, datetime, timedelta

from engine import optimizer
from engine.models import SOLine, ScheduleEntry

PS = date(2026, 8, 6)
DUE = date(2026, 9, 1)


def _line(so, item, due=DUE):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=10, delivery_date=due)


def _entry(so, item, end):
    return ScheduleEntry(batch_id=so, item_code=item, process_seq=1,
                         process_name="CNC", machine="CNC1", qty=10,
                         occupancy_min=60, start=datetime(2026, 8, 6, 8, 0),
                         end=end, so_refs=[so])


def _breach_for(days_off):
    """days_off > 0 = late, < 0 = early."""
    lines, sched = [], []
    for n, d in enumerate(days_off):
        so, item = f"SO{n}", f"IT-{n}"
        lines.append(_line(so, item))
        end = datetime(2026, 9, 1, 17, 0) + timedelta(days=d)
        sched.append(_entry(so, item, end))
    return optimizer.plan_metrics(sched, lines, PS)["ontime_breach"]


def test_early_and_late_are_penalised_identically():
    """The core of the owner's rule: 30 days early is exactly as bad as 30 late."""
    assert _breach_for([30]) == _breach_for([-30])
    assert _breach_for([30]) > 0


def test_inside_the_band_costs_nothing_either_direction():
    for d in (0, 4, -4, 3, -1):
        assert _breach_for([d]) == 0.0, f"{d} days off should be free"


def test_one_day_past_the_band_costs_one():
    """5 days off -> overage 1 -> 1 squared -> 1.0. Pins band=4 exactly."""
    assert _breach_for([5]) == 1.0
    assert _breach_for([-5]) == 1.0


def test_squaring_spreads_the_misses():
    """The owner's stated requirement: ten orders slightly off must beat one order
    badly off. 30 days out -> (30-4)^2 = 676; ten at 6 days -> 10 * (6-4)^2 = 40."""
    concentrated = _breach_for([30])
    spread = _breach_for([6] * 10)
    assert concentrated == 676.0
    assert spread == 40.0
    assert spread < concentrated


def test_cap_stops_one_hopeless_order_dominating():
    """Overage is capped at 60 before squaring, so 100 days out scores the same as
    64 days out. Without this a single doomed order swamps the whole plan."""
    assert _breach_for([100]) == _breach_for([64]) == 60.0 ** 2


def test_score_uses_ontime_breach_and_a_makespan_tiebreak():
    base = {"makespan_days": 50.0, "ontime_breach": 0.0}
    worse = {"makespan_days": 50.0, "ontime_breach": 10.0}
    assert optimizer.score(worse) - optimizer.score(base) == optimizer.ONTIME_WEIGHT * 10.0


def test_makespan_cannot_outrank_the_ontime_term():
    """Makespan is a TIE-BREAK. A plan one day shorter must never beat a plan with a
    genuinely better on-time result. At weight 0.1, 100 extra days of schedule are
    worth less than a single order 8 days off ((8-4)^2 = 16)."""
    shorter_but_worse = {"makespan_days": 10.0, "ontime_breach": 16.0}
    longer_but_better = {"makespan_days": 110.0, "ontime_breach": 0.0}
    assert optimizer.score(longer_but_better) < optimizer.score(shorter_but_worse)


def test_makespan_still_breaks_an_exact_tie():
    a = {"makespan_days": 50.0, "ontime_breach": 5.0}
    b = {"makespan_days": 60.0, "ontime_breach": 5.0}
    assert optimizer.score(a) < optimizer.score(b)


def test_plan_metrics_keeps_every_reported_field():
    """Global constraint: the UI and api read these. Losing one blanks a panel."""
    m = optimizer.plan_metrics([_entry("SO1", "IT-A", datetime(2026, 9, 20, 17, 0))],
                               [_line("SO1", "IT-A")], PS)
    for field in ("makespan_days", "late_orders", "total_late_days", "max_late_days",
                  "slip_severity", "ceiling_breach", "committed_promise_breach",
                  "max_committed_slip", "orders", "ontime_breach"):
        assert field in m, f"plan_metrics stopped reporting {field}"
    assert m["total_late_days"] == 19        # still reported even though score ignores it
    assert m["slip_severity"] == (19 - 2) ** 2
