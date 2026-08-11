"""POST /optimize/shard-result: worker-secret auth, accumulation, and
finalize-when-all-arrived == a single whole-contest run_contest winner."""
from datetime import date

import io
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine.loaders import load_all
from engine.config import Config
from engine.models import Order
from engine import optimize_service as osvc
from tests.new_sample_workbook import build_new_sample_bytes


def _payload(*, budget=6, candidates=(60, 80)):
    """A cloud payload on the fully-staffed new-engine sample workbook (mirrors
    tests/test_optimize_shard.py::_new_engine_payload) so a real contest run
    has routings/masters/operators to schedule against under scheduler=='new'."""
    raw = build_new_sample_bytes()
    so_lines, _masters = load_all(io.BytesIO(raw))
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                apply_operator_logic=True, overlap_percent=candidates[0])
    cfg.validate()
    return osvc.build_payload(orders, [], raw, cfg, seed=1,
                              candidates=list(candidates), budget_per_candidate=budget)


def _seed_running(m, payload, job_id="job-1"):
    """Put _OPTIMIZE into a running cloud job the collector will accept."""
    cfg = Config.from_dict(payload["config"])
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="running", job_id=job_id, cloud_payload=payload,
                           base_config=cfg, baseline=None, label="deep",
                           cancel=False, cloud_failed=False, claimed=False,
                           shards={}, shard_total=None, evals=0, best=None,
                           started_mono=0.0)


def test_shard_result_requires_worker_secret(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    c = TestClient(m.app)
    r = c.post("/optimize/shard-result", json={"job_id": "x", "shard_index": 0,
                                               "shard_total": 2, "rows": []})
    assert r.status_code in (401, 403)  # no secret header → rejected


def test_all_shards_finalize_matches_run_contest(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    full = osvc.run_contest(payload, processes=1)     # the reference winner
    _seed_running(m, payload)
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    SHARD_TOTAL = 3
    for idx in range(SHARD_TOTAL):
        out = osvc.run_contest_slice(payload, idx, SHARD_TOTAL, processes=1)
        r = c.post("/optimize/shard-result", headers=hdr, json={
            "job_id": "job-1", "shard_index": idx, "shard_total": SHARD_TOTAL,
            "rows": out["rows"], "evals": out["evals"], "cancelled": out["cancelled"]})
        assert r.status_code == 200
    # After the last shard the job finalized to the same winner run_contest found.
    assert m._OPTIMIZE["state"] == "done"
    res = m._OPTIMIZE["result"]
    assert res is not None
    # _finalize_optimize stores the winning knob value under "best_overlap"
    # and the winning machine-set under "flexible_machines" (confirmed by
    # reading _finalize_optimize's `result={...}` assignment) — not
    # "overlap"/"flexible" as an earlier sketch of this test assumed.
    assert res["best_overlap"] == full["winner_overlap"]
    assert res.get("flexible_machines") == full["winner_flexible"]


def test_finalize_crash_terminalizes_instead_of_wedging(monkeypatch):
    """FIX 2 regression. The endpoint's `_finalize_from_shards` call was
    unguarded — a raise there (reachable beyond malformed worker data:
    `_finalize_optimize` reads the operator table from the durable store
    before its terminal state write, and a transient DB blip raises there)
    left `shards_finalizing` latched with `shards` still populated and state
    stuck "running" forever: `_start_optimize`'s guard then 409s every future
    optimize, including the daily auto-run, until a process restart. The
    endpoint must still terminalize the job even though it re-raises (so the
    worker's retry/error handling is unaffected)."""
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")

    def _boom(job_id):
        raise RuntimeError("boom: transient store blip")
    monkeypatch.setattr(m, "_finalize_from_shards", _boom)

    c = TestClient(m.app, raise_server_exceptions=False)
    r = c.post("/optimize/shard-result", headers={"X-Worker-Secret": "s3cr3t"},
               json={"job_id": "job-1", "shard_index": 0, "shard_total": 1,
                     "rows": [], "evals": 10, "cancelled": False})
    assert r.status_code == 500, "the crash must still surface to the caller"
    assert m._OPTIMIZE["state"] == "failed", (
        "a finalize crash must terminalize the job, not leave it wedged 'running'")
    assert m._OPTIMIZE["cancel"] is False
    assert m._OPTIMIZE["cloud_failed"] is False
    assert "could not finalize" in (m._OPTIMIZE["error"] or "")


def test_stale_shard_is_noop(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    c = TestClient(m.app)
    r = c.post("/optimize/shard-result", headers={"X-Worker-Secret": "s3cr3t"},
               json={"job_id": "OTHER", "shard_index": 0, "shard_total": 2, "rows": []})
    assert r.status_code == 200        # ignored, never crashes
    assert m._OPTIMIZE["state"] == "running"


def test_duplicate_final_shard_does_not_double_finalize(monkeypatch):
    """The worker's _call retries up to 5x (at-least-once delivery). A
    duplicate POST of the already-recorded LAST shard_index arriving WHILE
    the first finalize is still mid-flight (state still "running") must not
    fire a second _finalize_from_shards — that would double-run
    _finalize_optimize (double auto-note / double auto-apply). A re-entrant
    spy fires the duplicate POST from inside the first (and, if the guard
    holds, only) call to _finalize_from_shards, deterministically reproducing
    the race window without threading."""
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    SHARD_TOTAL = 3
    slices = [osvc.run_contest_slice(payload, i, SHARD_TOTAL, processes=1) for i in range(SHARD_TOTAL)]

    calls = []
    orig = m._finalize_from_shards
    def spy(job_id):
        calls.append(job_id)
        if len(calls) == 1:
            # A duplicate of the FINAL shard arrives while we're still mid-finalize
            # (state is still "running" here — orig hasn't run yet).
            last = SHARD_TOTAL - 1
            c.post("/optimize/shard-result", headers=hdr, json={
                "job_id": "job-1", "shard_index": last, "shard_total": SHARD_TOTAL,
                "rows": slices[last]["rows"], "evals": slices[last]["evals"],
                "cancelled": slices[last]["cancelled"]})
        orig(job_id)
    monkeypatch.setattr(m, "_finalize_from_shards", spy)

    for i in range(SHARD_TOTAL):
        c.post("/optimize/shard-result", headers=hdr, json={
            "job_id": "job-1", "shard_index": i, "shard_total": SHARD_TOTAL,
            "rows": slices[i]["rows"], "evals": slices[i]["evals"],
            "cancelled": slices[i]["cancelled"]})

    assert calls == ["job-1"]                 # finalize claimed exactly ONCE
    assert m._OPTIMIZE["state"] == "done"


def test_out_of_range_shard_index_is_noop(monkeypatch):
    """An out-of-range shard_index must never pad the accumulated count toward
    a premature finalize."""
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload)
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda job_id: calls.append(job_id))
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    r = c.post("/optimize/shard-result", headers=hdr, json={
        "job_id": "job-1", "shard_index": 999, "shard_total": 2,
        "rows": [], "evals": 0, "cancelled": False})
    assert r.status_code == 200                  # 200 no-op, never crashes
    assert m._OPTIMIZE["state"] == "running"      # still running, not finalized
    assert calls == []                            # finalize never triggered
    assert 999 not in m._OPTIMIZE["shards"]
    assert len(m._OPTIMIZE["shards"]) == 0


def test_shards_available_predicate(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    # No shards yet.
    assert m._shards_available("job-1") is False
    out0 = osvc.run_contest_slice(payload, 0, 3, processes=1)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["shard_total"] = 3
        m._OPTIMIZE["shards"][0] = {"rows": out0["rows"], "evals": out0["evals"],
                                     "cancelled": out0["cancelled"]}
    # A running job matching job_id, with >=1 shard, is available.
    assert m._shards_available("job-1") is True
    # A different/unknown job_id is not.
    assert m._shards_available("job-OTHER") is False
    # Not running (e.g. already finished) → not available.
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
    assert m._shards_available("job-1") is False


def test_partial_finalize_uses_arrived_shards(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    # Only shard 0 of 3 arrives (shards 1,2 never posted):
    out0 = osvc.run_contest_slice(payload, 0, 3, processes=1)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["shard_total"] = 3
        m._OPTIMIZE["shards"][0] = {"rows": out0["rows"], "evals": out0["evals"],
                                     "cancelled": out0["cancelled"]}
    # Watchdog partial finalize over the one arrived shard:
    m._finalize_from_shards("job-1")
    assert m._OPTIMIZE["state"] == "done"      # a valid winner from shard 0 alone
    # the winner is one of shard 0's candidates
    shard0_overlaps = {r["overlap"] for r in out0["rows"]}
    assert m._OPTIMIZE["result"]["best_overlap"] in shard0_overlaps


def test_second_finalize_after_done_is_noop(monkeypatch):
    """After a (partial) finalize has already moved the job to 'done', a
    second call to _finalize_from_shards for the same job_id must be a no-op
    — its entry guard sees state != 'running' and returns immediately,
    leaving the stored result untouched. This is the invariant that keeps the
    watchdog's shard-salvage claim and the /optimize/shard-result collector's
    claim mutually exclusive: once either has finalized, the flag-holder
    check on the OTHER path finds `_OPTIMIZE["state"]` no longer "running"."""
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    out0 = osvc.run_contest_slice(payload, 0, 3, processes=1)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["shard_total"] = 3
        m._OPTIMIZE["shards"][0] = {"rows": out0["rows"], "evals": out0["evals"],
                                     "cancelled": out0["cancelled"]}
    m._finalize_from_shards("job-1")
    assert m._OPTIMIZE["state"] == "done"
    result_after_first = m._OPTIMIZE["result"]

    # A second finalize call (e.g. a stray watchdog tick) must not re-run.
    m._finalize_from_shards("job-1")
    assert m._OPTIMIZE["state"] == "done"
    assert m._OPTIMIZE["result"] == result_after_first


def test_no_winner_finalize_clears_shards_so_watchdog_goes_local(monkeypatch):
    """FINAL-REVIEW fix: when every shard's rows merge to no eligible winner,
    _finalize_from_shards must clear _OPTIMIZE["shards"] (not just set
    cloud_failed) — otherwise the watchdog's next tick still sees
    have_shards=True, can't claim a shard-finalize (already claimed by this
    call), and never falls to go_local either, wedging the job in
    state="running" forever. Two shards posting empty rows (via the real
    /optimize/shard-result endpoint, so the all-arrived collector path fires)
    reproduces the no-winner merge without needing a crafted payload."""
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    SHARD_TOTAL = 2
    for idx in range(SHARD_TOTAL):
        r = c.post("/optimize/shard-result", headers=hdr, json={
            "job_id": "job-1", "shard_index": idx, "shard_total": SHARD_TOTAL,
            "rows": [], "evals": 0, "cancelled": False})
        assert r.status_code == 200
    assert m._OPTIMIZE["cloud_failed"] is True
    assert m._OPTIMIZE["shards"] == {}          # cleared -> watchdog sees have_shards=False
    assert m._OPTIMIZE["state"] == "running"    # still running; watchdog will go local next tick


def test_duplicate_shard_after_no_winner_finalize_does_not_repopulate_shards(monkeypatch):
    """Residual close on Finding 1: the worker retries a POST up to 5x
    (at-least-once delivery), so a duplicate/late shard-result can arrive
    AFTER a no-winner finalize has already cleared _OPTIMIZE["shards"]={}
    and set cloud_failed=True, while state is still "running" (the watchdog
    hasn't ticked yet). Without the `not shards_finalizing` recording guard,
    that duplicate POST would re-enter the `state=="running" and job matches`
    check, pass, and re-write _OPTIMIZE["shards"][idx] — repopulating the
    dict the fix just emptied. Since shards_finalizing is already latched
    True, the watchdog's next tick would then see have_shards=True,
    claim_shard_finalize=False (already claimed), go_local=False — wedged
    again, same bug via a different door. The guard must reject the
    duplicate as a no-op instead."""
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    SHARD_TOTAL = 2
    for idx in range(SHARD_TOTAL):
        r = c.post("/optimize/shard-result", headers=hdr, json={
            "job_id": "job-1", "shard_index": idx, "shard_total": SHARD_TOTAL,
            "rows": [], "evals": 0, "cancelled": False})
        assert r.status_code == 200
    assert m._OPTIMIZE["cloud_failed"] is True
    assert m._OPTIMIZE["shards"] == {}
    assert m._OPTIMIZE["state"] == "running"

    # A duplicate/late POST of shard 0 arrives after the claim.
    r = c.post("/optimize/shard-result", headers=hdr, json={
        "job_id": "job-1", "shard_index": 0, "shard_total": SHARD_TOTAL,
        "rows": [], "evals": 0, "cancelled": False})
    assert r.status_code == 200                # still a 200 no-op
    assert m._OPTIMIZE["shards"] == {}          # NOT repopulated
    assert m._OPTIMIZE["shards_finalizing"] is True


def test_progress_sums_across_shards(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    c.post("/optimize/progress", headers=hdr,
           json={"job_id": "job-1", "evals": 10, "shard_index": 0})
    c.post("/optimize/progress", headers=hdr,
           json={"job_id": "job-1", "evals": 7, "shard_index": 1})
    assert m._OPTIMIZE["evals"] == 17          # summed, not max
    # a shard's own count going up replaces only its bucket
    c.post("/optimize/progress", headers=hdr,
           json={"job_id": "job-1", "evals": 25, "shard_index": 0})
    assert m._OPTIMIZE["evals"] == 32
