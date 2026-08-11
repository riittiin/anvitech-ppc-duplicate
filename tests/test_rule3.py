"""Rule 3 — workload-aware priority (slack), with the cycle-time-sum work metric.

Seeded with the sample's two items: SAMPLE RING A (cycle 3+5+2 = 10 min) has more
work than SAMPLE PIN B (cycle 4+2 = 6 min), so on an equal delivery date A (the
heavier, more at-risk order) must be prioritized first.
"""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule2_sort_by_date, rule3_tiebreak_process_time
from tests.sample_workbook import ITEM_A, ITEM_B


def _batch_from_routing(masters, item_code, d):
    return Batch(batch_id=item_code, item_code=item_code, item_name=item_code,
                 qty=10, so_delivery_date=d, source_so_refs=[item_code])


def test_higher_process_time_wins_same_date(loaded, config):
    _, masters = loaded
    d = date(2025, 3, 7)
    # Same delivery date; ITEM_A (more cycle time) must beat ITEM_B (less).
    batches = [
        _batch_from_routing(masters, ITEM_B, d),
        _batch_from_routing(masters, ITEM_A, d),
    ]
    ordered = rule2_sort_by_date.run(batches, config=config, masters=masters)
    ordered = rule3_tiebreak_process_time.run(ordered, config=config, masters=masters)
    assert [b.item_code for b in ordered] == [ITEM_A, ITEM_B]


def test_item_a_has_higher_total_process_time_than_b(loaded):
    _, masters = loaded
    assert (masters.routings[ITEM_A].total_cycle_time()
            > masters.routings[ITEM_B].total_cycle_time())


def _slack_masters():
    masters = Masters(
        machines={"M": Machine(machine_no="M", display_name="M", machine_type="t")},
        calendar=WorkCalendar(),
    )
    # 20 min/pc, single process -> a 100-pc batch needs far more work than a 1-pc one.
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "op", cycle_time=20, total_time=None,
                           suggested_machine="M", allotted_machine=None)],
    )
    return masters


def test_heavy_later_order_beats_trivial_earlier_order():
    """The scenario that broke the naive rule: 100 pcs due one day LATER must
    outrank 1 pc due earlier, because its slack (lateness risk) is far lower."""
    masters = _slack_masters()
    cfg = Config(plan_start_date=date(2025, 6, 2))  # metric=slack, no window (defaults)
    big = Batch(batch_id="BIG", item_code="X", item_name="X", qty=100,
                so_delivery_date=date(2025, 6, 18), source_so_refs=["BIG"])
    small = Batch(batch_id="SMALL", item_code="X", item_name="X", qty=1,
                  so_delivery_date=date(2025, 6, 17), source_so_refs=["SMALL"])

    ordered = rule3_tiebreak_process_time.run([small, big], config=cfg, masters=masters)
    assert [b.batch_id for b in ordered] == ["BIG", "SMALL"]


def _os_masters():
    """Item Y: one real machine step (10 min/pc) + a big OUTSOURCED block
    (machine cell = 'OS', 5000 min vendor turnaround). The OS block is a flat
    calendar wait, NOT per-piece machine work — it must never be multiplied by qty
    into Rule 3's work/slack, nor summed into the per-piece cycle-time total."""
    masters = Masters(
        machines={"M": Machine(machine_no="M", display_name="M", machine_type="t")},
        calendar=WorkCalendar(),
    )
    masters.routings["Y"] = Routing(
        item_code="Y", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "TURN", cycle_time=10, total_time=None,
                    suggested_machine="M", allotted_machine="M"),
            Process(2, "PAINTING OS", cycle_time=5000, total_time=None,
                    suggested_machine="OS", allotted_machine="OS"),
        ],
    )
    return masters


def test_os_turnaround_excluded_from_work_needed():
    """Work needed = real machine occupancy only. The 5000-min OS block must NOT
    be charged as 5000 x qty (that made an outsourced job masquerade as a
    million-minute machining job and dominate the slack ranking)."""
    masters = _os_masters()
    cfg = Config(plan_start_date=date(2025, 6, 2))
    routing = masters.routings["Y"]
    # Only the 10 min/pc TURN step: 10*50 + 90 setup. NOT + 5000*50.
    assert rule3_tiebreak_process_time._work_needed(routing, 50, cfg) == 10 * 50 + 90


def test_os_turnaround_excluded_from_cycle_per_piece():
    """The 'Cycle time per piece' column (Batch.total_process_time) = the real
    per-piece machining cycle only; the OS turnaround is not per-piece time."""
    masters = _os_masters()
    cfg = Config(plan_start_date=date(2025, 6, 2))
    batch = Batch(batch_id="Y", item_code="Y", item_name="Y", qty=50,
                  so_delivery_date=date(2025, 6, 20), source_so_refs=["Y"])
    rule3_tiebreak_process_time.run([batch], config=cfg, masters=masters)
    assert batch.total_process_time == 10  # not 10 + 5000


def test_legacy_window_zero_keeps_strict_date_order():
    """With window=0, a later-due heavy order does NOT jump an earlier one."""
    masters = _slack_masters()
    cfg = Config(plan_start_date=date(2025, 6, 2), priority_window_days=0)
    big = Batch(batch_id="BIG", item_code="X", item_name="X", qty=100,
                so_delivery_date=date(2025, 6, 18), source_so_refs=["BIG"])
    small = Batch(batch_id="SMALL", item_code="X", item_name="X", qty=1,
                  so_delivery_date=date(2025, 6, 17), source_so_refs=["SMALL"])

    ordered = rule3_tiebreak_process_time.run([small, big], config=cfg, masters=masters)
    assert [b.batch_id for b in ordered] == ["SMALL", "BIG"]  # earlier date stays first
