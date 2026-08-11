import io
from datetime import date
import pytest
from engine.config import Config
from engine import book_store, loaders, optimizer
from tests.new_sample_workbook import build_new_sample_bytes


def test_optimizer_sweep_forwards_frozen_without_error():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))   # load_all returns a 2-TUPLE
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    line = so_lines[0]
    row = {"so_no": line.so_no, "item_code": line.item_code, "process": "",
           "op_seq": None, "machine": "CNC1", "operator": "Alpha",
           "remaining_qty": 3, "prev_start": "2025-03-03T08:00:00"}
    sr = optimizer.sweep_optimize(so_lines, cfg, masters, budget_evals=30, frozen=[row])
    assert sr is not None  # threaded frozen through the new-engine delegate without error
