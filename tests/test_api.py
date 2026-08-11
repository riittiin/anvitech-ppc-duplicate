"""API smoke tests — login gate + order-book flow (upload → plan → actuals).

The app is gated by a signed session cookie now (not Basic Auth), so tests log in
as admin via POST /login (the TestClient keeps the cookie). A fresh client is made
per test so the cookie matches each test's isolated store/secret."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api import auth  # noqa: E402
from tests.sample_workbook import build_sample_bytes, SO1, ITEM_A  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()   # generated workbook (Test4 format) — replaces Test2

_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]


@pytest.fixture
def client():
    """A TestClient logged in as admin (cookie persisted on the client)."""
    c = TestClient(app)
    r = c.post("/login", data={"username": _ADMIN, "password": _ADMIN_PWD})
    assert r.status_code == 200  # 303 followed to the app shell at /
    return c


def _upload_test_workbook(client):
    return client.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})


def test_requires_login():
    anon = TestClient(app)
    assert anon.post("/run", json={"config": {}}).status_code == 401
    # Wrong password does not establish a session.
    bad = TestClient(app)
    assert bad.post("/login", data={"username": _ADMIN, "password": "wrong"}).status_code == 401
    assert bad.get("/orders").status_code == 401


def test_empty_book_plans_cleanly(client):
    r = client.post("/run", json={"config": {}})
    assert r.status_code == 200
    trace = r.json()["trace"]
    for rule in ["rule1", "rule2", "rule3", "rule6", "rule7", "rule8"]:
        assert rule in trace
    assert r.json()["orders"]["rows"] == []          # nothing uploaded yet


def test_upload_merges_then_plans(client):
    up = _upload_test_workbook(client)
    assert up.status_code == 200
    body = up.json()
    assert body["added"] == 3                        # 3 distinct SO numbers

    r = client.post("/run", json={"config": {}})
    assert r.status_code == 200
    assert len(r.json()["orders"]["rows"]) == 3
    assert len(r.json()["trace"]["rule1"]["output"]["rows"]) >= 1
    # The Plan tab labels a schedulable order "scheduled" (honesty: the label reflects
    # actual schedule membership, so a held-out order is never mislabelled — see _plan).
    out = r.json()["trace"]["rule8"]["output"]
    plan_i = out["columns"].index("In this plan")
    assert all(row[plan_i] == "scheduled" for row in out["rows"])


def test_reupload_same_file_adds_nothing(client):
    _upload_test_workbook(client)
    again = _upload_test_workbook(client).json()
    assert again["added"] == 0
    assert len(again["flagged"]) >= 3                # every SO# now flagged


def test_reupload_with_a_changed_delivery_date_updates_the_order(client):
    """A director edits SO Delivery Date in Excel and re-imports: the date moves,
    the recorded production and the order's identity do not."""
    import datetime
    import io
    from tests.sample_workbook import build_workbook

    _upload_test_workbook(client)
    before = client.get("/orders").json()["orders"]["rows"]
    assert before, "upload should have seeded the book"

    # Rebuild the same workbook with SO1's delivery date pushed out by 30 days.
    wb = build_workbook()
    ws = wb["Sales Order (SO) list"]
    old = ws.cell(row=2, column=24).value          # 'SO Delivery Date' column
    assert isinstance(old, datetime.date), f"expected a date in that cell, got {old!r}"
    ws.cell(row=2, column=24).value = old + datetime.timedelta(days=30)
    buf = io.BytesIO()
    wb.save(buf)

    r = client.post("/upload", files={"file": ("t2.xlsx", buf.getvalue(), XLSX_MIME)})
    body = r.json()
    assert body["added"] == 0          # no new orders
    assert body["updated"] == 1        # exactly the one changed row
    assert any("delivery date updated" in f["reason"] for f in body["flagged"])

    orders = client.get("/orders").json()["orders"]
    after = orders["rows"]
    assert len(after) == len(before)   # no duplicate order was created

    cols = orders["columns"]
    si, ii, di = cols.index("SO No"), cols.index("Item Code"), cols.index("SO Delivery Date")
    so1_row = next(row for row in after if row[si] == SO1 and row[ii] == ITEM_A)
    assert so1_row[di] == "09-04-2025"  # 2025-03-10 + 30 days, DD-MM-YYYY display


def test_reupload_with_a_changed_delivery_date_preserves_actuals_and_commitment(client, monkeypatch):
    """The design spec's missing round trip: punch some production and commit an
    order BEFORE a re-import that changes a (different) order's delivery date —
    the punched progress, its derived Running status, and the committed order's
    promised_date/commitment must all survive untouched."""
    import datetime
    import io
    from tests.sample_workbook import build_workbook, SO2, SO3, ITEM_B

    _upload_test_workbook(client)

    # Partially punch SO1/ITEM_A (ordered qty 5) — Running, not Complete.
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "operator": "Operator One",
        "entry_date": "2025-03-10", "qty_produced": 2,
    })
    assert r.status_code == 200

    # Commit SO3/ITEM_B — snapshots its current expected completion as a promise.
    import api.main as _m
    monkeypatch.setattr(_m, "COMMITMENT_FEATURE_ENABLED", True)   # lanes are hidden by default
    r = client.post("/orders/commit", json={"orders": [[SO3, ITEM_B]]})
    assert r.status_code == 200

    before = client.get("/orders").json()["orders"]
    cols = before["columns"]
    si, ii = cols.index("SO No"), cols.index("Item Code")
    promised_i, lane_i = cols.index("Promised"), cols.index("Lane")
    so3_before = next(row for row in before["rows"] if row[si] == SO3 and row[ii] == ITEM_B)
    assert so3_before[lane_i] == "committed"
    assert so3_before[promised_i]          # a promise was snapshotted

    # Re-import the same workbook with SO2's (a DIFFERENT order, same item as
    # SO1) delivery date pushed out by 30 days.
    wb = build_workbook()
    ws = wb["Sales Order (SO) list"]
    old = ws.cell(row=3, column=24).value          # SO2's 'SO Delivery Date' cell
    assert isinstance(old, datetime.date), f"expected a date in that cell, got {old!r}"
    new_date = old + datetime.timedelta(days=30)
    ws.cell(row=3, column=24).value = new_date
    buf = io.BytesIO()
    wb.save(buf)

    r = client.post("/upload", files={"file": ("t3.xlsx", buf.getvalue(), XLSX_MIME)})
    body = r.json()
    assert body["added"] == 0
    assert body["updated"] == 1
    assert any("delivery date updated" in f["reason"] for f in body["flagged"])

    after = client.get("/orders").json()["orders"]
    cols = after["columns"]
    si, ii = cols.index("SO No"), cols.index("Item Code")
    dd_i, status_i = cols.index("SO Delivery Date"), cols.index("Status")
    promised_i, lane_i = cols.index("Promised"), cols.index("Lane")

    # (a) SO2's delivery date moved.
    so2 = next(row for row in after["rows"] if row[si] == SO2 and row[ii] == ITEM_A)
    assert so2[dd_i] == new_date.strftime("%d-%m-%Y")

    # (b) SO1's recorded production survives (the punch itself, still on file)
    # and its derived Running status is unaffected by the re-import.
    so1 = next(row for row in after["rows"] if row[si] == SO1 and row[ii] == ITEM_A)
    assert so1[status_i] == "Running"
    from engine import book_store
    so1_actuals = [a for a in book_store.load_actuals()
                   if a.so_no == SO1 and a.item_code == ITEM_A]
    assert len(so1_actuals) == 1
    assert so1_actuals[0].qty_produced == 2

    # (c) SO3 keeps its commitment and promised_date exactly as snapshotted.
    so3_after = next(row for row in after["rows"] if row[si] == SO3 and row[ii] == ITEM_B)
    assert so3_after[lane_i] == "committed"
    assert so3_after[promised_i] == so3_before[promised_i]


def test_reupload_changing_a_punched_committed_orders_own_date_preserves_its_state(client, monkeypatch):
    """The tight version of the round trip: the SAME order is punched, committed,
    AND the one whose own delivery date changes on re-import. This is the case
    that actually exercises the update path (`updated_orders`, built via
    `dataclasses.replace`) on a record carrying both recorded production and a
    commitment/promise — proving that path preserves both, not merely that an
    unrelated record survives an unrelated upload."""
    import datetime
    import io
    from tests.sample_workbook import build_workbook

    _upload_test_workbook(client)

    # Partially punch SO1/ITEM_A (ordered qty 5) — Running, not Complete.
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "operator": "Operator One",
        "entry_date": "2025-03-10", "qty_produced": 2,
    })
    assert r.status_code == 200

    # Commit THAT SAME order — snapshots its current expected completion as a promise.
    import api.main as _m
    monkeypatch.setattr(_m, "COMMITMENT_FEATURE_ENABLED", True)   # lanes are hidden by default
    r = client.post("/orders/commit", json={"orders": [[SO1, ITEM_A]]})
    assert r.status_code == 200

    before = client.get("/orders").json()["orders"]
    cols = before["columns"]
    si, ii = cols.index("SO No"), cols.index("Item Code")
    promised_i, lane_i = cols.index("Promised"), cols.index("Lane")
    so1_before = next(row for row in before["rows"] if row[si] == SO1 and row[ii] == ITEM_A)
    assert so1_before[lane_i] == "committed"
    assert so1_before[promised_i]          # a promise was snapshotted

    # Re-import the same workbook with SO1's OWN delivery date pushed out by
    # 30 days — this is the order that is punched AND committed.
    wb = build_workbook()
    ws = wb["Sales Order (SO) list"]
    old = ws.cell(row=2, column=24).value          # SO1's 'SO Delivery Date' cell
    assert isinstance(old, datetime.date), f"expected a date in that cell, got {old!r}"
    new_date = old + datetime.timedelta(days=30)
    ws.cell(row=2, column=24).value = new_date
    buf = io.BytesIO()
    wb.save(buf)

    r = client.post("/upload", files={"file": ("t4.xlsx", buf.getvalue(), XLSX_MIME)})
    body = r.json()
    assert body["added"] == 0
    assert body["updated"] == 1        # SO1 went through the UPDATE path...
    so1_flags = [f for f in body["flagged"] if f["so_no"] == SO1 and f["item_code"] == ITEM_A]
    assert len(so1_flags) == 1
    assert "delivery date updated" in so1_flags[0]["reason"]
    assert "duplicate" not in so1_flags[0]["reason"]   # ...never the duplicate/no-op path

    after = client.get("/orders").json()["orders"]
    cols = after["columns"]
    si, ii = cols.index("SO No"), cols.index("Item Code")
    dd_i, status_i = cols.index("SO Delivery Date"), cols.index("Status")
    promised_i, lane_i = cols.index("Promised"), cols.index("Lane")
    so1_after = next(row for row in after["rows"] if row[si] == SO1 and row[ii] == ITEM_A)

    # (a) SO1's own delivery date moved.
    assert so1_after[dd_i] == new_date.strftime("%d-%m-%Y")

    # (b) SO1's recorded production and derived Running status survive on the
    # very record whose date just changed.
    assert so1_after[status_i] == "Running"
    from engine import book_store
    so1_actuals = [a for a in book_store.load_actuals()
                   if a.so_no == SO1 and a.item_code == ITEM_A]
    assert len(so1_actuals) == 1
    assert so1_actuals[0].qty_produced == 2

    # (c) SO1 keeps its own commitment and promised_date exactly as snapshotted —
    # the update path must not have rebuilt the Order without them.
    assert so1_after[lane_i] == "committed"
    assert so1_after[promised_i] == so1_before[promised_i]


def test_actual_marks_order_complete(client):
    _upload_test_workbook(client)
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "operator": "Operator One",
        "entry_date": "2025-03-10", "qty_produced": 5, "mark_complete": True,
    })
    assert r.status_code == 200 and r.json()["completed_order"] is True

    rows = client.get("/orders").json()["orders"]["rows"]
    cols = client.get("/orders").json()["orders"]["columns"]
    si, sti = cols.index("SO No"), cols.index("Status")
    so = next(row for row in rows if row[si] == SO1)
    assert so[sti] == "Complete"


def test_actual_rejects_future_entry_date(client):
    # A future-dated punch (e.g. a typo'd year) would advance the whole plan
    # clock decades forward via orderbook.effective_plan_start_date — reject
    # it outright. A past/today date still works.
    _upload_test_workbook(client)
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "operator": "Operator One",
        "entry_date": "2099-01-01", "qty_produced": 5,
    })
    assert r.status_code == 400
    assert "cannot be after today" in r.json()["detail"]

    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "operator": "Operator One",
        "entry_date": "2025-03-10", "qty_produced": 5,
    })
    assert r.status_code == 200


def test_delete_selected_and_clear_all(client):
    _upload_test_workbook(client)
    assert len(client.get("/orders").json()["orders"]["rows"]) == 3

    # Delete one order permanently (admin password required to confirm). Orders are
    # identified by the (SO#, item) pair, not the SO# alone.
    d = client.post("/orders/delete", json={"orders": [[SO1, ITEM_A]], "password": _ADMIN_PWD})
    assert d.status_code == 200 and d.json()["deleted"] == 1
    rows = client.get("/orders").json()["orders"]["rows"]
    assert len(rows) == 2 and not any(SO1 in str(r) for r in rows)

    # Clear everything.
    assert client.post("/orders/clear", json={"password": _ADMIN_PWD}).status_code == 200
    assert client.get("/orders").json()["orders"]["rows"] == []


def test_delete_rejected_without_correct_password(client):
    _upload_test_workbook(client)
    # Wrong / missing password → 403, nothing deleted.
    assert client.post("/orders/delete", json={"orders": [[SO1, ITEM_A]], "password": "wrong"}).status_code == 403
    assert client.post("/orders/delete", json={"orders": [[SO1, ITEM_A]]}).status_code == 403
    assert client.post("/orders/clear", json={"password": "wrong"}).status_code == 403
    assert len(client.get("/orders").json()["orders"]["rows"]) == 3   # all still there


def test_bad_upload_returns_400(client):
    bad = client.post("/upload", files={"file": ("x.xlsx", b"not excel", "application/octet-stream")})
    assert bad.status_code == 400


def test_report_lists_pending_master_data(client):
    _upload_test_workbook(client)
    assert "PENDING_MASTER_DATA" in str(client.get("/report").json())


def _parse_dt(s):
    # "DD-MM-YYYY HH:MM" -> a sortable tuple (chronological, cross-month safe).
    from datetime import datetime
    return datetime.strptime(s, "%d-%m-%Y %H:%M")


def _earliest_start_for(plan, machine):
    out = plan["trace"]["rule6"]["output"]
    ci = {c: i for i, c in enumerate(out["columns"])}
    starts = [_parse_dt(row[ci["Start"]]) for row in out["rows"] if row[ci["Machine"]] == machine]
    return min(starts)


def test_recorded_downtime_does_not_affect_the_schedule(client):
    """Recorded times (downtime, actual setup) are for the record only — the feedback
    loop is quantity-only. Compared against the SAME punch (same date + qty) with no
    downtime, adding a huge breakdown must NOT change the schedule. (Both punches
    advance the plan clock equally — that's a separate, intended effect.)"""
    _upload_test_workbook(client)
    base = client.post("/run", json={"persist": True}).json()
    out = base["trace"]["rule6"]["output"]
    ci = {c: i for i, c in enumerate(out["columns"])}
    first = min(out["rows"], key=lambda r: _parse_dt(r[ci["Start"]]))
    machine, item, process = first[ci["Machine"]], first[ci["Item Code"]], first[ci["Process"]]
    so_to_items = client.get("/items").json()["so_to_items"]
    so = next((s for s, lines in so_to_items.items()
               if any(l["item_code"] == item for l in lines)), "X")

    def punch(**extra):
        body = {"so_no": so, "item_code": item, "entry_date": "2025-03-07",
                "process": process, "qty_produced": 1, "operator": "Operator One"}
        body.update(extra)
        return client.post("/actuals", json=body).json()["actuals_ids"][-1]

    # Same date + qty, NO downtime.
    eid = punch()
    no_downtime = _earliest_start_for(client.post("/run", json={"persist": True}).json(), machine)
    client.post("/actuals/rollback", json={"id": eid})

    # Same date + qty, huge breakdown + setup overrun.
    punch(machine_breakdown_min=600, actual_setup_min=999)
    after = client.post("/run", json={"persist": True}).json()
    with_downtime = _earliest_start_for(after, machine)

    assert with_downtime == no_downtime                 # downtime made zero difference
    titles = [t["title"] for t in after["trace"]["rule6"].get("tables", [])]
    assert not any("Downtime fed back" in t for t in titles)   # feature is gone
