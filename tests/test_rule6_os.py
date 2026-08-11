"""Rule 6 — OS (outsourcing) steps reserve their cycle-time as a continuous block."""
from datetime import date, datetime, timedelta

from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _P(seq, name, cyc, sug=None, allot=None):
    return Process(seq=seq, name=name, cycle_time=cyc, total_time=None,
                   suggested_machine=sug, allotted_machine=allot)


def test_is_os_detects_allotted_os():
    assert rule6_allocate._is_os(_P(1, "CNC OS", 7200, sug=None, allot="OS"))


def test_is_os_name_only_when_no_real_machine():
    # name has 'OS' and no machine -> OS
    assert rule6_allocate._is_os(_P(1, "BANDSAW OS", None, sug=None, allot=None))
    # name has 'OS' BUT a real machine is assigned -> NOT OS (the sample's 'CNC OS')
    assert not rule6_allocate._is_os(_P(1, "CNC OS", 5, sug="CNC1/CNC2", allot=None))
    # ordinary step -> NOT OS
    assert not rule6_allocate._is_os(_P(1, "BANDSAW", 3, sug="BS1", allot=None))


def _masters(procs, machines=("M",)):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type="CNC lathe",
                     available_hrs_per_day=19.5) for m in machines}
    masters = Masters(machines=ms, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None, processes=procs)
    return masters


def _batch(qty=10, item="X"):
    return Batch(batch_id="B", item_code=item, item_name="x", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["B"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)   # 2025-03-05 is a Wednesday


def test_os_block_is_flat_not_multiplied_by_qty():
    # 7200-min OS turnaround is 7200 whether the order is 8 pieces or 800.
    procs = [_P(1, "CNC OS", 7200, allot="OS")]
    for q in (8, 800):
        sched = rule6_allocate.run([_batch(q)], config=_cfg(), masters=_masters(procs))
        os_e = [e for e in sched if e.process_seq == 1][0]
        assert os_e.occupancy_min == 7200          # flat, no ×qty, no setup
        assert os_e.machine == "OS / Outsourced"
        assert os_e.operator == ""


def test_os_block_is_continuous_across_a_thursday():
    # 1440-min (1 day) OS starting Wed 08:00 ends Thu 08:00 exactly — it does NOT
    # skip Anvitech's Thursday off (the vendor runs 24x7).
    procs = [_P(1, "CNC OS", 1440, allot="OS")]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    os_e = [e for e in sched if e.process_seq == 1][0]
    assert os_e.start == datetime(2025, 3, 5, 8, 0)
    assert os_e.end == os_e.start + timedelta(minutes=1440)   # == Thu 2025-03-06 08:00


def test_successor_waits_for_full_os_block():
    procs = [_P(1, "CNC OS", 600, allot="OS"), _P(2, "OP", 1, sug="M")]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    os_e = [e for e in sched if e.process_seq == 1][0]
    op_e = [e for e in sched if e.process_seq == 2][0]
    assert op_e.start >= os_e.end            # next process starts only after OS returns


def test_two_orders_can_be_at_os_in_parallel():
    # Unlimited OS capacity: two batches' OS blocks overlap in wall-clock (OS is not
    # a constraining resource).
    procs = [_P(1, "CNC OS", 600, allot="OS")]
    m = _masters(procs)
    b1, b2 = _batch(item="X"), _batch(item="X")
    b2.batch_id = "B2"
    sched = rule6_allocate.run([b1, b2], config=_cfg(), masters=m)
    os_entries = [e for e in sched if e.process_seq == 1]
    assert len(os_entries) == 2
    assert os_entries[0].start == os_entries[1].start      # both start together


def test_blank_cycle_os_is_zero_duration_milestone():
    procs = [_P(1, "PAINTING OS", None, allot="OS"), _P(2, "OP", 1, sug="M")]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    os_e = [e for e in sched if e.process_seq == 1][0]
    assert os_e.occupancy_min == 0 and os_e.start == os_e.end
    assert os_e.machine == "OS / Outsourced"


def test_os_lane_excluded_from_machine_view():
    procs = [_P(1, "OP", 1, sug="M"), _P(2, "CNC OS", 600, allot="OS")]
    m = _masters(procs)
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=m)
    timeline, summary = rule6_allocate.build_machine_view(sched, m, _cfg())
    lanes = {r["Machine"] for r in summary}
    assert "OS / Outsourced" not in lanes      # not a machine — kept off utilization
    assert "M" in lanes                          # real machines still reported


# --------------------------------------------------------------------------- #
# An OS step waits for its in-house predecessor to FULLY complete (no overlap
# into an outsourced step) — you can't ship parts that aren't machined yet.
# --------------------------------------------------------------------------- #
def test_os_predecessor_fully_completes_before_os_starts_overlap_on():
    procs = [_P(1, "CNC FIRST SIDE", 6, sug="M"), _P(2, "OUTSOURCE", 600, allot="OS")]
    cfg = _cfg(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    sched = rule6_allocate.run([_batch(20)], config=cfg, masters=_masters(procs))
    p1 = [e for e in sched if e.process_seq == 1][0]
    os_e = [e for e in sched if e.process_seq == 2][0]
    assert os_e.start == p1.end          # full completion, NOT the 50% overlap point


def test_inhouse_successor_still_overlaps_overlap_on():
    # Contrast: when the next step is in-house, overlap still applies (starts early).
    procs = [_P(1, "CNC FIRST SIDE", 6, sug="M"), _P(2, "CNC SECOND SIDE", 6, sug="M2")]
    cfg = _cfg(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    sched = rule6_allocate.run([_batch(20)], config=cfg,
                               masters=_masters(procs, machines=("M", "M2")))
    p1 = [e for e in sched if e.process_seq == 1][0]
    p2 = [e for e in sched if e.process_seq == 2][0]
    assert p2.start < p1.end             # in-house successor overlaps (unchanged)


def test_os_predecessor_full_completion_overlap_off_unchanged():
    procs = [_P(1, "CNC FIRST SIDE", 6, sug="M"), _P(2, "OUTSOURCE", 600, allot="OS")]
    cfg = _cfg(overlap_mode=OVERLAP_SEQUENTIAL)
    sched = rule6_allocate.run([_batch(20)], config=cfg, masters=_masters(procs))
    p1 = [e for e in sched if e.process_seq == 1][0]
    os_e = [e for e in sched if e.process_seq == 2][0]
    assert os_e.start == p1.end          # already full under sequential; stays full
