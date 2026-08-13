import pathlib
from datetime import date, datetime

from engine.config import Config
from engine.models import (Batch, Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)
from engine.loaders import normalize_process_name
from cp_engine import domain, windows


def _cfg():
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp")


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1), remaining=None):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = remaining


def test_machining_machines_are_cnc_vmc_by_id_or_type():
    assert domain.is_machining_machine(Machine("CNC1", "CNC 1", "misc"))
    assert domain.is_machining_machine(Machine("VMC2", "VMC 2", "misc"))
    assert domain.is_machining_machine(Machine("X1", "X 1", "CNC lathe"))
    assert not domain.is_machining_machine(Machine("MD1", "MD 1", "manual"))


def test_machine_options_are_always_the_allotted_suggested_union():
    """Spec §3: the solver picks the machine. Allotted first, deduped, and
    Suggested is NOT merely a fallback for a blank Allotted the way it is in
    roster_engine._candidates — that restriction is exactly what is lifted."""
    masters = Masters(machines={
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe"),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe"),
    })
    ops = domain._ops_from_processes([
        Process(1, "CNC FIRST SIDE", 5.0, None, "CNC4", "CNC1"),
    ], masters)
    assert ops[0].machine_options == ("CNC1", "CNC4")


def test_a_machine_not_in_the_master_is_dropped_from_the_options():
    masters = Masters(machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe")})
    ops = domain._ops_from_processes([
        Process(1, "CNC FIRST SIDE", 5.0, None, "CNC9", "CNC1"),
    ], masters)
    assert ops[0].machine_options == ("CNC1",)


def test_op_kind_reads_dispatch_os_and_machining():
    masters = Masters(machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe"),
                                "MD1": Machine("MD1", "MD 1", "manual")})
    ops = domain._ops_from_processes([
        Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
        Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
        Process(3, "DEBURING", 1.5, None, None, "MD1"),
        Process(4, "DISPATCH", None, None, None, None),
    ], masters)
    assert [o.kind for o in ops] == [
        "machining", "outsourced", "manual", "dispatch"]


def test_qty_for_reads_the_batch_remainder_never_a_line_remainder():
    """2026-08-11: a frozen op ran one clubbed line's 88 pieces and left the
    other line's 281 in no plan at all. The quantity is a BATCH number."""
    job = domain.Job("B1", "ITEM", 535, None, ("SO120", "SO122"), (), {3: 242})
    assert job.qty_for(3) == 242
    assert job.qty_for(1) == 535


def test_qty_for_reads_a_real_batchs_process_qty():
    """The brief's test doubles set ``process_remaining`` directly, which would
    pass even if build_jobs never read the real Batch field. A real
    ``engine.models.Batch`` never carries ``process_remaining`` — only
    ``process_qty``, keyed by NORMALIZED PROCESS NAME (2026-08-11 class of bug:
    reading only ``process_remaining`` silently plans every in-progress batch at
    its full quantity)."""
    routing = Routing("GOOD", "ok", "cust", "rm", None, [
        Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
        Process(2, "DEBURING", 1.5, None, None, "MD1"),
    ])
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe"),
                  "MD1": Machine("MD1", "MD 1", "manual")},
        routings={"GOOD": routing})
    batch = Batch(
        batch_id="B1", item_code="GOOD", item_name="Good item", qty=535,
        so_delivery_date=date(2026, 12, 1), source_so_refs=["SO120", "SO122"],
        process_qty={normalize_process_name("CNC FIRST SIDE"): 242,
                     normalize_process_name("DEBURING"): 100})

    jobs, by_key, skipped = domain.build_jobs([batch], masters)

    assert skipped == []
    job = jobs[0]
    assert job.qty_for(1) == 242
    assert job.qty_for(2) == 100


def test_build_jobs_skips_an_item_with_no_routing_instead_of_raising():
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe")},
        routings={"GOOD": Routing("GOOD", "ok", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])})
    jobs, by_key, skipped = domain.build_jobs(
        [_B("B1", "GOOD", 10), _B("B2", "MISSING", 5)], masters)
    assert [j.key for j in jobs] == ["B1"]
    assert skipped == ["MISSING"]
    assert by_key["B1"].item_code == "GOOD"


def test_shifts_are_minutes_from_plan_start_and_thursday_is_off():
    cal = WorkCalendar()                    # Thursday (weekday 3) is the weekly off
    got = windows.build_shifts(
        datetime(2026, 8, 12, 8, 0), cal, _cfg(), horizon_days=4)
    assert (got[0].day, got[0].shift, got[0].start, got[0].end) == (
        date(2026, 8, 12), "first", 0, 11 * 60)
    assert (got[1].shift, got[1].start, got[1].end) == (
        "second", 11 * 60, 21 * 60)         # 19:00 -> 05:00, crosses midnight
    assert all(w.day != date(2026, 8, 13) for w in got)   # Thursday skipped
    assert got[2].day == date(2026, 8, 14)
    assert [w.index for w in got] == list(range(len(got)))


def _covered(breaks, start, end):
    """True if every minute in [start, end) falls inside some break tuple.

    Tests the MEANING of the break list (which minutes are forbidden), not its
    representation (exact tuple boundaries) — machine_breaks is free to coalesce
    touching/overlapping tuples into fewer, wider ones as long as the forbidden
    minutes are unchanged."""
    cursor = start
    for b_start, b_end in sorted(breaks):
        if b_start > cursor:
            return False
        cursor = max(cursor, b_end)
        if cursor >= end:
            return True
    return cursor >= end


def test_a_single_shift_station_is_broken_only_outside_its_first_shift():
    """08:00-19:00, NOT the legacy 09:00-18:00 manual window — that discrepancy
    hid 9,470 minutes of real planned work from four reporting features
    (2026-08-07). One window, everywhere."""
    cal = WorkCalendar()
    shifts = windows.build_shifts(
        datetime(2026, 8, 12, 8, 0), cal, _cfg(), horizon_days=1)
    manual = Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)
    breaks = windows.machine_breaks(manual, shifts, horizon_min=1440)
    assert _covered(breaks, 11 * 60, 21 * 60)      # the second shift is unavailable
    cnc = Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)
    cnc_breaks = windows.machine_breaks(cnc, shifts, 1440)
    assert not any(b_start < 21 * 60 and b_end > 11 * 60
                   for b_start, b_end in cnc_breaks)  # none of that window is broken


def test_breaks_are_minimal_and_non_overlapping():
    """Pins the coalescing invariant a consumer relies on: pyjobshop keys a
    discrete break-duration choice per mode on start-time domains, so a
    redundant touching boundary (found empirically: every working day, for
    every single-shift machine) doubles the interval count for no reason."""
    cal = WorkCalendar()
    shifts = windows.build_shifts(
        datetime(2026, 8, 12, 8, 0), cal, _cfg(), horizon_days=5)
    manual = Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)
    breaks = windows.machine_breaks(manual, shifts, horizon_min=5 * 1440)
    assert len(breaks) > 2                        # several working days are in range
    assert breaks == sorted(breaks)
    assert all(a_end < b_start for (a_start, a_end), (b_start, b_end)
               in zip(breaks, breaks[1:]))


def test_operator_shift_reads_the_settings_row():
    assert windows.operator_shift(Operator("A", "CNC1", ["CNC1"], "2nd shift")) == "second"
    assert windows.operator_shift(Operator("B", "CNC1", ["CNC1"], "First shift")) == "first"
    assert windows.operator_shift(Operator("C", "CNC1", ["CNC1"], "")) == "first"


def test_cp_engine_never_imports_ppc_engine_or_roster_engine():
    """The rebuild stands alone so the two can be compared. roster_engine.report
    is the ONE deliberate exception (spec §8): its four rule checks are an
    independent implementation of the four rules, which is exactly what makes
    running them against the CP plan worth anything."""
    root = pathlib.Path(__file__).resolve().parent.parent / "cp_engine"
    bad = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        if "ppc_engine" in text:
            bad.append((path.name, "ppc_engine"))
        for line in text.splitlines():
            if "roster_engine" in line and "roster_engine.report" not in line:
                bad.append((path.name, line.strip()))
    assert bad == []
