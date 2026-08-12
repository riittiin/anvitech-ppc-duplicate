"""Don't begin an operation you cannot meaningfully start.

Live book, CNC7: an operation began at 18:48 — twelve minutes before the shift
ended — did 12 minutes, and then held the machine across a 37-hour gap. Setup
alone is 90 minutes, so those 12 minutes could not even finish the programming.
Starting on Friday morning instead would have finished the job in ~14 hours
rather than 88 and left CNC7 free all Wednesday evening.

The machine is RESERVED from an operation's first segment to its last (verified:
no other job ever enters the gaps), so a token start blocks the machine for the
whole stretch. Across the book that is 1,241 of 3,787 reserved CNC/VMC hours —
33% — sitting idle.

Rule: an operation may only begin in a window that can absorb at least its setup
(machining) or a minimum useful slice (everything else). Once begun it continues
normally across shift handovers, which cost nothing.
"""
from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import (Machine, MachineKind, Operator, Role,
                                         Shift)
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import flow_scheduler as FS
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools


def _shop(kind=OperationKind.MACHINING, cycle=30.0):
    machines = {"CNC1": Machine(id="CNC1", type_text="CNC lathe",
                                kind=MachineKind.MACHINING, available_hrs_per_day=19.5)}
    operators = (Operator(name="DAY", role=Role.OPERATOR,
                          qualified_machines=frozenset({"CNC1"}), base_shift=Shift.FIRST),)
    op = Operation(seq=1, name="CNC FIRST SIDE", kind=kind,
                   machine_options=("CNC1",), cycle_min=cycle)
    return Masters(machines=machines, operators=operators,
                   routings={"IT": Routing("IT", "part", (op,))}, calendar=ShopCalendar())


def _lay(masters, qty, start, cfg=None):
    cfg = cfg or PlanConfig(plan_start=start)
    order = Order(so_no="SO1", item_code="IT", item_name="part", qty=qty,
                  due_date=date(2026, 12, 1))
    op = masters.routings["IT"].operations[0]
    board = StaffingBoard(build_machine_pools(masters))
    dur = FS.operation_duration_min(op, qty, cfg)
    return FS._lay_on_machine(masters.machines["CNC1"], start, dur, order, op,
                              qty, board, masters, cfg)


class TestNoTokenStart:

    def test_it_does_not_start_12_minutes_before_the_shift_ends(self):
        """The live CNC7 case. Shift ends 19:00; 12 minutes cannot fit a 90-min setup."""
        m = _shop()
        laid = _lay(m, qty=10, start=datetime(2026, 8, 12, 18, 48))
        first = min(s.start for s in laid["segments"])
        assert first.hour >= 8 and first.date() > date(2026, 8, 12), \
            f"began a job it could not start, at {first}"

    def test_a_window_that_fits_the_setup_is_used(self):
        m = _shop()
        laid = _lay(m, qty=10, start=datetime(2026, 8, 12, 8, 0))
        assert min(s.start for s in laid["segments"]) == datetime(2026, 8, 12, 8, 0)

    def test_exactly_enough_room_for_the_setup_still_starts(self):
        """19:00 - 90 min = 17:30. Not a token start: the setup completes."""
        m = _shop()
        laid = _lay(m, qty=10, start=datetime(2026, 8, 12, 17, 30))
        assert min(s.start for s in laid["segments"]) == datetime(2026, 8, 12, 17, 30)

    def test_setup_is_still_charged_once(self):
        m = _shop()
        cfg = PlanConfig(plan_start=datetime(2026, 8, 12, 18, 48))
        laid = _lay(m, qty=10, start=cfg.plan_start, cfg=cfg)
        booked = sum((s.end - s.start).total_seconds() / 60 for s in laid["segments"])
        assert abs(booked - (cfg.setup_min + 10 * 30.0)) < 1e-6

    def test_the_work_still_completes(self):
        m = _shop()
        laid = _lay(m, qty=10, start=datetime(2026, 8, 12, 18, 48))
        assert laid is not None and laid["segments"]

    def test_a_manual_op_has_no_setup_so_a_short_window_is_fine(self):
        """No setup to strand — only the minimum useful slice applies."""
        m = _shop(kind=OperationKind.MANUAL, cycle=1.0)
        laid = _lay(m, qty=20, start=datetime(2026, 8, 12, 18, 20))
        assert min(s.start for s in laid["segments"]) == datetime(2026, 8, 12, 18, 20)

    def test_it_still_continues_across_a_shift_handover(self):
        """Only the START is restricted; continuing is free and must not change."""
        m = _shop()
        m = Masters(machines=m.machines,
                    operators=m.operators + (Operator(
                        name="NIGHT", role=Role.OPERATOR,
                        qualified_machines=frozenset({"CNC1"}), base_shift=Shift.SECOND),),
                    routings=m.routings, calendar=m.calendar)
        laid = _lay(m, qty=20, start=datetime(2026, 8, 12, 15, 0))   # 90 + 600 min
        assert len({s.operator for s in laid["segments"]}) > 1
