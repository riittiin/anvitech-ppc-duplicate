"""Broadening a qualification must never remove feasibility.

`roster_for_shift` matches over `shop.machining_ids` only, and `scheduler.
_floating_operators` staffs the benches from whoever the matching did NOT take.
So every cross-qualified person was absorbed onto a CNC/VMC, the benches were
left with an empty pool, and `_shift_demand`'s forward routing walk kept
reporting CNC demand for the step BEYOND the starved bench (it assumes upstream
will be manned). That is a stable deadlock: the same roster is computed shift
after shift until the horizon runs out and `schedule` raises `Unschedulable`.

The direction is what makes it a defect rather than a capacity limit: TAKING A
QUALIFICATION AWAY FIXES IT. This shop's admins assign machines to people in a
Settings table, and the 2026-08-07 incident (Sandeep Kumar given CNC4, dropped
from its pool, CNC4 idle with work waiting) established that a Settings
assignment must never be silently punished.

The reproduction below is the reviewer's table, measured. It needs a routing
that RETURNS to a machining machine after a bench — that is what keeps the
forward walk reporting CNC demand forever. A straight CNC -> CNC -> bench ->
bench routing drains its CNC demand once every job has cleared machining, so the
crew is released to the benches on its own and the book merely runs slow: it was
measured OK on all five rows and is NOT the reproduction (see the report).

Every test here was RED before `roster._match_reserving_benches` existed.
"""

import os
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import objective, roster, scheduler
from roster_engine.domain import build_jobs, build_shop
from roster_engine.worktime import ShiftWindow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MACHINING = ("CNC1", "CNC2", "CNC3", "VMC1")
BENCHES = ("MD1", "MW1", "MPK1")
ALL_MACHINES = MACHINING + BENCHES


class _B:
    def __init__(self, key, qty, due, item="ITEM"):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [key], due
        self.process_remaining = None


def _machines():
    out = {mid: Machine(mid, mid, "CNC lathe", available_hrs_per_day=19.5)
           for mid in MACHINING}
    out.update({mid: Machine(mid, mid, "manual", available_hrs_per_day=9.5)
                for mid in BENCHES})
    return out


def _routing():
    """CNC -> bench -> CNC -> bench -> bench. The step AFTER the bench is what
    keeps CNC demand alive while the bench starves."""
    return Routing("ITEM", "d", "", "", None, [
        Process(1, "CNC FIRST SIDE", 4.0, None, None, "CNC1/CNC2"),
        Process(2, "DEBURING", 2.0, None, None, "MD1"),
        Process(3, "CNC SECOND SIDE", 4.0, None, None, "CNC3/VMC1"),
        Process(4, "WASHING", 1.0, None, None, "MW1"),
        Process(5, "PACKING", 1.0, None, None, "MPK1"),
    ])


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0, **kw)


def _op(name, machines, shift="First shift"):
    return Operator(name, "/".join(machines), list(machines), shift)


def _plan(operators, n=6, overlap=1.0):
    """Due dates deliberately sit BEFORE the plan start (08-12), so every order
    finishes late.

    `objective.score` is SYMMETRIC — the owner's rule is that finishing early is
    exactly as bad as finishing late — so on a book that finishes early a faster
    crew scores WORSE, and "adding a qualification never worsens the score" would
    be measuring earliness rather than capacity. In the late regime, which is the
    one this shop actually lives in, score is strictly monotone in completion and
    the property means what it says. (Measured: with the far-future due dates
    this fixture first carried, the generalist crew's makespan 4.15 -> 3.19 d
    read as a score REGRESSION 71.4 -> 106.3, entirely from earliness.)
    """
    masters = Masters(machines=_machines(), routings={"ITEM": _routing()},
                      operators=list(operators), calendar=WorkCalendar())
    batches = [_B(f"B{i}", 30, date(2026, 8, 4) + timedelta(days=i))
               for i in range(n)]
    jobs, _by, _sk = build_jobs(batches, masters)
    plan = scheduler.schedule(jobs, [j.key for j in jobs], build_shop(masters),
                              _cfg(), overlap=overlap)
    return plan, jobs


def _score(operators, n=6):
    plan, jobs = _plan(operators, n=n)
    return objective.score(objective.compute_metrics(plan, jobs, _cfg()), _cfg())


_SPECIALISED = (_op("A", MACHINING), _op("B", BENCHES))
_GENERALISTS = (_op("A", ALL_MACHINES), _op("B", ALL_MACHINES))
_GENERALISTS_PLUS_BENCH = _GENERALISTS + (_op("C", BENCHES),)
_ONE_DEQUALIFIED = (_op("A", ALL_MACHINES), _op("B", BENCHES))
_GENERALISTS_PLUS_NIGHT = _GENERALISTS + (
    _op("C", ALL_MACHINES, shift="Second shift"),)


# --------------------------------------------------------------------------- #
# The reviewer's table — the five rows, measured
# --------------------------------------------------------------------------- #

def test_row1_a_specialised_crew_plans():
    """One person on all 4 machining machines, one on all 3 benches. Nobody is
    cross-qualified, so nothing can be absorbed and this always worked."""
    plan, _jobs = _plan(_SPECIALISED)
    assert plan.placements


def test_row2_the_same_headcount_made_generalist_must_still_plan():
    """THE DEFECT. Same two people, same total coverage, each now qualified on
    all seven machines. Both were taken onto CNCs, MD1 was left with an empty
    pool, and the forward walk kept reporting CNC demand for step 3 — which can
    never open, because step 2 is the starved bench. `Unschedulable` before the
    fix."""
    plan, _jobs = _plan(_GENERALISTS)
    assert plan.placements


def test_row3_a_bench_only_third_person_always_rescued_it():
    """Adding somebody who can ONLY do benches gives the matching nothing to
    absorb, so this worked before the fix too — and must keep working."""
    plan, _jobs = _plan(_GENERALISTS_PLUS_BENCH)
    assert plan.placements


def test_row4_taking_the_cncs_away_from_one_generalist_fixed_it():
    """The wrong direction, pinned. This crew is row 2's with qualifications
    REMOVED from B, and it planned while row 2 did not."""
    plan, _jobs = _plan(_ONE_DEQUALIFIED)
    assert plan.placements


def test_row5_a_second_shift_generalist_does_not_rescue_the_first_shift():
    """The benches are single-shift stations, so a night-shift person can never
    staff them — the first shift deadlocks exactly as in row 2. Pinned because
    it is the row that shows the deadlock is about WHO IS ON THIS SHIFT, not
    about headcount."""
    plan, _jobs = _plan(_GENERALISTS_PLUS_NIGHT)
    assert plan.placements


# --------------------------------------------------------------------------- #
# The property the defect violated
# --------------------------------------------------------------------------- #

def _add(base, name, extra):
    """`base` with `extra` added to `name`'s Settings machine list."""
    out = []
    for o in base:
        if o.name == name:
            merged = tuple(o.machines) + tuple(
                m for m in extra if m not in o.machines)
            out.append(_op(o.name, merged, shift=o.shift))
        else:
            out.append(o)
    return tuple(out)


_MONOTONE_CASES = [
    ("bench person gains every CNC", _SPECIALISED, "B", MACHINING),
    ("bench person gains one CNC", _SPECIALISED, "B", ("CNC1",)),
    ("bench person gains two CNCs", _SPECIALISED, "B", ("CNC1", "CNC3")),
    ("machining person gains every bench", _SPECIALISED, "A", BENCHES),
    ("machining person gains one bench", _SPECIALISED, "A", ("MD1",)),
    ("the third person gains every CNC", _GENERALISTS_PLUS_BENCH, "C", MACHINING),
    ("the third person gains one CNC", _GENERALISTS_PLUS_BENCH, "C", ("CNC2",)),
    ("a de-qualified generalist gets the CNCs back",
     _ONE_DEQUALIFIED, "B", MACHINING),
]


def test_adding_a_qualification_never_makes_a_book_unschedulable():
    """The property, over eight crews. A Settings assignment is the admin
    TELLING the planner something is possible; it may cost the plan nothing and
    it may gain it something, but it can never take an option away.

    2026-08-13 review finding: this used to be `assert True, label` after two
    bare `_plan(...)` calls, non-vacuous only because `_plan` raises
    `Unschedulable` when the fix is reverted — a future `try/except` or
    refactor around either call would make it silently green. Bind the plans
    and assert on them directly, so the assertion itself is what fails."""
    for label, base, name, extra in _MONOTONE_CASES:
        narrow_plan, _jobs = _plan(base)              # the narrower crew plans...
        broad_plan, _jobs = _plan(_add(base, name, extra))
        assert narrow_plan.placements, label
        assert broad_plan.placements, label           # ...so the broader one must too


def test_adding_a_qualification_never_worsens_the_score():
    for label, base, name, extra in _MONOTONE_CASES:
        before = _score(base)
        after = _score(_add(base, name, extra))
        assert after <= before + 1e-9, (
            f"{label}: adding a qualification worsened the plan "
            f"({before:.4f} -> {after:.4f})")


def test_the_generalist_crew_is_at_least_as_good_as_the_specialised_one():
    """The sharpest form of the same property: row 2's crew is row 1's crew with
    strictly more qualifications, so its plan may not be worse. Before the fix
    row 1 scored a real number and row 2 did not exist."""
    assert _score(_GENERALISTS) <= _score(_SPECIALISED) + 1e-9


# --------------------------------------------------------------------------- #
# The residual, measured and bounded — NOT a clean pass
# --------------------------------------------------------------------------- #

_MID_MACHINING = ("CNC1", "CNC2", "CNC3", "VMC1")
_MID_BENCHES = ("MD1", "MW1", "MPK1", "MI1")


def _mid_crew(dinesh=_MID_BENCHES):
    return (_op("Anturam", _MID_MACHINING),
            _op("Bhavesh", _MID_MACHINING),
            _op("Chetan", ("CNC1", "CNC2", "MD1", "MW1")),
            _op("Dinesh", dinesh),
            _op("Eshan", ("CNC3", "VMC1", "MPK1")),
            _op("Farid", _MID_MACHINING, shift="Second shift"),
            _op("Gopal", ("CNC1", "CNC3", "MI1"), shift="Second shift"))


def _mid_plan(operators, n=30):
    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in _MID_MACHINING}
    machines.update({m: Machine(m, m, "manual", available_hrs_per_day=9.5)
                     for m in _MID_BENCHES})
    routings = {
        "P": Routing("P", "d", "", "", None, [
            Process(1, "CNC FIRST SIDE", 3.0, None, None, "CNC1/CNC2"),
            Process(2, "DEBURING", 1.5, None, None, "MD1"),
            Process(3, "CNC SECOND SIDE", 3.0, None, None, "CNC3/VMC1"),
            Process(4, "WASHING", 0.8, None, None, "MW1"),
            Process(5, "INSPECTION", 0.5, None, None, "MI1"),
            Process(6, "PACKING", 0.5, None, None, "MPK1")]),
        "Q": Routing("Q", "d", "", "", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1/CNC2/CNC3"),
            Process(2, "DEBURING", 2.0, None, None, "MD1"),
            Process(3, "PACKING", 0.5, None, None, "MPK1")]),
    }
    masters = Masters(machines=machines, routings=routings,
                      operators=list(operators), calendar=WorkCalendar())
    batches = [_B(f"J{i}", 20 + (i % 5) * 10,
                  date(2026, 8, 6) + timedelta(days=i % 9),
                  item="P" if i % 3 else "Q") for i in range(n)]
    jobs, _by, _sk = build_jobs(batches, masters)
    plan = scheduler.schedule(jobs, [j.key for j in jobs], build_shop(masters),
                              _cfg(), overlap=1.0)
    return objective.score(objective.compute_metrics(plan, jobs, _cfg()), _cfg())


def _straight_plan(operators, n=6):
    """A straight routing — CNC -> CNC -> bench -> bench -> bench, never back to
    a machining machine. Its machining demand DRAINS, so a starved bench is only
    delayed here, never deadlocked: every crew plans at HEAD. It is the book on
    which a reservation can only cost or gain."""
    machines = {mid: Machine(mid, mid, "CNC lathe", available_hrs_per_day=19.5)
                for mid in MACHINING}
    machines.update({mid: Machine(mid, mid, "manual", available_hrs_per_day=9.5)
                     for mid in BENCHES})
    routing = Routing("ITEM", "d", "", "", None, [
        Process(1, "CNC FIRST SIDE", 4.0, None, None, "CNC1/CNC2"),
        Process(2, "CNC SECOND SIDE", 4.0, None, None, "CNC3/VMC1"),
        Process(3, "DEBURING", 2.0, None, None, "MD1"),
        Process(4, "WASHING", 1.0, None, None, "MW1"),
        Process(5, "PACKING", 1.0, None, None, "MPK1")])
    masters = Masters(machines=machines, routings={"ITEM": routing},
                      operators=list(operators), calendar=WorkCalendar())
    batches = [_B(f"B{i}", 30, date(2026, 8, 4) + timedelta(days=i))
               for i in range(n)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return scheduler.schedule(jobs, [j.key for j in jobs], build_shop(masters),
                              _cfg(), overlap=1.0)


def test_the_first_shift_still_runs_two_cncs_at_once_on_a_straight_routing():
    """END TO END, and the one test that catches the `ready` plumbing going
    away. Two generalists, a straight routing: on the first shift the benches
    have only PROJECTED work — every piece is still on a CNC — so both operators
    belong on CNCs and two machining machines must be cutting at the same
    instant.

    If `scheduler._shift_demand` stops reporting the standing figure, or the
    roster stops distinguishing it from the projection, the bench's promise
    takes one of them and this book goes 51 -> 54 late-days, 3.29 -> 4.19 days
    of makespan — a REGRESSION on a book that worked perfectly well before the
    fix. Nothing else in this file notices, because that book still plans."""
    plan = _straight_plan(_GENERALISTS)
    day_one = [p for p in plan.placements
               if p.machine in MACHINING and p.start.date() == date(2026, 8, 12)]
    concurrent = any(a.start < b.end and b.start < a.end
                     for a in day_one for b in day_one
                     if a.machine != b.machine)
    assert concurrent, (
        "only one machining machine ran on day one — a projected bench demand "
        f"took an operator off a CNC that had work standing at it: {day_one}")


def test_the_mid_size_monotonicity_gap_is_closed_but_not_to_zero():
    """HONEST BOUND, not a clean pass. 30 jobs, 4 machining machines, 4 benches,
    7 operators. Broadening the bench-only helper Dinesh with all four machining
    machines is a pure Settings widening, so in a perfectly monotone engine it
    would cost exactly nothing.

    Measured: at HEAD it cost **299 late-days / 9.30 d** against the narrow
    crew's 169 / 4.39 — the same defect in its non-fatal form, a severe slowdown
    rather than an outright `Unschedulable`. With the reservation it is **182 /
    5.12**: the gap is cut from 5.33x the narrow crew's score to 1.24x, but it
    is NOT zero.

    Treating projected bench work as standing work does close it exactly (169 /
    4.39, monotone to the last decimal) — and it costs an ALREADY-WORKING book
    51 -> 54 late-days and 3.29 -> 4.19 d of makespan, which the brief forbids.
    The residual is deliberate; see the task report. This test is the barrier
    that stops it growing back.
    """
    narrow = _mid_plan(_mid_crew())
    broad = _mid_plan(_mid_crew(dinesh=_MID_BENCHES + _MID_MACHINING))
    assert broad < narrow * 1.5, (
        f"the mid-size monotonicity gap grew: {narrow:.2f} -> {broad:.2f} "
        f"({broad / narrow:.2f}x; measured 1.24x, and 5.33x before the fix)")


def test_broadening_the_mid_size_crew_the_other_way_costs_nothing_at_all():
    """The control for the test above: giving a MACHINING-only person the
    benches touches nothing, because the bench pool was never the thing under
    pressure. Exactly equal, not merely close."""
    narrow = _mid_plan(_mid_crew())
    broad = _mid_plan(_add(_mid_crew(), "Anturam", _MID_BENCHES))
    assert broad == narrow


# --------------------------------------------------------------------------- #
# The mechanism, at the roster's own level
# --------------------------------------------------------------------------- #

def _shop(operators, machines=None, absent=None):
    return build_shop(
        Masters(machines=machines or _machines(), operators=list(operators),
                calendar=WorkCalendar()), absent or {})


def _win(shift="first"):
    if shift == "first":
        return ShiftWindow(date(2026, 8, 12), "first",
                           datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 19, 0))
    return ShiftWindow(date(2026, 8, 12), "second",
                       datetime(2026, 8, 12, 19, 0), datetime(2026, 8, 13, 5, 0))


def test_a_bench_with_work_keeps_one_qualified_person_off_the_machining_match():
    """Two cross-qualified people, four machining machines all with more demand
    than the bench. Unreserved, the matching takes both — MD1's pool is then
    empty and its 360 minutes cannot be run by anybody."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert len(got) == 1, f"both operators were absorbed onto machining: {got}"
    assert set(got.values()) < {"A", "B"}


def test_a_bench_with_no_work_reserves_nobody():
    """The reservation is a response to real demand, not a standing tax on
    cross-qualified people: with the bench idle both operators stay on CNCs."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 0.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 0.0})
    assert len(got) == 2


def test_a_bench_already_covered_by_a_free_helper_reserves_nobody():
    """C can only do benches, so MD1's pool is non-empty whatever the matching
    does. Nothing is taken off machining — this is why row 3 was, and stays,
    unaffected."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES),
                  _op("C", BENCHES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert len(got) == 2
    assert "C" not in got.values()


def test_a_bench_nobody_is_qualified_for_reserves_nobody():
    """MD1 has work and no operator in Settings can touch it. That is a real
    constraint the app reports on its own (MACHINE_NO_OPERATOR); crippling the
    machining roster over it would help nothing."""
    shop = _shop([_op("A", MACHINING), _op("B", MACHINING)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert len(got) == 2


def test_a_bench_that_does_not_run_this_shift_reserves_nobody():
    """The benches here are single-shift stations. On the second shift they are
    shut, so their demand cannot be a reason to take a night operator off a
    CNC."""
    shop = _shop([_op("A", ALL_MACHINES, shift="Second shift"),
                  _op("B", ALL_MACHINES, shift="Second shift")])
    got = roster.roster_for_shift(
        _win("second"), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert len(got) == 2


def test_an_absent_helper_cannot_be_the_reserved_one():
    """Eligibility for the reservation is the SAME rule the matching uses —
    Settings qualification, shift, and presence. Reserving somebody who is on
    leave would leave the bench exactly as starved while costing a CNC."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES),
                  _op("C", BENCHES)],
                 absent={"C": [(datetime(2026, 8, 12, 0, 0),
                                datetime(2026, 8, 13, 0, 0))]})
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert len(got) == 1                  # C is away, so A or B must cover MD1


def test_every_starved_bench_gets_its_own_reserved_person():
    """One reservation is not enough when two benches have work and no single
    person covers both: the constraint is per-bench, so each uncovered bench
    costs one more person off machining. Here A is MD1's only hope and B is
    MW1's, so both are released and only the machining-only C is rostered."""
    shop = _shop([_op("A", ("CNC1", "CNC2", "MD1")),
                  _op("B", ("CNC3", "VMC1", "MW1")),
                  _op("C", MACHINING)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0, "MW1": 360.0}, {}, {},
        ready={"CNC1": 1260.0, "CNC3": 1260.0,
               "MD1": 360.0, "MW1": 360.0})
    assert list(got.values()) == ["C"]


def test_the_reservation_never_darkens_machining_that_has_standing_work():
    """THE GUARD, and the reason reserving against a projection is safe. Same two
    benches, but now the only two people are also the only two who can run a CNC:
    releasing both would leave every machining machine dark while CNC1 and CNC3
    have work physically waiting — and, in a real routing, the machining work is
    what FEEDS the benches, so the reservation would be waiting for work it had
    just stopped anyone from making. One bench is covered; machining keeps a
    machine that has standing work."""
    shop = _shop([_op("A", ("CNC1", "CNC2", "MD1")),
                  _op("B", ("CNC3", "VMC1", "MW1"))])
    ready = {"CNC1": 1260.0, "CNC3": 1260.0, "MD1": 360.0, "MW1": 360.0}
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0, "MW1": 360.0}, {}, {}, ready=ready)
    assert len(got) == 1
    assert ready.get(next(iter(got)), 0.0) > 0.0


def test_projected_bench_work_never_takes_a_seat_that_has_standing_work():
    """The WEAK reservation. MD1's minutes are only projected — its feeder has
    not run — while both CNCs have work physically waiting. Giving a CNC up for
    a promise is what a reservation must not do: measured, it cost an
    already-working book 51 -> 54 late-days and 3.29 -> 4.19 days of makespan,
    because a straight CNC -> CNC -> bench routing projects its bench work from
    the very first shift."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "MD1": 360.0}, {}, {},
        ready={"CNC1": 1260.0, "CNC2": 1260.0})
    assert len(got) == 2


def test_projected_bench_work_does_take_a_seat_that_is_itself_speculative():
    """The other half of the WEAK reservation, and why refusing projections
    outright is not good enough either. CNC3 has nothing standing at it — the
    matching would man it on the same projection MD1 is claiming — so vacating
    it costs the plan nothing, and it buys a helper who can take the deburring
    the moment it is released mid-shift. Without this, a third generalist sat
    locked to an idle CNC for a whole window while MD1 went unstaffed."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES),
                  _op("C", ALL_MACHINES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "MD1": 360.0}, {}, {},
        ready={"CNC1": 1260.0, "CNC2": 1260.0})
    assert sorted(got) == ["CNC1", "CNC2"]


def test_a_projection_alone_never_takes_the_last_machining_operator():
    """The same guard, at its sharpest, and the deadlock this fix would otherwise
    have created in the opposite direction: one operator, one CNC with work
    standing at it, one bench whose 900 minutes are only PROJECTED (its feeder is
    the CNC). Reserving him would darken the CNC, so the bench work he was
    reserved for could never arrive."""
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    shop = _shop([_op("Anturam", ("CNC1", "MD1"))], machines=machines)
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 100.0, "MD1": 900.0}, {}, {},
        ready={"CNC1": 100.0})
    assert got == {"CNC1": "Anturam"}


def test_an_explicit_empty_ready_is_not_silently_treated_as_none():
    """2026-08-13 review finding. ``ready=None`` means a caller supplied NO
    information and gets the conservative fallback (everything demanded is
    read as standing, so the guard above protects Anturam's only seat exactly
    as ``ready={"CNC1": 100.0}`` does in the test above — both feed the guard
    the same CNC1=100 figure). ``ready={}``, in contrast, is an INFORMED claim
    that nothing anywhere is standing right now, not even CNC1 itself — the
    same 900-minute-projected-bench setup, but now CNC1's own demand is also
    only projected. Taken at its word, the seat is exactly as speculative as
    the bench's promise, so the guard has nothing left to protect and the
    reservation may take it.

    ``(ready if ready else demand)`` could not tell ``None`` and ``{}`` apart —
    both are falsy in Python — so an explicit "nothing is standing anywhere"
    silently fell back to ``demand`` and inherited the conservative guard,
    which is exactly mutation M3 (the reviewer measured it costs an
    already-working book 51 -> 54 late-days). This is the same defect as the
    reproduction above, only visible with the last operator on the last
    machine, where the guard's own protection is the thing being bypassed.
    """
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    shop = _shop([_op("Anturam", ("CNC1", "MD1"))], machines=machines)
    demand = {"CNC1": 100.0, "MD1": 900.0}

    conservative = roster.roster_for_shift(_win(), shop, demand, {}, {},
                                           ready=None)
    assert conservative == {"CNC1": "Anturam"}, (
        "ready=None must stay the CONSERVATIVE fallback (= demand), which "
        f"protects the only operator's only seat: got {conservative}")

    informed = roster.roster_for_shift(_win(), shop, demand, {}, {},
                                       ready={})
    assert informed != conservative, (
        "an explicit empty ready was silently coerced to the same "
        f"conservative fallback as ready=None: {informed}")


def test_a_part_in_the_chuck_is_given_up_last():
    """A carry-over is worth CARRY_BONUS, so it is the most expensive thing the
    reservation could take and is therefore taken only when nothing else can be.
    B is on a carry-over; A is the one released to the bench."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {"CNC1": "B1"}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert list(got) == ["CNC1"]          # the chuck keeps its machine manned


def test_the_reservation_is_not_a_bench_roster():
    """Rule 1 binds CNC/VMC only. The reserved person is simply left OUT of the
    machining match — they are never returned as manning a bench, or the
    scheduler's per-interval bench staffing (worth ~50% of late-days) would be
    replaced by a one-person-per-shift lock."""
    shop = _shop([_op("A", ALL_MACHINES), _op("B", ALL_MACHINES)])
    got = roster.roster_for_shift(
        _win(), shop,
        {"CNC1": 1260.0, "CNC2": 1260.0, "CNC3": 1260.0, "VMC1": 1260.0,
         "MD1": 360.0}, {}, {}, ready={"CNC1": 1260.0, "MD1": 360.0})
    assert set(got) <= set(MACHINING)


# --------------------------------------------------------------------------- #
# The measured bench behaviour must survive
# --------------------------------------------------------------------------- #

def test_one_helper_still_serves_two_benches_at_different_times():
    """The reservation must not have become a lock. One helper, two benches, one
    job: the same person does the deburring and then the packing, in one shift,
    and is never on both at once."""
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
        "MPK1": Machine("MPK1", "MPK 1", "manual", available_hrs_per_day=9.5),
    }
    processes = [Process(1, "CNC FIRST SIDE", 2.0, None, None, "CNC1"),
                 Process(2, "DEBURING", 2.0, None, None, "MD1"),
                 Process(3, "PACKING", 1.0, None, None, "MPK1")]
    masters = Masters(machines=machines,
                      routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                                processes)},
                      operators=[_op("A", ("CNC1",)), _op("H", ("MD1", "MPK1"))],
                      calendar=WorkCalendar())
    jobs, _by, _sk = build_jobs([_B("B1", 20, date(2026, 12, 1))], masters)
    plan = scheduler.schedule(jobs, ["B1"], build_shop(masters), _cfg(),
                              overlap=1.0)
    on = {p.machine: p for p in plan.placements if p.machine}
    assert {s[2] for s in on["MD1"].segments} == {"H"}
    assert {s[2] for s in on["MPK1"].segments} == {"H"}
    assert on["MD1"].end <= on["MPK1"].start


def test_nobody_is_in_two_places_at_one_instant():
    """Over the whole deadlock book with the generalist crew: every operator's
    segments, across every machine and both staffing mechanisms, are pairwise
    disjoint."""
    plan, _jobs = _plan(_GENERALISTS)
    busy: dict = {}
    for p in plan.placements:
        for start, end, who in p.segments:
            busy.setdefault(who, []).append((start, end, p.machine, p.job_key))
    for who, spans in busy.items():
        spans.sort()
        for (s1, e1, m1, j1), (s2, e2, m2, j2) in zip(spans, spans[1:]):
            assert s2 >= e1, (
                f"{who} is on {m1}/{j1} until {e1} and on {m2}/{j2} from {s2}")


def test_one_operator_per_machining_machine_per_shift_still_holds():
    """Rule 1. Within one shift window a machining machine's segments all name
    the same person."""
    plan, _jobs = _plan(_GENERALISTS)
    for p in plan.placements:
        if p.machine not in MACHINING:
            continue
        by_window: dict = {}
        for start, end, who in p.segments:
            # 08:00-19:00 first, 19:00-05:00 second: the shift a segment sits in
            key = (start.date(), "first" if 8 <= start.hour < 19 else "second")
            by_window.setdefault(key, set()).add(who)
        for key, names in by_window.items():
            assert len(names) == 1, f"{p.machine} {key} was manned by {names}"


def test_no_operation_is_interrupted_by_another_job():
    plan, _jobs = _plan(_GENERALISTS)
    by_machine: dict = {}
    for p in plan.placements:
        if p.machine:
            by_machine.setdefault(p.machine, []).append(p)
    for mid, items in by_machine.items():
        items.sort(key=lambda p: p.start)
        for first, second in zip(items, items[1:]):
            assert second.start >= first.end, f"{mid} interleaved two jobs"


def test_quantities_come_from_job_qty_for():
    plan, jobs = _plan(_GENERALISTS)
    by_key = {j.key: j for j in jobs}
    for p in plan.placements:
        assert p.qty == int(max(by_key[p.job_key].qty_for(p.op_seq), 0))


_HASH_SEED_SCRIPT = textwrap.dedent("""
    from datetime import date, timedelta

    from engine.config import Config
    from engine.models import (Machine, Masters, Operator, Process, Routing,
                               WorkCalendar)
    from roster_engine import scheduler
    from roster_engine.domain import build_jobs, build_shop

    MACHINING = ("CNC1", "CNC2", "CNC3", "VMC1")
    BENCHES = ("MD1", "MW1", "MPK1")
    ALL_MACHINES = MACHINING + BENCHES


    class B:
        def __init__(self, key, qty, due):
            self.batch_id, self.item_code, self.qty = key, "ITEM", qty
            self.so_refs, self.delivery_date = [key], due
            self.process_remaining = None


    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in MACHINING}
    machines.update({m: Machine(m, m, "manual", available_hrs_per_day=9.5)
                     for m in BENCHES})
    processes = [Process(1, "CNC FIRST SIDE", 4.0, None, None, "CNC1/CNC2"),
                 Process(2, "DEBURING", 2.0, None, None, "MD1"),
                 Process(3, "CNC SECOND SIDE", 4.0, None, None, "CNC3/VMC1"),
                 Process(4, "WASHING", 1.0, None, None, "MW1"),
                 Process(5, "PACKING", 1.0, None, None, "MPK1")]
    operators = [Operator(n, "/".join(ALL_MACHINES), list(ALL_MACHINES),
                          "First shift") for n in ("A", "B")]
    masters = Masters(machines=machines,
                      routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                                processes)},
                      operators=operators, calendar=WorkCalendar())
    batches = [B("B%d" % i, 30, date(2026, 8, 20) + timedelta(days=i))
               for i in range(6)]
    jobs, _bk, _sk = build_jobs(batches, masters)
    plan = scheduler.schedule(
        jobs, [j.key for j in jobs], build_shop(masters),
        Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0),
        overlap=1.0)
    for p in sorted(plan.placements, key=lambda x: (x.job_key, x.op_seq)):
        print(p.job_key, p.op_seq, p.machine, p.start, p.end, p.segments)
""")


def test_the_reserved_plan_is_deterministic_across_hash_seeds():
    """The reservation walks benches and operator pools — both of which are
    naturally sets/dicts. Only a fresh interpreter with a different string hash
    seed re-orders those, which is what actually varies between the optimizer's
    worker processes."""
    outputs = set()
    for seed in ("0", "1", "2", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run([sys.executable, "-c", _HASH_SEED_SCRIPT],
                                cwd=REPO_ROOT, env=env, capture_output=True,
                                text=True, check=True)
        outputs.add(result.stdout)
    assert len(outputs) == 1, f"plan varied across PYTHONHASHSEED: {outputs}"
    assert outputs.pop().strip()          # non-vacuous: it really planned
