"""Regression tests for the 2026-07-15 live findings (owner deploy-blocker):

1. `_rebalance_operators` could DOUBLE-BOOK a person: with asymmetric machine
   qualification, the fairness walk reassigns an op to the only person qualified
   for a later machine, then "keeps the original" on that later op even though the
   original is now busy (live: Ankush Jadhav on CNC1 + VMC3 at once).
2. `build_shiftwise_timeline` billed an already-busy person when a shift had more
   concurrent machine-segments than qualified people (`pool = free or eligible`),
   pushing Analytics operators past 100% (live: Mahesh 107%, Saif 106.3%).
   Uncoverable segments must be surfaced as UNSTAFFED, never silently double-billed.
3. `build_analytics` operator utilization must NEVER exceed 100%, and unstaffed
   hours must be visible in the headline.

Every fix is schedule-neutral (owner instruction): no start/end/makespan changes.
"""
from datetime import date, datetime

from engine.config import Config
from engine.models import Batch, Machine, Masters, Operator, ScheduleEntry, WorkCalendar
from engine.rules import rule6_allocate


def _entry(bid, machine, start, end, operator, item="X"):
    return ScheduleEntry(batch_id=bid, item_code=item, process_seq=1, process_name="op",
                         machine=machine, qty=1,
                         occupancy_min=(end - start).total_seconds() / 60,
                         start=start, end=end, notes="", so_refs=[bid], operator=operator)


def _no_person_overlap(entries):
    by_op = {}
    for e in entries:
        if e.operator:
            by_op.setdefault(e.operator, []).append((e.start, e.end))
    for iv in by_op.values():
        iv.sort()
        for a, b in zip(iv, iv[1:]):
            if b[0] < a[1]:
                return False
    return True


# --------------------------------------------------------------------------- #
# 1. Rebalance must never double-book (asymmetric qualification)
# --------------------------------------------------------------------------- #
def _asymmetric_masters():
    M = Machine(machine_no="M", display_name="M", machine_type="CNC lathe",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    V = Machine(machine_no="V", display_name="V", machine_type="Vertical Machining center",
                hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    # 'Alpha' sorts before 'Beta', so the fairness walk prefers Alpha on ties.
    alpha = Operator(name="Alpha", preferred_machines_raw="M,V", machines=["M", "V"],
                     shift="First shift")
    beta = Operator(name="Beta", preferred_machines_raw="M", machines=["M"],
                    shift="First shift")
    return Masters(machines={"M": M, "V": V}, operators=[alpha, beta],
                   calendar=WorkCalendar())


def test_rebalance_never_double_books_with_asymmetric_pools():
    """Original plan: Beta on M (08-12), Alpha on V (09-10) — feasible. The walk
    moves the M op to Alpha (least loaded), then the V op's ONLY qualified person
    is busy. Keeping Alpha there anyway double-books him — the live Ankush bug."""
    masters = _asymmetric_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    h = lambda hr, mi=0: datetime(2025, 3, 5, hr, mi)
    sched = [_entry("A", "M", h(8), h(12), "Beta"),
             _entry("B", "V", h(9), h(10), "Alpha")]
    before = [(e.batch_id, e.machine, e.start, e.end) for e in sched]

    rule6_allocate._rebalance_operators(sched, masters, cfg)

    after = [(e.batch_id, e.machine, e.start, e.end) for e in sched]
    assert before == after, "rebalance must never move any start/end (schedule-neutral)"
    assert _no_person_overlap(sched), "one person cannot run two machines at once"
    # V can only ever be run by Alpha.
    assert next(e for e in sched if e.machine == "V").operator == "Alpha"


# --------------------------------------------------------------------------- #
# 2. Shift-wise billing must never assign a busy person; overflow is UNSTAFFED
# --------------------------------------------------------------------------- #
def _night_crunch_masters():
    """Two two-shift machines whose SECOND shift has only ONE qualified person —
    the live '3 VMCs at night, 2 night people' overload in miniature."""
    mk = lambda no, typ: Machine(machine_no=no, display_name=no, machine_type=typ,
                                 hr_rate=0.0, provisional=False, available_hrs_per_day=19.5)
    day = Operator(name="DayOp", preferred_machines_raw="M,V", machines=["M", "V"],
                   shift="First shift")
    night = Operator(name="NightOp", preferred_machines_raw="M,V", machines=["M", "V"],
                     shift="Second shift")
    m = Masters(machines={"M": mk("M", "CNC lathe"), "V": mk("V", "CNC lathe")},
                operators=[day, night], calendar=WorkCalendar())
    return m


def _night_crunch_schedule():
    d = datetime(2025, 3, 5)
    return [
        _entry("B1", "M", d.replace(hour=19), d.replace(hour=23), "NightOp"),
        _entry("B2", "V", d.replace(hour=19, minute=30), d.replace(hour=22), "NightOp"),
    ], [
        Batch(batch_id="B1", item_code="X", item_name="X", qty=1,
              so_delivery_date=date(2025, 3, 20), source_so_refs=["B1"]),
        Batch(batch_id="B2", item_code="X", item_name="X", qty=1,
              so_delivery_date=date(2025, 3, 20), source_so_refs=["B2"]),
    ]


def test_shiftwise_never_bills_a_busy_person():
    masters = _night_crunch_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched, batches = _night_crunch_schedule()

    rows = rule6_allocate.build_shiftwise_timeline(sched, masters, cfg, batches)

    staffed = [r for r in rows if r["Operator"] and r["Operator"] != rule6_allocate.UNSTAFFED]
    # a person's billed segments never overlap
    by_op = {}
    for r in staffed:
        s = datetime.combine(r["Date"], datetime.strptime(r["Start"], "%d-%m %H:%M").time())
        by_op.setdefault(r["Operator"], []).append((s, s.replace()))  # start ordering is enough
    # NightOp can man only ONE of the two concurrent machines; the other is unstaffed.
    assert sum(1 for r in rows if r["Operator"] == "NightOp") == 1
    assert sum(1 for r in rows if r["Operator"] == rule6_allocate.UNSTAFFED) == 1


def test_rebalance_respects_reserved_operator_intervals():
    """Two-pass integration (2026-07-15 audit): pass 2's fairness rebalance must not
    hand an op to a person the COMMITTED pass already has on another machine at that
    time (live-style find: same person on CNC7 pass-1 and CNC4 pass-2 at once).
    ``reserved`` operator intervals count as busy for the walk AND the repair."""
    masters = _asymmetric_masters()          # Alpha (M,V), Beta (M only)
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    h = lambda hr: datetime(2025, 3, 5, hr, 0)
    # Pass-2 schedule: one op on M, originally Beta's. Alpha has less load, so the
    # blind walk would hand it to Alpha — but pass 1 has Alpha busy 8-12 elsewhere.
    sched = [_entry("A", "M", h(9), h(11), "Beta")]
    reserved = {"Alpha": [(h(8), h(12))]}

    rule6_allocate._rebalance_operators(sched, masters, cfg, reserved=reserved)

    assert sched[0].operator == "Beta", \
        "rebalance must not steal a person who is reserved by the committed pass"


def test_shiftwise_follows_the_schedule_operator_when_possible():
    """Integration (2026-07-15 audit): the shift-wise download must name the SAME
    person the schedule/Gantt shows, whenever that person covers the shift and is
    free — a worker's printed sheet and the Gantt must not disagree. The fair walk
    only takes over where the named person physically can't work (other shift,
    already busy)."""
    masters = _night_crunch_masters()
    # Two first-shift people on machine M; the schedule assigned the SECOND-listed.
    from engine.models import Operator
    masters.operators.append(Operator(name="DayOp2", preferred_machines_raw="M,V",
                                      machines=["M", "V"], shift="First shift"))
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    d = datetime(2025, 3, 5)
    sched = [_entry("B1", "M", d.replace(hour=9), d.replace(hour=15), "DayOp2")]
    batches = [Batch(batch_id="B1", item_code="X", item_name="X", qty=1,
                     so_delivery_date=date(2025, 3, 20), source_so_refs=["B1"])]

    rows = rule6_allocate.build_shiftwise_timeline(sched, masters, cfg, batches)

    assert len(rows) == 1
    assert rows[0]["Operator"] == "DayOp2"   # follows the plan, not the walk's favourite


def test_analytics_operator_utilization_never_exceeds_100_even_when_overloaded():
    from engine.analytics import build_analytics
    masters = _night_crunch_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched, batches = _night_crunch_schedule()

    an = build_analytics(sched, masters, cfg, batches)

    for r in an["operators"]:
        assert r["Utilization %"] is None or r["Utilization %"] <= 100.0, r
        assert r["Operator"] != rule6_allocate.UNSTAFFED   # sentinel is not a person
    # the uncoverable 2.5 h are SURFACED, not hidden or double-billed
    assert an["headline"]["unstaffed_hrs"] == 2.5
