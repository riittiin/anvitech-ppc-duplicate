"""Rule 6 — parallel split of an alternative-machine step (split_parallel)."""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters, Operator
from engine.rules import rule6_allocate


def _masters(suggested="M/N", n_machines=2):
    # CNC-typed so the 90-min setup applies (setup is CNC/VMC-only); split in practice
    # happens on alternative CNC/VMC machines.
    machines = {"M": Machine("M", "M", "CNC lathe"), "N": Machine("N", "N", "CNC lathe")}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "OP", cycle_time=10, total_time=None,
                           suggested_machine=suggested, allotted_machine=None)],
    )
    return masters


def _batch(qty):
    return Batch(batch_id="B1", item_code="X", item_name="X", qty=qty,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])


def _cfg(**kw):
    # These tests exercise the split MECHANISM at small quantities, so pin a low
    # threshold (the production default is 401 — only batches over 400 split).
    kw.setdefault("split_min_qty", 2)
    return Config(plan_start_date=date(2025, 3, 5), **kw)


def test_splits_qty_across_free_alternatives_in_parallel():
    masters = _masters("M/N")
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True), masters=masters)
    assert len(sched) == 2                                   # one entry per machine
    assert {e.machine for e in sched} == {"M", "N"}
    assert sorted(e.qty for e in sched) == [25, 25]          # 50 split evenly
    assert {e.start for e in sched} == {sched[0].start}      # both start together (parallel)
    # Each half: 25*10 + 90 = 340 min; single machine would be 50*10 + 90 = 590.
    assert all(e.occupancy_min == 340 for e in sched)


def test_odd_quantity_splits_with_remainder():
    sched = rule6_allocate.run([_batch(51)], config=_cfg(split_parallel=True), masters=_masters("M/N"))
    assert sorted(e.qty for e in sched) == [25, 26]


def test_off_by_default_runs_on_one_machine():
    sched = rule6_allocate.run([_batch(50)], config=_cfg(), masters=_masters("M/N"))
    assert len(sched) == 1 and sched[0].qty == 50            # no split when flag off


def test_no_split_when_only_one_machine_listed():
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True), masters=_masters("M"))
    assert len(sched) == 1 and sched[0].machine == "M"


def test_no_split_below_min_qty():
    cfg = _cfg(split_parallel=True, split_min_qty=10)
    sched = rule6_allocate.run([_batch(8)], config=cfg, masters=_masters("M/N"))
    assert len(sched) == 1                                   # 8 < 10 → not split


def test_split_only_above_400_by_default_else_single_least_queued():
    # Business rule: parallel split only for batches OVER 400; smaller batches go
    # entirely to the least-queued alternative. Uses the REAL default threshold.
    base = dict(plan_start_date=date(2025, 3, 5), split_parallel=True)   # default split_min_qty=401
    # 400 -> NOT split (400 is not "more than 400").
    s400 = rule6_allocate.run([_batch(400)], config=Config(**base), masters=_masters("M/N"))
    assert len(s400) == 1 and s400[0].qty == 400
    # 401 -> split across the alternatives.
    s401 = rule6_allocate.run([_batch(401)], config=Config(**base), masters=_masters("M/N"))
    assert len(s401) == 2 and {e.machine for e in s401} == {"M", "N"}
    # Below the threshold with one machine busy, the whole batch goes to the
    # LEAST-queued machine (M is free; N is loaded).
    s = rule6_allocate.run([_batch(300)], config=Config(**base),
                           masters=_masters("M/N"), machine_lost_min={"N": 500})
    assert len(s) == 1 and s[0].machine == "M"


def test_no_split_when_second_machine_free_too_late():
    # N is busy 600 min — it can't finish any share before M would finish alone
    # (50*10+90 = 590) → no split.
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True),
                               masters=_masters("M/N"), machine_lost_min={"N": 600})
    assert len(sched) == 1 and sched[0].machine == "M"


def test_splits_even_when_second_machine_frees_soon():
    # N is busy only 100 min. It frees soon and has low load → it still takes a
    # (smaller) share so the step finishes sooner; M (free now) takes more.
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True),
                               masters=_masters("M/N"), machine_lost_min={"N": 100})
    assert len(sched) == 2                       # split happened
    bym = {e.machine: e.qty for e in sched}
    assert bym["M"] > bym["N"]                   # M (free now) gets the larger share
    assert bym["M"] + bym["N"] == 50
    # And it finishes sooner than running all 50 on M.
    single = rule6_allocate.run([_batch(50)], config=_cfg(), masters=_masters("M"))
    assert max(e.end for e in sched) < single[0].end


def test_faster_machine_gets_the_larger_share():
    # M = two-shift (more hours/day), N = single-shift manual (9-6); both free +
    # operator-covered → the faster machine takes more so both finish together.
    machines = {"M": Machine("M", "M", "CNC lathe", available_hrs_per_day=19.5),
                "N": Machine("N", "N", "Manual Inspection", available_hrs_per_day=9.5)}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "OP", 10, None, "M/N", None)])
    ops = [Operator("om", "M", machines=["M"], shift="First shift"),
           Operator("om2", "M", machines=["M"], shift="Second shift"),
           Operator("on", "N", machines=["N"], shift="First shift")]
    masters.operators = ops
    sched = rule6_allocate.run([_batch(50)],
                               config=_cfg(split_parallel=True, apply_operator_logic=True),
                               masters=masters)
    assert len(sched) == 2
    bym = {e.machine: e.qty for e in sched}
    assert bym["M"] > bym["N"]                   # two-shift machine takes the bigger load


def test_split_finishes_sooner_than_single():
    m = _masters("M/N")
    single = rule6_allocate.run([_batch(50)], config=_cfg(), masters=m)
    split = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True), masters=_masters("M/N"))
    single_end = max(e.end for e in single)
    split_end = max(e.end for e in split)
    assert split_end < single_end
