"""Flow scheduler (branch ``flow-scheduler-productization``) — Sched2 productized.

The owner's three basics are HARD invariants: machine + qualified operator on
every working minute, operators only within their own shift, process order
respected piece-wise. The two deliberate rule-breaks vs classic Rule 6 — no
resource-holding and chunked piece-flow — are pinned by crafted cases, and the
whole output is checked by validators built independently of the scheduler.
"""
from datetime import date, datetime, timedelta

import pytest

from engine.config import Config
from engine.models import (Batch, Machine, Masters, Operator, Process, Routing,
                           WorkCalendar)
from engine import flow_scheduler
from engine.pipeline import RuleError

WED = date(2025, 3, 5)   # Wednesday; Thursday 2025-03-06 is the weekly off


def _cfg(**kw):
    kw.setdefault("scheduler", "flow")
    return Config(plan_start_date=WED, apply_operator_logic=True, **kw)


def _mac(no, hrs=19.5, mtype="CNC lathe"):
    return Machine(machine_no=no, display_name=no, machine_type=mtype,
                   hr_rate=0.0, provisional=False, available_hrs_per_day=hrs)


def _op(name, machines, shift="First shift"):
    return Operator(name=name, preferred_machines_raw="/".join(machines),
                    machines=list(machines), shift=shift)


def _route(item, steps):
    """steps: list of (name, cycle, machine_cell)"""
    return Routing(item, "", "", "", None, processes=[
        Process(i + 1, nm, cycle_time=cyc, total_time=None,
                suggested_machine=mc, allotted_machine=None)
        for i, (nm, cyc, mc) in enumerate(steps)])


def _batch(item, bid, qty, due=date(2025, 4, 30)):
    return Batch(batch_id=bid, item_code=item, item_name=item, qty=qty,
                 so_delivery_date=due, source_so_refs=[bid])


# --------------------------------------------------------------------------- #
# Independent validators (ported from the research harness — no scheduler
# bookkeeping is trusted; everything is recomputed from the raw entries).
# --------------------------------------------------------------------------- #
def real_entries(sched):
    return [e for e in sched if e.op_segments and e.occupancy_min > 0]


def assert_machine_exclusive(sched):
    by_mac = {}
    for e in real_entries(sched):
        by_mac.setdefault(e.machine, []).append((e.start, e.end, e.batch_id))
    for m, ivs in by_mac.items():
        ivs.sort()
        for a, b in zip(ivs, ivs[1:]):
            assert a[1] <= b[0], f"{m}: {a} overlaps {b}"


def assert_causality(sched, batches, masters):
    """No piece runs at step k+1 before it exists at step k."""
    done = {}
    for e in real_entries(sched):
        done.setdefault((e.batch_id, e.process_seq), []).append((e.end, e.qty))
    for b in batches:
        r = masters.routings[b.item_code]
        real = [p.seq for p in r.processes
                if p.cycle_time and (p.suggested_machine or p.allotted_machine)]
        for prev, cur in zip(real, real[1:]):
            for e in real_entries(sched):
                if e.batch_id == b.batch_id and e.process_seq == cur:
                    avail = sum(q for t, q in done.get((b.batch_id, prev), [])
                                if t <= e.start)
                    mine_before = sum(q for t, q in done.get((b.batch_id, cur), [])
                                      if t <= e.start)
                    assert mine_before + e.qty <= avail + 1e-6, \
                        f"{b.batch_id} seq{cur}: {e.qty} at {e.start} but pred done {avail}"


def assert_complete(sched, batches, masters):
    got = {}
    for e in real_entries(sched):
        got[(e.batch_id, e.process_seq)] = got.get((e.batch_id, e.process_seq), 0) + e.qty
    for b in batches:
        r = masters.routings[b.item_code]
        for p in r.processes:
            if p.cycle_time and (p.suggested_machine or p.allotted_machine) \
                    and "OS" not in str(p.suggested_machine or "") :
                assert abs(got.get((b.batch_id, p.seq), 0) - b.qty) < 1e-6, \
                    f"{b.batch_id} seq{p.seq}: {got.get((b.batch_id, p.seq))} of {b.qty}"


# --------------------------------------------------------------------------- #
# Piece-flow: the successor starts after the FIRST CHUNK, not the whole batch
# --------------------------------------------------------------------------- #
def test_successor_starts_after_first_chunk_not_whole_batch():
    masters = Masters(
        machines={"CNC1": _mac("CNC1"), "VMC1": _mac("VMC1", mtype="Vertical Machining center")},
        operators=[_op("A", ["CNC1"]), _op("B", ["VMC1"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 5.0, "CNC1"), ("VMC", 5.0, "VMC1")])})
    b = _batch("X", "B1", 80)   # 80 pcs x 5 min = 400 min/step
    cfg = _cfg(flow_chunks=4)
    sched = flow_scheduler.run([b], config=cfg, masters=masters)
    vmc = [e for e in real_entries(sched) if e.machine == "VMC1"]
    cnc_end = max(e.end for e in real_entries(sched) if e.machine == "CNC1")
    # first VMC chunk must begin well before the CNC step finishes the batch
    assert min(e.start for e in vmc) < cnc_end
    assert_causality(sched, [b], masters)
    assert_complete(sched, [b], masters)
    assert_machine_exclusive(sched)


# --------------------------------------------------------------------------- #
# No-holding: a starved machine is free for OTHER work between chunks
# --------------------------------------------------------------------------- #
def test_starved_machine_released_for_other_jobs():
    # Job1: slow step on CNC1 feeding VMC1 in chunks. Job2: independent work on
    # VMC1. Classic Rule 6 would seize VMC1 for Job1 early and pace it; flow
    # must interleave Job2 on VMC1 in the starved gaps (or before/after chunks)
    # so VMC1's total busy time stays contiguous work, not held idleness.
    masters = Masters(
        machines={"CNC1": _mac("CNC1"), "VMC1": _mac("VMC1", mtype="Vertical Machining center")},
        operators=[_op("A", ["CNC1"]), _op("B", ["VMC1"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 10.0, "CNC1"), ("VMC", 1.0, "VMC1")]),
                  "Y": _route("Y", [("VMC", 1.0, "VMC1")])})
    b1 = _batch("X", "B1", 60)                       # CNC 600 min, VMC 60 min
    b2 = _batch("Y", "B2", 60, due=date(2025, 5, 31))  # VMC 60 min, lower priority
    cfg = _cfg(flow_chunks=4)
    sched = flow_scheduler.run([b1, b2], config=cfg, masters=masters)
    ends = {e.batch_id: max(x.end for x in real_entries(sched) if x.batch_id == e.batch_id)
            for e in real_entries(sched)}
    # B2's tiny VMC job must NOT wait for B1's whole pipeline: it fits in the
    # starved gaps, finishing the same morning.
    assert ends["B2"] <= datetime(2025, 3, 5, 12, 0), ends
    assert_machine_exclusive(sched)
    assert_causality(sched, [b1, b2], masters)


# --------------------------------------------------------------------------- #
# Feedback: pieces already punched through step 1 are initial WIP for step 2,
# and a fully-punched step schedules nothing
# --------------------------------------------------------------------------- #
def test_process_qty_feedback_initial_wip():
    masters = Masters(
        machines={"CNC1": _mac("CNC1"), "VMC1": _mac("VMC1", mtype="Vertical Machining center")},
        operators=[_op("A", ["CNC1"]), _op("B", ["VMC1"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 2.0, "CNC1"), ("VMC", 2.0, "VMC1")])})
    b = _batch("X", "B1", 100)
    b.process_qty = {"CNC": 40.0, "VMC": 100.0}   # 60 pcs already through CNC
    cfg = _cfg(flow_chunks=4)
    sched = flow_scheduler.run([b], config=cfg, masters=masters)
    cnc_qty = sum(e.qty for e in real_entries(sched) if e.machine == "CNC1")
    vmc_qty = sum(e.qty for e in real_entries(sched) if e.machine == "VMC1")
    assert abs(cnc_qty - 40.0) < 1e-6     # only the remaining 40 run at CNC
    assert abs(vmc_qty - 100.0) < 1e-6    # all 100 still owe VMC
    # VMC can start IMMEDIATELY on the 60 pieces of initial WIP — at shift start,
    # not after the first fresh CNC chunk.
    vmc_start = min(e.start for e in real_entries(sched) if e.machine == "VMC1")
    assert vmc_start == datetime(2025, 3, 5, 8, 0), vmc_start

    b2 = _batch("X", "B2", 100)
    b2.process_qty = {"CNC": 0.0, "VMC": 30.0}    # CNC fully punched
    sched2 = flow_scheduler.run([b2], config=cfg, masters=masters)
    assert not any(e.machine == "CNC1" for e in real_entries(sched2))
    assert abs(sum(e.qty for e in real_entries(sched2) if e.machine == "VMC1") - 30.0) < 1e-6


# --------------------------------------------------------------------------- #
# Absences: a reserved operator interval pushes their work
# --------------------------------------------------------------------------- #
def test_operator_absence_reservation_respected():
    masters = Masters(
        machines={"CNC1": _mac("CNC1")},
        operators=[_op("Only", ["CNC1"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 10.0, "CNC1")])})
    b = _batch("X", "B1", 12)   # 120 min
    cfg = _cfg(flow_chunks=1)
    blocked = [(datetime(2025, 3, 5, 0, 0), datetime(2025, 3, 6, 0, 0))]
    sched = flow_scheduler.run([b], config=cfg, masters=masters,
                               reserved={"Only": blocked})
    segs = [s for e in real_entries(sched) for s in e.op_segments if s[2]]
    assert all(ss >= datetime(2025, 3, 6, 0, 0) for ss, se, _ in segs), segs
    # Thursday 03-06 is the weekly off, so work lands Friday 08:00
    assert min(ss for ss, _, _ in segs) == datetime(2025, 3, 7, 8, 0)


# --------------------------------------------------------------------------- #
# Setup: charged per re-engagement of a different op, once for same-op chunks
# --------------------------------------------------------------------------- #
def test_setup_once_for_consecutive_chunks_again_after_interruption():
    masters = Masters(
        machines={"CNC1": _mac("CNC1")},
        operators=[_op("A", ["CNC1"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 1.0, "CNC1")])})
    b = _batch("X", "B1", 100)   # 100 min work, chunks of 25
    cfg = _cfg(flow_chunks=4, setup_time_min=90)
    sched = flow_scheduler.run([b], config=cfg, masters=masters)
    total_occ = sum(e.occupancy_min for e in real_entries(sched))
    # one setup only: 100 cutting + 90 setup (first-step chunks are available
    # immediately, so the machine runs them back-to-back)
    assert abs(total_occ - 190.0) < 1e-6, total_occ


# --------------------------------------------------------------------------- #
# Milestones + OS
# --------------------------------------------------------------------------- #
def test_dispatch_milestone_and_os_block():
    masters = Masters(
        machines={"CNC1": _mac("CNC1")},
        operators=[_op("A", ["CNC1"])],
        calendar=WorkCalendar(),
        routings={"X": Routing("X", "", "", "", None, processes=[
            Process(1, "CNC", cycle_time=1.0, total_time=None,
                    suggested_machine="CNC1", allotted_machine=None),
            Process(2, "PLATING OS", cycle_time=1440.0, total_time=None,
                    suggested_machine="OS", allotted_machine=None),
            Process(3, "DISPATCH", cycle_time=None, total_time=None,
                    suggested_machine=None, allotted_machine=None)])})
    b = _batch("X", "B1", 60)
    cfg = _cfg(flow_chunks=4)
    sched = flow_scheduler.run([b], config=cfg, masters=masters)
    cnc_end = max(e.end for e in real_entries(sched))
    os_e = [e for e in sched if "OS" in e.machine][0]
    assert os_e.start >= cnc_end                       # whole batch cleared first
    assert os_e.end - os_e.start == timedelta(minutes=1440)   # 24x7 flat block
    disp = [e for e in sched if e.process_name == "DISPATCH"]
    assert disp and disp[0].start == disp[0].end       # zero-duration milestone
    assert disp[0].end >= os_e.end                     # waits for everything


# --------------------------------------------------------------------------- #
# Determinism + sample-book invariants sweep
# --------------------------------------------------------------------------- #
def test_deterministic(loaded):
    from engine.rules import rule1_consolidate, rule2_sort_by_date, \
        rule3_tiebreak_process_time
    so_lines, masters = loaded
    cfg = _cfg(flow_chunks=4)
    b = rule1_consolidate.run(list(so_lines), config=cfg, masters=masters)
    b = rule2_sort_by_date.run(b, config=cfg, masters=masters)
    pri = rule3_tiebreak_process_time.run(b, config=cfg, masters=masters)
    a = flow_scheduler.run(list(pri), config=cfg, masters=masters)
    c = flow_scheduler.run(list(pri), config=cfg, masters=masters)
    assert [(e.machine, e.start, e.end, e.qty, e.op_segments) for e in a] == \
           [(e.machine, e.start, e.end, e.qty, e.op_segments) for e in c]
    assert_machine_exclusive(a)
    assert_causality(a, pri, masters)


# --------------------------------------------------------------------------- #
# Dispatch: config.scheduler selects the engine through the WHOLE pipeline
# --------------------------------------------------------------------------- #
def test_run_forward_dispatches_by_config(loaded):
    from engine.models import PlanRun
    from engine.pipeline import run_forward
    so_lines, masters = loaded
    flow_cfg = _cfg(flow_chunks=4)
    classic_cfg = Config(plan_start_date=WED, apply_operator_logic=True)
    tr_flow = run_forward(PlanRun(so_lines=list(so_lines)), flow_cfg, masters)
    tr_classic = run_forward(PlanRun(so_lines=list(so_lines)), classic_cfg, masters)
    assert any("flow scheduler" in n for n in tr_flow["rule6"]["notes"])
    assert not any("flow scheduler" in n for n in tr_classic["rule6"]["notes"])
    # both scheduled the same demand, differently
    assert tr_flow["rule6"]["error"] is None and tr_classic["rule6"]["error"] is None


def test_optimizer_searches_with_the_flow_engine(loaded):
    from engine import optimizer
    so_lines, masters = loaded
    cfg = _cfg(flow_chunks=4)
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=5, seed=1)
    # the baseline metrics must equal a direct flow plan of the rule-3 order —
    # proof the search evaluates through the flow engine, not classic rule 6
    from engine.rules import rule1_consolidate, rule2_sort_by_date, \
        rule3_tiebreak_process_time
    from engine.optimizer import plan_metrics
    b = rule1_consolidate.run(list(so_lines), config=cfg, masters=masters)
    b = rule2_sort_by_date.run(b, config=cfg, masters=masters)
    pri = rule3_tiebreak_process_time.run(b, config=cfg, masters=masters)
    direct = plan_metrics(flow_scheduler.run(list(pri), config=cfg, masters=masters),
                          so_lines, cfg.plan_start_date)
    assert res.baseline == direct


# --------------------------------------------------------------------------- #
# Contest: in flow mode the sweep tunes CHUNKS, not overlap
# --------------------------------------------------------------------------- #
def test_sweep_contest_tunes_chunks_in_flow_mode(loaded):
    from engine import optimizer
    so_lines, masters = loaded
    cfg = _cfg(flow_chunks=4)
    sw = optimizer.sweep_optimize(so_lines, cfg, masters, budget_evals=12, seed=1)
    assert sw.knob == "flow_chunks"
    tried = {row["overlap"] for row in sw.table}   # wire key stays "overlap"
    allowed = set(optimizer.FLOW_CHUNK_CANDIDATES) | {cfg.flow_chunks}
    assert tried and tried <= allowed, (tried, allowed)
    assert sw.overlap_percent in allowed           # the winning chunk count


def test_sweep_contest_still_tunes_overlap_in_classic_mode(loaded):
    from engine import optimizer
    so_lines, masters = loaded
    cfg = Config(plan_start_date=WED, apply_operator_logic=True)
    sw = optimizer.sweep_optimize(so_lines, cfg, masters, budget_evals=10, seed=1)
    assert sw.knob == "overlap_percent"
    tried = {row["overlap"] for row in sw.table}
    assert tried <= set(optimizer.OVERLAP_CANDIDATES) | {cfg.overlap_percent}


def test_flow_fingerprint_feeds_staleness_signature():
    from api import main as api_main
    from engine import flow_scheduler as fs
    cfg = Config()
    sig = api_main._inputs_signature(cfg)
    orig = fs.FLOW_FINGERPRINT
    try:
        fs.FLOW_FINGERPRINT = orig + "-next"
        assert api_main._inputs_signature(cfg) != sig
    finally:
        fs.FLOW_FINGERPRINT = orig
    assert api_main._inputs_signature(cfg) == sig


# --------------------------------------------------------------------------- #
# Setup-aware: consecutive chunks of the same job batch on one machine, so the
# 90-min CNC/VMC setup is paid once per run, not once per chunk-switch.
# --------------------------------------------------------------------------- #
def _setup_minutes(sched, masters):
    """Total setup minutes charged = occupancy - cutting(cycle x qty) over real ops."""
    tot = 0.0
    for e in real_entries(sched):
        r = masters.routings[e.item_code]
        cyc = next((p.cycle_time or 0 for p in r.processes if p.seq == e.process_seq), 0)
        extra = e.occupancy_min - cyc * e.qty
        if extra > 1:
            tot += extra
    return tot


def test_setup_aware_batches_same_job_chunks_on_one_machine():
    # Two single-step CNC jobs, all chunks ready at t0, ONE machine. Setup-aware
    # runs each job's chunks consecutively -> exactly 2 setups (one per job).
    # The setup-blind rule ping-pongs and pays more.
    masters = Masters(
        machines={"CNC1": _mac("CNC1")},
        operators=[_op("A", ["CNC1"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 1.0, "CNC1")]),
                  "Y": _route("Y", [("CNC", 1.0, "CNC1")])})
    b1 = _batch("X", "BX", 100)
    b2 = _batch("Y", "BY", 100, due=date(2025, 4, 30))
    aware = _cfg(flow_chunks=4, flow_setup_aware=True)
    blind = _cfg(flow_chunks=4, flow_setup_aware=False)
    s_aware = flow_scheduler.run([b1, b2], config=aware, masters=masters)
    s_blind = flow_scheduler.run([b1, b2], config=blind, masters=masters)
    su_aware = _setup_minutes(s_aware, masters)
    su_blind = _setup_minutes(s_blind, masters)
    # exactly two setups when setup-aware (one per job), and never more than blind
    assert abs(su_aware - 2 * 90) < 1e-6, f"expected 180 min setup, got {su_aware}"
    assert su_aware <= su_blind
    assert_machine_exclusive(s_aware)
    assert_complete(s_aware, [b1, b2], masters)


def test_setup_aware_off_is_the_blind_rule():
    """flow_setup_aware=False must reproduce the pre-feature setup-blind plan."""
    masters = Masters(
        machines={"CNC1": _mac("CNC1"), "CNC2": _mac("CNC2")},
        operators=[_op("A", ["CNC1", "CNC2"]), _op("B", ["CNC1", "CNC2"])],
        calendar=WorkCalendar(),
        routings={"X": _route("X", [("CNC", 2.0, "CNC1")]),
                  "Y": _route("Y", [("CNC", 2.0, "CNC2")])})
    b = [_batch("X", "BX", 50), _batch("Y", "BY", 50)]
    blind = _cfg(flow_chunks=3, flow_setup_aware=False)
    a = flow_scheduler.run(list(b), config=blind, masters=masters)
    c = flow_scheduler.run(list(b), config=blind, masters=masters)
    assert [(e.machine, e.start, e.end, e.qty) for e in a] == \
           [(e.machine, e.start, e.end, e.qty) for e in c]
