"""Idle-gap harvest (step 2). Pure post-pass, not yet wired into decode.

The guarantee that matters is not "the score did not worsen" — it is that NO ORDER
finishes later, asserted per order. The design gives it by construction (a block's
start never moves and a block only ever gets shorter), so if one moves later the
harvest is buggy and must be fixed, never covered up with an aggregate check.
"""
import io
from datetime import date

import pytest

from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from engine import book_store, loaders
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.gap_harvest import harvest
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True)


@pytest.fixture()
def plan():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    orders, _ = _orders_from_batches(rule1_consolidate.run(so_lines, _CONF), nm)
    cfg = _plan_config(_CONF)
    return decode(orders, [o.key for o in orders], nm, cfg), nm, cfg


def _machine_ivs(sched):
    out = {}
    for s in sched.segments:
        if s.machine_id and s.end > s.start:
            out.setdefault(s.machine_id, []).append((s.start, s.end, s.order_key, s.op_seq))
    return out


def _operator_ivs(sched):
    out = {}
    for s in sched.segments:
        if s.operator and s.end > s.start:
            out.setdefault(s.operator, []).append((s.start, s.end))
    return out


def test_no_order_ever_finishes_later(plan):
    """THE guarantee. Per order, never in aggregate."""
    before, nm, cfg = plan
    after = harvest(before, nm, cfg)
    for key, was in before.completion.items():
        now = after.completion.get(key)
        assert now is not None, f"{key} lost its completion"
        assert now <= was, f"{key} moved LATER: {was} -> {now}"


def test_no_machine_is_double_booked(plan):
    before, nm, cfg = plan
    after = harvest(before, nm, cfg)
    for mid, ivs in _machine_ivs(after).items():
        ivs.sort()
        for (s1, e1, k1, q1), (s2, e2, k2, q2) in zip(ivs, ivs[1:]):
            assert s2 >= e1, f"{mid}: {k1}/{q1} {s1}-{e1} overlaps {k2}/{q2} {s2}-{e2}"


def test_no_operator_is_in_two_places_at_once(plan):
    before, nm, cfg = plan
    after = harvest(before, nm, cfg)
    for name, ivs in _operator_ivs(after).items():
        ivs.sort()
        for (s1, e1), (s2, e2) in zip(ivs, ivs[1:]):
            assert s2 >= e1, f"{name} double-booked {s1}-{e1} vs {s2}-{e2}"


def test_no_work_is_created_or_lost(plan):
    """Harvesting moves work earlier; it must never invent or drop machine minutes
    beyond the setup a fresh engagement legitimately costs."""
    before, nm, cfg = plan
    after = harvest(before, nm, cfg)

    def per_op(sched):
        out = {}
        for s in sched.segments:
            if s.machine_id is None:
                continue
            out[(s.order_key, s.op_seq)] = out.get((s.order_key, s.op_seq), 0.0) + \
                (s.end - s.start).total_seconds() / 60.0
        return out

    a, b = per_op(before), per_op(after)
    assert set(a) == set(b), "an operation appeared or vanished"
    for k in a:
        # after >= before is allowed only by setup on a re-engagement; never LESS work
        assert b[k] >= a[k] - 1e-6, f"{k} lost work: {a[k]} -> {b[k]}"
        assert b[k] <= a[k] + cfg.setup_min + 1e-6, f"{k} gained more than one setup"


def test_a_plan_with_no_holes_is_returned_untouched(plan):
    """Idempotence guard: harvesting twice must change nothing the second time."""
    before, nm, cfg = plan
    once = harvest(before, nm, cfg)
    twice = harvest(once, nm, cfg)
    assert [(s.order_key, s.op_seq, s.start, s.end) for s in twice.segments] == \
           [(s.order_key, s.op_seq, s.start, s.end) for s in once.segments]


def test_harvested_work_respects_the_routing(plan):
    """Nothing may be pulled into a hole before the step feeding it has finished."""
    before, nm, cfg = plan
    after = harvest(before, nm, cfg)
    pos = {}
    for item, rt in nm.routings.items():
        for i, o in enumerate(rt.operations):
            pos[(item, o.seq)] = i
    span = {}
    for s in after.segments:
        k = (s.order_key, s.op_seq)
        cur = span.get(k)
        span[k] = (min(cur[0], s.start), max(cur[1], s.end)) if cur else (s.start, s.end)
    for (order_key, op_seq), (st, _en) in span.items():
        me = pos.get((order_key[1], op_seq))
        if me is None:
            continue
        for (ok, sq), (_s2, en2) in span.items():
            if ok != order_key:
                continue
            p = pos.get((order_key[1], sq))
            if p is not None and p < me:
                assert st >= _s2, f"{order_key} step {op_seq} starts before step {sq}"
