"""Rule 2 — sort by SO delivery date (earliest first)."""
from datetime import date

from engine.models import Batch
from engine.rules import rule2_sort_by_date


def _b(item, d):
    return Batch(batch_id=item, item_code=item, item_name=item, qty=1,
                 so_delivery_date=d, source_so_refs=[item])


def test_orders_by_date(config):
    batches = [
        _b("late", date(2025, 4, 10)),
        _b("early", date(2025, 3, 7)),
        _b("mid", date(2025, 3, 18)),
    ]
    out = rule2_sort_by_date.run(batches, config=config)
    assert [b.item_code for b in out] == ["early", "mid", "late"]


def test_stable_for_equal_dates(config):
    d = date(2025, 3, 7)
    batches = [_b("first", d), _b("second", d)]
    out = rule2_sort_by_date.run(batches, config=config)
    assert [b.item_code for b in out] == ["first", "second"]
