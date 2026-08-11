from datetime import date, datetime
from types import SimpleNamespace as NS

from api.main import _expected_by_order, _movers, _format_movers


def test_expected_by_order_takes_latest_end_per_order():
    e1 = NS(end=datetime(2025, 3, 10, 9, 0), so_refs=["SO1"], item_code="A")
    e2 = NS(end=datetime(2025, 3, 14, 9, 0), so_refs=["SO1"], item_code="A")  # later
    got = _expected_by_order([e1, e2])
    assert got[("SO1", "A")] == date(2025, 3, 14)


def test_movers_flags_only_orders_that_moved_later_beyond_threshold():
    old = {("SO1", "A"): date(2025, 3, 10), ("SO2", "B"): date(2025, 3, 10),
           ("SO3", "C"): date(2025, 3, 10)}
    new = {("SO1", "A"): date(2025, 3, 16),   # +6d -> flagged
           ("SO2", "B"): date(2025, 3, 11),   # +1d -> NOT (threshold is >1)
           ("SO3", "C"): date(2025, 3, 4)}    # earlier -> NOT
    out = _movers(old, new, threshold=1)
    assert out == [(("SO1", "A"), 6, date(2025, 3, 16))]


def test_format_movers_empty_is_blank():
    assert _format_movers([]) == ""


def test_format_movers_lists_worst_first_and_counts_overflow():
    movers = [(("SO1", "A"), 6, date(2025, 3, 16)),
              (("SO2", "B"), 4, date(2025, 3, 14)),
              (("SO3", "C"), 3, date(2025, 3, 13)),
              (("SO4", "D"), 2, date(2025, 3, 12))]
    s = _format_movers(movers)
    assert s.startswith(" ⚠ 4 order(s) now finish later than before: ")
    assert "SO1-A +6d" in s and "16-Mar" in s
    assert "+1 more" in s          # 4 movers, only top 3 named
