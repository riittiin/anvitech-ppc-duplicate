"""The replay decoder: genome + today's book -> laid-out times.

The tests below are written against the CONTRACT, not the implementation: the
decoder decides nothing, so every test that pins a decision (machine, operator,
order, release) works by making the decoder's own preference DIFFERENT from the
genome's answer and asserting the genome wins.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from cp_engine import decode, domain
from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)

PLAN_START = datetime(2026, 8, 12, 8, 0)      # a Wednesday; 13-08 is the weekly off
REPO_ROOT = Path(__file__).resolve().parents[1]


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1), remaining=None):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = remaining


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0, **kw)


_DEFAULT_MACHINES = {
    "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
    "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
    "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
}


def _masters(routings, operators, machines=None):
    return Masters(
        machines=dict(machines or _DEFAULT_MACHINES),
        routings=routings, operators=list(operators), calendar=WorkCalendar())


def _lay(masters, batches, g, frozen=None, config=None):
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, {})
    return decode.lay_out(jobs, shop, config or _cfg(), PLAN_START, g,
                          frozen=frozen)


def _routing():
    return {"A": Routing("A", "a", "cust", "rm", None, [
        Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1/CNC4"),
        Process(2, "DEBURING", 1.0, None, None, "MD1")])}


def _op(plan, seq):
    return [p for p in plan.placements if p.op_seq == seq][0]


# --------------------------------------------------------------------------- #
# The decoder decides nothing
# --------------------------------------------------------------------------- #

def test_the_decoder_uses_the_genome_machine_not_its_own_preference():
    """The decoder DECIDES NOTHING. If it re-picked a machine, the plan on
    screen would not be the plan that was solved (spec §8)."""
    masters = _masters(_routing(),
                       [Operator("N", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                        Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC4", ("B1", 2): "MD1"},
         "cp_roster": {("CNC4", 0): "N"}, "cp_overlap_of": {"B1": 10},
         "ranks": {}, "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    machines = {p.op_seq: p.machine for p in plan.placements}
    assert machines[1] == "CNC4"


def test_the_operator_on_a_machining_op_comes_from_the_roster():
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("S", "CNC1", ["CNC1"], "First shift"),
                                    Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", 0): "S"}, "cp_overlap_of": {"B1": 10},
         "ranks": {}, "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    first = _op(plan, 1)
    assert [who for _s, _e, who in first.segments] == ["S"]


def test_the_genome_rank_decides_which_job_takes_the_machine_first():
    """Two batches, one machine, both ready at the plan start. The order is the
    solver's, not the decoder's — reverse the ranks and the plan reverses."""
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift")])
    base = {"cp_machine_of": {("B1", 1): "CNC1", ("B2", 1): "CNC1"},
            "cp_roster": {("CNC1", i): "N" for i in range(20)},
            "cp_overlap_of": {"B1": 10, "B2": 10}, "cp_completion": {},
            "cp_solved_book_sig": ""}
    batches = [_B("B1", "A", 10), _B("B2", "A", 10)]

    forward = _lay(masters, batches, dict(base, ranks={"SO-B1\x1fA": 1,
                                                       "SO-B2\x1fA": 2}))
    reverse = _lay(masters, batches, dict(base, ranks={"SO-B1\x1fA": 2,
                                                       "SO-B2\x1fA": 1}))
    first_of = lambda plan: min(plan.placements, key=lambda p: p.start).job_key
    assert first_of(forward) == "B1"
    assert first_of(reverse) == "B2"


def test_an_operation_spanning_two_shifts_is_segmented_by_the_roster():
    """Rule 2 keeps the operation whole; op_segments names WHO ran each part.
    Five surfaces read that list, and the shift-wise export is a live floor
    document people plan their day around."""
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 2.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("S", "CNC1", ["CNC1"], "2nd shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1"},
         "cp_roster": {("CNC1", 0): "N", ("CNC1", 1): "S"},
         "cp_overlap_of": {"B1": 400}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 400)], g)
    first = _op(plan, 1)
    assert len(first.segments) >= 2
    assert [who for _s, _e, who in first.segments][:2] == ["N", "S"]
    assert first.segments[0][1] == first.segments[1][0]      # contiguous, not split


# --------------------------------------------------------------------------- #
# The release — the exact half of the pair (spec §5.3)
# --------------------------------------------------------------------------- #

def test_the_release_is_computed_in_worked_minutes_not_wall_clock():
    """The decoder is the EXACT half of the pair (spec §5.3). An overnight gap
    must not release pieces that were never cut."""
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 100}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 100)], g)
    first, second = _op(plan, 1), _op(plan, 2)
    worked = sum((e - s).total_seconds() / 60.0
                 for s, e, _who in first.segments if s < second.start)
    assert worked >= 90 + 100 * 5 - 1e-6      # setup + all 100 pieces
    # The MOMENT, not just the accumulated segment: the setup is part of the
    # worked minutes the successor waits for, exactly as ``rules._setup_charged``
    # puts it into the model's own release bound.
    assert second.start == PLAN_START + timedelta(minutes=90 + 100 * 5)


def test_a_release_that_straddles_the_weekly_off_waits_for_the_worked_minutes():
    """The discriminating case the test above cannot see: the predecessor's work
    does not fit in one shift, so wall-clock and worked minutes give different
    answers 12 hours apart.

    MD1 is a single-shift bench: 800 minutes of deburring runs 08:00-19:00 on
    Wed 12-08 (660 min) and resumes Fri 14-08 (13-08 is the weekly off) at 08:00,
    finishing the 800th minute at 10:20. Wall clock would put it at 21:20 on the
    Wednesday — inside a dark shop, on pieces nobody cut.
    """
    masters = _masters(
        {"C": Routing("C", "c", "cust", "rm", None, [
            Process(1, "DEBURING", 1.0, None, None, "MD1"),
            Process(2, "CNC FIRST SIDE", 0.1, None, None, "CNC1")])},
        [Operator("M", "MD1", ["MD1"], "First shift"),
         Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("S", "CNC1", ["CNC1"], "2nd shift")])
    g = {"cp_machine_of": {("B2", 1): "MD1", ("B2", 2): "CNC1"},
         "cp_roster": {("CNC1", i): ("N" if i % 2 == 0 else "S")
                       for i in range(20)},
         "cp_overlap_of": {"B2": 800}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B2", "C", 800)], g)
    first, second = _op(plan, 1), _op(plan, 2)

    assert first.machine == "MD1" and second.machine == "CNC1"
    # The wall-clock answer, which must NOT be the one the decoder gives.
    assert PLAN_START + timedelta(minutes=800) == datetime(2026, 8, 12, 21, 20)
    assert second.start >= datetime(2026, 8, 14, 10, 20)
    worked = sum((e - s).total_seconds() / 60.0
                 for s, e, _who in first.segments if s < second.start)
    assert worked >= 800 - 1e-6


def test_a_repeat_of_the_same_part_pays_no_setup_and_releases_that_much_earlier():
    """Rule 4's credit, and the release that follows from it. The model grants a
    setup-free mode to a back-to-back same-(item, seq) pair; if the decoder
    charged 90 minutes anyway the two would disagree by exactly that."""
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B2", 1): "CNC1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10, "B2": 10},
         "ranks": {"SO-B1\x1fA": 1, "SO-B2\x1fA": 2},
         "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10), _B("B2", "A", 10)], g)
    by_job = {p.job_key: p for p in plan.placements}
    assert by_job["B1"].work_min == 90 + 10 * 5.0
    assert by_job["B2"].work_min == 10 * 5.0          # warm fixture, no setup


def test_setup_follows_the_machine_not_the_step_kind():
    """A step whose FIRST option is a bench is manual-KIND, but on a CNC it still
    changes a fixture. The model charges setup per MODE (``mid in
    shop.machining_ids``); reading ``op.kind`` here would hand the plan 90
    minutes of CNC capacity that does not exist — the trap that produced two
    defect rounds in Task 5."""
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "DEBURING", 5.0, None, None, "MD1/CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    first = _op(plan, 1)
    assert first.machine == "CNC1"
    assert first.work_min == 90 + 10 * 5.0


def test_a_fast_step_never_finishes_before_the_step_that_feeds_it():
    """Overlap may START a step early; it may never let it END first — it cannot
    finish pieces its predecessor has not delivered (RULES.md:132). 590 minutes
    of turning feed 10 minutes of deburring, released after one piece.

    The paced tail is real OCCUPANCY, not a cosmetic end date: ``B2`` is waiting
    at the same bench and may not be dropped into it. Publishing the paced end
    while freeing the machine underneath it would double-book MD1.
    """
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
            Process(2, "DEBURING", 0.1, None, None, "MD1")]),
         "E": Routing("E", "e", "cust", "rm", None, [
             Process(1, "HEAT TREATMENT OS", 100.0, None, None, "OS"),
             Process(2, "DEBURING", 0.1, None, None, "MD1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1",
                           ("B2", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 1, "B2": 1},
         "ranks": {"SO-B1\x1fA": 1, "SO-B2\x1fE": 2},
         "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 100), _B("B2", "E", 100)], g)
    first, second = _op(plan, 1), _op(plan, 2)
    assert second.job_key == "B1" and first.job_key == "B1"
    assert second.start < first.end                       # overlap really fired
    assert second.end >= first.end                        # but it is paced out
    assert plan.completion["B1"] >= first.end

    other = [p for p in plan.placements
             if p.job_key == "B2" and p.machine == "MD1"][0]
    assert other.start >= second.end                      # the bench was HELD


# --------------------------------------------------------------------------- #
# Off-machine lanes — visible, never silently skipped
# --------------------------------------------------------------------------- #

def test_an_outsourced_block_and_the_dispatch_milestone_are_both_placed():
    """Outsourcing is shown, never ignored (2026-08-09: 0 of 1,648 delay-report
    rows ever named an OS step). OS is fully sequential both sides, and DISPATCH
    waits for the whole batch, not for its immediate predecessor."""
    masters = _masters(
        {"D": Routing("D", "d", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
            Process(2, "HEAT TREATMENT OS", 100.0, None, None, "OS"),
            Process(3, "DEBURING", 1.0, None, None, "MD1"),
            Process(4, "DISPATCH", 0.0, None, None, "")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 3): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         # k = 1, so if OS were allowed to overlap it would start after ONE
         # piece — 45 minutes before the turning finishes. It may not.
         "cp_overlap_of": {"B1": 1}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "D", 10)], g)

    assert {p.op_seq for p in plan.placements} == {1, 2, 3, 4}
    turning, outsourced, dispatch = _op(plan, 1), _op(plan, 2), _op(plan, 4)
    assert outsourced.machine is None
    assert outsourced.start == turning.end                # nothing overlaps INTO OS
    assert (outsourced.end - outsourced.start) == timedelta(minutes=100)
    assert dispatch.start == dispatch.end                 # a zero-duration milestone
    assert dispatch.end == max(p.end for p in plan.placements)


# --------------------------------------------------------------------------- #
# Nothing is ever dropped
# --------------------------------------------------------------------------- #

def test_an_op_the_genome_never_saw_is_reported_not_silently_dropped():
    """An order uploaded since the solve. It must be laid out and FLAGGED, never
    dropped — a piece of work in no plan at all is the 2026-08-11 defect."""
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {}, "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    assert "B1" in plan.unassigned
    assert {p.op_seq for p in plan.placements} == {1, 2}


def test_a_genome_machine_the_routing_no_longer_lists_falls_back_and_is_flagged():
    """The masters moved under the genome. The op still runs, on a machine its
    routing really lists, and the job is flagged so the staleness banner fires."""
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "MD1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    assert "B1" in plan.unassigned
    assert _op(plan, 1).machine == "CNC1"     # options[0], not the stale MD1


# --------------------------------------------------------------------------- #
# Frozen work
# --------------------------------------------------------------------------- #

def test_a_frozen_op_keeps_its_machine_and_pays_no_setup_on_resume():
    """The three numbers are DELIBERATELY different. 88 is the SO line's own
    remainder, 369 is what the clubbed BATCH still owes, 535 is the order. A
    frozen row pins WHERE and WHEN, never HOW MUCH — reading 88 off the row is
    the 2026-08-11 director escalation, in which 281 pieces of a clubbed order
    were in no plan at all. Setting the row and the batch to the SAME number
    (the first version of this test) makes the assertion pass either way."""
    masters = _masters(_routing(),
                       [Operator("N", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                        Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC4", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    pins = [{"job_key": "B1", "op_seq": 1, "machine": "CNC1", "operator": "N",
             "remaining_qty": 88, "prev_start": PLAN_START}]
    plan = _lay(masters, [_B("B1", "A", 535, remaining={1: 369})], g, frozen=pins)
    first = _op(plan, 1)
    assert first.machine == "CNC1"            # the pin beats the genome
    assert first.qty == 369                   # batch remainder, never the line's 88
    assert first.work_min == 369 * 5.0        # no setup on resume


def test_a_frozen_pin_beats_the_solved_start():
    """A pin beats the genome, and that includes ``cp_start_of``. The part is
    physically in the chuck; holding a running machine back to a start the
    solver computed for a book that has since moved would idle it for nothing."""
    masters = _masters(_routing(),
                       [Operator("N", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                        Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": "",
         "cp_start_of": {("B1", 1): "2026-08-17T08:00:00"}}
    pins = [{"job_key": "B1", "op_seq": 1, "machine": "CNC1", "operator": "N",
             "remaining_qty": 10, "prev_start": PLAN_START}]
    plan = _lay(masters, [_B("B1", "A", 10, remaining={1: 10})], g, frozen=pins)
    assert _op(plan, 1).start == PLAN_START


# --------------------------------------------------------------------------- #
# The solved start is a HARD FLOOR — what makes the replay the same plan
# --------------------------------------------------------------------------- #

def test_the_solved_start_is_a_hard_floor():
    """A single global job rank cannot reproduce a solve: the solver's real
    decision is a per-machine sequence with starts it was free NOT to left-shift.
    Measured before this key existed — 12 books solved OPTIMAL, replayed 1-7
    completion-days adrift. The decoder must not pull an op earlier than the
    solve put it, even when the machine is standing idle."""
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": "",
         "cp_start_of": {("B1", 1): "2026-08-12T13:00:00"}}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    assert _op(plan, 1).start == datetime(2026, 8, 12, 13, 0)


def test_the_floor_is_a_lower_bound_never_a_pull_forward():
    """``max(floor, earliest feasible)``, not ``min`` and not an equality. A
    floor in the past cannot make an op run before its predecessor delivered, and
    cannot put anything before the plan start."""
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 100}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": "",
         # Both floors are at or before the plan start. Neither may bind.
         "cp_start_of": {("B1", 1): "2026-08-01T00:00:00",
                         ("B1", 2): "2026-08-01T00:00:00"}}
    plan = _lay(masters, [_B("B1", "A", 100)], g)
    first, second = _op(plan, 1), _op(plan, 2)
    assert first.start == PLAN_START
    assert second.start == PLAN_START + timedelta(minutes=90 + 100 * 5)


def test_the_bench_operator_comes_from_the_genome():
    """``cp_roster`` names nobody at a bench (Rule 1 rosters CNC/VMC only), yet
    ``model._add_modes`` really does book a capacity-1 operator renewable there.
    Throwing that away meant the decoder re-invented it — the measured source of
    the only LATER-than-solved drift. The name-sorted fallback would pick C."""
    masters = _masters(_routing(), [Operator("N", "CNC1", ["CNC1"], "First shift"),
                                    Operator("C", "MD1", ["MD1"], "First shift"),
                                    Operator("D", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
         "cp_roster": {("CNC1", i): "N" for i in range(20)},
         "cp_overlap_of": {"B1": 10}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": "", "cp_bench_of": {("B1", 2): "D"}}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    assert [who for _s, _e, who in _op(plan, 2).segments] == ["D"]


# --------------------------------------------------------------------------- #
# The fallback must reach a machine somebody is actually on
# --------------------------------------------------------------------------- #

def test_an_unsolved_op_prefers_a_machine_the_genome_actually_mans():
    """Measured on a real solve of a CNC1/CNC4 routing: the genome rostered CNC1
    in 2 shifts of 33 and CNC4 in 31. A new order taking ``options[0]`` sat idle
    19 DAYS waiting for CNC1's next rostered shift while CNC4 stood staffed."""
    masters = _masters(_routing(),
                       [Operator("N", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                        Operator("M", "MD1", ["MD1"], "First shift")])
    g = {"cp_machine_of": {}, "cp_roster": {("CNC4", i): "N" for i in range(20)},
         "cp_overlap_of": {}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 10)], g)
    assert "B1" in plan.unassigned
    assert _op(plan, 1).machine == "CNC4"     # NOT options[0], which is CNC1


def test_a_new_order_on_a_barely_rostered_machine_is_still_planned():
    """The other half of the same defect. Delete the genome's later roster entry
    for CNC1 and the new order got ZERO placements and no completion — reported
    in ``dropped``, but absent from the Gantt, which is the 2026-08-11 shape.

    The rule is narrow on purpose: a rostered machine the solve left dark stays
    dark (under E2 the part waits in the chuck, which is the point of that
    encoding) UNLESS it is carrying work the genome never assigned.

    B1 fills the ONE rostered shift to the minute (90 setup + 570 pieces = 660),
    and B9 is a DIFFERENT item so Rule 4 grants it no warm fixture. Both matter:
    the first version of this test let B9 tuck into the tail of shift 0 as a
    same-part repeat and passed with the escape removed.
    """
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")]),
         "Z": Routing("Z", "z", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("S", "CNC1", ["CNC1"], "2nd shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1"},
         "cp_roster": {("CNC1", 0): "N"},          # ONE shift, and nothing after
         "cp_overlap_of": {"B1": 570}, "ranks": {"SO-B1\x1fA": 1},
         "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 570), _B("B9", "Z", 30)], g)
    assert _op(plan, 1).work_min == 90 + 570        # shift 0 is full to the minute
    assert plan.dropped == ()
    assert "B9" in plan.unassigned
    assert [p for p in plan.placements if p.job_key == "B9"]
    assert "B9" in plan.completion


def _first_shifts_only_genome():
    """CNC1 rostered in the FIRST shift of every working day and nowhere else —
    the solver deliberately leaving the night shifts dark. Shift indices run
    0 = 12-08 first, 1 = 12-08 second, 2 = 14-08 first (13-08 is the weekly off),
    so the even ones are the first shifts."""
    return {"cp_machine_of": {("B1", 1): "CNC1"},
            "cp_roster": {("CNC1", i): "N" for i in range(0, 20, 2)},
            "cp_overlap_of": {"B1": 1000}, "ranks": {"SO-B1\x1fA": 1},
            "cp_completion": {}, "cp_solved_book_sig": "",
            "cp_start_of": {("B1", 1): "2026-08-12T08:00:00"}}


def _two_item_cnc_masters(second_shift_machines="CNC1"):
    return _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")]),
         "Z": Routing("Z", "z", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("S", second_shift_machines,
                  second_shift_machines.split("/"), "2nd shift")])


def test_a_new_order_never_moves_a_solved_order_earlier():
    """THE MOVED-BOOK INVARIANT, and the one a solved-book-only drift harness
    structurally cannot see: on a replay of the solved book ``fallback_ops`` is
    empty, so the escape never fires and the hole stays hidden.

    Measured with the escape keyed on the MACHINE for the whole horizon: B1
    completed 14-08 15:10 alone and 13-08 **02:10** once one unrelated 30-piece
    order landed on the same CNC — the solved order pulled 1 day 13 h EARLIER
    through night shifts the solver had left dark on purpose. Early drift on
    solved work is the direction that must not happen, and it also contradicts
    this module's own rule that a freed slot is nobody's to move up into."""
    masters = _two_item_cnc_masters()
    g = _first_shifts_only_genome()
    alone = _lay(masters, [_B("B1", "A", 1000)], g)
    moved = _lay(masters, [_B("B1", "A", 1000), _B("B9", "Z", 30)], g)

    assert alone.completion["B1"] == datetime(2026, 8, 14, 15, 10)
    assert moved.completion["B1"] == alone.completion["B1"]
    # ...and the new order is still planned, never dropped for want of staffing.
    assert moved.dropped == ()
    assert "B9" in moved.completion


def test_the_escape_staffs_the_new_order_and_nothing_else():
    """The narrow half of the same rule. The escape opens a dark shift FOR the op
    the genome never assigned; the solved book may not ride along, so the new
    order runs after the solved work rather than in front of it."""
    masters = _two_item_cnc_masters()
    plan = _lay(masters, [_B("B1", "A", 1000), _B("B9", "Z", 30)],
                _first_shifts_only_genome())
    b1 = [p for p in plan.placements if p.job_key == "B1"][0]
    b9 = [p for p in plan.placements if p.job_key == "B9"][0]
    assert b9.start >= b1.end
    # B1 never touches a night shift: every one of its segments is inside a
    # first-shift window, which is all the genome rostered.
    for start, end, _who in b1.segments:
        assert start.hour >= 8 and end.hour <= 19


def test_a_pool_staffed_cnc_obeys_rule_1():
    """Once a CNC falls to the pool it must be bound to ONE person for the WHOLE
    shift, exactly as the roster binds the genome's own. Staffing it the way a
    bench is staffed — per operation, with only a clash check — planned a
    flexible helper on CNC1 08:00-10:00 and MD1 10:00-10:01 in the same first
    shift: the live 2026-08-07 Sandeep Kumar shape, and precisely what
    ``roster_engine.report.operator_split_violations`` flags. Spec §8 requires
    that check to read 0 against a CP plan, so it is asserted here rather than
    left for the surface that will run it."""
    from engine.config import Config as _Config
    from roster_engine import report as roster_report

    masters = _masters(
        {"Z": Routing("Z", "z", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")]),
         "M": Routing("M", "m", "cust", "rm", None,
                      [Process(1, "DEBURING", 1.0, None, None, "MD1")])},
        [Operator("H", "CNC1/MD1", ["CNC1", "MD1"], "First shift"),
         Operator("S", "CNC1", ["CNC1"], "2nd shift")])
    g = {"cp_machine_of": {}, "cp_roster": {("CNC1", 1): "S"},
         "cp_overlap_of": {}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B9", "Z", 30), _B("B8", "M", 1)], g)

    entries = [_RosterEntry(p) for p in plan.placements if p.machine]
    config = _Config(plan_start_date=date(2026, 8, 12), scheduler="cp")
    assert roster_report.operator_split_violations(entries, config, masters) == []
    assert roster_report.segmentation_violations(entries) == []
    assert roster_report.machine_conflict_violations(entries) == []
    # Non-vacuous: the fixture really does put H on both machines' work.
    assert {p.machine for p in plan.placements} == {"CNC1", "MD1"}
    assert {who for p in plan.placements for _s, _e, who in p.segments} == {"H"}


def test_a_pool_pick_can_never_steal_a_person_the_genome_already_rostered():
    """The pool is picked AFTER the genome's own people are reserved, and only
    from those free for the whole shift. Taking the name-sorted first regardless
    puts A on the CNC the genome rostered him to AND on the escape-staffed one,
    in the same shift — Rule 1, broken by the fix that was meant to keep the new
    order planned."""
    from engine.config import Config as _Config
    from roster_engine import report as roster_report

    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")]),
         "Z": Routing("Z", "z", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC4")])},
        # A sorts first and is qualified for BOTH; Zoe is qualified for CNC4 only.
        [Operator("A", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
         Operator("Zoe", "CNC4", ["CNC4"], "First shift"),
         Operator("Q", "CNC4", ["CNC4"], "2nd shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1"},
         # CNC4 IS rostered — but in the night shift, so shift 0 is dark for it.
         "cp_roster": {("CNC1", 0): "A", ("CNC4", 1): "Q"},
         "cp_overlap_of": {"B1": 400}, "ranks": {"SO-B1\x1fA": 1},
         "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 400), _B("B9", "Z", 30)], g)

    on_cnc4 = [p for p in plan.placements if p.machine == "CNC4"][0]
    assert [who for _s, _e, who in on_cnc4.segments] == ["Zoe"]
    entries = [_RosterEntry(p) for p in plan.placements if p.machine]
    config = _Config(plan_start_date=date(2026, 8, 12), scheduler="cp")
    assert roster_report.operator_split_violations(entries, config, masters) == []
    # Non-vacuous: A really is held on CNC1 for that whole first shift.
    on_cnc1 = [p for p in plan.placements if p.machine == "CNC1"][0]
    assert on_cnc1.segments[0][0] == PLAN_START
    assert [who for _s, _e, who in on_cnc1.segments][0] == "A"


class _RosterEntry:
    """The four fields ``roster_engine.report``'s checks read off a schedule
    entry. Built here rather than through the app seam because that seam is a
    later task; the checks themselves are an INDEPENDENT implementation of the
    four rules, which is exactly what makes running them worth anything."""

    def __init__(self, placement):
        self.machine = placement.machine
        self.op_segments = list(placement.segments)
        self.start = placement.start
        self.end = placement.end
        self.batch_id = placement.job_key
        self.process_seq = placement.op_seq


def test_a_rostered_machine_whose_named_operator_left_the_shift_is_still_manned():
    """An admin edits an operator's shift after the solve. The genome still names
    him, he cannot be there, and without a fallback that machine goes dark for
    the WHOLE replay — the plan wants it manned, and somebody else is qualified."""
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "2nd shift"),      # was First at solve time
         Operator("S", "CNC1", ["CNC1"], "First shift")])
    g = {"cp_machine_of": {("B1", 1): "CNC1"},
         "cp_roster": {("CNC1", 0): "N"},
         "cp_overlap_of": {"B1": 60}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 60)], g)
    first = _op(plan, 1)
    assert first.start == PLAN_START
    assert [who for _s, _e, who in first.segments][0] == "S"


# --------------------------------------------------------------------------- #
# The crew is never in two places at once
# --------------------------------------------------------------------------- #

def test_one_helper_is_never_on_two_benches_at_the_same_instant():
    """Rule 1 rosters CNC/VMC only, so the genome carries no bench assignment at
    all — the decoder has to staff a bench itself. It may not do so by publishing
    the same person at two stations simultaneously: the model booked him as a
    capacity-1 renewable, and the shift-wise sheet is a floor document."""
    machines = {
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
        "MD2": Machine("MD2", "MD 2", "manual", available_hrs_per_day=9.5),
    }
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "DEBURING", 1.0, None, None, "MD1/MD2")])},
        [Operator("M", "MD1/MD2", ["MD1", "MD2"], "First shift")],
        machines=machines)
    g = {"cp_machine_of": {("B1", 1): "MD1", ("B2", 1): "MD2"},
         "cp_roster": {}, "cp_overlap_of": {"B1": 60, "B2": 60},
         "ranks": {"SO-B1\x1fA": 1, "SO-B2\x1fA": 2},
         "cp_completion": {}, "cp_solved_book_sig": ""}
    plan = _lay(masters, [_B("B1", "A", 60), _B("B2", "A", 60)], g)

    busy: dict = {}
    for p in plan.placements:
        for start, end, who in p.segments:
            for other_start, other_end in busy.setdefault(who, []):
                assert not (start < other_end and other_start < end), (
                    f"{who} is at two machines between {start} and {end}")
            busy[who].append((start, end))
    assert {p.machine for p in plan.placements} == {"MD1", "MD2"}


# --------------------------------------------------------------------------- #
# Solve, then replay — spec §8's integrity check, on a REAL solve
# --------------------------------------------------------------------------- #

def _contended_book():
    """Four jobs over a shop with two CNCs and three benches served by two
    helpers. Helper contention is deliberate: it is what makes the per-machine
    sequence a real decision, and it was the shape that exposed the drift."""
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC2": Machine("CNC2", "CNC 2", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
        "MD2": Machine("MD2", "MD 2", "manual", available_hrs_per_day=9.5),
        "MI1": Machine("MI1", "MI 1", "inspection", available_hrs_per_day=9.5),
    }
    routings, batches = {}, []
    for i, (cycle, qty, due) in enumerate([(2.0, 60, 14), (1.0, 120, 15),
                                           (3.0, 40, 16), (1.0, 80, 14)]):
        item = f"IT{i}"
        routings[item] = Routing(item, item, "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", cycle, None, None, "CNC1/CNC2"),
            Process(2, "DEBURING", 0.5, None, None, "MD1/MD2"),
            Process(3, "INSP", 0.3, None, None, "MI1")])
        batches.append(_B(f"B{i}", item, qty, date(2026, 8, due)))
    operators = [
        Operator("A", "CNC1/CNC2", ["CNC1", "CNC2"], "First shift"),
        Operator("B", "CNC1/CNC2", ["CNC1", "CNC2"], "2nd shift"),
        Operator("C", "MD1/MD2/MI1", ["MD1", "MD2", "MI1"], "First shift"),
        Operator("D", "MD1/MD2/MI1", ["MD1", "MD2", "MI1"], "First shift"),
    ]
    return _masters(routings, operators, machines), batches


def test_a_solved_book_replays_to_the_same_completion_dates():
    """Spec §8: on the book that was solved, drift must be 0. Not asserted with
    a hand-built genome — this SOLVES, reads ``Solved.genome``, replays it, and
    compares against the solver's own completions.

    Measured over 40 generated books of this shape: 280 of 280 orders replay to
    the SAME completion date, in both directions. Before ``cp_start_of`` existed
    the same experiment drifted 1-7 days per book.
    """
    pytest.importorskip("pyjobshop")
    from cp_engine import solve

    masters, batches = _contended_book()
    solved = solve.solve_book(batches, masters, _cfg(), PLAN_START,
                              time_limit=20, horizon_days=20, num_workers=1)
    assert solved.status_ok, solved.status

    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, {})
    plan = decode.lay_out(jobs, shop, _cfg(), PLAN_START, solved.genome)

    assert plan.unassigned == ()          # the genome covers the book it solved
    assert plan.dropped == ()
    for key, solved_end in solved.completion.items():
        assert plan.completion[key].date() == solved_end.date(), (
            f"{key}: solved {solved_end}, replayed {plan.completion[key]}")


def test_the_genome_carries_the_solved_start_and_the_bench_crew():
    """The producing half of the pair. ``cp_start_of`` must be an ABSOLUTE ISO
    datetime equal to the solver's own start (a relative offset would slide the
    whole plan when the stored plan clock advances between solve and replay), and
    ``cp_bench_of`` must actually name the helper the solver booked — Rule 1
    rosters CNC/VMC only, so ``cp_roster`` structurally cannot."""
    pytest.importorskip("pyjobshop")
    from cp_engine import solve

    masters, batches = _contended_book()
    solved = solve.solve_book(batches, masters, _cfg(), PLAN_START,
                              time_limit=20, horizon_days=20, num_workers=1)
    assert solved.status_ok, solved.status

    starts = solved.genome["cp_start_of"]
    assert set(starts) == set(solved.windows)
    for key, (solved_start, _end) in solved.windows.items():
        assert (datetime.fromisoformat(starts[key])
                == PLAN_START + timedelta(minutes=solved_start)), key

    bench = solved.genome["cp_bench_of"]
    # Every DEBURING and INSP step ran at a bench and must name its helper; no
    # CNC step may, because a machining mode books no operator at all.
    by_name = {o.name: o for o in masters.operators}
    for (job_key, op_seq), who in bench.items():
        assert op_seq in (2, 3), (job_key, op_seq)
        assert solved.machine_of[(job_key, op_seq)] in by_name[who].machines
    assert set(bench) == {key for key in solved.windows if key[1] in (2, 3)}
    assert not any(key[1] == 1 for key in bench)


def test_no_op_in_a_replayed_solve_starts_before_the_solver_put_it():
    """The floor's own guarantee, and the reason the per-machine sequence comes
    back without a sequence key: solved starts on one machine do not overlap, so
    pinning every op at or after its solved start reproduces their order."""
    pytest.importorskip("pyjobshop")
    from cp_engine import solve

    masters, batches = _contended_book()
    solved = solve.solve_book(batches, masters, _cfg(), PLAN_START,
                              time_limit=20, horizon_days=20, num_workers=1)
    assert solved.status_ok, solved.status

    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    plan = decode.lay_out(jobs, domain.build_shop(masters, {}), _cfg(),
                          PLAN_START, solved.genome)
    started = {}
    for p in plan.placements:
        started.setdefault((p.job_key, p.op_seq), p.start)
    checked = 0
    for key, (solved_start, _end) in solved.windows.items():
        if key not in started:
            continue
        checked += 1
        assert started[key] >= PLAN_START + timedelta(minutes=solved_start), key
    assert checked >= len(solved.windows) - 1      # the fixture is not vacuous


# --------------------------------------------------------------------------- #
# The replay path runs on Render
# --------------------------------------------------------------------------- #

def test_decode_imports_with_no_solver_installed():
    """``cp_engine.decode`` runs on the production server, which deliberately has
    neither pyjobshop nor ortools. Proven by blocking both and importing."""
    probe = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('pyjobshop', 'ortools'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import cp_engine.decode\n"
        "assert 'pyjobshop' not in sys.modules and 'ortools' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
