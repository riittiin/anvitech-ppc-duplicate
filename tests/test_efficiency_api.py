"""GET /efficiency + GET /efficiency.csv: admin-only monthly operator efficiency
report (Task 3 of docs/superpowers/specs/2026-07-18-operator-efficiency-report-design.md).

Role gating, year/month validation (-> 400), JSON shape (dict keys mirror
engine.efficiency.monthly_report's column contract), and the CSV download's
exact filename + header row + "-" rendering for None cells.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Actual, Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
    ])


def _admin_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _user_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech_user",
                           "password": "anvitech12345678"})
    return c


def _punch(so_no="SO1", item_code=ITEM_A, entry_date=date(2025, 8, 4),
          operator="Operator One", process="BANDSAW", shift="First shift",
          qty_produced=10.0, qty_rejected=0.0):
    book_store.append_actual(Actual(
        so_no=so_no, item_code=item_code, entry_date=entry_date,
        operator=operator, process=process, shift=shift,
        qty_produced=qty_produced, qty_rejected=qty_rejected,
    ))


# --- role gating ---------------------------------------------------------- #
def test_user_gets_403_on_both_endpoints():
    m = _api(); _seed_book(); _punch()
    user = _user_client(m)

    r = user.get("/efficiency", params={"year": 2025, "month": 8})
    assert r.status_code == 403

    r = user.get("/efficiency.csv", params={"year": 2025, "month": 8})
    assert r.status_code == 403


# --- admin JSON happy path ------------------------------------------------- #
def test_admin_json_happy_path():
    m = _api(); _seed_book()
    _punch(qty_produced=10.0)
    _punch(operator="Operator Two", process="INSP", shift="Second shift",
          qty_produced=3.0)
    admin = _admin_client(m)

    r = admin.get("/efficiency", params={"year": 2025, "month": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["year"] == 2025
    assert body["month"] == 8
    rows = body["rows"]
    names = {row["Operator"] for row in rows}
    assert {"Operator One", "Operator Two"} <= names
    one = next(row for row in rows if row["Operator"] == "Operator One")
    assert one["Good qty"] == 10.0
    assert one["Earned (min)"] == 30.0     # BANDSAW cycle 3 * qty 10


# --- validation ------------------------------------------------------------ #
@pytest.mark.parametrize("year,month", [
    (2025, 0), (2025, 13), (1999, 8), (2101, 8),
])
def test_bad_year_or_month_is_400_json(year, month):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.get("/efficiency", params={"year": year, "month": month})
    assert r.status_code == 400


def test_non_integer_year_month_is_400_json():
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.get("/efficiency", params={"year": "abc", "month": "8"})
    assert r.status_code in (400, 422)


@pytest.mark.parametrize("year,month", [
    (2025, 0), (2025, 13), (1999, 8), (2101, 8),
])
def test_bad_year_or_month_is_400_csv(year, month):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.get("/efficiency.csv", params={"year": year, "month": month})
    assert r.status_code == 400


# --- CSV shape -------------------------------------------------------------- #
def test_csv_filename_header_and_dash_for_none():
    m = _api(); _seed_book()
    # A no-standard punch (process not on ITEM_A's routing) -> Efficiency/Pace
    # stay None for this operator -> rendered as "-" in the CSV.
    _punch(operator="Operator Three", process="NOT-A-REAL-PROCESS",
          qty_produced=5.0)
    admin = _admin_client(m)

    r = admin.get("/efficiency.csv", params={"year": 2025, "month": 8})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    disp = r.headers["content-disposition"]
    assert 'filename="operator-efficiency-2025-08.csv"' in disp

    text = r.content.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln]
    header = lines[0].split(",")
    assert header == [
        "Operator", "Days worked", "Days absent", "Attended (min)",
        "Earned (min)", "Efficiency %", "Pace vs standard (x)", "Good qty",
        "Rejected qty", "Reject %", "Downtime (min)", "Setup (min)",
        "Jobs handled", "No-standard punches",
    ]
    body = "\n".join(lines[1:])
    assert "Operator Three" in body
    row = next(ln for ln in lines[1:] if ln.startswith("Operator Three"))
    cells = row.split(",")
    eff_idx = header.index("Efficiency %")
    pace_idx = header.index("Pace vs standard (x)")
    assert cells[eff_idx] == "-"
    assert cells[pace_idx] == "-"


def test_zero_padded_month_in_filename():
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.get("/efficiency.csv", params={"year": 2025, "month": 3})
    assert r.status_code == 200
    assert 'filename="operator-efficiency-2025-03.csv"' in r.headers["content-disposition"]


def test_csv_neutralizes_formula_injection_in_operator_name():
    # An operator name starting with '=' (or +/-/@) would be executed as a
    # formula by a spreadsheet app opening the CSV — the server must prefix
    # a leading single quote so it renders as inert text.
    m = _api(); _seed_book()
    _punch(operator="=cmd|' /C calc'!A0", qty_produced=10.0)
    admin = _admin_client(m)

    r = admin.get("/efficiency.csv", params={"year": 2025, "month": 8})
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln]
    row = next(ln for ln in lines[1:] if "cmd|" in ln)
    assert row.startswith("\"'=cmd|") or row.startswith("'=cmd|")
