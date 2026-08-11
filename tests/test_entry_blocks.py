"""One display entry per CONTINUOUS BLOCK, not per operation (2026-08-09, step 1 of
docs/superpowers/specs/2026-08-09-idle-gap-harvest-design.md).

`_entries_from_schedule` groups every segment of an operation into ONE entry spanning
min(start)..max(end). That is correct today, because an operation is never interrupted
by another job. It stops being correct the moment the idle-gap harvest splits a job to
fill a hole: the same operation would appear as two blocks days apart and collapse into
one entry, drawing a Gantt bar straight ACROSS the gap just freed, feeding false
ROUTING_ORDER_VIOLATIONs, over-counting RUNNING hours and publishing the wrong qty.

The rule, and the scoping that makes this step safe:

  * Nights, weekly offs and shift changes DO NOT split an entry. An operation already
    spans those every day; they must stay one entry, exactly as now.
  * Only a REAL interruption splits it — the machine ran some OTHER job in between.

Nothing in today's plans is interrupted, so this change is a no-op for every current
schedule: same entries, same dates, same everything. That is the point — it can be
shipped and verified on its own before the harvest exists.
"""
from datetime import datetime, timedelta

from engine.new_engine import _entries_from_schedule
from ppc_engine.domain.routing import OperationKind
from ppc_engine.scheduler.schedule import Schedule, Segment

T0 = datetime(2025, 3, 3, 8, 0)
KEY = ("B1", "X")


def _seg(machine, s, e, op_seq=1, key=KEY, qty=100, operator="Alpha",
         kind=OperationKind.MACHINING, name="CNC FIRST SIDE"):
    return Segment(key, op_seq, name, kind, machine, operator, s, e, qty)


def _entries(segments):
    return _entries_from_schedule(Schedule(tuple(segments), {}), {})


def test_an_operation_spanning_a_night_stays_ONE_entry():
    """The everyday case. A job runs 08:00-19:00, stops for the night, resumes 08:00.
    Nothing interrupted it — it must stay a single entry, as it is today."""
    segs = [_seg("CNC1", T0, T0 + timedelta(hours=11)),
            _seg("CNC1", T0 + timedelta(days=1), T0 + timedelta(days=1, hours=4))]
    out = _entries(segs)
    assert len(out) == 1, [(e.start, e.end) for e in out]
    assert out[0].start == T0
    assert out[0].end == T0 + timedelta(days=1, hours=4)
    assert len(out[0].op_segments) == 2      # both shifts still visible underneath


def test_an_operation_interrupted_by_another_job_becomes_TWO_entries():
    """The case the harvest creates: 120 pieces early in a hole, the rest later, with
    somebody else's job on that machine in between. One entry per block."""
    a_start, a_end = T0, T0 + timedelta(hours=2)
    other_s, other_e = T0 + timedelta(hours=3), T0 + timedelta(hours=7)
    b_start, b_end = T0 + timedelta(hours=8), T0 + timedelta(hours=10)
    segs = [
        _seg("CNC1", a_start, a_end, qty=120),
        _seg("CNC1", other_s, other_e, key=("B2", "Y"), qty=50, operator="Bravo"),
        _seg("CNC1", b_start, b_end, qty=380),
    ]
    ours = [e for e in _entries(segs) if e.batch_id == "B1"]
    assert len(ours) == 2, [(e.start, e.end) for e in ours]
    ours.sort(key=lambda e: e.start)
    assert (ours[0].start, ours[0].end) == (a_start, a_end)
    assert (ours[1].start, ours[1].end) == (b_start, b_end)
    # Neither block may claim the gap the other job occupied.
    assert ours[0].end <= other_s and ours[1].start >= other_e


def test_each_block_reports_its_OWN_quantity():
    """A split job made 120 pieces early and 380 later. Publishing 120 for both (the
    old `qty=first.qty`) would understate the second block on every sheet."""
    segs = [
        _seg("CNC1", T0, T0 + timedelta(hours=2), qty=120),
        _seg("CNC1", T0 + timedelta(hours=3), T0 + timedelta(hours=7),
             key=("B2", "Y"), qty=50, operator="Bravo"),
        _seg("CNC1", T0 + timedelta(hours=8), T0 + timedelta(hours=10), qty=380),
    ]
    ours = sorted((e for e in _entries(segs) if e.batch_id == "B1"),
                  key=lambda e: e.start)
    assert [e.qty for e in ours] == [120.0, 380.0]


def test_each_block_reports_its_own_occupancy_not_the_whole_span():
    segs = [
        _seg("CNC1", T0, T0 + timedelta(hours=2), qty=120),
        _seg("CNC1", T0 + timedelta(hours=3), T0 + timedelta(hours=7),
             key=("B2", "Y"), qty=50, operator="Bravo"),
        _seg("CNC1", T0 + timedelta(hours=8), T0 + timedelta(hours=10), qty=380),
    ]
    ours = sorted((e for e in _entries(segs) if e.batch_id == "B1"),
                  key=lambda e: e.start)
    assert [e.occupancy_min for e in ours] == [120.0, 120.0]


def test_a_gap_with_nothing_in_it_does_not_split_the_entry():
    """A hole on the machine that nobody else used is just idle time inside the job's
    span (a starved op waiting on its predecessor). Not an interruption."""
    segs = [_seg("CNC1", T0, T0 + timedelta(hours=2)),
            _seg("CNC1", T0 + timedelta(hours=6), T0 + timedelta(hours=8))]
    assert len(_entries(segs)) == 1


def test_off_lane_steps_are_untouched():
    """OS / dispatch milestones have no machine, so nothing can interrupt them."""
    segs = [Segment(KEY, 1, "BANDSAW OS", OperationKind.OUTSOURCED, None, None,
                    T0, T0 + timedelta(days=2), 100)]
    out = _entries(segs)
    assert len(out) == 1 and out[0].machine == "OS / Outsourced"
