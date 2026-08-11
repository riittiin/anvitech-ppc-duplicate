"""Off-machine steps (DISPATCH / OS outsourcing) + alternatives-everywhere.

A step with NO machine and NO cycle time (DISPATCH, an outside-service OS step) is
**included as a visible zero-duration milestone** — no machine, no operator, no time,
but it IS scheduled and shown (OS / outsourcing must be visible, not ignored). A
blank-machine step that DOES have a cycle time is still NOT scheduled on a phantom
station (missing data must fail loud). Alternative-machine '/' logic + parallel split
apply to ANY step (e.g. inspection), not just CNC."""
from datetime import date

from engine.config import Config, OVERLAP_PERCENT
from engine.models import (Batch, Process, Routing, Machine, WorkCalendar, Masters, Operator)
from engine.rules import rule6_allocate


def _masters(processes, machines=("M",), operators=None):
    ms = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5) for m in machines}
    masters = Masters(machines=ms, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None, processes=processes)
    if operators is not None:
        masters.operators = operators
    return masters


def _batch(qty=10):
    return Batch(batch_id="B", item_code="X", item_name="x", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["B"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)


def test_dispatch_step_is_included_as_milestone():
    procs = [Process(1, "OP", 10, 10, "M", None),
             Process(2, "DISPATCH", None, None, None, None)]   # blank machine, no time
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    by_seq = {e.process_seq: e for e in sched}
    assert 1 in by_seq and 2 in by_seq                 # BOTH scheduled now
    d = by_seq[2]
    assert d.occupancy_min == 0 and d.start == d.end    # zero-duration milestone
    assert d.machine == "Off-machine" and not d.operator


def test_leading_os_step_is_included_as_milestone():
    procs = [Process(1, "BANDSAW OS", None, None, None, None),  # outsourced, no time
             Process(2, "OP", 10, 10, "M", None)]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    by_seq = {e.process_seq: e for e in sched}
    assert 1 in by_seq and 2 in by_seq                 # OS step shown, real op runs
    assert by_seq[1].machine == "OS / Outsourced"      # labelled as outsourcing
    assert by_seq[1].occupancy_min == 0
    # The zero-duration OS milestone must not push the real op later than a plain run.
    plain = rule6_allocate.run([_batch()], config=_cfg(),
                               masters=_masters([Process(1, "OP", 10, 10, "M", None)]))
    assert by_seq[2].start == plain[0].start


def test_dispatch_does_not_block_under_operator_logic():
    ops = [Operator("op", "M", machines=["M"], shift="First shift"),
           Operator("op2", "M", machines=["M"], shift="Second shift")]
    procs = [Process(1, "OP", 10, 10, "M", None),
             Process(2, "DISPATCH", None, None, None, None)]
    sched = rule6_allocate.run([_batch()],
                               config=_cfg(apply_operator_logic=True),
                               masters=_masters(procs, operators=ops))
    assert any(e.process_seq == 1 for e in sched)      # planned, not blocked
    assert any(e.process_seq == 2 for e in sched)      # DISPATCH milestone included


def test_blank_machine_with_cycle_time_fails_loud():
    # Missing-data guard: a blank machine but a real cycle time is NOT scheduled on an
    # invented station — it fails loud (batch held + flagged) so the gap surfaces.
    procs = [Process(1, "MYSTERY", 30, 30, None, None)]   # blank machine, HAS time
    notes = []
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs), notes=notes)
    assert all(e.process_seq != 1 for e in sched)          # NOT scheduled on a phantom
    assert any("MYSTERY" in n and "machine" in n.lower() for n in notes)   # flagged loudly


def test_inspection_alternatives_split_in_parallel():
    # Alternatives + split are generic — inspection MI1/MI2/MI3 behaves like CNC.
    machines = {m: Machine(m, m, "Manual Inspection", available_hrs_per_day=9.5)
                for m in ("MI1", "MI2", "MI3")}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None,
                                    processes=[Process(1, "INSPECTION", 10, 10, "MI1/MI2/MI3", None)])
    masters.operators = [Operator(m, m, machines=[m], shift="First shift") for m in ("MI1", "MI2", "MI3")]
    sched = rule6_allocate.run([_batch(60)],
                               config=_cfg(apply_operator_logic=True, split_parallel=True, split_min_qty=2),
                               masters=masters)
    used = {e.machine for e in sched}
    assert len(sched) >= 2                              # split happened
    assert used <= {"MI1", "MI2", "MI3"} and len(used) >= 2


def test_dispatch_waits_for_all_processes_not_just_predecessor():
    # SLOW step (one machine), then a FAST step (another machine) that OVERLAPS and
    # finishes BEFORE slow. DISPATCH must wait for the LATEST-finishing process (slow),
    # not the fast step's overlap point — you can't ship before every piece is done.
    procs = [Process(1, "SLOW", 10, 10, "M", None),
             Process(2, "FAST", 1, 1, "M2", None),
             Process(3, "DISPATCH", None, None, None, None)]
    cfg = _cfg(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    sched = rule6_allocate.run([_batch(100)], config=cfg,
                               masters=_masters(procs, machines=("M", "M2")))
    by_seq = {}
    for e in sched:
        by_seq.setdefault(e.process_seq, e)
    slow, fast, disp = by_seq[1], by_seq[2], by_seq[3]
    assert fast.start < slow.end              # overlap: fast STARTS early (pipelined)
    assert disp.start == slow.end             # DISPATCH waits for the LATEST process (slow)
    assert disp.occupancy_min == 0 and disp.start == disp.end   # still a zero-duration milestone


def test_fast_step_cannot_finish_before_its_predecessor():
    # A FAST step overlaps a SLOW predecessor: it STARTS early (pipelined) but can't
    # FINISH before the slow step delivers the last piece — it is paced to the slow end.
    procs = [Process(1, "SLOW", 10, 10, "M", None),
             Process(2, "FAST", 1, 1, "M2", None)]
    cfg = _cfg(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    sched = rule6_allocate.run([_batch(100)], config=cfg,
                               masters=_masters(procs, machines=("M", "M2")))
    slow = [e for e in sched if e.process_seq == 1][0]
    fast = [e for e in sched if e.process_seq == 2][0]
    assert fast.start < slow.end              # overlap START kept (begins before slow ends)
    assert fast.end == slow.end               # paced: finishes no earlier than its predecessor
