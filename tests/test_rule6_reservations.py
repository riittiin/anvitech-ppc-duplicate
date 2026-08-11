from datetime import date, datetime
from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _masters():
    m = Masters(machines={"M": Machine("M", "M", "mill")}, calendar=WorkCalendar())
    m.routings["X"] = Routing("X", "", "", "", None, processes=[
        Process(1, "op", cycle_time=10, total_time=None, suggested_machine="M",
                allotted_machine=None)])
    return m


def _batch():
    return Batch("B1", "X", "X", 1, date(2026, 3, 7), source_so_refs=["SO"])


def test_reserved_none_is_unchanged():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    base = rule6_allocate.run([_batch()], config=cfg, masters=_masters())
    same = rule6_allocate.run([_batch()], config=cfg, masters=_masters(), reserved=None)
    assert [(e.machine, e.start, e.end) for e in base] == \
           [(e.machine, e.start, e.end) for e in same]


def test_op_is_pushed_past_a_reserved_block_on_its_machine():
    cfg = Config(plan_start_date=date(2025, 3, 5))          # Wed 08:00 start
    # Reserve machine M for the first 2 hours; the 10-min op must start at/after 10:00.
    reserved = {"M": [(datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 5, 10, 0))]}
    sched = rule6_allocate.run([_batch()], config=cfg, masters=_masters(), reserved=reserved)
    op = sched[0]
    assert op.start >= datetime(2025, 3, 5, 10, 0)
    assert op.end <= datetime(2025, 3, 5, 10, 30)          # ran right after the block


def test_split_entries_never_overlap_a_reservation():
    """Regression: split parallel halves are pushed clear of reserved intervals."""
    # Two alternative machines M, N; item X may run on either.
    masters = Masters(
        machines={
            "M": Machine("M", "M", "mill"),
            "N": Machine("N", "N", "mill")
        },
        calendar=WorkCalendar()
    )
    masters.routings["X"] = Routing(
        "X", "", "", "", None,
        processes=[
            Process(1, "op", cycle_time=1, total_time=None,
                   suggested_machine="M/N", allotted_machine=None)
        ]
    )
    cfg = Config(plan_start_date=date(2025, 3, 5), split_parallel=True, split_min_qty=1)
    # Qty > 1 and split_min_qty=1 ensures split will attempt to run on both machines.
    batch = Batch("B1", "X", "X", 600, date(2025, 3, 7), source_so_refs=["SO"])
    # Reserve machine N for 30 min at the start so a split half can't sit there.
    reserved = {"N": [(datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 5, 8, 30))]}
    sched = rule6_allocate.run([batch], config=cfg, masters=masters, reserved=reserved)
    # Assert all scheduled entries respect reservations on their machine.
    for e in sched:
        for (rs, re_) in reserved.get(e.machine, []):
            # No overlap: entry end <= reservation start OR entry start >= reservation end
            assert not (e.start < re_ and e.end > rs), \
                f"entry on {e.machine} {e.start}-{e.end} overlaps reservation {rs}-{re_}"
    assert sched, "expected a schedule"
