"""Parallel batch splitting for the NEW engine (ppc_engine).

The classic engine's equivalent (Rule 6 `split_parallel`) is covered by
tests/test_parallel_split.py. The new engine had no splitting at all, so the
setting silently did nothing once production moved to scheduler='new'.

A long operation whose routing allows several machines runs on ONE of them start
to finish. On the live book that is 35 operations and 712 machine-hours — the
biggest a 51.5-hour job on CNC3 while CNC6 sits idle and allowed.

MEASURED RESULT: splitting them made the plan WORSE (397 -> 401 late-days), so it
ships OFF. Those machines are idle for lack of an OPERATOR, not for lack of work:
CNC3/CNC6/CNC7 are three machines run by two day operators and one night
operator. Splitting spends those hands faster and starves the third machine. The
capability is kept and tested because the calculus flips in any cell that has
more hands than machines.

Splitting it costs one extra setup per extra machine, so it only pays when each
part carries enough work to be worth its own setup. That trade-off is the whole
design: `split_ways()` decides how many ways to split, and returns 1 when
splitting would be a waste.

Hard rules that splitting must not break:
  - one operator mans one machine for the whole shift (RULES.md Rule 1)
  - the same person is never on two machines at once
  - the operation is finished only when its LAST part finishes
"""
from datetime import date, datetime, timedelta

import pytest

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import (Machine, MachineKind, Operator, Role,
                                         Shift)
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import flow_scheduler as FS
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools

MON = date(2026, 8, 10)          # a working Monday


# --------------------------------------------------------------------------- #
# The economics: how many ways is it worth splitting?
# --------------------------------------------------------------------------- #

class TestSplitWays:
    """`split_ways(work_min, setup_min, machines, ratio, max_ways) -> int`.

    Returns 1 (don't split) unless every part would carry at least
    ``ratio × setup`` minutes of real cutting.
    """

    def test_a_short_job_is_never_split(self):
        # 10 minutes of work, 90-minute setup. Splitting is absurd.
        assert FS.split_ways(10, 90, machines=2) == 1

    def test_a_job_barely_longer_than_the_setup_is_not_split(self):
        # 120 min work: each half carries 60 min, less than one setup. Not worth it.
        assert FS.split_ways(120, 90, machines=2) == 1

    def test_a_long_job_splits_in_two(self):
        # 400 min work: each half carries 200 min > 2×90. Worth it.
        assert FS.split_ways(400, 90, machines=2) == 2

    def test_split_is_capped_by_available_machines(self):
        assert FS.split_ways(10_000, 90, machines=2) == 2

    def test_a_very_long_job_uses_more_machines_when_allowed(self):
        # 3000 min over 3 machines = 1000 each, comfortably above 2×90.
        assert FS.split_ways(3000, 90, machines=3) == 3

    def test_ratio_controls_how_eagerly_it_splits(self):
        # 400 min, 2 machines -> 200 per part. ratio 2.0 allows it (needs 180)...
        assert FS.split_ways(400, 90, machines=2, ratio=2.0) == 2
        # ...ratio 3.0 does not (would need 270 per part).
        assert FS.split_ways(400, 90, machines=2, ratio=3.0) == 1

    def test_zero_setup_still_respects_the_machine_cap(self):
        assert FS.split_ways(400, 0, machines=2) == 2

    def test_one_machine_means_no_split(self):
        assert FS.split_ways(10_000, 90, machines=1) == 1

    def test_never_returns_less_than_one(self):
        assert FS.split_ways(0, 90, machines=3) == 1


# --------------------------------------------------------------------------- #
# The staffing board must be cloneable, or a split double-books people
# --------------------------------------------------------------------------- #

class TestStaffingClone:
    """Laying part B has to see part A's operator as taken. The board is
    read-only during placement, so the split path needs a scratch copy."""

    def test_clone_starts_equal(self):
        b = StaffingBoard()
        b.commit("CNC3", MON, Shift.FIRST, "Sidhu", datetime(2026, 8, 10, 8),
                 datetime(2026, 8, 10, 12))
        c = b.clone()
        assert not c.free_during("Sidhu", datetime(2026, 8, 10, 9),
                                 datetime(2026, 8, 10, 10))

    def test_writing_to_the_clone_does_not_touch_the_original(self):
        b = StaffingBoard()
        c = b.clone()
        c.commit("CNC6", MON, Shift.FIRST, "Rohan", datetime(2026, 8, 10, 8),
                 datetime(2026, 8, 10, 12))
        assert b.free_during("Rohan", datetime(2026, 8, 10, 8),
                             datetime(2026, 8, 10, 12))
        assert not c.free_during("Rohan", datetime(2026, 8, 10, 8),
                                 datetime(2026, 8, 10, 12))

    def test_writing_to_the_original_does_not_touch_the_clone(self):
        b = StaffingBoard()
        c = b.clone()
        b.commit("CNC6", MON, Shift.FIRST, "Rohan", datetime(2026, 8, 10, 8),
                 datetime(2026, 8, 10, 12))
        assert c.free_during("Rohan", datetime(2026, 8, 10, 8),
                             datetime(2026, 8, 10, 12))

    def test_clone_keeps_the_machine_pools(self):
        m = _shop()
        b = StaffingBoard(build_machine_pools(m))
        assert b.clone()._pools == b._pools


# --------------------------------------------------------------------------- #
# Placement behaviour
# --------------------------------------------------------------------------- #

def _shop(n_machines=2, n_operators=3, cycle=30.0):
    """Default shop has a SPARE operator (3 people, 2 machines) so the split path
    is reachable — see TestOperatorSlackGuard for why a spare is required."""
    machines = {f"CNC{i+1}": Machine(id=f"CNC{i+1}", type_text="CNC lathe",
                                     kind=MachineKind.MACHINING,
                                     available_hrs_per_day=19.5)
                for i in range(n_machines)}
    quals = frozenset(machines)
    operators = tuple(Operator(name=f"OP{i+1}", role=Role.OPERATOR,
                               qualified_machines=quals, base_shift=Shift.FIRST)
                      for i in range(n_operators))
    op = Operation(seq=1, name="CNC FIRST SIDE", kind=OperationKind.MACHINING,
                   machine_options=tuple(machines), cycle_min=cycle)
    routings = {"ITEM": Routing("ITEM", "test part", (op,))}
    return Masters(machines=machines, operators=operators, routings=routings,
                   calendar=ShopCalendar())


def _cfg(**kw):
    # Splitting is OFF in production (measured worse on the live book — see
    # PlanConfig.split_enabled). These tests exercise the capability, so they turn
    # it on explicitly; `test_off_by_default` pins the production default.
    kw.setdefault("plan_start", datetime(2026, 8, 10, 8))
    kw.setdefault("split_enabled", True)
    return PlanConfig(**kw)


def _place(masters, qty, cfg=None):
    cfg = cfg or _cfg()
    order = Order(so_no="SO1", item_code="ITEM", item_name="test part",
                  qty=qty, due_date=date(2026, 12, 1))
    op = masters.routings["ITEM"].operations[0]
    board = StaffingBoard(build_machine_pools(masters))
    return FS._place_operation(op, order, cfg.plan_start, {}, board, masters, cfg)


class TestSplitPlacement:

    def test_a_long_operation_runs_on_two_machines_at_once(self):
        m = _shop()
        p = _place(m, qty=40)                    # 40 x 30 min = 1200 min of work
        used = {s.machine_id for s in p["segments"] if s.machine_id}
        assert len(used) == 2, f"expected a 2-way split, got {used}"

    def test_each_part_has_its_own_operator(self):
        """The rule that matters: one person cannot man two machines at once."""
        m = _shop()
        p = _place(m, qty=40)
        by_machine = {}
        for s in p["segments"]:
            if s.machine_id and s.operator:
                by_machine.setdefault(s.machine_id, set()).add(s.operator)
        assert len(by_machine) == 2
        a, b = by_machine.values()
        assert not (a & b), f"same operator on both machines: {a & b}"

    def test_splitting_roughly_halves_the_elapsed_time(self):
        m = _shop()
        one = _place(_shop(n_machines=1, n_operators=1), qty=40)
        two = _place(m, qty=40)
        assert two["end"] < one["end"]

    def test_a_short_operation_is_not_split(self):
        m = _shop(cycle=1.0)
        p = _place(m, qty=10)                    # 10 minutes of work
        used = {s.machine_id for s in p["segments"] if s.machine_id}
        assert len(used) == 1

    def test_the_operation_ends_when_its_LAST_part_ends(self):
        m = _shop()
        p = _place(m, qty=40)
        assert p["end"] == max(s.end for s in p["segments"])

    def test_every_piece_is_still_scheduled(self):
        """`qty` is repeated on each time-window segment of a part, not divided,
        so the parts' quantities are summed per MACHINE — one figure per part."""
        m = _shop()
        p = _place(m, qty=40)
        per_machine = {}
        for s in p["segments"]:
            if s.machine_id:
                per_machine[s.machine_id] = s.qty
        assert sum(per_machine.values()) == 40, per_machine

    def test_an_odd_quantity_is_split_without_losing_a_piece(self):
        m = _shop()
        p = _place(m, qty=41)
        per_machine = {}
        for s in p["segments"]:
            if s.machine_id:
                per_machine[s.machine_id] = s.qty
        assert sum(per_machine.values()) == 41, per_machine

    def test_a_single_option_operation_never_splits(self):
        m = _shop(n_machines=1, n_operators=2)
        p = _place(m, qty=40)
        assert len({s.machine_id for s in p["segments"] if s.machine_id}) == 1

    def test_no_split_when_only_one_operator_is_available(self):
        """Two machines but one person — splitting is physically impossible."""
        m = _shop(n_machines=2, n_operators=1)
        p = _place(m, qty=40)
        assert len({s.machine_id for s in p["segments"] if s.machine_id}) == 1

    def test_splitting_never_finishes_later_than_not_splitting(self):
        for qty in (5, 20, 40, 200):
            solo = _place(_shop(n_machines=1, n_operators=1), qty=qty)
            both = _place(_shop(n_machines=2, n_operators=2), qty=qty)
            assert both["end"] <= solo["end"], f"split lost at qty={qty}"

    def test_disabled_by_config(self):
        m = _shop()
        p = _place(m, qty=40, cfg=_cfg(split_enabled=False))
        assert len({s.machine_id for s in p["segments"] if s.machine_id}) == 1

    def test_off_by_default_in_production(self):
        """Production must not split: it measured worse on the live book."""
        assert PlanConfig(plan_start=datetime(2026, 8, 10, 8)).split_enabled is False

    def test_the_placement_reports_every_machine_it_used(self):
        """The caller advances machine_free per machine; it needs them all."""
        m = _shop()
        p = _place(m, qty=40)
        assert set(p["machine_ends"]) == {"CNC1", "CNC2"}
        for mid, end in p["machine_ends"].items():
            assert end <= p["end"]


# --------------------------------------------------------------------------- #
# Splitting must not consume the last free operator
# --------------------------------------------------------------------------- #

class TestOperatorSlackGuard:
    """Measured on the live book: CNC3/CNC6/CNC7 share two day operators. A 2-way
    split took both and left CNC7 unmanned — late-days went 397 -> 421. Splitting
    is only free capacity when the group has MORE hands than parts."""

    def test_no_split_when_operators_exactly_equal_the_parts(self):
        m = _shop(n_machines=2, n_operators=2)     # 2 people, a 2-way split needs both
        p = _place(m, qty=40)
        assert len({s.machine_id for s in p["segments"] if s.machine_id}) == 1

    def test_splits_when_a_spare_operator_remains(self):
        m = _shop(n_machines=2, n_operators=3)     # 3 people, 2 parts -> one spare
        p = _place(m, qty=40)
        assert len({s.machine_id for s in p["segments"] if s.machine_id}) == 2

    def test_still_no_double_booking_with_a_spare(self):
        m = _shop(n_machines=2, n_operators=3)
        p = _place(m, qty=40)
        per = {}
        for s in p["segments"]:
            if s.machine_id and s.operator:
                per.setdefault(s.machine_id, set()).add(s.operator)
        a, b = per.values()
        assert not (a & b)
