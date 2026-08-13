from datetime import date, datetime

import pytest

pytest.importorskip("pyjobshop")

from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)
from cp_engine import domain, model, windows


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = None


PLAN_START = datetime(2026, 8, 12, 8, 0)


def _masters(processes, operators=()):
    return Masters(
        machines={
            "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
            "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
            "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
        },
        routings={"ITEM": Routing("ITEM", "d", "cust", "rm", None, processes)},
        operators=list(operators), calendar=WorkCalendar())


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0, **kw)


def _build(masters, batches, setup_mode="credit"):
    jobs, _by_key, _skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, {})
    shifts = windows.build_shifts(PLAN_START, masters.calendar, _cfg(), 30)
    return model.build(jobs, shop, _cfg(), PLAN_START, shifts,
                       setup_mode=setup_mode), jobs


def test_a_machining_task_gets_one_mode_per_machine_option_and_no_operator():
    """Spec §3: the operator leaves the mode definitions. A machining task on
    two candidate machines has exactly TWO modes — not two-times-the-qualified-
    operator-count, which is what today's model builds."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 5.0, None, "CNC4", "CNC1")],
        operators=[Operator("A", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                   Operator("B", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10)])
    task_idx = built.task_of[("B1", 1)]
    modes = [m for m in built.data.modes if m.task == task_idx]
    assert len(modes) == 2
    assert all(len(m.resources) == 1 for m in modes)


def test_a_manual_task_keeps_its_operator_in_the_mode():
    """Rule 1 binds CNC/VMC only. A helper walks between stations, so manual and
    inspection ops keep a free per-task operator choice (spec §3)."""
    masters = _masters(
        [Process(1, "DEBURING", 2.0, None, None, "MD1")],
        operators=[Operator("A", "MD1", ["MD1"], "First shift"),
                   Operator("B", "MD1", ["MD1"], "First shift")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10)])
    task_idx = built.task_of[("B1", 1)]
    modes = [m for m in built.data.modes if m.task == task_idx]
    assert len(modes) == 2                      # one per qualified operator
    assert all(len(m.resources) == 2 for m in modes)   # machine + operator


def test_setup_is_charged_into_a_machining_duration_and_never_a_manual_one():
    """Rule 4's encoding is inverted (spec §5.4): 90 min is always in the
    duration and credited back only for a same-part changeover."""
    masters = _masters([Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
                        Process(2, "DEBURING", 2.0, None, None, "MD1")],
                       operators=[Operator("A", "MD1", ["MD1"], "First shift")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10)])
    cnc = [m for m in built.data.modes if m.task == built.task_of[("B1", 1)]]
    manual = [m for m in built.data.modes if m.task == built.task_of[("B1", 2)]]
    assert cnc[0].duration == 90 + 10 * 5
    assert manual[0].duration == 10 * 2


def test_an_outsourced_step_is_a_flat_block_on_an_unlimited_pool():
    masters = _masters([Process(1, "BAND SAW OS", 2880.0, None, None, "OS")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10)])
    modes = [m for m in built.data.modes if m.task == built.task_of[("B1", 1)]]
    assert len(modes) == 1
    assert modes[0].resources == [built.os_res]
    assert modes[0].duration == 2880           # flat, never qty x cycle


def test_a_dispatch_milestone_gets_no_task():
    masters = _masters([Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
                        Process(2, "DISPATCH", None, None, None, None)])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10)])
    assert ("B1", 1) in built.task_of
    assert ("B1", 2) not in built.task_of


def test_os_is_sequential_on_both_sides_and_in_house_steps_are_not():
    masters = _masters([Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
                        Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
                        Process(3, "DEBURING", 2.0, None, None, "MD1")],
                       operators=[Operator("A", "MD1", ["MD1"], "First shift")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10)])
    kinds = {type(c).__name__ for c in built.data.constraints.end_before_start}
    assert kinds                                # both OS edges are hard sequential
    assert len(built.data.constraints.end_before_start) == 2


def test_a_task_may_span_a_break_but_is_never_split():
    """Rule 2: allow_breaks lets the part stay in the chuck overnight; a CP
    interval is contiguous, so the operation can never be sliced."""
    masters = _masters([Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 500)])
    task = built.data.tasks[built.task_of[("B1", 1)]]
    assert task.allow_breaks is True
    assert task.optional is False


def test_a_job_with_no_delivery_date_gets_no_due_date():
    """pyjobshop asserts due_date is not None when it builds tardiness vars, and
    an undated order has no date to miss. Recording 0 would claim it landed
    exactly on its date — the one value the objective calls perfect."""
    masters = _masters([Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])
    built, _jobs = _build(masters, [_B("B1", "ITEM", 10, due=None)])
    assert built.data.jobs[built.job_of["B1"]].due_date is None
    assert "B1" not in built.dated_jobs
