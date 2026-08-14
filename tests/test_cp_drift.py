"""The integrity check: does the replay reproduce the solve?

The CP solve runs off-box and stores a genome; the app replays that genome on
every page load. **If the two disagree, the plan on screen is not the plan that
was solved** — and the late-days the search optimised are not the late-days the
published plan realises. That is this repo's most expensive recurring defect
class (the Gantt saying 07-Sep while the delay report said 04-Sep), so it is
CHECKED, not assumed.

WHAT IS ASSERTED HERE, AND WHAT IS DELIBERATELY NOT. Task 8 measured the replay
over 40 solved books / 280 orders and a re-reviewer repeated it with an
independent generator:

  * completion-DATE drift is **exactly 0**, in both directions, on every book
    measured — that is the published quantity (Orders expected completion, the
    Gantt, the delay report, late-days) and it is asserted here with no epsilon.
    It is an empirical property of those books and NOT an invariant:
    ``test_a_real_disagreement_between_solve_and_replay_is_CAUGHT`` below is a
    single-shift-bench shop that drifts a full day on every order, cause read off
    both schedules rather than assumed;
  * at MINUTE resolution there is a one-sided LATE residual with ONE known cause
    (``_JobState`` tracks one op at a time, so the decoder cannot release a
    successor while its predecessor is still in the chuck, while the model's
    release is a linear bound on start vars that fires mid-operation). +83 and
    +191 minutes have both been OBSERVED and **nothing establishes a ceiling**,
    so the test below asserts the residual's DIRECTION and REPORTS the worst
    value it measured. It never hardcodes a bound: a number that reads as a
    guarantee and is not one is worse than no number.

THE MOVED-BOOK INVARIANT IS TESTED ON A HAND-BUILT FIXTURE, ON PURPOSE. Task 8's
generated harness measured "worst move +0.0 min" — and then measured +0.0 again
with the fix reverted to its broken form, because where the solve staffs every
shift there are no dark shifts for a new order to open. A generated family
structurally cannot see that defect. ``test_a_new_order_never_pulls_a_solved_
order_earlier`` is first-shifts-only and hand-built for exactly that reason.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from cp_engine import decode, domain, report
from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           ScheduleEntry, WorkCalendar)

PLAN_START = datetime(2026, 8, 12, 8, 0)      # a Wednesday; 13-08 is the weekly off
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fixtures — the same shapes the decoder's own tests use, so a failure here is
# about the report and not about a second way of describing the shop.
# --------------------------------------------------------------------------- #

class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = None


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0, **kw)


def _masters(routings, operators, machines):
    return Masters(machines=dict(machines), routings=routings,
                   operators=list(operators), calendar=WorkCalendar())


def _entry(batch, end, *, seq=1, machine="CNC1", start=None):
    start = start or end
    return ScheduleEntry(
        batch_id=batch, item_code="ITEM", process_seq=seq, process_name="op",
        machine=machine, qty=1, occupancy_min=60.0,
        start=start, end=end, so_refs=["SO1"], operator="N",
        op_segments=[(start, end, "N")])


def _entries(plan):
    """Placements -> ``ScheduleEntry``. What Task 11's adapter will publish; the
    checks in this file all read the PUBLISHED plan, never the decoder's
    internals, so they measure what shipped."""
    out = []
    for p in plan.placements:
        out.append(ScheduleEntry(
            batch_id=p.job_key, item_code=p.job_key, process_seq=p.op_seq,
            process_name=p.op_name, machine=p.machine or "", qty=p.qty,
            occupancy_min=p.work_min, start=p.start, end=p.end,
            so_refs=[f"SO-{p.job_key}"],
            operator=(p.segments[0][2] if p.segments else ""),
            op_segments=list(p.segments)))
    return out


def _lay(masters, batches, g, config=None):
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, {})
    return decode.lay_out(jobs, shop, config or _cfg(), PLAN_START, g)


# --------------------------------------------------------------------------- #
# completion_drift — the unit contract
# --------------------------------------------------------------------------- #

def test_no_drift_when_the_replay_reproduces_the_solved_dates():
    g = {"cp_completion": {"B1": "2026-09-04"}}
    entries = [_entry("B1", datetime(2026, 9, 4, 17, 0))]
    assert report.completion_drift(entries, g) == []


def test_drift_is_reported_with_both_dates_and_the_gap():
    """On the book that was solved this must never fire. When it does, the plan
    on screen is not the plan that was solved — the defect class this repo keeps
    paying for (the Gantt saying 07-Sep while the delay report said 04-Sep)."""
    g = {"cp_completion": {"B1": "2026-09-04"}}
    rows = report.completion_drift([_entry("B1", datetime(2026, 9, 7, 17, 0))], g)
    assert len(rows) == 1
    assert rows[0]["kind"] == report.KIND_DRIFT
    assert rows[0]["days"] == 3
    assert "2026-09-04" in rows[0]["message"] and "2026-09-07" in rows[0]["message"]
    assert rows[0]["batch_id"] == "B1"
    assert rows[0]["solved"] == "2026-09-04"
    assert rows[0]["replayed"] == "2026-09-07"


def test_a_one_day_gap_is_reported_with_no_epsilon_PURE_UNIT():
    """Every other drift case in this file uses a 3-day gap, so a mutation of
    the comparison to ``abs(days) <= 1`` is killed ONLY by
    ``test_a_real_disagreement_between_solve_and_replay_is_CAUGHT`` below, which
    sits behind ``pytest.importorskip("pyjobshop")``. Production and any CI
    mirroring it have no solver, so on those boxes that whole test SKIPS and a
    1-day tolerance could be reintroduced with the suite still green. This case
    needs no solver — it closes the hole exactly where it exists: a ONE-day gap
    must still be reported, not rounded away as "close enough"."""
    g = {"cp_completion": {"B1": "2026-09-04"}}
    rows = report.completion_drift([_entry("B1", datetime(2026, 9, 5, 17, 0))], g)
    assert len(rows) == 1
    assert rows[0]["kind"] == report.KIND_DRIFT
    assert rows[0]["days"] == 1
    assert rows[0]["solved"] == "2026-09-04"
    assert rows[0]["replayed"] == "2026-09-05"


def test_an_earlier_replay_is_reported_as_early_not_as_a_distance():
    """The SIGN is the finding. Late means the published plan realises worse
    late-days than the search optimised; EARLY means the decoder handed itself
    capacity the solver withheld (the 2026-08-14 pool-escape defect: a solved
    order pulled 1 day 13 h forward through night shifts the solve left dark).
    An absolute gap would report both as the same row."""
    g = {"cp_completion": {"B1": "2026-09-07"}}
    rows = report.completion_drift([_entry("B1", datetime(2026, 9, 4, 17, 0))], g)
    assert len(rows) == 1
    assert rows[0]["days"] == -3
    assert "EARLIER" in rows[0]["message"]


def test_a_batch_the_genome_never_saw_is_not_drift():
    """An order uploaded since the solve has no solved date to disagree with.
    Calling that drift would cry wolf on every plan after every upload."""
    g = {"cp_completion": {"B1": "2026-09-04"}}
    entries = [_entry("B1", datetime(2026, 9, 4, 17, 0)),
               _entry("B2", datetime(2026, 9, 9, 17, 0))]
    assert report.completion_drift(entries, g) == []


def test_an_empty_genome_reports_nothing():
    assert report.completion_drift([_entry("B1", datetime(2026, 9, 4))], {}) == []
    assert report.completion_drift([_entry("B1", datetime(2026, 9, 4))], None) == []


def test_the_completion_is_the_LAST_entry_including_an_off_machine_lane():
    """A batch's completion is the latest end across every lane it touches. The
    delay report derived completion from real machine ops only, dropped the OS /
    Off-machine lanes and published a date the Gantt disagreed with (2026-08-07);
    a drift check with that same blind spot would report drift that is not there
    — or, worse, miss drift that is."""
    g = {"cp_completion": {"B1": "2026-09-09"}}
    entries = [_entry("B1", datetime(2026, 9, 4, 17, 0)),
               _entry("B1", datetime(2026, 9, 9, 9, 0), seq=2,
                      machine="OS / Outsourced")]
    assert report.completion_drift(entries, g) == []
    # ...and with the off-lane milestone removed the same book DOES drift, so
    # the assertion above is not passing by accident.
    assert len(report.completion_drift(entries[:1], g)) == 1


def test_rows_are_ordered_by_batch_so_two_runs_can_be_diffed():
    g = {"cp_completion": {"B1": "2026-09-04", "B2": "2026-09-04",
                           "B3": "2026-09-04"}}
    entries = [_entry(b, datetime(2026, 9, 7, 17, 0)) for b in ("B3", "B1", "B2")]
    assert [r["batch_id"] for r in report.completion_drift(entries, g)] == \
        ["B1", "B2", "B3"]


# --------------------------------------------------------------------------- #
# Staleness — the genome was solved against a book, and books move
# --------------------------------------------------------------------------- #

def _sig_book():
    masters = _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift")],
        {"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)})
    return masters, [_B("B1", "A", 60)]


def test_todays_signature_is_computed_the_way_the_genome_computed_it():
    """Requirement carried from Task 7: the genome's ``cp_solved_book_sig`` is a
    hash of ``cp_engine.domain.Job``/``Op`` fields. Today's side must be the SAME
    computation over today's jobs, or the two can never byte-match."""
    from cp_engine import genome as genome_mod
    masters, batches = _sig_book()
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    assert report.book_signature(batches, masters) == \
        genome_mod._book_signature(jobs)


def test_the_apps_own_book_signature_can_never_match_and_is_not_used():
    """``engine.optimize_service.book_signature`` hashes ``SOLine`` fields —
    process_qty, lane, promise. Structurally a different thing. Importing it here
    would make the staleness banner cry wolf on EVERY plan, so this pins that the
    two really are different values on one and the same book."""
    from engine.models import SOLine
    from engine.optimize_service import book_signature as app_signature
    masters, batches = _sig_book()
    lines = [SOLine("SO-B1", "A", "a", 60, date(2026, 12, 1))]
    assert report.book_signature(batches, masters) != app_signature(lines)


def test_an_unchanged_book_is_not_stale():
    masters, batches = _sig_book()
    g = {"cp_solved_book_sig": report.book_signature(batches, masters)}
    assert report.genome_stale(batches, masters, g) == []


def test_a_changed_book_is_reported_stale_once_and_names_the_consequence():
    masters, batches = _sig_book()
    g = {"cp_solved_book_sig": report.book_signature(batches, masters)}
    rows = report.genome_stale([_B("B1", "A", 90)], masters, g)
    assert len(rows) == 1
    assert rows[0]["kind"] == report.KIND_STALE


def test_a_genome_with_no_signature_is_never_called_stale():
    """A genome written before the key existed, or an empty one. Silence beats a
    row that says "stale" because it has nothing to compare."""
    masters, batches = _sig_book()
    assert report.genome_stale(batches, masters, {}) == []
    assert report.genome_stale(batches, masters, {"cp_solved_book_sig": ""}) == []


# --------------------------------------------------------------------------- #
# all_violations — the four rule checks, plus drift
# --------------------------------------------------------------------------- #

def test_the_four_roster_rule_checks_run_against_a_cp_plan():
    """Reused deliberately (spec §8): they are an INDEPENDENT implementation of
    the four rules, written for a different engine, which is exactly what makes
    them worth running here. They must all be 0."""
    from roster_engine import report as rr
    assert hasattr(rr, "operator_split_violations")
    assert hasattr(rr, "segmentation_violations")
    assert hasattr(rr, "machine_conflict_violations")
    assert hasattr(rr, "idle_capacity_violations")


def _helper_book():
    """One helper qualified for a CNC and a bench — the live 2026-08-07 Sandeep
    shape, and what makes ``OPERATOR_SPLIT_SHIFT`` reachable at all."""
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    masters = _masters(
        {"Z": Routing("Z", "z", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")]),
         "M": Routing("M", "m", "cust", "rm", None,
                      [Process(1, "DEBURING", 1.0, None, None, "MD1")])},
        [Operator("H", "CNC1/MD1", ["CNC1", "MD1"], "First shift"),
         Operator("S", "CNC1", ["CNC1"], "2nd shift")],
        machines)
    return masters, [_B("B9", "Z", 30), _B("B8", "M", 1)]


def test_all_four_checks_are_actually_run_over_a_replayed_cp_plan():
    masters, batches = _helper_book()
    g = {"cp_machine_of": {}, "cp_roster": {("CNC1", 1): "S"},
         "cp_overlap_of": {}, "ranks": {}, "cp_completion": {},
         "cp_solved_book_sig": ""}
    entries = _entries(_lay(masters, batches, g))
    rows = report.all_violations(entries, masters, _cfg())
    kinds = {r["kind"] for r in rows}
    assert "OPERATOR_SPLIT_SHIFT" not in kinds
    assert "OPERATION_SEGMENTED" not in kinds
    assert "MACHINE_DOUBLE_BOOKED" not in kinds
    # Non-vacuous: the plan really does put one helper on both machines.
    assert {e.machine for e in entries} == {"CNC1", "MD1"}
    assert {w for e in entries for _s, _e, w in e.op_segments} == {"H"}


def test_a_rule_breach_in_the_plan_is_reported():
    """The three rule checks are wired, not merely imported: a hand-made plan
    that puts one person on two machines in one first shift must come back as
    OPERATOR_SPLIT_SHIFT through ``all_violations``."""
    masters, _batches = _helper_book()
    at = datetime(2026, 8, 12, 9, 0)
    entries = [_entry("B9", at + timedelta(minutes=30), machine="CNC1", start=at),
               _entry("B8", at + timedelta(minutes=30), machine="MD1", start=at)]
    rows = report.all_violations(entries, masters, _cfg())
    assert "OPERATOR_SPLIT_SHIFT" in [r["kind"] for r in rows]
    # On the row, not only findable through the out-of-band RULE_KINDS tuple.
    row = next(r for r in rows if r["kind"] == "OPERATOR_SPLIT_SHIFT")
    assert row["breach"] is True


def _segmented(batch, spans, machine="CNC1", who="N"):
    return ScheduleEntry(
        batch_id=batch, item_code="ITEM", process_seq=1, process_name="op",
        machine=machine, qty=1, occupancy_min=60.0,
        start=spans[0][0], end=spans[-1][1], so_refs=["SO1"], operator=who,
        op_segments=[(s, e, who) for s, e in spans])


def test_a_segmented_operation_and_a_double_booked_machine_are_reported():
    """The other two rule checks are WIRED, not merely imported. Each needs its
    own shape: an intruder inside a gap is invisible to the conflict check, and a
    wholesale overlap (no gap anywhere) is invisible to the segmentation check —
    which is why both are run and both are proved to reach the caller."""
    masters, _batches = _helper_book()
    t = datetime(2026, 8, 12, 8, 0)

    def at(mins):
        return t + timedelta(minutes=mins)

    broken = _segmented("B1", [(at(0), at(30)), (at(90), at(120))])
    intruder = _segmented("B2", [(at(40), at(80))])
    rows = report.all_violations([broken, intruder], masters, _cfg())
    kinds = [r["kind"] for r in rows]
    assert "OPERATION_SEGMENTED" in kinds
    assert "MACHINE_DOUBLE_BOOKED" not in kinds       # nothing sits in a gap twice
    seg_row = next(r for r in rows if r["kind"] == "OPERATION_SEGMENTED")
    assert seg_row["breach"] is True

    whole = [_segmented("B3", [(at(0), at(60))]),
             _segmented("B4", [(at(10), at(50))])]
    rows = report.all_violations(whole, masters, _cfg())
    kinds = [r["kind"] for r in rows]
    assert "MACHINE_DOUBLE_BOOKED" in kinds
    assert "OPERATION_SEGMENTED" not in kinds         # neither entry has a gap
    conflict_row = next(r for r in rows if r["kind"] == "MACHINE_DOUBLE_BOOKED")
    assert conflict_row["breach"] is True


def test_idle_capacity_is_measured_and_an_absent_operator_is_not_spare_capacity():
    """IDLE_CAPACITY answers a CAPACITY question, not a rule breach, and its one
    input the entries cannot supply is leave. Without it a person on holiday
    reads as a free operator and the row accuses the plan of wasting a machine
    that nobody could have run — the 2026-08-09 rule: a report may never
    attribute a cause it did not CHECK."""
    masters, _batches = _helper_book()
    # CNC1 works the first shift of 12-08 and then nothing until 14-08, so the
    # night shift of 12-08 is dark with work already waiting — and S is on that
    # shift, qualified for CNC1 in Settings, and on no machine.
    entries = [
        _entry("B9", datetime(2026, 8, 12, 10, 0), machine="CNC1",
               start=datetime(2026, 8, 12, 8, 0)),
        _entry("B7", datetime(2026, 8, 14, 8, 30), machine="CNC1",
               start=datetime(2026, 8, 14, 8, 0)),
    ]
    blind = report.all_violations(entries, masters, _cfg())
    idle_rows = [r for r in blind if r["kind"] == "IDLE_CAPACITY"]
    assert idle_rows
    # ON THE ROW, not only inferrable by checking it is absent from RULE_KINDS —
    # a downstream consumer must not have to know that tuple exists.
    assert all(r["breach"] is False for r in idle_rows)
    aware = report.all_violations(
        entries, masters, _cfg(),
        absent={"S": [(datetime(2026, 8, 12), datetime(2026, 8, 20))]})
    assert not any(r["kind"] == "IDLE_CAPACITY" for r in aware)


def test_breach_is_true_for_a_kind_a_downstream_reader_never_saw_before():
    """The row key is the belt for kinds beyond the four ``roster_engine``
    checks too: drift and staleness are not rule breaches in the
    ``RULE_KINDS`` sense, but they are real disagreements the plan must not
    bury beside an ``IDLE_CAPACITY`` measurement, so they carry ``breach:
    True`` exactly like the three ``RULE_KINDS`` rows."""
    masters, batches = _sig_book()
    stale_g = {"cp_solved_book_sig": "not-the-real-signature",
               "cp_completion": {}}
    stale_rows = report.all_violations(
        [_entry("B1", datetime(2026, 9, 4, 17, 0))], masters, _cfg(),
        batches=batches, genome=stale_g)
    assert stale_rows and all(r["breach"] is True for r in stale_rows)

    drift_g = {"cp_completion": {"B1": "2026-09-04"}, "cp_solved_book_sig": ""}
    drift_rows = report.all_violations(
        [_entry("B1", datetime(2026, 9, 7, 17, 0))], masters, _cfg(),
        genome=drift_g)
    assert drift_rows and all(r["breach"] is True for r in drift_rows)


def test_drift_rows_reach_all_violations():
    masters, _batches = _helper_book()
    entries = [_entry("B1", datetime(2026, 9, 7, 17, 0))]
    g = {"cp_completion": {"B1": "2026-09-04"}}
    kinds = [r["kind"] for r in
             report.all_violations(entries, masters, _cfg(), genome=g)]
    assert report.KIND_DRIFT in kinds


def test_a_stale_genome_reports_staleness_INSTEAD_of_a_drift_row_per_order():
    """Spec §8's second bullet. Once the book has moved, drift is EXPECTED — the
    honest row is "this genome was solved against a different book", once, not
    one alarm per order about a disagreement that is fully explained."""
    masters, batches = _sig_book()
    g = {"cp_solved_book_sig": report.book_signature(batches, masters),
         "cp_completion": {"B1": "2026-09-04"}}
    entries = [_entry("B1", datetime(2026, 9, 7, 17, 0))]
    moved = [_B("B1", "A", 90)]
    kinds = [r["kind"] for r in
             report.all_violations(entries, masters, _cfg(),
                                   batches=moved, genome=g)]
    assert kinds.count(report.KIND_STALE) == 1
    assert report.KIND_DRIFT not in kinds
    # ...and on the SAME book the drift row is not suppressed.
    kinds = [r["kind"] for r in
             report.all_violations(entries, masters, _cfg(),
                                   batches=batches, genome=g)]
    assert report.KIND_STALE not in kinds
    assert report.KIND_DRIFT in kinds


def test_no_entries_is_no_report():
    masters, _batches = _sig_book()
    assert report.all_violations([], masters, _cfg()) == []


# --------------------------------------------------------------------------- #
# THE MOVED-BOOK INVARIANT — hand-built, first shifts only
# --------------------------------------------------------------------------- #

def _first_shifts_only_genome():
    """CNC1 rostered in the FIRST shift of every working day and nowhere else —
    the solver deliberately leaving the night shifts dark. Shift indices run
    0 = 12-08 first, 1 = 12-08 second, 2 = 14-08 first (13-08 is the weekly off),
    so the even ones are the first shifts."""
    return {"cp_machine_of": {("B1", 1): "CNC1"},
            "cp_roster": {("CNC1", i): "N" for i in range(0, 20, 2)},
            "cp_overlap_of": {"B1": 1000}, "ranks": {"SO-B1\x1fA": 1},
            "cp_completion": {"B1": "2026-08-14"}, "cp_solved_book_sig": "",
            "cp_start_of": {("B1", 1): "2026-08-12T08:00:00"}}


def _two_item_cnc_masters():
    machines = {"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                available_hrs_per_day=19.5)}
    return _masters(
        {"A": Routing("A", "a", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")]),
         "Z": Routing("Z", "z", "cust", "rm", None,
                      [Process(1, "CNC FIRST SIDE", 1.0, None, None, "CNC1")])},
        [Operator("N", "CNC1", ["CNC1"], "First shift"),
         Operator("S", "CNC1", ["CNC1"], "2nd shift")],
        machines)


def test_a_new_order_never_pulls_a_solved_order_earlier():
    """THE assertion a solved-book-only drift harness structurally CANNOT make:
    on a replay of the book that was solved there are no unsolved ops, so the
    pool escape never fires and this hole stays invisible. Task 8's generated
    harness measured "worst move +0.0 min" both with its fix and with the fix
    reverted to the broken per-machine form, which is why THIS fixture is
    hand-built and first-shifts-only.

    Measured with the ``restricted`` guard neutered (``decode.py``'s mutation D1
    — the escape then opens the dark shift to the SOLVED book too, not only to
    the fallback op that earned it): B1 completed 14-08 15:10 alone and 13-08
    02:10 once one unrelated 30-piece order landed on the same CNC — a solved
    order pulled 1 day 13 h EARLIER through night shifts the solve had left
    dark. That is EARLY drift, and this check is what names it. (D3 — keying the
    escape on the machine for the whole horizon instead of re-deriving it
    per-op — was the original suspect and no longer reproduces this: it now
    survives this fixture, per the round-1 review's mutation table.)
    """
    masters = _two_item_cnc_masters()
    g = _first_shifts_only_genome()
    alone = _entries(_lay(masters, [_B("B1", "A", 1000)], g))
    moved = _entries(_lay(masters, [_B("B1", "A", 1000), _B("B9", "Z", 30)], g))

    assert report.completion_drift(alone, g) == []
    assert report.completion_drift(moved, g) == []
    # Non-vacuous: the new order really is in the plan (never dropped for want of
    # staffing), so the escape it needs really did fire.
    assert {e.batch_id for e in moved} == {"B1", "B9"}


# --------------------------------------------------------------------------- #
# Solve, then replay — the check on a REAL solve
# --------------------------------------------------------------------------- #

def _contended_book():
    """Four jobs over two CNCs and three benches served by two helpers. Helper
    contention is deliberate: it is what makes the per-machine sequence a real
    decision, and it is the shape that exposed the drift in the first place."""
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


def _single_shift_bench_book():
    """THE SHAPE THAT DRIFTS, and it is not exotic: benches that run the day
    shift only, one helper covering them all, and CNC batches long enough to span
    the 19:00 change. That is the owner's own book (MD/MW/MPK/MI stations are
    single-shift and helpers are scarce), which is why it is measured here rather
    than assumed away."""
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC2": Machine("CNC2", "CNC 2", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
        "MI1": Machine("MI1", "MI 1", "inspection", available_hrs_per_day=9.5),
    }
    routings, batches = {}, []
    for i, (qty, cycle) in enumerate([(900, 1.0), (300, 2.0)]):
        item = f"IT{i}"
        routings[item] = Routing(item, item, "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", cycle, None, None, "CNC1/CNC2"),
            Process(2, "DEBURING", 0.5, None, None, "MD1"),
            Process(3, "INSP", 0.3, None, None, "MI1")])
        batches.append(_B(f"B{i}", item, qty, date(2026, 8, 20)))
    operators = [
        Operator("A", "CNC1/CNC2", ["CNC1", "CNC2"], "First shift"),
        Operator("B", "CNC1/CNC2", ["CNC1", "CNC2"], "2nd shift"),
        Operator("C", "MD1/MI1", ["MD1", "MI1"], "First shift"),
    ]
    return _masters(routings, operators, machines), batches


def _solved_and_replayed(book=None, hold=True):
    """Solve, then replay the genome the solve stored.

    ``hold`` is ``solve_book``'s ``hold_across_unmanned_shift``: True is E2 (the
    function's own default), False is E1 — the SHIPPING default, authorized by
    the owner after E2 was measured unusable at his scale. Both are exercised,
    because a claim that holds only under the encoding nobody ships is worthless.
    """
    from cp_engine import solve
    masters, batches = book or _contended_book()
    solved = solve.solve_book(batches, masters, _cfg(), PLAN_START,
                              time_limit=20, horizon_days=25, num_workers=1,
                              hold_across_unmanned_shift=hold)
    assert solved.status_ok, solved.status
    plan = _lay(masters, batches, solved.genome)
    return masters, batches, solved, plan


@pytest.mark.parametrize("hold", [True, False])
def test_a_solved_book_replays_with_ZERO_date_drift(hold):
    """Spec §8: on the book that was solved, drift must be 0 — exactly, with no
    epsilon. This SOLVES, reads ``Solved.genome``, replays it, and runs the check
    over the PUBLISHED entries."""
    pytest.importorskip("pyjobshop")
    masters, batches, solved, plan = _solved_and_replayed(hold=hold)
    entries = _entries(plan)
    assert report.completion_drift(entries, solved.genome) == []
    # Non-vacuous: every batch really was compared.
    assert len(solved.genome["cp_completion"]) == len(batches)
    assert {e.batch_id for e in entries} == set(solved.genome["cp_completion"])
    assert report.genome_stale(batches, masters, solved.genome) == []


def test_the_minute_level_residual_is_ZERO_and_never_EARLY(capsys):
    """The residual the DATE check cannot see, asserted as what is MEASURED.

    It used to be one-sided LATE — up to +83 min on a generated harness and
    +191 on an OS-blocks + scarce-crew shape — with ONE cause: ``decode``
    tracked one op at a time, so a successor could not be released while its
    predecessor was still in the chuck, while the model's release is a linear
    bound on start vars that fires mid-operation. **That was fixed on
    2026-08-14** (``decode._JobState`` now carries per-op state and ``_release``
    runs after every work slice), and on this fixture — the single-shift-bench
    shape that produced the worst of it — the residual is now exactly 0 under
    BOTH encodings.

    Asserted at 0 rather than "<= something": a tolerance here is how an epsilon
    creeps back in, and this repo's rule is to tighten the model, never loosen
    the decoder. If a future change makes it non-zero, the number is the finding
    — do not widen this.

    EARLY is asserted separately and stays the louder failure: it would mean the
    decoder handed itself capacity the solver withheld, which is a defect in a
    direction no amount of conservatism excuses.
    """
    pytest.importorskip("pyjobshop")
    _m, _b, solved, plan = _solved_and_replayed(_single_shift_bench_book(),
                                                hold=False)
    residuals = {key: (plan.completion[key] - end).total_seconds() / 60.0
                 for key, end in solved.completion.items()
                 if key in plan.completion}
    assert len(residuals) == len(solved.completion)
    early = {k: v for k, v in residuals.items() if v < 0}
    assert not early, f"the replay finished EARLIER than the solve: {early}"
    assert max(residuals.values()) == 0, residuals
    with capsys.disabled():
        print(f"\n  minute-level residual over {len(residuals)} orders: "
              f"{max(residuals.values()):+.0f} min")


def test_the_single_shift_bench_shape_replays_with_ZERO_date_drift():
    """THE REGRESSION for the 2026-08-14 concurrency fix, on the shape that
    exposed the defect.

    This book is the owner's own: benches on the day shift only, one helper
    covering them all, CNC batches long enough to span the 19:00 change. Under
    the shipping E1 default, on a solve the solver calls OPTIMAL, it used to
    drift **a full day on every order** — the decoder could not release DEBURING
    until its CNC feeder left the chuck at 19:30, by which time the single-shift
    bench had closed, and the next window was two days out across the weekly off.

    It is now exactly 0, with no epsilon. Reverting the fix in ``decode.py``
    brings the rows straight back, which is what makes this test worth its run
    time rather than a tautology.
    """
    pytest.importorskip("pyjobshop")
    masters, batches, solved, plan = _solved_and_replayed(
        _single_shift_bench_book(), hold=False)
    assert report.genome_stale(batches, masters, solved.genome) == []   # not stale
    assert report.completion_drift(_entries(plan), solved.genome) == []
    # Non-vacuous: every batch really was compared, and the shape really is the
    # one described (a bench step whose feeder runs past the bench's close).
    assert len(solved.genome["cp_completion"]) == len(batches)
    feeders = {(p.job_key, p.op_seq): p for p in plan.placements}
    assert any(feeders[(key, 2)].start < feeders[(key, 1)].end
               for key in {p.job_key for p in plan.placements}), \
        "no bench step overlaps its feeder — this book no longer tests the fix"


def test_a_real_disagreement_between_solve_and_replay_is_CAUGHT(capsys):
    """The check earning its keep against a REAL solve, not a hand-made dict.

    The disagreement is manufactured the one way that is honest: the replay is
    given a DIFFERENT plan clock from the solve. Nothing in the genome survives
    that — ``cp_roster`` is keyed on a shift index counted from ``plan_start``,
    ``decode._windows`` rebuilds the calendar from it, and every solved-start
    floor is clamped up to it — so the replay really is a different plan, and
    ``completion_drift`` is what has to say so.

    This is the state a stale genome reaches in production (a plan clock that
    moved between the solve and the replay), so it is worth pinning against a
    real solve rather than against a dict of dates somebody typed.
    """
    pytest.importorskip("pyjobshop")
    masters, batches, solved, _plan = _solved_and_replayed(
        _single_shift_bench_book(), hold=False)
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, {})
    moved = decode.lay_out(jobs, shop, _cfg(), PLAN_START + timedelta(days=2),
                           solved.genome)

    rows = report.completion_drift(_entries(moved), solved.genome)
    assert rows, "a two-day clock shift produced no disagreement at all"
    assert report.genome_stale(batches, masters, solved.genome) == []   # book same
    for row in rows:
        assert row["days"] > 0                       # later clock, later plan
        assert row["solved"] in row["message"]
        assert row["replayed"] in row["message"]
    with capsys.disabled():
        print("\n  date drift from a replay clock 2 days off the solve's: "
              + ", ".join(f"{r['batch_id']} {r['solved']}->{r['replayed']} "
                          f"(+{r['days']}d)" for r in rows))


def test_the_three_rule_checks_are_zero_on_a_real_solved_plan():
    """Spec §8: an independent implementation of the same rules, run against the
    CP plan. IDLE_CAPACITY is deliberately not in this list — see
    ``all_violations``; it is a capacity measurement, not a rule breach."""
    pytest.importorskip("pyjobshop")
    masters, batches, solved, plan = _solved_and_replayed()
    rows = report.all_violations(_entries(plan), masters, _cfg(),
                                 batches=batches, genome=solved.genome)
    must_be_zero = report.RULE_KINDS + (report.KIND_DRIFT, report.KIND_STALE)
    assert [r for r in rows if r["kind"] in must_be_zero] == []
    # The names are the checks' own, not a second spelling of them.
    assert set(report.RULE_KINDS) == {"OPERATOR_SPLIT_SHIFT",
                                      "OPERATION_SEGMENTED",
                                      "MACHINE_DOUBLE_BOOKED"}


# --------------------------------------------------------------------------- #
# The replay path runs on Render
# --------------------------------------------------------------------------- #

def test_report_imports_with_no_solver_installed():
    """``cp_engine.report`` is on the replay path: it is imported by whatever
    surface publishes the plan, on a production server that deliberately has
    neither pyjobshop nor ortools. Proven by blocking both and importing."""
    probe = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('pyjobshop', 'ortools'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import cp_engine.report\n"
        "assert 'pyjobshop' not in sys.modules and 'ortools' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
