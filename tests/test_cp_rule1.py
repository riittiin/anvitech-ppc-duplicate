"""Rule 1 — one operator mans one machine for a whole shift, CNC/VMC only.

These are SOLVED-model tests. Asserting on the roster variables alone would pass
under a rule that never binds, so every test that can be phrased as an assertion
about the resulting SCHEDULE is phrased that way: two jobs and one operator must
come out serialised (makespan 2x), the same two jobs and two operators must come
out concurrent (makespan 1x).

They compose the layers here rather than through ``cp_engine.solve``, which does
not exist until Task 6 — a test that cannot be run is not a test.
"""

from dataclasses import dataclass
from datetime import date, datetime

import pytest

pytest.importorskip("pyjobshop")

from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)
from cp_engine import domain, model, rules, windows

PLAN_START = datetime(2026, 8, 12, 8, 0)     # a Wednesday; Thursday is the off day


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = None


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0, **kw)


def _masters(routings, operators, machines=None):
    machines = machines or {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
    }
    return Masters(machines=machines, routings=routings,
                   operators=list(operators), calendar=WorkCalendar())


def _op(name, machines, shift="First shift"):
    return Operator(name, "/".join(machines), list(machines), shift)


def _proc(machine, cycle=5.0, name="CNC FIRST SIDE", seq=1):
    """Process(seq, name, cycle, total, SUGGESTED, ALLOTTED) — suggested first."""
    return Process(seq, name, cycle, None, None, machine)


# --------------------------------------------------------------------------- #
# Composition helper — Task 6's solve_book, minus everything Task 6 owns
# --------------------------------------------------------------------------- #

@dataclass
class _Solved:
    ok: bool
    built: object
    roster: object            # rules.Roster
    manned: dict              # (machine id, shift index) -> operator name
    machine_of: dict          # (job key, op seq) -> machine id
    span: dict                # (job key, op seq) -> (start, end) in plan minutes

    def makespan(self) -> int:
        return max(end for _start, end in self.span.values())


def _solve_tiny(masters, batches, *, hold=True, absent=None,
                horizon_days=20, time_limit=30):
    """domain -> windows -> model -> CPModel -> add_roster -> solve.

    The horizon is CLOSED here (every task must end inside the last shift).
    ``windows.machine_breaks`` only describes time up to the horizon, so past it
    a machine has no breaks and no unstaffed shifts — a solver handed a job it
    cannot staff will happily park it out there and report OPTIMAL. Task 6 owns
    the real fix; without the cap these tests would measure the leak instead of
    Rule 1 (verified: E1 with an unstaffable second shift reports OPTIMAL at
    minute 28,620 uncapped, INFEASIBLE capped).
    """
    from ortools.sat.python import cp_model as cp_sat
    from pyjobshop.solvers.ortools.CPModel import CPModel

    config = _cfg()
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, absent or {})
    shifts = windows.build_shifts(PLAN_START, masters.calendar, config,
                                  horizon_days)
    built = model.build(jobs, shop, config, PLAN_START, shifts,
                        hold_across_unmanned_shift=hold)

    cp = CPModel(built.data)
    horizon = shifts[-1].end
    for task_idx in range(built.data.num_tasks):
        cp.model.add(cp.variables.task_vars[task_idx].end <= horizon)

    roster = rules.add_roster(cp.model, cp.variables, built, shop,
                              hold_across_unmanned_shift=hold)

    solver = cp_sat.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 1
    status = solver.solve(cp.model)
    ok = status in (cp_sat.OPTIMAL, cp_sat.FEASIBLE)
    if not ok:
        return _Solved(False, built, roster, {}, {}, {})

    manned = {(mid, idx): name
              for (name, mid, idx), var in roster.x.items()
              if solver.value(var)}
    machine_of, span = {}, {}
    for key, task_idx in built.task_of.items():
        task_var = cp.variables.task_vars[task_idx]
        span[key] = (solver.value(task_var.start), solver.value(task_var.end))
        for mid in sorted(built.machine_res_order):
            assign = cp.variables.assign_vars.get(
                (task_idx, built.machine_res_index(mid)))
            if assign is not None and solver.value(assign.present):
                machine_of[key] = mid
    return _Solved(True, built, roster, manned, machine_of, span)


def _machines_per_person(res) -> dict:
    """(operator, shift index) -> the machines he was rostered on."""
    out: dict = {}
    for (mid, shift_idx), name in res.manned.items():
        out.setdefault((name, shift_idx), set()).add(mid)
    return out


# --------------------------------------------------------------------------- #
# Rule 1
# --------------------------------------------------------------------------- #

def test_one_operator_never_mans_two_machines_in_one_shift():
    """Rule 1, the whole point. Two jobs that would both love to run at once,
    one operator qualified on both machines — the solver must serialise them
    onto ONE machine rather than staff both.

    Proved from the SCHEDULE: each job is 90 min setup + 10 x 5 min = 140 min,
    so a plan that broke Rule 1 would run both at once and land at 140. Under
    Rule 1 the only honest answer is 280."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1/CNC4")]),
         "B": Routing("B", "b", "c", "rm", None, [_proc("CNC1/CNC4")])},
        [_op("Narayan", ["CNC1", "CNC4"])])
    res = _solve_tiny(masters, [_B("B1", "A", 10), _B("B2", "B", 10)])
    assert res.ok
    assert res.makespan() == 280            # serialised, not concurrent
    assert all(len(v) == 1 for v in _machines_per_person(res).values()), res.manned


def test_two_operators_may_run_two_machines_in_the_same_shift():
    """The negative control. Without it, a rule that simply refuses to staff
    anything would pass the test above.

    Same two 140-minute jobs, one machine each, one operator each: they MUST
    come out concurrent, on two machines manned by two different people in the
    same shift."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")]),
         "B": Routing("B", "b", "c", "rm", None, [_proc("CNC4")])},
        [_op("Narayan", ["CNC1"]), _op("Sidhu", ["CNC4"])])
    res = _solve_tiny(masters, [_B("B1", "A", 10), _B("B2", "B", 10)])
    assert res.ok
    assert res.makespan() == 140            # concurrent, not serialised
    assert {mid for (mid, _s) in res.manned} == {"CNC1", "CNC4"}

    # ...and concurrent because two people really were on the floor together.
    shared = [s.index for s in res.built.shifts
              if _runs_in(res, ("B1", 1), s) and _runs_in(res, ("B2", 1), s)]
    assert shared
    idx = shared[0]
    assert res.manned[("CNC1", idx)] != res.manned[("CNC4", idx)]


def _runs_in(res, key, shift) -> bool:
    start, end = res.span[key]
    return start < shift.end and shift.start < end


def test_qualification_is_exactly_the_settings_machine_list():
    """Role is NOT a gate (2026-08-07). A workbook 'helper' assigned CNC4 in
    Settings must be rosterable on CNC4 — and is the only reason CNC4 can run
    at all here."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1/CNC4")])},
        [_op("Sandeep", ["CNC4"])])
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.machine_of[("B1", 1)] == "CNC4"
    assert {mid for (mid, _s) in res.manned} == {"CNC4"}


def test_an_operator_is_only_rostered_on_his_own_shift():
    """He works the shift Settings gives him, and the machine cannot cut in the
    shift he is not there for: the work lands after 19:00, not at 08:00."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Narayan", ["CNC1"], shift="2nd shift")])
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    shifts = {s.index: s.shift for s in res.built.shifts}
    assert res.manned
    assert all(shifts[i] == windows.SECOND for (_m, i) in res.manned)
    # The first shift is 0..660; 140 minutes of work can only finish after it.
    assert res.span[("B1", 1)][1] >= 660 + 140


def test_a_machine_nobody_can_staff_is_never_used():
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1/CNC4")])},
        [_op("Narayan", ["CNC1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.machine_of[("B1", 1)] == "CNC1"


def test_an_absent_operator_is_not_rostered():
    """An impossible pairing is ABSENT from the model, not forbidden by a
    constraint somebody could forget to add."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Narayan", ["CNC1"]), _op("Sidhu", ["CNC1"])])
    absent = {"Narayan": [(datetime(2026, 8, 12, 0, 0),
                           datetime(2026, 9, 12, 0, 0))]}
    res = _solve_tiny(masters, [_B("B1", "A", 10)], absent=absent)
    assert res.ok
    assert not [k for k in res.roster.x if k[0] == "Narayan"]
    assert set(res.manned.values()) == {"Sidhu"}


def test_an_unrostered_alternative_machine_is_not_forbidden():
    """A routing may list a machining step's alternatives as CNC1/MD1. The step
    is machining (its kind comes from the first option) but MD1 is a manual
    station Rule 1 does not bind, so with CNC1 unstaffable the step must fall to
    MD1 — not be ruled out for failing to cover its work from a roster that was
    never going to have an entry for MD1."""
    machines = {"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                available_hrs_per_day=19.5),
                "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)}
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1/MD1")])},
        [_op("Anturam", ["MD1"])], machines=machines)
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.machine_of[("B1", 1)] == "MD1"
    assert not res.manned                  # nobody was needed on a CNC


def test_a_single_shift_machine_is_not_manned_at_night():
    """A machine that is SHUT at night is a different fact from nobody being
    available, and confusing the two hands the plan capacity that does not
    exist. A CNC on 9.5 hrs/day with only a second-shift operator can never run:
    he is there and the machine is not."""
    machines = {"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                available_hrs_per_day=9.5)}
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Ravi", ["CNC1"], shift="2nd shift")], machines=machines)
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert not res.ok
    assert res.roster.staffed              # the day shift is rosterable...
    assert all(res.built.shifts[i].shift == windows.FIRST
               for (_m, i) in res.roster.staffed)   # ...and only the day shift


def test_rule_1_does_not_bind_a_manual_station():
    """Binds CNC/VMC only. Helpers and inspectors physically walk between manual
    stations, and rostering them for a whole shift would delete capacity that
    really exists."""
    machines = {"MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
                "MD2": Machine("MD2", "MD 2", "manual", available_hrs_per_day=9.5)}
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("MD1/MD2", cycle=2.0, name="DEBURING")])},
        [_op("Anturam", ["MD1", "MD2"])], machines=machines)
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.roster.x == {} and res.roster.staffed == {}
    assert res.span[("B1", 1)] == (0, 20)      # no setup on a manual step


# --------------------------------------------------------------------------- #
# The two encodings of Rule 2's "may span an unmanned shift" clause
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("hold", [True, False])
def test_the_plan_is_valid_under_both_hold_encodings(hold):
    """E1 and E2 must BOTH produce a rule-clean plan (spec §5.1). They differ in
    whether an operation may span an unstaffed shift, not in whether Rule 1
    holds — so with BOTH shifts staffable they must agree exactly.

    (The brief's version of this test ran 400 pieces past a single first-shift
    operator, which E1 cannot schedule at all: 2,090 minutes of cutting can
    never fit inside one 660-minute shift when the next shift is dark. That is
    E1 working, not E1 broken, and it is what the test below measures.)"""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Narayan", ["CNC1"]), _op("Sidhu", ["CNC1"], shift="2nd shift")])
    res = _solve_tiny(masters, [_B("B1", "A", 400)], hold=hold)
    assert res.ok
    assert all(len(v) == 1 for v in _machines_per_person(res).values())
    # 2,090 minutes of work from 08:00 Wednesday, with Thursday off and the
    # 05:00-08:00 changeover idle: identical under both encodings.
    assert res.span[("B1", 1)] == (0, 3710)


def test_only_e2_holds_a_part_across_an_unmanned_shift():
    """The one behaviour the flag exists to choose between, measured.

    2,090 minutes of cutting, one first-shift operator, a second shift nobody
    can man. E1 blocks the dark shift outright, so the operation may not span it
    and there is no plan. E2 holds the part in the chuck across it — the machine
    is occupied, the clock runs, but no pieces are cut until 08:00."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Narayan", ["CNC1"])])

    assert not _solve_tiny(masters, [_B("B1", "A", 400)], hold=False).ok

    res = _solve_tiny(masters, [_B("B1", "A", 400)], hold=True)
    assert res.ok
    start, end = res.span[("B1", 1)]
    assert (start, end) == (0, 5870)
    unmanned = [s for s in res.built.shifts
                if _runs_in(res, ("B1", 1), s)
                and (s.index not in {i for (_m, i) in res.manned})]
    assert unmanned                     # it really did span a dark shift


def test_add_roster_refuses_a_flag_the_model_was_not_built_for():
    """E2 needs ``allow_idle`` on the machining tasks, which is fixed when
    ProblemData is built and cannot be relaxed afterwards. Two places therefore
    know the flag, so the two are checked against each other rather than trusted
    to stay in step."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1")])},
        [_op("Narayan", ["CNC1"])])
    from pyjobshop.solvers.ortools.CPModel import CPModel

    config = _cfg()
    jobs, _by_key, _skipped = domain.build_jobs([_B("B1", "A", 10)], masters)
    shop = domain.build_shop(masters, {})
    shifts = windows.build_shifts(PLAN_START, masters.calendar, config, 20)
    built = model.build(jobs, shop, config, PLAN_START, shifts,
                        hold_across_unmanned_shift=True)
    cp = CPModel(built.data)
    with pytest.raises(ValueError):
        rules.add_roster(cp.model, cp.variables, built, shop,
                         hold_across_unmanned_shift=False)
