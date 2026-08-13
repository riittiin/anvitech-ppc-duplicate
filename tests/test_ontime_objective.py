"""Symmetric on-time objective, engine side (spec 2026-08-06, reshaped 2026-08-13).

The owner's rule, in full: deliver on time; +/-4 days either side is fine; beyond
that early and late are equally bad.

THE OVERAGE IS NOW LINEAR, NOT SQUARED (2026-08-13, owner's explicit request —
a deliberate behaviour change, not a fudge). Squaring was the 2026-08-06 rule
that misses must be SPREAD across orders; the measured consequence was that the
search REFUSED plans which cut TOTAL late-days by concentrating them. A CP solver
found a plan 86 late-days better than the live one and the optimizer correctly
rejected it, because that plan had 51-53 late orders with a worst of 29-31 days
while the squared search prefers 55-60 late orders with a worst of 21-24.
Correlation between the score and total late-days on a loaded book: squared
-0.61, linear +0.49.

So the SPREADING property below is deliberately gone, and its test now pins the
opposite. Everything else is untouched and still pinned here: the 4-day band, the
60-day cap, and abs() — early and late still count the same.
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
    """5 days off -> overage 1 -> 1.0. Pins band=4 exactly. (Unchanged by the
    linear switch: at an overage of exactly 1, x and x^2 agree — which is why
    this test alone could never have caught the shape.)"""
    assert _breach_for([5]) == 1.0
    assert _breach_for([-5]) == 1.0


def test_the_breach_is_linear_in_the_overage():
    """The 2026-08-13 change, stated directly: the term is the SUM OF DAYS beyond
    the band, so twice the overage costs exactly twice as much. Under the old
    squared shape 2 days beyond band cost 4.0 and 4 days cost 16.0."""
    assert _breach_for([5]) == 1.0            # overage 1
    assert _breach_for([6]) == 2.0            # overage 2   (was 4.0)
    assert _breach_for([8]) == 4.0            # overage 4   (was 16.0)
    assert _breach_for([30]) == 26.0          # overage 26  (was 676.0)


def test_misses_are_no_longer_spread_across_orders():
    """The behaviour change, pinned so it cannot be undone by accident.

    This is the exact inverse of the deleted ``test_squaring_spreads_the_misses``.
    Under squaring, ten orders 6 days out (10 * 2^2 = 40) beat one order 30 days
    out ((30-4)^2 = 676) by 17x, and that preference is what made the search
    reject plans that concentrated their misses. Linear is INDIFFERENT between
    the two whenever the total overage matches: the score now tracks total
    late-days rather than their distribution.
    """
    # 13 orders 6 days out = 13 x 2 days of overage = one order 30 days out (26).
    assert _breach_for([6] * 13) == _breach_for([30]) == 26.0
    # ...and the general property: only the TOTAL overage counts, not its shape.
    assert _breach_for([10, 10]) == _breach_for([16]) == 12.0


def test_cap_stops_one_hopeless_order_dominating():
    """Overage is still capped at 60, so 100 days out scores the same as 64 days
    out. Without this a single doomed order swamps the whole plan and the search
    chases it instead of the orders it can still save. Only the SHAPE changed
    (60.0 rather than 60.0^2) — the cap itself is untouched."""
    assert _breach_for([100]) == _breach_for([64]) == 60.0
    # Non-vacuity: the cap is really binding here — one day inside it costs less.
    assert _breach_for([63]) == 59.0 < _breach_for([64])
    # ...and it is symmetric, like the band.
    assert _breach_for([-100]) == 60.0


def test_score_uses_ontime_breach_and_a_makespan_tiebreak():
    base = {"makespan_days": 50.0, "ontime_breach": 0.0}
    worse = {"makespan_days": 50.0, "ontime_breach": 10.0}
    assert optimizer.score(worse) - optimizer.score(base) == optimizer.ONTIME_WEIGHT * 10.0


def test_makespan_cannot_outrank_the_ontime_term():
    """Makespan is a TIE-BREAK. A plan one day shorter must never beat a plan with
    a genuinely better on-time result.

    Docstring corrected 2026-08-13: a breach of 16.0 used to mean a single order
    8 days off ((8-4)^2); under the linear shape the same 16.0 means one order 20
    days off, or eight orders 6 days off. The assertion is unchanged and still
    holds — but the DAYS it represents grew, which is the honest cost of the
    linear switch and is measured in
    tests/test_roster_search.py::test_makespan_is_only_a_tie_break.
    """
    shorter_but_worse = {"makespan_days": 10.0, "ontime_breach": 16.0}
    longer_but_better = {"makespan_days": 110.0, "ontime_breach": 0.0}
    assert optimizer.score(longer_but_better) < optimizer.score(shorter_but_worse)


def test_a_day_of_makespan_is_worth_a_tenth_of_a_day_of_miss():
    """The exchange rate that makes "tie-break" mean something, stated in DAYS so
    it cannot silently drift when the on-time shape changes. Unchanged by the
    2026-08-13 linear switch: x and x^2 agree at an overage of exactly 1."""
    free = {"makespan_days": 0.0, "ontime_breach": 0.0}
    one_day_over = {"makespan_days": 0.0, "ontime_breach": _breach_for([5])}
    a_day_longer = {"makespan_days": 1.0, "ontime_breach": 0.0}
    assert optimizer.score(one_day_over) - optimizer.score(free) == 1.0
    assert optimizer.score(a_day_longer) - optimizer.score(free) == 0.1


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
