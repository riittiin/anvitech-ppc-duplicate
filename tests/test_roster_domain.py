import pathlib
from datetime import date, datetime

from engine.config import Config
from engine.models import Batch, Machine, Masters, Operator, Process, Routing, WorkCalendar
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


def _job(**overrides):
    base = dict(key="B1", item_code="GOOD", qty=254, due=date(2026, 9, 1),
                so_refs=("SO1",), ops=(), remaining=None)
    base.update(overrides)
    return domain.Job(**base)


def test_qty_for_with_no_remaining_returns_the_full_batch_qty_for_any_step():
    job = _job(remaining=None)
    assert job.qty_for(1) == 254
    assert job.qty_for(99) == 254


def test_qty_for_with_populated_remaining_returns_the_per_step_value():
    job = _job(remaining={1: 88, 2: 254})
    assert job.qty_for(1) == 88
    assert job.qty_for(2) == 254


def test_qty_for_falls_back_to_full_batch_qty_when_a_step_is_absent_from_remaining():
    """A clubbed order's step with no entry in `remaining` (nothing punched on it
    yet) owes the full batch qty, not zero — this is the exact class of bug that
    dropped 281 pieces of a clubbed order into no plan at all (2026-08-11)."""
    job = _job(qty=254, remaining={1: 88})
    assert job.qty_for(2) == 254


def test_qty_for_returns_zero_when_a_step_is_fully_complete():
    job = _job(remaining={1: 0})
    assert job.qty_for(1) == 0


def test_build_jobs_translates_name_keyed_process_qty_to_seq_keyed_remaining():
    """Real engine.models.Batch keys process_qty by NORMALIZED PROCESS NAME, not
    op_seq — build_jobs must translate it via the routing using the exact same
    normalizer the order book used to key it (engine.loaders.normalize_process_name),
    exactly as engine.new_engine._orders_from_batches does. A multi-word process
    name ('CNC FIRST SIDE') is the case a wrong normalizer (e.g. one that strips
    spaces instead of collapsing them) silently drops."""
    routing = Routing("ITEM1", "desc", "cust", "steel", None, [
        # Irregular casing/spacing straight off the sheet -- must normalize to the
        # SAME key process_qty is stored under, or the lookup below silently misses.
        Process(1, "Cnc  First Side", 5.0, None, None, "CNC1"),
        Process(2, "cnc second side", 5.0, None, None, "CNC1"),
        Process(3, "DEBURING", 1.5, None, None, "MD1"),
    ])
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe"),
                  "MD1": Machine("MD1", "MD 1", "manual")},
        routings={"ITEM1": routing})
    batch = Batch(
        batch_id="26-27SO120+26-27SO122", item_code="ITEM1", item_name="widget",
        qty=535, so_delivery_date=date(2026, 9, 1),
        source_so_refs=["26-27SO120", "26-27SO122"],
        # process_qty is keyed by NORMALIZED process name (engine.models.Batch's own
        # documented contract) -- exercising the multi-word key is the point: a
        # normalizer that strips spaces instead of collapsing them would produce
        # "CNCFIRSTSIDE"/"CNCSECONDSIDE" and never match these routing steps.
        process_qty={
            "CNC FIRST SIDE": 88,    # 254 (SO120's own remainder) already made elsewhere
            "CNC SECOND SIDE": 535,  # nothing made yet on this step
        })

    jobs, by_key, skipped = domain.build_jobs([batch], masters)

    assert skipped == []
    job = jobs[0]
    assert job.qty_for(1) == 88          # CNC FIRST SIDE: per-step remaining
    assert job.qty_for(2) == 535         # CNC SECOND SIDE: multi-word step survives
    assert job.qty_for(3) == 535         # DEBURING: absent from process_qty -> full qty


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
