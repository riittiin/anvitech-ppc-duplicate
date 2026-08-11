"""Committed-promise adapter: rule1_consolidate._finalize computes the batch's
tightest committed promise (the only place holding both the batch AND its
member SO-lines); engine.new_engine._orders_from_batches threads it onto the
ppc Order; engine.new_engine._plan_config carries the slack + weight knobs.

Additive — an all-open book (no committed lines) must produce Batch(commitment=
"open", promised_date=None) and ppc Order.promise_date is None, so nothing
changes for the golden book.
"""
from __future__ import annotations

import io
from datetime import date

from engine import book_store
from engine.config import Config
from engine.models import SOLine
from engine.new_engine import _orders_from_batches, _plan_config
from engine.optimizer import COMMITTED_PROMISE_WEIGHT
from engine.rules import rule1_consolidate

from ppc_engine.loaders import load_all as new_load
from tests.new_sample_workbook import ITEM_A, build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3))


def test_batch_takes_the_earliest_committed_promise_among_its_lines():
    # Two lines of the same item, both within the consolidation window -> one
    # batch. Only one line is committed; its promise is the tightest (earliest)
    # even though it's not the line with the earliest SO delivery date.
    lines = [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5,
               delivery_date=date(2025, 3, 21)),
        SOLine(so_no="L2", item_code="X", item_name="X", qty=10,
               delivery_date=date(2025, 3, 28),
               commitment="committed", promised_date=date(2025, 4, 1)),
    ]
    batch = rule1_consolidate.run(lines, config=_CONF)[0]
    assert batch.commitment == "committed"
    assert batch.promised_date == date(2025, 4, 1)


def test_batch_takes_earliest_promise_when_several_lines_committed():
    lines = [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5,
               delivery_date=date(2025, 3, 21),
               commitment="committed", promised_date=date(2025, 4, 10)),
        SOLine(so_no="L2", item_code="X", item_name="X", qty=10,
               delivery_date=date(2025, 3, 28),
               commitment="committed", promised_date=date(2025, 4, 1)),
    ]
    batch = rule1_consolidate.run(lines, config=_CONF)[0]
    assert batch.commitment == "committed"
    assert batch.promised_date == date(2025, 4, 1)   # earliest of the two


def test_all_open_batch_stays_open_and_promiseless():
    lines = [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5,
               delivery_date=date(2025, 3, 21)),
        SOLine(so_no="L2", item_code="X", item_name="X", qty=10,
               delivery_date=date(2025, 3, 28)),
    ]
    batch = rule1_consolidate.run(lines, config=_CONF)[0]
    assert batch.commitment == "open"
    assert batch.promised_date is None


def test_committed_line_with_no_promised_date_does_not_count():
    # commitment == "committed" but promised_date is None (not yet snapshotted)
    # must not produce a promise -> falls back to open.
    lines = [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5,
               delivery_date=date(2025, 3, 21), commitment="committed",
               promised_date=None),
    ]
    batch = rule1_consolidate.run(lines, config=_CONF)[0]
    assert batch.commitment == "open"
    assert batch.promised_date is None


def _new_masters():
    wb_bytes = build_new_sample_bytes()
    book_store.save_masters_bytes(wb_bytes)
    return new_load(io.BytesIO(wb_bytes)).masters


def test_orders_from_batches_sets_promise_date_for_a_committed_batch():
    masters = _new_masters()
    lines = [
        SOLine(so_no="NSO-001", item_code=ITEM_A, item_name="a", qty=10,
               delivery_date=date(2025, 3, 21),
               commitment="committed", promised_date=date(2025, 4, 5)),
    ]
    batches = rule1_consolidate.run(lines, config=_CONF)
    orders, _ = _orders_from_batches(batches, masters)
    assert len(orders) == 1
    assert orders[0].promise_date == date(2025, 4, 5)


def test_orders_from_batches_leaves_promise_date_none_for_an_open_batch():
    masters = _new_masters()
    lines = [
        SOLine(so_no="NSO-001", item_code=ITEM_A, item_name="a", qty=10,
               delivery_date=date(2025, 3, 21)),
    ]
    batches = rule1_consolidate.run(lines, config=_CONF)
    orders, _ = _orders_from_batches(batches, masters)
    assert len(orders) == 1
    assert orders[0].promise_date is None


def test_plan_config_carries_committed_promise_slack_and_weight():
    pc = _plan_config(_CONF)
    assert pc.committed_promise_slack_days == float(_CONF.committed_promise_slack_days)
    assert pc.committed_promise_weight == COMMITTED_PROMISE_WEIGHT
