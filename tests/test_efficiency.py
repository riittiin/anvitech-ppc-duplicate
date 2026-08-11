"""engine/efficiency.py — the fair monthly operator efficiency report.

Every spec bullet from docs/superpowers/specs/2026-07-18-operator-efficiency-report-design.md
is exercised here. Self-contained data (Masters/Routing/Process built inline);
no store, no clock.
"""
from datetime import date

from engine.config import Config
from engine.models import Actual, Masters, Process, Routing, WorkCalendar
from engine.efficiency import monthly_report, _cycle_for


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _routing(item_code, procs):
    """procs = list of (name, cycle_time)."""
    processes = [
        Process(seq=i + 1, name=n, cycle_time=ct, total_time=None,
                suggested_machine=None, allotted_machine=None)
        for i, (n, ct) in enumerate(procs)
    ]
    return Routing(item_code=item_code, description="", customer="", rm_type="",
                   moq=None, processes=processes)


def _masters(routings):
    m = Masters()
    for r in routings:
        m.routings[r.item_code] = r
    return m


def _actual(**kw):
    base = dict(so_no="SO1", item_code="A", entry_date=date(2025, 8, 4),
                operator="Ravi", shift="First", process="TURNING",
                qty_produced=0.0, qty_rejected=0.0)
    base.update(kw)
    return Actual(**base)


CFG = Config()
# First = 08->19 = 11h = 660min ; Second = 19->(24+5)=29 = 10h = 600min ;
# manual = 09->18 = 9h = 540min.
FIRST_MIN = (CFG.first_shift_end_hour - CFG.first_shift_start_hour) * 60          # 660
SECOND_MIN = ((24 + CFG.second_shift_end_hour) - CFG.first_shift_end_hour) * 60   # 600
MANUAL_MIN = (CFG.manual_end_hour - CFG.manual_start_hour) * 60                   # 540


def _row(rows, operator):
    return next(r for r in rows if r["Operator"] == operator)


# --------------------------------------------------------------------------- #
# Column contract
# --------------------------------------------------------------------------- #
EXPECTED_KEYS = [
    "Operator", "Days worked", "Days absent", "Attended (min)", "Earned (min)",
    "Efficiency %", "Pace vs standard (x)", "Good qty", "Rejected qty",
    "Reject %", "Downtime (min)", "Setup (min)", "Jobs handled",
    "No-standard punches",
]


def test_column_keys_exact():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    rows = monthly_report([_actual(qty_produced=10)], [], masters, CFG, 2025, 8)
    assert list(rows[0].keys()) == EXPECTED_KEYS


# --------------------------------------------------------------------------- #
# Empty / month boundary
# --------------------------------------------------------------------------- #
def test_empty_month_returns_empty_list():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    assert monthly_report([], [], masters, CFG, 2025, 8) == []


def test_month_boundary_filters_entry_date():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        _actual(entry_date=date(2025, 7, 31), qty_produced=100),  # excluded
        _actual(entry_date=date(2025, 8, 1), qty_produced=10),    # included
        _actual(entry_date=date(2025, 9, 1), qty_produced=100),   # excluded
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Good qty"] == 10               # only the August punch
    assert r["Earned (min)"] == 100.0        # 10 cycle * 10 good


# --------------------------------------------------------------------------- #
# Earned / efficiency / pace arithmetic
# --------------------------------------------------------------------------- #
def test_earned_and_efficiency_basic():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    # 60 good * 10 min = 600 earned; one First window = 660 attended.
    rows = monthly_report([_actual(qty_produced=60)], [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == float(FIRST_MIN)
    assert r["Earned (min)"] == 600.0
    assert r["Efficiency %"] == round(600.0 / FIRST_MIN * 100, 1)   # ~90.9
    assert r["Pace vs standard (x)"] == round(FIRST_MIN / 600.0, 2)  # attended/earned


# --------------------------------------------------------------------------- #
# Multi-job on one day counts the window ONCE
# --------------------------------------------------------------------------- #
def test_multi_job_same_day_shift_counts_window_once():
    masters = _masters([
        _routing("A", [("TURNING", 10.0)]),
        _routing("B", [("MILLING", 5.0)]),
    ])
    actuals = [
        _actual(item_code="A", process="TURNING", qty_produced=10),
        _actual(item_code="B", process="MILLING", qty_produced=20, so_no="SO2"),
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == float(FIRST_MIN)     # ONE window, not two
    assert r["Earned (min)"] == 10 * 10 + 20 * 5       # 200
    assert r["Jobs handled"] == 2                      # two distinct orders
    assert r["Days worked"] == 1


# --------------------------------------------------------------------------- #
# Two shifts same day = two windows
# --------------------------------------------------------------------------- #
def test_two_shifts_same_day_two_windows():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        _actual(shift="First", qty_produced=10),
        _actual(shift="Second", qty_produced=10),
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == float(FIRST_MIN + SECOND_MIN)
    assert r["Days worked"] == 1                        # still one calendar day


def test_blank_shift_uses_manual_window():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    rows = monthly_report([_actual(shift="", qty_produced=10)], [], masters, CFG, 2025, 8)
    assert _row(rows, "Ravi")["Attended (min)"] == float(MANUAL_MIN)


# --------------------------------------------------------------------------- #
# Real capture-form vocabulary: "1st shift" / "2nd shift" (not "First"/"Second")
# --------------------------------------------------------------------------- #
def test_1st_shift_text_uses_first_window_not_manual():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    rows = monthly_report(
        [_actual(shift="1st shift", qty_produced=60)], [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == float(FIRST_MIN)          # 660, not 540 (manual)
    assert r["Earned (min)"] == 600.0
    assert r["Efficiency %"] == round(600.0 / FIRST_MIN * 100, 1)   # ~90.9


def test_2nd_shift_text_uses_second_window_not_manual():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    rows = monthly_report(
        [_actual(shift="2nd shift", qty_produced=60)], [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == float(SECOND_MIN)         # 600, not 540 (manual)
    assert r["Earned (min)"] == 600.0
    assert r["Efficiency %"] == 100.0


def test_mixed_shift_vocabulary_same_day_one_window():
    """"1st shift" and "First shift" both normalize to 'first' — one shift,
    one window, not two, even though the raw text differs."""
    masters = _masters([
        _routing("A", [("TURNING", 10.0)]),
        _routing("B", [("MILLING", 5.0)]),
    ])
    actuals = [
        _actual(item_code="A", process="TURNING", shift="1st shift", qty_produced=10),
        _actual(item_code="B", process="MILLING", shift="First shift", qty_produced=20,
                so_no="SO2"),
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == float(FIRST_MIN)          # ONE window, not two
    assert r["Jobs handled"] == 2


# --------------------------------------------------------------------------- #
# Downtime + setup neutrality (subtract from attended, never earn/penalize)
# --------------------------------------------------------------------------- #
def test_downtime_and_setup_subtract_from_attended():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    control = monthly_report([_actual(qty_produced=60)], [], masters, CFG, 2025, 8)
    treated = monthly_report(
        [_actual(qty_produced=60, actual_setup_min=30, no_power_min=20,
                 tool_problem_min=10)],
        [], masters, CFG, 2025, 8)
    rc, rt = _row(control, "Ravi"), _row(treated, "Ravi")
    # Earned unchanged (same good qty); attended drops by 30 setup + 30 downtime.
    assert rt["Earned (min)"] == rc["Earned (min)"] == 600.0
    assert rt["Attended (min)"] == rc["Attended (min)"] - 60
    assert rt["Downtime (min)"] == 30.0
    assert rt["Setup (min)"] == 30.0
    # Efficiency rises purely because attended shrank (neutral, not a penalty).
    assert rt["Efficiency %"] > rc["Efficiency %"]


def test_downtime_setup_summed_across_days_punches_then_floored():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    # One window (660). Downtime+setup exceed it → attended floors at 0 → eff None.
    actuals = [
        _actual(qty_produced=10, actual_setup_min=400, no_power_min=400),
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == 0.0
    assert r["Efficiency %"] is None
    assert r["Pace vs standard (x)"] is None


# --------------------------------------------------------------------------- #
# Rejects earn nothing + reject %
# --------------------------------------------------------------------------- #
def test_rejects_earn_nothing_and_reject_pct():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    # produced 100, rejected 20 -> good 80 -> earned 800; reject% = 20/100 = 20.
    rows = monthly_report([_actual(qty_produced=100, qty_rejected=20)], [],
                          masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Good qty"] == 80
    assert r["Rejected qty"] == 20
    assert r["Earned (min)"] == 800.0          # only good earns
    assert r["Reject %"] == 20.0


# --------------------------------------------------------------------------- #
# No-standard punch: excluded from BOTH sides, flagged
# --------------------------------------------------------------------------- #
def test_no_standard_punch_excluded_and_flagged():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        _actual(item_code="A", process="TURNING", qty_produced=10),   # has standard
        _actual(item_code="A", process="GHOST STEP", qty_produced=99), # no standard
        _actual(item_code="Z", process="TURNING", qty_produced=99, so_no="SO9"),  # no routing
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Earned (min)"] == 100.0          # only the TURNING/A punch earns
    assert r["No-standard punches"] == 2
    # Good qty column still reflects everything the operator produced.
    assert r["Good qty"] == 10 + 99 + 99


def test_wholly_no_standard_day_contributes_nothing_to_attended():
    """Reviewer's exact walk: Aug-4 First standard (60x10, window 660) + Aug-5
    First wholly-no-standard punch => attended stays 660 (the Aug-5 window is
    excluded from BOTH sides), efficiency 600/660 = 90.9, flag = 1."""
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        _actual(entry_date=date(2025, 8, 4), qty_produced=60),                  # standard
        _actual(entry_date=date(2025, 8, 5), process="GHOST", qty_produced=5),  # no standard
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    assert r["Attended (min)"] == 660.0
    assert r["Earned (min)"] == 600.0
    assert r["Efficiency %"] == 90.9
    assert r["No-standard punches"] == 1


def test_no_standard_punch_downtime_does_not_deduct_from_shared_window():
    """Mixed same day/shift: only the STANDARD punch's downtime/setup deduct
    from the shared window — a no-standard punch's downtime is excluded along
    with the punch itself."""
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        _actual(qty_produced=60, actual_setup_min=15),               # standard
        _actual(process="GHOST", qty_produced=5, no_power_min=30),   # no standard, downtime 30
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    r = _row(rows, "Ravi")
    # window 660 - the standard punch's own setup 15 only; the ghost's 30 ignored.
    assert r["Attended (min)"] == 660.0 - 15
    assert r["Earned (min)"] == 600.0
    assert r["No-standard punches"] == 1


# --------------------------------------------------------------------------- #
# Process name matches by NORMALIZED comparison
# --------------------------------------------------------------------------- #
def test_process_match_normalized_case_and_spacing():
    masters = _masters([_routing("A", [("CNC First Side", 10.0)])])
    rows = monthly_report(
        [_actual(process="  cnc   first side ", qty_produced=10)],
        [], masters, CFG, 2025, 8)
    assert _row(rows, "Ravi")["Earned (min)"] == 100.0


def test_cycle_for_helper():
    masters = _masters([_routing("A", [("TURNING", 7.5)])])
    assert _cycle_for(masters, "A", "turning") == 7.5
    assert _cycle_for(masters, "A", "missing") is None
    assert _cycle_for(masters, "NOPE", "turning") is None


# --------------------------------------------------------------------------- #
# Absence days from the table (working days within the month only)
# --------------------------------------------------------------------------- #
def test_absence_days_from_table_working_days_in_month():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    # Aug 2025: Thursdays (weekly off) are 7,14,21,28. Range 6..8 Aug -> 6,8 working
    # (7 is Thursday, skipped) = 2 working days in month.
    absences = [{"operator": "Ravi", "from_date": "2025-08-06", "to_date": "2025-08-08"},
                {"operator": "Ravi", "from_date": "2025-09-01", "to_date": "2025-09-03"}]  # other month
    rows = monthly_report([_actual(qty_produced=10)], absences, masters, CFG, 2025, 8)
    assert _row(rows, "Ravi")["Days absent"] == 2


def test_absence_for_operator_with_no_punches_still_appears():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    absences = [{"operator": "Sita", "from_date": "2025-08-04", "to_date": "2025-08-05"}]
    rows = monthly_report([_actual(qty_produced=10)], absences, masters, CFG, 2025, 8)
    sita = _row(rows, "Sita")
    assert sita["Days absent"] == 2
    assert sita["Days worked"] == 0
    assert sita["Efficiency %"] is None      # never attended


# --------------------------------------------------------------------------- #
# Legacy punch without an operator -> "Unattributed"
# --------------------------------------------------------------------------- #
def test_unattributed_bucket():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        _actual(operator="Ravi", qty_produced=60),
        _actual(operator="", qty_produced=30),        # legacy
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    names = [r["Operator"] for r in rows]
    assert "Unattributed" in names
    assert _row(rows, "Unattributed")["Good qty"] == 30


# --------------------------------------------------------------------------- #
# Sorting: efficiency desc, None-eff after numeric, Unattributed always last
# --------------------------------------------------------------------------- #
def test_sorting_order():
    masters = _masters([_routing("A", [("TURNING", 10.0)])])
    actuals = [
        # Ravi: high efficiency (earned 600 / 660)
        _actual(operator="Ravi", qty_produced=60),
        # Sita: lower efficiency (earned 300 / 660)
        _actual(operator="Sita", qty_produced=30),
        # Gita: no standard -> earned 0 -> efficiency None
        _actual(operator="Gita", process="GHOST", qty_produced=50),
        # Unattributed high efficiency but must still be LAST
        _actual(operator="", qty_produced=60),
    ]
    rows = monthly_report(actuals, [], masters, CFG, 2025, 8)
    names = [r["Operator"] for r in rows]
    assert names.index("Ravi") < names.index("Sita")       # eff desc
    assert names.index("Sita") < names.index("Gita")       # numeric before None
    assert names[-1] == "Unattributed"                     # always last
