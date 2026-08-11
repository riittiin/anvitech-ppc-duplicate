from engine import book_store

def test_last_applied_schedule_round_trips():
    rows = [{"batch_id": "B1", "item_code": "IT-A", "process_seq": 2,
             "machine": "CNC1", "operator": "Alpha", "start": "2026-07-29T08:00:00",
             "end": "2026-07-29T10:00:00", "so_refs": ["SO1"]}]
    book_store.save_last_applied_schedule(rows)
    assert book_store.load_last_applied_schedule() == rows

def test_frozen_ops_round_trip_and_clear():
    rows = [{"so_no": "SO1", "item_code": "IT-A", "process": "CNC first side",
             "op_seq": 2, "machine": "CNC1", "operator": "Alpha", "remaining_qty": 40,
             "prev_start": "2026-07-29T08:00:00"}]
    book_store.save_frozen_ops(rows)
    assert book_store.load_frozen_ops() == rows
    book_store.clear_frozen_ops()
    assert book_store.load_frozen_ops() == []
