"""The four invariants, CHECKED — and, just as important, SILENT on legal plans.

This codebase's rule is that an invariant which is checked beats one that is
merely intended (2026-08-07). Its other rule, learned the same way, is that a
report may never attribute a cause it did not check (2026-08-09) — a banner that
cries wolf on a legal plan trains the directors to ignore it, which is worse than
no banner at all.

So every check here is pinned TWICE: once on a plan that really violates it, and
once on a legal plan that plausibly LOOKS like it does —

  * a 19:00 shift handover (two machines, one person, two different shifts);
  * a helper walking between two benches in one shift, which this engine's roster
    permits by design (roster.py rosters CNC/VMC only);
  * an operation held overnight in the chuck with nothing else on the machine;
  * an operation that spans an unmanned shift and whose successor starts when the
    shop reopens;
  * a machining step that legitimately SKIPS its 90-minute setup because the same
    (item, process) was already on the machine;
  * a single-shift bench that is dark at night because it has no night shift.

Fixture notes — the brief's sketch could not run as written:
  * ``Config`` has no ``first_shift_start``/``first_shift_end`` time fields. It
    stores hour ints (``first_shift_start_hour`` 8, ``first_shift_end_hour`` 19,
    ``second_shift_end_hour`` 5). ``roster_engine.worktime`` is the one definition
    of the shift clock and the report reads it rather than re-deriving it.
  * ``Routing`` is ``(item_code, description, customer, rm_type, moq, processes)``
    — the brief's ``Routing("ITEM", "d", [Process(...)])`` puts the process list
    in the ``customer`` slot and leaves the routing empty.
  * an entry's ``occupancy_min`` is its MACHINE time (setup + cutting), not its
    wall-clock span — the brief's helper derived it from ``end - start``, which
    made the implied setup 500 minutes on its own fractional-piece fixture and the
    check silently unreachable.
"""

from datetime import date, datetime, timedelta
from datetime import time as _time

import pytest

from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           ScheduleEntry, WorkCalendar)
from roster_engine import report, worktime

PLAN_START = date(2026, 8, 12)          # a Wednesday; the 13th is the weekly off


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _cfg(scheduler="roster"):
    return Config(plan_start_date=PLAN_START, scheduler=scheduler)


def _dt(h, m=0, day=12):
    return datetime(2026, 8, day, h, m)


def _entry(machine, seq, start, end, operator="", batch="B1", item="ITEM",
           qty=10, occupancy=None, segments=None):
    """One ScheduleEntry, with the real field semantics.

    ``occupancy_min`` defaults to the segments' own total (machine time), NOT the
    wall-clock span: an operation held over a dark shift spans more time than it
    occupies, and the rounding check derives the implied setup from this number.
    """
    segs = list(segments) if segments is not None else [(start, end, operator)]
    if occupancy is None:
        occupancy = sum((e - s).total_seconds() / 60.0 for s, e, _w in segs)
    return ScheduleEntry(
        batch_id=batch, item_code=item, process_seq=seq, process_name=f"P{seq}",
        machine=machine, qty=qty, occupancy_min=occupancy, start=start, end=end,
        operator=(segs[0][2] if segs else operator), op_segments=segs)


_MACHINES = {
    "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
    "CNC2": Machine("CNC2", "CNC 2", "CNC lathe", available_hrs_per_day=19.5),
    "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
    "MD1": Machine("MD1", "MD 1", "Deburring", available_hrs_per_day=9.5),
    "MD2": Machine("MD2", "MD 2", "Deburring", available_hrs_per_day=9.5),
}


def _masters(operators=None, routings=None, machines=None):
    return Masters(machines=dict(machines if machines is not None else _MACHINES),
                   routings=dict(routings or {}),
                   operators=list(operators or []),
                   calendar=WorkCalendar())


def _kinds(rows):
    return [r["kind"] for r in rows]


# --------------------------------------------------------------------------- #
# Rule 1 — one operator, one machine, one shift
# --------------------------------------------------------------------------- #

def test_an_operator_on_two_machines_in_one_shift_is_flagged():
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC4", 2, _dt(13), _dt(17), "Narayan", batch="B2")]
    rows = report.operator_split_violations(entries, _cfg(), _masters())
    assert len(rows) == 1
    assert rows[0]["kind"] == "OPERATOR_SPLIT_SHIFT"
    assert "Narayan" in rows[0]["message"]
    assert "CNC1" in rows[0]["message"] and "CNC4" in rows[0]["message"]
    assert "Narayan" in rows[0]["ref"]


def test_the_split_check_works_without_masters_for_task_11():
    """Task 11 calls the two-argument form. A CNC/VMC id is machining either way."""
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC4", 2, _dt(13), _dt(17), "Narayan", batch="B2")]
    assert _kinds(report.operator_split_violations(entries, _cfg())) == [
        "OPERATOR_SPLIT_SHIFT"]


def test_the_same_operator_on_the_same_machine_all_shift_is_clean():
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC1", 2, _dt(12), _dt(17), "Narayan", batch="B2")]
    assert report.operator_split_violations(entries, _cfg(), _masters()) == []


def test_a_shift_change_at_1900_is_not_a_split():
    """The legal case that most looks like the illegal one."""
    entries = [_entry("CNC1", 1, _dt(8), _dt(19), "Narayan"),
               _entry("CNC4", 1, _dt(19), _dt(23), "Narayan", batch="B2")]
    assert report.operator_split_violations(entries, _cfg(), _masters()) == []


def test_the_night_shift_crosses_midnight_so_0200_is_the_previous_days_shift():
    entries = [_entry("CNC1", 1, _dt(22, 0, 14), _dt(23, 0, 14), "Sidhu"),
               _entry("CNC4", 1, _dt(2, 0, 15), _dt(4, 0, 15), "Sidhu",
                      batch="B2")]
    rows = report.operator_split_violations(entries, _cfg(), _masters())
    assert _kinds(rows) == ["OPERATOR_SPLIT_SHIFT"]
    assert "14-08-2026" in rows[0]["message"]      # the night the shift STARTED


def test_0200_and_the_following_evening_are_two_different_night_shifts():
    entries = [_entry("CNC4", 1, _dt(2, 0, 15), _dt(4, 0, 15), "Sidhu"),
               _entry("CNC1", 1, _dt(20, 0, 15), _dt(23, 0, 15), "Sidhu",
                      batch="B2")]
    assert report.operator_split_violations(entries, _cfg(), _masters()) == []


def test_a_helper_walking_between_two_benches_in_one_shift_is_legal():
    """roster.py rosters CNC/VMC only: a helper physically walks between
    deburring and packing, and forbidding it deletes capacity the shop has."""
    entries = [_entry("MD1", 1, _dt(9), _dt(11), "Helper"),
               _entry("MD2", 1, _dt(11), _dt(13), "Helper", batch="B2")]
    assert report.operator_split_violations(entries, _cfg(), _masters()) == []


def test_a_helper_on_two_benches_at_the_same_instant_is_flagged():
    entries = [_entry("MD1", 1, _dt(9), _dt(11), "Helper"),
               _entry("MD2", 1, _dt(10), _dt(12), "Helper", batch="B2")]
    rows = report.operator_split_violations(entries, _cfg(), _masters())
    assert _kinds(rows) == ["OPERATOR_SPLIT_SHIFT"]
    assert "same time" in rows[0]["message"]


def test_a_bench_and_a_cnc_in_one_shift_is_a_split():
    """Whoever mans a CNC mans it for the whole shift — they are not also at a
    bench that shift (the roster excludes rostered people from the bench pool)."""
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("MD1", 1, _dt(13), _dt(15), "Narayan", batch="B2")]
    assert _kinds(report.operator_split_violations(entries, _cfg(), _masters())) \
        == ["OPERATOR_SPLIT_SHIFT"]


def test_an_unnamed_operator_is_never_a_split():
    """OS / off-machine milestones carry no person, and neither does an
    operator-less segment. They must not collide into one phantom worker."""
    entries = [_entry("OS / Outsourced", 1, _dt(8), _dt(12), "", segments=[]),
               _entry("CNC1", 2, _dt(8), _dt(12), ""),
               _entry("CNC4", 3, _dt(8), _dt(12), "", batch="B2")]
    assert report.operator_split_violations(entries, _cfg(), _masters()) == []


# --------------------------------------------------------------------------- #
# Rule 2 — an operation runs to completion
# --------------------------------------------------------------------------- #

def test_an_interrupted_operation_is_flagged():
    interrupted = _entry("CNC1", 1, _dt(8), _dt(18), "N", occupancy=240.0,
                         segments=[(_dt(8), _dt(10), "N"),
                                   (_dt(16), _dt(18), "N")])
    other = _entry("CNC1", 1, _dt(11), _dt(15), "N", batch="B2")
    rows = report.segmentation_violations([interrupted, other])
    assert len(rows) == 1
    assert rows[0]["kind"] == "OPERATION_SEGMENTED"
    assert "B1" in rows[0]["ref"]
    assert "B2" in rows[0]["message"]


def test_a_shift_gap_inside_one_operation_is_not_segmentation():
    """Nothing else ran on the machine — the part just waited in the chuck."""
    held = _entry("CNC1", 1, _dt(8), _dt(12, 0, 14), "N",
                  segments=[(_dt(8), _dt(19), "N"),
                            (_dt(8, 0, 14), _dt(12, 0, 14), "N")])
    assert report.segmentation_violations([held]) == []


def test_another_job_on_a_different_machine_in_the_gap_is_not_segmentation():
    held = _entry("CNC1", 1, _dt(8), _dt(12, 0, 14), "N",
                  segments=[(_dt(8), _dt(19), "N"),
                            (_dt(8, 0, 14), _dt(12, 0, 14), "N")])
    elsewhere = _entry("CNC4", 1, _dt(20), _dt(23), "S", batch="B2")
    assert report.segmentation_violations([held, elsewhere]) == []


def test_another_job_outside_the_gap_is_not_segmentation():
    held = _entry("CNC1", 1, _dt(8), _dt(12, 0, 14), "N",
                  segments=[(_dt(8), _dt(19), "N"),
                            (_dt(8, 0, 14), _dt(12, 0, 14), "N")])
    after = _entry("CNC1", 1, _dt(13, 0, 14), _dt(15, 0, 14), "N", batch="B2")
    assert report.segmentation_violations([held, after]) == []


def test_a_job_that_fully_spans_the_gap_is_still_segmentation():
    """The brief only tested the other job's START or END inside the gap; a job
    that swallows the gap whole interrupts the operation just as much."""
    interrupted = _entry("CNC1", 1, _dt(8), _dt(18), "N", occupancy=240.0,
                         segments=[(_dt(8), _dt(10), "N"),
                                   (_dt(16), _dt(18), "N")])
    other = _entry("CNC1", 1, _dt(9), _dt(17), "N", batch="B2")
    assert _kinds(report.segmentation_violations([interrupted, other])) == [
        "OPERATION_SEGMENTED"]


def test_a_second_entry_of_the_same_step_on_one_machine_is_not_an_intruder():
    """The classic engine publishes a parallel-split step as SEVERAL entries of
    the same (batch, seq). The other half of an operation is that operation, not
    another job taking the machine away — without the skip the split reports
    itself as its own interruption."""
    half = _entry("CNC1", 1, _dt(8), _dt(18), "N", occupancy=240.0,
                  segments=[(_dt(8), _dt(10), "N"), (_dt(16), _dt(18), "N")])
    other_half = _entry("CNC1", 1, _dt(11), _dt(15), "N")
    assert half.batch_id == other_half.batch_id
    assert half.process_seq == other_half.process_seq
    assert report.segmentation_violations([half, other_half]) == []


# --------------------------------------------------------------------------- #
# The owner's original complaint, as a number
# --------------------------------------------------------------------------- #

def _idle_masters(sidhu=("CNC2",)):
    return _masters(
        operators=[Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
                   Operator("Sidhu", "/".join(sidhu), list(sidhu),
                            "Second shift")],
        machines={"CNC1": _MACHINES["CNC1"], "CNC2": _MACHINES["CNC2"]})


def _idle_entries():
    # CNC2's work is a one-step job: ready from the plan's first moment, yet it
    # does not run until the night of the 14th. CNC2 is dark on the night of the
    # 12th and Sidhu — qualified, on that shift — is on no machine at all.
    return [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
            _entry("CNC2", 1, _dt(19, 0, 14), _dt(21, 0, 14), "Sidhu",
                   batch="B2")]


def test_a_dark_machine_with_ready_work_and_a_free_operator_is_flagged():
    rows = report.idle_capacity_violations(_idle_entries(), _idle_masters(),
                                           _cfg())
    assert _kinds(rows) == ["IDLE_CAPACITY"]
    assert "CNC2" in rows[0]["ref"] and "12-08-2026" in rows[0]["ref"]
    assert "Sidhu" in rows[0]["message"]


def test_a_dark_machine_is_clean_when_the_qualified_operator_is_working():
    masters = _idle_masters(sidhu=("CNC1", "CNC2"))
    entries = _idle_entries()
    entries.append(_entry("CNC1", 1, _dt(20), _dt(22), "Sidhu", batch="B3"))
    assert report.idle_capacity_violations(entries, masters, _cfg()) == []


def test_a_dark_machine_is_clean_when_its_work_was_not_yet_ready():
    # B2's CNC2 step is fed by a CNC1 step that does not finish until midday on
    # the 14th, so it could not have run on the night of the 12th however free
    # the crew was. Sidhu is not qualified for CNC1, so the feeding step's own
    # wait is not a finding either.
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC1", 1, _dt(8, 0, 14), _dt(12, 0, 14), "Narayan",
                      batch="B2"),
               _entry("CNC2", 2, _dt(19, 0, 14), _dt(21, 0, 14), "Sidhu",
                      batch="B2")]
    assert report.idle_capacity_violations(entries, _idle_masters(), _cfg()) == []


def test_a_single_shift_bench_is_not_idle_capacity_at_night():
    """MD1 has no second shift, so a night-shift helper qualified for it is not
    unused capacity — the bench is simply shut."""
    masters = _masters(
        operators=[Operator("Helper", "MD1", ["MD1"], "First shift"),
                   Operator("Nightowl", "MD1", ["MD1"], "Second shift")],
        machines={"MD1": _MACHINES["MD1"]})
    entries = [_entry("MD1", 1, _dt(9), _dt(12), "Helper"),
               _entry("MD1", 1, _dt(9, 0, 14), _dt(12, 0, 14), "Helper",
                      batch="B2")]
    assert report.idle_capacity_violations(entries, masters, _cfg()) == []


def test_an_absent_operator_is_not_idle_capacity():
    absent = {"Sidhu": [(datetime(2026, 8, 12), datetime(2026, 8, 14))]}
    assert report.idle_capacity_violations(
        _idle_entries(), _idle_masters(), _cfg(), absent=absent) == []


def test_the_weekly_off_is_never_reported_as_idle_capacity():
    rows = report.idle_capacity_violations(_idle_entries(), _idle_masters(),
                                           _cfg())
    assert all("13-08-2026" not in r["ref"] for r in rows)


# --------------------------------------------------------------------------- #
# Whole pieces
# --------------------------------------------------------------------------- #

_ROUTING = {"ITEM": Routing("ITEM", "desc", "cust", "rm", None, [
    Process(1, "P1", 25.0, None, None, "CNC1"),
    Process(2, "P2", 5.0, None, None, "CNC4")])}

# qty 8 x 25 min = 200 min of cutting, + a 90-minute setup = 290 minutes of
# machine time, run straight through from 08:00.
_PREV = _entry("CNC1", 1, _dt(8), _dt(12, 50), "N", qty=8, occupancy=290.0)


def test_a_fractional_piece_release_is_flagged():
    # 08:00 + 90 setup + 137.5 min = 4.7 pieces. There is no 0.7 of a piece.
    nxt = _entry("CNC4", 2, _dt(11, 27) + timedelta(seconds=30), _dt(18), "S",
                 qty=8)
    rows = report.overlap_rounding_violations([_PREV, nxt],
                                              _masters(routings=_ROUTING))
    assert _kinds(rows) == ["OVERLAP_FRACTIONAL_PIECE"]
    assert "B1" in rows[0]["ref"]
    # The two branches say DIFFERENT things and must stay distinguishable: this
    # one is a mid-release fraction, not a release before any piece exists.
    assert "4.70 pieces into" in rows[0]["message"]
    assert "whole piece of" not in rows[0]["message"]


def test_a_whole_piece_release_is_clean():
    nxt = _entry("CNC4", 2, _dt(11, 10), _dt(18), "S", qty=8)   # 90 + 4 x 25
    assert report.overlap_rounding_violations(
        [_PREV, nxt], _masters(None, _ROUTING)) == []


def test_a_legitimately_skipped_setup_is_not_a_fractional_release():
    """The same (item, process) was already on this machine, so no setup was
    charged. Assuming the nominal 90 minutes turns a clean 4-piece release into
    0.4 of a piece — a false positive on exactly the case the skip exists for."""
    prev = _entry("CNC1", 1, _dt(8), _dt(11, 20), "N", qty=8, occupancy=200.0)
    nxt = _entry("CNC4", 2, _dt(9, 40), _dt(18), "S", qty=8)    # 4 x 25, no setup
    assert report.overlap_rounding_violations(
        [prev, nxt], _masters(None, _ROUTING)) == []


def test_a_successor_delayed_by_a_busy_machine_is_not_a_fractional_release():
    """It was released on a whole piece and then waited for its own machine.
    Reporting the fraction it happens to see at its start blames the release for
    a delay that was contention."""
    nxt = _entry("CNC4", 2, _dt(11, 30), _dt(18), "S", qty=8)
    busy = _entry("CNC4", 1, _dt(8), _dt(11, 30), "S", batch="B9", item="OTHER")
    assert report.overlap_rounding_violations(
        [_PREV, nxt, busy], _masters(None, _ROUTING)) == []


def test_a_successor_that_starts_when_the_shop_reopens_is_clean():
    """The operation was held over an unmanned shift and the weekly off; its
    successor starts at 08:00 when the first shift opens, not on a fraction."""
    prev = _entry("CNC1", 1, _dt(16), _dt(9, 50, 14), "N", qty=8,
                  occupancy=290.0,
                  segments=[(_dt(16), _dt(19), "N"),
                            (_dt(8, 0, 14), _dt(9, 50, 14), "N")])
    nxt = _entry("CNC4", 2, _dt(8, 0, 14), _dt(18, 0, 14), "S", qty=8)
    assert report.overlap_rounding_violations(
        [prev, nxt], _masters(None, _ROUTING)) == []


def test_a_successor_that_starts_while_the_machine_is_dark_is_clean():
    prev = _entry("CNC1", 1, _dt(16), _dt(9, 50, 14), "N", qty=8,
                  occupancy=290.0,
                  segments=[(_dt(16), _dt(19), "N"),
                            (_dt(8, 0, 14), _dt(9, 50, 14), "N")])
    nxt = _entry("CNC4", 2, _dt(19, 30), _dt(23), "S", qty=8)
    assert report.overlap_rounding_violations(
        [prev, nxt], _masters(None, _ROUTING)) == []


def test_a_sequential_successor_has_nothing_to_round():
    nxt = _entry("CNC4", 2, _dt(13), _dt(18), "S", qty=8)
    assert report.overlap_rounding_violations(
        [_PREV, nxt], _masters(None, _ROUTING)) == []


def test_a_successor_released_before_a_single_piece_exists_is_flagged():
    nxt = _entry("CNC4", 2, _dt(9, 40), _dt(18), "S", qty=8)   # 10 min of cutting
    rows = report.overlap_rounding_violations([_PREV, nxt],
                                              _masters(None, _ROUTING))
    assert _kinds(rows) == ["OVERLAP_FRACTIONAL_PIECE"]
    # This is the UNCONDITIONAL branch — the only one that can fire on a
    # contended plan, since the other is muted whenever anything explains the
    # start. Asserting the kind alone lets the branch be deleted silently: the
    # pair falls through to the muted branch and emits the other phrasing.
    assert "0.40 pieces before a single whole piece of" in rows[0]["message"]


def test_an_item_with_no_routing_is_skipped_not_crashed():
    nxt = _entry("CNC4", 2, _dt(11, 27), _dt(18), "S", qty=8)
    assert report.overlap_rounding_violations([_PREV, nxt], _masters()) == []


# --------------------------------------------------------------------------- #
# How much of this check is muted, as a number
# --------------------------------------------------------------------------- #

def test_the_scan_counts_a_detection_that_is_muted():
    """Measured on real plans, EVERY fractional detection was muted by this one
    clause. A bare 0 rows must never be read as "the engine releases on whole
    pieces"; the counts are what makes the difference visible."""
    nxt = _entry("CNC4", 2, _dt(11, 30), _dt(18), "S", qty=8)
    busy = _entry("CNC4", 1, _dt(8), _dt(11, 30), "S", batch="B9", item="OTHER")
    scan = report.overlap_rounding_scan([_PREV, nxt, busy],
                                        _masters(None, _ROUTING))
    assert (scan["detected"], scan["muted"], scan["reported"]) == (1, 1, 0)
    assert scan["rows"] == []
    assert len(scan["muted_rows"]) == 1
    assert scan["muted_rows"][0]["kind"] == "OVERLAP_FRACTIONAL_PIECE"
    assert scan["muted_rows"][0]["explained"] == report.EXPLAIN_MACHINE_BUSY
    assert "4.80 pieces into" in scan["muted_rows"][0]["message"]


def test_the_scan_counts_a_detection_that_is_reported():
    nxt = _entry("CNC4", 2, _dt(11, 27) + timedelta(seconds=30), _dt(18), "S",
                 qty=8)
    masters = _masters(None, _ROUTING)
    scan = report.overlap_rounding_scan([_PREV, nxt], masters)
    assert (scan["detected"], scan["muted"], scan["reported"]) == (1, 0, 1)
    assert scan["muted_rows"] == []
    assert scan["rows"] == report.overlap_rounding_violations([_PREV, nxt],
                                                              masters)


def test_the_scan_counts_nothing_on_a_whole_piece_release():
    nxt = _entry("CNC4", 2, _dt(11, 10), _dt(18), "S", qty=8)   # 90 + 4 x 25
    scan = report.overlap_rounding_scan([_PREV, nxt], _masters(None, _ROUTING))
    assert (scan["detected"], scan["muted"], scan["reported"]) == (0, 0, 0)


def test_the_dark_hours_of_an_overnight_hold_are_not_counted_as_cutting():
    """Worked minutes, not elapsed. The predecessor is held from 19:00 on the
    12th to 08:00 on the 14th; by the wall clock its successor starts 41 hours
    in, by the machine's own work it starts on exactly 6 whole pieces. And this
    fixture is NOT muted — it reaches the arithmetic, which the two overnight
    tests above do not."""
    prev = _entry("CNC1", 1, _dt(16), _dt(9, 50, 14), "N", qty=8,
                  occupancy=290.0,
                  segments=[(_dt(16), _dt(19), "N"),
                            (_dt(8, 0, 14), _dt(9, 50, 14), "N")])
    nxt = _entry("CNC4", 2, _dt(9, 0, 14), _dt(18, 0, 14), "S", qty=8)
    scan = report.overlap_rounding_scan([prev, nxt], _masters(None, _ROUTING))
    assert (scan["detected"], scan["muted"], scan["reported"]) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# The whole set
# --------------------------------------------------------------------------- #

def _clean_plan():
    masters = _masters(
        operators=[Operator("Narayan", "CNC1", ["CNC1"], "First shift")],
        machines={"CNC1": _MACHINES["CNC1"]}, routings=_ROUTING)
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan")]
    return entries, masters


def test_all_violations_is_empty_on_a_clean_roster_plan():
    entries, masters = _clean_plan()
    assert report.all_violations(entries, masters, _cfg()) == []


def test_all_violations_is_empty_on_an_empty_plan():
    assert report.all_violations([], _masters(), _cfg()) == []


def test_all_violations_collects_every_kind_it_finds():
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC4", 1, _dt(13), _dt(17), "Narayan", batch="B2")]
    rows = report.all_violations(entries, _masters(), _cfg())
    assert "OPERATOR_SPLIT_SHIFT" in _kinds(rows)
    assert all(set(r) >= {"kind", "ref", "message"} for r in rows)


def test_identical_inputs_give_identical_row_order():
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC4", 1, _dt(13), _dt(17), "Narayan", batch="B2"),
               _entry("MD1", 1, _dt(9), _dt(11), "Helper"),
               _entry("MD2", 1, _dt(10), _dt(12), "Helper", batch="B3")]
    masters = _masters(operators=[
        Operator("Narayan", "CNC1", ["CNC1", "CNC4"], "First shift"),
        Operator("Helper", "MD1", ["MD1", "MD2"], "First shift")])
    first = report.all_violations(entries, masters, _cfg())
    second = report.all_violations(list(reversed(entries)), masters, _cfg())
    assert len(first) >= 2
    assert first == second


def test_the_report_follows_worktimes_shift_clock_rather_than_a_copy():
    """The shift clock is READ from ``roster_engine.worktime`` — the one
    definition the scheduler plans by — never re-derived here. A local copy that
    agrees today is precisely the 2026-08-07 incident (four reporting features
    each rebuilt the working window and hid 9,470 minutes of planned work), so
    move worktime's clock and the report must move with it.
    """
    entries, masters = _split_plan()        # 08:00-12:00 CNC1, 13:00-17:00 CNC4
    assert _kinds(report.operator_split_violations(entries, _cfg(), masters)) \
        == ["OPERATOR_SPLIT_SHIFT"]         # one shift on the real 08:00-19:00

    def _moved_clock(day, shift, config):
        """First 08:00-13:00, second 13:00-22:00 — the two bookings above now
        fall in DIFFERENT shifts, so the same plan is a legal handover."""
        if shift == report.FIRST:
            return (datetime.combine(day, _time(8)),
                    datetime.combine(day, _time(13)))
        return (datetime.combine(day, _time(13)),
                datetime.combine(day, _time(22)))

    original = worktime._shift_bounds
    worktime._shift_bounds = _moved_clock
    try:
        assert report.operator_split_violations(entries, _cfg(), masters) == []
        assert report.shift_key(_dt(9), _cfg()) == (PLAN_START, report.FIRST)
        assert report.shift_key(_dt(14), _cfg()) == (PLAN_START, report.SECOND)
    finally:
        worktime._shift_bounds = original

    assert _kinds(report.operator_split_violations(entries, _cfg(), masters)) \
        == ["OPERATOR_SPLIT_SHIFT"]


def test_the_lane_strings_match_the_adapters():
    """The off-machine lanes are matched literally. Duplicated in report.py so
    roster_engine keeps its zero app imports; pinned equal here."""
    from engine import roster_adapter
    assert report.OFF_LANES == {roster_adapter.OS_LANE, roster_adapter.OFF_LANE}


# --------------------------------------------------------------------------- #
# The API hook is gated to the roster engine
# --------------------------------------------------------------------------- #

def _split_plan():
    masters = _masters(
        operators=[Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"],
                            "First shift")],
        machines={"CNC1": _MACHINES["CNC1"], "CNC4": _MACHINES["CNC4"]})
    entries = [_entry("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _entry("CNC4", 1, _dt(13), _dt(17), "Narayan", batch="B2")]
    return entries, masters


def _report_kinds(scheduler):
    from api import main as api_main
    entries, masters = _split_plan()
    table = api_main._report_for_book(masters, [], absences=[],
                                      config=_cfg(scheduler), schedule=entries)
    if "Kind" not in table["columns"]:
        return set()
    kind = table["columns"].index("Kind")
    return {row[kind] for row in table["rows"]}


_ROSTER_KINDS = {"OPERATOR_SPLIT_SHIFT", "OPERATION_SEGMENTED", "IDLE_CAPACITY",
                 "OVERLAP_FRACTIONAL_PIECE", "MACHINE_DOUBLE_BOOKED"}


def test_a_roster_plan_report_is_wired_to_the_checks():
    assert "OPERATOR_SPLIT_SHIFT" in _report_kinds("roster")


@pytest.mark.parametrize("scheduler", ["new", "classic", "flow"])
def test_no_other_engines_report_gains_these_rows(scheduler):
    """The live engine really does violate two of these on every plan. Those rows
    belong in the Task 11 harness, not in the directors' data-gaps banner on a
    site that has not switched engines (the plan's Global Constraint: nothing
    changes until DEFAULT_SCHEDULER=roster)."""
    assert _report_kinds(scheduler) & _ROSTER_KINDS == set()
