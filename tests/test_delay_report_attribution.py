"""The delay report must name the REAL cause of every wait (live 2026-08-09).

The owner audited the live exports and found operator Narayan Fatak and CNC1 both
idle in a window the report blamed on "waiting for a free qualified operator".
Three defects, each proven in the source before this was written:

  1. `_classify_free(a, b, clock)` takes NO operator data at all — it prints
     "waiting for a free qualified operator" for ANY machine-free interval inside
     working hours. It is a fallback bucket, not a finding.
  2. `_order_ops` drops `machine in _OFF_LANES`, so outsourcing never appears. A
     96-hour OS block becomes a GAP and is billed to the next in-house machine.
     Measured on the owner's export: 0 of 1,648 detail rows ever named an OS step.
  3. `plan_start = datetime.combine(config.plan_start_date, midnight)` while the
     plan really begins at the plan-start floor, charging every order the hours
     before the plan existed — 607 h across 57 orders on the live export.

Measured consequence: of 3,142.6 h reported as "waiting for an operator",
1,331.1 h (55 days, 308 windows, all 57 orders) had a qualified operator sitting
free. Directors were told 130.9 days of crew loss.
"""
from datetime import date, datetime

from engine.config import Config
from engine.delay_report import build_delay_report
from engine.models import (Batch, Machine, Masters, Operator, Routing, ScheduleEntry,
                           SOLine, WorkCalendar)

PS = date(2025, 3, 3)


def _cfg():
    return Config(plan_start_date=PS)


def _line(so="SO1", item="X", qty=100, due=date(2025, 3, 20)):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=qty, delivery_date=due)


def _entry(so, item, seq, machine, s, e, op="", proc="CNC"):
    return ScheduleEntry(batch_id="B_" + so, item_code=item, process_seq=seq,
                         process_name=proc, machine=machine, qty=100,
                         occupancy_min=(e - s).total_seconds() / 60, start=s, end=e,
                         so_refs=[so], operator=op,
                         op_segments=([(s, e, op)] if op else []))


def _masters(operators=()):
    ms = {mid: Machine(machine_no=mid, display_name=mid, machine_type="CNC lathe",
                       available_hrs_per_day=19.5) for mid in ("M", "N")}
    m = Masters(machines=ms, calendar=WorkCalendar(), operators=list(operators))
    for it in ("X",):
        m.routings[it] = Routing(item_code=it, description=it, customer="",
                                 rm_type="", moq=None, processes=[])
    return m


def _batch(so="SO1", item="X"):
    return Batch(batch_id="B_" + so, item_code=item, item_name=item, qty=1,
                 so_delivery_date=date(2025, 3, 10), source_so_refs=[so])


def _states(rep):
    return [r["State"] for r in rep["detail"]]


def _hours_of(rep, state_substr):
    return sum(r["Hours"] for r in rep["detail"] if state_substr in r["State"])


# --------------------------------------------------------------------------- #
# 1. Outsourcing must be visible, and must not be billed to the crew
# --------------------------------------------------------------------------- #
def test_outsourcing_is_reported_as_outsourcing_not_as_a_crew_shortage():
    """A 48-hour OS block is the order's real constraint. It used to vanish and be
    charged to the next machine's operators."""
    os_entry = _entry("SO1", "X", 1, "OS / Outsourced",
                      datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 5, 8, 0),
                      proc="BANDSAW OS")
    machining = _entry("SO1", "X", 2, "M",
                       datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 5, 12, 0),
                       op="Alpha")
    masters = _masters([Operator("Alpha", "M", ["M"], "First shift")])
    rep = build_delay_report([os_entry, machining], [_line()], [_batch()],
                             _cfg(), masters)

    assert any("OUTSOURCED" in s for s in _states(rep)), _states(rep)
    assert _hours_of(rep, "OUTSOURCED") == 48
    # Nothing in that window may be blamed on the crew.
    for r in rep["detail"]:
        if r["From"] < datetime(2025, 3, 5, 8, 0) and "crew" in r["State"]:
            raise AssertionError(f"outsourcing billed to the crew: {r}")


def test_the_summary_reports_outsourcing_as_its_own_cause():
    os_entry = _entry("SO1", "X", 1, "OS / Outsourced",
                      datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 5, 8, 0),
                      proc="BANDSAW OS")
    machining = _entry("SO1", "X", 2, "M",
                       datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 5, 12, 0), op="Alpha")
    masters = _masters([Operator("Alpha", "M", ["M"], "First shift")])
    rep = build_delay_report([os_entry, machining], [_line(due=date(2025, 3, 4))],
                             [_batch()], _cfg(), masters)
    s = rep["summary"][0]
    assert "Outsourced (days)" in s
    assert s["Outsourced (days)"] == 2.0
    assert "outsourc" in s["Why"].lower()


# --------------------------------------------------------------------------- #
# 2. The clock starts when the plan starts
# --------------------------------------------------------------------------- #
def test_the_report_starts_when_the_plan_starts_not_at_midnight():
    """The plan began at 14:00; the hours before that are not a delay anybody caused."""
    e = _entry("SO1", "X", 1, "M", datetime(2025, 3, 3, 14, 0),
               datetime(2025, 3, 3, 18, 0), op="Alpha")
    masters = _masters([Operator("Alpha", "M", ["M"], "First shift")])
    rep = build_delay_report([e], [_line()], [_batch()], _cfg(), masters)
    earliest = min(r["From"] for r in rep["detail"])
    assert earliest == datetime(2025, 3, 3, 14, 0), rep["detail"][:3]


# --------------------------------------------------------------------------- #
# 3. "Waiting for an operator" must mean an operator was actually missing
# --------------------------------------------------------------------------- #
def _gap_plan():
    """SO1 runs on M, pauses 10:00-14:00 while M is free, then runs again."""
    return [_entry("SO1", "X", 1, "M", datetime(2025, 3, 3, 8, 0),
                   datetime(2025, 3, 3, 10, 0), op="Alpha"),
            _entry("SO1", "X", 2, "M", datetime(2025, 3, 3, 14, 0),
                   datetime(2025, 3, 3, 18, 0), op="Alpha")]


def test_a_free_qualified_operator_is_never_reported_as_a_crew_shortage():
    plan = _gap_plan()
    masters = _masters([Operator("Alpha", "M", ["M"], "First shift")])
    rep = build_delay_report(plan, [_line()], [_batch()], _cfg(), masters)
    gap = [r for r in rep["detail"]
           if r["From"] >= datetime(2025, 3, 3, 10, 0)
           and r["To"] <= datetime(2025, 3, 3, 14, 0)]
    assert gap, "the 10:00-14:00 gap must be explained"
    assert not any("crew" in r["State"] for r in gap), (
        "Alpha was free the whole time; this is spare capacity, not a crew shortage: "
        + str(gap))
    assert any("IDLE" in r["State"] for r in gap), gap


def test_crew_is_still_blamed_when_every_qualified_operator_really_is_busy():
    """The honest case must survive: Alpha is the only person who can run M, and
    she is on machine N for the whole gap."""
    plan = _gap_plan() + [
        _entry("SO2", "X", 1, "N", datetime(2025, 3, 3, 10, 0),
               datetime(2025, 3, 3, 14, 0), op="Alpha")]
    masters = _masters([Operator("Alpha", "M/N", ["M", "N"], "First shift")])
    rep = build_delay_report(plan, [_line()], [_batch()], _cfg(), masters)
    gap = [r for r in rep["detail"]
           if r["From"] >= datetime(2025, 3, 3, 10, 0)
           and r["To"] <= datetime(2025, 3, 3, 14, 0)
           and r["Machine"] != "N"]
    assert any("crew" in r["State"] for r in gap), gap


def test_a_second_shift_operator_does_not_excuse_a_first_shift_gap():
    """Shift matters: a night-shift person being free says nothing about a day gap."""
    plan = _gap_plan()
    masters = _masters([Operator("Alpha", "M", ["M"], "First shift"),
                        Operator("Night", "M", ["M"], "Second shift")])
    # Alpha (the only first-shift person) is busy on N for the whole gap.
    plan = plan + [_entry("SO2", "X", 1, "N", datetime(2025, 3, 3, 10, 0),
                          datetime(2025, 3, 3, 14, 0), op="Alpha")]
    masters.operators[0].machines = ["M", "N"]
    rep = build_delay_report(plan, [_line()], [_batch()], _cfg(), masters)
    gap = [r for r in rep["detail"]
           if r["From"] >= datetime(2025, 3, 3, 10, 0)
           and r["To"] <= datetime(2025, 3, 3, 14, 0) and r["Machine"] != "N"]
    assert any("crew" in r["State"] for r in gap), gap


# --------------------------------------------------------------------------- #
# 4. The accounting invariant must survive the new states
# --------------------------------------------------------------------------- #
def test_every_hour_is_still_accounted_for():
    os_entry = _entry("SO1", "X", 1, "OS / Outsourced",
                      datetime(2025, 3, 3, 8, 0), datetime(2025, 3, 4, 8, 0),
                      proc="BANDSAW OS")
    plan = [os_entry,
            _entry("SO1", "X", 2, "M", datetime(2025, 3, 4, 14, 0),
                   datetime(2025, 3, 4, 18, 0), op="Alpha")]
    masters = _masters([Operator("Alpha", "M", ["M"], "First shift")])
    rep = build_delay_report(plan, [_line()], [_batch()], _cfg(), masters)
    total = sum(r["Hours"] for r in rep["detail"])
    span = (datetime(2025, 3, 4, 18, 0) - datetime(2025, 3, 3, 8, 0)).total_seconds() / 3600
    assert abs(total - span) < 1e-6, (total, span, rep["detail"])


# --------------------------------------------------------------------------- #
# 5. The invariant, checked over a whole generated plan rather than a crafted pair
# --------------------------------------------------------------------------- #
def test_no_crew_row_anywhere_in_a_real_plan_has_a_free_qualified_operator():
    """The claim "every qualified operator was busy" must hold for EVERY crew row in
    a full plan, verified against the same bookings the scheduler committed. This is
    the check that failed 308 times on the owner's live export."""
    import io
    from engine import book_store, delay_report as dr
    from engine.loaders import load_all
    from engine.operator_coverage import qualified_operators
    from engine.pipeline import run_forward
    from engine.models import PlanRun
    from tests.new_sample_workbook import build_new_sample_bytes

    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = load_all(io.BytesIO(wb))
    cfg = Config(plan_start_date=PS, scheduler="new", apply_operator_logic=True)
    pr = PlanRun(so_lines=list(so_lines))
    run_forward(pr, cfg, masters)
    assert pr.schedule, "the fixture must produce a schedule"

    rep = dr.build_delay_report(pr.schedule, so_lines, pr.batches_prioritized,
                                cfg, masters)
    busy = dr._operator_bookings(pr.schedule)
    offenders = []
    for r in rep["detail"]:
        if r["State"] != "WAITING (crew)" or not r["Machine"]:
            continue
        for name in qualified_operators(r["Machine"], r["From"], masters, cfg):
            if not any(bs < r["To"] and r["From"] < be
                       for bs, be in busy.get(name, [])):
                offenders.append((r["Machine"], r["From"], r["To"], name))
                break
    assert offenders == [], (
        "these windows blame the crew while someone qualified was free: "
        + str(offenders[:5]))
