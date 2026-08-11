"""Rule 6 — parallelization toggle chooses Allotted-only vs Allotted∪Suggested."""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _masters(sug, allot, machines, cyc=1):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type="CNC lathe",
                     available_hrs_per_day=19.5) for m in machines}
    masters = Masters(machines=ms, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None,
                                    processes=[Process(1, "CNC", cyc, cyc, sug, allot)])
    return masters


def _batch(qty):
    return Batch(batch_id="B", item_code="X", item_name="x", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["B"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)


def _used(sched):
    return {e.machine for e in sched}


def test_off_uses_allotted_only():
    # sug CNC3/CNC6, allot CNC3, split OFF -> runs on CNC3, never CNC6.
    m = _masters(sug="CNC3/CNC6", allot="CNC3", machines=("CNC3", "CNC6"))
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=False), masters=m)
    assert _used(sched) == {"CNC3"}


def test_off_blank_allotted_falls_back_to_suggested():
    # allot blank, sug CNC6, split OFF -> still schedules (fallback), on CNC6.
    m = _masters(sug="CNC6", allot=None, machines=("CNC3", "CNC6"))
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=False), masters=m)
    assert _used(sched) == {"CNC6"}


def test_on_uses_union_of_allotted_and_suggested():
    # allot CNC4, sug CNC3/CNC6, split ON, large batch -> a SUGGESTED-only machine
    # (CNC6) AND the allotted (CNC4) both get work: the union is in play.
    m = _masters(sug="CNC3/CNC6", allot="CNC4", machines=("CNC3", "CNC4", "CNC6"))
    sched = rule6_allocate.run([_batch(1000)],
                               config=_cfg(split_parallel=True, split_min_qty=401), masters=m)
    used = _used(sched)
    assert "CNC4" in used and "CNC6" in used   # allotted + suggested-only both used
    assert len(sched) >= 2                       # physically split


def test_on_contrasts_with_off_on_same_routing():
    # Same routing: OFF stays on the allotted machine, ON reaches the suggested-only one.
    m = _masters(sug="CNC3/CNC6", allot="CNC4", machines=("CNC3", "CNC4", "CNC6"))
    off = rule6_allocate.run([_batch(1000)],
                             config=_cfg(split_parallel=False, split_min_qty=401), masters=m)
    assert _used(off) == {"CNC4"}               # OFF -> allotted only


def test_on_keeps_the_over_400_split_threshold():
    # split ON but batch <= 400 -> NOT physically split (avoid a 2nd setup); one entry.
    m = _masters(sug="CNC3/CNC6", allot="CNC4", machines=("CNC3", "CNC4", "CNC6"))
    sched = rule6_allocate.run([_batch(50)],
                               config=_cfg(split_parallel=True, split_min_qty=401), masters=m)
    assert len(sched) == 1


def test_os_detection_is_toggle_independent():
    # An Allotted=OS step is OS regardless; a named-'OS' step WITH a real machine is not.
    os_step = Process(1, "CNC OS", 3600, None, None, "OS")
    assert rule6_allocate._is_os(os_step) is True
    real_named_os = Process(1, "CNC OS", 5, 5, "CNC1/CNC2", None)
    assert rule6_allocate._is_os(real_named_os) is False
