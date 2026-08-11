import io
from datetime import date
import pytest
from engine.config import Config
from engine import book_store, loaders
from engine.new_engine import _orders_from_batches, _ppc_frozen, _new_masters, _plan_config
from engine.rules import rule1_consolidate
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import FrozenOp
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)


def test_ppc_frozen_maps_so_and_process_to_frozenop():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    o0 = orders[0]
    batch = batch_by_key[o0.key]
    routing = nm.routings[o0.item_code]
    mop = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    row = {"so_no": batch.source_so_refs[0], "item_code": o0.item_code,
           "process": mop.name, "op_seq": mop.seq, "machine": mop.machine_options[0],
           "operator": "Alpha", "remaining_qty": 7, "prev_start": "2025-03-03T08:00:00"}
    fos = _ppc_frozen([row], orders, batch_by_key, nm)
    assert len(fos) == 1
    fo = fos[0]
    assert isinstance(fo, FrozenOp)
    assert fo.order_key == o0.key and fo.op_seq == mop.seq
    assert fo.machine_id == mop.machine_options[0] and fo.operator == "Alpha"
    assert fo.remaining_qty == 7


def test_ppc_frozen_drops_unmappable_rows():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    rows = [{"so_no": "GHOST", "item_code": "NOPE", "process": "x", "op_seq": 1,
             "machine": "CNC1", "operator": "Alpha", "remaining_qty": 5,
             "prev_start": "2025-03-03T08:00:00"}]
    assert _ppc_frozen(rows, orders, batch_by_key, nm) == []


def test_ppc_frozen_drops_malformed_rows_without_raising():
    # Reviewer-caught robustness bug: a row that DOES map to a real scheduled batch but
    # carries a malformed remaining_qty (None) or prev_start (None) must be DROPPED, not
    # raise -- one bad row must never crash _ppc_frozen (or, once wired in, the whole Plan).
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    o0 = orders[0]
    batch = batch_by_key[o0.key]
    routing = nm.routings[o0.item_code]
    mop = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    base = {"so_no": batch.source_so_refs[0], "item_code": o0.item_code,
            "process": mop.name, "op_seq": mop.seq, "machine": mop.machine_options[0],
            "operator": "Alpha", "remaining_qty": 7, "prev_start": "2025-03-03T08:00:00"}

    bad_qty = dict(base, remaining_qty=None)
    bad_qty_str = dict(base, remaining_qty="abc")
    bad_prev_start = dict(base, prev_start=None)

    assert _ppc_frozen([bad_qty], orders, batch_by_key, nm) == []
    assert _ppc_frozen([bad_qty_str], orders, batch_by_key, nm) == []
    assert _ppc_frozen([bad_prev_start], orders, batch_by_key, nm) == []
    # The well-formed row still maps normally (valid path unaffected).
    assert len(_ppc_frozen([base], orders, batch_by_key, nm)) == 1


def test_new_engine_run_pins_frozen_step():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    b0 = batches[0]
    routing = nm.routings[b0.item_code]
    mop = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    row = {"so_no": b0.source_so_refs[0], "item_code": b0.item_code,
           "process": mop.name, "op_seq": mop.seq, "machine": mop.machine_options[0],
           "operator": "Alpha", "remaining_qty": 6, "prev_start": "2025-03-03T08:00:00"}
    from engine.new_engine import run as new_run
    entries = new_run(batches, config=_CONF, masters=masters, frozen=[row])
    hit = [e for e in entries if e.batch_id == b0.batch_id and e.process_seq == mop.seq]
    assert hit and hit[0].machine == mop.machine_options[0]
    assert hit[0].operator == "Alpha"


def test_sweep_optimize_accepts_frozen_and_pins_winner():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, batch_by_key = _orders_from_batches(batches, nm)
    b0 = batches[0]
    mop = next(op for op in nm.routings[b0.item_code].operations
               if op.machine_options and op.cycle_min > 0)
    row = {"so_no": b0.source_so_refs[0], "item_code": b0.item_code, "process": mop.name,
           "op_seq": mop.seq, "machine": mop.machine_options[0], "operator": "Alpha",
           "remaining_qty": 5, "prev_start": "2025-03-03T08:00:00"}
    from engine.new_engine import sweep_optimize
    sr = sweep_optimize(so_lines, _CONF, masters, budget_evals=40, frozen=[row])
    assert sr.result.ranks  # produced a plan without error
