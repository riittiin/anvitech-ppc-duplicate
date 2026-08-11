"""The Settings operator table is AUTHORITATIVE. If an admin assigns someone to a
machine, the scheduler runs them on it — full stop.

Live bug, 2026-08-07 (owner, in front of Anvitech directors): Sandeep Kumar was
assigned CNC4 in Settings, Analytics showed him at 0%, and CNC4 sat idle with work
waiting. Root cause: ``ppc_engine`` gated the operator pool on ROLE as well as on the
assigned machine — ``o.role == ROLE_FOR_KIND[machine.kind] and mid in
o.qualified_machines`` — and role is inherited BY NAME from the workbook's operator
sheet (a FOSSIL since 2026-07-18) and never re-derived from what the admin assigned. A
person the workbook called a "helper" could therefore never be scheduled on a CNC no
matter what Settings said, and nothing anywhere reported it.

Reproduced on Test9: assigning CNC4 to Sandeep Kumar left him with 0 minutes on CNC4
and DROPPED his total work from 5,455 to 1,705 minutes — the admin's action made things
worse, silently.

Role silently overrode Settings in three places, all fixed here:
  * ppc_engine/scheduler/staffing.build_machine_pools  — who may run a machine
  * ppc_engine/loaders/loader._staffed_machines        — whether a machine counts as
    staffed at all (an unstaffed machine's orders are BLOCKED as unschedulable)
  * ppc_engine/worktime._shift_for                     — a non-operator role was forced
    to FIRST shift, ignoring the shift the admin set
"""
from datetime import date

import pytest

from ppc_engine.domain.resources import (Machine, MachineKind, Operator, Role, Shift)
from ppc_engine.scheduler.staffing import build_machine_pools


class _M:
    """Minimal masters stand-in: build_machine_pools only reads machines + operators."""
    def __init__(self, machines, operators):
        self.machines = machines
        self.operators = operators


def _machines():
    return {
        "CNC4": Machine(id="CNC4", type_text="CNC lathe",
                        kind=MachineKind.MACHINING, available_hrs_per_day=19.5),
        "MW1": Machine(id="MW1", type_text="Washing",
                       kind=MachineKind.MANUAL, available_hrs_per_day=9.5),
        "MI3": Machine(id="MI3", type_text="Inspection",
                       kind=MachineKind.INSPECTION, available_hrs_per_day=9.5),
    }


def test_settings_assignment_wins_over_the_workbook_role():
    """The reported bug. A workbook 'helper' assigned CNC4 in Settings must be in CNC4's
    pool. One person legitimately spans kinds — Sandeep Kumar runs manual stations AND
    CNC4 — which a single role can never express, so role must not gate the machine."""
    helper = Operator(name="Sandeep Kumar", role=Role.HELPER,
                      qualified_machines=frozenset({"MW1", "CNC4"}),
                      base_shift=Shift.FIRST)
    pools = build_machine_pools(_M(_machines(), (helper,)))
    assert "Sandeep Kumar" in [o.name for o in pools["CNC4"]]
    assert "Sandeep Kumar" in [o.name for o in pools["MW1"]]


def test_an_unassigned_machine_never_gets_the_operator():
    """The converse must still hold — assignment is the ONLY thing that qualifies."""
    helper = Operator(name="Sandeep Kumar", role=Role.HELPER,
                      qualified_machines=frozenset({"MW1"}), base_shift=Shift.FIRST)
    pools = build_machine_pools(_M(_machines(), (helper,)))
    assert [o.name for o in pools["CNC4"]] == []
    assert [o.name for o in pools["MI3"]] == []


def test_a_machine_is_staffed_if_anyone_is_assigned_to_it():
    """`_staffed_machines` gated on role too, so a machine staffed only by a
    role-mismatched person counted as UNSTAFFED and its orders were blocked as
    unschedulable — the same bug, with a much bigger blast radius."""
    from ppc_engine.loaders.loader import _staffed_machines
    helper = Operator(name="Sandeep Kumar", role=Role.HELPER,
                      qualified_machines=frozenset({"CNC4"}), base_shift=Shift.FIRST)
    assert "CNC4" in _staffed_machines(_M(_machines(), (helper,)))


def test_qualification_violations_reports_an_operator_off_their_machine_list():
    """The invariant that makes this class visible instead of silent. It caught nothing
    for weeks because nothing was checking — an operator ended up on CNC5 they were
    never assigned to (frozen in-progress work re-pinning a de-qualified person) and the
    plan simply shipped it."""
    from datetime import datetime as _dt
    from engine.models import ScheduleEntry
    from engine.new_engine import qualification_violations

    ok = ScheduleEntry(batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
                       machine="CNC4", qty=1, occupancy_min=60.0,
                       start=_dt(2025, 3, 3, 8, 0), end=_dt(2025, 3, 3, 9, 0), notes="",
                       so_refs=["SO1"], operator="Sandeep Kumar",
                       op_segments=[(_dt(2025, 3, 3, 8, 0), _dt(2025, 3, 3, 9, 0),
                                     "Sandeep Kumar")])
    bad = ScheduleEntry(batch_id="B2", item_code="X", process_seq=1, process_name="CNC",
                        machine="CNC5", qty=1, occupancy_min=60.0,
                        start=_dt(2025, 3, 3, 9, 0), end=_dt(2025, 3, 3, 10, 0), notes="",
                        so_refs=["SO2"], operator="Sandeep Kumar",
                        op_segments=[(_dt(2025, 3, 3, 9, 0), _dt(2025, 3, 3, 10, 0),
                                      "Sandeep Kumar")])
    masters = _M({}, (Operator(name="Sandeep Kumar", role=Role.HELPER,
                               qualified_machines=frozenset({"CNC4"}),
                               base_shift=Shift.FIRST),))

    assert qualification_violations([ok], masters) == []
    hits = qualification_violations([ok, bad], masters)
    assert len(hits) == 1
    assert hits[0]["kind"] == "OPERATOR_NOT_QUALIFIED"
    assert "CNC5" in hits[0]["ref"]


def test_the_shift_an_admin_sets_is_used_for_every_role():
    """Settings owns the shift for everyone (2026-08-05 rule). A non-operator role was
    silently forced to FIRST, so putting a helper on nights did nothing. Latent on
    Test9 — every helper/inspector happens to be first shift — but a landmine."""
    from ppc_engine.config import PlanConfig
    from ppc_engine.worktime import effective_shift
    cfg = PlanConfig(plan_start=None, week_anchor=None)
    night_helper = Operator(name="Night Helper", role=Role.HELPER,
                            qualified_machines=frozenset({"MW1"}),
                            base_shift=Shift.SECOND)
    assert effective_shift(night_helper, date(2026, 8, 10), cfg) == Shift.SECOND
