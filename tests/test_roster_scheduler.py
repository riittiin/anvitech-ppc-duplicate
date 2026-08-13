"""The shift clock — one operator, one machine, one uninterrupted operation.

The first twelve tests are the brief's. Everything under "Brief corrections"
pins a defect found in the brief's skeleton while implementing it; each of those
was RED against the sketch as written and is written so that restoring the
sketch's line makes it RED again.
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


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = None


def _masters(processes, operators, machines=None):
    machines = machines or {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    # engine.models.Routing is (item_code, description, customer, rm_type, moq,
    # processes) — the brief's three-argument call put the process LIST in the
    # `customer` slot and left `processes` empty, so every job would have had no
    # routing at all.
    return Masters(machines=machines,
                   routings={"ITEM": Routing("ITEM", "d", "", "", None, processes)},
                   operators=operators, calendar=WorkCalendar())


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0, **kw)


def _run(masters, batches, overlap=1.0, crew_rank=None):
    jobs, _by_key, _skipped = build_jobs(batches, masters)
    shop = build_shop(masters)
    return scheduler.schedule(jobs, [j.key for j in jobs], shop, _cfg(),
                              overlap=overlap, crew_rank=crew_rank or {})


# --------------------------------------------------------------------------- #
# The brief's twelve
# --------------------------------------------------------------------------- #

def test_an_operation_is_never_interrupted_by_another_job():
    """No segmentation: once CNC1 starts B1 it finishes B1 before touching B2."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 60), _B("B2", "ITEM", 60)])
    by_job = {p.job_key: p for p in plan.placements if p.machine == "CNC1"}
    assert by_job["B1"].end <= by_job["B2"].start


def test_one_operator_is_never_on_two_machines_in_a_shift():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")],
        [Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 20), _B("B2", "ITEM", 20)])
    used = {p.machine for p in plan.placements if p.machine}
    assert used == {"CNC1"}          # one person -> one machine, so CNC4 stays dark


def test_a_job_crossing_1900_is_handed_to_the_next_shift_operator():
    """The owner's 5 p.m. example: start at 17:00, 90 min setup, work spills past
    19:00, and the night-shift operator continues the SAME operation."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC1", ["CNC1"], "Second shift")])
    plan = _run(masters, [_B("B1", "ITEM", 60)])
    seg = [p for p in plan.placements if p.machine == "CNC1"][0].segments
    assert [s[2] for s in seg] == ["Narayan", "Sidhu"]
    assert seg[0][1] == seg[1][0]        # contiguous — no gap, no second setup


def test_setup_is_charged_once_per_operation_not_per_shift():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC1", ["CNC1"], "Second shift")])
    plan = _run(masters, [_B("B1", "ITEM", 60)])
    p = [x for x in plan.placements if x.machine == "CNC1"][0]
    assert p.work_min == 90.0 + 60 * 10.0


def test_no_new_setup_when_the_same_item_and_process_runs_back_to_back():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 10), _B("B2", "ITEM", 10)])
    work = {p.job_key: p.work_min for p in plan.placements if p.machine == "CNC1"}
    assert work["B1"] == 90.0 + 100.0
    assert work["B2"] == 100.0           # fixture already on -> no second setup


def test_a_manual_step_is_charged_no_setup():
    masters = _masters(
        [Process(1, "DEBURING", 2.0, None, None, "MD1")],
        [Operator("Anturam", "MD1", ["MD1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 30)])
    assert [p.work_min for p in plan.placements if p.machine == "MD1"] == [60.0]


def test_overlap_releases_the_successor_after_whole_pieces():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 10.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 10)], overlap=0.8)
    first = [p for p in plan.placements if p.op_seq == 1][0]
    second = [p for p in plan.placements if p.op_seq == 2][0]
    # 8 of 10 pieces cleared: 90 setup + 8 x 10 = 170 worked minutes in.
    assert second.start >= first.start
    assert second.start < first.end          # genuinely overlapped
    assert first.end > second.start
    assert second.start == datetime(2026, 8, 12, 10, 50)


def test_a_successor_never_starts_before_its_predecessor():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 1.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 40)], overlap=0.8)
    first = [p for p in plan.placements if p.op_seq == 1][0]
    second = [p for p in plan.placements if p.op_seq == 2][0]
    assert second.start > first.start
    assert second.end >= first.end           # pacing: never finishes first


def test_os_is_a_flat_lead_time_that_needs_no_machine_or_operator():
    masters = _masters(
        [Process(1, "BAND SAW OS", 2880.0, None, None, "OS"),
         Process(2, "DEBURING", 2.0, None, None, "MD1")],
        [Operator("Anturam", "MD1", ["MD1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 5)])
    os_p = [p for p in plan.placements if p.op_seq == 1][0]
    deb = [p for p in plan.placements if p.op_seq == 2][0]
    assert os_p.machine is None and os_p.segments == ()
    assert (os_p.end - os_p.start).total_seconds() / 60.0 == 2880.0
    assert deb.start >= os_p.end             # fully sequential out of OS


def test_dispatch_waits_for_every_process_of_the_batch():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 1.0, None, None, "CNC4"),
         Process(3, "DISPATCH", None, None, None, None)],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 20)], overlap=0.5)
    latest = max(p.end for p in plan.placements if p.op_seq in (1, 2))
    dispatch = [p for p in plan.placements if p.op_seq == 3][0]
    assert dispatch.start == dispatch.end == latest
    assert plan.completion["B1"] == latest


def test_a_machine_with_no_rostered_operator_holds_its_job_rather_than_dropping_it():
    """Nobody on the night shift -> the part stays in the chuck and resumes in the
    morning. Honest, and it must not be segmented onto another machine."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 100)])
    p = [x for x in plan.placements if x.machine == "CNC1"][0]
    assert {s[2] for s in p.segments} == {"Narayan"}
    assert p.work_min == 90.0 + 1000.0
    assert p.end.date() > p.start.date()     # spans days


def test_the_plan_is_deterministic():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")],
        [Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
         Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")])
    batches = [_B("B1", "ITEM", 30), _B("B2", "ITEM", 30), _B("B3", "ITEM", 30)]
    runs = [_run(masters, batches) for _ in range(5)]
    signature = [[(p.job_key, p.op_seq, p.machine, p.start, p.end)
                  for p in sorted(r.placements, key=lambda x: (x.job_key, x.op_seq))]
                 for r in runs]
    assert all(s == signature[0] for s in signature)


# --------------------------------------------------------------------------- #
# Brief corrections — each of these was RED against the brief's skeleton
# --------------------------------------------------------------------------- #

_HASH_SEED_SCRIPT = textwrap.dedent("""
    from datetime import date

    from engine.config import Config
    from engine.models import (Machine, Masters, Operator, Process, Routing,
                               WorkCalendar)
    from roster_engine import scheduler
    from roster_engine.domain import build_jobs, build_shop


    class B:
        def __init__(self, key, qty):
            self.batch_id, self.item_code, self.qty = key, "ITEM", qty
            self.so_refs, self.delivery_date = [key], date(2026, 12, 1)
            self.process_remaining = None


    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    processes = [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4"),
                 Process(2, "DEBURING", 2.0, None, None, "MD1")]
    operators = [Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                 Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                 Operator("Anturam", "MD1", ["MD1"], "First shift")]
    masters = Masters(machines=machines,
                      routings={"ITEM": Routing("ITEM", "d", "", "", None, processes)},
                      operators=operators, calendar=WorkCalendar())
    batches = [B("B1", 30), B("B2", 30), B("B3", 30)]
    jobs, _bk, _sk = build_jobs(batches, masters)
    plan = scheduler.schedule(
        jobs, [j.key for j in jobs], build_shop(masters),
        Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0), overlap=0.8)
    for p in sorted(plan.placements, key=lambda x: (x.job_key, x.op_seq)):
        print(p.job_key, p.op_seq, p.machine, p.start, p.end, p.segments)
""")


def test_the_plan_is_deterministic_across_hash_seeds():
    """The in-process determinism test above cannot see the real hazard: within
    ONE interpreter a set/frozenset/dict iterates the same way every time, so an
    unsorted iteration over `shop.machining_ids` (a frozenset) or over a `demand`
    dict passes it trivially. Only a fresh interpreter with a different string
    hash seed re-orders those, which is what actually varies between the
    optimizer's worker processes."""
    outputs = set()
    for seed in ("0", "1", "2", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run([sys.executable, "-c", _HASH_SEED_SCRIPT],
                                cwd=REPO_ROOT, env=env, capture_output=True,
                                text=True, check=True)
        outputs.add(result.stdout)
    assert len(outputs) == 1, f"plan varied across PYTHONHASHSEED: {outputs}"


def test_two_machines_never_run_the_same_operation():
    """The brief's `_next_job` filters on ready/qty/machine options only — nothing
    records that a machine has ALREADY claimed that job's current operation, so
    two alternative machines both pick it up and the batch is built twice.

    The fixture has to be an operation that does NOT finish inside one window.
    The earlier two-short-jobs version of this test was VACUOUS (2026-08-12
    review): both jobs completed inside a single `_work` call, so the job was
    already advanced past that operation before a second machine ever looked at
    it, and the test passed with the claim guard deleted. Here 100 pieces x 10
    min + 90 setup = 1090 minutes against a 660-minute shift, so CNC1 is still
    holding the part when CNC4 asks for work in the same shift. Without the claim
    CNC4 takes it too, both machines run it to the end, and the second one to
    finish walks off the end of the routing — `IndexError: tuple index out of
    range` in `_work`.
    """
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 100)])
    assert [(p.job_key, p.op_seq, p.machine) for p in plan.placements] == [
        ("B1", 1, "CNC1")]
    placed = plan.placements[0]
    assert placed.work_min == 90.0 + 1000.0
    # Genuinely spans the window end — that is what makes the guard load-bearing.
    assert placed.end.date() > placed.start.date()


def test_release_is_measured_in_worked_minutes_not_wall_clock():
    """The brief releases the successor at `js.started + need` — ELAPSED time from
    the operation's start. This operation runs 660 minutes on Wednesday, is HELD
    overnight (nobody on the night shift) and Thursday (weekly off), and resumes
    Friday. Its 80th piece is cut 230 worked minutes into Friday, i.e. 11:50 —
    the wall clock reaches `need` at 22:50 on Wednesday, in the middle of a dark
    shop, and would release pieces that were never cut."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 1.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 100)], overlap=0.8)
    first = [p for p in plan.placements if p.op_seq == 1][0]
    second = [p for p in plan.placements if p.op_seq == 2][0]
    assert first.work_min == 90.0 + 1000.0
    assert second.start == datetime(2026, 8, 14, 11, 50)
    assert second.end >= first.end


def test_overlap_still_happens_when_the_setup_was_skipped():
    """`release.work_min_before_release` charges a MACHINING step the NOMINAL
    setup unconditionally, but `_release_moment` walks the operation's real
    segments — which contain no setup when the machine was already set up for
    this (item, process). `need` then exceeds every minute the machine worked,
    the walk clamps to the last segment's end, and `released_at == end`: overlap
    silently switches itself off, fully sequential, at every setting.

    This fires on exactly the repeat-item case the setup-skip exists for, so it
    is systematic. Here B1 charges its setup and overlaps correctly at 10:50;
    B2 runs the same (item, process) straight after with NO setup, so its 8th of
    10 pieces is cut 80 worked minutes in — 12:30, not 12:50.
    """
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        "CNC5": Machine("CNC5", "CNC 5", "CNC lathe", available_hrs_per_day=19.5),
    }
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 10.0, None, None, "CNC4/CNC5")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift"),
         Operator("Ravi", "CNC5", ["CNC5"], "First shift")],
        machines=machines)
    plan = _run(masters, [_B("B1", "ITEM", 10), _B("B2", "ITEM", 10)],
                overlap=0.8)
    at = {(p.job_key, p.op_seq): p for p in plan.placements}
    # B1 charges the setup: 90 + 8 x 10 = 170 worked minutes from 08:00.
    assert at[("B1", 1)].work_min == 90.0 + 100.0
    assert at[("B1", 2)].start == datetime(2026, 8, 12, 10, 50)
    # B2 is the same fixture on the same machine — no second setup, so its
    # release is 8 x 10 = 80 worked minutes from 11:10.
    assert at[("B2", 1)].work_min == 100.0
    assert at[("B2", 1)].start == datetime(2026, 8, 12, 11, 10)
    assert at[("B2", 2)].start == datetime(2026, 8, 12, 12, 30)
    assert at[("B2", 2)].start < at[("B2", 1)].end        # genuinely overlapped


def test_a_downstream_machine_is_manned_for_work_released_mid_shift():
    """`_demand` in the brief counts only each job's CURRENT operation, so the
    machine that runs the SUCCESSOR has zero demand at the shift boundary, is
    left dark by the roster, and the overlap it was rostered to exploit cannot
    happen. Both operations here belong to the same shift."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 10.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 10)], overlap=0.8)
    second = [p for p in plan.placements if p.op_seq == 2][0]
    assert second.start.date() == date(2026, 8, 12)     # same day, not next shift
    assert second.segments[0][2] == "Sidhu"


def test_a_machine_waits_for_work_arriving_later_in_the_same_shift():
    """The brief's `_run_machine` returns the moment `_next_job` finds nothing
    startable at `now`, so a machine whose only work is released at 10:50 goes
    home at 08:00 and the whole shift is lost."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 10.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 10)], overlap=0.8)
    second = [p for p in plan.placements if p.op_seq == 2][0]
    assert second.start == datetime(2026, 8, 12, 10, 50)
    assert second.end == datetime(2026, 8, 12, 14, 0)


def test_a_carried_over_machine_always_reports_positive_demand():
    """Task 4 review, carried forward: the roster hands an `in_progress` machine a
    dominating CARRY_BONUS. Reporting zero demand for it therefore mans an empty
    chuck at the expense of a machine with real work, so the two must agree —
    every machine reported as in_progress carries at least the minutes still left
    on the part sitting in it. Driven through the helper because the invariant has
    to hold for ANY job state, including ones the main loop would not produce."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    jobs, _bk, _sk = build_jobs([_B("B1", "ITEM", 60)], masters)
    shop = build_shop(masters)
    window = next(iter(scheduler.iter_shifts(datetime(2026, 8, 12, 8, 0),
                                             shop.calendar, _cfg())))
    state = {"B1": scheduler._JobState(jobs[0], datetime(2026, 8, 12, 8, 0))}
    # A job whose next chance is well past this window contributes no demand at
    # all on its own — only the part in the chuck can put CNC1 on the board.
    state["B1"].ready = datetime(2026, 9, 1, 8, 0)
    state["B1"].on_machine = "CNC1"
    ms = scheduler._MachineState()
    ms.job_key, ms.op_seq, ms.remaining = "B1", 1, 240.0
    demand, ready, in_progress = scheduler._shift_demand(
        ["B1"], state, {"CNC1": ms}, window, 1.0, 90.0)
    assert in_progress == {"CNC1": "B1"}
    assert demand.get("CNC1", 0.0) >= 240.0
    # A part in the chuck is standing at that machine, not merely projected to
    # arrive there — the bench reservation reads `ready`, so the two maps must
    # agree about a carry-over exactly as `demand` and `in_progress` do.
    assert ready.get("CNC1", 0.0) >= 240.0


def test_frozen_none_empty_and_absent_behave_identically():
    """`_apply_frozen` is Task 6's; until then the hook must not change a plan."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    jobs, _bk, _sk = build_jobs([_B("B1", "ITEM", 60), _B("B2", "ITEM", 20)],
                                masters)
    shop, seq = build_shop(masters), [j.key for j in jobs]

    def sig(**kw):
        plan = scheduler.schedule(jobs, seq, shop, _cfg(), **kw)
        return ([(p.job_key, p.op_seq, p.machine, p.start, p.end, p.segments)
                 for p in plan.placements], dict(plan.completion))

    assert sig() == sig(frozen=None) == sig(frozen=[])


def test_an_operation_is_not_interrupted_even_when_pacing_extends_it():
    """Pacing stretches a starved successor's END so it never finishes before its
    predecessor delivered the last piece. The brief applies that as a post-process
    over already-published placements, which silently overlaps whatever the
    machine was given in the meantime — a hole in exactly the invariant this
    engine exists to make structural.

    Two batches feed one deburring bench. Each CNC step (290 min) releases its
    successor after 110 worked minutes at overlap 0.1, i.e. 09:50, but does not
    FINISH until 12:50 — so each 10-minute deburring step is honestly occupied
    until 12:50. Post-hoc pacing hands MD1 the second batch at 10:00 and then
    stretches both bars to 12:50, double-booking one operator on one bench for
    nearly three hours.
    """
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4"),
         Process(2, "DEBURING", 0.5, None, None, "MD1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift"),
         Operator("Anturam", "MD1", ["MD1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 20), _B("B2", "ITEM", 20)],
                overlap=0.1)
    on_md1 = sorted((p for p in plan.placements if p.machine == "MD1"),
                    key=lambda p: p.start)
    assert len(on_md1) == 2
    for earlier, later in zip(on_md1, on_md1[1:]):
        assert earlier.end <= later.start, "MD1 double-booked by a paced end"
    for key in ("B1", "B2"):
        first = [p for p in plan.placements
                 if p.job_key == key and p.op_seq == 1][0]
        second = [p for p in plan.placements
                  if p.job_key == key and p.op_seq == 2][0]
        assert second.end >= first.end
        assert second.work_min == 10.0     # the paced tail is not worked time


def test_a_job_the_sequence_forgot_is_still_planned():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    jobs, _bk, _sk = build_jobs([_B("B1", "ITEM", 10), _B("B2", "ITEM", 10)],
                                masters)
    plan = scheduler.schedule(jobs, ["B2", "B2", "nope"], build_shop(masters),
                              _cfg())
    assert sorted(plan.completion) == ["B1", "B2"]
    # B2 was named first, so it takes the machine first.
    order = sorted(plan.placements, key=lambda p: p.start)
    assert [p.job_key for p in order] == ["B2", "B1"]


_BENCHES = {
    "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    "MD2": Machine("MD2", "MD 2", "manual", available_hrs_per_day=9.5),
}


def test_one_helper_cannot_work_two_benches_at_the_same_instant():
    """roster.py rosters only CNC/VMC because a helper walks between benches. The
    brief read that as "any floating qualified person mans this station", which
    gives the same helper to EVERY bench they are qualified for — measured on a
    60-job synthetic book, 11 windows in which one person is on two benches at
    once. Walking between benches means being at one of them at a time."""
    masters = _masters(
        [Process(1, "DEBURING", 2.0, None, None, "MD1/MD2")],
        [Operator("Anturam", "MD1/MD2", ["MD1", "MD2"], "First shift")],
        machines=dict(_BENCHES))
    plan = _run(masters, [_B("B1", "ITEM", 30), _B("B2", "ITEM", 30)])
    assert {p.machine for p in plan.placements} == {"MD1"}
    segs = sorted(s for p in plan.placements for s in p.segments)
    for earlier, later in zip(segs, segs[1:]):
        assert earlier[1] <= later[0]


def test_two_helpers_do_work_two_benches_in_parallel():
    """The counterpart: one bench per person is a cap on SIMULTANEITY, not a
    refusal to use the benches. With two helpers both benches run at once."""
    masters = _masters(
        [Process(1, "DEBURING", 2.0, None, None, "MD1/MD2")],
        [Operator("Anturam", "MD1/MD2", ["MD1", "MD2"], "First shift"),
         Operator("Sanjay", "MD1/MD2", ["MD1", "MD2"], "First shift")],
        machines=dict(_BENCHES))
    plan = _run(masters, [_B("B1", "ITEM", 30), _B("B2", "ITEM", 30)])
    assert {p.machine for p in plan.placements} == {"MD1", "MD2"}
    assert {p.start for p in plan.placements} == {datetime(2026, 8, 12, 8, 0)}
    assert len({p.segments[0][2] for p in plan.placements}) == 2


def _bench_masters(operators):
    """Two benches, two items, one item pinned to each bench."""
    return Masters(
        machines=dict(_BENCHES),
        routings={
            "ONE": Routing("ONE", "d", "", "", None,
                           [Process(1, "DEBURING", 2.0, None, None, "MD1")]),
            "TWO": Routing("TWO", "d", "", "", None,
                           [Process(1, "DEBURING", 2.0, None, None, "MD2")]),
        },
        operators=operators, calendar=WorkCalendar())


def _assert_no_operator_double_booking(plan):
    """Nobody is in two places at the same moment. Reads the published segments,
    which are the operator record."""
    by_operator: dict = {}
    for p in plan.placements:
        for start, end, who in p.segments:
            by_operator.setdefault(who, []).append((start, end, p.machine))
    for who, spans in sorted(by_operator.items()):
        spans.sort()
        for earlier, later in zip(spans, spans[1:]):
            assert earlier[1] <= later[0], (
                f"{who} is on {earlier[2]} and {later[2]} at the same moment "
                f"({earlier[0]}-{earlier[1]} vs {later[0]}-{later[1]})")


def test_a_helper_is_never_on_two_benches_at_the_same_instant():
    """The brief's station rule was "any floating qualified person mans this
    station", which hands the SAME person to every bench they are qualified for —
    49 same-instant double-bookings on a 50-job book. Both benches here have work
    ready at 08:00 and exactly one helper is on shift, so the sketch runs both at
    once with Anturam's name on both bars."""
    masters = _bench_masters(
        [Operator("Anturam", "MD1/MD2", ["MD1", "MD2"], "First shift")])
    plan = _run(masters, [_B("B1", "ONE", 30), _B("B2", "TWO", 30)])
    _assert_no_operator_double_booking(plan)


def test_one_helper_serves_two_benches_at_different_times_in_one_shift():
    """The other half, and the one that stops the fix regressing into a
    shift-long lock. The brief's prose is "on that shift, present, and not
    already occupied AT THAT MOMENT" — interval exclusion. Locking the helper to
    one bench for the whole shift also removes the double-booking, but it deletes
    capacity the shop really has: measured on helper-scarce books it cost +52%
    late-days (41 vs 27 on 50 jobs / 6 stations / 2 helpers) and left one helper
    idle 600 of 660 minutes.

    One helper, an hour of work on each of two benches. He does MD1 08:00-09:00,
    walks over, and does MD2 09:00-10:00 — same shift, never both at once.
    """
    masters = _bench_masters(
        [Operator("Anturam", "MD1/MD2", ["MD1", "MD2"], "First shift")])
    plan = _run(masters, [_B("B1", "ONE", 30), _B("B2", "TWO", 30)])
    by_machine = {p.machine: p for p in plan.placements}
    assert sorted(by_machine) == ["MD1", "MD2"]
    assert (by_machine["MD1"].start, by_machine["MD1"].end) == (
        datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 9, 0))
    assert (by_machine["MD2"].start, by_machine["MD2"].end) == (
        datetime(2026, 8, 12, 9, 0), datetime(2026, 8, 12, 10, 0))
    assert {s[2] for p in plan.placements for s in p.segments} == {"Anturam"}
    _assert_no_operator_double_booking(plan)


def test_a_bench_with_a_part_on_it_keeps_its_helper():
    """The carry-over rule is not a CNC privilege. One helper, two benches: MD1's
    400-piece job runs out the first day with 140 minutes left, and MD2 is sitting
    on 800 minutes of untouched work. Raw demand says go to MD2 and strand the
    part; the part in hand wins, exactly as it does on a chuck."""
    masters = _bench_masters(
        [Operator("Anturam", "MD1/MD2", ["MD1", "MD2"], "First shift")])
    plan = _run(masters, [_B("B1", "ONE", 400), _B("B2", "TWO", 400)])
    on_md1 = [p for p in plan.placements if p.machine == "MD1"][0]
    assert on_md1.job_key == "B1"
    assert on_md1.segments[0][0] == datetime(2026, 8, 12, 8, 0)
    # Thursday is the weekly off, so the second day is Friday: 140 minutes left.
    assert on_md1.end == datetime(2026, 8, 14, 10, 20)


def test_the_helper_order_the_caller_happens_to_use_never_moves_a_bench():
    masters_forward = _bench_masters(
        [Operator("Amit", "MD1/MD2", ["MD1", "MD2"], "First shift"),
         Operator("Zafar", "MD1/MD2", ["MD1", "MD2"], "First shift")])
    masters_backward = _bench_masters(
        [Operator("Zafar", "MD1/MD2", ["MD1", "MD2"], "First shift"),
         Operator("Amit", "MD1/MD2", ["MD1", "MD2"], "First shift")])
    batches = [_B("B1", "ONE", 30), _B("B2", "TWO", 30)]

    def sig(masters):
        plan = _run(masters, batches)
        return sorted((p.machine, p.segments[0][2]) for p in plan.placements)

    assert sig(masters_forward) == sig(masters_backward)
    assert sig(masters_forward) == [("MD1", "Amit"), ("MD2", "Zafar")]


def test_a_step_with_a_machine_but_no_work_does_not_park_the_machine():
    """A routing step with a machine and a zero cycle time is not a milestone —
    `_settle_milestones` correctly leaves it to the shift loop. The brief's
    `_run_machine` then computes `take <= 0` and RETURNS, so the machine goes home
    for good, the step is never placed, and the whole book fails loud at the
    horizon. It must be placed as a zero-duration step and the chain carry on."""
    masters = _masters(
        [Process(1, "VISUAL CHECK", 0.0, None, None, "MD1"),
         Process(2, "DEBURING", 2.0, None, None, "MD1")],
        [Operator("Anturam", "MD1", ["MD1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 30)])
    check = [p for p in plan.placements if p.op_seq == 1][0]
    deb = [p for p in plan.placements if p.op_seq == 2][0]
    assert check.start == check.end == datetime(2026, 8, 12, 8, 0)
    assert check.work_min == 0.0
    assert (deb.start, deb.end) == (datetime(2026, 8, 12, 8, 0),
                                    datetime(2026, 8, 12, 9, 0))


def test_the_auto_mode_plan_start_floor_is_honoured():
    """`Config.plan_start_floor` is an ISO **string**, and it is a floor on top of
    08:00 of the plan-start date — the brief returned it verbatim, which puts a
    str into every datetime comparison in the module."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    jobs, _bk, _sk = build_jobs([_B("B1", "ITEM", 10)], masters)
    cfg = _cfg()
    cfg.plan_start_floor = "2026-08-12T14:00:00"
    plan = scheduler.schedule(jobs, ["B1"], build_shop(masters), cfg)
    assert plan.placements[0].start == datetime(2026, 8, 12, 14, 0)
    # A floor EARLIER than 08:00 must not drag the plan backwards.
    cfg.plan_start_floor = "2026-08-11T03:00:00"
    plan = scheduler.schedule(jobs, ["B1"], build_shop(masters), cfg)
    assert plan.placements[0].start == datetime(2026, 8, 12, 8, 0)


def test_pace_is_a_standing_guard_over_whatever_produced_the_plan():
    """The engine paces at claim time, so this sweep should never bind on a plan
    the shift clock built. It is kept as the invariant over the PUBLISHED plan —
    Task 6 pre-places frozen operations that never pass through the shift loop —
    so it is pinned directly: a starved successor's end is lifted to its
    predecessor's, and a zero-duration milestone is moved WHOLE, never stretched
    into a bar that claims hours it never had."""
    at = datetime(2026, 8, 12, 8, 0)
    def _p(seq, start_h, end_h, machine="CNC1"):
        return scheduler.Placement("B1", seq, f"OP{seq}", "machining", machine, 5,
                                   at.replace(hour=start_h), at.replace(hour=end_h),
                                   60.0, ())
    milestone = scheduler.Placement("B1", 3, "DISPATCH", "dispatch", None, 0,
                                    at.replace(hour=11), at.replace(hour=11), 0.0, ())
    completion = {}
    out = scheduler._pace([_p(1, 8, 17), _p(2, 9, 11, "CNC4"), milestone], completion)
    by_seq = {p.op_seq: p for p in out}
    assert by_seq[2].start == at.replace(hour=9)          # start never moves
    assert by_seq[2].end == at.replace(hour=17)           # end paced to op 1's
    assert by_seq[3].start == by_seq[3].end == at.replace(hour=17)
    assert completion["B1"] == at.replace(hour=17)


def test_an_unstaffable_machine_fails_loud_rather_than_dropping_the_work():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Anturam", "MD1", ["MD1"], "First shift")])
    try:
        _run(masters, [_B("B1", "ITEM", 10)])
    except RuntimeError as exc:
        assert "B1" in str(exc) and "CNC1" in str(exc)
    else:                                   # pragma: no cover - failure path
        raise AssertionError("an unschedulable book must fail loud")
