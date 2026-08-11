"""Per-shift operator handoff (branch ``operator-shift-handoff``).

The shop rule: an operation must have a QUALIFIED operator ON EACH SHIFT it runs
in. When a machine-op crosses the 19:00 / 05:00 shift change a fresh qualified
operator for the new shift takes over (a handoff — same machine, different person);
if no qualified operator on the new shift is free, the machine PAUSES until one is.
Nobody is ever billed outside their own shift, and nobody runs two machines at once.

This file encodes the 5 HARD invariants from the brief as executable tests, plus a
crafted multi-shift handoff and a contended-night pause case. Invariants #1/#2 are
checked directly on the scheduler's per-segment bookings (``ScheduleEntry.op_segments``),
which are the authoritative record of who worked each shift.
"""
from datetime import date, datetime, timedelta

from engine.config import Config, OVERLAP_PERCENT
from engine.models import Batch, Machine, Masters, Operator, Process, Routing, WorkCalendar
from engine.pipeline import RuleError
from engine.rules import rule6_allocate
from engine.rules.rule6_allocate import _next_shift_boundary
from engine.operator_coverage import _shift_of, qualified_operators
from engine.analytics import build_analytics
from engine.worktime import WorkClock


# --------------------------------------------------------------------------- #
# Shared invariant checkers (operate on the real per-segment bookings).
# --------------------------------------------------------------------------- #
def _segments(schedule):
    """All (operator, start, end, machine) shift-segments the scheduler booked."""
    out = []
    for e in schedule:
        for (ss, se, op) in (e.op_segments or []):
            if op:
                out.append((op, ss, se, e.machine))
    return out


def assert_no_operator_double_booked(schedule):
    """INVARIANT 1 — no operator is booked on two machines at overlapping times."""
    by_op = {}
    for op, s, e, m in _segments(schedule):
        by_op.setdefault(op, []).append((s, e, m))
    for op, ivs in by_op.items():
        ivs.sort()
        for (s0, e0, m0), (s1, e1, m1) in zip(ivs, ivs[1:]):
            assert e0 <= s1, f"{op} double-booked: {m0} {s0}-{e0} overlaps {m1} {s1}-{e1}"


def assert_within_shift(schedule, masters, config):
    """INVARIANT 2 — no operator is billed outside their own shift's clock windows.
    A segment never crosses a shift boundary, and its operator is qualified for that
    shift (so a first-shift person only appears 08:00-19:00, second 19:00-05:00)."""
    for op, s, e, m in _segments(schedule):
        assert _next_shift_boundary(s, config) >= e, \
            f"{op} segment {s}-{e} on {m} crosses a shift boundary"
        assert op in qualified_operators(m, s, masters, config), \
            f"{op} is not a qualified {_shift_of(s, config)}-shift operator for {m} at {s}"


def assert_fully_staffed(schedule, masters, config, batches=None):
    """INVARIANT 3 — every scheduled non-OS/non-provisional minute has a real
    qualified operator: analytics unstaffed_hrs == 0."""
    an = build_analytics(schedule, masters, config, batches)
    assert an["headline"]["unstaffed_hrs"] == 0.0, \
        f"unstaffed_hrs = {an['headline']['unstaffed_hrs']} (expected 0)"
    for r in an["operators"]:
        assert r["Utilization %"] is None or r["Utilization %"] <= 100.0, r


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
WED = date(2025, 3, 5)   # a Wednesday (not the Thursday weekly-off), 08:00 start


def _cfg(**kw):
    return Config(plan_start_date=WED, apply_operator_logic=True, **kw)


def _cnc(no):
    return Machine(machine_no=no, display_name=no, machine_type="CNC lathe",
                   hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)


def _single_cnc_masters(operators, routings):
    return Masters(machines={"CNC1": _cnc("CNC1")}, operators=operators,
                   calendar=WorkCalendar(), routings=routings)


def _route(item, cyc, qty_machine="CNC1"):
    return Routing(item, "", "", "", None,
                   processes=[Process(1, "CNC", cycle_time=cyc, total_time=None,
                                      suggested_machine=qty_machine, allotted_machine=None)])


def _batch(item, bid, qty):
    return Batch(batch_id=bid, item_code=item, item_name=item, qty=qty,
                 so_delivery_date=date(2025, 4, 30), source_so_refs=[bid])


# --------------------------------------------------------------------------- #
# Crafted correctness: a multi-shift op hands off at 19:00
# --------------------------------------------------------------------------- #
def test_op_crossing_19h_hands_off_to_the_night_operator():
    day = Operator(name="DayOp", preferred_machines_raw="CNC1", machines=["CNC1"],
                   shift="First shift")
    night = Operator(name="NightOp", preferred_machines_raw="CNC1", machines=["CNC1"],
                     shift="Second shift")
    masters = _single_cnc_masters([day, night], {"X": _route("X", 10)})
    # 10 min/pc x 60 + 90 setup = 690 min. From 08:00: 660 min fills 08:00-19:00,
    # the last 30 min spill into the second shift → a handoff.
    sched = rule6_allocate.run([_batch("X", "B1", 60)], config=_cfg(), masters=masters)

    e = next(x for x in sched if x.machine == "CNC1")
    assert len(e.op_segments) == 2, e.op_segments
    (s0, e0, o0), (s1, e1, o1) = e.op_segments
    assert o0 == "DayOp" and s0 == datetime(2025, 3, 5, 8, 0) and e0 == datetime(2025, 3, 5, 19, 0)
    assert o1 == "NightOp" and s1 == datetime(2025, 3, 5, 19, 0) and e1 == datetime(2025, 3, 5, 19, 30)
    assert e.end == datetime(2025, 3, 5, 19, 30)
    assert_no_operator_double_booked(sched)
    assert_within_shift(sched, masters, _cfg())
    assert_fully_staffed(sched, masters, _cfg(), [_batch("X", "B1", 60)])


def test_machine_pauses_when_no_night_crew_is_free():
    """Two two-shift machines, ONE night operator, plentiful day crew. Both ops run
    into the night, but the sole night person can staff only ONE at a time — the
    other machine PAUSES until they free. No unstaffed minute; nobody double-booked."""
    masters = Masters(
        machines={"CNC1": _cnc("CNC1"), "CNC2": _cnc("CNC2")},
        operators=[
            Operator(name="DayA", preferred_machines_raw="CNC1,CNC2",
                     machines=["CNC1", "CNC2"], shift="First shift"),
            Operator(name="DayB", preferred_machines_raw="CNC1,CNC2",
                     machines=["CNC1", "CNC2"], shift="First shift"),
            Operator(name="NightN", preferred_machines_raw="CNC1,CNC2",
                     machines=["CNC1", "CNC2"], shift="Second shift"),
        ],
        calendar=WorkCalendar(),
        routings={"X1": _route("X1", 10, "CNC1"), "X2": _route("X2", 10, "CNC2")},
    )
    batches = [_batch("X1", "B1", 60), _batch("X2", "B2", 60)]
    sched = rule6_allocate.run(batches, config=_cfg(), masters=masters)

    assert_no_operator_double_booked(sched)
    assert_within_shift(sched, masters, _cfg())
    assert_fully_staffed(sched, masters, _cfg(), batches)
    # The sole night operator works both machines' night portions, back to back.
    night = sorted((s, e) for op, s, e, m in _segments(sched) if op == "NightN")
    assert len(night) == 2 and night[0][1] <= night[1][0], night
    # One machine's op finished later than the other because it waited for the crew.
    ends = sorted(e.end for e in sched)
    assert ends[-1] > ends[0]


# --------------------------------------------------------------------------- #
# INVARIANT 3 on the real sample workbook (operator logic ON)
# --------------------------------------------------------------------------- #
def test_sample_book_is_fully_staffed_and_conflict_free(loaded):
    _, masters = loaded
    from tests.sample_workbook import ITEM_A, ITEM_B
    batches = [_batch(ITEM_A, "A", 50), _batch(ITEM_B, "B", 100)]
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True,
                 split_parallel=True, overlap_mode=OVERLAP_PERCENT, overlap_percent=80)
    sched = rule6_allocate.run(list(batches), config=cfg, masters=masters)
    assert_no_operator_double_booked(sched)
    assert_within_shift(sched, masters, cfg)
    assert_fully_staffed(sched, masters, cfg, batches)


# --------------------------------------------------------------------------- #
# CONTAINMENT — every segment lies inside its machine's own working windows
# --------------------------------------------------------------------------- #
def test_every_op_segment_lies_inside_its_machine_clock_windows(loaded):
    """Every operator shift-segment must fall ENTIRELY inside its own machine's
    working-clock windows — no minute booked outside the machine's shifts or on an
    off day. This directly guards the window/lookup consistency the whole handoff
    feature depends on (the segment clock and the op-lookup clock must agree)."""
    _, masters = loaded
    from tests.sample_workbook import ITEM_A, ITEM_B
    batches = [_batch(ITEM_A, "A", 50), _batch(ITEM_B, "B", 100)]
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True,
                 split_parallel=True, overlap_mode=OVERLAP_PERCENT, overlap_percent=80)
    sched = rule6_allocate.run(list(batches), config=cfg, masters=masters)
    clock_for, _ = rule6_allocate._clock_factory(masters, cfg)
    checked = 0
    for e in sched:
        clk = clock_for(e.machine)
        for (ss, se, op) in (e.op_segments or []):
            span = (se - ss).total_seconds() / 60.0
            working = clk.working_minutes_between(ss, se)
            assert abs(working - span) < 1e-6, (
                f"segment {ss}-{se} on {e.machine} (op {op!r}) lies partly outside the "
                f"machine's working windows: {working:.3f} working min of {span:.3f} span")
            checked += 1
    assert checked > 0        # the run actually produced operator segments to check


# --------------------------------------------------------------------------- #
# FAIL LOUD — guard exhaustion raises, never silently under-schedules
# --------------------------------------------------------------------------- #
def test_lay_segments_raises_when_the_guard_is_exhausted():
    """A run too long to lay within the segment guard (20000 shift segments) must
    FAIL LOUD with a RuleError, not return silently with quantity left unscheduled."""
    cal = WorkCalendar(weekly_off_weekday=6)          # near-24/7 calendar
    clk = WorkClock(cal, [(0, 24 * 60)])              # a full-day window each working day
    cfg = _cfg()
    ps = datetime(2025, 3, 5, 8, 0)
    op_lookup = lambda m, t: ["Op"]                   # always one free qualified operator
    # 20,000,000 machine-minutes cannot be laid within 20000 shift segments, so the
    # guard trips before ``remaining`` reaches zero.
    try:
        rule6_allocate._lay_segments("CNC1", clk, ps, 20_000_000, op_lookup, {},
                                     {}, ps, cfg)
    except RuleError as ex:
        assert ex.rule == "rule6"
        assert "guard exhausted" in ex.message.lower()
    else:
        raise AssertionError("expected RuleError on guard exhaustion, none raised")


# --------------------------------------------------------------------------- #
# INVARIANT 4 — plentiful crew: timing unchanged (only contention bites)
# --------------------------------------------------------------------------- #
def _manual_masters(n_helpers):
    machines = {"MD1": Machine(machine_no="MD1", display_name="MD1",
                               machine_type="Manual deburring", available_hrs_per_day=9.5),
                "MD2": Machine(machine_no="MD2", display_name="MD2",
                               machine_type="Manual deburring", available_hrs_per_day=9.5)}
    helpers = [Operator(name=f"HP{i}", preferred_machines_raw="MD1,MD2",
                        machines=["MD1", "MD2"], shift="First shift")
               for i in range(1, n_helpers + 1)]
    routings = {"Y1": Routing("Y1", "", "", "", None,
                              processes=[Process(1, "DEBUR", cycle_time=10, total_time=None,
                                                 suggested_machine="MD1", allotted_machine=None)]),
                "Y2": Routing("Y2", "", "", "", None,
                              processes=[Process(1, "DEBUR", cycle_time=10, total_time=None,
                                                 suggested_machine="MD2", allotted_machine=None)])}
    return Masters(machines=machines, operators=helpers, calendar=WorkCalendar(),
                   routings=routings)


def _timing(sched):
    return sorted((e.machine, e.start, e.end, e.qty) for e in sched)


def test_plentiful_crew_timing_is_independent_of_headcount():
    """Two concurrent single-shift ops. With 2 helpers (just enough) vs 5 helpers
    (abundant) the SCHEDULE (machine + start + end) is byte-identical — the handoff
    logic only changes timing under genuine contention, never when crew is plentiful.
    Every op is a single shift segment (no pause / no handoff)."""
    batches = [_batch("Y1", "B1", 5), _batch("Y2", "B2", 5)]
    a = rule6_allocate.run(list(batches), config=_cfg(), masters=_manual_masters(2))
    b = rule6_allocate.run(list(batches), config=_cfg(), masters=_manual_masters(5))
    assert _timing(a) == _timing(b)
    for sched in (a, b):
        for e in sched:
            assert len(e.op_segments) == 1        # single-shift → one segment, no handoff
    assert_no_operator_double_booked(a)
    assert_within_shift(a, _manual_masters(2), _cfg())


# --------------------------------------------------------------------------- #
# INVARIANT 5 — determinism
# --------------------------------------------------------------------------- #
def test_determinism_same_inputs_same_schedule():
    day = Operator(name="DayOp", preferred_machines_raw="CNC1", machines=["CNC1"],
                   shift="First shift")
    night = Operator(name="NightOp", preferred_machines_raw="CNC1", machines=["CNC1"],
                     shift="Second shift")

    def _run():
        masters = _single_cnc_masters([day, night], {"X": _route("X", 10)})
        return rule6_allocate.run([_batch("X", "B1", 120)], config=_cfg(), masters=masters)

    def _key(sched):
        return [(e.machine, e.start, e.end, e.operator, tuple(e.op_segments)) for e in sched]

    assert _key(_run()) == _key(_run())
