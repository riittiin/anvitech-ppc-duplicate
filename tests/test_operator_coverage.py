"""Operator coverage → per-machine working windows (pure helper)."""
from engine.config import Config
from engine.models import Machine, Operator, Masters, WorkCalendar
from engine.operator_coverage import machine_windows

CFG = Config()
FIRST = (CFG.first_shift_start_hour * 60, CFG.first_shift_end_hour * 60)        # 480,1140
SECOND = (CFG.first_shift_end_hour * 60, (24 + CFG.second_shift_end_hour) * 60)  # 1140,1740
MANUAL = (CFG.manual_start_hour * 60, CFG.manual_end_hour * 60)                  # 540,1080


# --- against the generated sample (via the loaded fixture) ----------------- #
def test_sample_windows_and_blocked(loaded):
    _, masters = loaded
    windows, report = machine_windows(masters, CFG)
    # CNC1 covered both shifts; CNC2 first only; BS1 (9.5) manual first-shift.
    assert windows["CNC1"] == [FIRST, SECOND]
    assert windows["CNC2"] == [FIRST]
    assert windows["BS1"] == [MANUAL]
    # No operator for VMC1 / MI1 / MW1 → blocked (empty window).
    assert windows["VMC1"] == [] and windows["MI1"] == [] and windows["MW1"] == []
    blocked_ids = {b["machine_id"] for b in report["blocked"]}
    assert {"VMC1", "MI1", "MW1"} <= blocked_ids
    # Provisional CNC9 bypasses the gate (two-shift window, not blocked).
    assert windows["CNC9"] == [FIRST, SECOND]
    assert "CNC9" not in blocked_ids


# --- synthetic, focused cases --------------------------------------------- #
def _masters(machines, operators):
    return Masters(machines={m.machine_no: m for m in machines},
                   operators=operators, calendar=WorkCalendar())


def test_type_name_specialty_matches_by_machine_type():
    # Operator lists "Milling M/c" (a type, not a machine no) → must match MM1.
    mm1 = Machine("MM1", "MM1", "Milling M/c", available_hrs_per_day=9.5)
    op = Operator("Shrikrishna", "Milling M/c", machines=["MILLINGMC"], shift="First shift")
    windows, report = machine_windows(_masters([mm1], [op]), CFG)
    assert windows["MM1"] == [MANUAL]
    assert report["unmatched_specialties"] == []


def test_second_shift_only_machine():
    cnc = Machine("CNC7", "CNC 7", "CNC lathe", available_hrs_per_day=19.5)
    op = Operator("Nageh", "CNC 7", machines=["CNC7"], shift="Second shift")
    windows, _ = machine_windows(_masters([cnc], [op]), CFG)
    assert windows["CNC7"] == [SECOND]   # eligible both, covered second only


def test_manual_with_only_second_shift_operator_is_blocked():
    # Manual (9.5) needs a FIRST-shift operator; its only op is second-shift → blocked.
    anturam = Machine("ANTURAM", "Anturam", "Manual deburring", available_hrs_per_day=9.5)
    op = Operator("Kartik", "Manual Deburring", machines=["MANUALDEBURRING"], shift="Second shift")
    windows, report = machine_windows(_masters([anturam], [op]), CFG)
    assert windows["ANTURAM"] == []
    assert any(b["machine_id"] == "ANTURAM" for b in report["blocked"])


def test_unmatched_specialty_is_reported():
    # Operator lists CNC2 which isn't a machine → reported, no scheduling effect.
    cnc1 = Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)
    op = Operator("X", "CNC 1, CNC 2", machines=["CNC1", "CNC2"], shift="First shift")
    _, report = machine_windows(_masters([cnc1], [op]), CFG)
    toks = {(u["operator"], u["specialty"]) for u in report["unmatched_specialties"]}
    assert ("X", "CNC2") in toks


def test_blank_available_hrs_defaults_two_shift():
    m = Machine("CNCX", "CNC X", "CNC lathe", available_hrs_per_day=None)
    op = Operator("O", "CNC X", machines=["CNCX"], shift="First shift")
    windows, _ = machine_windows(_masters([m], [op]), CFG)
    assert windows["CNCX"] == [FIRST]   # treated as two-shift, covered first only
