"""Rule 7 — capture daily entry (full form) via the durable store + aggregation."""
from datetime import date

from engine.models import Actual
from engine.rules import rule7_capture_actuals


def _entry(**kw):
    base = dict(so_no="24-25SO214", item_code="61240807-01", entry_date=date(2025, 8, 1))
    base.update(kw)
    return Actual(**base)


def test_full_form_round_trip():
    a = _entry(
        shift="1st shift", item_name="RING NX910", process="CNC FIRST SIDE",
        qty_produced=82, qty_rejected=3, actual_setup_min=120,
        no_power_min=80, no_operator_min=30, tool_problem_min=15, remarks="late start",
    )
    rule7_capture_actuals.run(a)

    r = rule7_capture_actuals.load_actuals()[0]
    assert r.shift == "1st shift" and r.process == "CNC FIRST SIDE"
    assert r.qty_produced == 82 and r.qty_rejected == 3 and r.good_qty() == 79
    assert r.total_downtime_min() == 125               # 80 + 30 + 15
    assert r.remarks == "late start"


def test_appends():
    rule7_capture_actuals.run(_entry(qty_produced=5))
    rule7_capture_actuals.run(_entry(qty_produced=3, entry_date=date(2025, 8, 2)))
    assert len(rule7_capture_actuals.load_actuals()) == 2


def test_aggregate_by_item_sums_downtime_per_item():
    actuals = [
        _entry(qty_produced=40, qty_rejected=1, no_operator_min=30, tool_problem_min=10),
        _entry(qty_produced=42, qty_rejected=2, no_operator_min=20, no_power_min=80,
               entry_date=date(2025, 8, 2)),
    ]
    rows = rule7_capture_actuals.aggregate_by_item(actuals)
    assert len(rows) == 1
    row = rows[0]
    assert row["Qty Produced"] == 82 and row["Good Qty"] == 79
    assert row["No Operator"] == 50                    # 30 + 20
    assert row["Total Downtime (min)"] == 140          # (30+10) + (20+80)


def test_operator_round_trips_through_json():
    a = _entry(operator="Operator One")
    d = a.to_json()
    assert d["operator"] == "Operator One"
    back = Actual.from_json(d)
    assert back.operator == "Operator One"


def test_operator_defaults_to_empty_string():
    a = _entry()
    assert a.operator == ""


def test_legacy_from_json_without_operator_key_loads_as_empty_string():
    a = _entry(operator="Operator One")
    d = a.to_json()
    del d["operator"]                       # simulate a pre-Task-1 saved record
    back = Actual.from_json(d)
    assert back.operator == ""


def test_operator_appears_in_as_row():
    a = _entry(operator="Operator One")
    assert a.as_row()["Operator"] == "Operator One"
