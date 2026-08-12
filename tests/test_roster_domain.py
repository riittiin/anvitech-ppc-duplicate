import pathlib
from datetime import date, datetime

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import domain, worktime


def _cfg():
    return Config(plan_start_date=date(2026, 8, 12))


def test_machining_machines_are_cnc_vmc_by_id_or_type():
    assert domain.is_machining_machine(Machine("CNC1", "CNC 1", "misc"))
    assert domain.is_machining_machine(Machine("VMC2", "VMC 2", "misc"))
    assert domain.is_machining_machine(Machine("X1", "X 1", "CNC lathe"))
    assert domain.is_machining_machine(
        Machine("X2", "X 2", "Vertical Machining center"))
    assert not domain.is_machining_machine(Machine("MD1", "MD 1", "manual"))
    assert not domain.is_machining_machine(Machine("MI1", "MI 1", "inspection"))


def test_op_kind_reads_dispatch_os_and_machining():
    masters = Masters(machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe")})
    ops = domain._ops_from_processes([
        Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
        Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
        Process(3, "DEBURING", 1.5, None, None, "MD1"),
        Process(4, "DISPATCH", None, None, None, None),
    ], masters)
    assert [o.kind for o in ops] == [
        "machining", "outsourced", "manual", "dispatch"]
    assert ops[0].machine_options == ("CNC1",)


def test_a_second_shift_window_crosses_midnight_and_thursday_is_off():
    cal = WorkCalendar()                       # Thursday (weekday 3) is the weekly off
    got = list(worktime.iter_shifts(
        datetime(2026, 8, 12, 8, 0), cal, _cfg()))[:4]
    assert (got[0].day, got[0].shift) == (date(2026, 8, 12), "first")
    assert got[0].start == datetime(2026, 8, 12, 8, 0)
    assert got[0].end == datetime(2026, 8, 12, 19, 0)
    assert (got[1].day, got[1].shift) == (date(2026, 8, 12), "second")
    assert got[1].start == datetime(2026, 8, 12, 19, 0)
    assert got[1].end == datetime(2026, 8, 13, 5, 0)     # crosses midnight
    # 2026-08-13 is a Thursday -> skipped entirely
    assert all(w.day != date(2026, 8, 13) for w in got)
    assert got[2].day == date(2026, 8, 14)


def test_a_single_shift_station_runs_only_the_first_shift_0800_1900():
    manual = Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)
    cnc = Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)
    assert worktime.machine_runs_shift(manual, "first")
    assert not worktime.machine_runs_shift(manual, "second")
    assert worktime.machine_runs_shift(cnc, "second")


def test_build_jobs_skips_an_item_with_no_routing_instead_of_raising():
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe")},
        routings={"GOOD": Routing("GOOD", "ok", "", "", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])})

    class _B:
        def __init__(self, key, item, qty):
            self.batch_id, self.item_code, self.qty = key, item, qty
            self.so_refs, self.delivery_date = ["SO1"], date(2026, 9, 1)
            self.process_remaining = None

    jobs, by_key, skipped = domain.build_jobs(
        [_B("B1", "GOOD", 10), _B("B2", "MISSING", 5)], masters)
    assert [j.key for j in jobs] == ["B1"]
    assert skipped == ["MISSING"]
    assert by_key["B1"].item_code == "GOOD"


def test_roster_engine_never_imports_ppc_engine():
    """The whole point of the rebuild: this engine stands alone, so the two can be
    compared. An import would couple them silently."""
    root = pathlib.Path(__file__).resolve().parent.parent / "roster_engine"
    offenders = [p.name for p in root.rglob("*.py")
                 if "ppc_engine" in p.read_text()]
    assert offenders == []


def test_machining_predicate_agrees_with_the_classic_setup_rule():
    """is_machining_machine is deliberately DUPLICATED rather than imported, so the
    package stands alone. Duplication drifts unless it is pinned — this asserts the
    two agree on every machine, so the 90-minute setup is charged to the same set."""
    from engine.rules.rule6_allocate import _is_setup_machine

    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe"),
        "VMC2": Machine("VMC2", "VMC 2", "Vertical Machining center"),
        "MD1": Machine("MD1", "MD 1", "manual"),
        "MI1": Machine("MI1", "MI 1", "inspection"),
        "MPK3": Machine("MPK3", "MPK 3", "packing"),
        "CMM": Machine("CMM", "CMM", "inspection"),
    }
    masters = Masters(machines=machines)
    for mid, machine in machines.items():
        assert domain.is_machining_machine(machine) == _is_setup_machine(mid, masters), mid
