"""Task 6: `_finalize_optimize` recomputes the winner AT the winning machine-set
(so the Optimize panel's shown numbers match what Apply actually produces), and
`/optimize/status` exposes that winning machine-set.

Fixtures mirror tests/test_freeze_api.py's new-engine harness (force
DEFAULT_SCHEDULER=new + the new-engine sample workbook, shrink the optimize
budgets so a quick search converges fast in-process). Kept local to this
module (not tests/conftest.py) so they don't change behaviour for the rest of
the suite, which validates the classic engine by default.
"""
import importlib
import json
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.config import Config
from engine.models import Order
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A, ITEM_B, SO1, SO2


def _api_with_new_engine(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    return m


def _seed_new_book():
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([
        Order(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
        Order(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21)),
    ])


@pytest.fixture
def new_engine_client_with_book(monkeypatch):
    """A logged-in admin TestClient, wired to the new engine, with the new
    sample workbook's masters + two orders (one per item) merged into the
    order book. Optimize budgets are shrunk so a quick search converges fast
    in a test process (the live default is 1000 evals/click)."""
    m = _api_with_new_engine(monkeypatch)
    _seed_new_book()

    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")   # trigger the one-time operator seed

    m._OPT_BUDGETS = {"quick": 20, "deep": 20}
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)

    yield client


def _wait_done(client, timeout_s=60):
    """Poll GET /optimize/status until the job leaves the 'running' state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get("/optimize/status").json()
        if body["state"] in ("done", "idle"):
            return body
        time.sleep(0.2)
    raise AssertionError("optimize did not finish within the timeout")


def test_finalize_recomputes_winner_at_its_machine_set(new_engine_client_with_book):
    """A local sweep's winning machine-set must be stored and surfaced on
    /optimize/status, so the Optimize panel and Apply agree (shown == applied)."""
    client = new_engine_client_with_book
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_done(client)
    st = client.get("/optimize/status").json()
    assert st["state"] == "done", st
    assert "flexible_machines" in st
    assert "current_flexible" in st
    assert isinstance(st["flexible_machines"], bool)
    assert isinstance(st["current_flexible"], bool)


WORKER_SECRET = "test-worker-secret"


def test_result_endpoint_stores_winner_flexible(monkeypatch):
    """A cloud worker's winning machine-set (`winner_flexible`) must round-trip
    through POST /optimize/result -> _finalize_optimize -> GET /optimize/status,
    exactly like `winner_overlap` already does. Mirrors
    tests/test_optimize_cloud.py::test_cloud_round_trip_via_the_worker_endpoints
    but only drives the worker-facing /optimize/result leg directly (no need to
    actually run a contest) against the new-engine harness."""
    m = _api_with_new_engine(monkeypatch)
    _seed_new_book()
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "fake-token")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", WORKER_SECRET)
    # Oracle claim window (2026-08-01): 0 = dispatch immediately, preserving
    # this test's existing timing (it waits on the dispatch stub, not a claim).
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", "0")
    m._OPT_BUDGETS = {"quick": 20, "deep": 20}

    dispatched = {}
    monkeypatch.setattr(
        m, "_dispatch_workflow",
        lambda cloud, job_id: dispatched.setdefault("job_id", job_id) or True)

    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")   # trigger the one-time operator seed

    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["mode"] == "cloud", st
    deadline = time.monotonic() + 10
    while "job_id" not in dispatched and time.monotonic() < deadline:
        time.sleep(0.02)
    job_id = dispatched.get("job_id")
    assert job_id, "worker was never dispatched"

    r = client.post(
        "/optimize/result",
        headers={"X-Worker-Secret": WORKER_SECRET},
        json={"job_id": job_id, "winner_overlap": 80, "winner_flexible": True,
              "ranks": {}, "best": {"total_late_days": 1, "makespan_days": 1,
                                     "ontime_breach": 0.0},
              "rows": [], "evals": 10, "cancelled": False})
    assert r.status_code == 200, r.text

    body = _wait_done(client)
    assert body.get("flexible_machines") is True


def test_apply_persists_machine_set_and_plan_reproduces(new_engine_client_with_book):
    """Task 7: 'Apply this plan' must persist the winning machine-set
    (Config.flexible_machines) into the saved plan config alongside the
    winning overlap, so `_plan` (the everyday '/run') reproduces the same
    machine choices the applied winner used — a machining op that only
    the Allotted+Suggested UNION could reach (CNC2 here) must land there
    after Apply, not just under a one-off Optimize search.

    Deterministic union-win setup, mirroring
    tests/test_flexible_machines.py::test_run_places_op_on_suggested_machine_only_when_flexible:
    Item A's 'CNC FIRST SIDE' step has Allotted=CNC1 only, Suggested=CNC1/CNC2
    (tests/new_sample_workbook.py). A single Item-A order never contends for
    CNC1, so CNC2 goes unused regardless of the flag; a SECOND Item-A order far
    enough from the first's delivery date to stay a separate Rule-1 batch
    (>10-day consolidation window), plus a second CNC1/CNC2-qualified
    first-shift operator ('Echo', alongside the fixture's 'Alpha'), creates
    real contention two operators can actually exploit in parallel.

    The finished job's result is stubbed directly (state='done'), mirroring
    tests/test_manual_apply_backstop.py's `_stage` pattern, so the win is
    forced deterministically rather than depending on a real search finding
    it within the test's shrunk budget.
    """
    client = new_engine_client_with_book
    import api.main as m
    from engine.models import Order

    # A second Item-A order, 26 days after SO1's 2025-03-20 delivery (well past
    # the default 10-day consolidation window) -> stays a separate Rule-1 batch.
    book_store.add_orders([Order("NSO-003", ITEM_A, ITEM_A, 50, date(2025, 4, 15))])
    # A second CNC1/CNC2-qualified first-shift operator so the extra capacity
    # the Suggested machine (CNC2) unlocks can actually be used in parallel.
    r = client.post("/operators", json={"name": "Echo", "machines_raw": "CNC1, CNC2",
                                        "shift": "First shift"})
    assert r.status_code == 200, r.text

    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {
            "best": {"total_late_days": 1, "makespan_days": 1.0,
                     "max_late_days": 1, "max_committed_slip": 0},
            "ranks": {}, "budget": 20, "seed": 1, "baseline": {},
            "best_overlap": 80, "current_overlap": 70,
            "knob": "overlap_percent", "flexible_machines": True,
        }

    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text

    cfg = Config.from_dict(json.loads(book_store.load_plan_config()))
    assert cfg.flexible_machines is True         # persisted
    assert cfg.overlap_percent == 80             # unchanged behaviour: overlap still persists too

    # _plan now uses the union (Allotted + Suggested) — a machining op lands
    # on a Suggested-only machine (CNC2), the same one the applied winner used.
    resp = client.post("/run", json={})
    assert resp.status_code == 200, resp.text
    table = resp.json()["trace"]["rule6"]["output"]
    m_idx = table["columns"].index("Machine")
    machines = {row[m_idx] for row in table["rows"]}
    assert "CNC2" in machines
