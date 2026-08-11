"""Capture-actuals rollback — delete one mis-punched entry, return that order to
normal, including un-archiving an order that the entry had marked complete."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api import auth  # noqa: E402
from tests.sample_workbook import build_sample_bytes, SO1, ITEM_A  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()
_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"username": _ADMIN, "password": _ADMIN_PWD})
    c.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})
    return c


# ITEM_A routing: BANDSAW -> CNC OS -> INSP (the finished-goods gate). The feedback
# precedence guard (2026-07-25) requires upstream steps recorded before downstream, so
# tests that hit the gate first record the upstream chain; tests that only need *a*
# punch (mark-complete / latest-day / record-drop are process-agnostic) use the first
# step, which is always allowed.
def _punch(client, process, **kw):
    body = {"so_no": SO1, "item_code": ITEM_A, "entry_date": "2025-03-10",
            "process": process, "operator": "Operator One"}
    body.update(kw)
    r = client.post("/actuals", json=body)
    assert r.status_code == 200, r.text
    return r.json()["actuals_ids"][-1]   # the just-appended entry's id


def _post_gate(client, qty_produced, **kw):
    """Record the upstream chain then the finished gate (INSP) carrying kw. Returns the
    INSP entry id."""
    up = max(qty_produced, 1)
    _punch(client, "BANDSAW", qty_produced=up)
    _punch(client, "CNC OS", qty_produced=up)
    return _punch(client, "INSP", qty_produced=qty_produced, **kw)


def _status(client, so):
    o = client.get("/orders").json()["orders"]
    ci = {c: i for i, c in enumerate(o["columns"])}
    row = next((r for r in o["rows"] if r[ci["SO No"]] == so), None)
    return None if row is None else row[ci["Status"]]


def _remaining(client, so):
    o = client.get("/orders").json()["orders"]
    ci = {c: i for i, c in enumerate(o["columns"])}
    row = next(r for r in o["rows"] if r[ci["SO No"]] == so)
    return row[ci["Remaining"]]


def test_rollback_normal_entry_restores_order(client):
    b = _punch(client, "BANDSAW", qty_produced=3)     # SO-001 ordered 5
    c = _punch(client, "CNC OS", qty_produced=3)
    i = _punch(client, "INSP", qty_produced=3)        # the finished gate → remaining drops
    assert _status(client, SO1) == "Running"
    assert _remaining(client, SO1) == 2
    # Roll back in REVERSE (guard: can't remove upstream while downstream depends on it).
    for eid in (i, c, b):
        assert client.post("/actuals/rollback", json={"id": eid}).status_code == 200
    assert _status(client, SO1) == "Pending"          # no actuals left
    assert _remaining(client, SO1) == 5


def test_rollback_mark_complete_unarchives_order(client):
    # mark-complete is order-level, so the first step suffices (single entry).
    eid = _punch(client, "BANDSAW", qty_produced=5, mark_complete=True)
    assert _status(client, SO1) == "Complete"
    r = client.post("/actuals/rollback", json={"id": eid}).json()
    assert r["uncompleted_order"] is True
    assert _status(client, SO1) == "Pending"          # back to active, recomputed


def test_rollback_one_of_many_keeps_the_rest(client):
    _punch(client, "BANDSAW", qty_produced=3)
    _punch(client, "CNC OS", qty_produced=3)
    e1 = _punch(client, "INSP", qty_produced=2)
    _punch(client, "INSP", qty_produced=1)
    client.post("/actuals/rollback", json={"id": e1})
    assert _remaining(client, SO1) == 4               # only the qty-2 INSP entry removed
    assert _status(client, SO1) == "Running"


def test_rollback_keeps_complete_if_another_entry_still_marks_it(client):
    e1 = _punch(client, "BANDSAW", qty_produced=5, mark_complete=True)   # completes + archives
    _punch(client, "BANDSAW", qty_produced=0, mark_complete=True)        # 2nd complete flag remains
    r = client.post("/actuals/rollback", json={"id": e1}).json()
    assert r["uncompleted_order"] is False
    assert _status(client, SO1) == "Complete"               # stays complete


def test_rollback_unknown_id_is_404(client):
    assert client.post("/actuals/rollback", json={"id": "nope"}).status_code == 404


def test_negative_quantity_is_rejected(client):
    r = client.post("/actuals", json={"so_no": SO1, "item_code": ITEM_A,
                                      "entry_date": "2025-03-10", "qty_produced": -5})
    assert r.status_code == 422                       # pydantic ge=0 guard


def test_malformed_entry_date_is_400(client):
    r = client.post("/actuals", json={"so_no": SO1, "item_code": ITEM_A,
                                      "entry_date": "03/10/2025", "qty_produced": 1})
    assert r.status_code == 400


def test_missing_operator_is_400(client):
    r = client.post("/actuals", json={"so_no": SO1, "item_code": ITEM_A,
                                      "entry_date": "2025-03-10", "qty_produced": 1})
    assert r.status_code == 400
    assert "operator" in r.json()["detail"].lower()


def test_blank_operator_is_400(client):
    r = client.post("/actuals", json={"so_no": SO1, "item_code": ITEM_A,
                                      "entry_date": "2025-03-10", "qty_produced": 1,
                                      "operator": "   "})
    assert r.status_code == 400


def test_unknown_operator_is_400(client):
    r = client.post("/actuals", json={"so_no": SO1, "item_code": ITEM_A,
                                      "entry_date": "2025-03-10", "qty_produced": 1,
                                      "operator": "Nobody Real"})
    assert r.status_code == 400
    assert "operator" in r.json()["detail"].lower()


def test_only_latest_date_entries_are_shown_and_rollback_able(client):
    old = {"so_no": SO1, "item_code": ITEM_A, "process": "BANDSAW", "operator": "Operator One",
           "entry_date": "2025-03-01", "qty_produced": 1}
    e_old = client.post("/actuals", json=old).json()["actuals_ids"][-1]
    new = {"so_no": SO1, "item_code": ITEM_A, "process": "BANDSAW", "operator": "Operator One",
           "entry_date": "2025-03-02", "qty_produced": 1}
    resp = client.post("/actuals", json=new).json()
    e_new = resp["actuals_ids"][-1]

    # The Saved-entries list shows ONLY the latest day (2 March) — one row.
    assert resp["actuals_ids"] == [e_new]
    assert len(resp["actuals"]["rows"]) == 1

    # The older day (1 March) is locked — rollback refused.
    assert client.post("/actuals/rollback", json={"id": e_old}).status_code == 400
    # The latest day (2 March) can still be rolled back.
    assert client.post("/actuals/rollback", json={"id": e_new}).status_code == 200


def test_rolled_back_entry_drops_out_of_the_record(client):
    # A rolled-back entry disappears from the actuals record (and so from any plan).
    eid = _punch(client, "BANDSAW", qty_produced=1, machine_breakdown_min=600)
    on = client.post("/run", json={}).json()
    assert any(a != "" for row in on["trace"]["rule7"]["output"]["rows"] for a in row)
    client.post("/actuals/rollback", json={"id": eid})
    after = client.post("/run", json={}).json()
    assert after["trace"]["rule7"]["output"]["rows"] == []   # no actuals left
