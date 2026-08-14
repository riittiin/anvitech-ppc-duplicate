"""An absent operator is never planned, through the seam.

Absences reach every engine as ``reserved={name: [(from, to)]}`` — physical
unavailability, not a promise. The seam is where they stop being an app concept
and become ``Shop.absent``; a seam that dropped them would plan work for people
on leave and nothing anywhere would say so.
"""

from datetime import date, datetime

from engine import cp_adapter
from engine.config import Config
from engine.models import (Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)


class _B:
    def __init__(self, key, item, qty):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.source_so_refs = ["SO1"], ["SO1"]
        self.delivery_date, self.process_remaining = date(2026, 12, 1), None


def _masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        routings={"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])},
        operators=[Operator("N", "CNC1", ["CNC1"], "First shift"),
                   Operator("S", "CNC1", ["CNC1"], "First shift")],
        calendar=WorkCalendar())


def _cfg():
    return Config(plan_start_date=date(2026, 8, 12), scheduler="cp",
                  setup_time_min=90.0)


def test_an_absent_operator_is_never_planned():
    """Absences are PHYSICAL unavailability. Dropping them silently plans work
    for people on leave."""
    reserved = {"N": [(datetime(2026, 8, 1), datetime(2026, 12, 1))]}
    entries = cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                             masters=_masters(), reserved=reserved)
    names = {who for e in entries for _s, _e, who in e.op_segments}
    assert "N" not in names
    assert names == {"S"}


def test_the_same_book_with_nobody_away_plans_the_absent_man():
    """NON-VACUITY. Without this, the test above passes on a fixture where "N"
    was never going to be picked anyway — the qualified pool is name-sorted, so
    N IS the free plan's choice, and only the absence moves it."""
    entries = cp_adapter.run([_B("B1", "A", 10)], config=_cfg(),
                             masters=_masters())
    names = {who for e in entries for _s, _e, who in e.op_segments}
    assert names == {"N"}


def test_a_reserved_block_that_names_no_operator_is_reported_not_swallowed():
    """``reserved`` can also carry MACHINE ids (the classic engine's two-pass
    reservations). This engine reserves people, not machines, so a key it cannot
    honour is said out loud — a constraint that quietly does nothing is this
    codebase's most expensive recurring defect."""
    notes = []
    cp_adapter.run([_B("B1", "A", 10)], config=_cfg(), masters=_masters(),
                   notes=notes,
                   reserved={"CNC1": [(datetime(2026, 8, 1),
                                       datetime(2026, 12, 1))]})
    assert any("CNC1" in note for note in notes), notes
