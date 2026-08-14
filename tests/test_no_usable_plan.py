"""A deep search that finds NO PLAN must end in a sentence, never a KeyError.

THE LIVE FAILURE (2026-08-15, twice, owner blocked). "Start deep search" on the
CP engine, the solve ran on the GitHub Actions worker, posted its result — and
the app answered:

    Optimization failed: could not finalize the cloud result: 'ontime_breach'

THE CHAIN, and every link is pinned below:

  1. The CP solve is TIME-BOXED. When ``cp_engine.solve.solve_book`` comes back
     with ``status_ok`` False — no solution inside the limit, a normal outcome on
     a big book — ``cp_adapter.solve`` returns a bare ``OptimizeResult``.
  2. ``OptimizeResult.best`` defaults to an **EMPTY DICT**, not None.
  3. So the worker posts a contest row ``{"eligible": True, "best": {}}``, and
     EVERY "did this candidate produce a plan?" guard on the way home asked
     ``best is None`` — which ``{}`` sails straight through.
  4. ``{}`` is elected winner and reaches ``optimizer.score``, whose ``metrics
     ["ontime_breach"]`` is REQUIRED on purpose (a defaulted one would score a
     missing plan as a PERFECT plan). KeyError — surfaced raw, to the owner.

The fix is NOT to soften ``score``. It is ``optimizer.scoreable``: one definition
of "this dict is a plan that can be ranked", asked at every gate a metrics dict of
unknown provenance passes through.

Every test here was RED before the fix; see the report for the mutation matrix.
"""
from __future__ import annotations

import importlib
import io
import json
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, cp_adapter, optimize_service as osvc, optimizer
from engine.config import Config
from engine.loaders import load_all
from engine.models import Order
from tests.new_sample_workbook import build_new_sample_bytes

SECRET = "s3cr3t"
HDR = {"X-Worker-Secret": SECRET}
PLAN_START = date(2025, 3, 3)


def _metrics(**kw):
    """A COMPLETE metrics dict — every key ``score`` requires."""
    out = {"ontime_breach": 5.0, "makespan_days": 10.0, "total_late_days": 3,
           "max_late_days": 2, "max_committed_slip": 0}
    out.update(kw)
    return out


# =========================================================================== #
# 1. The source: what an unsolved CP book actually hands back
# =========================================================================== #

class _NoSolution:
    """What ``cp_engine.solve.solve_book`` returns when it proved nothing."""
    status_ok = False
    status = "UNKNOWN"
    genome: dict = {}
    total_late_days = 0
    spread = 0
    lower_bound_days = 0


def _cp_book():
    raw = build_new_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    cfg = Config(scheduler="cp", plan_start_date=PLAN_START,
                 apply_operator_logic=True, cp_time_limit_sec=5, cp_num_workers=1)
    cfg.validate()
    return raw, so_lines, masters, cfg


def test_an_unsolved_cp_book_reports_no_plan_as_an_EMPTY_dict(monkeypatch):
    """The fixture the rest of this file uses is the REAL thing, not a guess.

    If this ever starts returning None the tests below stop discriminating, so it
    is asserted rather than assumed."""
    _raw, so_lines, masters, cfg = _cp_book()
    import cp_engine.solve as cps
    monkeypatch.setattr(cps, "solve_book", lambda *a, **k: _NoSolution())
    res = cp_adapter.solve(so_lines, cfg, masters)
    assert res.best == {} and res.best is not None, (
        "an unsolved CP book no longer reports {} — retune these tests")
    assert not optimizer.scoreable(res.best)


# =========================================================================== #
# 2. The one definition
# =========================================================================== #

def test_score_still_fails_loud_on_a_metrics_dict_that_lost_its_objective():
    """The forbidden fix, pinned. Defaulting ``ontime_breach`` would make a plan
    with no objective at all score BETTER than every real plan."""
    with pytest.raises(KeyError):
        optimizer.score({"makespan_days": 10.0})


def test_scoreable_is_the_gate_score_needs():
    assert optimizer.scoreable(_metrics())
    assert not optimizer.scoreable({})            # OptimizeResult.best's default
    assert not optimizer.scoreable(None)
    assert not optimizer.scoreable({"score": 1200})        # a progress heartbeat
    assert not optimizer.scoreable({"makespan_days": 10.0})
    # ...and it demands exactly what score reads, so the two cannot drift.
    for key in optimizer.SCORE_REQUIRED_KEYS:
        partial = _metrics()
        partial.pop(key)
        assert not optimizer.scoreable(partial)
        with pytest.raises(KeyError):
            optimizer.score(partial)


# =========================================================================== #
# 3. The contest: an empty candidate can never be elected winner
# =========================================================================== #

def _payload(scheduler="cp"):
    raw = build_new_sample_bytes()
    so_lines, _m = load_all(io.BytesIO(raw))
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    cfg = Config(scheduler=scheduler, plan_start_date=PLAN_START,
                 apply_operator_logic=True,
                 **({"overlap_percent": 60} if scheduler != "cp" else {}))
    cfg.validate()
    return raw, orders, cfg, osvc.build_payload(
        orders, [], raw, cfg, seed=1,
        candidates=list(osvc.cloud_candidates(cfg)), budget_per_candidate=1)


def _row(best, overlap=None, **kw):
    row = {"overlap": overlap, "flexible": False, "seed": 1, "eligible": True,
           "best": best, "evals": 1, "ranks": {}, "genome": {}, "crew_rank": {},
           "cancelled": False}
    row.update(kw)
    return row


def test_pick_winner_never_elects_a_candidate_that_found_nothing():
    """The single-row case is the live one: with one row the old code skipped the
    score comparison entirely (`best is None` short-circuits the first row in), so
    the empty dict was elected in silence and only blew up at finalize."""
    assert osvc.pick_winner(None, False, [_row({})], base_seed=1) is None


def test_pick_winner_scores_the_real_candidates_around_an_empty_one():
    """Two rows: the old code reached ``score({})`` inside the comparison itself."""
    rows = [_row({}, overlap=60), _row(_metrics(ontime_breach=99.0), overlap=70),
            _row(_metrics(ontime_breach=1.0), overlap=80)]
    win = osvc.pick_winner(60, False, rows, base_seed=1)      # no KeyError
    assert win is not None and win["overlap"] == 80


def test_merge_shard_rows_reports_no_plan_rather_than_electing_an_empty_one():
    _raw, _orders, _cfg, payload = _payload("cp")
    merged = osvc.merge_shard_rows(payload, [_row({})], 1, False)
    assert merged["best"] is None
    # The rows are still published for the panel — only the WINNER is refused.
    assert len(merged["rows"]) == 1


# =========================================================================== #
# 4. The whole way home: a worker posting a CP result the app cannot apply
# =========================================================================== #

def _api(monkeypatch, scheduler="cp"):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", SECRET)
    monkeypatch.setenv("DEFAULT_SCHEDULER", scheduler)
    import api.main as m
    importlib.reload(m)
    return m


def _seed_running(m, payload, cfg, *, job_id="job-1", auto=False):
    """A running CLOUD job, in the state the watchdog leaves it in: the baseline
    is already measured (``cloud_job`` does that before dispatching), which is
    exactly what made the live failure reach ``score``."""
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="running", job_id=job_id, cloud_payload=payload,
                           base_config=cfg, baseline=_metrics(), label="deep",
                           cancel=False, cloud_failed=False, claimed=False,
                           auto=auto, shards={}, shard_total=None,
                           shards_finalizing=False, shard_evals={}, evals=0,
                           best=None, started_mono=0.0)


def _store_book(raw, orders, cfg):
    book_store.save_masters_bytes(raw)
    book_store.save_plan_config(json.dumps(cfg.to_dict()))
    book_store.add_orders(list(orders.values()))


def _post_shards(c, rows, *, job_id="job-1", total=20):
    """All 20 shards of the GitHub matrix. A cp contest is ONE job, so shard 0
    carries the only row and the other 19 report nothing — the real shape."""
    for idx in range(total):
        r = c.post("/optimize/shard-result", headers=HDR, json={
            "job_id": job_id, "shard_index": idx, "shard_total": total,
            "rows": rows if idx == 0 else [], "evals": 1 if idx == 0 else 0,
            "cancelled": False})
        assert r.status_code == 200, r.text


def test_a_cp_solve_that_found_nothing_ends_in_a_SENTENCE(monkeypatch):
    """THE LIVE BUG. Before the fix this raised KeyError('ontime_breach') out of
    the shard collector and the owner read it verbatim."""
    m = _api(monkeypatch)
    raw, orders, cfg, payload = _payload("cp")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    _post_shards(TestClient(m.app), [_row({})])

    err = m._OPTIMIZE["error"] or ""
    assert "ontime_breach" not in err and "KeyError" not in err, (
        f"a raw Python error reached the owner: {err!r}")
    assert m._OPTIMIZE["state"] == "failed"
    assert "unchanged" in err and "deep search" in err.lower()
    # ...and no local fallback is armed: the app server has no solver, so the
    # watchdog would only report the WRONG cause ("worker not reachable").
    assert m._OPTIMIZE.get("cloud_failed") is False


def test_the_unusable_result_leaves_the_applied_plan_alone(monkeypatch):
    """"Your current plan is unchanged" has to be TRUE, not just printed."""
    m = _api(monkeypatch)
    raw, orders, cfg, payload = _payload("cp")
    _store_book(raw, orders, cfg)
    book_store.save_plan_priority({"KEEP\x1fME": 1}, {"saved_at": "x"})
    book_store.save_cp_genome({"cp_overlap_of": {"OLD": 1}})
    _seed_running(m, payload, cfg)
    _post_shards(TestClient(m.app), [_row({})])
    assert (book_store.load_plan_priority() or {}).get("ranks") == {"KEEP\x1fME": 1}
    assert book_store.load_cp_genome() == {"cp_overlap_of": {"OLD": 1}}


def test_the_done_button_leaves_a_durable_note_when_the_search_finds_nothing(monkeypatch):
    """2026-08-09's rule: a Done click must never end in silence. The panel's
    error lives in process memory; the floor only ever sees the note."""
    m = _api(monkeypatch)
    raw, orders, cfg, payload = _payload("cp")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg, auto=True)
    _post_shards(TestClient(m.app), [_row({})])
    note = (book_store.load_auto_note() or {}).get("text", "")
    assert "unchanged" in note and "ontime_breach" not in note


def test_a_real_cp_result_still_finalizes_through_the_shards(monkeypatch):
    """The fix must not make every CP contest look empty."""
    m = _api(monkeypatch)
    raw, orders, cfg, payload = _payload("cp")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    good = _row(_metrics(ontime_breach=1.0), genome={"cp_overlap_of": {"B1": 1}})
    _post_shards(TestClient(m.app), [good])
    assert m._OPTIMIZE["state"] == "done", m._OPTIMIZE["error"]
    assert m._OPTIMIZE["result"]["genome"] == {"cp_overlap_of": {"B1": 1}}


def test_finalize_itself_never_scores_something_that_is_not_a_plan(monkeypatch):
    """The crash site, driven directly. ``_finalize_optimize`` is the LAST step of
    every contest and it is shared by every engine, so it holds the line even if a
    future producer (a new worker, a merge from an older one) gets past the
    contest's own gate. Belt AND braces, both asserted."""
    m = _api(monkeypatch)
    raw, orders, cfg, _payload_ = _payload("cp")
    _store_book(raw, orders, cfg)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="J9", state="running", started_mono=0.0,
                           auto=False, result=None)
    stored = m._finalize_optimize("J9", cfg, _metrics(), "deep",
                                  winner_overlap=None, ranks={}, best={},
                                  evals=1, table=[], cancelled=False)
    assert stored is True
    res = m._OPTIMIZE["result"]
    # Normalised to the ONE spelling of "no plan", so `_auto_apply_result`'s
    # `if not best` and `_optimize_apply`'s `res["best"] or {}` both read it right.
    assert res["best"] is None and res["improved"] is False


def test_finalize_survives_a_baseline_that_cannot_be_ranked(monkeypatch):
    """The other side of the comparison gets the same test. The contest-start
    baseline is only REPLACED by a fresh measurement when that measurement
    succeeds; when it raises (a store blip, a book that will not plan) the
    contest-start dict stands, and a partially populated one — a worker's progress
    heartbeat posts ``{"score": N}`` — must not take the whole contest down with
    it. Nothing is claimed as an improvement that could not be measured."""
    m = _api(monkeypatch)
    raw, orders, cfg, _payload_ = _payload("cp")
    _store_book(raw, orders, cfg)

    def _boom(**kw):
        raise RuntimeError("cannot measure the incumbent right now")

    monkeypatch.setattr(m, "_incumbent_metrics", _boom)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="J8", state="running", started_mono=0.0,
                           auto=False, result=None)
    stored = m._finalize_optimize("J8", cfg, {"score": 1200}, "deep",
                                  winner_overlap=None, ranks={},
                                  best=_metrics(ontime_breach=0.0),
                                  evals=1, table=[], cancelled=False)
    assert stored is True                      # no KeyError on the baseline
    assert m._OPTIMIZE["result"]["improved"] is False


def test_the_classic_sweep_skips_a_contender_that_produced_no_plan(monkeypatch):
    """The retired engines' own contest has the identical gate, and it was written
    with the identical wrong test. A contender that found nothing must be skipped,
    not scored — and must never win by default."""
    from engine.models import SOLine
    raw, so_lines, masters, _cfg = _cp_book()
    cfg = Config(scheduler="classic", plan_start_date=PLAN_START, overlap_percent=60)
    calls = []

    def fake_optimize(lines, config, mst, **kw):
        calls.append(config.overlap_percent)
        if config.overlap_percent == 60:              # the current setting: nothing
            return optimizer.OptimizeResult(evals=1)
        return optimizer.OptimizeResult(ranks={"a": 1}, best=_metrics(ontime_breach=2.0),
                                        evals=1)

    monkeypatch.setattr(optimizer, "optimize", fake_optimize)
    sw = optimizer.sweep_optimize(so_lines, cfg, masters, budget_evals=8,
                                  candidates=(60, 80))
    assert calls, "the sweep never ran a contender"
    assert sw.overlap_percent == 80, "an empty contender won the contest"
    assert optimizer.scoreable(sw.result.best)


# =========================================================================== #
# 5. The unsharded (Oracle) worker posts to /optimize/result — same class
# =========================================================================== #

def test_an_unsolved_cp_result_on_the_legacy_endpoint_ends_in_the_same_sentence(monkeypatch):
    m = _api(monkeypatch)
    raw, orders, cfg, payload = _payload("cp")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    r = TestClient(m.app).post("/optimize/result", headers=HDR, json={
        "job_id": "job-1", "winner_overlap": None, "ranks": {}, "best": {},
        "rows": [_row({})], "evals": 1, "cancelled": False})
    assert r.status_code == 200 and r.json().get("fallback") is None
    assert m._OPTIMIZE["state"] == "failed"
    assert "ontime_breach" not in (m._OPTIMIZE["error"] or "")


def test_a_GOOD_cp_result_is_not_thrown_away_for_having_no_knob(monkeypatch):
    """Under cp there is no knob, so a winning result reports ``winner_overlap``
    None — and the endpoint used to read that as "no result" and burn the
    watchdog on a local fallback that cannot solve. Only an engine that HAS a
    knob may be required to report a winning value."""
    m = _api(monkeypatch)
    raw, orders, cfg, payload = _payload("cp")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    r = TestClient(m.app).post("/optimize/result", headers=HDR, json={
        "job_id": "job-1", "winner_overlap": None, "ranks": {"SO1\x1fI1": 1},
        "best": _metrics(ontime_breach=1.0), "rows": [_row(_metrics())],
        "evals": 1, "cancelled": False,
        "genome": {"cp_overlap_of": {"B1": 1}}})
    assert r.status_code == 200
    assert m._OPTIMIZE["state"] == "done", m._OPTIMIZE["error"]
    assert m._OPTIMIZE["result"]["genome"] == {"cp_overlap_of": {"B1": 1}}


# =========================================================================== #
# 6. Every other engine is untouched — they share this finalize path
# =========================================================================== #

def test_a_knobbed_engine_still_falls_back_to_local_when_nothing_came_home(monkeypatch):
    """classic/flow/new/roster CAN re-run the contest in process, so for them an
    empty result must still arm the watchdog's local fallback — unchanged."""
    m = _api(monkeypatch, scheduler="classic")
    raw, orders, cfg, payload = _payload("classic")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    _post_shards(TestClient(m.app), [_row({}, overlap=60)])
    assert m._OPTIMIZE["state"] == "running"      # still the watchdog's job
    assert m._OPTIMIZE["cloud_failed"] is True
    assert "ontime_breach" not in (m._OPTIMIZE["error"] or "")


def test_a_knobbed_engine_still_needs_its_winning_value(monkeypatch):
    m = _api(monkeypatch, scheduler="classic")
    raw, orders, cfg, payload = _payload("classic")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    r = TestClient(m.app).post("/optimize/result", headers=HDR, json={
        "job_id": "job-1", "winner_overlap": None, "ranks": {},
        "best": _metrics(), "rows": [], "evals": 1, "cancelled": False})
    assert r.status_code == 200 and r.json().get("fallback") == "local"
    assert m._OPTIMIZE["cloud_failed"] is True


def test_a_worker_error_still_reports_the_workers_own_message(monkeypatch):
    m = _api(monkeypatch, scheduler="classic")
    raw, orders, cfg, payload = _payload("classic")
    _store_book(raw, orders, cfg)
    _seed_running(m, payload, cfg)
    r = TestClient(m.app).post("/optimize/result", headers=HDR, json={
        "job_id": "job-1", "error": "worker exploded"})
    assert r.json().get("fallback") == "local"
    assert m._OPTIMIZE["error"] == "worker exploded"
