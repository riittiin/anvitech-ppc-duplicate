"""Use the part of a window an operator IS free for, instead of idling all of it.

`candidate_operator` is all-or-nothing: it needs somebody free for the whole
chunk `_lay_on_machine` wants, so one minute of conflict rejects them and the
machine skips the entire window. Live book: CNC6 on 17 Aug wanted an 11-hour
chunk and was refused because the only free operator was busy for ONE minute.

This was tried once under the old watermark model and reverted — partial slices
stretched an operation, and a stretched operation BLOCKED its machine for the
whole span. With the machine booking calendar that is no longer true: a hole
inside an operation is reusable, so a shorter slice costs nothing. Retried and
kept: 44 h -> 26.5 h of machine+operator+work idle, late-days unchanged at 350.
"""
from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.resources import (Machine, MachineKind, Operator, Role,
                                         Shift)
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools

MON = date(2026, 8, 10)


def _t(h, m=0):
    return datetime(2026, 8, 10, h, m)


def _shop(n=1, quals=("CNC1",)):
    machines = {"CNC1": Machine(id="CNC1", type_text="CNC lathe",
                                kind=MachineKind.MACHINING, available_hrs_per_day=19.5)}
    ops = tuple(Operator(name=f"OP{i+1}", role=Role.OPERATOR,
                         qualified_machines=frozenset(quals), base_shift=Shift.FIRST)
                for i in range(n))
    op = Operation(seq=1, name="CNC FIRST SIDE", kind=OperationKind.MACHINING,
                   machine_options=("CNC1",), cycle_min=30.0)
    return Masters(machines=machines, operators=ops,
                   routings={"IT": Routing("IT", "p", (op,))}, calendar=ShopCalendar())


def _cfg(**kw):
    kw.setdefault("plan_start", _t(8))
    return PlanConfig(**kw)


class TestLongestAvailablePrefix:

    def test_a_fully_free_operator_gives_the_whole_interval(self):
        m = _shop()
        b = StaffingBoard(build_machine_pools(m))
        name, end = b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m, _cfg())
        assert (name, end) == ("OP1", _t(19))

    def test_one_busy_minute_does_not_cost_the_whole_window(self):
        """The live CNC6 case."""
        m = _shop()
        b = StaffingBoard(build_machine_pools(m))
        b.commit("X", MON, Shift.FIRST, "OP1", _t(18, 59), _t(19))
        name, end = b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m, _cfg())
        assert name == "OP1" and end == _t(18, 59)

    def test_it_picks_the_longest_free_stretch(self):
        m = _shop(n=2)
        b = StaffingBoard(build_machine_pools(m))
        b.commit("X", MON, Shift.FIRST, "OP1", _t(10), _t(19))
        b.commit("X", MON, Shift.FIRST, "OP2", _t(15), _t(19))
        name, end = b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m, _cfg())
        assert name == "OP2" and end == _t(15)

    def test_busy_at_the_start_yields_nobody(self):
        m = _shop()
        b = StaffingBoard(build_machine_pools(m))
        b.commit("X", MON, Shift.FIRST, "OP1", _t(8), _t(19))
        assert b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m, _cfg())[0] is None

    def test_the_other_shift_is_not_offered(self):
        m = _shop()
        m = Masters(machines=m.machines,
                    operators=(Operator(name="N", role=Role.OPERATOR,
                                        qualified_machines=frozenset({"CNC1"}),
                                        base_shift=Shift.SECOND),),
                    routings=m.routings, calendar=m.calendar)
        b = StaffingBoard(build_machine_pools(m))
        assert b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m, _cfg())[0] is None

    def test_an_unqualified_operator_is_not_offered(self):
        m = _shop(quals=("CNC9",))
        b = StaffingBoard(build_machine_pools(m))
        assert b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m, _cfg())[0] is None

    def test_a_slice_below_the_floor_is_refused(self):
        m = _shop()
        b = StaffingBoard(build_machine_pools(m))
        b.commit("X", MON, Shift.FIRST, "OP1", _t(8, 2), _t(19))
        assert b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(19), m,
            _cfg(min_slice_min=30))[0] is None

    def test_it_never_returns_more_than_asked(self):
        m = _shop()
        b = StaffingBoard(build_machine_pools(m))
        assert b.longest_available_prefix(
            m.machines["CNC1"], MON, Shift.FIRST, _t(8), _t(12), m, _cfg())[1] <= _t(12)
