"""Rule 3 — overlap in whole pieces, per job — and Rule 4 — setup on change.

SOLVED-model tests. A test that reads the constraint back off the variables it
just built proves only that it was built; every rule here is phrased as an
assertion about the resulting SCHEDULE — how many minutes a machine really
paid, and when the successor really started.

They compose the layers here rather than through ``cp_engine.solve``, which does
not exist until Task 6 — a test that cannot be run is not a test. The harness is
``tests/test_cp_rule1.py::_solve_tiny`` plus the two rules this task adds.

Note there is already an objective: ``ProblemData`` defaults to minimising
makespan and ``CPModel.__init__`` posts it. That is what makes these assertions
crisp — the solver takes the credit and the earliest legal release whenever it
can, so a missing constraint shows up as a SHORTER plan.
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

# The shift grid these tests reason in, in minutes from PLAN_START:
#   first  0..660      second 660..1260     Thursday off
#   first  2880..3540  second 3540..4140    (Friday)
# A two-shift machine (19.5 hrs/day) runs 0..1260; a single-shift one 0..660.

_CNC1 = Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)
_CNC4 = Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5)
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
    """Process(seq, name, cycle, total, SUGGESTED, ALLOTTED) — suggested first.

    ``domain._candidates`` reads Allotted then Suggested, so ``machine`` is the
    FIRST option and therefore the one a step's kind is read off.
    """
    return Process(seq, name, cycle, None, suggested, machine)


# --------------------------------------------------------------------------- #
# Composition helper — Task 6's solve_book, minus everything Task 6 owns
# --------------------------------------------------------------------------- #

@dataclass
class _Solved:
    ok: bool
    built: object
    span: dict           # (job key, op seq) -> (start, end) in plan minutes
    processing: dict     # (job key, op seq) -> minutes of the mode it selected
    machine_of: dict     # (job key, op seq) -> machine id
    released: dict       # job key -> k, the pieces the solver released

    def makespan(self) -> int:
        return max(end for _start, end in self.span.values())

    def machine_busy_minutes(self, mid: str) -> int:
        """Processing minutes booked on this machine — setup INCLUDED, because
        setup is inside the selected mode's duration (Rule 4, inverted). This is
        the number a wrongly granted credit makes too small."""
        return sum(minutes for key, minutes in self.processing.items()
                   if self.machine_of.get(key) == mid)


def _solve_tiny(masters, batches, *, hold=True, absent=None, setup_mode="credit",
                pin_k_max=False, horizon_days=20, time_limit=30):
    """domain -> windows -> model -> CPModel -> the three rules -> solve.

    The horizon is CLOSED here for the reason ``test_cp_rule1._solve_tiny``
    documents: past the last shift a machine has no breaks and no unstaffed
    shifts, so an unstaffable job parks out there and reports OPTIMAL.
    """
    from ortools.sat.python import cp_model as cp_sat
    from pyjobshop.solvers.ortools.CPModel import CPModel

    config = _cfg()
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, absent or {})
    shifts = windows.build_shifts(PLAN_START, masters.calendar, config,
                                  horizon_days)
    built = model.build(jobs, shop, config, PLAN_START, shifts,
                        setup_mode=setup_mode,
                        hold_across_unmanned_shift=hold)

    cp = CPModel(built.data)
    horizon = shifts[-1].end
    for task_idx in range(built.data.num_tasks):
        cp.model.add(cp.variables.task_vars[task_idx].end <= horizon)

    rules.add_roster(cp.model, cp.variables, built, shop,
                     hold_across_unmanned_shift=hold)
    released = rules.add_release(cp.model, cp.variables, built, config)
    if pin_k_max:
        # Force every job to its LATEST legal release. Nothing in a makespan
        # objective ever wants that, so it is the only way to see what the top of
        # k's domain means — and the top of a wrong domain is where the harm is.
        #
        # ``max(...)``, not ``domain[-1]``: ``proto.domain`` is a protobuf
        # repeated field, which does NOT index from the end like a list — it
        # silently returns 0, which pins k outside its own domain and reports
        # INFEASIBLE with nothing to say why.
        for var in released.values():
            cp.model.add(var == max(var.proto.domain))
    rules.add_setup_credit(cp.model, cp.variables, built, config)

    solver = cp_sat.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 1
    status = solver.solve(cp.model)
    if status not in (cp_sat.OPTIMAL, cp_sat.FEASIBLE):
        return _Solved(False, built, {}, {}, {}, {})

    span, processing, machine_of = {}, {}, {}
    for key, task_idx in built.task_of.items():
        task_var = cp.variables.task_vars[task_idx]
        span[key] = (solver.value(task_var.start), solver.value(task_var.end))
        processing[key] = solver.value(task_var.processing)
        for mid in sorted(built.machine_res_order):
            assign = cp.variables.assign_vars.get(
                (task_idx, built.machine_res_index(mid)))
            if assign is not None and solver.value(assign.present):
                machine_of[key] = mid
    return _Solved(True, built, span, processing, machine_of,
                   {job_key: solver.value(k) for job_key, k in released.items()})


# --------------------------------------------------------------------------- #
# Rule 3 — the successor starts once k pieces have cleared
# --------------------------------------------------------------------------- #

def _two_step_masters(first_cycle, second_cycle):
    """CNC1 (setup + cutting) feeding MD1, one operator each so Rule 1 cannot
    serialise them by accident."""
    return _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=first_cycle, seq=1),
            _proc("MD1", cycle=second_cycle, name="DEBURING", seq=2)])},
        [_op("Narayan", ["CNC1"]), _op("Anturam", ["MD1"])])


def test_released_pieces_are_whole_and_at_least_one():
    """Rule 3: releasing on 5.6 pieces starts a process on a piece that does not
    exist. k is an integer variable in 1..qty, so this holds by construction —
    the test pins that the model really carries whole pieces, one per JOB."""
    res = _solve_tiny(_two_step_masters(2.0, 10.0), [_B("B1", "A", 20)])
    assert res.ok
    k = res.released["B1"]
    assert isinstance(k, int) and 1 <= k <= 20


def test_the_successor_waits_for_the_pieces_the_solver_released():
    """The whole of Rule 3 in one schedule.

    CNC1 runs 90 setup + 20 x 2 = 130 minutes; MD1 then wants 20 x 10 = 200.
    With no release rule at all the deburring could start at minute 0 and finish
    at 200 — pieces processed before they were cut. Releasing k=1 piece puts its
    earliest start at 90 + 2 = 92.
    """
    res = _solve_tiny(_two_step_masters(2.0, 10.0), [_B("B1", "A", 20)])
    assert res.ok
    assert res.span[("B1", 1)] == (0, 130)
    assert res.released["B1"] == 1
    assert res.span[("B1", 2)] == (92, 292)
    assert res.makespan() == 292


def test_the_release_is_measured_from_the_tail_when_the_step_spans_a_break():
    """The bound that actually binds, and why BOTH are written down.

    1,500 pieces x 1 minute on CNC1 outlasts Wednesday: 1,590 minutes of
    processing into a machine open 0..1260, so the part is held over the dark
    Thursday and the operation ends ~1,620 minutes later than its work alone.

    Measured from the HEAD (start + setup + k x cycle) the first piece cleared at
    minute 91. Measured from the TAIL (end - (qty - k) x cycle) it cleared at
    ~1,711 — the head is optimistic exactly when a break falls early, and the
    tail pessimistic when one falls late. This test pins the tail, which is the
    one that binds; without it the deburring starts inside Wednesday's first
    shift and the plan comes out ~1,350 minutes shorter than the shop can run.
    """
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=1.0, seq=1),
            _proc("MD1", cycle=1.0, name="DEBURING", seq=2)])},
        [_op("Narayan", ["CNC1"]), _op("Sidhu", ["CNC1"], shift="2nd shift"),
         _op("Anturam", ["MD1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 1500)])
    assert res.ok

    qty = cutting = 1500
    k = res.released["B1"]
    a_start, a_end = res.span[("B1", 1)]
    b_start, _b_end = res.span[("B1", 2)]

    assert a_end - a_start > res.processing[("B1", 1)]      # it really was held
    # The tail bound, in the model's own scaled integer arithmetic.
    assert qty * b_start >= qty * a_end - (qty - k) * cutting
    # ...and it is strictly later than the head bound allowed, so this schedule
    # could not have been produced by the head bound alone.
    assert b_start > a_start + 90 + k


def test_k_counts_the_pieces_the_STEP_still_owes_not_the_batch_size():
    """Which quantity k is counted in, on a re-plan with work in progress.

    The batch is 60 pieces but its first step has only 5 left to cut
    (``process_remaining``), so 5 is the most that step can ever release. k is
    ONE decision for the whole job, so its domain has to be legal for every step
    it governs — sized on the batch's 60 instead, the tail bound at the top of
    the domain reads ``a.end - (5 - 60) x cycle``, i.e. it demands the successor
    start 550 minutes AFTER its predecessor finished. Rule 3 makes no such
    statement; fully sequential is as strict as it gets.

    Pinned at the top of the domain, because that is where a wrong domain shows.

    THREE steps, deliberately: with only one overlapping pair the smallest and
    the largest remainder are the same number and the rule under test is
    invisible. Here step 1 owes 5 and step 2 owes 40, so k must take the
    SMALLEST — the largest is legal for step 2 and nonsense for step 1.
    """
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=2.0, seq=1),
            _proc("MD1", cycle=2.0, name="DEBURING", seq=2),
            _proc("MD1", cycle=2.0, name="INSPECTION", seq=3)])},
        [_op("Narayan", ["CNC1"]), _op("Anturam", ["MD1"])])
    batch = _B("B1", "A", 60)
    batch.process_remaining = {1: 5, 2: 40, 3: 40}
    res = _solve_tiny(masters, [batch], pin_k_max=True)
    assert res.ok

    assert res.released["B1"] == 5              # min(5, 40) — not 60, and not 40
    assert res.span[("B1", 1)] == (0, 100)      # 90 setup + 5 x 2
    # The whole remainder released == fully sequential, and never stricter.
    assert res.span[("B1", 2)][0] == res.span[("B1", 1)][1]


def test_a_successor_never_finishes_before_the_step_feeding_it():
    """Pacing. The 2026-07-25 lesson: the machine-wise schedule was processing
    pieces before they existed, and the old numbers were infeasible, not
    better."""
    res = _solve_tiny(_two_step_masters(20.0, 0.5), [_B("B1", "A", 50)])
    assert res.ok
    first = res.span[("B1", 1)]
    second = res.span[("B1", 2)]
    assert second[1] >= first[1]
    assert second[0] > first[0]


def test_an_os_step_is_sequential_and_never_overlaps():
    """A vendor block hands nothing over gradually — no Rule 3 release either
    side of it (pinned by ``model.build``'s end_before_start, and pinned again
    here so ``_overlaps`` can never quietly start pipelining into one)."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=5.0, seq=1),
            _proc("OS", cycle=2880.0, name="BAND SAW OS", seq=2),
            _proc("MD1", cycle=1.0, name="DEBURING", seq=3)])},
        [_op("Narayan", ["CNC1"]), _op("Anturam", ["MD1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.span[("B1", 2)][0] >= res.span[("B1", 1)][1]
    assert res.span[("B1", 3)][0] >= res.span[("B1", 2)][1]
    assert "B1" not in res.released          # no overlapping pair in this job


# --------------------------------------------------------------------------- #
# Rule 3 — a routing step that has no task at all
# --------------------------------------------------------------------------- #

def test_a_dispatch_milestone_mid_routing_does_not_break_the_release():
    """``model.build`` gives a DISPATCH milestone no task and chains the
    precedence straight past it. The release has to step over it the same way:
    carrying the milestone as the predecessor looks up a task that does not
    exist and raises — on a real book, on any routing with a mid-list DISPATCH.

    The two real steps either side must still be linked, or the milestone would
    quietly delete a release rule as well."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=2.0, seq=1),
            Process(2, "DISPATCH", None, None, None, None),
            _proc("MD1", cycle=2.0, name="DEBURING", seq=3)])},
        [_op("Narayan", ["CNC1"]), _op("Anturam", ["MD1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 20)])
    assert res.ok
    assert ("B1", 2) not in res.span             # the milestone really has no task
    assert res.released["B1"] == 1               # step 1 -> step 3 is still linked
    assert res.span[("B1", 1)] == (0, 130)
    assert res.span[("B1", 3)][0] == 92          # 90 setup + one 2-minute piece


def test_a_finished_step_mid_routing_does_not_break_the_release():
    """The same hole reached the other way, and the one that turns up on a
    re-plan rather than in a routing: step 2 has nothing left to make, so
    ``model.build`` skips it (``qty_for`` is 0) and the release must chain
    1 -> 3 without looking for its task."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=2.0, seq=1),
            _proc("MD1", cycle=2.0, name="DEBURING", seq=2),
            _proc("MD1", cycle=2.0, name="INSPECTION", seq=3)])},
        [_op("Narayan", ["CNC1"]), _op("Anturam", ["MD1"])])
    batch = _B("B1", "A", 20)
    batch.process_remaining = {1: 20, 2: 0, 3: 20}
    res = _solve_tiny(masters, [batch])
    assert res.ok
    assert ("B1", 2) not in res.span             # nothing left to make on it
    assert res.released["B1"] == 1
    assert res.span[("B1", 1)] == (0, 130)
    assert res.span[("B1", 3)][0] == 92


# --------------------------------------------------------------------------- #
# Rule 4 — 90 minutes on a CNC/VMC whenever the part or the side changes
# --------------------------------------------------------------------------- #

def test_the_same_part_back_to_back_pays_setup_only_once():
    """Rule 4. Two batches of the SAME item and process on one machine: the
    second must be credited its 90 minutes, so the pair costs one setup, not
    two."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 60), _B("B2", "A", 60)])
    assert res.ok
    assert res.machine_busy_minutes("CNC1") == 90 + 60 + 60
    assert sorted(res.processing.values()) == [60, 150]
    assert res.makespan() == 210


def test_a_different_part_pays_its_own_setup():
    """The negative control for the test above: without it, a credit granted
    unconditionally would pass."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)]),
         "B": Routing("B", "b", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 60), _B("B2", "B", 60)])
    assert res.ok
    assert res.machine_busy_minutes("CNC1") == 90 + 60 + 90 + 60
    assert res.processing == {("B1", 1): 150, ("B2", 1): 150}


def test_the_same_part_on_two_machines_pays_two_setups():
    """The credit is a statement about ONE machine's previous job. Two sibling
    batches run in parallel on two CNCs each meet a cold fixture, so each pays —
    a credit keyed on the part alone would make the second one 60 minutes."""
    machines = {"CNC1": _CNC1, "CNC4": _CNC4}
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("CNC1", cycle=1.0, suggested="CNC4")])},
        [_op("Narayan", ["CNC1"]), _op("Sidhu", ["CNC4"])], machines=machines)
    res = _solve_tiny(masters, [_B("B1", "A", 60), _B("B2", "A", 60)])
    assert res.ok
    assert res.machine_of[("B1", 1)] != res.machine_of[("B2", 1)]
    assert res.processing == {("B1", 1): 150, ("B2", 1): 150}
    assert res.makespan() == 150             # parallel, and both paid


def test_a_sibling_running_elsewhere_does_not_warm_this_machine():
    """The fixture is on ONE machine. A sibling batch that finished on CNC4 at
    minute 150 leaves CNC1 as cold as it was, so a step starting on CNC1 at 150
    still pays its 90.

    It takes four batches to catch this, because the cheap shapes let the solver
    dodge it: crediting across machines pins ``b.start == a.end``, and wherever
    both siblings could simply share one machine that pinning costs at least the
    90 it saves. Here it does not — P holds CNC1 until 150 so B2 cannot start
    earlier, and Q wants CNC4 from 150 so B2 cannot follow B1 there. Reading the
    predecessor's machine as "wherever it happens to be" finishes this book in
    246 minutes; the truth is 300.
    """
    machines = {"CNC1": _CNC1, "CNC4": _CNC4}
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("CNC1", cycle=1.0, suggested="CNC4")]),
         "P": Routing("P", "p", "c", "rm", None, [_proc("CNC1", cycle=1.0)]),
         "Q": Routing("Q", "q", "c", "rm", None, [_proc("CNC4", cycle=1.0)])},
        [_op("Narayan", ["CNC1"]), _op("Sidhu", ["CNC4"])], machines=machines)
    res = _solve_tiny(masters, [_B("B1", "A", 60), _B("B2", "A", 60),
                                _B("P1", "P", 60), _B("Q1", "Q", 6)])
    assert res.ok
    assert res.makespan() == 300
    # Whichever way round the solver puts them, no A batch ran on a warm CNC.
    assert res.processing[("B1", 1)] + res.processing[("B2", 1)] == 300


def test_the_same_part_on_its_other_side_pays_again():
    """Rule 4 is same part AND same side. First side then second side is the same
    item on the same machine and still a fixture change — a signature keyed on
    the item alone would hand this one a free changeover."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [
            _proc("CNC1", cycle=1.0, name="CNC FIRST SIDE", seq=1),
            _proc("CNC1", cycle=1.0, name="CNC SECOND SIDE", seq=2)])},
        [_op("Narayan", ["CNC1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 60)])
    assert res.ok
    assert res.machine_busy_minutes("CNC1") == 90 + 60 + 90 + 60
    assert res.processing == {("B1", 1): 150, ("B1", 2): 150}


def test_a_manual_step_never_pays_setup():
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("MD1", cycle=1.0, name="DEBURING")])},
        [_op("Anturam", ["MD1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 60)])
    assert res.ok
    assert res.machine_busy_minutes("MD1") == 60


def test_a_model_says_whether_its_credit_modes_were_ever_constrained():
    """The setup-free modes are unsafe until ``add_setup_credit`` links them: an
    unlinked model lets every member of every same-part group take its
    setup-free mode unconditionally, inventing 90 minutes of CNC capacity per
    task — with no exception, no failing test and nothing in any report.

    So the model carries the answer rather than a comment asking callers to
    remember, exactly as ``add_roster`` checks ``hold_across_unmanned_shift``
    instead of trusting its caller. This is the half that belongs here; Task 6's
    ``solve_book`` asserts it before solving.
    """
    from pyjobshop.solvers.ortools.CPModel import CPModel

    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    config = _cfg()
    jobs, _by_key, _skipped = domain.build_jobs(
        [_B("B1", "A", 60), _B("B2", "A", 60)], masters)
    shop = domain.build_shop(masters, {})
    shifts = windows.build_shifts(PLAN_START, masters.calendar, config, 20)
    built = model.build(jobs, shop, config, PLAN_START, shifts)

    assert built.setup_free_modes                 # there IS something to link...
    assert built.setup_credit_linked is False     # ...and it is not linked yet

    cp = CPModel(built.data)
    rules.add_setup_credit(cp.model, cp.variables, built, config)
    assert built.setup_credit_linked is True


def test_a_model_with_no_credit_modes_is_linked_too():
    """The flag means "safe to solve", not "did any work" — a book with no
    sibling batches builds no credit modes and is already safe, and a caller
    asserting the flag must not have to special-case that."""
    from pyjobshop.solvers.ortools.CPModel import CPModel

    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    config = _cfg()
    jobs, _by_key, _skipped = domain.build_jobs([_B("B1", "A", 60)], masters)
    shop = domain.build_shop(masters, {})
    shifts = windows.build_shifts(PLAN_START, masters.calendar, config, 20)
    built = model.build(jobs, shop, config, PLAN_START, shifts)

    assert built.setup_free_modes == {}
    cp = CPModel(built.data)
    rules.add_setup_credit(cp.model, cp.variables, built, config)
    assert built.setup_credit_linked is True


def test_setup_mode_always_charges_every_changeover():
    """``setup_mode="always"`` is the conservative fallback if the credit turns
    out to cost more solve time than it saves: durations are over-estimated, so
    the plan stays runnable, but it is NOT Rule 4 as written. Same fixture as
    the same-part test above, and it must now pay twice."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None, [_proc("CNC1", cycle=1.0)])},
        [_op("Narayan", ["CNC1"])])
    res = _solve_tiny(masters, [_B("B1", "A", 60), _B("B2", "A", 60)],
                      setup_mode="always")
    assert res.ok
    assert res.machine_busy_minutes("CNC1") == 90 + 60 + 90 + 60


# --------------------------------------------------------------------------- #
# Rule 4 follows the MACHINE, never the step's kind
# --------------------------------------------------------------------------- #

def test_a_step_that_lands_on_a_cnc_pays_setup_whatever_its_kind():
    """``domain._kind_for_machine_id`` reads a step's kind off its FIRST machine
    option and ``_candidates`` puts Allotted first, so a routing written
    ``MD1/CNC1`` is a manual-KIND step that can perfectly well run on a CNC.

    Rule 4 is a statement about the MACHINE — "90 minutes on a CNC/VMC" — so this
    step pays when it lands on CNC1. Keyed on the kind it would be handed a free
    fixture change and the plan would claim 90 minutes of CNC capacity that does
    not exist."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("MD1", cycle=5.0, name="DEBURING",
                             suggested="CNC1")])},
        [_op("Pravin", ["CNC1"])])          # nobody can run MD1
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.machine_of[("B1", 1)] == "CNC1"
    assert res.processing[("B1", 1)] == 90 + 50


def test_a_step_that_lands_on_a_bench_pays_none_whatever_its_kind():
    """The other direction, and the one that costs the shop time rather than
    inventing it: ``CNC1/MD1`` is a machining-KIND step, but a bench has no
    fixture to change. Charged by kind it would carry 90 minutes of setup onto a
    deburring bench."""
    masters = _masters(
        {"A": Routing("A", "a", "c", "rm", None,
                      [_proc("CNC1", cycle=5.0, suggested="MD1")])},
        [_op("Anturam", ["MD1"])])          # nobody can run CNC1
    res = _solve_tiny(masters, [_B("B1", "A", 10)])
    assert res.ok
    assert res.machine_of[("B1", 1)] == "MD1"
    assert res.processing[("B1", 1)] == 50
