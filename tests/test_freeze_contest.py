"""engine/optimize_service.py threading the `frozen` field through the
contest payload and book signature — in-progress operations must survive
the JSON round-trip to a cloud worker and must be able to move the book
fingerprint so a freeze re-triggers the auto-optimize."""
import io
from datetime import date

from engine.config import Config
from engine import book_store, loaders, optimize_service, orderbook
from tests.new_sample_workbook import build_new_sample_bytes


def test_payload_round_trips_frozen():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                apply_operator_logic=True)
    orders = book_store.load_active_orders()  # may be empty; fine for the round-trip
    frozen = [{"so_no": "SO1", "item_code": "IT-A", "process": "CNC first side",
              "op_seq": 2, "machine": "CNC1", "operator": "Alpha",
              "remaining_qty": 4, "prev_start": "2026-07-29T08:00:00"}]
    payload = optimize_service.build_payload(orders, [], wb, cfg, seed=1, frozen=frozen)
    assert payload["frozen"] == frozen
    parsed = optimize_service.parse_payload(payload)
    assert parsed[-1] == frozen  # frozen is the last element of the parse tuple


def test_book_signature_changes_with_frozen():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      book_store.load_actuals(), masters)
    a = optimize_service.book_signature(lines, absences=[], frozen=[])
    b = optimize_service.book_signature(lines, absences=[],
            frozen=[{"so_no": "SO1", "item_code": "IT-A", "op_seq": 2,
                     "machine": "CNC1", "remaining_qty": 4}])
    assert a != b
    # Byte-identical for the empty/default case (no pre-existing caller's
    # signature may move just because `frozen` now exists as a parameter).
    assert optimize_service.book_signature(lines) == optimize_service.book_signature(lines, frozen=[])
    assert optimize_service.book_signature(lines, absences=[]) == a
