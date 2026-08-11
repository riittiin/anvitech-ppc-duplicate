from datetime import datetime, date
from engine import freeze
from engine.models import ScheduleEntry, Masters, Routing, Process, SOLine
from engine.loaders import normalize_process_name as _np


def test_schedule_projection_keeps_machine_ops_only():
    entries = [
        ScheduleEntry(batch_id="B1", item_code="IT-A", process_seq=2,
                      process_name="CNC first side", machine="CNC1", qty=100,
                      occupancy_min=120, start=datetime(2026,7,29,8,0),
                      end=datetime(2026,7,29,10,0), operator="Alpha", so_refs=["SO1"]),
        ScheduleEntry(batch_id="B1", item_code="IT-A", process_seq=9,
                      process_name="DISPATCH", machine="OS / Outsourced", qty=100,
                      occupancy_min=0, start=datetime(2026,7,29,10,0),
                      end=datetime(2026,7,29,10,0), operator="", so_refs=["SO1"]),
    ]
    rows = freeze.schedule_projection(entries)
    assert len(rows) == 1
    r = rows[0]
    assert r["machine"] == "CNC1" and r["operator"] == "Alpha"
    assert r["process_seq"] == 2 and r["so_refs"] == ["SO1"]
    assert r["start"] == "2026-07-29T08:00:00"


def test_compute_frozen_set_picks_partially_punched_steps():
    # Build real model objects matching engine.models schema.
    proc1 = Process(seq=1, name="CNC prep", cycle_time=1.0, total_time=1.0,
                    suggested_machine="CNC1", allotted_machine="CNC1")
    proc2 = Process(seq=2, name="CNC first side", cycle_time=1.0, total_time=1.0,
                    suggested_machine="CNC1", allotted_machine="CNC1")
    routing = Routing(item_code="IT-A", description="", customer="",
                      rm_type="", moq=None, processes=[proc1, proc2])
    masters = Masters(routings={"IT-A": routing})

    # Step seq 2 has remaining 40 (partially done) → frozen.
    line = SOLine(so_no="SO1", item_code="IT-A", item_name="x", qty=100,
                  delivery_date=date(2026, 8, 15),
                  process_qty={_np("CNC first side"): 40, _np("CNC prep"): 0})

    applied = [{"batch_id": "B1", "item_code": "IT-A", "process_seq": 2,
                "process_name": "CNC first side", "machine": "CNC1", "operator": "Alpha",
                "start": "2026-07-29T08:00:00", "end": "2026-07-29T10:00:00",
                "so_refs": ["SO1"]}]

    good = {("SO1", "IT-A", _np("CNC first side")): 60,   # 60 done, 40 left → in progress
            ("SO1", "IT-A", _np("CNC prep")): 100}        # fully done → not frozen

    rows = freeze.compute_frozen_set(applied, [line], good, masters)
    assert len(rows) == 1
    r = rows[0]
    assert r["so_no"] == "SO1" and r["op_seq"] == 2 and r["machine"] == "CNC1"
    assert r["operator"] == "Alpha" and r["remaining_qty"] == 40
    assert r["prev_start"] == "2026-07-29T08:00:00"


def test_compute_frozen_set_skips_step_not_in_last_plan():
    # Build real model objects.
    proc = Process(seq=2, name="CNC first side", cycle_time=1.0, total_time=1.0,
                   suggested_machine="CNC1", allotted_machine="CNC1")
    routing = Routing(item_code="IT-A", description="", customer="",
                      rm_type="", moq=None, processes=[proc])
    masters = Masters(routings={"IT-A": routing})

    line = SOLine(so_no="SO1", item_code="IT-A", item_name="x", qty=100,
                  delivery_date=date(2026, 8, 15),
                  process_qty={_np("CNC first side"): 40})

    good = {("SO1", "IT-A", _np("CNC first side")): 60}

    # No applied rows → empty result.
    assert freeze.compute_frozen_set([], [line], good, masters) == []
