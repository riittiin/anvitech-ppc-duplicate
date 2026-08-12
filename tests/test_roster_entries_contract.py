"""The seam: roster_engine -> the ScheduleEntry list every existing surface reads.

Three output contracts are pinned here because breaking any of them fails
SILENTLY on a live screen:

  * ``op_segments`` is a LIST of ``(start, end, operator)`` tuples sorted by start
    — ``ScheduleEntry.operator_label``, the shift-wise export, Analytics' operator
    hours and the delay report all read it, and ``rule6_allocate`` even assigns
    into it by index, so a tuple would raise there;
  * OS / off-machine entries carry the EXACT lane strings the consumers match on
    (``delay_report._OFF_LANES``, ``analytics.NON_MACHINE_LANES``,
    ``freeze._OS_LANES``) — the 2026-08-09 defect was outsourcing being billed to
    an in-house machine;
  * every real-machine entry that occupies time names an operator, because
    ``engine/freeze.py`` pins machine AND operator.

The frozen-row translation has its own section. The rows are built by actually
calling ``engine.freeze.compute_frozen_set`` — the real producer — because its
rows carry NO ``order_key`` and are keyed per SO LINE while a roster ``Job.key``
is a BATCH id. Fed raw, every row is dropped and the plan looks perfectly
well-formed while the in-progress part is planned on a machine it is not on.
"""

from datetime import date, datetime

import pytest

from engine import roster_adapter
from engine.config import OVERLAP_PERCENT, Config
from engine.freeze import compute_frozen_set
from engine.models import (Batch, Machine, Masters, Operator, Process, Routing,
                           ScheduleEntry, SOLine, WorkCalendar)

PLAN_START = date(2026, 8, 12)

_MACHINES = {
    "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
    "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
    "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
}

# Seq, name, cycle_time, total_time, suggested_machine, allotted_machine — the
# REAL Process signature. The brief's fixture used Routing("ITEM", "d", [...]),
# which puts the process list in the `customer` slot and leaves the routing empty,
# so every job would have been skipped for having no routing at all.
_PROCESSES = [
    Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4"),
    Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
    Process(3, "DEBURING", 2.0, None, None, "MD1"),
    Process(4, "DISPATCH", None, None, None, None),
]


def _masters(processes=None, operators=None):
    return Masters(
        machines=dict(_MACHINES),
        routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                  list(processes or _PROCESSES))},
        operators=list(operators or [
            Operator("Anturam", "MD1", ["MD1"], "First shift"),
            Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
            Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
        ]),
        calendar=WorkCalendar())


def _batch(batch_id="B001", item="ITEM", qty=20, so_refs=("26-27SO1",),
           process_qty=None):
    """The REAL ``engine.models.Batch``: item_name, so_delivery_date and
    source_so_refs — not the brief's delivery_date/so_refs."""
    return Batch(batch_id=batch_id, item_code=item, item_name="Item", qty=qty,
                 so_delivery_date=date(2026, 12, 1),
                 source_so_refs=list(so_refs), process_qty=process_qty)


def _cfg(**kw):
    kw.setdefault("plan_start_date", PLAN_START)
    kw.setdefault("scheduler", "roster")
    kw.setdefault("setup_time_min", 90)
    kw.setdefault("overlap_mode", OVERLAP_PERCENT)
    kw.setdefault("overlap_percent", 80)
    return Config(**kw)


def _run(batches=None, masters=None, notes=None, **kw):
    return roster_adapter.run(batches if batches is not None else [_batch()],
                              config=kw.pop("config", None) or _cfg(),
                              notes=notes,
                              masters=masters if masters is not None else _masters(),
                              **kw)


def _at(entries, seq, batch_id="B001"):
    hits = [e for e in entries if e.process_seq == seq and e.batch_id == batch_id]
    assert len(hits) == 1, f"expected exactly one entry for step {seq}: {hits}"
    return hits[0]


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

def test_returns_schedule_entries():
    entries = _run()
    assert entries and all(isinstance(e, ScheduleEntry) for e in entries)
    # every routing step is published, milestones included
    assert sorted(e.process_seq for e in entries) == [1, 2, 3, 4]


def test_op_segments_are_start_end_operator_triples_in_order():
    for e in _run():
        assert isinstance(e.op_segments, list), "rule6_allocate assigns by index"
        for seg in e.op_segments:
            assert isinstance(seg, tuple) and len(seg) == 3
            assert isinstance(seg[0], datetime) and isinstance(seg[1], datetime)
            assert isinstance(seg[2], str)
        starts = [s[0] for s in e.op_segments]
        assert starts == sorted(starts)


def test_every_machine_entry_names_an_operator():
    """engine/freeze.py pins machine AND operator; an empty operator freezes a
    ghost. Entries that occupy no time at all are milestones in all but name."""
    seen = 0
    for e in _run():
        if e.machine in (roster_adapter.OS_LANE, roster_adapter.OFF_LANE):
            continue
        if e.end <= e.start:
            continue
        seen += 1
        assert e.operator, f"{e.process_name} has no operator"
        assert e.op_segments and all(seg[2] for seg in e.op_segments)
        assert e.operator == e.op_segments[0][2]
    assert seen >= 2, "fixture must actually produce real-machine entries"


def test_off_lane_names_match_the_consumers():
    """delay_report._OFF_LANES, analytics.NON_MACHINE_LANES and freeze._OS_LANES
    all match on these exact strings. Emit anything else and outsourcing gets
    billed to an in-house machine again (the 2026-08-09 defect)."""
    from engine.analytics import NON_MACHINE_LANES
    from engine.delay_report import _OFF_LANES
    from engine.freeze import _OS_LANES

    lanes = {roster_adapter.OS_LANE, roster_adapter.OFF_LANE}
    assert lanes == _OFF_LANES == NON_MACHINE_LANES == _OS_LANES

    entries = _run()
    assert _at(entries, 2).machine == roster_adapter.OS_LANE     # BAND SAW OS
    assert _at(entries, 4).machine == roster_adapter.OFF_LANE    # DISPATCH
    for seq in (2, 4):
        assert _at(entries, seq).op_segments == []
        assert _at(entries, seq).operator == ""


def test_so_refs_batch_id_and_qty_survive_onto_every_entry():
    entries = _run([_batch(so_refs=("26-27SO1", "26-27SO2"))])
    for e in entries:
        assert e.batch_id == "B001"
        assert e.item_code == "ITEM"
        assert list(e.so_refs) == ["26-27SO1", "26-27SO2"]
    assert _at(entries, 1).qty == 20
    assert _at(entries, 1).occupancy_min == 90.0 + 20 * 10.0


def test_operator_label_renders_a_shift_handoff():
    e = ScheduleEntry("B1", "ITEM", 1, "P", "CNC1", 10, 60.0,
                      datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 20, 0),
                      op_segments=[(datetime(2026, 8, 12, 8, 0),
                                    datetime(2026, 8, 12, 19, 0), "Narayan"),
                                   (datetime(2026, 8, 12, 19, 0),
                                    datetime(2026, 8, 12, 20, 0), "Sidhu")])
    assert e.operator_label() == "Narayan → Sidhu"


def test_a_real_entry_survives_the_downstream_consumers():
    """The whole point of the seam: existing surfaces consume the output
    unchanged. schedule_projection is the strictest reader (it wants machine,
    operator and ISO times on every in-house op)."""
    from engine.freeze import schedule_projection

    rows = schedule_projection(_run())
    assert rows and all(r["machine"] in _MACHINES for r in rows)
    assert all(r["operator"] for r in rows)
    assert {r["process_seq"] for r in rows} == {1, 3}   # OS/DISPATCH excluded


def test_an_unrouted_item_is_skipped_and_reported_not_raised():
    notes = []
    entries = _run([_batch(), _batch("B002", item="GHOST")], notes=notes)
    assert {e.batch_id for e in entries} == {"B001"}
    assert any("GHOST" in n for n in notes), notes


def test_empty_batches_returns_empty():
    assert _run([]) == []
    assert roster_adapter.run([_batch()], config=_cfg(), masters=None) == []


def test_identical_inputs_give_byte_identical_entries():
    sig = lambda es: [(e.batch_id, e.process_seq, e.machine, e.qty,
                       e.occupancy_min, e.start, e.end, e.operator,
                       tuple(e.op_segments), tuple(e.so_refs)) for e in es]
    assert sig(_run()) == sig(_run())


def test_pipeline_dispatches_roster_and_nothing_else_moves():
    from engine import flow_scheduler, new_engine, pipeline
    from engine.rules import rule6_allocate

    assert pipeline.scheduler_for(_cfg()) is roster_adapter.run
    assert pipeline.scheduler_for(Config()) is rule6_allocate.run
    assert pipeline.scheduler_for(Config(scheduler="classic")) is rule6_allocate.run
    assert pipeline.scheduler_for(Config(scheduler="new")) is new_engine.run
    assert pipeline.scheduler_for(Config(scheduler="flow")) is flow_scheduler.run


def test_operator_absences_reach_the_shop():
    """``reserved`` is how operator absences arrive (optimize_service.
    absence_reservations). Drop it and the plan books people who are on leave."""
    operators = [Operator("Anturam", "MD1", ["MD1"], "First shift"),
                 Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")]
    away = {"Narayan": [(datetime(2026, 8, 12, 0, 0), datetime(2026, 8, 14, 0, 0))]}
    masters = _masters(operators=operators)

    free = _at(_run([_batch()], masters=masters), 1)
    assert free.operator == "Narayan" and free.start == datetime(2026, 8, 12, 8, 0)

    absent = _at(_run([_batch()], masters=masters, reserved=away), 1)
    assert absent.start >= datetime(2026, 8, 14, 8, 0), \
        "an absent operator was rostered anyway"


def test_a_reserved_key_that_is_not_an_operator_is_reported():
    notes = []
    _run(reserved={"CNC1": [(datetime(2026, 8, 12, 0, 0),
                             datetime(2026, 8, 13, 0, 0))]}, notes=notes)
    assert any("CNC1" in n for n in notes), notes


# --------------------------------------------------------------------------- #
# The frozen-row translation — SO LINE rows onto BATCH operations
# --------------------------------------------------------------------------- #

def _applied_rows(machine="CNC4", operator="Sidhu", so_refs=("26-27SO1",),
                  seq=1, name="CNC FIRST SIDE"):
    """What ``freeze.schedule_projection`` writes for the applied plan."""
    return [{"batch_id": "B999", "item_code": "ITEM", "process_seq": seq,
             "process_name": name, "machine": machine, "operator": operator,
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T18:00:00",
             "so_refs": list(so_refs)}]


def _so_line(so_no="26-27SO1", remaining=12):
    return SOLine(so_no=so_no, item_code="ITEM", item_name="Item", qty=20,
                  delivery_date=date(2026, 12, 1),
                  process_qty={"CNC FIRST SIDE": remaining})


def _real_frozen(applied_rows=None, so_lines=None, good=8):
    """Rows in ``compute_frozen_set``'s EXACT shape, from the real producer:
    {so_no, item_code, process, op_seq, machine, operator, remaining_qty,
    prev_start} — no order_key, no machine_id, keyed per SO LINE."""
    lines = so_lines or [_so_line()]
    rows = compute_frozen_set(
        applied_rows if applied_rows is not None else _applied_rows(),
        lines,
        {(ln.so_no, ln.item_code, "CNC FIRST SIDE"): good for ln in lines},
        _masters())
    assert rows, "the fixture must actually produce frozen rows"
    for r in rows:
        assert set(r) == {"so_no", "item_code", "process", "op_seq", "machine",
                          "operator", "remaining_qty", "prev_start"}
    return rows


def test_the_real_producers_rows_actually_pin():
    """Fed raw, 100% of these rows are dropped: they carry no order_key and a
    roster Job.key is a BATCH id. The plan then looks perfectly well-formed while
    the part is planned on a machine it is not physically on, paying a 90-minute
    setup it does not owe."""
    notes = []
    control = _at(_run(), 1)
    assert control.machine == "CNC1"                      # unpinned choice
    assert control.occupancy_min == 90.0 + 20 * 10.0      # ...and it pays setup

    entries = _run(frozen=_real_frozen(), notes=notes)
    pinned = _at(entries, 1)
    assert pinned.machine == "CNC4", "the frozen row did not pin"
    assert pinned.occupancy_min == 20 * 10.0, "a resumed op paid a setup"
    assert not [n for n in notes if "pin" in n.lower()], notes


def test_the_quantity_comes_from_the_batch_never_from_the_row():
    """2026-08-11, director escalation: ``remaining_qty`` is ONE SO line's
    remainder while the op it pins is a BATCH operation. Taking it dropped 281
    pieces of a clubbed order into no plan at all."""
    batch = _batch(qty=369, so_refs=("26-27SO1", "26-27SO2"),
                   process_qty={"CNC FIRST SIDE": 242})
    rows = _real_frozen(so_lines=[_so_line("26-27SO1", 88)])
    assert rows[0]["remaining_qty"] == 88
    pinned = _at(_run([batch], frozen=rows), 1)
    assert pinned.qty == 242, "the row's per-SO-line remainder became the qty"
    assert pinned.occupancy_min == 242 * 10.0


def test_several_clubbed_lines_collapse_to_one_pin_and_one_operation():
    """Two SO lines clubbed into one batch, both part-finished on the same step:
    freeze emits two rows, but an operation runs once, on one machine."""
    batch = _batch(qty=369, so_refs=("26-27SO1", "26-27SO2"),
                   process_qty={"CNC FIRST SIDE": 242})
    applied = (_applied_rows(machine="CNC4", so_refs=("26-27SO1",))
               + _applied_rows(machine="CNC4", so_refs=("26-27SO2",)))
    rows = _real_frozen(applied, [_so_line("26-27SO1", 88),
                                  _so_line("26-27SO2", 154)])
    assert len(rows) == 2
    seen = []
    for order in (rows, list(reversed(rows))):
        entries = _run([batch], frozen=order)
        pinned = _at(entries, 1)               # exactly ONE step-1 entry
        assert pinned.qty == 242
        assert pinned.occupancy_min == 242 * 10.0
        seen.append((pinned.machine, pinned.start, pinned.end))
    assert seen[0] == seen[1] == ("CNC4", *seen[0][1:])


def test_a_spaced_machine_name_is_normalised_before_it_is_compared():
    """The applied plan can spell a machine the Machine master's way ("CNC 4").
    ``op.machine_options`` are normalised by ``loaders.parse_resource_candidates``,
    so an un-normalised pin is compared against "CNC4" and silently dropped."""
    rows = _real_frozen(_applied_rows(machine="CNC 4"))
    assert rows[0]["machine"] == "CNC 4"
    assert _at(_run(frozen=rows), 1).machine == "CNC4"


def test_prev_start_is_read_as_a_datetime_or_an_iso_string():
    """compute_frozen_set emits an ISO string; other callers hand a datetime."""
    iso = _real_frozen()
    native = [dict(r, prev_start=datetime.fromisoformat(r["prev_start"]))
              for r in iso]
    assert _at(_run(frozen=iso), 1).machine == "CNC4"
    assert _at(_run(frozen=native), 1).machine == "CNC4"


def test_a_pin_that_could_not_be_applied_is_reported_never_dropped_in_silence():
    """The accounting closes: every row handed to the scheduler is either an
    applied pin or is named in ``Plan.unpinned``, and the caller is told."""
    notes = []
    rows = _real_frozen(_applied_rows(machine="MD1"))   # routing says CNC1/CNC4
    entries = _run(frozen=rows, notes=notes)
    assert _at(entries, 1).machine == "CNC1"            # falls through, not fatal
    assert _at(entries, 1).occupancy_min == 90.0 + 20 * 10.0
    joined = " | ".join(notes)
    assert "MD1" in joined and "26-27SO1" in joined, notes


def test_a_row_whose_so_line_is_in_no_batch_is_reported():
    """A completed or deleted SO leaves a frozen row behind. It cannot pin
    anything — and that must be said, not swallowed."""
    notes = []
    rows = _real_frozen(_applied_rows(so_refs=("26-27SO9",)),
                        [_so_line("26-27SO9", 12)])
    entries = _run(frozen=rows, notes=notes)
    assert _at(entries, 1).machine == "CNC1"
    assert any("26-27SO9" in n for n in notes), notes


def test_frozen_none_and_empty_are_identical_to_no_frozen_at_all():
    sig = lambda es: [(e.batch_id, e.process_seq, e.machine, e.start, e.end,
                       e.qty, e.occupancy_min) for e in es]
    base = sig(_run())
    assert sig(_run(frozen=None)) == base
    assert sig(_run(frozen=[])) == base
    assert sig(_run(frozen={})) == base


def test_frozen_may_also_arrive_grouped_by_machine():
    """``pipeline.run_forward``'s docstring types ``frozen`` as {machine id ->
    rows} while ``book_store.load_frozen_ops`` returns a flat list. Both work."""
    rows = _real_frozen()
    assert _at(_run(frozen={"CNC4": rows}), 1).machine == "CNC4"


@pytest.mark.parametrize("bad", [
    {"op_seq": 99}, {"machine": "CNC9"}, {"machine": ""},
    {"so_no": "NOPE"}, {"op_seq": "x"},
])
def test_a_malformed_or_stale_row_never_raises(bad):
    notes = []
    rows = [dict(_real_frozen()[0], **bad)]
    entries = _run(frozen=rows, notes=notes)
    assert _at(entries, 1).machine == "CNC1"
    assert notes, f"row {bad} was dropped in silence"
