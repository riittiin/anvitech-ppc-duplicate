"""The owner's objective — total late-days first, then the most even spread —
and ``solve_book``, the composition that finally runs the whole engine.

Two layers of test, deliberately:

* the first four are PURE ``objective`` tests on a bare ``CpModel``. They pin the
  encoding (exact integer squares, the strict cap, the slack knob) on arithmetic
  that has nothing to do with a shop, because that is the only place the
  encoding is visible on its own.
* everything after ``solve_book`` appears is a SOLVED-SCHEDULE test. A fairness
  test that passes by restating the encoding is worthless; the forced-choice
  book below is a real solve of a real (tiny) shop where the total late-days is
  provably the same either way and only the DISTRIBUTION can differ.
"""

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("pyjobshop")

from ortools.sat.python import cp_model as ortools_cp

from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)
from cp_engine import objective, solve

PLAN_START = datetime(2026, 8, 12, 8, 0)     # a Wednesday; Thursday is the off day

# The shift grid these tests reason in, minutes from PLAN_START. Only Thursday is
# off, so a first-shift-only station runs 0..660 (Wed), 2880..3540 (Fri),
# 4320..4980 (Sat)...  A two-shift machine adds 660..1260, 3540..4140, ...
#
# ``model._due_minutes`` puts the due instant at the LAST minute of the delivery
# date, so a delivery date of 12-08 is minute 960 and every further day is +1440.

_CNC1 = Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)
_MD1 = Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = None


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0, **kw)


def _masters(routings, operators, machines=None):
    machines = machines or {"CNC1": _CNC1, "MD1": _MD1}
    return Masters(machines=machines, routings=routings,
                   operators=list(operators), calendar=WorkCalendar())


def _op(name, machines, shift="First shift"):
    return Operator(name, "/".join(machines), list(machines), shift)


def _proc(machine, cycle=5.0, name="CNC FIRST SIDE", seq=1, suggested=None):
    """Process(seq, name, cycle, total, SUGGESTED, ALLOTTED) — suggested first."""
    return Process(seq, name, cycle, None, suggested, machine)


def _solve(masters, batches, **kw):
    kw.setdefault("time_limit", 30)
    kw.setdefault("horizon_days", 20)
    kw.setdefault("num_workers", 1)
    return solve.solve_book(batches, masters, _cfg(), PLAN_START, **kw)


# --------------------------------------------------------------------------- #
# The encoding, on a bare model
# --------------------------------------------------------------------------- #

def test_squares_are_exact_integers_via_the_integer_chords():
    """Sum of D^2 is encoded with linear lower-bounding lines, not
    add_multiplication_equality, so the model stays linear. At integer D the
    tightest line IS D^2, and minimisation selects it.

    Swept over the WHOLE domain including both ends, because the encoding's
    correctness is an end-of-range property: each line is the chord through
    ``(k, k^2)`` and ``(k+1, (k+1)^2)``, so the last one carries d = CAP_DAYS.
    Drop it and 60^2 comes back as 3,598."""
    for value in range(0, objective.CAP_DAYS + 1):
        model = ortools_cp.CpModel()
        d = model.new_int_var(value, value, "d")
        sq = objective._square(model, d, "sq")
        model.minimize(sq)
        solver = ortools_cp.CpSolver()
        solver.parameters.num_workers = 1
        assert solver.solve(model) in (ortools_cp.OPTIMAL, ortools_cp.FEASIBLE)
        assert solver.value(sq) == value * value


def test_fairness_spreads_lateness_when_the_total_is_unchanged():
    """The owner's example. Ten orders sharing 100 unavoidable late-days: an even
    split scores 1,000, nine-slightly-late-plus-one-disaster scores 6,760. Same
    total, so phase 1 is indifferent and phase 2 decides.

    Why squared is exactly the right knob and not an arbitrary convex pick: with
    the total held fixed, Var(D) = (sum D^2)/n - ((sum D)/n)^2 and the second term
    is a constant, so minimising sum D^2 IS minimising the variance."""
    model = ortools_cp.CpModel()
    days = {f"J{i}": model.new_int_var(0, objective.CAP_DAYS, f"D{i}")
            for i in range(10)}
    model.add(sum(days.values()) == 100)
    objective.phase_two(model, days, total_star=100, slack_days=0)
    solver = ortools_cp.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) in (ortools_cp.OPTIMAL, ortools_cp.FEASIBLE)
    got = sorted(solver.value(v) for v in days.values())
    assert got == [10] * 10


def test_fairness_never_buys_evenness_with_late_days():
    """slack_days defaults to 0, so phase 2 is a STRICT tie-break. It may not
    raise the total by even one day — b7beb18 (2026-08-13) made the on-time term
    linear precisely to stop the objective spreading at the total's expense."""
    model = ortools_cp.CpModel()
    days = {"A": model.new_int_var(0, 100, "A"), "B": model.new_int_var(0, 100, "B")}
    # An even split is available only at a HIGHER total: (0,10) totals 10,
    # (6,6) totals 12. The cap must keep the uneven, cheaper plan.
    model.add_allowed_assignments([days["A"], days["B"]], [(0, 10), (6, 6)])
    objective.phase_two(model, days, total_star=10, slack_days=0)
    solver = ortools_cp.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) in (ortools_cp.OPTIMAL, ortools_cp.FEASIBLE)
    assert (solver.value(days["A"]), solver.value(days["B"])) == (0, 10)


def test_slack_lets_the_owner_buy_evenness_when_he_asks_for_it():
    """The same book with two days of slack: now the even plan is reachable and
    phase 2 takes it. The knob exists so the trade-off is a config change rather
    than a redesign."""
    model = ortools_cp.CpModel()
    days = {"A": model.new_int_var(0, 100, "A"), "B": model.new_int_var(0, 100, "B")}
    model.add_allowed_assignments([days["A"], days["B"]], [(0, 10), (6, 6)])
    objective.phase_two(model, days, total_star=10, slack_days=2)
    solver = ortools_cp.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) in (ortools_cp.OPTIMAL, ortools_cp.FEASIBLE)
    assert (solver.value(days["A"]), solver.value(days["B"])) == (6, 6)


# --------------------------------------------------------------------------- #
# Days late, on a solved shop
# --------------------------------------------------------------------------- #

def _one_bench_step(cycle=1.0):
    return _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("MD1", cycle=cycle, name="DEBURING")])},
        [_op("Anturam", ["MD1"])])


def test_an_early_order_is_never_credited():
    """D is TARDINESS, not lateness: finishing early contributes nothing. The
    symmetric term in the incumbent ``optimizer.score`` is exactly what made the
    app reject a plan 86 late-days better."""
    res = _solve(_one_bench_step(), [_B("B1", "A", 60, due=date(2026, 9, 30))])
    assert res.status_ok
    assert res.total_late_days == 0.0
    assert res.spread == 0.0
    # It really did finish early — a vacuous zero would pass otherwise.
    assert res.completion["B1"].date() < date(2026, 9, 30)


def test_an_order_that_is_late_is_counted_in_whole_days():
    """660 minutes of bench work from 08:00 Wednesday ends at 19:00 Wednesday.
    Due the day before, that is one day late — the app's own
    ``(completion.date() - due_date).days``."""
    res = _solve(_one_bench_step(), [_B("B1", "A", 660, due=date(2026, 8, 11))])
    assert res.status_ok
    assert res.task_window("B1", 1) == (0, 660)
    assert res.completion["B1"] == PLAN_START + timedelta(minutes=660)
    assert res.total_late_days == 1.0


def test_an_order_past_the_cap_does_not_make_the_model_infeasible():
    """The cap is on the PENALTY, never on the completion.

    Written as ``0 <= D <= 60`` with ``D * 1440 >= end - due`` — the shape the
    brief carried — the upper bound becomes a hard deadline: no order may finish
    more than 60 days after its delivery date, and a book with anything older
    than that on it has NO PLAN AT ALL. Overdue orders are the normal state of
    this shop's book, so that is not a corner case.

    So D is ``min(true tardiness, CAP_DAYS)``: the search still stops chasing a
    hopeless order past 60 days, and the plan still exists."""
    res = _solve(_one_bench_step(), [_B("B1", "A", 660, due=date(2026, 1, 1))])
    assert res.status_ok
    assert res.total_late_days == float(objective.CAP_DAYS)
    # ...and the true figure is reported rather than silently replaced by 60.
    assert res.stats["uncapped_total_late_days"] > objective.CAP_DAYS


def test_an_undated_order_gets_no_late_day_variable():
    """An order with no delivery date cannot be judged on-time or late, and
    recording 0.0 would claim a perfect landing."""
    res = _solve(_one_bench_step(), [_B("B1", "A", 60, due=None)])
    assert res.status_ok
    assert res.total_late_days == 0.0
    assert res.stats["dated_jobs"] == 0
    assert res.stats["jobs"] == 1


# --------------------------------------------------------------------------- #
# Fairness, from a real solve
# --------------------------------------------------------------------------- #

def _forced_choice():
    """A book whose TOTAL late-days is the same either way, and whose spread is
    not. This is the whole feature in one fixture.

    One bench station, one first-shift operator, two 660-minute batches: exactly
    one full shift each. The station runs 0..660 (Wednesday) and 2880..3540
    (Friday — Thursday is off), so the two completions are pinned at minute 660
    and minute 3540 whatever the solver does. Only WHICH batch gets which is
    free.

      B_EARLY is due 12-08 (minute 960):  finishing at  660 -> 0 days late
                                          finishing at 3540 -> 2 days late
      B_LATE  is due 11-08 (minute -480): finishing at  660 -> 1 day  late
                                          finishing at 3540 -> 3 days late

    So the two plans are (0, 3) and (1, 2). **Both total 3** — phase 1 cannot
    tell them apart, and 3 is the proven minimum. Phase 2 must return (1, 2),
    which scores 5 against the disaster plan's 9.
    """
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("MD1", cycle=1.0, name="DEBURING")])},
        [_op("Anturam", ["MD1"])])
    # Deliberately listed disaster-first: the order the batches arrive in is the
    # order the model builds them in, and a phase-1-only solver has no reason to
    # move off the first plan it finds.
    return masters, [_B("EARLY", "A", 660, due=date(2026, 8, 12)),
                     _B("LATE", "A", 660, due=date(2026, 8, 11))]


def test_the_plan_spreads_lateness_when_the_total_cannot_be_improved():
    """The heart of the feature, measured on a schedule rather than on
    arithmetic that restates the encoding.

    Both plans cost the shop three late-days. The fair one puts one day on each
    order; the other lands a clean order and blows the second to three. Ten
    orders ten days late scores 1,000; nine orders two days plus one order
    eighty-two scores 6,760 at the identical total — this is that, in miniature.
    """
    masters, batches = _forced_choice()
    res = _solve(masters, batches)
    assert res.status_ok

    # The completions really are pinned, so the ONLY freedom is who gets which.
    assert sorted(res.task_window(k, 1)[1] for k in ("EARLY", "LATE")) == [
        660, 3540]
    assert res.total_late_days == 3.0          # the proven minimum, both ways
    assert res.spread == 5.0                   # (1,2), not (0,3) which is 9
    assert sorted(res.days_late.values()) == [1, 2]
    # ...and it is the ALREADY-overdue order that went first.
    assert res.days_late["LATE"] == 1 and res.days_late["EARLY"] == 2


def test_phase_two_never_raises_the_total_on_a_real_book():
    """The strict-tie-break rule, at solve level. Whatever phase 2 does to the
    distribution, the total it hands back is the total phase 1 proved."""
    masters, batches = _forced_choice()
    res = _solve(masters, batches)
    assert res.status_ok
    assert res.total_late_days == res.stats["phase_one_total"]


# --------------------------------------------------------------------------- #
# The five obligations solve_book carries
# --------------------------------------------------------------------------- #

def _unstaffable_second_shift():
    """2,090 minutes of cutting on a two-shift CNC with only a first-shift
    operator. Under E1 (hold=False) the dark second shift BLOCKS the machine, so
    the work can never fit inside a 660-minute day and there is no plan."""
    return _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Narayan", ["CNC1"])]), [_B("B1", "A", 400)]


def test_the_horizon_is_closed_so_unstaffable_work_cannot_escape_past_it():
    """``windows.machine_breaks`` describes time only up to the horizon. Past the
    last shift a machine has no breaks and no unstaffed shifts at all, so a
    solver handed work it cannot staff parks it out there and reports OPTIMAL —
    measured at minute 28,620 on this very book. The objective would then be
    measured against a schedule that cannot happen.

    Both test harnesses cap it locally and flagged the real fix as solve_book's.
    """
    masters, batches = _unstaffable_second_shift()
    assert not _solve(masters, batches,
                      hold_across_unmanned_shift=False).status_ok

    # The control: the same book IS schedulable when the part may be held across
    # the dark shift, and every minute of it lands inside the calendar.
    res = _solve(masters, batches, hold_across_unmanned_shift=True)
    assert res.status_ok
    horizon = res.shifts[-1].end
    assert max(end for _s, end in res.windows.values()) <= horizon


def test_solve_book_refuses_to_solve_a_model_whose_credit_modes_are_unlinked():
    """Obligation 2. ``model.build`` creates the setup-free modes and NOTHING in
    the model stops the solver selecting one — ``rules.add_setup_credit`` is what
    ties each to "the machine's previous job was the same part". Skip the call
    and every member of every same-part group takes its free mode
    unconditionally: the plan invents 90 minutes of CNC capacity per affected
    task, with no exception, no failing test and no report row anywhere.

    So the flag is CHECKED before the solve, loudly. (A ``raise``, not an
    ``assert``: ``python -O`` strips assertions, and this one guards capacity
    that does not exist.)"""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    batches = [_B("B1", "A", 60), _B("B2", "A", 60)]

    original = solve.rules.add_setup_credit
    try:
        solve.rules.add_setup_credit = lambda *a, **k: None
        with pytest.raises(RuntimeError, match="setup"):
            _solve(masters, batches)
    finally:
        solve.rules.add_setup_credit = original


def test_solve_book_really_grants_the_rule_4_credit():
    """The positive half of the same obligation: proving the guard exists proves
    nothing about the call being made. Two batches of the SAME item and process
    on one machine cost ONE setup, not two."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    res = _solve(masters, [_B("B1", "A", 60), _B("B2", "A", 60)])
    assert res.status_ok
    assert res.machine_busy_minutes("CNC1") == 90 + 60 + 60


def test_op_qty_reports_what_the_step_owes_at_batch_level():
    """``op_qty`` is the pieces a step really ran. Derived from the BATCH's
    per-step remainder (``Job.qty_for``), never from a per-SO-line number — the
    2026-08-11 escalation was a frozen row that laid one clubbed line's 88 pieces
    and left the other line's 281 in no plan at all."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("MD1", cycle=1.0, name="DEBURING", seq=1),
            _proc("MD1", cycle=1.0, name="INSPECTION", seq=2)])},
        [_op("Anturam", ["MD1"])])
    batch = _B("B1", "A", 60)
    batch.process_remaining = {1: 10, 2: 55}
    res = _solve(masters, [batch])
    assert res.status_ok
    assert res.op_qty("B1", 1) == 10
    assert res.op_qty("B1", 2) == 55
    assert res.task_window("B1", 1)[1] - res.task_window("B1", 1)[0] == 10


# --------------------------------------------------------------------------- #
# Rule 3's k, under a tardiness objective
# --------------------------------------------------------------------------- #

def test_the_release_takes_its_loosest_legal_value_under_this_objective_too():
    """Obligation 3, answered rather than assumed.

    ``k`` appears ONLY in two lower bounds on the successor's start, both
    monotonically increasing in k. Nothing forces a successor to start AT its
    lower bound, so every schedule legal at some k is also legal at k = 1: the
    feasible set at k = 1 contains every other, and no objective whatever can
    make k > 1 strictly pay. It is a decision variable with no decision.

    Under this objective, then, Rule 3 is "release after one piece" — maximum
    overlap, always. That is a real finding about the owner's overlap rule and
    not a failure: the CP engine does not tune overlap the way the incumbent's
    contest did, it simply always takes the loosest legal release and lets the
    machine calendars and the objective decide the rest.

    The fixture is the shape that could most plausibly have paid: a fast
    successor whose machine another, more urgent order also wants."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=2.0, seq=1),
            _proc("MD1", cycle=0.5, name="DEBURING", seq=2)]),
         "B": Routing("B", "b", "c", "rm", None,
                      [_proc("MD1", cycle=1.0, name="DEBURING")])},
        [_op("Narayan", ["CNC1"]), _op("Anturam", ["MD1"])])
    res = _solve(masters, [_B("SLOW", "A", 100, due=date(2026, 8, 20)),
                           _B("URGENT", "B", 100, due=date(2026, 8, 12))])
    assert res.status_ok
    assert res.genome["cp_overlap_of"]["SLOW"] == 1


# --------------------------------------------------------------------------- #
# What solve_book hands on
# --------------------------------------------------------------------------- #

def test_the_genome_carries_every_key_the_replay_needs():
    masters, batches = _forced_choice()
    res = _solve(masters, batches)
    assert res.status_ok
    from cp_engine import genome
    assert set(res.genome) == set(genome.KEYS)
    assert res.genome["cp_machine_of"][("LATE", 1)] == "MD1"
    assert res.genome["cp_completion"]["LATE"] == "2026-08-12"
    # It round-trips through the store's own flattening unchanged.
    assert genome.from_json(genome.to_json(res.genome)) == res.genome


def test_the_same_inputs_and_seed_give_the_same_genome():
    masters, batches = _forced_choice()
    first = _solve(masters, batches)
    second = _solve(masters, batches)
    assert first.status_ok and second.status_ok
    assert first.genome == second.genome
    assert (first.total_late_days, first.spread) == (second.total_late_days,
                                                     second.spread)


def test_frozen_work_is_refused_rather_than_silently_ignored():
    """Task 9 owns the frozen set. Until it lands, a caller that passes one must
    be told — a frozen op quietly dropped is the 2026-08-11 class of bug."""
    masters, batches = _forced_choice()
    with pytest.raises(NotImplementedError):
        _solve(masters, batches, frozen=[{"job": "LATE", "op_seq": 1}])


def test_the_stats_carry_the_model_size_the_spike_measures():
    masters, batches = _forced_choice()
    res = _solve(masters, batches)
    for key in ("variables", "booleans", "constraints", "tasks", "jobs",
                "shifts", "phase_one_runtime", "phase_two_runtime"):
        assert key in res.stats, key
    assert res.stats["booleans"] > 0
    assert res.stats["constraints"] > 0


def test_progress_is_reported_from_both_phases():
    """The contest UI polls a progress line. Both phases report, and each says
    WHICH phase it is — "still improving the total" and "now evening it out" are
    different things to look at on a screen."""
    seen = []
    masters, batches = _forced_choice()
    res = _solve(masters, batches, on_progress=seen.append)
    assert res.status_ok
    assert {row["phase"] for row in seen} == {1, 2}
    assert all("objective" in row and "bound" in row for row in seen)


def test_a_cancelled_search_keeps_the_best_plan_it_had():
    """Stop must not throw the work away — the incumbent engine's Stop keeps the
    best-so-far, and so does this one: a usable plan comes back, with a genome
    the replay can consume.

    Cancelling on the FIRST solution costs optimality, and the numbers say so
    out loud: this book's optimum is 3 late-days and stopping at once returns 4.
    That gap is what proves the cancellation really fired rather than the test
    watching a full search finish."""
    masters, batches = _forced_choice()
    res = _solve(masters, batches, should_cancel=lambda: True)
    assert res.status_ok
    assert res.status == "FEASIBLE"                  # not OPTIMAL — it stopped
    assert res.total_late_days == 4.0
    assert res.genome["cp_machine_of"][("LATE", 1)] == "MD1"
    assert _solve(masters, batches).total_late_days == 3.0    # the control


def test_an_unroutable_item_is_reported_and_the_rest_still_plans():
    """RULES.md's fail-localized rule: an item with no recipe is skipped and
    named, never raised, and never allowed to take the book down with it."""
    masters, batches = _forced_choice()
    res = _solve(masters, batches + [_B("GHOST", "NO-SUCH-ITEM", 10)])
    assert res.status_ok
    assert res.stats["skipped_item_codes"] == ["NO-SUCH-ITEM"]
    assert res.total_late_days == 3.0
