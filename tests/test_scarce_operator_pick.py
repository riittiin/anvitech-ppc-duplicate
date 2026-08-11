"""Scarce-first operator pick (branch ``crew-smart-scheduling``).

When several qualified operators are FREE for a shift segment, the scheduler now
spends the LEAST-FLEXIBLE person first (fewest machines on their qualification
list) instead of simply the earliest-free/sheet-order one. Flexible people stay
available for the machines only they can run, so fewer machines pause waiting
for crew. Measured on the real 71-order book (2026-07-19): makespan 78.5 -> 73.7
days on the identical batch sequence, with every shift-handoff invariant intact.

The crafted case below is the minimal failure of the old pick: a flexible
operator (qualified on CNC1+CNC2) gets grabbed for CNC1 work that a CNC1-only
specialist could do, so CNC2 — which ONLY the flexible person can run — sits
idle. Scarce-first sends the specialist to CNC1 and CNC2 starts immediately.
"""
from datetime import date, datetime

from engine.config import Config
from engine.models import Batch, Machine, Masters, Operator, Process, Routing, WorkCalendar
from engine.rules import rule6_allocate

from tests.test_shift_handoff import (assert_fully_staffed,
                                      assert_no_operator_double_booked,
                                      assert_within_shift)

WED = date(2025, 3, 5)   # a Wednesday, plans start 08:00


def _cfg(**kw):
    return Config(plan_start_date=WED, apply_operator_logic=True, **kw)


def _cnc(no):
    return Machine(machine_no=no, display_name=no, machine_type="CNC lathe",
                   hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)


def _masters():
    # "Flex" is listed FIRST (sheet order) and qualified on both machines;
    # "Narrow" is CNC1-only. The old earliest-free pick (sheet-order tie-break)
    # sent Flex to CNC1 and left CNC2 without its only operator.
    return Masters(
        machines={"CNC1": _cnc("CNC1"), "CNC2": _cnc("CNC2")},
        operators=[
            Operator(name="Flex", preferred_machines_raw="CNC1/CNC2",
                     machines=["CNC1", "CNC2"], shift="First shift"),
            Operator(name="Narrow", preferred_machines_raw="CNC1",
                     machines=["CNC1"], shift="First shift"),
        ],
        calendar=WorkCalendar(),
        routings={
            "A": Routing("A", "", "", "", None, processes=[
                Process(1, "CNC", cycle_time=1.0, total_time=None,
                        suggested_machine="CNC1", allotted_machine=None)]),
            "B": Routing("B", "", "", "", None, processes=[
                Process(1, "CNC", cycle_time=1.0, total_time=None,
                        suggested_machine="CNC2", allotted_machine=None)]),
        },
    )


def _batches():
    return [
        Batch(batch_id="B-A", item_code="A", item_name="A", qty=60,
              so_delivery_date=date(2025, 4, 30), source_so_refs=["SO-A"]),
        Batch(batch_id="B-B", item_code="B", item_name="B", qty=60,
              so_delivery_date=date(2025, 4, 30), source_so_refs=["SO-B"]),
    ]


def test_specialist_takes_the_shared_machine_so_the_exclusive_one_never_waits():
    masters = _masters()
    cfg = _cfg()
    sched = rule6_allocate.run(_batches(), config=cfg, masters=masters)
    by_machine = {e.machine: e for e in sched}
    assert set(by_machine) == {"CNC1", "CNC2"}

    # CNC2 can only be run by Flex — it must start at 08:00, not wait for Flex
    # to finish CNC1 work.
    e2 = by_machine["CNC2"]
    assert e2.start == datetime(2025, 3, 5, 8, 0), \
        f"CNC2 waited for the flexible operator: starts {e2.start}"
    assert e2.op_segments[0][2] == "Flex"

    # And the specialist covers CNC1.
    e1 = by_machine["CNC1"]
    assert e1.start == datetime(2025, 3, 5, 8, 0)
    assert e1.op_segments[0][2] == "Narrow"

    assert_no_operator_double_booked(sched)
    assert_within_shift(sched, masters, cfg)
    assert_fully_staffed(sched, masters, cfg, _batches())


def test_single_candidate_pick_unchanged():
    """With one qualified operator per machine there is no choice to make —
    the pick (and the whole schedule) is identical to the legacy behaviour."""
    masters = _masters()
    masters.operators[0] = Operator(name="Flex", preferred_machines_raw="CNC2",
                                    machines=["CNC2"], shift="First shift")
    cfg = _cfg()
    sched = rule6_allocate.run(_batches(), config=cfg, masters=masters)
    ops = {e.machine: e.op_segments[0][2] for e in sched}
    assert ops == {"CNC1": "Narrow", "CNC2": "Flex"}


def test_deterministic():
    masters = _masters()
    cfg = _cfg()
    a = rule6_allocate.run(_batches(), config=cfg, masters=masters)
    b = rule6_allocate.run(_batches(), config=cfg, masters=masters)
    assert [(e.machine, e.start, e.end, e.op_segments) for e in a] == \
           [(e.machine, e.start, e.end, e.op_segments) for e in b]


def test_flexibility_rank_counts_type_qualified_machines():
    """An operator qualified by machine TYPE (one token covering every machine
    of that type) is the FLEXIBLE one — the raw list-length rank inverted this
    (review-caught): rank must count the master's machines the entry resolves
    to, mirroring operator_coverage's id-OR-type matching."""
    masters = _masters()
    # 3 CNCs; "Typed" lists ONE token = the normalized machine type, so they can
    # run all three; "Solo" lists exactly one machine id.
    masters.machines["CNC3"] = _cnc("CNC3")
    masters.operators = [
        Operator(name="Typed", preferred_machines_raw="CNC lathe",
                 machines=["CNCLATHE"], shift="First shift"),
        Operator(name="Solo", preferred_machines_raw="CNC1",
                 machines=["CNC1"], shift="First shift"),
    ]
    ranks = rule6_allocate._operator_flexibility(masters)
    assert ranks["Typed"] == 3, ranks
    assert ranks["Solo"] == 1, ranks


def test_scheduler_fingerprint_feeds_the_staleness_signature():
    """A scheduler-semantics change (SCHEDULER_FINGERPRINT bump) must flip the
    applied-optimization staleness signature, so a deploy that changes HOW the
    scheduler books people flags the applied plan stale instead of replaying
    old ranks behind a green banner (review-caught deploy hole)."""
    import importlib
    from api import main as api_main
    from engine.config import Config as Cfg
    cfg = Cfg()
    sig_now = api_main._inputs_signature(cfg)
    orig = rule6_allocate.SCHEDULER_FINGERPRINT
    try:
        rule6_allocate.SCHEDULER_FINGERPRINT = orig + "-next"
        assert api_main._inputs_signature(cfg) != sig_now
    finally:
        rule6_allocate.SCHEDULER_FINGERPRINT = orig
    assert api_main._inputs_signature(cfg) == sig_now
