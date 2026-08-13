"""Degrading instead of refusing — and the invariants that guard the difference.

THE DEFECT THIS FILE EXISTS FOR (final whole-branch review, 2026-08-13). One
machine with nobody qualified for it in Settings cost the ENTIRE book its plan:
``schedule`` raised, ``run_forward`` stopped the chain, and the planner got a
banner and no schedule at all — for every order that never touches that machine.
Measured on the repo's own sample workbook with MI1 unstaffed (item B does not
use MI1): ``new`` 2 entries, ``classic`` 4 entries, ``roster`` **0**.

That inverts CLAUDE.md principle 5(a) — a master-data gap is non-blocking and
skips only the affected order — and it is not hypothetical: CLAUDE.md records
that routings naming machines the Machine master has not caught up with (CNC7,
VMC3, CNC6) are an expected, ongoing condition. They register as provisional
machines nobody is qualified for until an admin assigns someone.

BOTH DIRECTIONS ARE PINNED HERE, because the fix has an obvious wrong shape:
degrading everything, so a book nothing can be planned from returns an empty
schedule that reads as "no work to do". A dropped order must never be silent
(the repo's most expensive recurring defect class), and a book that genuinely
cannot be run must still fail loud.
"""

import dataclasses
import io
from datetime import date, datetime

import pytest

from engine import pipeline, roster_adapter
from engine.config import Config
from engine.loaders import load_all
from engine.models import (Machine, Masters, Operator, PlanRun, Process, Routing,
                           ScheduleEntry, WorkCalendar)
from roster_engine import report as rreport
from roster_engine import scheduler
from roster_engine.domain import build_jobs, build_shop
from tests.sample_workbook import ITEM_A, ITEM_B, build_sample_bytes

PLAN_START = date(2026, 8, 12)


def _cfg(**kw):
    base = dict(plan_start_date=PLAN_START, scheduler="roster",
                overlap_percent=80, setup_time_min=90.0,
                apply_operator_logic=True)
    base.update(kw)
    return Config(**base)


def _book(machines_for_extra):
    """The shipped sample book with ONE Settings operator added, covering exactly
    ``machines_for_extra``. Operators are the Settings table, not the workbook
    sheet (CLAUDE.md), so this is what an admin does — not a data edit.

    Item A: BANDSAW(BS1) -> CNC(CNC1/CNC2) -> INSP(MI1).
    Item B: CNC(CNC9, provisional) -> WASHING(MW1).
    The sheet staffs CNC1/CNC2/BS1 only.
    """
    raw = build_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    ops = list(masters.operators) + [
        Operator(name="Operator Four",
                 preferred_machines_raw="/".join(machines_for_extra),
                 machines=list(machines_for_extra), shift="First shift")]
    return so_lines, dataclasses.replace(masters, operators=ops)


def _plan(so_lines, masters, **kw):
    run = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(run, _cfg(), masters, **kw)
    return run, trace


def _items(entries):
    return {e.item_code for e in entries}


def _notes(trace):
    return " ".join(trace["rule6"]["notes"])


# =========================================================================== #
# I1 — degrade at the placement-failure boundary
# =========================================================================== #

def test_one_unstaffable_machine_does_not_cost_the_whole_book_its_plan():
    """MI1 has nobody. Item B never touches MI1 and must still be planned."""
    so_lines, masters = _book(["MW1", "CNC9"])          # MI1 deliberately left out
    run, trace = _plan(so_lines, masters)
    assert trace["rule6"]["error"] is None, trace["rule6"]["error"]
    assert _items(run.schedule) == {ITEM_B}, [e.item_code for e in run.schedule]


def test_the_order_that_could_not_be_planned_is_never_silent():
    """A dropped order with no note is this codebase's worst failure mode: the
    plan looks complete and an order is simply gone. The note must name the order,
    the step and the machine, through the same ``notes`` channel ``build_jobs``
    reports its NO_ROUTING skips on."""
    so_lines, masters = _book(["MW1", "CNC9"])
    _run, trace = _plan(so_lines, masters)
    note = _notes(trace)
    assert "could NOT be scheduled" in note, note
    assert "MI1" in note and "INSP" in note, note
    assert ITEM_A in note, note


def test_a_dropped_order_leaves_no_half_laid_routing():
    """Item A's BANDSAW and CNC steps ARE placeable; only its INSP step is not.
    Publishing the first two would draw half a routing on the Gantt and let the
    delay report bill an order that will never ship — worse than an omission."""
    so_lines, masters = _book(["MW1", "CNC9"])
    run, _trace = _plan(so_lines, masters)
    assert not [e for e in run.schedule if e.item_code == ITEM_A], run.schedule


def test_a_book_where_nothing_can_be_placed_still_fails_loud():
    """The other direction. The sample workbook AS SHIPPED staffs CNC1/CNC2/BS1
    only, so item A is blocked at MI1 and item B at its very first step (CNC9) —
    not one order can be completed. Degrading THAT would hand back an empty
    schedule that reads as "nothing to do"; it stays a typed RuleError."""
    raw = build_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    run = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(run, _cfg(), masters)
    err = trace["rule6"]["error"]
    assert err and err["rule"] == "rule6", trace["rule6"]
    assert "not place a single order" in err["message"], err["message"]
    # And it is LOCALIZED: the earlier rules' tabs survive (CLAUDE.md 5(b)).
    assert trace["rule1"]["error"] is None and trace["rule1"]["output"]["rows"]


def test_the_engine_reports_the_dropped_jobs_on_the_plan_object():
    """The seam reads ``Plan.dropped``; pin the engine's own contract so a future
    change cannot quietly stop populating it while the notes still look fine."""
    so_lines, masters = _book(["MW1", "CNC9"])
    from engine.rules import rule1_consolidate
    batches = rule1_consolidate.run(list(so_lines), config=_cfg(), masters=masters)
    jobs, _by, _sk = build_jobs(batches, masters)
    plan = scheduler.schedule(jobs, [j.key for j in jobs], build_shop(masters),
                              _cfg(), overlap=0.8)
    assert len(plan.dropped) == 1, plan.dropped
    _key, seq, name, options, why = plan.dropped[0]
    assert (seq, name, options) == (3, "INSP", ("MI1",))
    assert "no operator in Settings is qualified" in why


# --------------------------------------------------------------------------- #
# ...and it reaches a surface a DIRECTOR reads
# --------------------------------------------------------------------------- #
#
# The rule6 note above renders at the bottom of the Rule 6 tab (web/app.js:926).
# `_report_for_book` never read rule6 notes, so the `#data-gaps-card` banner —
# the one the directors actually see — said nothing, and on the Orders tab the
# dropped order's expected completion was simply blank. For the unstaffed-machine
# case the pre-existing MACHINE_NO_OPERATOR row does reach the banner, but it
# names the CAUSE, not the consequence; for the "machines ARE staffable, horizon
# ran out" case the banner showed nothing at all.


def _banner(so_lines, masters, **kw):
    """The validation report as `_report_for_book` builds it for /run, as
    {kind: [message, ...]}."""
    from api import main as api_main
    run, _trace = _plan(so_lines, masters, **kw)
    table = api_main._report_for_book(
        masters, list(so_lines), absences=[], config=_cfg(),
        schedule=run.schedule, batches=run.batches_prioritized)
    kind = table["columns"].index("Kind")
    message = table["columns"].index("Message")
    out: dict = {}
    for row in table["rows"]:
        out.setdefault(row[kind], []).append(row[message])
    return out


def test_the_banner_names_the_order_that_is_in_no_plan():
    so_lines, masters = _book(["MW1", "CNC9"])          # MI1 has nobody
    rows = _banner(so_lines, masters)
    said = " ".join(rows.get("ORDER_NOT_PLANNED", []))
    assert said, sorted(rows)
    assert ITEM_A in said, said
    assert "in NO plan" in said, said
    assert "INSP" in said and "MI1" in said, said
    assert "no operator in Settings is qualified" in said, said


def test_the_banner_names_it_for_the_reason_that_has_no_other_row_either():
    """MI1 IS staffed — its only operator is away for the whole horizon — so
    MACHINE_NO_OPERATOR does not fire and, before this row, a re-reviewer found
    four banner rows and not one of them named the machine or the order."""
    so_lines, masters = _book(["MW1", "CNC9"])
    masters = dataclasses.replace(masters, operators=list(masters.operators) + [
        Operator(name="Operator Five", preferred_machines_raw="MI1",
                 machines=["MI1"], shift="First shift")])
    away = {"Operator Five": [(datetime(2026, 1, 1), datetime(2028, 1, 1))]}
    rows = _banner(so_lines, masters, reserved=away)
    # Non-vacuity: every OTHER row in this banner is about something else. The
    # MACHINE_NO_OPERATOR row that does fire names VMC1, a machine no order uses.
    others = [m for k, v in rows.items() if k != "ORDER_NOT_PLANNED" for m in v]
    assert others, rows                                  # there ARE other rows
    assert not [m for m in others if "MI1" in m or ITEM_A in m], others
    said = " ".join(rows.get("ORDER_NOT_PLANNED", []))
    assert ITEM_A in said and "in NO plan" in said, said
    # ...and it does not invent a cause it did not check (the 2026-08-09 rule).
    assert "IS staffable in Settings" in said, said
    assert "no operator in Settings is qualified" not in said, said


def test_the_banner_says_nothing_about_dropped_orders_on_a_clean_book():
    so_lines, masters = _book(["MI1", "MW1", "CNC9"])
    assert "ORDER_NOT_PLANNED" not in _banner(so_lines, masters)


def test_an_item_with_no_routing_is_not_reported_twice():
    """It already has a NO_ROUTING row of its own, and two rows for one condition
    is a banner nobody trusts."""
    so_lines, masters = _book(["MI1", "MW1", "CNC9"])
    masters = dataclasses.replace(
        masters, routings={k: v for k, v in masters.routings.items()
                           if k != ITEM_B})
    rows = _banner(so_lines, masters)
    assert "NO_ROUTING" in rows, sorted(rows)
    assert "ORDER_NOT_PLANNED" not in rows, rows.get("ORDER_NOT_PLANNED")


# --------------------------------------------------------------------------- #
# ...and the reason is CHECKED, not assumed (2026-08-09)
# --------------------------------------------------------------------------- #

def test_a_staffable_machine_is_not_blamed_on_a_missing_qualification():
    """MI1 IS staffed in Settings — its only operator is away for the whole
    horizon. The old text asserted "no shift ever had a qualified, rostered
    operator" for EVERY failure, including this one and including the ones
    ``_STALL_WINDOWS`` produces out of a merely slow book. A report may not
    attribute a cause it did not check; when the machines are staffable the
    honest answer is that the cause is unexplained, and it says so.

    (This is also the Task 6 deferral: a machine that cannot be MANNED for the
    whole horizon used to kill the book, because ``_staffable`` reads Settings
    and deliberately ignores absences.)
    """
    so_lines, masters = _book(["MW1", "CNC9"])
    masters = dataclasses.replace(masters, operators=list(masters.operators) + [
        Operator(name="Operator Five", preferred_machines_raw="MI1",
                 machines=["MI1"], shift="First shift")])
    away = {"Operator Five": [(datetime(2026, 1, 1), datetime(2028, 1, 1))]}
    run, trace = _plan(so_lines, masters, reserved=away)
    assert trace["rule6"]["error"] is None, trace["rule6"]["error"]
    assert _items(run.schedule) == {ITEM_B}
    note = _notes(trace)
    assert "ARE staffable" in note, note
    assert "no operator in Settings is qualified" not in note, note


def test_a_clean_book_reports_nothing_dropped():
    """The check must not cry wolf: a fully staffed book plans both items and
    says nothing at all about dropped orders."""
    so_lines, masters = _book(["MI1", "MW1", "CNC9"])
    run, trace = _plan(so_lines, masters)
    assert _items(run.schedule) == {ITEM_A, ITEM_B}
    assert "could NOT be scheduled" not in _notes(trace)


# =========================================================================== #
# I2 — the SEARCH must never prefer a plan that loses an order
# =========================================================================== #
#
# THE DEFECT (final review, 2026-08-13). Degrading made a dropped order CHEAP to
# the objective: ``objective.compute_metrics`` skips a job with no completion
# entry, so a plan that loses an order is scored only on the orders it kept.
# Measured on the book below, same config:
#
#   MI1 STAFFED   (item A planned)  dropped=0 jobs_scored=2 total_late=1034 SCORE=7200.20
#   MI1 UNSTAFFED (item A DROPPED)  dropped=1 jobs_scored=1 total_late= 512  SCORE=3600.20
#
# A plan that loses work scored STRICTLY BETTER. Before the degrade change that
# candidate raised and was scored `+inf`, so the search could never choose it.
# It was latent (0 of 600 evaluated candidates dropped anything across 4
# shop-sized books) and harmless while a drop is candidate-INDEPENDENT, but it
# goes live the moment a drop becomes candidate-DEPENDENT — which is exactly the
# "the horizon ran out but the machines ARE staffable" reason above.
#
# The fix is in the search's evaluator, not in what ``schedule`` returns: it
# ranks candidates on (dropped orders, score), so fewer drops always wins and the
# ordinary objective still drives the climb among candidates that drop the same.
# BOTH halves are pinned here — the second because scoring a dropping candidate
# `+inf` (the obvious fix) would leave a book that can only ever be planned with
# a drop with no incumbent at all, undoing the degrade fix one level up.


def _drop_fixture():
    """Two jobs, and a fake scheduler that DROPS one of them unless the sequence
    puts it first. Fake on purpose: a candidate-dependent drop is what the defect
    needs and no small real book produces one reliably. The dropping plan is
    deliberately the one that SCORES better — job A is 20 days late when it is
    planned and contributes nothing at all when it is dropped."""
    from roster_engine.domain import Job, Op, Shop

    a = Job(key="A", item_code="IA", qty=10, due=date(2026, 8, 20),
            so_refs=("SO1",), ops=(Op(1, "CNC", "machining", 10.0, ("CNC1",)),),
            remaining=None)
    b = Job(key="B", item_code="IB", qty=1, due=date(2026, 9, 30),
            so_refs=("SO2",), ops=(Op(1, "CNC", "machining", 1.0, ("CNC1",)),),
            remaining=None)
    shop = Shop(machines={}, operators=(), calendar=WorkCalendar(),
                machining_ids=frozenset({"CNC1"}), absent={})
    late = datetime(2026, 9, 9, 12, 0)          # 20 days past A's date
    on_time = datetime(2026, 9, 30, 12, 0)      # exactly B's date

    def fake_schedule(jobs, sequence, _shop, _config, **kw):
        if list(sequence)[:1] == ["A"]:
            return scheduler.Plan((), {"A": late, "B": on_time}, (), ())
        return scheduler.Plan((), {"B": on_time}, (), (
            ("A", 1, "CNC", ("CNC1",), "no operator in Settings is qualified"),))

    return [a, b], shop, fake_schedule


def test_a_candidate_that_drops_an_order_never_beats_one_that_keeps_it(monkeypatch):
    """The headline. The dropping candidate scores 0.x against the keeping
    candidate's 256.x, and the search must still return the one that plans both
    orders. Mutation-checked: score the plans on the objective alone and this
    fails."""
    from roster_engine import search as roster_search

    jobs, shop, fake = _drop_fixture()
    monkeypatch.setattr(scheduler, "schedule", fake)
    res = roster_search.optimize(jobs, shop, _cfg(), overlap=0.8,
                                 budget_evals=40, seed=1)
    assert res.sequence[:1] == ["A"], res.sequence
    assert res.dropped_jobs == 0, res.dropped_jobs
    # ...and it really did see the cheaper, order-losing plan and refuse it.
    assert res.score > 100.0, res.score


def test_the_search_still_climbs_among_candidates_that_drop_the_same_order():
    """The interaction the obvious fix gets wrong. With MI1 unstaffed EVERY
    candidate loses item A — the drop is candidate-INDEPENDENT — so scoring a
    dropping candidate `+inf` would leave the search with no incumbent, no
    metrics and nothing to climb, and the book would be back to having no plan.
    It must still return a real, scored plan."""
    from engine.rules import rule1_consolidate
    from roster_engine import search as roster_search

    so_lines, masters = _book(["MW1", "CNC9"])          # MI1 has nobody
    batches = rule1_consolidate.run(list(so_lines), config=_cfg(), masters=masters)
    jobs, _by, _sk = build_jobs(batches, masters)
    res = roster_search.optimize(jobs, build_shop(masters), _cfg(), overlap=0.8,
                                 budget_evals=30, seed=1)
    assert res.sequence, res
    assert res.score < float("inf"), res.score
    assert res.metrics is not None, "no plan to hand back — the degrade is undone"
    assert res.dropped_jobs >= 1, res.dropped_jobs


# --------------------------------------------------------------------------- #
# The search's own diagnosis (Task 9 deferral)
# --------------------------------------------------------------------------- #

def test_the_search_does_not_report_a_budget_problem_as_a_data_problem():
    """``search.optimize`` used to re-raise the scheduler's message verbatim when
    no candidate could be built. That message was written for a SINGLE PASS, where
    it really is a shop fact. In a search it is one of two causes — a book that
    cannot be staffed, or a crew neighbourhood too big for the budget — and this
    function cannot tell them apart, so it must not pick one."""
    from roster_engine import search as roster_search
    raw = build_sample_bytes()
    _so, masters = load_all(io.BytesIO(raw))            # as shipped: unstaffable
    from engine.rules import rule1_consolidate
    so_lines, _m = load_all(io.BytesIO(raw))
    batches = rule1_consolidate.run(list(so_lines), config=_cfg(), masters=masters)
    jobs, _by, _sk = build_jobs(batches, masters)
    with pytest.raises(scheduler.Unschedulable) as caught:
        roster_search.optimize(jobs, build_shop(masters), _cfg(), overlap=0.8,
                               budget_evals=6, seed=1)
    message = str(caught.value)
    assert "cannot tell those apart" in message, message
    assert "crew search needs a bigger budget" in message, message
    # The blocking step is still carried, so the seam can name a record id.
    assert caught.value.blocked


# --------------------------------------------------------------------------- #
# plan_start_date=None: typed, contained, and defaulted (Task 7 deferral)
# --------------------------------------------------------------------------- #

def test_an_unresolved_plan_start_is_a_typed_error_the_seam_can_contain():
    with pytest.raises(scheduler.PlanStartMissing):
        scheduler.schedule([], [], build_shop(_book(["MI1"])[1]),
                           Config(plan_start_date=None))
    assert issubclass(scheduler.PlanStartMissing, ValueError)


def test_the_seam_contains_it_rather_than_letting_it_unwind_run_forward():
    """``except Unschedulable`` never saw a ``ValueError``, so this one escaped
    ``run_forward`` and discarded the whole trace — every per-rule tab blank, a
    500 with nothing to show the planner. Reached with a config the ``or
    date.today()`` fallback cannot rewrite (it is not a dataclass), which is the
    only way left in."""
    class _Cfg:                                    # not a dataclass on purpose
        plan_start_date = None
        overlap_percent = 80
        setup_time_min = 90.0
        crew_rank = None

    so_lines, masters = _book(["MI1", "MW1", "CNC9"])
    from engine.rules import rule1_consolidate
    batches = rule1_consolidate.run(list(so_lines), config=_cfg(), masters=masters)
    with pytest.raises(pipeline.RuleError) as caught:
        roster_adapter.run(batches, config=_Cfg(), masters=masters, notes=[])
    assert caught.value.rule == "rule6"
    assert "plan_start_date is None" in caught.value.message


def test_the_seam_resolves_an_unresolved_plan_start_instead_of_failing():
    """``None`` means "auto: start from today (IST)"; the API boundary normally
    resolves it, and the sibling ``new_engine.optimize_sequence`` carries the same
    ``or date.today()`` fallback for the paths that do not go through it."""
    so_lines, masters = _book(["MI1", "MW1", "CNC9"])
    from engine.rules import rule1_consolidate
    cfg = _cfg(plan_start_date=None)
    batches = rule1_consolidate.run(list(so_lines), config=cfg, masters=masters)
    entries = roster_adapter.run(batches, config=cfg, masters=masters, notes=[])
    assert entries
    assert min(e.start for e in entries).date() >= date.today()


# =========================================================================== #
# M2 — a conflict is not a duplicate
# =========================================================================== #

_ROW = {"so_no": "SO1", "item_code": "ITEM", "process": "CNC FIRST SIDE",
        "op_seq": 1, "machine": "CNC1", "operator": "Narayan",
        "remaining_qty": 10, "prev_start": "2026-08-11T08:00:00"}


def _pin_masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
                  "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5)},
        routings={"ITEM": Routing("ITEM", "d", "", "", None, [
            Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")])},
        operators=[Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")],
        calendar=WorkCalendar())


class _Batch:
    def __init__(self):
        self.batch_id, self.item_code, self.qty = "B1", "ITEM", 20
        self.source_so_refs = ["SO1", "SO2"]
        self.so_delivery_date = date(2026, 12, 1)
        self.process_qty = None


def _pins_note(rows):
    say = []
    roster_adapter._pins(rows, {"B1": _Batch()}, _pin_masters(), say)
    return " ".join(say)


def test_two_clubbed_rows_on_the_SAME_machine_are_reported_as_duplicates():
    note = _pins_note([dict(_ROW), dict(_ROW, so_no="SO2",
                                        prev_start="2026-08-11T09:00:00")])
    assert "already covers ON THE SAME MACHINE" in note, note
    assert "CONFLICT" not in note, note


def test_two_rows_naming_DIFFERENT_machines_are_reported_as_a_conflict():
    """The legitimate clubbed-SO-lines case and a genuine data conflict used to
    print the same sentence — and the reviewer saw it 8-11 times per book on a WIP
    run, i.e. on the owner's screen. One operation cannot be in two chucks."""
    note = _pins_note([dict(_ROW), dict(_ROW, so_no="SO2", machine="CNC4",
                                        prev_start="2026-08-11T09:00:00")])
    assert "CONFLICT" in note, note
    assert "kept CNC1" in note, note                    # the earlier start wins
    assert "already covers ON THE SAME MACHINE" not in note, note


def test_the_engine_itself_tells_a_conflict_from_a_duplicate():
    """The seam dedups first, so the engine's own copy of the rule is a no-op on
    the app path — which is exactly why it needs its own test."""
    assert scheduler._supersede_reason("CNC1", "CNC1") == scheduler._SUPERSEDED
    conflict = scheduler._supersede_reason("CNC1", "CNC4")
    assert "DIFFERENT machines" in conflict and "CNC4" in conflict


# =========================================================================== #
# M8 — one authoritative definition, not two that agree today
# =========================================================================== #

def test_the_seam_and_the_engine_share_one_definition_of_a_frozen_row():
    assert roster_adapter.row_value is scheduler.row_value
    assert roster_adapter.prev_start_key is scheduler.prev_start_key
    assert roster_adapter.pin_rank is scheduler.pin_rank


def test_both_halves_pick_the_same_row_out_of_a_pair():
    """Behavioural, not just identity: the two rank tuples used to DIFFER — the
    seam's carried the SO number, the engine's did not — so a tie the seam broke
    one way the engine could have broken the other. Both a plain earlier-start
    case and the tie the SO number exists to break."""
    early = dict(_ROW, so_no="SO2")
    late = dict(_ROW, so_no="SO1", prev_start="2026-08-11T10:00:00")
    say = []
    pins = roster_adapter._pins([late, early], {"B1": _Batch()}, _pin_masters(), say)
    assert [p["so_no"] for p in pins] == ["SO2"]
    assert scheduler.pin_rank(early, "CNC1") < scheduler.pin_rank(late, "CNC1")
    # A dead tie on start/machine/operator: the SO number decides, in both halves.
    tie_a, tie_b = dict(_ROW, so_no="SO1"), dict(_ROW, so_no="SO2")
    assert scheduler.pin_rank(tie_a, "CNC1") < scheduler.pin_rank(tie_b, "CNC1")
    pins = roster_adapter._pins([tie_b, tie_a], {"B1": _Batch()}, _pin_masters(), [])
    assert [p["so_no"] for p in pins] == ["SO1"]


# =========================================================================== #
# M4 — no machine is in two places at once
# =========================================================================== #

def _entry(batch, seq, machine, start_h, end_h, who="Narayan"):
    start = datetime(2026, 8, 12, start_h, 0)
    end = datetime(2026, 8, 12, end_h, 0)
    return ScheduleEntry(batch_id=batch, item_code="ITEM", process_seq=seq,
                         process_name=f"STEP{seq}", machine=machine, qty=10,
                         occupancy_min=(end - start).total_seconds() / 60.0,
                         start=start, end=end, so_refs=["SO1"], operator=who,
                         op_segments=[(start, end, who)])


def test_two_operations_at_the_same_time_on_one_machine_are_caught():
    """The strongest available guard on the no-segmentation guarantee, and the one
    ``segmentation_violations`` structurally cannot give."""
    entries = [_entry("B1", 1, "CNC1", 8, 12), _entry("B2", 1, "CNC1", 9, 11)]
    rows = rreport.machine_conflict_violations(entries)
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "MACHINE_DOUBLE_BOOKED"
    assert "at the same time" in rows[0]["message"]


def test_the_segmentation_check_is_blind_to_exactly_that_case():
    """Non-vacuity, argued rather than asserted: the new check is not a duplicate
    of the old one. ``segmentation_violations`` fires only when an intruder sits
    INSIDE a gap, and two fully-overlapping entries have no gap anywhere."""
    entries = [_entry("B1", 1, "CNC1", 8, 12), _entry("B2", 1, "CNC1", 9, 11)]
    assert rreport.segmentation_violations(entries) == []


def test_it_is_part_of_all_violations():
    entries = [_entry("B1", 1, "CNC1", 8, 12), _entry("B2", 1, "CNC1", 9, 11)]
    kinds = {r["kind"] for r in
             rreport.all_violations(entries, _pin_masters(), _cfg())}
    assert "MACHINE_DOUBLE_BOOKED" in kinds, kinds


def test_back_to_back_and_different_machines_are_not_flagged():
    """It must not cry wolf. Touching ends share no real time, and one operator
    handing a machine over at 12:00 is the normal case."""
    entries = [_entry("B1", 1, "CNC1", 8, 12), _entry("B2", 1, "CNC1", 12, 15),
               _entry("B3", 1, "CNC4", 8, 12)]
    assert rreport.machine_conflict_violations(entries) == []


def test_a_split_step_published_as_several_entries_is_not_a_clash():
    """The retired classic engine publishes a parallel split as several entries of
    ONE (batch, step) — a different question from another job taking the machine
    away, and the reason ``_intruder`` skips it too."""
    entries = [_entry("B1", 1, "CNC1", 8, 12), _entry("B1", 1, "CNC1", 9, 11)]
    assert rreport.machine_conflict_violations(entries) == []
