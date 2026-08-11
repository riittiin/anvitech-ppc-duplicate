from datetime import date
from engine.models import Order, SOLine


def test_order_defaults_to_open_and_no_promise():
    o = Order(so_no="SO1", item_code="X", item_name="X", ordered_qty=10,
              delivery_date=date(2026, 7, 20))
    assert o.commitment == "open"
    assert o.promised_date is None
    assert o.committed_at is None


def test_order_commitment_round_trips_through_json():
    o = Order(so_no="SO1", item_code="X", item_name="X", ordered_qty=10,
              delivery_date=date(2026, 7, 20), commitment="committed",
              promised_date=date(2026, 7, 22), committed_at="2026-07-13T09:00:00")
    back = Order.from_json(o.to_json())
    assert back.commitment == "committed"
    assert back.promised_date == date(2026, 7, 22)
    assert back.committed_at == "2026-07-13T09:00:00"


def test_soline_defaults_to_open():
    s = SOLine(so_no="SO1", item_code="X", item_name="X", qty=10,
               delivery_date=date(2026, 7, 20))
    assert s.commitment == "open"
    assert s.promised_date is None
