"""Frozen in-progress work — the part is in the chuck, so the machine is not a
choice any more.

The first six tests are the brief's, four of them corrected (see the notes on
each): as written, three asserted the opposite of the rule they were named for or
passed whatever the engine did, and one made its own fixture unschedulable.
Everything under "Strengthening" pins a property the brief's six leave open, and
each of those was written to go RED under a specific mutation of the fix.
"""

import os
import subprocess
import sys
import textwrap
from datetime import date, datetime

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import scheduler
from roster_engine.domain import build_jobs, build_shop

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLAN_START = date(2026, 8, 12)


class _B:
    def __init__(self, key, item, qty, remaining=None):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = ["SO1", "SO2"], date(2026, 12, 1)
        self.process_remaining = remaining


_MACHINES = {
    "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
    "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
    "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
}

_BOTH = ("CNC1", "CNC4")


def _masters(operators=None, processes=None):
    """Two CNC machines and a bench; a two-step routing whose FIRST step may run on
    either CNC. ``Routing`` is (item_code, description, customer, rm_type, moq,
    processes) — the brief's three-argument call put the process list in the
    ``customer`` slot and left the routing empty, so every job would have been
    skipped for having no routing at all."""
    processes = processes or [
        Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4"),
        Process(2, "CNC SECOND SIDE", 5.0, None, None, "CNC4"),
    ]
    operators = operators or [
        Operator("Narayan", "CNC1/CNC4", list(_BOTH), "First shift"),
        Operator("Sidhu", "CNC1/CNC4", list(_BOTH), "First shift"),
    ]
    return Masters(machines=dict(_MACHINES),
                   routings={"ITEM": Routing("ITEM", "d", "", "", None, processes)},
                   operators=operators, calendar=WorkCalendar())


def _cfg():
    return Config(plan_start_date=PLAN_START, setup_time_min=90.0)


def _fixture(remaining=None, operators=None, processes=None, qty=535):
    masters = _masters(operators, processes)
    jobs, _by, _sk = build_jobs([_B("B1", "ITEM", qty, remaining)], masters)
    return jobs, build_shop(masters), _cfg()


def _frozen(**kw):
    row = {"order_key": "B1", "op_seq": 1, "machine_id": "CNC4",
           "operator": "Narayan", "remaining_qty": 100,
           "prev_start": datetime(2026, 8, 11, 8, 0)}
    row.update(kw)
    return [row]


def _run(jobs, shop, cfg, frozen=None, sequence=None, overlap=1.0):
    return scheduler.schedule(jobs, sequence or [j.key for j in jobs], shop, cfg,
                              overlap=overlap, frozen=frozen)


def _at(plan, op_seq, job_key="B1"):
    hits = [p for p in plan.placements
            if p.op_seq == op_seq and p.job_key == job_key]
    assert len(hits) == 1, f"expected exactly one placement for op {op_seq}: {hits}"
    return hits[0]


# --------------------------------------------------------------------------- #
# The brief's six
# --------------------------------------------------------------------------- #

def test_a_frozen_op_stays_on_the_machine_it_is_physically_on():
    """The brief pinned CNC1 — which is where an unpinned plan puts this operation
    anyway (machines are tried in id order), so the test passed with the whole
    feature deleted. It pins CNC4 here, and the control run proves the difference
    is the pin and not the fixture."""
    jobs, shop, cfg = _fixture()
    assert _at(_run(jobs, shop, cfg), 1).machine == "CNC1"        # control
    plan = _run(jobs, shop, cfg, frozen=_frozen(machine_id="CNC4"))
    assert _at(plan, 1).machine == "CNC4"


def test_a_resumed_op_is_charged_no_setup():
    """The brief asserted ``work_min == 100 * 10.0`` — 100 being the ROW's
    ``remaining_qty``, the one number a frozen row must never supply. The batch
    owes 535 pieces, so a resumed run is 535 x 10 and not one minute of setup."""
    jobs, shop, cfg = _fixture()
    assert _at(_run(jobs, shop, cfg), 1).work_min == 90.0 + 535 * 10.0   # control
    plan = _run(jobs, shop, cfg, frozen=_frozen())
    assert _at(plan, 1).work_min == 535 * 10.0


def test_frozen_qty_comes_from_the_batch_not_the_row():
    """2026-08-11: a frozen row derived remaining_qty per SO LINE while the op it
    pins is a BATCH operation, so a clubbed order lost 281 pieces to no plan at
    all. The quantity must come from the batch's own process_remaining."""
    jobs, shop, cfg = _fixture(remaining={1: 242})
    plan = _run(jobs, shop, cfg, frozen=_frozen(remaining_qty=88))
    first = _at(plan, 1)
    assert first.qty == 242
    assert first.work_min == 242 * 10.0


def test_a_frozen_op_never_runs_before_the_step_that_feeds_it():
    """2026-08-09: frozen ops were laid at their machine's free time with no
    reference to the routing, so a free machine ran step 2 before a busy machine
    could run step 1.

    Exact times, not just an ordering: laying step 2 at CNC4's free slot puts it
    at 08:00 on day one, which ``second.start >= first.start`` alone would not
    catch (both would be 08:00)."""
    jobs, shop, cfg = _fixture()
    plan = _run(jobs, shop, cfg, frozen=_frozen(op_seq=2, machine_id="CNC4",
                                                operator="Sidhu"))
    first, second = _at(plan, 1), _at(plan, 2)
    assert second.machine == "CNC4"                  # still pinned
    assert first.start == datetime(2026, 8, 12, 8, 0)
    assert second.start >= first.end                 # overlap 1.0 -> sequential
    assert second.end >= first.end
    assert first.qty == 535 and second.qty == 535    # nothing lost to the pin


def test_the_pinned_operator_is_dropped_if_settings_no_longer_qualifies_him():
    """The machine pin stays (the work is physically there); only the person is
    re-staffed. Live 2026-08-03: removing a machine from someone with work in
    progress froze them straight back onto it.

    The brief re-built the shop with a single operator qualified for CNC1 only —
    which leaves step 2 (CNC4 only) unstaffable for ever, so the scheduler fails
    loud and the test never reaches its assertion. Here Narayan keeps his job but
    loses CNC4 in Settings, which is exactly the live edit, and the pin is the
    machine an unpinned plan would NOT have chosen."""
    jobs, shop, cfg = _fixture(operators=[
        Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
        Operator("Sidhu", "CNC1/CNC4", list(_BOTH), "First shift")])
    plan = _run(jobs, shop, cfg, frozen=_frozen(machine_id="CNC4",
                                                operator="Narayan"))
    first = _at(plan, 1)
    assert first.machine == "CNC4"                       # the pin holds
    assert {s[2] for s in first.segments} == {"Sidhu"}   # the person does not


def test_frozen_none_is_identical_to_no_frozen_argument():
    jobs, shop, cfg = _fixture()
    sig = lambda p: ([(x.job_key, x.op_seq, x.machine, x.start, x.end, x.segments,
                       x.qty, x.work_min) for x in p.placements],
                     dict(p.completion))
    a = sig(_run(jobs, shop, cfg))
    b = sig(_run(jobs, shop, cfg, frozen=None))
    c = sig(_run(jobs, shop, cfg, frozen=[]))
    assert a == b == c


# --------------------------------------------------------------------------- #
# Strengthening — each of these goes RED under a named mutation of the fix
# --------------------------------------------------------------------------- #

def test_several_clubbed_lines_in_progress_are_one_pin_and_one_operation():
    """The 2026-08-11 shape, exactly: two SO lines clubbed into one batch are BOTH
    part-finished on the same step, so freeze emits two rows for one operation.
    An operation runs once, on one machine, for what the BATCH owes.

    The pin is taken from the earliest-started row and the answer may not depend
    on the order the rows happen to arrive in — one pin per row (last wins) makes
    the two orderings disagree.
    """
    early = {"order_key": "B1", "op_seq": 1, "machine_id": "CNC4",
             "operator": "Narayan", "remaining_qty": 88,
             "prev_start": datetime(2026, 8, 11, 8, 0)}
    late = {"order_key": "B1", "op_seq": 1, "machine_id": "CNC1",
            "operator": "Sidhu", "remaining_qty": 281,
            "prev_start": datetime(2026, 8, 11, 14, 0)}
    seen = []
    for rows in ([early, late], [late, early]):
        jobs, shop, cfg = _fixture(remaining={1: 369})
        plan = _run(jobs, shop, cfg, frozen=rows)
        first = _at(plan, 1)                       # exactly ONE op-1 placement
        assert first.qty == 369                    # not 88, not 281, not 369+88
        assert first.work_min == 369 * 10.0        # and no setup, once
        seen.append((first.machine, first.start, first.end))
    assert seen[0] == seen[1] == ("CNC4", *seen[0][1:])


def test_a_part_in_the_chuck_resumes_before_new_work_on_that_machine():
    """Previous-plan order is a preference, but a machine physically holding a
    half-finished part cannot start something else first. B2 is named first in the
    sequence and would take CNC1 without the pin."""
    masters = _masters(processes=[Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                          "CNC1")])
    jobs, _by, _sk = build_jobs([_B("B1", "ITEM", 20), _B("B2", "ITEM", 20)],
                                masters)
    shop, cfg = build_shop(masters), _cfg()
    order = ["B2", "B1"]
    control = _run(jobs, shop, cfg, sequence=order)
    assert min(control.placements, key=lambda p: p.start).job_key == "B2"
    plan = _run(jobs, shop, cfg, sequence=order, frozen=_frozen(machine_id="CNC1"))
    assert min(plan.placements, key=lambda p: p.start).job_key == "B1"


def test_two_frozen_ops_on_one_machine_resume_in_previous_plan_order():
    masters = _masters(processes=[Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                          "CNC1")])
    jobs, _by, _sk = build_jobs([_B("B1", "ITEM", 20), _B("B2", "ITEM", 20)],
                                masters)
    shop, cfg = build_shop(masters), _cfg()
    rows = [{"order_key": "B1", "op_seq": 1, "machine_id": "CNC1",
             "operator": "Narayan", "remaining_qty": 20,
             "prev_start": datetime(2026, 8, 11, 15, 0)},
            {"order_key": "B2", "op_seq": 1, "machine_id": "CNC1",
             "operator": "Narayan", "remaining_qty": 20,
             "prev_start": datetime(2026, 8, 11, 9, 0)}]
    plan = _run(jobs, shop, cfg, sequence=["B1", "B2"], frozen=rows)
    # B2 started first in the previous plan, so it is the part on the machine.
    assert [p.job_key for p in sorted(plan.placements, key=lambda p: p.start)] == \
        ["B2", "B1"]


def test_a_pin_the_current_routing_cannot_honour_is_ignored_not_fatal():
    """A pin is only as good as the plan it came from. If the routing no longer
    lets that step run on that machine — or the machine has left the master —
    the step falls through to normal scheduling, exactly as freeze.py does for a
    step it cannot resolve. Refusing every machine instead would take the whole
    book out of the plan at the horizon."""
    jobs, shop, cfg = _fixture()
    for bad in ("MD1", "CNC9"):
        plan = _run(jobs, shop, cfg, frozen=_frozen(machine_id=bad))
        first = _at(plan, 1)
        assert first.machine == "CNC1"
        assert first.work_min == 90.0 + 535 * 10.0     # not frozen -> pays setup
    # An unknown job or step is ignored the same way.
    for bad in ({"order_key": "NOPE"}, {"op_seq": 99}):
        plan = _run(jobs, shop, cfg, frozen=_frozen(**bad))
        assert _at(plan, 1).machine == "CNC1"


def test_rows_in_the_apps_own_spelling_and_iso_times_are_understood():
    """``engine.freeze.compute_frozen_set`` emits ``machine`` (not ``machine_id``)
    and an ISO **string** ``prev_start``. A pin that silently does nothing because
    of a key name is the worst possible failure here: the plan looks fine and the
    part is on the wrong machine."""
    jobs, shop, cfg = _fixture()
    plan = _run(jobs, shop, cfg, frozen=[
        {"order_key": "B1", "op_seq": 1, "machine": "CNC4", "operator": "Narayan",
         "remaining_qty": 88, "prev_start": "2026-08-11T08:00:00"}])
    assert _at(plan, 1).machine == "CNC4"
    assert _at(plan, 1).work_min == 535 * 10.0


def test_the_roster_mans_the_machine_the_part_is_actually_on():
    """A pinned step's minutes belong to its pinned machine alone. Reported
    against every alternative in the routing they put a scarce operator on a
    machine that can never touch the part, the pinned machine goes dark, and with
    one operator the book never moves at all."""
    masters = Masters(
        machines={k: _MACHINES[k] for k in _BOTH},
        routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                  [Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                           "CNC1/CNC4")])},
        operators=[Operator("Narayan", "CNC1/CNC4", list(_BOTH), "First shift")],
        calendar=WorkCalendar())
    jobs, _by, _sk = build_jobs([_B("B1", "ITEM", 30)], masters)
    plan = _run(jobs, build_shop(masters), _cfg(),
                frozen=_frozen(machine_id="CNC4"))
    first = _at(plan, 1)
    assert first.machine == "CNC4"
    assert first.start == datetime(2026, 8, 12, 8, 0)


def test_a_machine_does_not_lose_its_shift_waiting_for_a_part_pinned_elsewhere():
    """``_earliest_ready`` is how a machine waits for work released later in the
    same shift. A step frozen onto ANOTHER machine is not work it can ever take,
    so waiting for it burns the rest of the shift on nothing: CNC1 would wake at
    08:30 for a part that is in CNC4's chuck, find nothing, park, and B2's own
    step would slip to the next day."""
    masters = Masters(
        machines=dict(_MACHINES),
        routings={
            "ONE": Routing("ONE", "d", "", "", None, [
                Process(1, "DEBURING", 1.0, None, None, "MD1"),
                Process(2, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")]),
            "TWO": Routing("TWO", "d", "", "", None, [
                Process(1, "DEBURING", 3.0, None, None, "MD1"),
                Process(2, "CNC FIRST SIDE", 10.0, None, None, "CNC1")]),
        },
        operators=[Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
                   Operator("Sidhu", "CNC4", ["CNC4"], "First shift"),
                   Operator("Anturam", "MD1", ["MD1"], "First shift")],
        calendar=WorkCalendar())
    jobs, _by, _sk = build_jobs([_B("B1", "ONE", 30), _B("B2", "TWO", 30)],
                                masters)
    plan = _run(jobs, build_shop(masters), _cfg(), sequence=["B1", "B2"],
                frozen=_frozen(op_seq=2, machine_id="CNC4"))
    pinned = _at(plan, 2, "B1")
    other = _at(plan, 2, "B2")
    assert (pinned.machine, pinned.start) == ("CNC4", datetime(2026, 8, 12, 8, 30))
    assert (other.machine, other.start) == ("CNC1", datetime(2026, 8, 12, 10, 0))


def test_pacing_never_binds_on_a_plan_with_frozen_work():
    """``_pace`` edits published ``end`` values without touching machine
    occupancy, so anything that makes it bind re-introduces the double-booking
    Task 5 removed by pacing at claim time instead. A frozen plan must still pass
    through it untouched."""
    jobs, shop, cfg = _fixture()
    bound = []
    real = scheduler._pace

    def spy(placements, completion):
        out = real(list(placements), completion)
        before = {(p.job_key, p.op_seq): (p.start, p.end) for p in placements}
        for p in out:
            if before[(p.job_key, p.op_seq)] != (p.start, p.end):
                bound.append((p.job_key, p.op_seq))
        return out

    scheduler._pace = spy
    try:
        _run(jobs, shop, cfg, frozen=_frozen(machine_id="CNC4"), overlap=0.5)
        _run(jobs, shop, cfg, frozen=_frozen(op_seq=2, machine_id="CNC4"),
             overlap=0.5)
    finally:
        scheduler._pace = real
    assert bound == []


_HASH_SEED_SCRIPT = textwrap.dedent("""
    from datetime import date, datetime

    from engine.config import Config
    from engine.models import (Machine, Masters, Operator, Process, Routing,
                               WorkCalendar)
    from roster_engine import scheduler
    from roster_engine.domain import build_jobs, build_shop


    class B:
        def __init__(self, key, qty):
            self.batch_id, self.item_code, self.qty = key, "ITEM", qty
            self.so_refs, self.delivery_date = [key], date(2026, 12, 1)
            self.process_remaining = {1: qty}


    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
    }
    processes = [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4"),
                 Process(2, "CNC SECOND SIDE", 5.0, None, None, "CNC4")]
    operators = [Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                 Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")]
    masters = Masters(machines=machines,
                      routings={"ITEM": Routing("ITEM", "d", "", "", None, processes)},
                      operators=operators, calendar=WorkCalendar())
    jobs, _bk, _sk = build_jobs([B("B1", 30), B("B2", 30)], masters)
    frozen = [
        {"order_key": "B1", "op_seq": 1, "machine_id": "CNC4",
         "operator": "Narayan", "remaining_qty": 5,
         "prev_start": datetime(2026, 8, 11, 8, 0)},
        {"order_key": "B1", "op_seq": 1, "machine_id": "CNC1",
         "operator": "Sidhu", "remaining_qty": 7,
         "prev_start": datetime(2026, 8, 11, 12, 0)},
        {"order_key": "B2", "op_seq": 1, "machine_id": "CNC1",
         "operator": "Sidhu", "remaining_qty": 9,
         "prev_start": datetime(2026, 8, 11, 10, 0)},
    ]
    plan = scheduler.schedule(
        jobs, [j.key for j in jobs], build_shop(masters),
        Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0),
        overlap=0.8, frozen=frozen)
    for p in sorted(plan.placements, key=lambda x: (x.job_key, x.op_seq)):
        print(p.job_key, p.op_seq, p.machine, p.start, p.end, p.qty, p.segments)
""")


def test_a_frozen_plan_is_deterministic_across_hash_seeds():
    """Pins are collected in a dict; only a fresh interpreter with a different
    string hash seed re-orders one, which is what varies between the optimizer's
    worker processes."""
    outputs = set()
    for seed in ("0", "1", "2", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run([sys.executable, "-c", _HASH_SEED_SCRIPT],
                                cwd=REPO_ROOT, env=env, capture_output=True,
                                text=True, check=True)
        outputs.add(result.stdout)
    assert len(outputs) == 1, f"plan varied across PYTHONHASHSEED: {outputs}"


# --------------------------------------------------------------------------- #
# Fix round 1 — the five findings of the 2026-08-13 review
# --------------------------------------------------------------------------- #

def _rival_masters():
    """Two CNCs; ITEM may run on either, OWN only on CNC1. OWN is what gives CNC1
    demand of its own, so the roster mans it on its own merits and the pin guard
    is the ONLY thing keeping a pinned ITEM off it."""
    return Masters(
        machines={k: _MACHINES[k] for k in _BOTH},
        routings={
            "ITEM": Routing("ITEM", "d", "", "", None,
                            [Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                     "CNC1/CNC4")]),
            "OWN": Routing("OWN", "d", "", "", None,
                           [Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                    "CNC1")]),
        },
        operators=[Operator("Narayan", "CNC1/CNC4", list(_BOTH), "First shift"),
                   Operator("Sidhu", "CNC1/CNC4", list(_BOTH), "First shift")],
        calendar=WorkCalendar())


def test_a_manned_rival_machine_may_not_take_a_pinned_part():
    """Finding 1. Every other fixture here pins the only interesting machine, so
    ``_shift_demand`` reports no demand for the rival, the roster never mans it,
    and ``_next_job``'s pin guard is masked — deleting the guard left all 15 tests
    green.

    Here Z gives CNC1 real demand of its own, so BOTH machines are manned in the
    same shift and A — pinned to CNC4 and FIRST in the sequence — is offered to
    CNC1 first. Only the guard sends it to CNC4.
    """
    masters = _rival_masters()
    jobs, _by, _sk = build_jobs([_B("A", "ITEM", 20), _B("Z", "OWN", 20)], masters)
    shop, cfg = build_shop(masters), _cfg()

    # Control: with no pin, A takes CNC1 (machines are offered in id order), so
    # the pin really is the only thing that can move it.
    assert _at(_run(jobs, shop, cfg, sequence=["A", "Z"]), 1, "A").machine == "CNC1"

    plan = _run(jobs, shop, cfg, sequence=["A", "Z"],
                frozen=_frozen(order_key="A", machine_id="CNC4"))
    pinned, rival = _at(plan, 1, "A"), _at(plan, 1, "Z")
    assert pinned.machine == "CNC4"
    assert pinned.work_min == 20 * 10.0                  # resumed: still no setup
    # Non-vacuity: CNC1 was manned and working at the same instant, on its own job.
    assert rival.machine == "CNC1"
    assert pinned.start == rival.start == datetime(2026, 8, 12, 8, 0)


def test_a_pin_no_one_can_staff_degrades_instead_of_killing_the_book():
    """Finding 2. ``_apply_frozen`` dropped a pin the ROUTING could not honour but
    not one the CREW could not: if Settings no longer qualifies anybody for the
    pinned machine, that job can never be placed and the whole book raised
    RuntimeError. One machine losing its operator must not cost every other order
    its plan — the condition is already reported separately as MACHINE_NO_OPERATOR.
    """
    masters = Masters(
        machines={k: _MACHINES[k] for k in _BOTH},
        routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                  [Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                           "CNC1/CNC4")])},
        operators=[Operator("Narayan", "CNC1", ["CNC1"], "First shift")],
        calendar=WorkCalendar())
    jobs, _by, _sk = build_jobs([_B("B1", "ITEM", 20), _B("B2", "ITEM", 20)],
                                masters)
    plan = _run(jobs, build_shop(masters), _cfg(), sequence=["B1", "B2"],
                frozen=_frozen(machine_id="CNC4"))
    pinned = _at(plan, 1, "B1")
    assert pinned.machine == "CNC1"
    assert pinned.work_min == 90.0 + 20 * 10.0     # not frozen any more -> setup
    assert _at(plan, 1, "B2").machine == "CNC1"    # the rest of the book survives
    rows = [(row.get("machine_id"), why) for row, why in plan.unpinned]
    assert len(rows) == 1 and rows[0][0] == "CNC4"
    assert "qualified" in rows[0][1]


def test_every_pin_that_could_not_be_applied_is_reported():
    """Finding 3. Against the REAL producer every row was dropped for want of a
    job key and nothing said so: the plan looked well-formed while the part was
    planned on a machine it is not on, paying a setup it does not owe. A silently
    dropped constraint is a named recurring defect class in this repo.

    The accounting closes exactly — every row is either the applied pin for its
    (job, op) or it is in ``unpinned`` — which is what Task 7's adapter checks.
    """
    jobs, shop, cfg = _fixture()
    rows = [
        dict(_frozen()[0]),                                    # applied
        dict(_frozen(order_key="NOPE")[0]),                    # no such job
        dict(_frozen(op_seq=99)[0]),                           # no such step
        dict(_frozen(machine_id="CNC9")[0]),                   # not in the master
        dict(_frozen(machine_id="MD1")[0]),                    # routing says no
        {"op_seq": 1, "machine_id": "CNC4"},                   # names no job
        dict(_frozen(operator="Sidhu",
                     prev_start=datetime(2026, 8, 11, 14, 0))[0]),   # superseded
    ]
    plan = _run(jobs, shop, cfg, frozen=rows)
    assert _at(plan, 1).machine == "CNC4"                      # one pin applied
    assert len(plan.unpinned) == len(rows) - 1                 # ...and six dropped
    whys = [why for _row, why in plan.unpinned]
    assert sum("job" in w for w in whys) >= 2                  # unknown job x2
    assert any("step 99" in w for w in whys)
    assert any("CNC9" in w for w in whys)
    assert any("MD1" in w for w in whys)
    assert any("superseded" in w for w in whys)
    # The rows come back verbatim, so a caller can name the SO on screen.
    assert plan.unpinned[0][0]["order_key"] == "NOPE"


def test_a_plan_with_nothing_frozen_reports_no_dropped_pins():
    jobs, shop, cfg = _fixture()
    assert _run(jobs, shop, cfg).unpinned == ()
    assert _run(jobs, shop, cfg, frozen=[]).unpinned == ()
    assert _run(jobs, shop, cfg, frozen=_frozen()).unpinned == ()


def test_the_planned_operator_is_kept_on_a_part_that_is_still_in_the_chuck():
    """Finding 4. The pinned OPERATOR was ignored entirely, so every re-plan
    free-reassigned the person on a physically-running job even when the planned
    person was still qualified and free. What churns is the name on the shift-wise
    sheet — a live floor document the directors read.

    Both people are equally eligible and equally free, so nothing but the
    preference can decide this: with no operator term in the value function the
    matching picks by index and Narayan wins both runs.
    """
    for who in ("Narayan", "Sidhu"):
        jobs, shop, cfg = _fixture()
        plan = _run(jobs, shop, cfg, frozen=_frozen(machine_id="CNC4",
                                                    operator=who))
        first = _at(plan, 1)
        assert first.machine == "CNC4"
        assert {s[2] for s in first.segments} == {who}


def test_a_preferred_operator_who_is_busy_elsewhere_does_not_starve_a_machine():
    """The bonus is a tie-break, not a reservation: one person cannot be preferred
    onto two machines at once, and preferring them on one must not leave the other
    dark. Sidhu is named on BOTH pinned parts; he can only have one."""
    masters = _rival_masters()
    jobs, _by, _sk = build_jobs([_B("A", "ITEM", 20), _B("Z", "OWN", 20)], masters)
    plan = _run(jobs, build_shop(masters), _cfg(), sequence=["A", "Z"], frozen=[
        {"order_key": "A", "op_seq": 1, "machine_id": "CNC4", "operator": "Sidhu",
         "prev_start": datetime(2026, 8, 11, 8, 0)},
        {"order_key": "Z", "op_seq": 1, "machine_id": "CNC1", "operator": "Sidhu",
         "prev_start": datetime(2026, 8, 11, 8, 0)},
    ])
    staffed = {_at(plan, 1, k).machine: {s[2] for s in _at(plan, 1, k).segments}
               for k in ("A", "Z")}
    assert set(staffed) == {"CNC1", "CNC4"}          # neither machine went dark
    assert staffed["CNC1"] | staffed["CNC4"] == {"Narayan", "Sidhu"}
    assert "Sidhu" in staffed["CNC4"] | staffed["CNC1"]


def test_a_frozen_step_is_not_charged_a_setup_in_the_shift_demand_estimate():
    """Finding 5. ``_shift_demand`` calls ``_setup_min_for`` so a resumed step is
    costed the way it will really run; charging the nominal 90 minutes there
    over-states its demand and can move the roster in a tight shift, while the
    real placement pays nothing. Deleting that call left all 15 tests green.

    One operator, two machines, and the two demands 90 minutes apart: B2's 100
    minutes on CNC1 beat B1's 60 resumed minutes on CNC4, so B2 runs first. Charge
    B1 a phantom setup and it becomes 150 and takes the shift instead.
    """
    masters = Masters(
        machines={k: _MACHINES[k] for k in _BOTH},
        routings={
            "ITEM": Routing("ITEM", "d", "", "", None,
                            [Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                     "CNC1/CNC4")]),
            "OWN": Routing("OWN", "d", "", "", None,
                           [Process(1, "CNC FIRST SIDE", 10.0, None, None,
                                    "CNC1")]),
        },
        operators=[Operator("Narayan", "CNC1/CNC4", list(_BOTH), "First shift")],
        calendar=WorkCalendar())
    jobs, _by, _sk = build_jobs([_B("B1", "ITEM", 6), _B("B2", "OWN", 1)], masters)
    plan = _run(jobs, build_shop(masters), _cfg(), sequence=["B1", "B2"],
                frozen=_frozen(machine_id="CNC4"))
    assert _at(plan, 1, "B1").machine == "CNC4"          # the pin still holds
    first_out = min(plan.placements, key=lambda p: (p.start, p.job_key))
    assert first_out.job_key == "B2"
    assert first_out.start == datetime(2026, 8, 12, 8, 0)
