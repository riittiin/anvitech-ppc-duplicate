"""A FROZEN (in-progress) operation must run the WHOLE batch's remaining pieces.

Live bug, 2026-08-11 (director escalation). Two SO lines of the same item — one
part-finished (254 ordered, 166 through CNC FIRST SIDE), one untouched (281,
status Pending) — are clubbed by Rule 1 into one batch. The Gantt showed CNC
FIRST SIDE running **88** pieces (254 - 166) instead of **369** (88 + 281): the
untouched SO's 281 pieces were never scheduled on that step at all, while every
downstream step still ran the full 535.

Root cause: ``engine.freeze.compute_frozen_set`` derives ``remaining_qty`` per SO
LINE, but the op it pins is a BATCH operation. ``_preplace_frozen`` lays exactly
``FrozenOp.remaining_qty`` pieces and then advances the order past that op, so
whatever the frozen row under-counted is never scheduled by the main loop.

The batch's per-step remaining is already computed correctly — Rule 1's
``batch.process_qty`` → ``Order.process_remaining``, the same number the main loop
uses. A frozen row's job is to pin machine + operator + resume order, NEVER to
redefine how much work there is.
"""
from __future__ import annotations

import io
from dataclasses import replace
from datetime import date

import pytest

from engine import book_store, freeze, loaders, new_engine
from engine.config import Config
from engine.models import SOLine
from engine.rules import rule1_consolidate
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True, consolidation_window_days=10)


@pytest.fixture()
def masters():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)          # new_engine reads the stored workbook
    return loaders.load_all(io.BytesIO(wb))[1]


def _line(masters, so_no, qty, delivery, done_on_first=0):
    """An SO line for ITEM_A; ``done_on_first`` pieces already through step 1.
    ``process_qty`` is None for an untouched line, exactly as ``active_so_lines``
    emits it for an order with no punches."""
    routing = masters.routings[ITEM_A]
    if done_on_first <= 0:
        pq = None
    else:
        first = routing.processes[0].seq
        pq = {loaders.normalize_process_name(p.name):
              (qty - done_on_first if p.seq == first else qty)
              for p in routing.processes}
    return SOLine(so_no=so_no, item_code=ITEM_A, item_name="x", qty=qty,
                  delivery_date=delivery, process_qty=pq)


def _frozen_for(masters, batch, lines, good_on_first):
    """The frozen set the app would derive: the applied plan ran step 1 of this
    batch on CNC1 with Alpha, and ``good_on_first`` maps SO no → pieces punched."""
    first = masters.routings[ITEM_A].processes[0]
    nkey = loaders.normalize_process_name(first.name)
    applied = [{"batch_id": batch.batch_id, "item_code": ITEM_A,
                "process_seq": first.seq, "process_name": first.name,
                "machine": "CNC1", "operator": "Alpha",
                "start": "2025-03-03T08:00:00", "end": "2025-03-03T10:00:00",
                "so_refs": list(batch.source_so_refs)}]
    good = {(so, ITEM_A, nkey): n for so, n in good_on_first.items()}
    return freeze.compute_frozen_set(applied, lines, good, masters)


def _first_step(masters, entries):
    first = masters.routings[ITEM_A].processes[0]
    return [e for e in entries if e.process_seq == first.seq]


def test_frozen_op_runs_the_whole_batch_not_only_the_punched_so(masters):
    """The reported bug: a part-finished SO clubbed with an untouched one."""
    started = _line(masters, "SO-0120", 254, date(2025, 4, 1), done_on_first=166)
    fresh = _line(masters, "SO-0122", 281, date(2025, 4, 3))
    batches = rule1_consolidate.run([started, fresh], _CONF, [], masters)
    assert len(batches) == 1, "fixture must club both SOs into ONE batch"
    assert batches[0].qty == 535

    frozen = _frozen_for(masters, batches[0], [started, fresh], {"SO-0120": 166})
    assert frozen, "fixture must actually freeze the in-progress step"

    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    bars = _first_step(masters, entries)
    assert len(bars) == 1
    assert bars[0].qty == 88 + 281, (
        f"frozen step scheduled {bars[0].qty} pieces; the batch still owes "
        f"{88 + 281} on it (88 left of SO-0120 + all 281 of untouched SO-0122)")


def test_frozen_op_gets_the_machine_time_for_the_whole_batch(masters):
    """Not just the label: the pinned machine must be booked for every piece."""
    started = _line(masters, "SO-0120", 254, date(2025, 4, 1), done_on_first=166)
    fresh = _line(masters, "SO-0122", 281, date(2025, 4, 3))
    batches = rule1_consolidate.run([started, fresh], _CONF, [], masters)
    frozen = _frozen_for(masters, batches[0], [started, fresh], {"SO-0120": 166})

    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    cycle = masters.routings[ITEM_A].processes[0].cycle_time
    occupancy = sum(e.occupancy_min for e in _first_step(masters, entries))
    # A resumed op is charged no setup, so it is exactly pieces x cycle.
    assert occupancy == pytest.approx((88 + 281) * cycle), (
        f"machine booked {occupancy} min; {88 + 281} pieces x {cycle} min "
        f"= {(88 + 281) * cycle} min of work remain on the step")


def test_two_in_progress_lines_in_one_batch_make_one_frozen_op(masters):
    """Both clubbed lines part-finished: the batch op is ONE operation, laid once,
    for the sum of what the two lines still owe (88 + 181), not laid twice."""
    a = _line(masters, "SO-0120", 254, date(2025, 4, 1), done_on_first=166)
    b = _line(masters, "SO-0122", 281, date(2025, 4, 3), done_on_first=100)
    batches = rule1_consolidate.run([a, b], _CONF, [], masters)
    frozen = _frozen_for(masters, batches[0], [a, b],
                         {"SO-0120": 166, "SO-0122": 100})
    assert len(frozen) == 2, "fixture must produce a frozen row per started line"

    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    bars = _first_step(masters, entries)
    assert len(bars) == 1
    assert bars[0].qty == 88 + 181
    cycle = masters.routings[ITEM_A].processes[0].cycle_time
    assert bars[0].occupancy_min == pytest.approx((88 + 181) * cycle)


def test_a_solo_batch_is_unchanged(masters):
    """Guard: with nothing clubbed, a frozen op still runs exactly the SO's own
    remaining — the fix must not move the plan where the bug never applied."""
    started = _line(masters, "SO-0120", 254, date(2025, 4, 1), done_on_first=166)
    batches = rule1_consolidate.run([started], _CONF, [], masters)
    frozen = _frozen_for(masters, batches[0], [started], {"SO-0120": 166})

    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    bars = _first_step(masters, entries)
    assert len(bars) == 1 and bars[0].qty == 88


def test_the_plan_is_checked_for_under_scheduled_steps(masters):
    """Defense in depth: an invariant that is CHECKED beats one merely intended.
    ``batch_quantity_violations`` flags any step the plan gives fewer pieces than the
    order book says it still owes — the exact shape of this bug, whatever future code
    path causes it."""
    started = _line(masters, "SO-0120", 254, date(2025, 4, 1), done_on_first=166)
    fresh = _line(masters, "SO-0122", 281, date(2025, 4, 3))
    batches = rule1_consolidate.run([started, fresh], _CONF, [], masters)
    frozen = _frozen_for(masters, batches[0], [started, fresh], {"SO-0120": 166})
    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)

    assert new_engine.batch_quantity_violations(entries, batches) == []

    # Non-vacuous: hand the checker the plan the BUG produced and it must speak up.
    first = masters.routings[ITEM_A].processes[0]
    broken = [replace(e, qty=88.0) if e.process_seq == first.seq else e for e in entries]
    rows = new_engine.batch_quantity_violations(broken, batches)
    assert len(rows) == 1
    assert rows[0]["kind"] == "BATCH_QTY_SHORT"
    assert "369" in rows[0]["message"] and "88" in rows[0]["message"]


def test_downstream_steps_never_exceed_the_step_feeding_them(masters):
    """The invariant behind the bug: no step may plan more pieces than the step
    before it delivers (already-punched pieces count as delivered)."""
    started = _line(masters, "SO-0120", 254, date(2025, 4, 1), done_on_first=166)
    fresh = _line(masters, "SO-0122", 281, date(2025, 4, 3))
    batches = rule1_consolidate.run([started, fresh], _CONF, [], masters)
    frozen = _frozen_for(masters, batches[0], [started, fresh], {"SO-0120": 166})

    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    by_seq = {}
    for e in entries:
        by_seq[e.process_seq] = by_seq.get(e.process_seq, 0) + e.qty
    already_done = {masters.routings[ITEM_A].processes[0].seq: 166}
    steps = sorted(by_seq)
    for prev, cur in zip(steps, steps[1:]):
        supplied = by_seq[prev] + already_done.get(prev, 0)
        assert by_seq[cur] <= supplied, (
            f"step {cur} plans {by_seq[cur]} pieces but step {prev} only "
            f"supplies {supplied}")
