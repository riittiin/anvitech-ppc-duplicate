"""Rule 5 — overlap mode: the % applies to CUTTING time only, setup excluded.

Machine occupancy = cutting time (cycle x qty) + a 90-min setup. Overlap measures
the percentage against the CUTTING portion only — the next machine's own setup
runs in parallel, so the previous operation's setup is not counted. A step with no
cutting time (a finishing/inspection step) does not overlap: the next step waits
for it to fully complete.

``elapsed_before_next(prev_occupancy_min, prev_run_min, config)`` returns how long
after the previous operation STARTS the next operation may start.
"""
from datetime import datetime

from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.models import ScheduleEntry
from engine.rules import rule5_overlap_mode as r5


def _entry(seq, name, occ, qty=10):
    return ScheduleEntry(
        batch_id="X1", item_code="X", process_seq=seq, process_name=name,
        machine="M", qty=qty, occupancy_min=occ,
        start=datetime(2025, 3, 5, 8, 0), end=datetime(2025, 3, 5, 9, 0),
    )


def _machining_then_finishing_schedule():
    # P1 machining: 100 cutting + 90 setup = 190 occ. P2/P3 finishing: 90 setup only.
    return [_entry(1, "CNC", 190), _entry(2, "DEBURRING", 90), _entry(3, "INSPECTION", 90)]


def test_overlap_view_shows_machining_handoff_overlapped():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    rows = r5.build_overlap_view(_machining_then_finishing_schedule(), cfg)
    assert len(rows) == 2  # two consecutive handoffs for three processes
    h1 = rows[0]  # CNC -> DEBURRING: machining step, overlaps
    assert h1["Overlap applied"].startswith("yes")
    # sequential would wait the full 190; overlap waits 50% of the 100 cutting = 50.
    assert h1["Next allowed after: this run (min)"] == 50
    assert h1["Pulled earlier (min)"] == 140


def test_overlap_view_finishing_step_not_overlapped():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    rows = r5.build_overlap_view(_machining_then_finishing_schedule(), cfg)
    h2 = rows[1]  # DEBURRING -> INSPECTION: no cutting, no overlap
    assert h2["Overlap applied"].startswith("no")
    assert h2["Pulled earlier (min)"] == 0


def test_overlap_view_sequential_mode_pulls_nothing():
    cfg = Config(overlap_mode=OVERLAP_SEQUENTIAL)
    rows = r5.build_overlap_view(_machining_then_finishing_schedule(), cfg)
    assert all(r["Pulled earlier (min)"] == 0 for r in rows)


def test_sequential_waits_for_full_previous():
    cfg = Config(overlap_mode=OVERLAP_SEQUENTIAL)
    # occupancy 290 = 200 cutting + 90 setup; sequential waits the whole operation.
    assert r5.elapsed_before_next(290, 200, cfg) == 290


def test_overlap_counts_cutting_only_not_setup():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    # occupancy 290 = 200 cutting + 90 setup. 50% of the CUTTING (200) = 100,
    # NOT 50% of 290. The 90-min setup is excluded.
    assert r5.elapsed_before_next(290, 200, cfg) == 100


def test_overlap_custom_percent_on_cutting():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=25)
    assert r5.elapsed_before_next(290, 200, cfg) == 50


def test_no_cutting_step_does_not_overlap():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    # A finishing step: occupancy 90 = 0 cutting + 90 setup. No cutting -> no
    # overlap -> the next step waits for full completion (returns full occupancy).
    assert r5.elapsed_before_next(90, 0, cfg) == 90
