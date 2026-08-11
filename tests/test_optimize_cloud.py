"""The cloud-optimize path: dispatch to GitHub Actions, worker endpoints
(secret-gated payload/progress/result), and the fallbacks that keep the
Optimize button working when the cloud misbehaves. No network — the dispatch
call is stubbed and the 'worker' is driven in-process via the same
optimize_service functions the real worker script uses."""
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, optimize_service
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B

SECRET = "test-worker-secret"


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _cloud_env(monkeypatch, timeout_min="5", oracle_claim_min="0"):
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "fake-token")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", SECRET)
    monkeypatch.setenv("OPTIMIZE_CLOUD_TIMEOUT_MIN", timeout_min)
    # Oracle claim window (2026-08-01): default 0 so every pre-existing test in
    # this file keeps today's immediate-GitHub-dispatch timing. Tests that
    # exercise the claim window itself override this explicitly.
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", oracle_claim_min)


def _wait(m, state, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = m._optimize_status()
        if st["state"] == state:
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for state={state}; last={st}")


def test_worker_endpoints_require_the_secret(monkeypatch):
    _cloud_env(monkeypatch)
    m = _api()
    client = TestClient(m.app)
    assert client.get("/optimize/job/xyz").status_code == 401           # no session
    assert client.get("/optimize/job/xyz",
                      headers={"X-Worker-Secret": "wrong"}).status_code == 401
    r = client.get("/optimize/job/xyz", headers={"X-Worker-Secret": SECRET})
    assert r.status_code == 404                                         # authed, no job


def test_worker_secret_check_does_not_500_on_non_ascii_header(monkeypatch):
    """hmac.compare_digest(str, str) raises TypeError on non-ASCII input;
    the check must fail closed (401/403), never 500."""
    _cloud_env(monkeypatch)
    m = _api()
    client = TestClient(m.app)
    r = client.post("/optimize/progress",
                     json={"job_id": "xyz"},
                     headers={"X-Worker-Secret": "£key".encode("latin-1")})
    assert r.status_code in (401, 403)


def test_cloud_round_trip_via_the_worker_endpoints(monkeypatch):
    """The full loop, in-process: dispatch (stubbed) → worker fetches the
    payload → runs the contest with the SAME service functions the real
    worker uses → posts progress + result → the job finishes with the same
    result shape as a local run."""
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    dispatched = {}
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda cloud, job_id: dispatched.setdefault("job_id", job_id) or True)

    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["state"] == "running" and st["mode"] == "cloud"
    t0 = time.time()
    while "job_id" not in dispatched and time.time() - t0 < 10:
        time.sleep(0.02)
    job_id = dispatched["job_id"]

    client = TestClient(m.app)
    h = {"X-Worker-Secret": SECRET}
    job = client.get(f"/optimize/job/{job_id}", headers=h)
    assert job.status_code == 200 and not job.json()["cancel"]
    payload = job.json()["payload"]
    payload["budget_per_candidate"] = 4          # keep the test fast

    out = optimize_service.run_contest(payload, processes=1)
    pr = client.post("/optimize/progress", headers=h,
                     json={"job_id": job_id, "evals": out["evals"],
                           "best": out["best"]})
    assert pr.status_code == 200 and pr.json() == {"cancel": False}
    rr = client.post("/optimize/result", headers=h,
                     json={"job_id": job_id, "winner_overlap": out["winner_overlap"],
                           "ranks": out["ranks"], "best": out["best"],
                           "rows": out["rows"], "evals": out["evals"]})
    assert rr.status_code == 200 and rr.json()["ok"]

    st = _wait(m, "done")
    # `best` is now the winner's ranks replayed LOCALLY (so the panel number == the plan
    # the user gets on Apply), which may differ a hair from the worker's own measurement.
    assert st["mode"] == "cloud" and st["best"]["makespan_days"] is not None
    assert st["best"] == m._metrics_for_ranks(out["ranks"], out["winner_overlap"])
    assert st["best_overlap"] == out["winner_overlap"]
    assert st["baseline"]                        # computed app-side
    meta = m._optimize_apply()                   # Apply works off a cloud result
    assert meta["best_overlap"] == out["winner_overlap"]


def test_dispatch_failure_falls_back_to_local_compute(monkeypatch):
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: False)
    m._start_optimize(budget_evals=15, label="deep", background=True)
    st = _wait(m, "done")
    assert st["mode"] == "local" and st["best"]   # computed here, button still works


def test_cloud_timeout_falls_back_to_local_compute(monkeypatch):
    _cloud_env(monkeypatch, timeout_min="0.005")   # ~0.3 s
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)
    m._start_optimize(budget_evals=15, label="deep", background=True)
    st = _wait(m, "done", timeout=30.0)
    assert st["mode"] == "local" and st["best"]


def test_worker_error_report_falls_back_to_local_compute(monkeypatch):
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    dispatched = {}
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda cloud, job_id: dispatched.setdefault("job_id", job_id) or True)
    m._start_optimize(budget_evals=15, label="deep", background=True)
    t0 = time.time()
    while "job_id" not in dispatched and time.time() - t0 < 10:
        time.sleep(0.02)
    client = TestClient(m.app)
    r = client.post("/optimize/result", headers={"X-Worker-Secret": SECRET},
                    json={"job_id": dispatched["job_id"], "error": "runner exploded"})
    assert r.status_code == 200 and r.json().get("fallback") == "local"
    st = _wait(m, "done", timeout=30.0)
    assert st["mode"] == "local" and st["best"]


def test_no_cloud_env_means_pure_local_as_before(monkeypatch):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)
    m = _api()
    _seed_book()
    st = m._start_optimize(budget_evals=15, label="deep", background=False)
    assert st["state"] == "done" and st["mode"] == "local"


def test_cloud_budget_env_override(monkeypatch):
    new_cfg = Config(scheduler="new")
    classic_cfg = Config(scheduler="classic")
    monkeypatch.delenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", raising=False)
    assert optimize_service.cloud_budget(new_cfg) == 150       # current defaults hold
    assert optimize_service.cloud_budget(classic_cfg) == 400
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "300")
    assert optimize_service.cloud_budget(new_cfg) == 300       # override, both modes
    assert optimize_service.cloud_budget(classic_cfg) == 300
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "garbage")
    assert optimize_service.cloud_budget(new_cfg) == 150       # invalid -> default
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "0")
    assert optimize_service.cloud_budget(new_cfg) == 150       # non-positive -> default


def test_pending_requires_secret_and_reports_unclaimed_job(monkeypatch):
    """GET /optimize/pending is the poll point for an always-on (Oracle) worker:
    it reports a running cloud job's id only while its payload is UNCLAIMED —
    once ANY worker fetches GET /optimize/job/{id} (the existing payload
    endpoint), /optimize/pending goes back to null even though the job is
    still running, so a second poller doesn't double-pick the same job."""
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)

    client = TestClient(m.app)
    H = {"X-Worker-Secret": SECRET}

    assert client.get("/optimize/pending").status_code == 401          # no secret -> session gate
    r = client.get("/optimize/pending", headers=H)
    assert r.status_code == 200 and r.json()["job_id"] is None         # idle -> null

    try:
        st = m._start_optimize(budget_evals=15, label="quick", background=True)
        assert st["state"] == "running" and st["mode"] == "cloud"

        jid = None
        t0 = time.time()
        while time.time() - t0 < 10:
            jid = client.get("/optimize/pending", headers=H).json()["job_id"]
            if jid:
                break
            time.sleep(0.02)
        assert jid                                                      # waiting + unclaimed

        job = client.get(f"/optimize/job/{jid}", headers=H)             # fetch = claim
        assert job.status_code == 200
        assert client.get("/optimize/pending", headers=H).json()["job_id"] is None  # claimed
    finally:
        # This test deliberately never posts a worker result, so cloud_job is
        # left parked waiting (its poll loop sleeps 2s between checks). Stop
        # it directly and wait out that poll interval so the background
        # thread has actually exited before the next test's _api() reload
        # rebinds _OPTIMIZE/_OPTIMIZE_LOCK out from under it (the thread looks
        # those names up fresh on the module each iteration).
        with m._OPTIMIZE_LOCK:
            m._OPTIMIZE["state"] = "done"
        time.sleep(2.2)


def test_claimed_in_window_skips_github_dispatch(monkeypatch):
    """An always-on (Oracle) worker fetches the payload inside the claim
    window: cloud_job must never fall through to the GitHub dispatch."""
    _cloud_env(monkeypatch, oracle_claim_min="0.05")   # ~3s window
    m = _api()
    _seed_book()
    calls = []
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda c, j: calls.append(j) or True)
    try:
        st = m._start_optimize(budget_evals=15, label="deep", background=True)
        assert st["state"] == "running" and st["mode"] == "cloud"
        with m._OPTIMIZE_LOCK:
            jid = m._OPTIMIZE["job_id"]
        client = TestClient(m.app)
        r = client.get(f"/optimize/job/{jid}", headers={"X-Worker-Secret": SECRET})
        assert r.status_code == 200                     # claim recorded
        time.sleep(4)                                    # let the window elapse
        assert calls == []                               # GitHub never dispatched
    finally:
        # Claimed → cloud_job falls straight into the watchdog loop, parked
        # with no worker ever posting a result. Same cleanup as the /pending
        # test above: stop it directly and wait out its 2s poll interval.
        with m._OPTIMIZE_LOCK:
            m._OPTIMIZE["state"] = "done"
        time.sleep(2.2)


def test_unclaimed_window_falls_through_to_github(monkeypatch):
    """Nobody claims the job inside the window: cloud_job falls through to
    the existing GitHub dispatch, exactly as before the Oracle tier existed."""
    _cloud_env(monkeypatch, oracle_claim_min="0.02")   # ~1s window
    m = _api()
    _seed_book()
    calls = []
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda c, j: calls.append(j) or True)
    try:
        m._start_optimize(budget_evals=15, label="deep", background=True)
        t0 = time.time()
        while not calls and time.time() - t0 < 10:
            time.sleep(0.05)
        assert len(calls) == 1                           # dispatched after the window
    finally:
        with m._OPTIMIZE_LOCK:
            m._OPTIMIZE["state"] = "done"
        time.sleep(2.2)


def test_zero_window_dispatches_immediately(monkeypatch):
    """ORACLE_CLAIM_TIMEOUT_MIN<=0 skips the wait entirely — today's
    immediate-dispatch behavior, unchanged."""
    _cloud_env(monkeypatch, oracle_claim_min="0")
    m = _api()
    _seed_book()
    calls = []
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda c, j: calls.append(j) or True)
    try:
        m._start_optimize(budget_evals=15, label="deep", background=True)
        t0 = time.time()
        while not calls and time.time() - t0 < 10:
            time.sleep(0.05)
        assert len(calls) == 1                           # dispatched immediately
    finally:
        with m._OPTIMIZE_LOCK:
            m._OPTIMIZE["state"] = "done"
        time.sleep(2.2)


def test_stop_during_claim_window_never_dispatches(monkeypatch):
    """A Stop pressed WHILE waiting for the Oracle poller to claim the job
    must be honoured promptly — not ignored for the rest of the window and
    then dispatched to GitHub anyway (the race the claim window introduced:
    pre-Oracle, dispatch was immediate so this was unreachable)."""
    _cloud_env(monkeypatch, oracle_claim_min="0.05")   # ~3s window
    m = _api()
    _seed_book()
    calls = []
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda c, j: calls.append(j) or True)
    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["state"] == "running" and st["mode"] == "cloud"
    time.sleep(0.5)
    m._optimize_cancel()                                 # Stop, ~0.5s into the window

    t0 = time.time()
    st = m._optimize_status()
    while st["state"] == "running" and time.time() - t0 < 5:
        time.sleep(0.05)
        st = m._optimize_status()
    assert st["state"] != "running"                      # reached a terminal state promptly
    assert calls == []                                   # GitHub never dispatched
    assert st["state"] == "failed" and "stopped" in (st["error"] or "").lower()


def test_last_moment_claim_during_final_sleep_skips_dispatch(monkeypatch):
    """A claim landing during the claim loop's LAST 2s sleep must still be
    honoured by a pre-dispatch re-check — not missed by a stale local
    `claimed=False` captured before the claim actually happened."""
    _cloud_env(monkeypatch, oracle_claim_min="0.05")   # ~3s window
    m = _api()
    _seed_book()
    calls = []
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda c, j: calls.append(j) or True)
    try:
        st = m._start_optimize(budget_evals=15, label="deep", background=True)
        assert st["state"] == "running" and st["mode"] == "cloud"
        with m._OPTIMIZE_LOCK:
            jid = m._OPTIMIZE["job_id"]
        time.sleep(2.5)                                  # deep into the ~3s window
        client = TestClient(m.app)
        r = client.get(f"/optimize/job/{jid}", headers={"X-Worker-Secret": SECRET})
        assert r.status_code == 200                      # claim recorded, last-moment
        time.sleep(2)                                     # window elapses + a margin
        assert calls == []                                # GitHub never dispatched
    finally:
        # Claimed → cloud_job falls straight into the watchdog loop, parked
        # with no worker ever posting a result. Same cleanup as the other
        # claim tests above.
        with m._OPTIMIZE_LOCK:
            m._OPTIMIZE["state"] = "done"
        time.sleep(2.2)
