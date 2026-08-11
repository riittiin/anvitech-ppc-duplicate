"""Gantt view-model built from the Rule 6 schedule."""
from datetime import date

from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward
from engine.gantt import build_gantt


def test_empty_schedule():
    g = build_gantt([], [], None)
    assert g["rows"] == [] and g["num_days"] == 0


def test_gantt_from_real_run(loaded):
    so_lines, masters = loaded
    pr = PlanRun(so_lines=so_lines)
    # Live default plan_start_date is None (auto today); pin the historical date.
    run_forward(pr, Config(plan_start_date=date(2025, 3, 1)), masters)
    g = build_gantt(pr.schedule, pr.batches_prioritized, masters)

    # One row per consolidated batch, each carrying the SO identity columns.
    assert len(g["rows"]) == len(pr.batches_prioritized)
    r0 = g["rows"][0]
    for key in ("item_name", "item_code", "so_no", "so_qty", "so_delivery_date", "completion", "bars"):
        assert key in r0
    assert r0["bars"], "first row should have process bars"

    # 'completion' = the date the LAST process of that order finishes (max bar end).
    import datetime as _dt
    for row in g["rows"]:
        latest = max(_dt.datetime.strptime(b["end"], "%d-%m-%Y %H:%M") for b in row["bars"])
        assert row["completion"] == latest.strftime("%d-%m-%Y")

    # Day axis covers the full schedule span.
    assert g["num_days"] >= 1
    assert len(g["days"]) == g["num_days"]
    assert sum(m["days"] for m in g["months"]) == g["num_days"]

    # Every bar references a coloured machine, a non-negative position, and carries
    # the operator field (so the Gantt can show who runs each step).
    for row in g["rows"]:
        for bar in row["bars"]:
            assert bar["machine"] in g["machine_colors"]
            assert bar["offset_days"] >= 0
            assert bar["duration_days"] >= 0
            assert "operator" in bar


def test_gantt_bars_carry_operator_when_logic_on():
    # With operator logic on, every scheduled bar names its operator.
    from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters, Operator
    import datetime
    machines = {"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="", rm_type="",
                                    moq=None, processes=[Process(1, "OP", 10, 10, "CNC1", None)])
    masters.operators = [Operator("Asha", "CNC1", machines=["CNC1"], shift="First shift")]
    cfg = Config(plan_start_date=datetime.date(2025, 3, 5), apply_operator_logic=True)
    from engine.rules import rule6_allocate
    b = Batch(batch_id="B", item_code="X", item_name="x", qty=5,
              so_delivery_date=datetime.date(2025, 3, 20), source_so_refs=["S"])
    sched = rule6_allocate.run([b], config=cfg, masters=masters)
    g = build_gantt(sched, [b], masters)
    assert g["rows"][0]["bars"][0]["operator"] == "Asha"


def test_gantt_bar_names_both_operators_on_a_shift_handoff():
    # An op whose op_segments span a shift handoff (two different operators)
    # must show both names on its bar, not just the first segment's operator.
    from engine.models import Batch, ScheduleEntry, Machine, Masters, WorkCalendar
    import datetime
    machines = {"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    b = Batch(batch_id="B", item_code="X", item_name="x", qty=5,
              so_delivery_date=datetime.date(2025, 3, 20), source_so_refs=["S"])
    start = datetime.datetime(2025, 3, 5, 8, 0)
    handoff = datetime.datetime(2025, 3, 5, 19, 0)
    end = datetime.datetime(2025, 3, 6, 5, 0)
    entry = ScheduleEntry(
        batch_id="B", item_code="X", process_seq=1, process_name="OP",
        machine="CNC1", qty=5, occupancy_min=(end - start).total_seconds() / 60,
        start=start, end=end, so_refs=["S"], operator="Alpha",
        op_segments=[(start, handoff, "Alpha"), (handoff, end, "Bravo")],
    )
    g = build_gantt([entry], [b], masters)
    assert g["rows"][0]["bars"][0]["operator"] == "Alpha → Bravo"


def test_gantt_bar_falls_back_to_entry_operator_without_segments():
    # OS/off-machine milestones (and operator-logic-off entries) carry no
    # op_segments; the bar must still show the entry's single operator field.
    from engine.models import Batch, ScheduleEntry, Masters, WorkCalendar
    import datetime
    masters = Masters(machines={}, calendar=WorkCalendar())
    b = Batch(batch_id="B", item_code="X", item_name="x", qty=5,
              so_delivery_date=datetime.date(2025, 3, 20), source_so_refs=["S"])
    start = datetime.datetime(2025, 3, 5, 8, 0)
    end = datetime.datetime(2025, 3, 5, 9, 0)
    entry = ScheduleEntry(
        batch_id="B", item_code="X", process_seq=1, process_name="OS",
        machine="OS / Outsourced", qty=5, occupancy_min=60,
        start=start, end=end, so_refs=["S"], operator="",
        op_segments=[],
    )
    g = build_gantt([entry], [b], masters)
    assert g["rows"][0]["bars"][0]["operator"] == ""


def test_bar_offsets_are_time_accurate(loaded):
    so_lines, masters = loaded
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, Config(plan_start_date=__import__("datetime").date(2025, 3, 1)), masters)
    g = build_gantt(pr.schedule, pr.batches_prioritized, masters)
    # Axis anchors at midnight of day 1; the first process starts at 08:00, so the
    # earliest bar sits 8/24 of a day in (within the first day column, not at 0).
    first = min(b["offset_days"] for r in g["rows"] for b in r["bars"])
    assert 0.0 <= first < 1.0
    assert abs(first - 8 / 24) < 1e-3   # 08:00 first-shift start
