"""Cross-tab integration: every surface must describe the ONE plan the same way.

These pin the owner's rule (2026-07-26): the same plan's makespan and operator
assignments must read identically on the Schedule table, the machine-wise view,
the Gantt, and Analytics — no surface may re-derive its own number.
"""
from datetime import date, datetime

from engine.config import Config
from engine.models import (Batch, Machine, Masters, WorkCalendar, Routing,
                            Process, ScheduleEntry)
from engine.rules import rule6_allocate
from engine import analytics, optimizer
from engine.gantt import build_gantt


def _masters():
    ms = {"M": Machine(machine_no="M", display_name="M", machine_type="CNC lathe",
                       available_hrs_per_day=19.5)}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    mm.routings["X"] = Routing(item_code="X", description="RING", customer="",
                               rm_type="", moq=None, processes=[Process(1, "CNC", 10, 10, "M", None)])
    return mm


def _handoff_entry():
    """One op on machine M, 08:00 -> next day 02:00, worked by two operators across
    the 19:00 shift boundary (Alpha day, Bravo night) — a real handoff."""
    start = datetime(2025, 3, 3, 8, 0)
    handoff = datetime(2025, 3, 3, 19, 0)
    end = datetime(2025, 3, 4, 2, 0)
    return ScheduleEntry(
        batch_id="B", item_code="X", process_seq=1, process_name="CNC",
        machine="M", qty=50, occupancy_min=600.0, start=start, end=end,
        notes="", so_refs=["SO"], operator="Alpha",
        op_segments=[(start, handoff, "Alpha"), (handoff, end, "Bravo")])


# ---- Makespan: one number everywhere -------------------------------------- #

def test_analytics_makespan_equals_plan_metrics_makespan():
    """Analytics must report the SAME makespan as the Optimize panel/plan metrics
    (days from plan start to the last end), not calendar-days-spanned."""
    e = _handoff_entry()
    cfg = Config(plan_start_date=date(2025, 3, 1))   # first op starts 03-03, so the two defs differ
    a = analytics.build_analytics([e], _masters(), cfg)
    pm = optimizer.plan_metrics([e], [], cfg.plan_start_date)
    assert a["window"]["makespan_days"] == pm["makespan_days"]
    assert a["headline"]["makespan_days"] == pm["makespan_days"]


# ---- Operator: the real handoff on every surface -------------------------- #

def test_schedule_row_shows_operator_handoff():
    e = _handoff_entry()
    assert e.as_row()["Operator"] == "Alpha → Bravo"


def test_machine_view_shows_operator_handoff():
    cfg = Config(plan_start_date=date(2025, 3, 1))
    timeline, _ = rule6_allocate.build_machine_view([_handoff_entry()], _masters(), cfg)
    assert timeline[0]["Operator"] == "Alpha → Bravo"


def test_all_surfaces_agree_on_the_operator_for_a_job():
    """Schedule table, machine-wise view, and Gantt must name the SAME operator(s)."""
    e = _handoff_entry()
    cfg = Config(plan_start_date=date(2025, 3, 1))
    b = Batch(batch_id="B", item_code="X", item_name="RING", qty=50,
              so_delivery_date=date(2025, 3, 20), source_so_refs=["SO"])
    row_op = e.as_row()["Operator"]
    mview_op = rule6_allocate.build_machine_view([e], _masters(), cfg)[0][0]["Operator"]
    gantt_op = build_gantt([e], [b], _masters())["rows"][0]["bars"][0]["operator"]
    assert row_op == mview_op == gantt_op == "Alpha → Bravo"


# ---- Back-compat guards --------------------------------------------------- #

def test_single_shift_operator_label_is_just_the_name():
    start = datetime(2025, 3, 3, 8, 0); end = datetime(2025, 3, 3, 12, 0)
    e = ScheduleEntry(batch_id="B", item_code="X", process_seq=1, process_name="CNC",
                      machine="M", qty=10, occupancy_min=240.0, start=start, end=end,
                      notes="", so_refs=["SO"], operator="Solo",
                      op_segments=[(start, end, "Solo")])
    assert e.as_row()["Operator"] == "Solo"


def test_no_operator_hides_the_column():
    """Operator-logic-off entry (no operator, no segments) keeps the column hidden —
    the golden trace has no Operator column and must stay byte-identical."""
    start = datetime(2025, 3, 3, 8, 0); end = datetime(2025, 3, 3, 12, 0)
    e = ScheduleEntry(batch_id="B", item_code="X", process_seq=1, process_name="CNC",
                      machine="M", qty=10, occupancy_min=240.0, start=start, end=end,
                      notes="", so_refs=["SO"], operator="", op_segments=[])
    assert "Operator" not in e.as_row()
