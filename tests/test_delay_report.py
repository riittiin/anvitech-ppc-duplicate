"""Delay justification report (pure module) — reconstructs WHY each order is delayed
from the finished plan. Every order's running + all waits must cover its whole span
with no unaccounted or double-counted minutes."""
from datetime import date, datetime

from engine.config import Config
from engine.models import (ScheduleEntry, Batch, SOLine, Machine, Masters,
                           WorkCalendar, Routing)
from engine.delay_report import build_delay_report


def _so_line(so="SO1", item="X", qty=100, due=date(2025, 3, 20)):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=qty, delivery_date=due)


def _entry(so, item, seq, machine, s, e, op="P", qty=100):
    return ScheduleEntry(batch_id="B_" + so, item_code=item, process_seq=seq,
                         process_name="CNC", machine=machine, qty=qty,
                         occupancy_min=(e - s).total_seconds() / 60, start=s, end=e,
                         notes="", so_refs=[so], operator=op, op_segments=[(s, e, op)])


def _masters(machines):
    ms = {mid: Machine(machine_no=mid, display_name=mid, machine_type=t,
                       available_hrs_per_day=hrs) for mid, t, hrs in machines}
    m = Masters(machines=ms, calendar=WorkCalendar())
    return m


def _with_routing(m, *items):
    for it in items:
        m.routings[it] = Routing(item_code=it, description=it, customer="", rm_type="",
                                 moq=None, processes=[])
    return m


def _batch(so, item, rank_date=date(2025, 3, 10)):
    return Batch(batch_id="B_" + so, item_code=item, item_name=item, qty=1,
                 so_delivery_date=rank_date, source_so_refs=[so])


# ---- Task 1: timeline skeleton -------------------------------------------- #

def test_on_time_order_has_one_running_block_and_no_late():
    cfg = Config(plan_start_date=date(2025, 3, 3))
    e = _entry("SO1", "X", 1, "M", datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 12, 0))
    masters = _with_routing(_masters([("M", "CNC lathe", 19.5)]), "X")
    rep = build_delay_report([e], [_so_line(due=date(2025, 3, 20))], [], cfg, masters)
    running = [r for r in rep["detail"] if r["State"] == "RUNNING"]
    assert len(running) == 1 and running[0]["Machine"] == "M"
    s = rep["summary"][0]
    assert s["Days Late"] <= 0
    total = sum(r["Hours"] for r in rep["detail"])
    # From the plan's FIRST SCHEDULED MOMENT, not midnight (2026-08-09): the hours
    # before the plan starts are nobody's delay, and used to be billed to the crew.
    span_h = (datetime(2025, 3, 3, 12, 0) - datetime(2025, 3, 3, 8, 0)).total_seconds() / 3600
    assert abs(total - span_h) < 1e-6


# ---- Task 2: machine-busy attribution ------------------------------------- #

def test_wait_names_every_higher_priority_blocker():
    cfg = Config(plan_start_date=date(2025, 3, 3))
    ours = _entry("SO1", "X", 1, "M", datetime(2025, 3, 3, 12, 0), datetime(2025, 3, 3, 16, 0))
    blk1 = _entry("SO2", "Y", 1, "M", datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 10, 0))
    blk2 = _entry("SO3", "Z", 1, "M", datetime(2025, 3, 3, 10, 0), datetime(2025, 3, 3, 12, 0))
    masters = _with_routing(_masters([("M", "CNC lathe", 19.5)]), "X", "Y", "Z")
    bp = [_batch("SO2", "Y"), _batch("SO3", "Z"), _batch("SO1", "X")]  # SO1 lowest priority
    rep = build_delay_report([ours, blk1, blk2], [_so_line("SO1", "X")], bp, cfg, masters)
    busy = [r for r in rep["detail"]
            if r["State"] == "WAITING (machine busy)" and r["SO No"] == "SO1"]
    whys = " | ".join(r["Why"] for r in busy)
    assert "SO2" in whys and "SO3" in whys and "higher priority" in whys
    assert abs(sum(r["Hours"] for r in busy) - 4.0) < 1e-6      # 08:00–12:00 fully attributed


# ---- Task 3: off-hours vs crew + full invariant --------------------------- #

def test_night_gap_is_off_hours_and_every_minute_is_attributed():
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    masters = _with_routing(_masters([("BS1", "Band saw", 9.5)]), "X")   # single-shift 09–18
    # Two ops a day apart, so the night between them is a real off-hours gap. (The
    # report now starts at the first scheduled op, so a LEADING night no longer exists
    # to be measured — that was time before the plan began.)
    e1 = _entry("SO1", "X", 1, "BS1", datetime(2025, 3, 3, 9, 0), datetime(2025, 3, 3, 11, 0))
    e2 = _entry("SO1", "X", 2, "BS1", datetime(2025, 3, 4, 9, 0), datetime(2025, 3, 4, 11, 0))
    rep = build_delay_report([e1, e2], [_so_line("SO1", "X")], [], cfg, masters)
    off = [r for r in rep["detail"] if r["State"] == "WAITING (off-hours)"]
    assert off and all("hours" in r["Why"].lower() for r in off)
    assert not [r for r in rep["detail"]
                if "unattributed" in r["State"] or "(free)" in r["State"]]
    total = sum(r["Hours"] for r in rep["detail"] if r["SO No"] == "SO1")
    span = (datetime(2025, 3, 4, 11, 0) - datetime(2025, 3, 3, 9, 0)).total_seconds() / 3600
    assert abs(total - span) < 1e-3


# ---- Task 4: summary aggregation + why ------------------------------------ #

def test_summary_totals_and_why_match_the_detail():
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    ours = _entry("SO1", "X", 1, "M", datetime(2025, 3, 3, 12, 0), datetime(2025, 3, 3, 16, 0))
    blk = _entry("SO2", "Y", 1, "M", datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 12, 0))
    masters = _with_routing(_masters([("M", "CNC lathe", 19.5)]), "X", "Y")
    bp = [_batch("SO2", "Y"), _batch("SO1", "X")]
    rep = build_delay_report([ours, blk], [_so_line("SO1", "X", due=date(2025, 3, 2))], bp, cfg, masters)
    s = next(r for r in rep["summary"] if r["SO No"] == "SO1")
    det_machine = sum(r["Hours"] for r in rep["detail"]
                      if r["SO No"] == "SO1" and r["State"] == "WAITING (machine busy)")
    assert abs(s["Waiting: machine (days)"] - round(det_machine / 24, 1)) < 1e-9
    assert s["Days Late"] == 1 and "machines busy" in s["Why"].lower()


def test_concurrent_ops_do_not_double_count_the_span():
    """An order's ops can run concurrently (parallel split / overlap). 'Working' is the
    MERGED wall-clock, and merged-running + waits still equals the span exactly."""
    from engine.delay_report import _merge, _hours
    cfg = Config(plan_start_date=date(2025, 3, 3))
    e1 = _entry("SO1", "X", 1, "M1", datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 14, 0))
    e2 = _entry("SO1", "X", 2, "M2", datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 12, 0))
    masters = _with_routing(_masters([("M1", "CNC lathe", 19.5), ("M2", "VMC", 19.5)]), "X")
    rep = build_delay_report([e1, e2], [_so_line("SO1", "X")], [], cfg, masters)
    rows = rep["detail"]
    merged = _merge([(r["From"], r["To"]) for r in rows if r["State"] == "RUNNING"])
    merged_h = sum(_hours(a, b) for a, b in merged)
    wait_h = sum(r["Hours"] for r in rows if r["State"].startswith("WAITING"))
    span = _hours(datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 14, 0))
    assert merged_h == 6.0                      # 08:00–14:00 merged, not 10h summed
    assert abs(merged_h + wait_h - span) < 1e-6
