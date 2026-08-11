"""Tests for the new operator-stable engine driving the old build (engine/new_engine.py).

Runs on a fully-staffed synthetic workbook (tests/new_sample_workbook.py) so the new
engine actually schedules it. Headline guarantees — true on any input:
  * NO fragmentation: every operation runs on ONE machine.
  * NO operator ping-pong: one operator per (machine, shift); an operator is never on two
    machines in the same shift (the exact rule the old system broke).
  * NO double-booking of a machine or an operator.
  * per-process feedback: a finished step re-plans as a zero-time milestone.
  * the optimizer's rank map replays to the same plan it found.
  * the whole old UI (pipeline -> Gantt + Analytics) builds from the new engine's output.
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, timedelta

import pytest

from engine import analytics, book_store, gantt, loaders, pipeline
from engine.config import Config
from engine.models import Batch, PlanRun
from engine.new_engine import _orders_from_batches, _plan_config, run, sweep_optimize
from engine.rules import rule1_consolidate

from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders import load_all as new_load
from ppc_engine.optimize import optimize as new_optimize
from ppc_engine.scheduler import decode
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)


@pytest.fixture()
def wb_bytes():
    return build_new_sample_bytes()


@pytest.fixture()
def new_masters(wb_bytes):
    """New-engine Masters + the workbook seeded into the store (so new_engine finds it)."""
    book_store.save_masters_bytes(wb_bytes)
    return new_load(io.BytesIO(wb_bytes)).masters


def _old_book(wb_bytes=None):
    """(so_lines, masters) via the OLD loaders — what the pipeline/optimizer consume.
    Module-level so other test files can call it directly (no fixture wiring needed);
    the ``old_book`` fixture below just wraps it over the shared ``wb_bytes`` fixture."""
    return loaders.load_all(io.BytesIO(wb_bytes if wb_bytes is not None else build_new_sample_bytes()))


@pytest.fixture()
def old_book(wb_bytes):
    """(so_lines, masters) via the OLD loaders — what the pipeline/optimizer consume."""
    return _old_book(wb_bytes)


def _shift_of(dt):
    if 8 <= dt.hour < 19:
        return (dt.date(), "1")
    return (dt.date() if dt.hour >= 19 else dt.date() - timedelta(days=1), "2")


def _assert_clean(segments):
    work = [s for s in segments if s.machine_id and s.operator]

    by_op = defaultdict(set)
    for s in segments:
        if s.machine_id:
            by_op[(s.order_key, s.op_seq)].add(s.machine_id)
    assert all(len(v) == 1 for v in by_op.values()), "an operation was split across machines"

    mach_shift, op_shift = defaultdict(set), defaultdict(set)
    for s in work:
        mach_shift[(s.machine_id, _shift_of(s.start))].add(s.operator)
        op_shift[(s.operator, _shift_of(s.start))].add(s.machine_id)
    assert all(len(v) == 1 for v in mach_shift.values()), "two operators on one machine in a shift"
    assert all(len(v) == 1 for v in op_shift.values()), "an operator on two machines in a shift"

    def _overlaps(intervals):
        iv = sorted(intervals)
        return any(iv[i][0] < iv[i - 1][1] for i in range(1, len(iv)))

    mach_iv, op_iv = defaultdict(list), defaultdict(list)
    for s in work:
        mach_iv[s.machine_id].append((s.start, s.end))
        op_iv[s.operator].append((s.start, s.end))
    assert not any(_overlaps(v) for v in mach_iv.values()), "a machine was double-booked"
    assert not any(_overlaps(v) for v in op_iv.values()), "an operator was double-booked"


def _decode_book(so_lines, masters):
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, masters)
    seq = [(b.batch_id, b.item_code) for b in batches]
    return decode(orders, seq, masters, _plan_config(_CONF))


def test_plan_has_no_fragmentation_or_pingpong(old_book, new_masters):
    so_lines, _ = old_book
    _assert_clean(_decode_book(so_lines, new_masters).segments)


def test_optimized_order_is_also_clean(old_book, new_masters):
    so_lines, _ = old_book
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, new_masters)
    res = new_optimize(orders, new_masters, _plan_config(_CONF), budget=40, seed=1)
    _assert_clean(decode(orders, res.best_sequence, new_masters, _plan_config(_CONF)).segments)


def test_unstaffed_op_is_skipped_not_crashed(new_masters):
    """A routing whose in-house step has NO machine with a qualified operator (e.g.
    incomplete master data / a provisional machine without operators) must NOT 500 the
    whole plan — the decoder would raise 'no runnable machine'. The order is skipped
    (like an unrouted order), so every other order still plans."""
    from dataclasses import replace
    item = next(iter(new_masters.routings))
    b = Batch(batch_id="B1", item_code=item, item_name="x", qty=10,
              so_delivery_date=date(2025, 4, 1), source_so_refs=["S"])
    kept, _ = _orders_from_batches([b], new_masters)   # fully staffed -> scheduled
    assert len(kept) == 1
    no_ops = replace(new_masters, operators=[])         # no qualified operator anywhere
    dropped, _ = _orders_from_batches([b], no_ops)      # skipped, not crashed
    assert dropped == []


def _ops_used(schedule):
    """Every operator name the schedule assigns — the single-name field plus each
    per-shift handoff segment (the truth for multi-shift ops)."""
    used = set()
    for e in schedule:
        if e.operator:
            used.add(e.operator)
        for seg in e.op_segments:
            if seg[2]:
                used.add(seg[2])
    return used


def test_new_engine_only_uses_app_owned_operators(old_book, new_masters):
    """Operators are APP-OWNED (managed in Settings); the workbook's operator sheet is
    a fossil. Deleting/editing an operator in the app MUST change what the engine
    schedules. Regression for the live wiring bug where the new engine re-read
    operators from the workbook and kept scheduling a deleted person."""
    from dataclasses import replace
    so_lines, masters = old_book
    batches = rule1_consolidate.run(so_lines, _CONF)
    baseline = run(batches, _CONF, None, masters=masters)   # full app set (== workbook here)
    used = _ops_used(baseline)
    assert used, "the sample should assign operators"

    victim = sorted(used)[0]                                 # the owner deletes this person
    kept = [o for o in masters.operators if o.name != victim]
    after = run(batches, _CONF, None, masters=replace(masters, operators=kept))
    used_after = _ops_used(after)

    assert victim not in used_after, (
        f"{victim!r} was removed from the app operator table but the engine still "
        f"scheduled them (it re-read the workbook operator sheet)")
    assert used_after <= {o.name for o in kept}, (
        f"engine used operators absent from the app table: {used_after - {o.name for o in kept}}")


def test_optimize_search_uses_app_operators_not_workbook(old_book, new_masters):
    """The SEQUENCE SEARCH must optimize against the app crew, not the workbook. With
    an operator deleted in the app, the winner it returns (replayed exactly like the
    plan) must not schedule that person. Regression: the search previously re-read the
    workbook operators, so it 'optimized taking the deleted operator into consideration'."""
    from dataclasses import replace
    from engine import new_engine
    so_lines, masters = old_book
    victim = "Bravo"
    kept = [o for o in masters.operators if o.name != victim]
    app_masters = replace(masters, operators=kept)

    res = new_engine.optimize_sequence(so_lines, _CONF, app_masters, budget_evals=15, seed=1)
    assert res.ranks, "the search should return a ranked plan"

    # Replay the winning ranks the same way _plan does, with the app crew.
    batches = rule1_consolidate.run(so_lines, _CONF)
    ordered, _ = pipeline.apply_priority_rank(batches, res.ranks)
    used = _ops_used(new_engine.run(ordered, _CONF, None, masters=app_masters))
    assert victim not in used
    assert used <= {o.name for o in kept}


def test_optimize_sequence_winner_matches_replay(old_book, new_masters):
    """The CLOUD path (optimize_sequence) must report a winner the app reproduces when
    it replays the ranks — else the Optimize panel promises a plan the user never gets
    (the live 2026-07-25 '52.5 promised, 55.6 applied' gap). The local-sweep invariant
    test covers `tune`, NOT this path."""
    from engine import optimizer, new_engine
    so_lines, masters = old_book
    res = new_engine.optimize_sequence(so_lines, _CONF, masters, budget_evals=60, seed=1)
    assert res.ranks and "makespan_days" in res.best

    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters, priority_rank=res.ranks)
    replayed = optimizer.plan_metrics(pr.schedule, so_lines, _CONF.plan_start_date)
    assert replayed["makespan_days"] == res.best["makespan_days"], \
        f"winner makespan {res.best['makespan_days']} != replay {replayed['makespan_days']}"
    assert replayed["total_late_days"] == res.best["total_late_days"], \
        f"winner late {res.best['total_late_days']} != replay {replayed['total_late_days']}"


def test_optimize_sequence_winner_matches_replay_with_consolidation(old_book, new_masters):
    """Same parity check, but with MULTIPLE SOs per item merged by Rule 1 into multi-SO
    batches — the real-book shape (67 orders consolidated) the tiny 2-order sample lacks.
    This is where the live '52.5 promised / 55.6 applied' gap lives."""
    from datetime import date as _date
    from engine import optimizer, new_engine
    from engine.models import SOLine
    from tests.new_sample_workbook import ITEM_A, ITEM_B
    _, masters = old_book
    # 8 SOs across the two items, delivery dates within the consolidation window so Rule 1
    # merges each item's lines into ONE batch carrying several source SOs.
    so_lines = [SOLine(so_no=f"S{n}", item_code=(ITEM_A if n % 2 == 0 else ITEM_B),
                       item_name="x", qty=10 + n, delivery_date=_date(2025, 3, 18 + n))
                for n in range(8)]
    res = new_engine.optimize_sequence(so_lines, _CONF, masters, budget_evals=120, seed=5)
    assert res.ranks and "makespan_days" in res.best

    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters, priority_rank=res.ranks)
    replayed = optimizer.plan_metrics(pr.schedule, so_lines, _CONF.plan_start_date)
    assert replayed["makespan_days"] == res.best["makespan_days"], \
        f"winner makespan {res.best['makespan_days']} != replay {replayed['makespan_days']}"


def test_optimize_sequence_winner_uses_app_operators_not_workbook(old_book, new_masters):
    """The reported winner metrics must be measured against the APP crew (the passed
    masters), NOT the workbook's operator sheet. Regression for the positional-arg bug
    (`run(best_batches, config, masters)` put masters in the `notes` slot → masters=None
    → the workbook's full crew), which promised a plan the reduced app crew can't match
    (live Test8: 53.7d/1191 promised vs 53.53d/1214 applied after an operator was
    deleted in the app)."""
    from dataclasses import replace
    from datetime import date as _date
    from engine import optimizer, new_engine
    from engine.models import SOLine
    from tests.new_sample_workbook import ITEM_A, ITEM_B
    _, masters = old_book
    # The owner deleted 'Bravo' in the app; the workbook sheet still lists them.
    reduced = replace(masters, operators=[o for o in masters.operators if o.name != "Bravo"])
    so_lines = [SOLine(so_no=f"S{n}", item_code=(ITEM_A if n % 2 == 0 else ITEM_B),
                       item_name="x", qty=20 + n, delivery_date=_date(2025, 3, 18 + n))
                for n in range(8)]
    res = new_engine.optimize_sequence(so_lines, _CONF, reduced, budget_evals=120, seed=5)

    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, reduced, priority_rank=res.ranks)
    replay = optimizer.plan_metrics(pr.schedule, so_lines, _CONF.plan_start_date)
    assert res.best["makespan_days"] == replay["makespan_days"], \
        f"winner {res.best['makespan_days']} (workbook crew?) != replay {replay['makespan_days']} (app crew)"
    assert res.best["total_late_days"] == replay["total_late_days"]


def test_per_process_feedback_finishes_step_as_milestone(new_masters):
    # process_qty is keyed by the ORDER BOOK's normaliser (loaders.normalize_process_name),
    # NOT new_engine._norm — build it that way so this test exercises the REAL production
    # path and would catch a normaliser mismatch that silently drops multi-word steps.
    item = next(c for c, r in new_masters.routings.items()
                if sum(1 for o in r.operations if o.kind == OperationKind.MACHINING) >= 2)
    ops = new_masters.routings[item].operations
    done = next(o for o in ops if o.kind == OperationKind.MACHINING)
    assert " " in done.name, "regression needs a multi-word step name (e.g. 'CNC FIRST SIDE')"
    pq = {loaders.normalize_process_name(o.name): (0 if o.seq == done.seq else 100) for o in ops}
    b = Batch(batch_id="T1", item_code=item, item_name="x", qty=100,
              so_delivery_date=date(2025, 4, 1), source_so_refs=["SO_T1"], process_qty=pq)

    orders, _ = _orders_from_batches([b], new_masters)
    # EVERY step must be present — a normaliser mismatch would drop the multi-word ones,
    # and the finished step must read 0 remaining (not silently fall back to full qty).
    assert set(orders[0].process_remaining) == {o.seq for o in ops}
    assert orders[0].process_remaining[done.seq] == 0

    entries = {e.process_seq: e for e in run([b], _CONF, None)}
    milestone = entries[done.seq]
    assert milestone.qty == 0
    assert (milestone.end - milestone.start).total_seconds() == 0  # finished step, zero time


def test_optimizer_ranks_replay_to_the_same_plan(old_book, new_masters):
    from engine import optimizer
    so_lines, masters = old_book
    sw = sweep_optimize(so_lines, _CONF, masters, budget_evals=40, seed=1)
    assert sw.result.ranks and "makespan_days" in sw.result.best

    # Replaying the ranks through the app's normal path reproduces the reported metrics.
    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters, priority_rank=sw.result.ranks)
    replayed = optimizer.plan_metrics(pr.schedule, so_lines, _CONF.plan_start_date)
    assert replayed["makespan_days"] == sw.result.best["makespan_days"]


def test_old_ui_builds_from_new_engine(old_book, new_masters):
    # The whole point: pipeline -> Gantt + Analytics build from the new engine's output.
    so_lines, masters = old_book
    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters)
    assert pr.schedule  # ScheduleEntry list produced

    g = gantt.build_gantt(pr.schedule, pr.batches, masters, {})
    assert g["rows"] and g["num_days"] > 0

    a = analytics.build_analytics(pr.schedule, masters, _CONF, pr.batches, [])
    assert a["machines"] and a["headline"]


def test_unrouted_order_is_skipped_not_crashing(old_book, new_masters):
    """Regression: an order whose item has no routing must be SKIPPED (it still shows in the
    book/report), never crash the plan. The classic engine tolerated this; the new engine
    once raised KeyError, which would have blanked the whole schedule."""
    from engine.models import SOLine
    so_lines, masters = old_book
    bad = SOLine(so_no="X9", item_code="NO_SUCH_ITEM", item_name="?", qty=5,
                 delivery_date=date(2025, 5, 1))
    pr = PlanRun(so_lines=list(so_lines) + [bad])
    pipeline.run_forward(pr, _CONF, masters)  # must not raise
    assert "NO_SUCH_ITEM" not in {e.item_code for e in pr.schedule}
    assert pr.schedule  # the routed orders still schedule


def test_operator_absence_is_honoured(old_book, new_masters):
    """The app's operator-absence feature must apply under the new engine: an absent
    operator is never assigned (regression — it was silently ignored)."""
    from engine import optimize_service
    so_lines, masters = old_book
    if not masters.operators:
        pytest.skip("no operators in sample")
    op = masters.operators[0].name
    reserved = optimize_service.absence_reservations(
        [{"operator": op, "from_date": "2025-03-01", "to_date": "2025-12-31"}])
    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters, reserved=reserved)
    assert all(e.operator != op for e in pr.schedule)


def test_no_op_finishes_before_its_predecessor(old_book, new_masters):
    """Piece-flow reality: a downstream op can never END before the step feeding it —
    the pieces it processes don't exist until the predecessor finishes. High overlap
    lets a fast downstream op finish its cutting early; the engine must PACE its
    displayed end to >= its predecessor's (the classic engine did; the new engine
    dropped it, re-introducing the 'deburring skipped for the last jobs' bug)."""
    from dataclasses import replace
    from engine import new_engine
    so_lines, masters = old_book
    _CONF_OV = replace(_CONF, overlap_percent=90)
    sched = new_engine.run(rule1_consolidate.run(so_lines, _CONF_OV), _CONF_OV, None, masters=masters)
    by_batch = defaultdict(list)
    for e in sched:
        by_batch[e.batch_id].append(e)
    bad = []
    for es in by_batch.values():
        es.sort(key=lambda e: e.process_seq)
        for i in range(1, len(es)):
            if es[i].end < es[i - 1].end:
                bad.append((es[i - 1].process_name, es[i - 1].end, es[i].process_name, es[i].end))
    assert not bad, f"downstream op finishes before predecessor: {bad[:3]}"


def test_op_work_never_finishes_before_its_predecessor(old_book, new_masters):
    """A downstream step's real WORK (op_segments) can't finish before the step feeding
    it — else the machine-wise schedule processes pieces before they exist (the
    'deburring skipped for the last jobs' bug, at the WORK level, not just the Gantt
    span). 2026-07-25 piece-flow spec."""
    from dataclasses import replace
    from engine import new_engine
    so_lines, masters = old_book
    cfg = replace(_CONF, overlap_percent=90)
    sched = new_engine.run(rule1_consolidate.run(so_lines, cfg), cfg, None, masters=masters)
    by_batch = defaultdict(list)
    for e in sched:
        if e.op_segments:
            by_batch[e.batch_id].append((e.process_seq, max(s[1] for s in e.op_segments)))
    bad = []
    for es in by_batch.values():
        es.sort()
        for i in range(1, len(es)):
            if es[i][1] < es[i - 1][1]:
                bad.append((es[i - 1], es[i]))
    assert not bad, f"downstream WORK finishes before predecessor: {bad[:3]}"


def test_optimize_sequence_honors_cancel(old_book, new_masters):
    """Stop & keep best (new engine): optimize_sequence stops promptly and reports
    cancelled when should_cancel trips — it must not run the whole budget first."""
    from engine.new_engine import optimize_sequence
    so_lines, old_masters = old_book
    res = optimize_sequence(so_lines, _CONF, old_masters, budget_evals=200,
                            should_cancel=lambda: True)
    assert res.cancelled is True
    assert res.evals <= 4          # broke out during the seed pass, nowhere near 200


def test_sweep_optimize_honors_cancel(old_book, new_masters):
    """Stop & keep best (new engine): the local Deep Search fallback stops promptly on
    cancel, reports cancelled, and still returns a usable best plan."""
    so_lines, old_masters = old_book
    sw = sweep_optimize(so_lines, _CONF, old_masters, budget_evals=200,
                        should_cancel=lambda: True)
    assert sw.cancelled is True


def test_new_engine_fingerprint_feeds_the_staleness_signature():
    """Production runs scheduler=='new' (DEFAULT_SCHEDULER=new), so a change to how
    the new engine allocates work — e.g. dropping automatic operator-shift rotation,
    2026-08-05 — must flip the applied-optimization staleness signature the same way
    a classic/flow fingerprint bump already does (test_scarce_operator_pick.py /
    test_flow_scheduler.py). Before this fix, engine.new_engine.SCHEDULER_FINGERPRINT
    was computed but never folded into api.main._inputs_signature, so a deploy that
    changed the new engine's semantics left the applied ranks looking fresh
    (inputs_changed: False) under a green banner."""
    from api import main as api_main
    from engine import new_engine
    from engine.config import Config as Cfg
    cfg = Cfg()
    sig_now = api_main._inputs_signature(cfg)
    orig = new_engine.SCHEDULER_FINGERPRINT
    try:
        new_engine.SCHEDULER_FINGERPRINT = orig + "-next"
        assert api_main._inputs_signature(cfg) != sig_now
    finally:
        new_engine.SCHEDULER_FINGERPRINT = orig
    assert api_main._inputs_signature(cfg) == sig_now
