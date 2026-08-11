"""run_forward(priority_rank=…) — replay a saved optimized sequence.

The rank map (from the Optimize feature) is keyed by the composite order key
"<SO No>\x1f<Item Code>". Ranked batches are reordered among the slots they
already occupy after Rule 3; unranked batches keep their Rule-3 positions, so a
brand-new order keeps its natural priority. ``None`` (the default and every
existing caller) is byte-identical to today."""
from datetime import date

from engine.config import Config
from engine.models import SOLine, Process, Routing, Machine, WorkCalendar, Masters, PlanRun
from engine.pipeline import run_forward, KEY_SEP


def _masters(items=("X", "Y", "Z")):
    ms = {"M": Machine(machine_no="M", display_name="M", machine_type="CNC lathe",
                       available_hrs_per_day=19.5)}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    for it in items:
        mm.routings[it] = Routing(item_code=it, description=it, customer="", rm_type="",
                                  moq=None, processes=[Process(1, "CNC", 5, 5, "M", None)])
    return mm


def _lines():
    # Three items -> three batches. Delivery dates give a stable Rule-3 order X, Y, Z.
    return [
        SOLine(so_no="S1", item_code="X", item_name="X", qty=10, delivery_date=date(2025, 3, 20)),
        SOLine(so_no="S2", item_code="Y", item_name="Y", qty=10, delivery_date=date(2025, 3, 25)),
        SOLine(so_no="S3", item_code="Z", item_name="Z", qty=10, delivery_date=date(2025, 3, 30)),
    ]


def _cfg():
    return Config(plan_start_date=date(2025, 3, 5))


def _run(priority_rank=None, lines=None, masters=None):
    pr = PlanRun(so_lines=lines or _lines())
    trace = run_forward(pr, _cfg(), masters or _masters(), priority_rank=priority_rank)
    return pr, trace


def _order(pr):
    return [b.item_code for b in pr.batches_prioritized]


def test_none_is_byte_identical():
    pr_default, tr_default = _run()                     # no arg path (existing callers)
    pr_none, tr_none = _run(priority_rank=None)
    assert _order(pr_default) == _order(pr_none) == ["X", "Y", "Z"]
    assert tr_default["rule3"]["output"] == tr_none["rule3"]["output"]
    assert [e.as_row() for e in pr_default.schedule] == [e.as_row() for e in pr_none.schedule]


def test_ranked_batches_reorder_among_their_slots():
    # Rank Z first, X second; Y unranked. Ranked slots are positions 0 (X) and 2 (Z)
    # -> Z takes slot 0, X takes slot 2, Y keeps its own slot 1.
    rank = {f"S3{KEY_SEP}Z": 1, f"S1{KEY_SEP}X": 2}
    pr, trace = _run(priority_rank=rank)
    assert _order(pr) == ["Z", "Y", "X"]
    # The Rule-3 tab shows the replayed order and says so.
    out = trace["rule3"]["output"]
    col = out["columns"].index("Item Code")
    assert [r[col] for r in out["rows"]] == ["Z", "Y", "X"]
    assert any("Optimized sequence" in n for n in trace["rule3"]["notes"])


def test_unranked_orders_keep_their_rule3_position():
    # Only Z is ranked -> nothing else moves; Z occupies the only ranked slot (its own).
    pr, _ = _run(priority_rank={f"S3{KEY_SEP}Z": 1})
    assert _order(pr) == ["X", "Y", "Z"]


def test_rank_map_with_no_matching_orders_is_inert():
    pr, trace = _run(priority_rank={f"GONE{KEY_SEP}OLD": 1})
    assert _order(pr) == ["X", "Y", "Z"]
    assert not any("Optimized sequence" in n for n in trace["rule3"]["notes"])


def test_consolidated_batch_uses_min_member_rank():
    # Two SO lines of the SAME item within the window -> one batch whose rank is
    # the best (minimum) of its members' ranks.
    masters = _masters(items=("X", "Y"))
    lines = [
        SOLine(so_no="A1", item_code="X", item_name="X", qty=5, delivery_date=date(2025, 3, 20)),
        SOLine(so_no="A2", item_code="X", item_name="X", qty=5, delivery_date=date(2025, 3, 22)),
        SOLine(so_no="B1", item_code="Y", item_name="Y", qty=5, delivery_date=date(2025, 3, 18)),
    ]
    # Y is due first -> Rule 3 order [Y, X]. Rank X's second line ahead of Y.
    rank = {f"A2{KEY_SEP}X": 1, f"B1{KEY_SEP}Y": 2}
    pr, _ = _run(priority_rank=rank, lines=lines, masters=masters)
    assert _order(pr) == ["X", "Y"]


def test_schedule_actually_follows_the_replayed_order():
    # With one machine and equal work, the first-priority batch starts first.
    rank = {f"S3{KEY_SEP}Z": 1, f"S1{KEY_SEP}X": 2, f"S2{KEY_SEP}Y": 3}
    pr, _ = _run(priority_rank=rank)
    starts = {e.item_code: e.start for e in pr.schedule}
    assert starts["Z"] < starts["X"] < starts["Y"]
