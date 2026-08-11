"""Task 13: operator absences block the person in every plan — pass-1, the
open pass, the joint replay, and the contest (local + payload round-trip)."""
import io
import json
from datetime import date, datetime, timedelta

from engine import optimize_service as svc
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import Order, PlanRun
from engine.pipeline import run_forward
from tests.sample_workbook import build_sample_bytes


def _book(overlap=80):
    raw = build_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    orders = {}
    for so in so_lines:
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=overlap,
                 plan_start_date=date(2025, 3, 1), apply_operator_logic=True)
    cfg.validate()
    return orders, cfg, masters


def test_absent_operator_gets_no_work_in_window():
    orders, cfg, masters = _book()
    from engine import orderbook
    so_lines = orderbook.active_so_lines(orders, [], masters)

    baseline = PlanRun(so_lines=list(so_lines))
    run_forward(baseline, cfg, masters)
    op = next(e.operator for e in baseline.schedule if getattr(e, "operator", ""))

    starts = [e.start for e in baseline.schedule]
    ends = [e.end for e in baseline.schedule]
    absence = [{"operator": op, "from_date": min(starts).date().isoformat(),
                "to_date": max(ends).date().isoformat()}]

    # Match absence_reservations' own window math exactly: 00:00 of from_date
    # to 00:00 of the day AFTER to_date.
    win_start = datetime.combine(min(starts).date(), datetime.min.time())
    win_end = datetime.combine(max(ends).date() + timedelta(days=1), datetime.min.time())

    setup = svc.prepare_contest(orders, [], masters, cfg, absences=absence)
    pr = PlanRun(so_lines=list(setup.target))
    run_forward(pr, setup.config, masters, reserved=setup.absence_reserved)

    assert any(e.operator == op for e in baseline.schedule if getattr(e, "operator", ""))
    # No entry that OVERLAPS the absence window may name the absent operator —
    # Rule 6's reserved-interval mechanism (same as committed-pass reservations)
    # pushes their work past the window rather than erasing it from the plan.
    assert not any(e.operator == op and e.start < win_end and e.end > win_start
                  for e in pr.schedule if getattr(e, "operator", ""))


def test_payload_round_trips_absences():
    orders, cfg, masters = _book()
    absences = [{"operator": "Ravi", "from_date": "2025-03-05",
                 "to_date": "2025-03-07"}]
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42,
                                absences=absences)
    payload = json.loads(json.dumps(payload))
    result = svc.parse_payload(payload)
    assert len(result) == 7
    _, _, _, _, absences2, _, _ = result
    assert absences2 == absences


def test_absence_reservations_shapes():
    rows = [
        {"operator": "A", "from_date": "2025-03-05", "to_date": "2025-03-07"},
        {"operator": "B", "from_date": "2025-03-10", "to_date": "2025-03-08"},  # swapped
        {"operator": "", "from_date": "2025-03-01", "to_date": "2025-03-02"},   # empty op
        {"operator": "C", "from_date": "not-a-date", "to_date": "2025-03-02"},  # malformed
        {"operator": "D"},                                                     # missing keys
    ]
    res = svc.absence_reservations(rows)
    assert set(res.keys()) == {"A", "B"}

    a0, a1 = res["A"][0]
    assert a0 == datetime(2025, 3, 5)
    assert a1 == datetime(2025, 3, 8)     # day AFTER to_date

    b0, b1 = res["B"][0]
    assert b0 == datetime(2025, 3, 8)     # swapped: from <= to
    assert b1 == datetime(2025, 3, 11)

    assert svc.absence_reservations([]) == {}
    assert svc.absence_reservations(None) == {}


def test_prepare_contest_reserves_only_absences():
    """Post-pivot (2026-07-15): the promise rule is gone — lanes have no
    scheduling effect, so ``reserved`` is EXACTLY the operator absences
    (== ``absence_reserved``). A committed lane no longer adds a pass-1 wall."""
    orders, cfg, masters = _book()
    committed_order = next(iter(orders.values()))
    committed_order.commitment = "committed"          # lane is now just a label
    committed_order.promised_date = committed_order.delivery_date

    absence = [{"operator": "Nobody-Else", "from_date": "2025-03-05",
                "to_date": "2025-03-06"}]

    setup = svc.prepare_contest(orders, [], masters, cfg, absences=absence)

    # Only physical unavailability reserves time — a committed lane adds no wall.
    assert "Nobody-Else" in setup.absence_reserved
    interval = setup.absence_reserved["Nobody-Else"][0]
    assert interval == (datetime(2025, 3, 5), datetime(2025, 3, 7))
    assert set(setup.absence_reserved) == {"Nobody-Else"}   # nothing else reserves time
