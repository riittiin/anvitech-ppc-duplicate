"""The ScheduleEntry contract five downstream surfaces depend on. Every
assertion here corresponds to a defect this repo has already paid for."""

import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from engine import cp_adapter
from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)
from engine.pipeline import RuleError

REPO_ROOT = Path(__file__).resolve().parents[1]


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.source_so_refs = [f"SO-{key}"], [f"SO-{key}"]
        self.delivery_date, self.process_remaining = due, None


def _masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5),
                  "MD1": Machine("MD1", "MD 1", "manual",
                                 available_hrs_per_day=9.5)},
        routings={"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
            Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
            Process(3, "DEBURING", 1.0, None, None, "MD1"),
            Process(4, "DISPATCH", None, None, None, None)])},
        operators=[Operator("N", "CNC1", ["CNC1"], "First shift"),
                   Operator("M", "MD1", ["MD1"], "First shift")],
        calendar=WorkCalendar())


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0, **kw)


def _run():
    return cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                          masters=_masters())


# --------------------------------------------------------------------------- #
# The entry contract
# --------------------------------------------------------------------------- #

def test_off_lane_names_match_the_consumers():
    """delay_report._OFF_LANES, analytics.NON_MACHINE_LANES and freeze._OS_LANES
    all match these LITERALLY. Anything else bills outsourcing to an in-house
    machine — the 2026-08-09 defect, where 0 of 1,648 detail rows ever named an
    OS step and 96 hours at a vendor were blamed on the next machine."""
    from engine import analytics, delay_report, freeze
    assert cp_adapter.OS_LANE in delay_report._OFF_LANES
    assert cp_adapter.OS_LANE in analytics.NON_MACHINE_LANES
    assert cp_adapter.OS_LANE in freeze._OS_LANES
    assert cp_adapter.OFF_LANE in delay_report._OFF_LANES
    lanes = {e.machine for e in _run()}
    assert cp_adapter.OS_LANE in lanes
    assert cp_adapter.OFF_LANE in lanes          # the DISPATCH milestone


def test_every_machine_entry_names_an_operator():
    """engine/freeze.py pins machine AND operator. An empty name freezes a
    ghost."""
    seen = 0
    for entry in _run():
        if entry.machine in (cp_adapter.OS_LANE, cp_adapter.OFF_LANE):
            continue
        seen += 1
        assert entry.operator, entry
        assert entry.op_segments, entry
    assert seen == 2                             # CNC FIRST SIDE + DEBURING


def test_op_segments_is_a_sorted_list_of_start_end_operator_tuples():
    """A LIST, not a tuple: rule6_allocate's operator-balance pass assigns into
    it by index and a tuple raises there. Five surfaces read this shape."""
    for entry in _run():
        assert isinstance(entry.op_segments, list)
        for segment in entry.op_segments:
            assert len(segment) == 3
            start, end, who = segment
            assert isinstance(start, datetime) and isinstance(end, datetime)
            assert isinstance(who, str)
        starts = [s for s, _e, _w in entry.op_segments]
        assert starts == sorted(starts)


def test_a_dispatch_milestone_waits_for_every_other_step():
    entries = {e.process_seq: e for e in _run()}
    assert entries[4].end >= max(e.end for e in _run() if e.process_seq != 4)


def test_the_adapter_never_reconsolidates():
    """Rule 1 already clubbed the SO lines. Two batches in, two batches out."""
    entries = cp_adapter.run([_B("B1", "A", 10), _B("B2", "A", 10)],
                             config=_cfg(), masters=_masters())
    assert {e.batch_id for e in entries} == {"B1", "B2"}


def test_an_outsourced_block_names_nobody_and_books_no_machine_time():
    """An OS step is at a vendor: no machine, no operator, no segments — which
    is exactly how delay_report tells 'at a vendor' from 'waiting for crew'."""
    os_entry = next(e for e in _run() if e.machine == cp_adapter.OS_LANE)
    assert os_entry.operator == ""
    assert os_entry.op_segments == []
    assert (os_entry.end - os_entry.start).total_seconds() / 60 == 2880


def test_entries_come_back_in_start_order():
    """The Schedule tab and the Rule 6 CSV render this list AS GIVEN, and two
    runs of one book must produce the same file — the same order the sibling
    seam publishes, so the two engines' output can be diffed line for line."""
    entries = cp_adapter.run([_B("B2", "A", 10), _B("B1", "A", 10)],
                             config=_cfg(), masters=_masters())
    assert [(e.start, e.batch_id, e.process_seq) for e in entries] == sorted(
        (e.start, e.batch_id, e.process_seq) for e in entries)


def test_an_item_with_no_routing_is_reported_not_silently_dropped():
    notes = []
    entries = cp_adapter.run([_B("B1", "A", 10), _B("B2", "GHOST", 5)],
                             config=_cfg(), masters=_masters(), notes=notes)
    assert {e.batch_id for e in entries} == {"B1"}
    assert any("GHOST" in note for note in notes), notes


def test_an_order_the_engine_could_not_place_is_named_not_silently_dropped():
    """An order in no plan at all — no bar on the Gantt, no row in the delay
    report — is this repo's most expensive recurring defect class (2026-08-11).
    One unstaffable machine must not cost every OTHER order its plan either."""
    masters = _masters()
    masters.machines["MW9"] = Machine("MW9", "MW 9", "manual",
                                      available_hrs_per_day=9.5)
    masters.routings["C"] = Routing("C", "c", "cust", "rm", None, [
        Process(1, "WELDING", 5.0, None, None, "MW9")])       # nobody runs MW9
    notes = []
    entries = cp_adapter.run([_B("B1", "A", 10), _B("B2", "C", 10)],
                             config=_cfg(), masters=masters, notes=notes)
    assert {e.batch_id for e in entries} == {"B1"}
    assert any("B2" in note and "MW9" in note for note in notes), notes


def test_a_book_nothing_can_be_placed_in_fails_as_a_LOCALIZED_rule_error():
    """``pipeline.run_rule`` catches ``RuleError`` and nothing else. A raw
    ``RuntimeError`` unwinds ``run_forward``, discards the trace Rules 1-3 filled
    in, blanks every per-rule tab and returns a 500 with nothing to show."""
    masters = _masters()
    masters.machines["MW9"] = Machine("MW9", "MW 9", "manual",
                                      available_hrs_per_day=9.5)
    masters.routings["C"] = Routing("C", "c", "cust", "rm", None, [
        Process(1, "WELDING", 5.0, None, None, "MW9")])
    with pytest.raises(RuleError) as err:
        cp_adapter.run([_B("B2", "C", 10)], config=_cfg(), masters=masters)
    assert err.value.rule == "rule6"


# --------------------------------------------------------------------------- #
# The plan clock — ONE definition, shared by the solve and the replay
# --------------------------------------------------------------------------- #

def test_the_plan_starts_at_the_first_shift_hour_of_the_plan_start_date():
    """The genome's shift indices are counted from this moment, so the solve and
    the replay must derive it identically — and midnight is not it (2026-08-09:
    607 hours were charged to a plan that had not begun)."""
    assert min(e.start for e in _run()) == datetime(2026, 8, 12, 8, 0)


def test_the_stored_plan_clock_floor_wins_when_it_is_later():
    """``plan_start_floor`` is the app's STORED plan clock (2026-08-07): an ISO
    string, and later than 08:00 whenever the day is already under way. Ignoring
    it plans work into hours that have already passed."""
    cfg = _cfg(plan_start_floor="2026-08-12T14:00:00")
    entries = cp_adapter.run([_B("B1", "A", 10)], config=cfg,
                             masters=_masters())
    assert min(e.start for e in entries) == datetime(2026, 8, 12, 14, 0)


# --------------------------------------------------------------------------- #
# The genome — run() REPLAYS, it never decides
# --------------------------------------------------------------------------- #

def _two_machine_masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5),
                  "CNC2": Machine("CNC2", "CNC 2", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        routings={"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1/CNC2")])},
        operators=[Operator("N", "CNC1/CNC2", ["CNC1", "CNC2"],
                            "First shift")],
        calendar=WorkCalendar())


def test_the_free_replay_picks_the_first_machine_option():
    """NON-VACUITY for the two tests below: with no genome and no pin, this book
    lands on CNC1. Both of them assert something else, so both discriminate."""
    entries = cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                             masters=_two_machine_masters())
    assert [e.machine for e in entries] == ["CNC1"]


def test_run_replays_the_genome_rather_than_deciding_for_itself():
    entries = cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                             masters=_two_machine_masters(),
                             genome={"cp_machine_of": {("B1", 1): "CNC2"}})
    assert [e.machine for e in entries] == ["CNC2"]


def test_a_json_shaped_genome_is_read_back_before_it_is_replayed():
    """The tuple-keyed maps cross JSON twice — the store and the cloud worker's
    payload. A ``"B1\\x1f1"`` key would miss every lookup against the real tuple
    and the replay would fall back for EVERY operation while the genome still
    looked well-formed (right key count, right value types, no exception)."""
    from cp_engine import genome as cp_genome
    flat = cp_genome.to_json({"cp_machine_of": {("B1", 1): "CNC2"}})
    entries = cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                             masters=_two_machine_masters(), genome=flat)
    assert [e.machine for e in entries] == ["CNC2"]


def test_an_order_the_genome_never_saw_is_flagged_but_still_planned():
    """It IS laid out — an order in no plan is the worse failure — but the note
    is what says the published plan is partly not the one that was searched."""
    notes = []
    entries = cp_adapter.run([_B("B1", "A", 10), _B("B2", "A", 10)],
                             config=_cfg(), masters=_two_machine_masters(),
                             notes=notes,
                             genome={"cp_machine_of": {("B1", 1): "CNC2"}})
    assert {e.batch_id for e in entries} == {"B1", "B2"}
    assert any("B2" in note and "searched plan" in note for note in notes), notes


def test_a_book_with_no_genome_at_all_is_not_flagged_as_unsearched():
    """Before the first search EVERY order is uncovered. A banner on every plan
    of a fresh book teaches the directors to ignore the one that is real."""
    notes = []
    cp_adapter.run([_B("B1", "A", 10), _B("B2", "A", 10)], config=_cfg(),
                   masters=_two_machine_masters(), notes=notes)
    assert not [note for note in notes if "searched plan" in note], notes


def test_the_genome_rides_on_the_config_the_way_the_app_hands_it_over():
    """``/run`` has no genome argument — Task 12 attaches it to the resolved
    config, exactly as the roster engine's crew genome rides there."""
    cfg = _cfg()
    object.__setattr__(cfg, "cp_genome", {"cp_machine_of": {("B1", 1): "CNC2"}})
    entries = cp_adapter.run([_B("B1", "A", 10)], config=cfg,
                             masters=_two_machine_masters())
    assert [e.machine for e in entries] == ["CNC2"]


# --------------------------------------------------------------------------- #
# Frozen work: SO LINES in, BATCH operations out
# --------------------------------------------------------------------------- #

def test_the_seam_reuses_the_one_frozen_row_translation():
    """ONE definition of how a per-SO-line frozen row becomes a batch pin. A
    second copy is the defect class this repo keeps paying for."""
    from engine import roster_adapter
    assert cp_adapter._pins is roster_adapter._pins


def test_a_frozen_row_keyed_BY_SO_LINE_pins_the_BATCH_operation():
    """``engine.freeze.compute_frozen_set`` emits ``(so_no, item_code)`` rows;
    a job key is a BATCH id, because Rule 1 clubs lines. Handed over raw, every
    row is dropped and the plan looks perfectly well-formed while the part is
    planned on a machine it is not physically on, paying a 90-minute setup it
    does not owe."""
    entries = cp_adapter.run(
        [_B("B1", "A", 10)], config=_cfg(), masters=_two_machine_masters(),
        genome={"cp_machine_of": {("B1", 1): "CNC1"}},
        frozen=[{"so_no": "SO-B1", "item_code": "A", "process": "CNC FIRST SIDE",
                 "op_seq": 1, "machine": "CNC2", "operator": "N",
                 "remaining_qty": 3, "prev_start": datetime(2026, 8, 12, 8, 0)}])
    assert [e.machine for e in entries] == ["CNC2"]     # the pin beats the genome


def test_a_frozen_row_pins_WHERE_never_HOW_MUCH():
    """The 2026-08-11 director escalation: ``remaining_qty`` is ONE SO line's
    remainder while the operation belongs to the whole clubbed batch. Taking it
    put 281 pieces of a real order in no plan at all."""
    entries = cp_adapter.run(
        [_B("B1", "A", 10)], config=_cfg(), masters=_two_machine_masters(),
        frozen=[{"so_no": "SO-B1", "item_code": "A", "op_seq": 1,
                 "machine": "CNC2", "operator": "N", "remaining_qty": 3,
                 "prev_start": datetime(2026, 8, 12, 8, 0)}])
    assert [e.qty for e in entries] == [10]


def test_a_frozen_row_this_plan_cannot_match_is_reported():
    notes = []
    cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                   masters=_two_machine_masters(), notes=notes,
                   frozen=[{"so_no": "SO-GONE", "item_code": "A", "op_seq": 1,
                            "machine": "CNC2", "operator": "N"}])
    assert any("SO-GONE" in note for note in notes), notes


# --------------------------------------------------------------------------- #
# The validation banner: breaches and measurements are not the same thing
# --------------------------------------------------------------------------- #

def _E(batch, seq, machine, start, end, who):
    from engine.models import ScheduleEntry
    return ScheduleEntry(batch_id=batch, item_code="A", process_seq=seq,
                         process_name=f"step {seq}", machine=machine, qty=10,
                         occupancy_min=(end - start).total_seconds() / 60,
                         start=start, end=end, so_refs=[f"SO-{batch}"],
                         operator=who, op_segments=[(start, end, who)])


def _mixed_entries():
    """A plan carrying ONE of each: a real rule BREACH (CNC1 running two
    operations at the same instant) and a capacity MEASUREMENT (MD1 dark on
    14-08 with work ready since 12-08 and M free). Hand-built rather than
    decoded, because this engine's whole job is to produce neither — a fixture
    that waited for the decoder to emit an idle-capacity row would be waiting on
    a defect."""
    return [
        _E("B1", 1, "CNC1", datetime(2026, 8, 12, 8, 0),
           datetime(2026, 8, 12, 10, 0), "N"),
        _E("B2", 1, "CNC1", datetime(2026, 8, 12, 9, 0),
           datetime(2026, 8, 12, 11, 0), "N"),
        _E("B1", 2, "MD1", datetime(2026, 8, 15, 8, 30),
           datetime(2026, 8, 15, 9, 0), "M"),
    ]


def test_plan_violations_partitions_breaches_from_capacity_measurements():
    """``report.all_violations`` returns rule BREACHES and capacity
    MEASUREMENTS in one list, told apart only by the ``breach`` key. Hand the
    whole list to the directors' banner and an IDLE_CAPACITY note sits beside a
    real rule breach and buries the row that matters."""
    from cp_engine import report
    masters, entries = _masters(), _mixed_entries()
    breaches, measured = cp_adapter.plan_violations(entries, masters, _cfg())
    assert "MACHINE_DOUBLE_BOOKED" in {r["kind"] for r in breaches}
    assert {r["kind"] for r in measured} == {"IDLE_CAPACITY"}
    assert [r for r in breaches if not r.get("breach")] == []
    assert [r for r in measured if r.get("breach")] == []
    # Nothing is lost between the two lists.
    assert len(breaches) + len(measured) == len(
        report.all_violations(entries, masters, _cfg()))


def test_plan_violations_partitions_a_clean_plan_to_nothing():
    breaches, measured = cp_adapter.plan_violations(_run(), _masters(), _cfg())
    assert (breaches, measured) == ([], [])


def test_a_row_with_no_breach_key_at_all_surfaces_rather_than_hides():
    """A row nobody recognised that SURFACES is a nuisance; one silently filed
    under 'measurements' is a rule breach the directors never see."""
    from cp_engine import report
    row = {"kind": "SOMETHING_NEW", "ref": "x", "message": "m"}
    original = report.all_violations
    try:
        report.all_violations = lambda *a, **kw: [dict(row)]
        breaches, measured = cp_adapter.plan_violations([1], _masters(), _cfg())
    finally:
        report.all_violations = original
    assert [r["kind"] for r in breaches] == ["SOMETHING_NEW"]
    assert measured == []


def test_plan_violations_passes_absences_through_so_leave_is_not_spare_capacity():
    """``IDLE_CAPACITY`` without leave data accuses the plan of wasting a
    machine nobody could have run."""
    masters, entries = _masters(), _mixed_entries()
    reserved = {"M": [(datetime(2026, 8, 14), datetime(2026, 8, 15))]}
    _b, blind = cp_adapter.plan_violations(entries, masters, _cfg())
    _b, with_leave = cp_adapter.plan_violations(entries, masters, _cfg(),
                                                reserved=reserved)
    assert blind, "the fixture no longer measures any idle capacity"
    assert with_leave == []


# --------------------------------------------------------------------------- #
# The two entry points are not the same thing
# --------------------------------------------------------------------------- #

def test_the_replay_never_imports_a_solver():
    """THE test of this task. Render has neither pyjobshop nor ortools and never
    will; ``/run`` imports this seam on boot and calls ``run()`` on every page
    load. A solver import at module level — or lazily, from anywhere the replay
    reaches — 500s the live site on boot, and only there: CI has pyjobshop
    installed, so every other test in this file passes either way."""
    probe = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('pyjobshop', 'ortools'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "from datetime import date\n"
        "from engine import cp_adapter\n"
        "from tests.test_cp_entries_contract import _B, _cfg, _masters\n"
        "entries = cp_adapter.run([_B('B1', 'A', 10)], config=_cfg(),\n"
        "                         masters=_masters())\n"
        "assert entries\n"
        "assert 'pyjobshop' not in sys.modules and 'ortools' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def _so_lines():
    """Real ``SOLine``s — ``solve()`` takes the BOOK and runs Rule 1 itself, so
    a batch double would never exercise the path the app uses."""
    from engine.models import SOLine
    return [SOLine("SO-1", "A", "a", 10, date(2026, 9, 1)),
            SOLine("SO-2", "A", "a", 8, date(2026, 9, 20))]


def test_solve_produces_a_genome_the_replay_reproduces_exactly():
    """The seam's whole promise: one plan clock, one book, two halves. If
    ``solve()`` and ``run()`` disagreed about the plan start by so much as an
    hour, every shift index in the roster would be off by one and the replay
    would silently be a different plan."""
    pytest.importorskip("pyjobshop")
    from cp_engine import report
    from engine.rules import rule1_consolidate
    masters, cfg, so_lines = _masters(), _cfg(), _so_lines()
    result = cp_adapter.solve(so_lines, cfg, masters, budget_evals=0, seed=42)
    assert result.genome
    assert result.ranks and all("\x1f" in key for key in result.ranks)
    batches = rule1_consolidate.run(so_lines, config=cfg, masters=masters)
    entries = cp_adapter.run(batches, config=cfg, masters=masters,
                             genome=result.genome)
    assert entries
    assert report.completion_drift(entries, result.genome) == []


def test_solve_publishes_the_metrics_of_the_plan_the_app_will_actually_build():
    """The panel's number must be measured on the REPLAY, not on the solve. The
    model and the decoder are two definitions of one schedule, so a metric taken
    anywhere else promises a plan the apply does not reproduce.

    ``cp_num_workers=1`` is REQUIRED here, and the reason is a finding rather
    than housekeeping (2026-08-14, Task 13). The shipping default is 4 because
    cores are the only measured lever on the proven bound — but a parallel search
    returns whichever tied optimum a worker reaches first, and **cp's objective
    contains no makespan term at all** (tardiness, then squared spread; unlike
    ``optimizer.score``'s 0.1 makespan tie-break). On this two-line book every
    schedule is on time, so the model is INDIFFERENT: 15 solves at 4 workers
    returned makespans of 2.46, 2.47 and **39.33** days, all equally optimal by
    its own objective. The non-vacuity assertion below compares one particular
    genome's makespan against the genome-less replay's, so it needs a
    deterministic solve; the flake it caused is what surfaced the finding.
    """
    pytest.importorskip("pyjobshop")
    from engine.optimizer import plan_metrics
    masters, so_lines = _masters(), _so_lines()
    cfg = _cfg(cp_num_workers=1)
    result = cp_adapter.solve(so_lines, cfg, masters, budget_evals=0, seed=42)
    batches = _batches()

    def _measure(genome):
        return plan_metrics(
            cp_adapter.run(batches, config=cfg, masters=masters, genome=genome),
            so_lines, date(2026, 8, 12), with_distribution=True)

    assert result.best["makespan_days"] == _measure(result.genome)["makespan_days"]
    # Non-vacuous: this book really does replay differently without the genome,
    # so the assertion above is about the genome and not about arithmetic.
    assert _measure(None)["makespan_days"] != _measure(result.genome)["makespan_days"]


def test_solve_hands_the_solver_the_measured_defaults_and_the_shared_clock():
    """Pinned because each is a decision somebody measured, and none of them
    would show up as a failure — a solve under the wrong encoding, the wrong
    budget or a different plan clock still returns a perfectly valid plan."""
    pytest.importorskip("pyjobshop")
    from cp_engine import solve as cp_solve
    seen = {}
    original = cp_solve.solve_book
    try:
        cp_solve.solve_book = lambda *a, **kw: seen.update(args=a, kw=kw) or _stub()
        cp_adapter.solve(_so_lines(), _cfg(), _masters(), seed=7,
                         frozen=[{"so_no": "SO-1", "item_code": "A",
                                  "op_seq": 1, "machine": "CNC1",
                                  "operator": "N"}])
    finally:
        cp_solve.solve_book = original
    # E1: measured 2026-08-14 (tractability findings, decision 2). solve_book's
    # OWN default is True, so a seam that omitted this would ship E2 silently.
    assert seen["kw"]["hold_across_unmanned_shift"] is False
    assert seen["kw"]["time_limit"] == cp_adapter.CP_TIME_LIMIT_SEC
    assert seen["kw"]["horizon_days"] == cp_adapter.CP_HORIZON_DAYS
    assert seen["kw"]["seed"] == 7
    # The frozen row arrived TRANSLATED — batch-keyed, no quantity.
    pin = seen["kw"]["frozen"][0]
    assert pin["order_key"] in {b.batch_id for b in _batches()}
    assert "remaining_qty" not in pin
    # The one plan clock both halves count their shifts from.
    assert seen["args"][3] == datetime(2026, 8, 12, 8, 0)


def _batches():
    from engine.rules import rule1_consolidate
    return rule1_consolidate.run(_so_lines(), config=_cfg(), masters=_masters())


def _stub():
    from cp_engine.solve import Solved
    return Solved(False, "INFEASIBLE", {}, None, None, None, {}, [], {})


def test_sweep_optimize_wraps_one_solve_and_never_sweeps_the_overlap():
    """Overlap is a MODEL VARIABLE under this engine — the solver picks it per
    job. Sweeping it outside would re-solve the same book N times for one
    answer, at N times the worker's wall clock."""
    pytest.importorskip("pyjobshop")
    swept = cp_adapter.sweep_optimize(_so_lines(), _cfg(), _masters(),
                                      budget_evals=0, seed=42)
    assert len(swept.table) == 1
    assert swept.overlap_percent == _cfg().overlap_percent
    assert swept.result.genome
