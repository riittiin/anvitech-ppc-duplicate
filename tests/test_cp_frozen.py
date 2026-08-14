"""Frozen in-progress work, at the MODEL level (spec §5.5).

A frozen row pins WHERE and WHEN, never HOW MUCH. The 2026-08-11 director
escalation is the reason this file exists: a row's ``remaining_qty`` is ONE SO
LINE's remainder while the operation it pins belongs to the BATCH Rule 1 clubbed
that line into, and reading it left 281 pieces of a clubbed order in no plan at
all.

**This fixture family passes vacuously by default** (CLAUDE.md, 2026-08-09), so
every pinning assertion here is paired with a NON-VACUITY test that shows what
the same book does with no pin — **parametrized over the same two encodings, so
the pairing holds leg for leg.** ``PINNED`` is deliberately the machine, and
``RESUMING`` the person, that the unpinned solve does NOT pick — both MEASURED,
not assumed, and both wrong on the first draft of this file (the solver freely
rosters S on CNC1 and N on CNC4, so pinning S there would have proved nothing).
If a solver change ever flips a free choice the non-vacuity tests fail loudly
rather than the pinning tests passing for free.

That claim was FALSE for the E2 legs until 2026-08-14: the partners were not
parametrized, so they measured E1 only, and under E2 both headline pins passed
with their force deleted. Two DIFFERENT causes, neither a constant to flip:

* WHERE — a resume credits away 90 minutes on the pinned machine ONLY, so the
  pin buys its own machine a discount the solver acts on by itself. The pair now
  charges no setup (``_NO_SETUP``), leaving the force as the only thing that can
  choose.
* WHO — this objective scores late days and spread, never people, so on an
  interchangeable crew the roster is a pure TIE and the free answer is a coin
  flip that lands on a different person under each encoding. No constant can be
  right for both; the pair moved to ``_sole_crew_masters``, where the un-forced
  answer is determined by qualification rather than by a tie.

Run with ``./.venv/bin/python -m pytest`` — homebrew python has no pyjobshop, so
bare pytest importorskips every test here into a vacuous pass.
"""

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("pyjobshop")

from cp_engine import solve as cp_solve
from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)

PLAN_START = datetime(2026, 8, 12, 8, 0)      # a Wednesday; Thursday is the off day

# What the unpinned solve picks, MEASURED and pinned by the non-vacuity tests
# below — never assumed. The pin then names the other machine and the other
# person, so every assertion in this file has something to discriminate.
FREE_MACHINE = "CNC4"
PINNED, FREE_ON_PINNED = "CNC1", "S"
RESUMING = "N"

# The floor tests, and the WHO pair, need work the objective WANTS early — due
# today, so every day it waits is a late-day. With a comfortable delivery date
# nothing is ever late, the solver is indifferent about where in a 20-day
# horizon it puts 20 minutes of work, and "it started late" proves nothing.
_URGENT = date(2026, 8, 12)

BOTH_ENCODINGS = pytest.mark.parametrize("hold", [False, True],
                                         ids=["E1", "E2"])


class _B:
    """The lightweight batch double the CP tests use — ``process_remaining`` is
    already seq-keyed, which ``domain._remaining_by_seq`` accepts as its fallback
    for exactly this shape."""

    def __init__(self, key, item, qty, due=date(2026, 12, 1), remaining=None):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = remaining


def _bench_masters():
    """Two DEBURRING benches, two helpers on both.

    Rule 1 rosters CNC/VMC only (``rules.add_roster`` iterates
    ``shop.machining_ids``) — helpers and inspectors physically walk between
    manual and inspection stations — so a bench is a different shape of pin: the
    machine is forced, and the WHO half is not applied at all.
    """
    return Masters(
        machines={
            "MD1": Machine("MD1", "Deburring 1", "Manual", available_hrs_per_day=11.0),
            "MD2": Machine("MD2", "Deburring 2", "Manual", available_hrs_per_day=11.0),
        },
        routings={"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "DEBURING", 5.0, None, None, "MD1/MD2")])},
        operators=[Operator(n, "MD1/MD2", ["MD1", "MD2"], "First shift")
                   for n in ("N", "S")],
        calendar=WorkCalendar())


def _sole_crew_masters():
    """``RESUMING`` is the ONLY person who can run ``FREE_MACHINE``.

    WHO is a pure tie for this objective — it scores late days and spread, never
    people — so on an interchangeable crew "the free solve rosters the other
    person" is a COIN FLIP, and it lands differently under each encoding
    (measured 2026-08-14: with the machine pinned and nobody named, E1 puts S on
    CNC1 and E2 puts N there). An operator assertion on that book therefore
    cannot discriminate under both encodings no matter which name it pins.

    This fixture removes the coin instead of betting on it. A second batch runs
    on CNC4 only, and only ``RESUMING`` is qualified there, so the un-forced
    answer is ``FREE_ON_PINNED`` on CNC1 under BOTH encodings — and forcing
    ``RESUMING`` onto CNC1 leaves CNC4 unmanned for that shift, which costs the
    plan two real late-days. The same shape the floor tests use: the solver
    demonstrably wanted the other answer.
    """
    return Masters(
        machines={
            "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
            "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        },
        routings={
            "A": Routing("A", "a", "cust", "rm", None, [
                Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1/CNC4")]),
            "C": Routing("C", "c", "cust", "rm", None, [
                Process(1, "CNC FIRST SIDE", 5.0, None, None, FREE_MACHINE)]),
        },
        operators=[Operator(RESUMING, "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                   Operator(FREE_ON_PINNED, PINNED, [PINNED], "First shift")],
        calendar=WorkCalendar())


def _SOLE_CREW_BOOK():
    """The frozen batch, plus one due TODAY that can only run on ``FREE_MACHINE``
    — which is what makes ``RESUMING`` wanted there and the WHO force costly."""
    return [_B("B1", "A", 10, remaining={1: 4}),
            _B("B2", "C", 4, due=_URGENT)]


def _masters(strip=(), night=False):
    """Two CNCs, a one-step routing, two first-shift operators on both machines.

    ``strip`` removes ``(operator, machine)`` pairs from the Settings table, so a
    test can take a machine away from somebody whose part is still in its chuck.
    ``night`` adds a second-shift operator, which is what lets a run longer than
    one shift exist at all under E1 — E1 forbids an operation spanning an
    UNSTAFFED shift, so with a first-shift-only crew nothing over 660 minutes can
    be scheduled anywhere.
    """
    def machines_for(name):
        return [m for m in ("CNC1", "CNC4") if (name, m) not in strip]

    crew = [(name, "First shift") for name in ("N", "S")]
    if night:
        crew.append(("T", "Second shift"))
    return Masters(
        machines={
            "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
            "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        },
        routings={"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1/CNC4")])},
        operators=[Operator(name, "/".join(machines_for(name)),
                            machines_for(name), shift)
                   for name, shift in crew],
        calendar=WorkCalendar())


def _solve(batches, frozen=None, masters=None, hold=False, setup=90.0):
    return cp_solve.solve_book(
        batches, masters if masters is not None else _masters(),
        Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
               setup_time_min=setup),
        PLAN_START, time_limit=30, horizon_days=20, num_workers=1,
        frozen=frozen, hold_across_unmanned_shift=hold)


def _shift_of(res, minute: int) -> int:
    """Which shift a minute falls in. Used where the assertion is about the shift
    the WORK landed in — the solver is free to place a book with nothing late
    anywhere in the horizon, so hardcoding an index there would be a coin flip.
    Shift 0 IS hardcoded where the claim is about the RESUME shift, which the pin
    fixes."""
    return next(s.index for s in res.shifts if s.start <= minute < s.end)


def _pin(machine=PINNED, operator=RESUMING, remaining_qty=4,
         prev_start=PLAN_START, **kw):
    row = {"job_key": "B1", "op_seq": 1, "machine": machine,
           "operator": operator, "remaining_qty": remaining_qty,
           "prev_start": prev_start}
    row.update(kw)
    return [row]


# --------------------------------------------------------------------------- #
# WHERE
# --------------------------------------------------------------------------- #

# ``setup_time_min=0`` on the WHERE pair, ON PURPOSE — it is the whole reason
# the E2 leg used to prove nothing. Rule 4 is INVERTED here (90 minutes is in
# every machining mode and credited back on a resume), so a pin makes ITS OWN
# machine 90 minutes cheaper than the same operation anywhere else, and under E2
# that alone is enough for the solver to select it: with the machine force
# deleted the op STILL landed on the pinned machine (measured 2026-08-14,
# mutation M-B). Charge no setup and the two machines are interchangeable, so
# the force is the only thing left that can decide. The 90 minutes has its own
# tests, below.
_NO_SETUP = 0.0


@BOTH_ENCODINGS
def test_with_no_pin_the_solver_picks_the_other_machine(hold):
    """Non-vacuity for the WHERE pin, under BOTH encodings — it used to be
    measured under E1 only, so the E2 leg asserted nothing."""
    free = _solve([_B("B1", "A", 10, remaining={1: 4})], hold=hold,
                  setup=_NO_SETUP)
    assert free.status_ok
    assert free.genome["cp_machine_of"][("B1", 1)] == FREE_MACHINE


@BOTH_ENCODINGS
def test_a_frozen_op_is_pinned_to_the_machine_it_is_physically_on(hold):
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=_pin(), hold=hold,
                 setup=_NO_SETUP)
    assert res.status_ok
    assert res.genome["cp_machine_of"][("B1", 1)] == PINNED
    assert res.stats["frozen_applied"] == 1


@BOTH_ENCODINGS
def test_a_frozen_op_pins_its_operator_onto_that_machine_for_the_shift(hold):
    """See ``_sole_crew_masters``: ``RESUMING`` is the only person who can man
    CNC4, so the un-forced answer is the OTHER person on CNC1 under both
    encodings, and forcing him here costs the plan two real late-days."""
    res = _solve(_SOLE_CREW_BOOK(), frozen=_pin(operator=RESUMING),
                 masters=_sole_crew_masters(), hold=hold)
    assert res.status_ok
    assert res.genome["cp_roster"][(PINNED, 0)] == RESUMING
    # He was WANTED on CNC4, and the pin overrode that: nobody mans CNC4 in the
    # resume shift and the batch that needs it slips two days.
    assert res.genome["cp_roster"].get((FREE_MACHINE, 0)) is None
    assert res.total_late_days > 0


@BOTH_ENCODINGS
def test_a_pin_with_no_operator_leaves_the_roster_to_the_solver(hold):
    """A row that names no person still pins the machine — WHERE without WHO.

    Also the non-vacuity partner of the test above, and the exact baseline the
    roster-force mutation reduces to: on the SAME book, with the machine pinned
    and nobody named, the solver rosters the OTHER person on it — now measured
    under both encodings, not just E1.
    """
    res = _solve(_SOLE_CREW_BOOK(), frozen=_pin(operator=None),
                 masters=_sole_crew_masters(), hold=hold)
    assert res.status_ok
    assert res.genome["cp_machine_of"][("B1", 1)] == PINNED
    assert res.genome["cp_roster"][(PINNED, 0)] == FREE_ON_PINNED
    assert res.genome["cp_roster"][(FREE_MACHINE, 0)] == RESUMING
    assert res.total_late_days == 0            # nothing was given up for it


# --------------------------------------------------------------------------- #
# WHERE on a BENCH — where there is no WHO to apply
# --------------------------------------------------------------------------- #

@BOTH_ENCODINGS
def test_with_no_pin_the_solver_picks_the_other_bench(hold):
    """Non-vacuity partner for the bench pin below. Measured, under both
    encodings, so the pin is known to move the work."""
    free = _solve([_B("B1", "A", 10, remaining={1: 4})],
                  masters=_bench_masters(), hold=hold)
    assert free.status_ok
    assert free.genome["cp_machine_of"][("B1", 1)] == "MD2"


@BOTH_ENCODINGS
def test_a_bench_pin_is_WHERE_only_and_is_never_reported_as_a_settings_fault(hold):
    """The 2026-08-14 review finding, and CLAUDE.md's 2026-08-09 rule: a report
    may never attribute a cause it did not CHECK.

    ``roster.x`` holds CNC/VMC entries only, because Rule 1 rosters machining
    machines only. So the operator lookup in ``pin_frozen`` MISSES for every
    bench pin — and it used to report the miss as *"N is not on MD1 for that
    shift under today's Settings"*, about a helper who IS on MD1. Nothing is
    wrong with those Settings; this engine simply does not roster bench work by
    shift, and the WHO half of a bench pin is not applied at all.

    It would not have been a rare line either: ``engine.freeze`` freezes every
    in-progress non-OS step and the owner's routings are largely
    MD/MW/MPK/MI/CMM/DTC, so this list would have been mostly false accusations
    against a correctly configured operator table.
    """
    res = _solve([_B("B1", "A", 10, remaining={1: 4})],
                 frozen=_pin(machine="MD1"), masters=_bench_masters(), hold=hold)
    assert res.status_ok
    assert res.genome["cp_machine_of"][("B1", 1)] == "MD1"   # WHERE is physics
    assert res.stats["frozen_applied"] == 1
    assert res.stats["frozen_unpinned"] == []               # and nothing invented


def test_a_machining_pin_still_reports_an_operator_it_cannot_honour():
    """The other half of the statement above: silence is scoped to BENCHES, not
    granted to every missing roster entry. On a machining machine — where Rule 1
    really does roster, and the lookup therefore means something — a pinned
    operator the Settings table no longer qualifies is still reported."""
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=_pin(),
                 masters=_masters(strip=[(RESUMING, PINNED)]))
    assert res.status_ok
    assert any("under today's Settings" in reason
               for reason in res.stats["frozen_unpinned"])


# --------------------------------------------------------------------------- #
# HOW MUCH — the 2026-08-11 escalation
# --------------------------------------------------------------------------- #

@BOTH_ENCODINGS
def test_frozen_qty_comes_from_the_batch(hold):
    """2026-08-11, director escalation: a frozen row pins WHERE and WHEN, never
    HOW MUCH. The row's own remaining_qty is a per-SO-LINE number and the op is
    a BATCH operation — reading it left 281 pieces of a clubbed order in no plan
    at all."""
    res = _solve([_B("B1", "A", 535, remaining={1: 242})],
                 frozen=_pin(remaining_qty=88), hold=hold,
                 masters=_masters(night=True))
    assert res.status_ok
    assert res.op_qty("B1", 1) == 242              # the batch's number, not 88
    # And the MACHINE really is booked for 242 pieces. ``op_qty`` is read off the
    # job, so this is what stops a DURATION taken off the row hiding behind it.
    assert res.machine_busy_minutes(PINNED) == 242 * 5


# --------------------------------------------------------------------------- #
# WHEN, and the setup
# --------------------------------------------------------------------------- #

@BOTH_ENCODINGS
def test_a_frozen_op_pays_no_setup_on_resume(hold):
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=_pin(), hold=hold)
    assert res.status_ok
    assert res.machine_busy_minutes(PINNED) == 4 * 5        # no 90


def test_an_unpinned_op_still_pays_its_setup():
    """The other half of the same statement: 90 minutes is IN every machining
    mode (Rule 4 inverted), and only a resume is credited it back."""
    res = _solve([_B("B1", "A", 10, remaining={1: 4})])
    assert res.status_ok
    assert res.machine_busy_minutes(FREE_MACHINE) == 90 + 4 * 5


@BOTH_ENCODINGS
def test_a_frozen_op_may_not_resume_before_the_previous_plan_started_it(hold):
    """``earliest_start`` from the previous plan. Friday's first shift opens at
    minute 2880 (Thursday is the shop's weekly off), so a pin that says the part
    was started then cannot be replanned into Wednesday — even though waiting
    costs this order two late-days and the solver is trying to avoid them."""
    res = _solve([_B("B1", "A", 10, due=_URGENT, remaining={1: 4})],
                 frozen=_pin(prev_start=PLAN_START + timedelta(days=2)),
                 hold=hold)
    assert res.status_ok
    assert res.task_window("B1", 1)[0] >= 2880
    assert res.total_late_days > 0            # it really did want to go earlier


@BOTH_ENCODINGS
def test_without_that_floor_the_op_would_start_immediately(hold):
    """Non-vacuity for the floor: the same urgent book, with the previous plan's
    start at the plan clock, runs in the first shift and is not late at all."""
    res = _solve([_B("B1", "A", 10, due=_URGENT, remaining={1: 4})],
                 frozen=_pin(), hold=hold)
    assert res.status_ok
    assert res.task_window("B1", 1)[0] < 660
    assert res.total_late_days == 0


# --------------------------------------------------------------------------- #
# A live plan must never simply fail
# --------------------------------------------------------------------------- #

def test_a_pinned_operator_who_is_no_longer_qualified_does_not_kill_the_plan():
    """The 2026-08-03 / 2026-08-07 "Sidhu Singe on CNC5" class, at the model
    layer. An admin has taken CNC1 off S in Settings while his part is still in
    the chuck. The MACHINE pin is physics and stays; the PERSON is re-staffed
    from today's Settings, because forcing a roster boolean that no longer exists
    would take the whole book out of the plan."""
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=_pin(),
                 masters=_masters(strip=[(RESUMING, PINNED)]))
    assert res.status_ok
    assert res.genome["cp_machine_of"][("B1", 1)] == PINNED
    assert res.stats["frozen_applied"] == 1          # the machine pin still landed
    # The person the admin disqualified is on that machine in NO shift...
    assert all(who != RESUMING
               for (mid, _s), who in res.genome["cp_roster"].items()
               if mid == PINNED)
    # ...and the work is still manned, by whoever Settings does allow.
    started_in = _shift_of(res, res.task_window("B1", 1)[0])
    assert res.genome["cp_roster"][(PINNED, started_in)] == FREE_ON_PINNED
    assert any(RESUMING in reason for reason in res.stats["frozen_unpinned"])


def test_two_pins_naming_two_operators_for_one_machine_shift_do_not_kill_it():
    """Rule 1 allows exactly one person per machine per shift, so two rows
    disagreeing about who is on that machine would make the model INFEASIBLE if
    both were forced. One wins by the same rank the decoder uses; the loser's
    person is re-staffed and reported, and BOTH machine pins still land."""
    rows = _pin(operator=RESUMING) + [
        {"job_key": "B2", "op_seq": 1, "machine": PINNED,
         "operator": FREE_ON_PINNED, "remaining_qty": 3,
         "prev_start": PLAN_START + timedelta(hours=1)}]
    res = _solve([_B("B1", "A", 10, remaining={1: 4}),
                  _B("B2", "A", 10, remaining={1: 3})], frozen=rows)
    assert res.status_ok
    assert res.genome["cp_machine_of"][("B1", 1)] == PINNED
    assert res.genome["cp_machine_of"][("B2", 1)] == PINNED
    assert res.genome["cp_roster"][(PINNED, 0)] == RESUMING     # earliest wins
    assert res.stats["frozen_applied"] == 2
    assert res.stats["frozen_unpinned"]


def test_one_person_cannot_be_pinned_to_two_machines_in_one_shift():
    """Nobody mans two machines in one shift (Rule 1), so two rows that put the
    same person on two of them would be INFEASIBLE if both were forced."""
    rows = _pin(machine=PINNED, operator=RESUMING) + [
        {"job_key": "B2", "op_seq": 1, "machine": FREE_MACHINE,
         "operator": RESUMING, "remaining_qty": 3,
         "prev_start": PLAN_START + timedelta(hours=1)}]
    res = _solve([_B("B1", "A", 10, remaining={1: 4}),
                  _B("B2", "A", 10, remaining={1: 3})], frozen=rows)
    assert res.status_ok
    assert res.stats["frozen_applied"] == 2
    assert res.genome["cp_roster"][(PINNED, 0)] == RESUMING     # earliest wins
    assert res.genome["cp_roster"][(FREE_MACHINE, 0)] != RESUMING
    assert any("two machines" in reason for reason in res.stats["frozen_unpinned"])


def test_two_rows_for_one_operation_keep_the_earliest_started_machine():
    """Several clubbed SO lines can each be in progress on the SAME batch step,
    and an operation runs once, on one machine. Two rows naming DIFFERENT
    machines are a real data conflict — the earlier-started row wins, by the same
    rank the replay decoder uses, so the answer cannot depend on the order the
    rows arrive in."""
    rows = _pin(machine=PINNED, prev_start=PLAN_START) + [
        {"job_key": "B1", "op_seq": 1, "machine": FREE_MACHINE,
         "operator": FREE_ON_PINNED, "remaining_qty": 9,
         "prev_start": PLAN_START + timedelta(hours=1)}]
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=rows)
    assert res.status_ok
    assert res.stats["frozen_applied"] == 1
    assert res.genome["cp_machine_of"][("B1", 1)] == PINNED
    assert any("cannot be" in reason for reason in res.stats["frozen_unpinned"])


def test_two_rows_for_one_operation_on_one_machine_are_a_duplicate():
    """The ordinary clubbed-SO-lines shape: same operation, same machine, two
    rows. That is a duplicate, not a conflict — reporting it as a conflict would
    attribute a cause nobody checked. The earlier row's PERSON is the one who
    resumes."""
    rows = _pin(operator=RESUMING, prev_start=PLAN_START) + [
        {"job_key": "B1", "op_seq": 1, "machine": PINNED,
         "operator": FREE_ON_PINNED, "remaining_qty": 9,
         "prev_start": PLAN_START + timedelta(hours=1)}]
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=rows)
    assert res.status_ok
    assert res.stats["frozen_applied"] == 1
    assert res.genome["cp_roster"][(PINNED, 0)] == RESUMING
    assert any("runs once" in reason for reason in res.stats["frozen_unpinned"])


def test_a_pin_onto_a_machine_nobody_can_man_is_reported_not_forced():
    """Rule 1 blocks a machining machine nobody is qualified for, so forcing an
    operation onto one is an infeasible model rather than a plan. The row is
    reported and the step is scheduled where it CAN run."""
    masters = _masters()
    masters.machines["CNC7"] = Machine("CNC7", "CNC 7", "CNC lathe",
                                       available_hrs_per_day=19.5)
    masters.routings["A"] = Routing("A", "a", "cust", "rm", None, [
        Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1/CNC4/CNC7")])
    res = _solve([_B("B1", "A", 10, remaining={1: 4})],
                 frozen=_pin(machine="CNC7"), masters=masters)
    assert res.status_ok
    assert res.stats["frozen_applied"] == 0
    assert res.genome["cp_machine_of"][("B1", 1)] != "CNC7"
    assert any("qualified for CNC7" in reason
               for reason in res.stats["frozen_unpinned"])


def test_a_row_naming_a_machine_the_step_no_longer_runs_on_is_reported():
    """A pin the current routing cannot honour falls through to normal
    scheduling — never a machine list of one impossible machine, which would take
    the whole book out of the plan at the horizon. The accounting closes, and the
    reason names the ROUTING, which is what an owner can act on."""
    res = _solve([_B("B1", "A", 10, remaining={1: 4})], frozen=_pin(machine="CNC9"))
    assert res.status_ok
    assert res.stats["frozen_applied"] == 0
    assert len(res.stats["frozen_unpinned"]) == 1
    assert "no longer runs on CNC9" in res.stats["frozen_unpinned"][0]
    # and it paid its setup, because nothing was resumed
    assert res.machine_busy_minutes(FREE_MACHINE) == 90 + 4 * 5


def test_pin_frozen_refuses_a_pin_set_the_model_was_not_built_for():
    """``model.build`` bakes a resumed op's missing setup and its earliest_start
    into ProblemData, where nothing added later can reach them. Forcing a
    DIFFERENT pin set onto that model would pin a machine whose mode still
    carries 90 minutes it does not owe. Same guard, same reason, as
    ``add_roster``'s hold-flag check."""
    from cp_engine import rules as cp_rules

    class _Built:
        pins = {}

    with pytest.raises(ValueError):
        cp_rules.pin_frozen(None, None, _Built(), None, {("B1", 1): object()})


# --------------------------------------------------------------------------- #
# The empty case is untouched
# --------------------------------------------------------------------------- #

def test_no_pins_is_byte_identical_to_no_frozen_argument():
    a = _solve([_B("B1", "A", 10)], frozen=[])
    b = _solve([_B("B1", "A", 10)], frozen=None)
    assert a.genome == b.genome
    assert a.stats["frozen_applied"] == b.stats["frozen_applied"] == 0


def test_a_resumed_ops_successor_is_not_held_back_by_a_setup_it_did_not_pay():
    """Regression for a REAL interaction between the two halves of this task.

    Rule 3 used to carry a HEAD release bound as well as the tail one —
    ``b.start >= a.start + setup + k x cycle`` — kept because the tail provably
    dominated it (``interval >= processing = setup + cutting``, so
    ``tail - head = breaks + idle >= 0``). Pinning a resumed op breaks that
    premise: its mode carries NO setup, so ``processing`` is the cutting time
    alone while the head bound still charged the predecessor 90 minutes it never
    paid, and the successor was held back by exactly that. MEASURED on a tardy
    12-order book: 140 late-days with the head bound, 139 without, and the ONLY
    leg of six that moved was a frozen one.

    So the head bound is gone (owner-authorized 2026-08-14) and this pins the
    behaviour: the successor of a resumed op is released off its predecessor's
    real end, not off a setup that was credited away.
    """
    masters = _masters()
    masters.routings["A"] = Routing("A", "a", "cust", "rm", None, [
        Process(1, "CNC FIRST SIDE", 5.0, None, None, PINNED),
        Process(2, "CNC SECOND SIDE", 5.0, None, None, FREE_MACHINE)])
    res = _solve([_B("B1", "A", 10, due=_URGENT)], frozen=_pin(), masters=masters)
    assert res.status_ok
    assert res.machine_busy_minutes(PINNED) == 10 * 5        # resumed, no setup
    # Step 1 runs 0..50. The head bound would have demanded step 2 start no
    # earlier than 0 + 90 + one piece; the tail bound demands only 50 - 9 x 5.
    assert res.task_window("B1", 2)[0] < 90
