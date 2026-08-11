"""API-level test for Task 17: persisting the applied plan's schedule at apply
time (`book_store.save_last_applied_schedule`, written from `_optimize_apply`).

The freeze feature is NEW-ENGINE-ONLY, so these tests force
``DEFAULT_SCHEDULER=new`` and seed the NEW-engine sample workbook (the classic
sample is deliberately under-staffed for the new engine's stricter operator
requirements — see tests/new_sample_workbook.py).

Fixtures here are local to this module (not tests/conftest.py) so they don't
change behaviour for the rest of the suite, which validates the classic engine
by default.
"""
import importlib
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A, ITEM_B, SO1, SO2


def _api_with_new_engine(monkeypatch):
    """Force the new engine (authoritative env var) and reload api.main so its
    module-level state (in-memory optimize job, masters cache, etc.) starts
    fresh under that env. Uses monkeypatch (not a raw os.environ write) so the
    env var is restored after the test — leaking DEFAULT_SCHEDULER=new into
    later tests in the same session silently switches them to the new engine
    (that leak is exactly what caused the first version of this fixture to
    fail unrelated tests in tests/test_optimize_api.py /
    tests/test_report_and_staleness.py when the whole suite ran)."""
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
def admin_client_with_book(monkeypatch):
    """A logged-in admin TestClient, wired to the new engine, with the new
    sample workbook's masters + two orders (one per item) merged into the
    order book. Optimize budgets are shrunk so a quick/deep search converges
    fast in a test process (the live default is 1000 evals/click)."""
    m = _api_with_new_engine(monkeypatch)
    _seed_new_book()

    # Trigger the one-time operator seed (from the workbook's operator sheet)
    # so the new engine has a qualified crew to schedule against.
    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")

    # Keep the search small + deterministic + LOCAL (no cloud dispatch — the
    # sample book has only 2 orders so this still converges to a real result).
    m._OPT_BUDGETS = {"quick": 20, "deep": 20}
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)

    yield client


def _wait_optimize_done(client, timeout_s=60):
    """Poll GET /optimize/status until the job leaves the 'running' state.
    Fails the test (rather than hanging) if it never finishes in time."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get("/optimize/status").json()
        if body["state"] in ("done", "idle"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"optimize did not finish within {timeout_s}s")


def test_apply_persists_last_applied_schedule(admin_client_with_book):
    client = admin_client_with_book
    # Nothing applied yet.
    assert book_store.load_last_applied_schedule() == []

    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    status = _wait_optimize_done(client)
    assert status["state"] == "done", status

    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text

    rows = book_store.load_last_applied_schedule()
    assert rows, "apply did not persist the applied schedule"
    assert {"batch_id", "item_code", "process_seq", "machine", "operator",
            "start", "end", "so_refs"} <= set(rows[0].keys())
    # Sanity: rows reference the two orders we seeded.
    all_refs = {ref for row in rows for ref in row["so_refs"]}
    assert SO1 in all_refs or SO2 in all_refs


def _punch_partial(client, qty_produced=20, entry_date="2025-03-20"):
    """Punch a partial good quantity (less than the ordered qty) on the first
    in-house machining step of SO1/ITEM_A ("CNC FIRST SIDE", 50 ordered) —
    good=20 leaves remaining_qty=30 > 0, so the step is in-progress and
    should be frozen. Operator "Alpha" is qualified for CNC1/CNC2 (see
    tests/new_sample_workbook.py OPERATORS). A second call with a small extra
    qty (still well under 50) keeps the step in-progress while moving the
    book signature, for tests that need a fresh "book changed" edge."""
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": entry_date, "qty_produced": qty_produced, "qty_rejected": 0,
        "shift": "1st shift", "process": "CNC FIRST SIDE", "operator": "Alpha",
    })
    assert r.status_code == 200, r.text
    return r


def test_done_computes_frozen_set_from_partial_punch(admin_client_with_book):
    client = admin_client_with_book
    # Apply an initial plan so a last-applied schedule exists.
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_optimize_done(client)
    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text

    # Punch a PARTIAL quantity on an in-progress in-house machining step.
    _punch_partial(client)

    # Compute the frozen set (Done path helper) directly.
    import api.main as m
    frozen = m._compute_and_store_frozen()
    assert frozen == book_store.load_frozen_ops()
    assert any(r["remaining_qty"] > 0 and r["machine"] for r in frozen)


def test_both_manual_and_auto_optimize_respect_frozen(admin_client_with_book, monkeypatch):
    """Owner decision, 2026-07-29: the admin's manual 'Start deep search'
    (POST /optimize) now ALSO respects the in-progress freeze — the same way
    the Done-button auto path already did (deep-search is pressed in week 2
    when week-1 work is already running). _start_optimize recomputes the
    frozen set from the latest punches + last-applied plan itself (inside the
    lock, right before reading it) — so BOTH the manual and auto call sites
    must see the stored, NON-EMPTY frozen set reach
    optimize_service.prepare_contest."""
    client = admin_client_with_book
    import api.main as m

    # The suite-wide autouse fixture (tests/conftest.py::_no_auto_optimize)
    # sets AUTO_OPTIMIZE=0 for every test so the self-tuning trigger never
    # fires spontaneously; re-enable it here since this test exercises
    # _try_start_auto directly (its documented override path).
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")

    # Apply an initial plan, then punch a partial quantity so a non-empty
    # frozen set gets stored ahead of both runs below.
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_optimize_done(client)
    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text
    _punch_partial(client)
    stored_frozen = m._compute_and_store_frozen()
    assert stored_frozen, "expected a non-empty frozen set from the partial punch"

    # Wrap prepare_contest to record the frozen= kwarg each call receives,
    # then call through to the real implementation. As of Task 19 + fix round
    # 1, THREE call sites explicitly pass frozen= on the path a single
    # /optimize click exercises, always in this fixed synchronous order:
    #   1. _incumbent_metrics() (the worst-order ceiling, called first by
    #      _start_optimize) — always the currently-stored frozen set.
    #   2. _start_optimize's OWN contest-setup call — the one this test is
    #      actually about (now the stored set for BOTH manual and auto).
    #   3. _metrics_for_ranks() inside _finalize_optimize, IF the search found
    #      any ranks — always the currently-stored frozen set (fix round 1,
    #      Critical #1), regardless of auto.
    # Calls 1 and 2 are guaranteed before the background search thread is
    # even spawned; call 3 (and, for an auto run that applies, further calls
    # from _auto_apply_result/_movement_note/_optimize_apply) land afterward
    # and are NOT asserted on here — their count depends on whether ranks were
    # found / whether the contest beat the incumbent, which is not
    # deterministic to pin down. Only calls 1 and 2 (a fixed pair, at a fixed
    # offset from a "before" checkpoint) are checked.
    recorded = []
    real_prepare_contest = m.optimize_service.prepare_contest

    def _recording_prepare_contest(*args, **kwargs):
        if "frozen" in kwargs:
            recorded.append(kwargs["frozen"])
        return real_prepare_contest(*args, **kwargs)

    monkeypatch.setattr(m.optimize_service, "prepare_contest", _recording_prepare_contest)

    # 1) Manual "Start deep search" (auto=False) — now RESTRICTED too:
    #    _start_optimize recomputes the frozen set itself right before reading
    #    it, so the stored (non-empty) set reaches prepare_contest at
    #    _start_optimize's own contest-setup call (the second of the fixed
    #    leading pair) even on the manual path.
    before = len(recorded)
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_optimize_done(client)
    assert len(recorded) >= before + 2
    assert recorded[before + 1] == stored_frozen, (
        f"manual optimize must pass the stored frozen set, got {recorded[before + 1]!r}")
    assert recorded[before + 1], "manual optimize must pass a NON-EMPTY frozen set here"
    # Checkpoint: everything from here on (finalize, possible auto-apply
    # bookkeeping) records additional frozen= calls whose COUNT depends on
    # whether the contest happened to beat the incumbent — not asserted on.
    checkpoint = len(recorded)

    # 2) Auto (Done-button) path — also restricted: the stored frozen set
    #    reaches prepare_contest. The manual run above wrote a last_searched
    #    snapshot of the (unchanged-since) book, so _try_start_auto would
    #    otherwise skip as "nothing material changed" — punch a bit more
    #    (still leaving the step in-progress) so the book signature moves and
    #    the auto path actually starts. _compute_and_store_frozen is pure/
    #    deterministic over current actuals + the applied schedule, so calling
    #    it here yields the exact same rows _start_optimize recomputes
    #    internally right before starting its own contest.
    _punch_partial(client, qty_produced=5)
    expected_frozen = m._compute_and_store_frozen()
    assert expected_frozen, "expected the frozen set to remain non-empty after the extra punch"
    started = m._try_start_auto()
    assert started, "expected _try_start_auto to start a contest (book changed since the applied/searched snapshot)"
    _wait_optimize_done(client)
    # The same synchronous pair repeats for this call: [checkpoint] is the
    # incumbent-ceiling call (non-empty, unconditionally), [checkpoint + 1] is
    # _start_optimize's own contest-setup call — must equal the stored,
    # non-empty frozen set (auto=True). Any further calls from finalize/apply
    # bookkeeping land after this pair and aren't asserted on here.
    assert len(recorded) >= checkpoint + 2
    assert recorded[checkpoint + 1] == expected_frozen
    assert recorded[checkpoint + 1], "auto path must pass the non-empty frozen set"


def test_plan_passes_stored_frozen_to_run_forward(admin_client_with_book, monkeypatch):
    """Task 19 integration keystone: the DISPLAYED plan (`_plan`, exercised here
    via GET /gantt) must pin the stored frozen (in-progress) set — so what
    everyone sees matches the frozen, contest-applied plan, not a fresh
    re-assignment of an already-started op."""
    client = admin_client_with_book
    import api.main as m

    # Apply an initial plan, then punch a partial quantity and compute+store a
    # frozen set (mirrors the "Done" flow's _compute_and_store_frozen step).
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_optimize_done(client)
    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text
    _punch_partial(client)
    m._compute_and_store_frozen()

    stored_frozen = book_store.load_frozen_ops()
    assert stored_frozen, "expected a non-empty frozen set from the partial punch"

    # Wrap run_forward to record the `frozen=` kwarg each call receives, then
    # delegate to the real implementation (captured before patching).
    real_run_forward = m.run_forward
    recorded = []

    def _recording_run_forward(*args, **kwargs):
        recorded.append(kwargs.get("frozen"))
        return real_run_forward(*args, **kwargs)

    monkeypatch.setattr(m, "run_forward", _recording_run_forward)

    # _plan caches by fingerprint (which now folds in the frozen set — see
    # _plan_fingerprint) — clear it directly so this request is guaranteed to
    # recompute rather than serve a result cached before the freeze existed.
    m._PLAN_CACHE.clear()

    resp = client.get("/gantt")
    assert resp.status_code == 200, resp.text

    assert recorded, "run_forward was not called by GET /gantt (_plan)"
    assert recorded[-1] == stored_frozen, (
        "_plan did not pass the stored frozen set through to run_forward: "
        f"{recorded[-1]!r} != {stored_frozen!r}")


def test_local_contest_search_is_frozen_aware(admin_client_with_book, monkeypatch):
    """Fix round 1, Critical #2: _start_optimize's LOCAL fallback (local_job)
    must pass the stored frozen set into optimizer.sweep_optimize — otherwise
    the sequence SEARCH itself never honors the freeze on the local path
    (cloud unconfigured/failed/timeout — the default in this fixture and on
    the free Render instance), even though every scoring/replay call site
    (prepare_contest/_all_lines_schedule) does."""
    client = admin_client_with_book
    import api.main as m

    # Re-enable the auto trigger for this test (the suite-wide autouse
    # fixture tests/conftest.py::_no_auto_optimize sets AUTO_OPTIMIZE=0 for
    # every test by design; dedicated auto tests re-enable it via monkeypatch).
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")

    # Force the LOCAL path: no cloud dispatch configured. The fixture already
    # deletes these env vars; re-assert via monkeypatch (never raw os.environ)
    # since this test's premise depends on the local fallback running.
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)

    # Apply an initial plan, punch a partial quantity, and compute+store a
    # non-empty frozen set (mirrors the "Done" flow's
    # _compute_and_store_frozen step).
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_optimize_done(client)
    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text
    _punch_partial(client)
    stored_frozen = m._compute_and_store_frozen()
    assert stored_frozen, "expected a non-empty frozen set from the partial punch"

    # Wrap optimizer.sweep_optimize to record the frozen= kwarg it receives on
    # each call, then delegate to the real implementation.
    recorded = []
    real_sweep_optimize = m.optimizer.sweep_optimize

    def _recording_sweep_optimize(*args, **kwargs):
        recorded.append(kwargs.get("frozen"))
        return real_sweep_optimize(*args, **kwargs)

    monkeypatch.setattr(m.optimizer, "sweep_optimize", _recording_sweep_optimize)

    # Trigger an AUTO contest directly (the book changed via the partial punch
    # since the applied plan, so _try_start_auto must not skip).
    started = m._try_start_auto()
    assert started, ("expected _try_start_auto to start a contest "
                     "(book changed since the applied plan)")
    _wait_optimize_done(client)

    assert recorded, ("optimizer.sweep_optimize was never called — expected "
                      "the local search path to run")
    assert recorded[0] == stored_frozen, (
        "the local contest SEARCH did not receive the stored frozen set: "
        f"{recorded[0]!r} != {stored_frozen!r}")
