"""LATE-ONLY on-time objective, engine side (2026-08-13, reverses spec 2026-08-06).

The owner's rule, in full: deliver on time; being up to 4 days LATE is fine; beyond
that lateness costs, and the misses must be SPREAD across orders rather than
concentrated on a few. Squaring the overage is what delivers the spreading.

**Earliness is FREE.** From 2026-08-06 to 2026-08-13 this term was SYMMETRIC —
`abs(gap)` — so an order 20 days early cost exactly what one 20 days late cost.
The owner reversed that on 2026-08-13: he wants total late-days minimised and does
not care how early an order lands. Only the earliness half was removed; the 4-day
band, the 60-day cap, the squaring, the weights and the makespan tie-break are all
untouched.
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


def test_finishing_early_contributes_exactly_zero():
    """The 2026-08-13 reversal. However early an order lands, it costs nothing —
    and the LATE side of the same magnitude is untouched, so this can never pass
    by the term having been switched off altogether."""
    for d in (-5, -6, -20, -30, -64, -100, -900):
        assert _breach_for([d]) == 0.0, f"{d} days EARLY must be free"
    assert _breach_for([30]) == 676.0        # (30 - 4)^2, exactly as before
    assert _breach_for([-30]) < _breach_for([30])


def test_earliness_is_free_even_piled_up_across_many_orders():
    """Not just one order: a whole book landing early is a zero-cost book. Guards
    the case where earliness leaked back in only in aggregate."""
    assert _breach_for([-30] * 20) == 0.0
    assert _breach_for([-30] * 20 + [30]) == 676.0   # the one LATE order, alone


def test_inside_the_band_still_costs_nothing():
    """The band is unchanged at 4 days. 0..4 late is free; so is dead on time."""
    for d in (0, 1, 3, 4):
        assert _breach_for([d]) == 0.0, f"{d} days late should be inside the band"
    assert _breach_for([5]) == 1.0           # and the very next day is not free


def test_one_day_past_the_band_costs_one():
    """5 days LATE -> overage 1 -> 1 squared -> 1.0. Pins band=4 exactly.
    5 days EARLY is now free — the one line this test lost in the reversal."""
    assert _breach_for([5]) == 1.0
    assert _breach_for([-5]) == 0.0


def test_squaring_spreads_the_misses():
    """The owner's stated requirement: ten orders slightly off must beat one order
    badly off. 30 days out -> (30-4)^2 = 676; ten at 6 days -> 10 * (6-4)^2 = 40."""
    concentrated = _breach_for([30])
    spread = _breach_for([6] * 10)
    assert concentrated == 676.0
    assert spread == 40.0
    assert spread < concentrated


def test_cap_stops_one_hopeless_order_dominating():
    """Overage is capped at 60 before squaring, so 100 days LATE scores the same as
    64 days late. Without this a single doomed order swamps the whole plan.

    The cap binds only on the late side now — it is the SAME cap, applied to the
    same overage; nothing about it changed in the 2026-08-13 reversal. 63 days is
    below it and must still be cheaper, or "capped" would just mean "flat"."""
    assert _breach_for([100]) == _breach_for([64]) == 60.0 ** 2
    assert _breach_for([63]) == 59.0 ** 2
    assert _breach_for([63]) < _breach_for([64])


def test_a_wholly_early_plan_beats_a_wholly_late_one_by_the_same_margin():
    """The end-to-end statement of the reversal, through the real `score`, which is
    what actually picks the contest winner. Same book, same margin, opposite sign:
    the early plan must win OUTRIGHT, not tie. Makespan is held equal so the
    tie-break cannot be what decides it."""
    margin = 20
    early = {"makespan_days": 50.0, "ontime_breach": _breach_for([-margin] * 8)}
    late = {"makespan_days": 50.0, "ontime_breach": _breach_for([margin] * 8)}
    assert optimizer.score(early) < optimizer.score(late)
    assert early["ontime_breach"] == 0.0
    assert late["ontime_breach"] == 8 * (margin - 4) ** 2   # 8 x 256 = 2048


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
