"""Analytics — utilization computed from a plan (hand-verified numbers)."""
import os
from datetime import date, datetime, timedelta

import pytest

from engine.config import Config, OVERLAP_PERCENT
from engine.models import (Batch, Process, Routing, Machine, WorkCalendar, Masters,
                            Operator, ScheduleEntry)
from engine.rules import rule6_allocate
from engine import analytics


def _masters(procs, machines):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type=t, available_hrs_per_day=hrs)
          for m, t, hrs in machines}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    mm.routings["X"] = Routing(item_code="X", description="RING", customer="", rm_type="",
                               moq=None, processes=procs)
    return mm


def _batch(qty=50):
    return Batch(batch_id="B", item_code="X", item_name="RING", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["SO"])


def test_machine_utilization_is_busy_over_available_in_window():
    # One two-shift machine M; P1 = 10 min/pc x 50 + 90 setup = 590 min busy.
    procs = [Process(1, "CNC", 10, 10, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))          # Wed; sequential; op-logic off
    sched = rule6_allocate.run([_batch(50)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    m = next(r for r in a["machines"] if r["Machine"] == "M")
    assert m["Busy (hrs)"] == round(590 / 60.0, 1)          # hand-computed busy
    clock_for, _ = rule6_allocate._clock_factory(masters, cfg)
    win_start = min(e.start for e in sched)
    win_end = max(e.end for e in sched)
    avail = clock_for("M").working_minutes_between(win_start, win_end) / 60.0
    assert m["Available (hrs)"] == round(avail, 1)
    assert m["Utilization %"] == round(m["Busy (hrs)"] / m["Available (hrs)"] * 100.0, 1)
    assert m["Ops"] == 1 and m["Pieces"] == 50


def test_machine_busy_matches_build_machine_view():
    # Cross-check: analytics Busy (hrs)*60 must equal build_machine_view's Busy (min).
    procs = [Process(1, "CNC", 4, 4, "M", None), Process(2, "VMC", 3, 3, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "VMC", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    _, summary = rule6_allocate.build_machine_view(sched, masters, cfg)
    mv = {r["Machine"]: r["Busy (min)"] for r in summary}
    for r in a["machines"]:
        assert round(r["Busy (hrs)"] * 60.0) == round(mv[r["Machine"]])   # two paths agree


def test_machine_group_rollup_by_type():
    procs = [Process(1, "CNC A", 5, 5, "M1", None), Process(2, "CNC B", 5, 5, "M2", None)]
    masters = _masters(procs, [("M1", "CNC lathe", 19.5), ("M2", "CNC lathe", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(40)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    g = next(r for r in a["machine_groups"] if r["Type"] == "CNC lathe")
    assert g["Machines"] == 2
    # Group util = EXACT total busy / EXACT total available across the two CNCs
    # (computed from raw minutes, not the rounded per-machine display values).
    clock_for, _ = rule6_allocate._clock_factory(masters, cfg)
    win_s = min(e.start for e in sched)
    win_e = max(e.end for e in sched)
    busy = sum(e.occupancy_min for e in sched) / 60.0
    avail = sum(clock_for(m).working_minutes_between(win_s, win_e) for m in ("M1", "M2")) / 60.0
    assert g["Utilization %"] == round(busy / avail * 100.0, 1)
    assert g["Busy (hrs)"] == round(busy, 1)


def test_process_work_share_sums_to_machine_busy():
    procs = [Process(1, "CNC", 4, 4, "M", None), Process(2, "WASH", 2, 2, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "Manual Washing", 9.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    total_proc = round(sum(p["Work (hrs)"] for p in a["processes"]), 1)
    total_machine = round(sum(m["Busy (hrs)"] for m in a["machines"]), 1)
    assert total_proc == total_machine                      # all work accounted for
    assert round(sum(p["Share %"] for p in a["processes"])) == 100


def test_operator_utilization_uses_shift_capacity():
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    assert a["operators"], "operator section populated when operator logic is on"
    o = a["operators"][0]
    assert o["Operator"] == "Op One"
    assert o["Busy (hrs)"] > 0 and 0 <= o["Utilization %"] <= 100


def test_operator_hours_attributed_per_shift_never_over_100():
    # THE RUPESH CASE: a two-shift machine (19.5 h/day) runs one LONG op (several
    # days, both shifts). Rule 6 pins ONE first-shift operator name on the whole op,
    # but the night hours are really worked by the second-shift man. Analytics must
    # bill each person only the hours of shifts he actually mans — so no operator
    # can ever exceed 100% of his own shift capacity.
    procs = [Process(1, "CNC", 30, 30, "M", None)]     # 30 min/pc x 200 = 6000 min >> one shift
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [
        Operator("Day Man", "M", machines=["M"], shift="First shift"),
        Operator("Night Man", "M", machines=["M"], shift="Second shift"),
    ]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched = rule6_allocate.run([_batch(200)], config=cfg, masters=masters)
    assert sum(e.occupancy_min for e in sched) == 6090          # sanity: multi-day op
    a = analytics.build_analytics(sched, masters, cfg)
    ops = {o["Operator"]: o for o in a["operators"]}
    # Both people appear: the night shift is genuinely manned by Night Man.
    assert "Day Man" in ops and "Night Man" in ops
    for o in ops.values():
        assert o["Busy (hrs)"] <= o["Available (hrs)"] + 0.1, \
            f'{o["Operator"]} billed {o["Busy (hrs)"]}h against {o["Available (hrs)"]}h'
        assert o["Utilization %"] <= 100.0


def test_operator_capacity_never_rotates_across_a_friday():
    """Shift rotation was removed 2026-08-05 (owner decision: the shift an admin sets
    holds every week). This test used to prove the OPPOSITE — that analytics rotated a
    two-shift operator's EFFECTIVE shift across a Friday so a nominal Second-shift
    (10h) person legitimately working a rotated First-shift (11h) day didn't show a
    false >100% (the live 2026-07-25 'Khansab Mulla 100.5%' bug). Rotation itself is
    gone now, so this proves the opposite invariant: a nominal Second-shift operator's
    capacity for a day AFTER an elapsed Friday still prices at Second-shift (10h), not
    a rotated First-shift (11h)."""
    masters = _masters([Process(1, "CNC", 30, 30, "M", None)],
                       [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("X", "M", machines=["M"], shift="Second shift")]
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),  # anchor Fri 28-02
                 apply_operator_logic=True)
    # 10-03 is a Monday AFTER the 07-03 Friday. Under the old rotation this would have
    # flipped X to First shift (11h); now the nominal Second shift must hold (10h).
    e = ScheduleEntry(
        batch_id="B", item_code="X", process_seq=1, process_name="CNC", machine="M",
        qty=8, occupancy_min=240.0,
        start=datetime(2025, 3, 10, 19, 0), end=datetime(2025, 3, 10, 23, 0),
        so_refs=["SO"], operator="X",
        op_segments=[(datetime(2025, 3, 10, 19, 0), datetime(2025, 3, 10, 23, 0), "X")])
    a = analytics.build_analytics([e], masters, cfg, [_batch(8)])
    o = next(o for o in a["operators"] if o["Operator"] == "X")
    assert o["Available (hrs)"] == 10.0, o          # nominal Second shift, unrotated
    assert o["Utilization %"] <= 100.0, o


def test_operator_capacity_uses_the_stored_shift_across_a_friday():
    """Analytics keeps its OWN copy of the rotation rule. With rotation removed,
    a plan spanning a Friday must measure a person against the shift on file for
    every day, not a rotated one."""
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    # scheduler="new" is the engine that used to rotate shifts on a Friday boundary;
    # if analytics still rotated too, the days on/after 2025-03-14 (one Friday after
    # the next Friday past the anchor -> an ODD rotation count) would flip to Second
    # shift (10h) instead of staying First shift (11h) for the whole window.
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 5),
                 apply_operator_logic=True)

    e1 = ScheduleEntry(batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
                        machine="M", qty=10, occupancy_min=60,
                        start=datetime(2025, 3, 5, 8, 0), end=datetime(2025, 3, 5, 9, 0),
                        operator="Op One")
    # Zero-duration, operator-less entry purely to stretch the plan window past a
    # second Friday (2025-03-14) without adding any billable work.
    e2 = ScheduleEntry(batch_id="B2", item_code="X", process_seq=1, process_name="OS",
                        machine="OS / Outsourced", qty=1, occupancy_min=0,
                        start=datetime(2025, 3, 17, 8, 0), end=datetime(2025, 3, 17, 8, 0),
                        operator="")
    sched = [e1, e2]

    a = analytics.build_analytics(sched, masters, cfg)
    o = next(o for o in a["operators"] if o["Operator"] == "Op One")

    win_start, win_end = date(2025, 3, 5), date(2025, 3, 17)
    working_days = []
    d = win_start
    while d <= win_end:
        if masters.calendar.is_working_day(d):
            working_days.append(d)
        d += timedelta(days=1)

    FIRST_SHIFT_HOURS = 11.0   # 08:00-19:00, the Config default
    expected = len(working_days) * FIRST_SHIFT_HOURS
    assert o["Available (hrs)"] == pytest.approx(expected), (
        "operator capacity mixed in a rotated (Second shift) day somewhere in the "
        "window -- analytics rotation was not fully removed")


def test_operator_utilization_never_exceeds_100_on_multi_machine_plan():
    # Two machines sharing one first-shift man + a night man: per-shift attribution
    # keeps everyone <= 100% even when ops overlap machines and shifts.
    procs = [Process(1, "CNC", 6, 6, "M", None), Process(2, "VMC", 6, 6, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "VMC", 19.5)])
    masters.operators = [
        Operator("Day Man", "M, N", machines=["M", "N"], shift="First shift"),
        Operator("Night Man", "M, N", machines=["M", "N"], shift="Second shift"),
    ]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True,
                 overlap_mode=OVERLAP_PERCENT, overlap_percent=80)
    sched = rule6_allocate.run([_batch(150)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    for o in a["operators"]:
        assert o["Utilization %"] is None or o["Utilization %"] <= 100.0


def test_headline_flags_bottleneck_and_underused():
    procs = [Process(1, "HEAVY", 20, 20, "M", None), Process(2, "LIGHT", 1, 1, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "Manual Washing", 9.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(200)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    assert a["headline"]["bottleneck"]["Machine"] == a["machines"][0]["Machine"]
    assert all(m["Utilization %"] <= 30 for m in a["headline"]["underused"])


def test_used_manual_machine_uncovered_still_shows_utilization():
    # Production (new engine) schedules work on a manual station that has NO
    # first-shift operator (uncovered by operator_coverage). Analytics must NOT report
    # "no capacity" (util '-') for a machine the plan actually uses — it measures the
    # station against its PHYSICAL manual window (09:00-18:00). Staffing is a separate
    # panel. Regression for the live 2026-07-24 "manual machines show '-'" report.
    masters = _masters([Process(1, "Wash", 6, 6, "MW", None)],
                       [("MW", "Manual Washing", 9.5)])          # single-shift (< 12h)
    masters.operators = [Operator("Night Only", "MW", machines=["MW"],
                                  shift="Second shift")]          # no FIRST-shift cover
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)  # a Wednesday
    e = ScheduleEntry(batch_id="B", item_code="X", process_seq=1, process_name="Wash",
                      machine="MW", qty=5, occupancy_min=30,
                      start=datetime(2025, 3, 5, 9, 0), end=datetime(2025, 3, 5, 10, 0),
                      so_refs=["SO"])
    a = analytics.build_analytics([e], masters, cfg)
    row = next(r for r in a["machines"] if r["Machine"] == "MW")
    assert row["Busy (hrs)"] == 0.5
    assert row["Available (hrs)"] == 1.0        # manual 09-18 window ∩ [09:00,10:00] = 60 min
    assert row["Utilization %"] == 50.0         # was None (0-capacity) — the bug
    assert row["Status"] != "no capacity"


def test_empty_plan_returns_empty_analytics():
    masters = _masters([Process(1, "CNC", 4, 4, "M", None)], [("M", "CNC lathe", 19.5)])
    a = analytics.build_analytics([], masters, Config())
    assert a["machines"] == [] and a["headline"] == {} and a["window"] is None


def test_operator_availability_reduced_by_absence():
    # A wide plan window (16 calendar days / 13 working days) with a single
    # short op for "Op One", so there is plenty of headroom: Available (hrs)
    # must drop by exactly 2 x their per-day shift hours for a 2-working-day
    # absence, and utilization must stay well under 100%.
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    cfg = Config(apply_operator_logic=True)

    # A real op for Op One, plus a zero-duration, operator-less entry 15 days
    # later purely to stretch the plan window (window = min start .. max end
    # across ALL schedule entries) without adding any billable work.
    e1 = ScheduleEntry(batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
                        machine="M", qty=10, occupancy_min=60,
                        start=datetime(2025, 3, 5, 8, 0), end=datetime(2025, 3, 5, 9, 0),
                        operator="Op One")
    e2 = ScheduleEntry(batch_id="B2", item_code="X", process_seq=1, process_name="OS",
                        machine="OS / Outsourced", qty=1, occupancy_min=0,
                        start=datetime(2025, 3, 20, 8, 0), end=datetime(2025, 3, 20, 8, 0),
                        operator="")
    sched = [e1, e2]

    a0 = analytics.build_analytics(sched, masters, cfg)
    o0 = next(o for o in a0["operators"] if o["Operator"] == "Op One")

    win_start = min(e.start for e in sched).date()
    win_end = max(e.end for e in sched).date()
    working_days = []
    d = win_start
    while d <= win_end:
        if masters.calendar.is_working_day(d):
            working_days.append(d)
        d += timedelta(days=1)
    assert len(working_days) >= 2, "need at least 2 working days in the plan window"
    per_day = o0["Available (hrs)"] / len(working_days)

    absence = [{"operator": "Op One", "from_date": working_days[1].isoformat(),
                "to_date": working_days[2].isoformat()}]
    a1 = analytics.build_analytics(sched, masters, cfg, absences=absence)
    o1 = next(o for o in a1["operators"] if o["Operator"] == "Op One")

    assert o1["Available (hrs)"] == pytest.approx(o0["Available (hrs)"] - 2 * per_day, abs=0.05)
    for o in a1["operators"]:
        assert o["Utilization %"] is None or o["Utilization %"] <= 100.0


def test_absences_default_none_is_byte_identical():
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a_default = analytics.build_analytics(sched, masters, cfg)
    a_explicit_none = analytics.build_analytics(sched, masters, cfg, absences=None)
    assert a_default == a_explicit_none


def test_absence_for_unrelated_operator_or_outside_window_is_ignored():
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a0 = analytics.build_analytics(sched, masters, cfg)
    o0 = next(o for o in a0["operators"] if o["Operator"] == "Op One")

    win_end = max(e.end for e in sched).date()
    far_future = (win_end + timedelta(days=365)).isoformat()
    absences = [
        {"operator": "Someone Else", "from_date": "2025-03-05", "to_date": "2025-03-06"},
        {"operator": "Op One", "from_date": far_future, "to_date": far_future},
        {"operator": "Op One", "from_date": "not-a-date", "to_date": "also-not-a-date"},
    ]
    a1 = analytics.build_analytics(sched, masters, cfg, absences=absences)
    o1 = next(o for o in a1["operators"] if o["Operator"] == "Op One")
    assert o1["Available (hrs)"] == o0["Available (hrs)"]


@pytest.mark.skipif(not os.path.exists("Test4.xlsx"), reason="real data file not present")
def test_analytics_invariants_on_real_plan():
    from engine.loaders import load_all
    so, masters = load_all("Test4.xlsx")
    cfg = Config(plan_start_date=date(2026, 7, 1), overlap_mode=OVERLAP_PERCENT,
                 overlap_percent=50, split_parallel=True)
    batches = [Batch(batch_id=f"B{i}", item_code=s.item_code, item_name=s.item_name,
                     qty=s.qty or 10, so_delivery_date=s.delivery_date, source_so_refs=[s.so_no])
               for i, s in enumerate(so[:20])]
    sched = rule6_allocate.run(batches, config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg, batches)
    for m in a["machines"]:
        u = m["Utilization %"]
        assert u is None or 0 <= u <= 100                       # in range
        assert m["Busy (hrs)"] <= m["Available (hrs)"] + 0.1    # busy never exceeds capacity
    assert round(sum(p["Work (hrs)"] for p in a["processes"]), 0) == \
           round(sum(m["Busy (hrs)"] for m in a["machines"]), 0)   # no work lost/invented
