"""Rule 6 — allocation: machine choice, occupancy, sequencing, machine sharing."""
from datetime import date, datetime

from engine.config import Config, OVERLAP_PERCENT
from engine.models import (
    Batch, Process, Routing, Machine, Operator, WorkCalendar, Masters, ScheduleEntry,
)
from engine.rules import rule6_allocate
from tests.sample_workbook import ITEM_A


def _batch(masters, item_code, qty=10):
    return Batch(batch_id="B1", item_code=item_code, item_name=item_code, qty=qty,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])


def test_first_process_starts_at_plan_start(loaded):
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))  # Wednesday
    sched = rule6_allocate.run([_batch(masters, ITEM_A)], config=cfg, masters=masters)
    assert sched[0].start == datetime(2025, 3, 5, 8, 0)
    # ITEM_A P1 (BANDSAW on BS1) is a MANUAL step: cycle 3 x 10 + 0 setup = 30 min
    # (the 90-min setup is charged to CNC/VMC machining only).
    assert sched[0].occupancy_min == 30


def test_setup_charged_to_cnc_vmc_only(loaded):
    """The 90-min setup (machine programming) applies to CNC/VMC steps only; manual
    stations (bandsaw, washing, inspection, etc.) occupy their station for run time
    alone."""
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(masters, ITEM_A)], config=cfg, masters=masters)
    by_seq = {e.process_seq: e for e in sched}
    # P1 BANDSAW on BS1 (manual) → no setup: 3 x 10 + 0 = 30.
    assert by_seq[1].machine == "BS1"
    assert by_seq[1].occupancy_min == 30
    # P2 CNC OS on a real CNC (CNC1/CNC2) → 90-min setup: 5 x 10 + 90 = 140.
    assert by_seq[2].machine.startswith("CNC")
    assert by_seq[2].occupancy_min == 5 * 10 + 90


def test_processes_are_sequenced_within_batch(loaded):
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(masters, ITEM_A)], config=cfg, masters=masters)
    for prev, nxt in zip(sched, sched[1:]):
        assert nxt.start >= prev.start  # later processes start no earlier


def test_overlap_starts_next_earlier_than_sequential(loaded):
    _, masters = loaded
    seq = rule6_allocate.run(
        [_batch(masters, ITEM_A)],
        config=Config(plan_start_date=date(2025, 3, 5)), masters=masters,
    )
    ov = rule6_allocate.run(
        [_batch(masters, ITEM_A)],
        config=Config(plan_start_date=date(2025, 3, 5), overlap_mode=OVERLAP_PERCENT, overlap_percent=50),
        masters=masters,
    )
    # The 2nd process should begin sooner under overlap than under sequential.
    assert ov[1].start <= seq[1].start


def _machine(mid):
    return Machine(machine_no=mid, display_name=mid, machine_type="t")


# --------------------------------------------------------------------------- #
# Per-process remaining: re-plan each process at ordered − done-at-that-step.
# --------------------------------------------------------------------------- #
def _multi_masters():
    procs = [Process(seq=i + 1, name=n, cycle_time=1, total_time=None,
                     suggested_machine=m, allotted_machine=None)
             for i, (n, m) in enumerate([("P1", "M1"), ("P2", "M2"), ("P3", "M3")])]
    routing = Routing(item_code="X", description="", customer="", rm_type="", moq=None,
                      processes=procs)
    return Masters(routings={"X": routing},
                   machines={m: _machine(m) for m in ("M1", "M2", "M3")},
                   calendar=WorkCalendar())


def _pq_batch(process_qty, qty=500):
    return Batch(batch_id="B1", item_code="X", item_name="X", qty=qty,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"],
                 process_qty=process_qty)


def test_rule6_schedules_each_process_at_its_remaining():
    masters = _multi_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))
    b = _pq_batch({"P1": 450, "P2": 480, "P3": 500})
    sched = rule6_allocate.run([b], config=cfg, masters=masters)
    assert {e.process_name: e.qty for e in sched} == {"P1": 450, "P2": 480, "P3": 500}


def test_rule6_skips_fully_done_process():
    masters = _multi_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))
    b = _pq_batch({"P1": 0, "P2": 500, "P3": 500})     # first step already complete
    sched = rule6_allocate.run([b], config=cfg, masters=masters)
    assert "P1" not in [e.process_name for e in sched]
    assert {e.process_name: e.qty for e in sched} == {"P2": 500, "P3": 500}


def test_rule6_without_process_qty_uses_full_batch_qty():
    masters = _multi_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_pq_batch(None)], config=cfg, masters=masters)
    assert all(e.qty == 500 for e in sched)            # unchanged behaviour


def test_step_with_no_machine_but_cycle_time_fails_loud_not_phantom():
    # A step with a BLANK machine but a real cycle time is a data gap — it must NOT be
    # scheduled on an invented station named after the process; it fails loud instead.
    procs = [Process(seq=1, name="CNC", cycle_time=1, total_time=None,
                     suggested_machine="M1", allotted_machine=None),
             Process(seq=2, name="MYSTERY", cycle_time=5, total_time=None,
                     suggested_machine=None, allotted_machine=None)]
    masters = Masters(routings={"X": Routing("X", "", "", "", None, processes=procs)},
                      machines={"M1": _machine("M1")}, calendar=WorkCalendar())
    b = Batch(batch_id="B1", item_code="X", item_name="X", qty=10,
              so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])
    notes = []
    sched = rule6_allocate.run([b], config=Config(plan_start_date=date(2025, 3, 5)),
                               masters=masters, notes=notes)
    used = {e.machine for e in sched}
    assert "MYSTERY" not in used and "M1" in used          # real step runs; gap does NOT
    assert any("MYSTERY" in n and "machine" in n.lower() for n in notes)   # flagged loudly


def _synthetic_masters():
    """Two machines (M, N) and two routings that contend for M.

    Batch A (priority 1): P1 on N, P2 on M — A's M-op is NOT ready until A's N-op
    finishes (at 09:40, the same moment B frees M).
    Batch B (priority 2): P1 on M — ready immediately at plan start.
    A non-delay scheduler must run B on M first (M would otherwise sit idle from
    plan start waiting for A's not-yet-ready M-op), then run A's M-op the instant
    it's ready — machine M never waits while work is available.
    """
    masters = Masters(
        machines={"M": _machine("M"), "N": _machine("N")},
        calendar=WorkCalendar(),
    )
    masters.routings["A"] = Routing(
        item_code="A", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "A-on-N", cycle_time=10, total_time=None, suggested_machine="N", allotted_machine=None),
            Process(2, "A-on-M", cycle_time=10, total_time=None, suggested_machine="M", allotted_machine=None),
        ],
    )
    masters.routings["B"] = Routing(
        item_code="B", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "short-on-M", cycle_time=10, total_time=None, suggested_machine="M", allotted_machine=None),
        ],
    )
    return masters


def test_machine_does_not_wait_when_lower_priority_op_is_ready():
    masters = _synthetic_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))  # Wednesday, 08:00 start
    a = Batch(batch_id="A", item_code="A", item_name="A", qty=1,
              so_delivery_date=date(2025, 3, 7), source_so_refs=["A"])
    b = Batch(batch_id="B", item_code="B", item_name="B", qty=1,
              so_delivery_date=date(2025, 3, 8), source_so_refs=["B"])

    sched = rule6_allocate.run([a, b], config=cfg, masters=masters)  # A is higher priority

    m_ops = sorted([e for e in sched if e.machine == "M"], key=lambda e: e.start)
    b_on_m = next(e for e in m_ops if e.batch_id == "B")
    a_on_m = next(e for e in m_ops if e.batch_id == "A")

    # B fills machine M at plan start instead of M idling until A's op is ready.
    assert b_on_m.start == datetime(2025, 3, 5, 8, 0)
    assert b_on_m.start < a_on_m.start


def test_machine_view_reports_zero_idle_when_continuous():
    masters = _synthetic_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))
    a = Batch(batch_id="A", item_code="A", item_name="A", qty=1,
              so_delivery_date=date(2025, 3, 7), source_so_refs=["A"])
    b = Batch(batch_id="B", item_code="B", item_name="B", qty=1,
              so_delivery_date=date(2025, 3, 8), source_so_refs=["B"])
    sched = rule6_allocate.run([a, b], config=cfg, masters=masters)

    timeline, summary = rule6_allocate.build_machine_view(sched, masters, cfg)
    # M runs B then A back-to-back -> the gap before A's op on M is ~0.
    m_rows = [r for r in timeline if r["Machine"] == "M"]
    a_row = next(r for r in m_rows if r["Batch"] == "A")
    assert a_row["Idle before (min)"] < 1
    m_summary = next(r for r in summary if r["Machine"] == "M")
    assert m_summary["Utilization %"] >= 99.0


def test_overlap_excludes_setup_and_skips_no_cutting_steps():
    """Under overlap: a machining step's successor starts after % of its CUTTING
    only (setup excluded); a no-cutting finishing step does NOT overlap — its
    successor waits for it to fully complete."""
    masters = Masters(
        machines={"M": _machine("M"), "N": _machine("N"), "O": _machine("O")},
        calendar=WorkCalendar(),
    )
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "machine", cycle_time=10, total_time=None, suggested_machine="M", allotted_machine=None),
            Process(2, "deburr", cycle_time=None, total_time=None, suggested_machine="N", allotted_machine=None),
            Process(3, "inspect", cycle_time=None, total_time=None, suggested_machine="O", allotted_machine=None),
        ],
    )
    cfg = Config(plan_start_date=date(2025, 3, 5),
                 overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    batch = Batch(batch_id="X1", item_code="X", item_name="X", qty=10,
                  so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])
    sched = rule6_allocate.run([batch], config=cfg, masters=masters)
    p1 = next(e for e in sched if e.process_seq == 1)  # machining (cutting 100 + setup 90)
    p2 = next(e for e in sched if e.process_seq == 2)  # finishing (no cutting)
    p3 = next(e for e in sched if e.process_seq == 3)  # finishing (no cutting)

    # P2 (after a machining step) overlaps: starts before P1 fully finishes.
    assert p2.start < p1.end
    # P3 (after a no-cutting step) does NOT overlap: starts when P2 fully ends.
    assert p3.start == p2.end


def test_no_routing_raises_rule_error(loaded):
    _, masters = loaded
    from engine.pipeline import RuleError
    bad = Batch(batch_id="BX", item_code="DOES-NOT-EXIST", item_name="x", qty=1,
                so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])
    try:
        rule6_allocate.run([bad], config=Config(plan_start_date=date(2025, 3, 1)),
                           masters=masters)
        assert False, "expected RuleError"
    except RuleError as e:
        assert e.rule == "rule6"


def test_seeded_machine_lost_time_delays_first_op():
    """machine_lost_min seeds a machine as unavailable for N working-minutes, so its
    first op starts that much later (downtime looping back into the schedule)."""
    masters = _synthetic_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))  # Wednesday, 08:00 start
    b = Batch(batch_id="B", item_code="B", item_name="B", qty=1,
              so_delivery_date=date(2025, 3, 8), source_so_refs=["B"])  # single op on M

    base = rule6_allocate.run([b], config=cfg, masters=masters)
    seeded = rule6_allocate.run([b], config=cfg, masters=masters, machine_lost_min={"M": 120})

    assert base[0].start == datetime(2025, 3, 5, 8, 0)        # M free at plan start
    assert seeded[0].start == datetime(2025, 3, 5, 10, 0)     # M held 120 working-min


def test_machine_lost_min_default_is_noop():
    masters = _synthetic_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))
    b = Batch(batch_id="B", item_code="B", item_name="B", qty=1,
              so_delivery_date=date(2025, 3, 8), source_so_refs=["B"])
    no_kw = rule6_allocate.run([b], config=cfg, masters=masters)
    explicit_none = rule6_allocate.run([b], config=cfg, masters=masters, machine_lost_min=None)
    assert [(e.machine, e.start, e.end) for e in no_kw] == \
           [(e.machine, e.start, e.end) for e in explicit_none]


def _alt_masters(suggested="M/N"):
    """Two machines M, N and one item 'X' whose single process may run on either."""
    masters = Masters(machines={"M": _machine("M"), "N": _machine("N")},
                      calendar=WorkCalendar())
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "OP", cycle_time=10, total_time=None,
                           suggested_machine=suggested, allotted_machine=None)],
    )
    return masters


def _xbatch(bid="X1"):
    return Batch(batch_id=bid, item_code="X", item_name="X", qty=1,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=[bid])


def test_alternative_machine_avoids_the_busy_one():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    masters = _alt_masters("M/N")
    # M is held busy by recorded downtime -> the op must take N.
    sched = rule6_allocate.run([_xbatch()], config=cfg, masters=masters,
                               machine_lost_min={"M": 600})
    assert sched[0].machine == "N"


def test_alternative_machine_prefers_first_listed_when_both_free():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    assert rule6_allocate.run([_xbatch()], config=cfg, masters=_alt_masters("M/N"))[0].machine == "M"
    assert rule6_allocate.run([_xbatch()], config=cfg, masters=_alt_masters("N/M"))[0].machine == "N"


def test_alternative_machines_load_balance_across_contending_ops():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    masters = _alt_masters("M/N")
    sched = rule6_allocate.run([_xbatch("A"), _xbatch("B")], config=cfg, masters=masters)
    assert {e.machine for e in sched} == {"M", "N"}   # split, not both on M


def test_alternative_machine_choice_is_noted():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    alt = rule6_allocate.run([_xbatch()], config=cfg, masters=_alt_masters("M/N"))[0]
    assert "M" in alt.notes and "N" in alt.notes and "chose" in alt.notes.lower()
    single = rule6_allocate.run([_xbatch()], config=cfg, masters=_alt_masters("M"))[0]
    assert single.notes == ""        # non-alternative ops stay un-noted


def test_seeded_machine_does_not_affect_other_machines():
    """Lost time on machine M must NOT delay an operation that runs on machine N
    (isolation — only the affected machine's queue slips)."""
    masters = _synthetic_masters()       # routing A: op1 on N, op2 on M
    cfg = Config(plan_start_date=date(2025, 3, 5))
    a = Batch(batch_id="A", item_code="A", item_name="A", qty=1,
              so_delivery_date=date(2025, 3, 7), source_so_refs=["A"])

    base = rule6_allocate.run([a], config=cfg, masters=masters)
    seeded = rule6_allocate.run([a], config=cfg, masters=masters, machine_lost_min={"M": 180})
    n_base = next(e for e in base if e.machine == "N")
    n_seed = next(e for e in seeded if e.machine == "N")
    assert n_seed.start == n_base.start    # the N op is untouched by M's lost time


def test_machine_view_includes_item_description(loaded):
    # The machine-wise timeline shows the item's description next to its code.
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(masters, ITEM_A)], config=cfg, masters=masters)
    timeline, _ = rule6_allocate.build_machine_view(sched, masters, cfg)
    assert timeline, "expected machine-view rows"
    row = timeline[0]
    assert "Item Description" in row                     # new column present
    assert row["Item Description"] == masters.routings[row["Item Code"]].description


def test_machine_view_includes_so_date_completion_and_qty(loaded):
    # Each machine-view row also shows the order's SO delivery date, its expected
    # completion (latest end across the order), and the pieces produced in that op.
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))
    batch = _batch(masters, ITEM_A, qty=10)
    sched = rule6_allocate.run([batch], config=cfg, masters=masters)
    timeline, _ = rule6_allocate.build_machine_view(sched, masters, cfg, [batch])
    row = timeline[0]
    assert row["SO Del date"] == date(2025, 3, 7)                     # from the order
    assert row["Expected completion"] == max(e.end for e in sched).date()
    assert row["Qty"] > 0                                             # pieces in this op


# --- Expedite window (least-slack tie-break within a small window) ----------- #

def _operator_contention_masters():
    """Two machines M, N sharing ONE operator OP1 (first shift), so only one of the
    two can run at a time. Each of two batches has a single op — LESS on M, MORE on
    N — both ready at plan start. N is briefly busy so MORE is feasible a few minutes
    AFTER LESS: pure non-delay then gives M+OP1 to the less-urgent LESS first, and the
    urgent MORE waits behind it. The expedite window lets MORE (least slack) take OP1
    first even though it is a few minutes later."""
    M = Machine(machine_no="M", display_name="M", machine_type="mill",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    N = Machine(machine_no="N", display_name="N", machine_type="mill",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    op1 = Operator(name="OP1", preferred_machines_raw="M, N", machines=["M", "N"],
                   shift="First shift")
    masters = Masters(machines={"M": M, "N": N}, operators=[op1], calendar=WorkCalendar())
    masters.routings["LESS"] = Routing(
        item_code="LESS", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "less-on-M", cycle_time=10, total_time=None,
                           suggested_machine="M", allotted_machine=None)],
    )
    masters.routings["MORE"] = Routing(
        item_code="MORE", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "more-on-N", cycle_time=10, total_time=None,
                           suggested_machine="N", allotted_machine=None)],
    )
    return masters


def _contention_batches():
    # LESS: 12 pcs -> 120 min block if it grabs OP1 first. Due far out (low urgency).
    less = Batch(batch_id="LESS", item_code="LESS", item_name="LESS", qty=12,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["LESS"])
    # MORE: 1 pc. Due soon (high urgency / least slack).
    more = Batch(batch_id="MORE", item_code="MORE", item_name="MORE", qty=1,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=["MORE"])
    return less, more


def _more_start(cfg):
    masters = _operator_contention_masters()
    less, more = _contention_batches()
    # N briefly busy (5 working-min) so MORE is feasible just after LESS -> a near-tie.
    sched = rule6_allocate.run([less, more], config=cfg, masters=masters,
                               machine_lost_min={"N": 5})
    return next(e for e in sched if e.batch_id == "MORE").start


def test_expedite_window_pulls_urgent_order_ahead_of_a_near_tie():
    base_cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    exp_cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True,
                     expedite_window_min=15)
    baseline_start = _more_start(base_cfg)
    expedite_start = _more_start(exp_cfg)
    # Without the window, urgent MORE waits behind the 120-min LESS block on OP1.
    assert baseline_start == datetime(2025, 3, 5, 10, 0)
    # With a 15-min window, MORE (least slack) takes OP1 first -> starts ~08:05.
    assert expedite_start < baseline_start
    assert expedite_start == datetime(2025, 3, 5, 8, 5)


def test_expedite_window_default_is_noop():
    """Default config (expedite_window_min=0) reproduces the legacy non-delay plan
    exactly, so the golden trace and existing plans are unchanged."""
    off = _more_start(Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True))
    explicit_zero = _more_start(Config(plan_start_date=date(2025, 3, 5),
                                       apply_operator_logic=True, expedite_window_min=0))
    assert off == explicit_zero == datetime(2025, 3, 5, 10, 0)


def test_expedite_window_negative_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        Config(expedite_window_min=-1).validate()


# --- Operator load balancing (fairness post-process, schedule-neutral) -------- #

def _op_pool_masters():
    """Two machines M, N; two operators OP1, OP2 both qualified for both, first shift."""
    M = Machine(machine_no="M", display_name="M", machine_type="mill",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    N = Machine(machine_no="N", display_name="N", machine_type="mill",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    op1 = Operator(name="OP1", preferred_machines_raw="M,N", machines=["M", "N"], shift="First shift")
    op2 = Operator(name="OP2", preferred_machines_raw="M,N", machines=["M", "N"], shift="First shift")
    return Masters(machines={"M": M, "N": N}, operators=[op1, op2], calendar=WorkCalendar())


def _entry(bid, machine, start, end, operator):
    return ScheduleEntry(batch_id=bid, item_code=bid, process_seq=1, process_name="op",
                         machine=machine, qty=1,
                         occupancy_min=(end - start).total_seconds() / 60,
                         start=start, end=end, notes="", so_refs=[bid], operator=operator)


def test_rebalance_operators_spreads_load_and_preserves_timing():
    """Two back-to-back ops both landed on OP1 while OP2 sat idle. Rebalancing hands
    one to OP2 (fair) WITHOUT moving any start/end time (schedule-neutral)."""
    masters = _op_pool_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    h = lambda hr: datetime(2025, 3, 5, hr, 0)
    sched = [_entry("A", "M", h(8), h(9), "OP1"),
             _entry("B", "M", h(9), h(10), "OP1")]
    before = [(e.batch_id, e.machine, e.start, e.end) for e in sched]

    rule6_allocate._rebalance_operators(sched, masters, cfg)

    after = [(e.batch_id, e.machine, e.start, e.end) for e in sched]
    assert before == after                                   # timing untouched
    assert {e.operator for e in sched} == {"OP1", "OP2"}     # load now spread


def test_rebalance_never_double_books_an_operator():
    """Two OVERLAPPING ops must end up on different people (one can't run two at once)."""
    masters = _op_pool_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    h = lambda hr: datetime(2025, 3, 5, hr, 0)
    sched = [_entry("A", "M", h(8), h(11), "OP1"),
             _entry("B", "N", h(9), h(10), "OP1")]   # overlaps A
    rule6_allocate._rebalance_operators(sched, masters, cfg)
    assert sched[0].operator != sched[1].operator


def test_balance_operator_load_defaults_off_and_validates():
    assert Config().balance_operator_load is False
    import pytest
    bad = Config()
    bad.balance_operator_load = "yes"
    with pytest.raises(ValueError):
        bad.validate()


# --- Shift-wise timeline (split each op into per-shift segments with the real op) --- #

def _shiftwise_masters():
    M = Machine(machine_no="M", display_name="M", machine_type="CNC lathe",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)   # two-shift
    op1 = Operator(name="DayOp", preferred_machines_raw="M", machines=["M"], shift="First shift")
    op2 = Operator(name="NightOp", preferred_machines_raw="M", machines=["M"], shift="Second shift")
    m = Masters(machines={"M": M}, operators=[op1, op2], calendar=WorkCalendar())
    m.routings["X"] = Routing(item_code="X", description="WIDGET", customer="", rm_type="",
                              moq=None, processes=[])
    return m


def test_shiftwise_splits_a_multi_shift_op_by_shift_with_real_operator():
    masters = _shiftwise_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    # One op on M from 08:00 to next day 02:00 → spans 1st shift (08–19) + 2nd shift (19–05).
    e = ScheduleEntry(batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
                      machine="M", qty=100, occupancy_min=1080,
                      start=datetime(2025, 3, 5, 8, 0), end=datetime(2025, 3, 6, 2, 0),
                      notes="", so_refs=["SO1"], operator="DayOp")
    b = Batch(batch_id="B1", item_code="X", item_name="X", qty=100,
              so_delivery_date=date(2025, 3, 20), source_so_refs=["SO1"])

    rows = rule6_allocate.build_shiftwise_timeline([e], masters, cfg, [b])

    assert len(rows) == 2                                            # split into two shifts
    first, second = rows[0], rows[1]
    assert first["Shift"] == "First shift" and first["Operator"] == "DayOp"
    assert second["Shift"] == "Second shift" and second["Operator"] == "NightOp"
    assert first["Item Description"] == "WIDGET"                     # carries full detail
    # the segment minutes cover only working shift time (no 05:00–08:00 gap double-count)
    assert first["Minutes"] == 11 * 60                              # 08:00–19:00
    # New 'Qty this shift' column (owner, 2026-07-27) via the legacy path: pieces split
    # per shift by working minutes, whole numbers summing to the order total.
    assert first["Qty this shift"] + second["Qty this shift"] == 100


def test_shiftwise_start_column_carries_the_date_like_end():
    """Start must show DD-MM HH:MM (date + time), matching End, so a sheet spanning
    several days is unambiguous (owner request for the director review, 2026-07-27)."""
    masters = _shiftwise_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    s = datetime(2025, 3, 5, 8, 0)
    handoff = datetime(2025, 3, 5, 19, 0)
    en = datetime(2025, 3, 6, 5, 0)
    e = ScheduleEntry(batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
                      machine="M", qty=100, occupancy_min=1260,
                      start=s, end=en, notes="", so_refs=["SO1"], operator="DayOp",
                      op_segments=[(s, handoff, "DayOp"), (handoff, en, "NightOp")])
    b = Batch(batch_id="B1", item_code="X", item_name="X", qty=100,
              so_delivery_date=date(2025, 3, 20), source_so_refs=["SO1"])
    rows = rule6_allocate.build_shiftwise_timeline([e], masters, cfg, [b])
    assert rows[0]["Start"] == "05-03 08:00"       # date + time, like End
    assert rows[0]["End"] == "05-03 19:00"
    assert rows[1]["Start"] == "05-03 19:00"       # night shift starts 05-03 19:00…
    assert rows[1]["End"] == "06-03 05:00"         # …and crosses midnight — the date shows it


def test_shiftwise_qty_this_shift_splits_pieces_per_window_and_sums_to_total():
    """New 'Qty this shift' column (owner, 2026-07-27): pieces expected in THAT
    operator's window, split proportional to working minutes as whole numbers that add
    up to the order total. The existing 'Qty' (order total) is kept alongside."""
    masters = _shiftwise_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    s = datetime(2025, 3, 5, 8, 0)
    handoff = datetime(2025, 3, 5, 19, 0)          # 08:00–19:00 = 660 min
    en = datetime(2025, 3, 6, 5, 0)                # 19:00–05:00 = 600 min; occupancy 1260
    e = ScheduleEntry(batch_id="B1", item_code="X", process_seq=1, process_name="CNC",
                      machine="M", qty=100, occupancy_min=1260,
                      start=s, end=en, notes="", so_refs=["SO1"], operator="DayOp",
                      op_segments=[(s, handoff, "DayOp"), (handoff, en, "NightOp")])
    b = Batch(batch_id="B1", item_code="X", item_name="X", qty=100,
              so_delivery_date=date(2025, 3, 20), source_so_refs=["SO1"])
    rows = rule6_allocate.build_shiftwise_timeline([e], masters, cfg, [b])
    assert [r["Qty"] for r in rows] == [100, 100]                   # order total column, unchanged
    assert rows[0]["Qty this shift"] == 52                          # 100 * 660/1260 = 52.4 -> 52
    assert rows[1]["Qty this shift"] == 48                          # remainder
    assert sum(r["Qty this shift"] for r in rows) == 100            # shifts add up to the total
