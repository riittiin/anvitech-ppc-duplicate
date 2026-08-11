from datetime import date
from engine import book_store
from engine.models import Order


def _seed():
    book_store.delete_all()
    book_store.add_orders([Order(so_no="SO1", item_code="X", item_name="X",
                                 ordered_qty=10, delivery_date=date(2026, 7, 20))])


def test_set_commitment_persists_lane_and_promise():
    _seed()
    ok = book_store.set_commitment("SO1", "X", "committed",
                                   date(2026, 7, 22), "2026-07-13T09:00:00")
    assert ok is True
    o = book_store.load_active_orders()[("SO1", "X")]
    assert o.commitment == "committed"
    assert o.promised_date == date(2026, 7, 22)
    assert o.committed_at == "2026-07-13T09:00:00"


def test_clear_commitment_resets_to_open():
    _seed()
    book_store.set_commitment("SO1", "X", "committed", date(2026, 7, 25), "2026-07-13T09:00:00")
    assert book_store.clear_commitment("SO1", "X") is True
    o = book_store.load_active_orders()[("SO1", "X")]
    assert o.commitment == "open" and o.promised_date is None


def test_set_commitment_unknown_order_returns_false():
    _seed()
    assert book_store.set_commitment("NOPE", "Y", "committed", None, "t") is False
