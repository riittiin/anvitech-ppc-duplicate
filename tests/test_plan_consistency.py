"""Cross-feature consistency: every surface must report the SAME completion date for
the same (SO#, item), derived from the SAME plan run.

Live 2026-08-07: the Gantt said 07-Sep and the delay justification report said 04-Sep
for one order. Two independent causes, both pinned here:

  1. The delay report built its OWN plan instead of reading the one every other tab
     shows (``_plan``'s cached run).
  2. The auto plan-start floor (``_ceil_next_hour(now)``) was recomputed on every
     planning entry, so two runs a few hours apart produced materially different
     schedules from identical inputs — measured on the real book: a 6-hour gap moved
     54 of 68 orders' completion dates, worst case by 24 days.
"""
import importlib
import json
from datetime import date, datetime

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.config import Config
from engine.delay_report import build_delay_report
from engine.models import (Batch, Machine, Masters, Routing, ScheduleEntry, SOLine,
                           WorkCalendar)
from engine.optimizer import expected_completion
from tests.sample_workbook import build_sample_bytes, ITEM_A
from engine.models import Order


def _api():
    import api.main as m
    importlib.reload(m)
    return m


# --- 1. ONE definition of "expected completion" ----------------------------- #

def test_delay_report_completion_matches_the_shared_definition_over_an_os_tail():
    """An order whose last step is outsourced finishes when the OS block ends. The
    delay report used to drop OS/off-machine lanes when deriving the completion, so it
    reported an earlier date than the Gantt and the Orders tab for the same order."""
    machining = ScheduleEntry(
        batch_id="B1", item_code="X", process_seq=1, process_name="CNC", machine="M",
        qty=100, occupancy_min=240.0, start=datetime(2025, 3, 3, 8, 0),
        end=datetime(2025, 3, 3, 12, 0), notes="", so_refs=["SO1"], operator="P",
        op_segments=[(datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 12, 0), "P")])
    outsourced = ScheduleEntry(
        batch_id="B1", item_code="X", process_seq=2, process_name="PLATING",
        machine="OS / Outsourced", qty=100, occupancy_min=4320.0,
        start=datetime(2025, 3, 3, 12, 0), end=datetime(2025, 3, 6, 12, 0),
        notes="", so_refs=["SO1"], operator="", op_segments=[])
    schedule = [machining, outsourced]

    masters = Masters(machines={"M": Machine(machine_no="M", display_name="M",
                                             machine_type="CNC lathe",
                                             available_hrs_per_day=19.5)},
                      calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="X", customer="",
                                    rm_type="", moq=None, processes=[])
    line = SOLine(so_no="SO1", item_code="X", item_name="X", qty=100,
                  delivery_date=date(2025, 3, 4))
    batch = Batch(batch_id="B1", item_code="X", item_name="X", qty=100,
                  so_delivery_date=date(2025, 3, 4), source_so_refs=["SO1"])

    canonical = expected_completion(schedule)[("SO1", "X")]
    assert canonical == date(2025, 3, 6)      # the whole order, OS tail included

    rep = build_delay_report(schedule, [line], [batch],
                             Config(plan_start_date=date(2025, 3, 3)), masters)
    row = rep["summary"][0]
    assert row["Expected Completion"] == canonical
    assert row["Days Late"] == (canonical - line.delivery_date).days


def test_expected_completion_is_the_latest_end_per_so_and_item():
    """A consolidated batch's entries carry every member SO, so each member gets the
    batch's completion — and a second item on the same SO is tracked separately."""
    def e(item, so_refs, end):
        return ScheduleEntry(batch_id="B", item_code=item, process_seq=1,
                             process_name="P", machine="M", qty=1, occupancy_min=60.0,
                             start=datetime(2025, 3, 3, 8, 0), end=end, notes="",
                             so_refs=so_refs, operator="", op_segments=[])
    sched = [e("X", ["SO1", "SO2"], datetime(2025, 3, 4, 10, 0)),
             e("X", ["SO1", "SO2"], datetime(2025, 3, 6, 10, 0)),
             e("Y", ["SO1"], datetime(2025, 3, 9, 10, 0))]
    assert expected_completion(sched) == {("SO1", "X"): date(2025, 3, 6),
                                          ("SO2", "X"): date(2025, 3, 6),
                                          ("SO1", "Y"): date(2025, 3, 9)}


# --- 2. The auto plan-start floor is pinned for the day --------------------- #
# ⚠️ The next-hour plan clock is switched OFF live (owner decision 2026-08-11: the plan
# starts at the 08:00 shift start again — api.main.PLAN_START_NEXT_HOUR). These two
# tests turn it back ON so the mechanism keeps its coverage for the day it is wanted
# again; the LIVE behaviour, including that all these dates stay consistent without any
# clock at all, is pinned in tests/test_plan_start_shift_start.py.

def test_finished_optimization_starts_the_plan_at_the_next_full_hour():
    """Owner's rule: when the optimization finishes, the plan begins at the next full
    hour. A contest that lands at 09:01 on Monday makes the plan start 10:00 Monday —
    and that clock then HOLDS for every feature until the next contest finishes."""
    m = _api()
    m.PLAN_START_NEXT_HOUR = True          # safe: every _api() call reloads the module
    clock = {"now": datetime(2026, 8, 10, 8, 30)}       # Monday, before the contest
    m._ist_now = lambda: clock["now"]
    m._ist_today = lambda: clock["now"].date()
    m._metrics_for_ranks = lambda *a, **k: None          # keep the contest's own numbers

    opening = m._resolve_config(Config(plan_start_date=None))
    assert opening.plan_start_floor == "2026-08-10T09:00"

    clock["now"] = datetime(2026, 8, 10, 9, 1)           # the contest finishes
    import time as _time
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="job1", started_mono=_time.monotonic())
    m._finalize_optimize("job1", Config(plan_start_date=None), None, "deep",
                         winner_overlap=70, ranks={"k": 1}, best={"total_late_days": 1},
                         evals=1, table=[], cancelled=False)

    clock["now"] = datetime(2026, 8, 10, 15, 40)         # hours later, any feature
    after = m._resolve_config(Config(plan_start_date=None))
    assert after.plan_start_floor == "2026-08-10T10:00"


def test_plan_clock_holds_between_optimizations():
    """Nothing finished, so nothing may move: the plan start an admin sees at 09:20
    must be the same one a report uses at 16:45."""
    m = _api()
    m.PLAN_START_NEXT_HOUR = True          # safe: every _api() call reloads the module
    clock = {"now": datetime(2026, 8, 10, 9, 20)}
    m._ist_now = lambda: clock["now"]
    m._ist_today = lambda: clock["now"].date()

    first = m._resolve_config(Config(plan_start_date=None))
    clock["now"] = datetime(2026, 8, 10, 16, 45)
    later = m._resolve_config(Config(plan_start_date=None))
    assert first.plan_start_floor == later.plan_start_floor == "2026-08-10T10:00"

    clock["now"] = datetime(2026, 8, 11, 8, 5)           # next day -> a fresh clock
    assert m._resolve_config(Config(plan_start_date=None)).plan_start_floor \
        == "2026-08-11T09:00"


def test_fixed_plan_start_date_carries_no_floor():
    """A pinned date (testing/reproducibility) still starts at 08:00 of that date."""
    m = _api()
    cfg = m._resolve_config(Config(plan_start_date=date(2025, 3, 3)))
    assert cfg.plan_start_floor is None


# --- 3. ONE "the plan you have now" number ---------------------------------- #

def test_optimize_panel_before_column_is_the_plan_the_user_actually_has():
    """The Optimize panel's 'Now' column and the auto-note's 'was N' must be the same
    plan. The panel used to measure the book with NO optimized sequence at all, so once
    an optimization was applied it showed a plan the user did not have — measured on
    Test9: panel 967 late-days / 61.68 days vs note 956 / 61.5, same instant."""
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20)),
                           Order("SO2", ITEM_A, ITEM_A, 25, date(2025, 3, 18))])
    m._current_masters()
    book_store.save_plan_priority({f"SO2\x1f{ITEM_A}": 0, f"SO1\x1f{ITEM_A}": 1},
                                  {"saved_at": "t"})

    import time as _time
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="j", started_mono=_time.monotonic())
    m._finalize_optimize("j", m._load_plan_config(), {"total_late_days": 999,
                                                      "makespan_days": 99.0,
                                                      "ontime_breach": 999.0}, "deep",
                         winner_overlap=70, ranks={f"SO1\x1f{ITEM_A}": 0},
                         best={"total_late_days": 1, "makespan_days": 1.0,
                               "ontime_breach": 1.0},
                         evals=1, table=[], cancelled=False)

    panel_before = m._OPTIMIZE["result"]["baseline"]
    note_before = m._incumbent_metrics()
    assert panel_before["total_late_days"] == note_before["total_late_days"]
    assert panel_before["makespan_days"] == note_before["makespan_days"]


# --- 4. Reporting features model the shop the way the ENGINE does ----------- #

def _manual_machine():
    # available_hrs_per_day below two_shift_threshold_hours -> a single-shift station
    # (manual / inspection / packing), the kind the two engines disagreed about.
    return Machine(machine_no="MI3", display_name="MI3", machine_type="Inspection",
                   available_hrs_per_day=9.0)


def test_single_shift_station_window_follows_the_active_engine():
    """The NEW engine runs a non-night station on the FIRST shift (08:00-19:00) — its
    `iter_windows` skips only the SECOND shift. Analytics and the delay report built
    their own model that gave those stations 09:00-18:00, so 2 hours of real planned
    work every day fell 'outside working hours'. Measured on Test9 before this fix:
    158 hours of scheduled work sat outside the window those features believed in."""
    from engine.operator_coverage import eligible_window
    new = eligible_window(_manual_machine(), Config(scheduler="new"))
    assert new == [(8 * 60, 19 * 60)]

    # The retired classic engine really did use 09:00-18:00 — it must not move, or the
    # ~500 tests that validate it (and the golden trace) would be measuring a new shop.
    classic = eligible_window(_manual_machine(), Config(scheduler="classic"))
    assert classic == [(9 * 60, 18 * 60)]


def test_single_shift_station_coverage_window_follows_the_active_engine():
    """Same rule for the operator-gated view (`machine_windows`), which feeds the
    Analytics capacity clock and the 'when each machine can run' table."""
    from engine.operator_coverage import machine_windows
    from engine.models import Operator
    mach = _manual_machine()
    masters = Masters(machines={"MI3": mach}, calendar=WorkCalendar())
    masters.operators.append(Operator(name="Sidhanath", shift="First shift",
                                      machines=["MI3"]))
    win_new, _ = machine_windows(masters, Config(scheduler="new"))
    assert win_new["MI3"] == [(8 * 60, 19 * 60)]

    win_classic, _ = machine_windows(masters, Config(scheduler="classic"))
    assert win_classic["MI3"] == [(9 * 60, 18 * 60)]


# --- 5. Nothing disappears for being idle ----------------------------------- #

def test_analytics_lists_every_operator_and_machine_even_with_no_work():
    """Live report 2026-08-07: Settings showed 20 staff, Analytics showed 19 — Sandeep
    Kumar was missing. Both lists were built by walking the SCHEDULE, so a person or a
    machine the plan gave no work to vanished instead of showing 0%. That is backwards
    for a utilization report: a completely idle resource is the one you most need to
    see. (On Test9 four machines — MA1, MP1, MPK3, MW3 — were silently absent.)"""
    from engine.analytics import build_analytics
    from engine.models import Operator

    busy_m = Machine(machine_no="CNC1", display_name="CNC 1",
                     machine_type="CNC lathe", available_hrs_per_day=19.5)
    idle_m = Machine(machine_no="MW3", display_name="MW3",
                     machine_type="Washing", available_hrs_per_day=9.0)
    masters = Masters(machines={"CNC1": busy_m, "MW3": idle_m}, calendar=WorkCalendar())
    masters.operators.extend([
        Operator(name="Rohan", shift="First shift", machines=["CNC1"]),
        Operator(name="Sandeep Kumar", shift="First shift", machines=["MW3"]),
    ])
    entry = ScheduleEntry(
        batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
        machine="CNC1", qty=10, occupancy_min=240.0,
        start=datetime(2025, 3, 3, 8, 0), end=datetime(2025, 3, 3, 12, 0),
        notes="", so_refs=["SO1"], operator="Rohan",
        op_segments=[(datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 3, 12, 0), "Rohan")])

    an = build_analytics([entry], masters, Config(scheduler="new",
                                                  apply_operator_logic=True), [])

    assert {m["Machine"] for m in an["machines"]} == {"CNC 1", "MW3"}
    assert {o["Operator"] for o in an["operators"]} == {"Rohan", "Sandeep Kumar"}

    idle_row = next(m for m in an["machines"] if m["Machine"] == "MW3")
    assert idle_row["Busy (hrs)"] == 0.0 and idle_row["Ops"] == 0
    idle_op = next(o for o in an["operators"] if o["Operator"] == "Sandeep Kumar")
    assert idle_op["Busy (hrs)"] == 0.0 and idle_op["Ops"] == 0


def test_delay_report_still_lists_an_order_with_no_in_house_work():
    """Same silent-omission class: the delay report skipped any order with no in-house
    operation, so a fully-outsourced order vanished from the report while appearing on
    the Orders tab and the Gantt. It must be listed, with its real completion date."""
    outsourced = ScheduleEntry(
        batch_id="B1", item_code="X", process_seq=1, process_name="PLATING",
        machine="OS / Outsourced", qty=10, occupancy_min=2880.0,
        start=datetime(2025, 3, 3, 8, 0), end=datetime(2025, 3, 5, 8, 0),
        notes="", so_refs=["SO1"], operator="", op_segments=[])
    masters = Masters(machines={}, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="X", customer="",
                                    rm_type="", moq=None, processes=[])
    line = SOLine(so_no="SO1", item_code="X", item_name="X", qty=10,
                  delivery_date=date(2025, 3, 4))

    rep = build_delay_report([outsourced], [line], [],
                             Config(plan_start_date=date(2025, 3, 3)), masters)
    assert [r["SO No"] for r in rep["summary"]] == ["SO1"]
    row = rep["summary"][0]
    assert row["Expected Completion"] == date(2025, 3, 5)
    assert row["Days Late"] == 1


# --- 6. Operators come from SETTINGS; machines come from the workbook -------- #

def test_operators_come_from_settings_not_the_workbook_sheet():
    """Owner's rule: operators, their shifts and their machines live in the Settings
    tab ONLY. The workbook's operator sheet seeds that table once and is a fossil after
    — the Excel is the source for the SO list and the Machine master, never for people.
    So Analytics must follow Settings: a person added there appears even though the
    workbook has never heard of them, and a person deleted there disappears even though
    the workbook still lists them."""
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    book_store.save_plan_config(json.dumps({"apply_operator_logic": True}))
    admin = TestClient(m.app)
    admin.post("/login", data={"username": "anvitech", "password": "1930rail"})

    from engine.loaders import load_all
    import io as _io
    _so, workbook_masters = load_all(_io.BytesIO(book_store.load_masters_bytes()))
    workbook_names = {o.name for o in workbook_masters.operators}

    seeded = {o["name"] for o in admin.get("/operators").json()["operators"]}
    assert seeded == workbook_names          # one-time seed from the sheet

    def analytics_operators():
        m._PLAN_CACHE.update(key=None, result=None, artifacts=None)
        run = admin.post("/run", json={}).json()
        return {o["Operator"] for o in run["trace"]["analytics"]["operators"]}

    assert admin.post("/operators", json={"name": "Settings Only Person",
                                          "machines_raw": "CNC1",
                                          "shift": "First shift"}).status_code == 200
    assert "Settings Only Person" not in workbook_names
    assert "Settings Only Person" in analytics_operators()

    victim = next(o for o in admin.get("/operators").json()["operators"]
                  if o["name"] in workbook_names)
    assert admin.delete(f"/operators/{victim['id']}").status_code == 200
    assert victim["name"] in workbook_names          # still in the Excel...
    assert victim["name"] not in analytics_operators()   # ...but gone from the plan


def test_machine_with_no_operator_in_settings_is_flagged():
    """Every machine in the workbook's Machine master must have somebody in Settings who
    can run it, or the plan can never schedule it. Report it (non-blocking), don't let
    it fail silently. A two-shift machine with nobody on the night shift is called out
    separately — it can still run days, but its night capacity is unusable."""
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    admin = TestClient(m.app)
    admin.post("/login", data={"username": "anvitech", "password": "1930rail"})
    admin.get("/operators")                     # trigger the one-time seed

    for o in admin.get("/operators").json()["operators"]:
        admin.delete(f"/operators/{o['id']}")   # nobody left in Settings at all

    m._PLAN_CACHE.update(key=None, result=None, artifacts=None)
    report = admin.post("/run", json={}).json()["report"]
    kinds = {r[0] for r in report["rows"]}
    assert "MACHINE_NO_OPERATOR" in kinds

    flagged = {r[1] for r in report["rows"] if r[0] == "MACHINE_NO_OPERATOR"}
    masters = m._current_masters()
    assert flagged == set(masters.machines)     # every machine, none skipped


# --- 7. The delay report reads the plan every other tab shows --------------- #

def test_delay_report_reuses_the_plan_run_instead_of_computing_its_own():
    """The report must be a VIEW of the cached plan, not a second plan. Guarded by
    making a second scheduling pass impossible: once /run has planned, building the
    report must not call the scheduler again."""
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()

    admin = TestClient(m.app)
    admin.post("/login", data={"username": "anvitech", "password": "1930rail"})
    assert admin.post("/run", json={}).status_code == 200

    def _boom(*a, **k):
        raise AssertionError("the delay report re-planned instead of reusing the plan")

    m.run_forward = _boom
    assert admin.get("/delay-report.xlsx").status_code == 200
