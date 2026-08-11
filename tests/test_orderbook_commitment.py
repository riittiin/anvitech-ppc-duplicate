from datetime import date
from engine import orderbook
from engine.models import Order


def _orders():
    return {
        ("SO1", "A"): Order("SO1", "A", "A", 10, date(2026, 7, 20),
                            commitment="committed", promised_date=date(2026, 7, 22)),
        ("SO2", "B"): Order("SO2", "B", "B", 10, date(2026, 7, 25)),   # open
    }


def test_active_so_lines_carry_lane_and_promise():
    lines = orderbook.active_so_lines(_orders(), actuals=[], masters=None)
    by_item = {l.item_code: l for l in lines}
    assert by_item["A"].commitment == "committed"
    assert by_item["A"].promised_date == date(2026, 7, 22)
    assert by_item["B"].commitment == "open"


def test_split_committed_open_partitions_lines():
    lines = orderbook.active_so_lines(_orders(), actuals=[], masters=None)
    protected, open_lines = orderbook.split_committed_open(lines)
    assert [l.item_code for l in protected] == ["A"]
    assert [l.item_code for l in open_lines] == ["B"]


def test_order_rows_show_lane_and_promised():
    rows = orderbook.order_rows(_orders(), {}, actuals=[], masters=None)
    a = next(r for r in rows if r["Item Code"] == "A")
    assert a["Lane"] == "committed"
    assert a["Promised"] == "22-07-2026"
    b = next(r for r in rows if r["Item Code"] == "B")
    assert b["Lane"] == "open" and b["Promised"] == ""
