import io
from datetime import date
from dataclasses import replace as _dc_replace
import pytest

from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from engine import book_store, loaders, orderbook
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import decode, FrozenOp
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)

@pytest.fixture()
def ctx():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, nm)
    seq = [o.key for o in orders]
    return orders, seq, nm

def test_decode_frozen_none_is_byte_identical(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    a = decode(orders, seq, nm, cfg)
    b = decode(orders, seq, nm, cfg, frozen=None)
    c = decode(orders, seq, nm, cfg, frozen=[])
    assert a.segments == b.segments == c.segments
    assert a.completion == b.completion == c.completion


def _entry(segs, order_key, op_seq):
    got = [s for s in segs if s.order_key == order_key and s.op_seq == op_seq]
    return sorted(got, key=lambda s: s.start)


def test_single_frozen_op_pinned_to_machine_operator_no_setup(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # Pick the first order's first machining op; find a machine option it has.
    o0 = orders[0]
    routing = nm.routings[o0.item_code]
    mach_op = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    mid = mach_op.machine_options[0]
    # Freeze 5 pieces of it on that machine with operator "Alpha", starting at plan_start.
    fo = FrozenOp(order_key=o0.key, op_seq=mach_op.seq, machine_id=mid,
                  operator="Alpha", remaining_qty=5, prev_start=cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[fo])
    segs = _entry(sched.segments, o0.key, mach_op.seq)
    assert segs, "frozen op was not scheduled"
    assert all(s.machine_id == mid for s in segs), "frozen op left its pinned machine"
    assert segs[0].operator == "Alpha", "frozen op not run by the planned operator"
    assert segs[0].start == cfg.plan_start, "frozen op did not resume at plan start"
    # No setup: total minutes == 5 * cycle (machining setup would add setup_min).
    total_min = sum((s.end - s.start).total_seconds() for s in segs) / 60.0
    assert abs(total_min - 5 * mach_op.cycle_min) < 1e-6, "frozen op charged setup time"


def test_frozen_op_hands_off_at_shift_boundary(ctx):
    """A frozen op long enough to span 19:00 must hand off from its planned FIRST-shift
    operator to a SECOND-shift-qualified substitute — neither is_operator_available nor
    free_during know about shifts, so without an explicit shift check the day-shift
    operator would wrongly keep running the night window (Critical bug, fix round 1).

    Freezes on order[1]'s (Item B) machining op (CNC SECOND SIDE) rather than order[0]'s
    (Item A): Item A's very next op (VMC FIRST SIDE) draws from the exact same
    operator/machine pool as its CNC step (both qualify only Alpha/Bravo across
    CNC1/CNC2/VMC1), so an artificially huge frozen quantity there would immediately
    chain the substitute onto a SECOND machine that same shift — a real, separately
    tested short-job exception (tests/test_short_job_staffing.py), not this bug. Item
    B's next op (WASHING) is a different role/pool (helper Charlie on MW1), so this
    scenario isolates just the shift-handoff behaviour this fix is about.
    """
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    o1 = orders[1]
    routing = nm.routings[o1.item_code]
    mach_op = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    mid = mach_op.machine_options[0]
    # Enough pieces to run ~13 hours — First shift is only 08:00-19:00 (11h), so this
    # must cross the 19:00 boundary into Second shift.
    qty = int(780 / mach_op.cycle_min) + 1
    fo = FrozenOp(order_key=o1.key, op_seq=mach_op.seq, machine_id=mid,
                  operator="Alpha", remaining_qty=qty, prev_start=cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[fo])
    segs = _entry(sched.segments, o1.key, mach_op.seq)
    assert len(segs) >= 2, "expected the frozen op to span the shift boundary"
    assert all(s.machine_id == mid for s in segs), "frozen op left its pinned machine"

    first_shift = [s for s in segs if s.start.hour < 19]
    night_shift = [s for s in segs if s.start.hour >= 19 or s.start.hour < 8]
    assert first_shift, "expected a First-shift segment before the handoff"
    assert all(s.operator == "Alpha" for s in first_shift), \
        "First-shift segment(s) should run under the planned operator Alpha"
    assert night_shift, "expected a Second-shift segment after the 19:00 handoff"
    assert all(s.operator != "Alpha" for s in night_shift), \
        "night-shift segment kept the day-shift operator — no handoff happened"
    assert all(s.operator == "Bravo" for s in night_shift), \
        "expected the Second-shift-qualified substitute (Bravo) to cover the night segment"

    from tests.test_new_engine import _assert_clean
    _assert_clean(sched.segments)


def test_two_frozen_on_one_machine_resume_in_prev_plan_order_before_new_work(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # Two different orders sharing one machine option for a machining op.
    def first_machining(o):
        return next(op for op in nm.routings[o.item_code].operations
                    if op.machine_options and op.cycle_min > 0)
    oa, ob = orders[0], orders[1]
    opa, opb = first_machining(oa), first_machining(ob)
    mid = opa.machine_options[0]
    assert mid in opb.machine_options, "test needs two orders that can share a machine"
    from datetime import timedelta
    # ob's frozen op has the EARLIER prev_start, so it must resume first.
    foa = FrozenOp(oa.key, opa.seq, mid, "Alpha", 4, cfg.plan_start + timedelta(hours=1))
    fob = FrozenOp(ob.key, opb.seq, mid, "Alpha", 4, cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[foa, fob])
    on_mid = sorted([s for s in sched.segments if s.machine_id == mid], key=lambda s: s.start)
    # ob's frozen op resumes first (earlier prev_start), then oa's, then any new work.
    frozen_keys_in_order = [s.order_key for s in on_mid
                            if (s.order_key, s.op_seq) in {(ob.key, opb.seq), (oa.key, opa.seq)}]
    assert frozen_keys_in_order[0] == ob.key and frozen_keys_in_order[1] == oa.key
    frozen_end = max(s.end for s in on_mid
                     if (s.order_key, s.op_seq) in {(ob.key, opb.seq), (oa.key, opa.seq)})
    new_starts = [s.start for s in on_mid
                  if (s.order_key, s.op_seq) not in {(ob.key, opb.seq), (oa.key, opa.seq)}]
    assert all(st >= frozen_end for st in new_starts), "new work started before frozen work finished"


def test_downstream_step_waits_for_frozen_predecessor(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # An order whose routing has a machining op followed by a later in-house op.
    o = next(o for o in orders
             if len([op for op in nm.routings[o.item_code].operations
                     if op.machine_options and op.cycle_min > 0]) >= 1
             and len(nm.routings[o.item_code].operations) >= 2)
    ops = nm.routings[o.item_code].operations
    fop = next(op for op in ops if op.machine_options and op.cycle_min > 0)
    succ = next((op for op in ops if op.seq > fop.seq), None)
    assert succ is not None
    fo = FrozenOp(o.key, fop.seq, fop.machine_options[0], "Alpha", 6, cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[fo])
    frozen_end = max(s.end for s in sched.segments
                     if s.order_key == o.key and s.op_seq == fop.seq)
    succ_start = min((s.start for s in sched.segments
                      if s.order_key == o.key and s.op_seq == succ.seq), default=None)
    if succ_start is not None:  # OS/dispatch successors may be zero-time milestones
        assert succ_start >= frozen_end, "successor started before the frozen op finished"


def test_absent_planned_operator_gets_substitute_machine_still_pinned(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # Use order[1] (Item B) to avoid conflicts; it has a different operator pool than order[0].
    o1 = orders[1]
    fop = next(op for op in nm.routings[o1.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    mid = fop.machine_options[0]
    # Mark the planned operator "Alpha" on leave for the plan-start day.
    cal = nm.calendar
    merged = dict(getattr(cal, "leaves", {}) or {})
    merged["Alpha"] = frozenset({cfg.plan_start.date()})
    nm_absent = _dc_replace(nm, calendar=_dc_replace(cal, leaves=merged))
    # Freeze a small quantity (2 pieces) to minimize interference with other work.
    fo = FrozenOp(o1.key, fop.seq, mid, "Alpha", 2, cfg.plan_start)
    sched = decode(orders, seq, nm_absent, cfg, frozen=[fo])
    segs = [s for s in sched.segments if s.order_key == o1.key and s.op_seq == fop.seq]
    assert segs, "frozen op vanished when its planned operator was absent"
    assert all(s.machine_id == mid for s in segs), "machine not pinned under absence"
    assert all(s.operator != "Alpha" for s in segs), "absent operator was still assigned"
    # No operator double-booked (reuse the suite's invariant checker).
    from tests.test_new_engine import _assert_clean
    _assert_clean(sched.segments)


def test_search_respects_frozen(ctx):
    from ppc_engine.optimize import optimize as new_optimize
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    o0 = orders[0]
    fop = next(op for op in nm.routings[o0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    fo = FrozenOp(o0.key, fop.seq, fop.machine_options[0], "Alpha", 5, cfg.plan_start)
    res = new_optimize(orders, nm, cfg, budget=40, seed=1, frozen=[fo])
    # Re-decode the winning sequence WITH the frozen set → the frozen op is pinned there.
    sched = decode(orders, list(res.best_sequence), nm, cfg, frozen=[fo])
    segs = [s for s in sched.segments if s.order_key == o0.key and s.op_seq == fop.seq]
    assert all(s.machine_id == fop.machine_options[0] for s in segs)
    assert segs[0].start == cfg.plan_start


def test_tune_overlap_forwards_frozen(ctx):
    from ppc_engine.optimize import tune_overlap
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    o0 = orders[0]
    fop = next(op for op in nm.routings[o0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    fo = FrozenOp(o0.key, fop.seq, fop.machine_options[0], "Alpha", 5, cfg.plan_start)
    tr = tune_overlap(orders, nm, cfg, lo=0.5, hi=0.9, seeds=(0,),
                      budget_per_eval=20, coarse=3, frozen=[fo])
    sched = decode(orders, list(tr.best_sequence), nm,
                   _dc_replace(cfg, overlap=tr.best_overlap), frozen=[fo])
    segs = [s for s in sched.segments if s.order_key == o0.key and s.op_seq == fop.seq]
    assert all(s.machine_id == fop.machine_options[0] for s in segs)
