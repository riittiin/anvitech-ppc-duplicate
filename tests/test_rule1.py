"""Rule 1 — consolidation. Self-contained SO lines (no workbook needed).

One item with three lines: 21/03 + 28/03 (7 days apart -> clubbed) and 10/04
(outside the 10-day window -> separate)."""
from datetime import date

from engine.config import Config
from engine.models import SOLine
from engine.rules import rule1_consolidate


def _lines():
    return [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5, delivery_date=date(2025, 3, 21)),
        SOLine(so_no="L2", item_code="X", item_name="X", qty=10, delivery_date=date(2025, 3, 28)),
        SOLine(so_no="L3", item_code="X", item_name="X", qty=10, delivery_date=date(2025, 4, 10)),
    ]


def test_process_qty_sums_across_clubbed_lines():
    # Two clubbed lines, each carrying per-process remaining -> batch sums them.
    lines = [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5, delivery_date=date(2025, 3, 21),
               process_qty={"P1": 4, "P2": 5}),
        SOLine(so_no="L2", item_code="X", item_name="X", qty=10, delivery_date=date(2025, 3, 28),
               process_qty={"P1": 10, "P2": 8}),
    ]
    batch = rule1_consolidate.run(lines, config=Config())[0]
    assert batch.qty == 15
    assert batch.process_qty == {"P1": 14, "P2": 13}


def test_process_qty_none_when_no_line_has_progress():
    batch = rule1_consolidate.run(_lines(), config=Config())[0]
    assert batch.process_qty is None


def test_process_qty_mixes_progress_and_fresh_line():
    # One line has progress, the other is fresh (None) -> fresh contributes full qty.
    lines = [
        SOLine(so_no="L1", item_code="X", item_name="X", qty=5, delivery_date=date(2025, 3, 21),
               process_qty={"P1": 4, "P2": 5}),
        SOLine(so_no="L2", item_code="X", item_name="X", qty=10, delivery_date=date(2025, 3, 28)),
    ]
    batch = rule1_consolidate.run(lines, config=Config())[0]
    assert batch.process_qty == {"P1": 14, "P2": 15}     # P1: 4+10, P2: 5+10


def test_club_within_window_and_split_outside():
    batches = rule1_consolidate.run(_lines(), config=Config())
    assert len(batches) == 2

    clubbed = next(b for b in batches if len(b.source_so_refs) == 2)
    separate = next(b for b in batches if len(b.source_so_refs) == 1)

    # 21/03 (qty 5) + 28/03 (qty 10) -> qty 15, SO delivery date = earliest (21/03).
    assert clubbed.qty == 15
    assert clubbed.so_delivery_date.isoformat() == "2025-03-21"
    # 10/04 line stands alone.
    assert separate.qty == 10
    assert separate.so_delivery_date.isoformat() == "2025-04-10"


def test_window_is_configurable():
    cfg = Config(consolidation_window_days=30)  # now 10/04 is within window of 21/03
    batches = rule1_consolidate.run(_lines(), config=cfg)
    assert len(batches) == 1
    assert batches[0].qty == 25
