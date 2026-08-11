"""Feedback trigger (spec 2026-07-22): the auto contest starts from POST
/optimize/done — the 'Done entering — update plan' button, available to BOTH
roles. It starts an auto-applying contest unless auto is disabled, one is already
running, or nothing material changed since the last applied plan (book + inputs
fingerprint). Unlike the removed Mon/Fri cron it is NOT cloud-only. Admin
mutations (upload, commit, delete, /run persist) still never start a contest on
their own. AUTO_OPTIMIZE=0 (internal test isolation only) disables everything."""
import time
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from engine import book_store, optimizer
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


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


def _auto_env(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    # Oracle claim window (2026-08-01): 0 = dispatch immediately, preserving
    # this file's existing fast-fallback-to-local timing.
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", "0")


# --------------------------------------------------------------------------- #
# POST /optimize/done — the feedback-driven trigger (both roles)
# --------------------------------------------------------------------------- #
def test_done_starts_contest_when_book_changed(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda budget_evals, label, background=True, auto=False:
                        starts.append((label, auto)))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is True
    assert starts == [("auto", True)]


def test_done_reachable_by_user_role(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda budget_evals, label, background=True, auto=False:
                        starts.append((label, auto)))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech_user",
                           "password": "anvitech12345678"})
    r = c.post("/optimize/done")
    assert r.status_code == 200            # NOT 403 — user role may trigger it
    assert r.json()["started"] is True
    assert starts == [("auto", True)]


def test_done_requires_login(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    c = TestClient(m.app)
    assert c.post("/optimize/done").status_code == 401


def test_done_skips_and_notes_when_nothing_changed(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    book_store.save_plan_priority({}, {"saved_at": "t",
                                       "book_sig": m._current_book_sig(),
                                       "inputs_sig": m._inputs_signature(cfg),
                                       # A contest is run against a plan START as well
                                       # as a book and settings; a record without it
                                       # cannot be known to match (see
                                       # tests/test_replan_on_date_advance.py).
                                       "plan_start": m._current_plan_start_sig()})
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert starts == []
    # Wording changed 2026-08-09 (the note now names who pressed and what to do);
    # what is pinned here is that the skip is EXPLAINED, never silent.
    _note = (book_store.load_auto_note() or {}).get("text", "")
    assert "nothing new to re-plan" in _note and "unchanged" in _note


def test_done_skips_when_last_searched_matches_even_without_applied_plan(monkeypatch):
    """No applied plan_priority at all, but a prior contest already SEARCHED
    this exact book+inputs (e.g. it found nothing worth applying) — a
    redundant Done click must still be skipped, not re-run the full contest."""
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    book_store.save_last_searched({"book_sig": m._current_book_sig(),
                                   "inputs_sig": m._inputs_signature(cfg),
                                   "plan_start": m._current_plan_start_sig()})
    assert book_store.load_plan_priority() is None
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert starts == []
    # Wording changed 2026-08-09 (the note now names who pressed and what to do);
    # what is pinned here is that the skip is EXPLAINED, never silent.
    _note = (book_store.load_auto_note() or {}).get("text", "")
    assert "nothing new to re-plan" in _note and "unchanged" in _note


def test_done_disabled_by_internal_env(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert starts == []


def test_done_no_op_when_contest_already_running(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "running"
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert starts == []


def test_admin_mutations_do_not_start_contests(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    monkeypatch.setattr(m, "COMMITMENT_FEATURE_ENABLED", True)   # lanes are hidden by default
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})

    r = c.post("/orders/commit", json={"orders": [["SO2", ITEM_B]]})
    assert r.status_code == 200 and not starts

    r = c.post("/upload", files={"file": ("sample.xlsx", build_sample_bytes(),
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200 and not starts

    r = c.post("/orders/uncommit", json={"orders": [["SO2", ITEM_B]]})
    assert r.status_code == 200 and not starts

    r = c.post("/run", json={"config": {}, "persist": True})
    assert r.status_code == 200 and not starts

    r = c.post("/orders/delete", json={"orders": [["SO1", ITEM_A]], "password": "1930rail"})
    assert r.status_code == 200 and not starts

    r = c.post("/orders/clear", json={"password": "1930rail"})
    assert r.status_code == 200 and not starts


def test_run_still_surfaces_the_auto_note(monkeypatch):
    """/run keeps reporting whatever note the last scheduled contest left —
    it just doesn't trigger a new one."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    book_store.save_auto_note({"text": "hello", "at": "t"})
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/run", json={})
    assert r.json()["auto_note"]["text"] == "hello"


# --------------------------------------------------------------------------- #
# No weekday gate (2026-07-29): the restricted optimize runs every day —
# only "already running" / "nothing changed" skip it.
# --------------------------------------------------------------------------- #
def test_done_runs_on_non_thursday_too(monkeypatch):
    """The Thursday-only gate is removed: /optimize/done starts the
    restricted re-optimization on ANY weekday when the book changed."""
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    monkeypatch.setattr(m, "_ist_today", lambda: date(2026, 7, 27))  # a Monday
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda budget_evals, label, background=True, auto=False:
                        starts.append((label, auto)))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    body = r.json()
    assert body["reason"] != "not_optimize_day"
    assert body["started"] is True
    assert starts == [("auto", True)]


def test_done_fires_optimize_on_thursday(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda budget_evals, label, background=True, auto=False:
                        starts.append((label, auto)))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is True
    assert r.json()["reason"] == "started"
    assert starts == [("auto", True)]


# --------------------------------------------------------------------------- #
# End-to-end contest behavior (unchanged machinery, just entered differently)
# --------------------------------------------------------------------------- #
def test_auto_contest_applies_only_when_strictly_better(monkeypatch):
    _auto_env(monkeypatch)
    # "manual" dispatch never calls a real worker; force a fast fallback to
    # local compute so this real contest finishes inside the test's patience.
    monkeypatch.setenv("OPTIMIZE_CLOUD_TIMEOUT_MIN", "0.01")
    m = _api()
    _seed_book()
    m._try_start_auto()                                 # real contest, sample book
    t0 = time.time()
    while m._optimize_status()["state"] == "running" and time.time() - t0 < 60:
        time.sleep(0.05)
    # The auto-apply hook runs just after the contest lands in "done" — and,
    # when it applies, the hook itself moves state on to "idle" again — so
    # give the note/priority writes a brief grace window before asserting.
    note = book_store.load_auto_note()
    t0 = time.time()
    while note is None and time.time() - t0 < 5:
        time.sleep(0.05)
        note = book_store.load_auto_note()
    assert note is not None
    saved = book_store.load_plan_priority()
    if saved:                                          # applied ⇒ strictly better + meta
        assert "auto" in note["text"].lower() or "re-optimized" in note["text"]
        assert saved["meta"]["book_sig"] == m._current_book_sig()
    else:                                               # not applied ⇒ honest note
        assert "still best" in note["text"]


def test_manual_apply_also_records_book_sig(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    m._start_optimize(budget_evals=15, label="deep", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority()["meta"]["book_sig"] == m._current_book_sig()


def test_optimize_status_carries_auto_field(monkeypatch):
    """/optimize/status must expose whether the current/last run was an
    auto-triggered contest, so the UI can suppress a stale Apply panel."""
    m = _api()
    _seed_book()
    st = m._optimize_status()
    assert "auto" in st
    assert st["auto"] is False
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["auto"] = True
    assert m._optimize_status()["auto"] is True


def test_auto_apply_keeps_current_when_no_plan(monkeypatch):
    """When the contest finishes with best=None (e.g. cancelled/empty),
    _auto_apply_result writes a 'kept the current plan' note and leaves
    plan_priority unchanged."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    # Save a known plan priority
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    # Stub the contest result: best=None
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["result"] = {"best": None}
        m._OPTIMIZE["auto"] = True
    # Call _auto_apply_result
    m._auto_apply_result()
    # Check: note reflects "no plan"
    note = book_store.load_auto_note()
    assert note is not None
    assert "no plan" in note["text"]
    # Check: plan_priority unchanged
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks


def test_auto_apply_keeps_current_when_worst_order_would_regress(monkeypatch):
    """The worst-order backstop (owner non-negotiable: the worst order must
    NEVER get later) — a contest result with a strictly BETTER score is still
    REJECTED if it would push the single worst order later than the
    incumbent's worst. Directly exercises the `elif not worst_ok` branch of
    _auto_apply_result, which otherwise has zero test coverage."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    # A known applied plan — must remain untouched if the backstop holds.
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    # Incumbent: worst order 46 days late (high total, to leave headroom for
    # `best` to score strictly better while still regressing the worst order).
    # `ontime_breach` is what score() reads since 2026-08-06; `total_late_days` is
    # still reported and is what the auto-note text asserts on. Both are needed:
    # the first makes the score premise true, the second makes the note assertion true.
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "ontime_breach": 500.0,
                                  "max_late_days": 46})
    # Contest best: strictly better score (far fewer total late-days) but its
    # worst order is pushed to 61 days late — the backstop must reject it.
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "ontime_breach": 100.0,
                                          "max_late_days": 61},
                                 "ranks": {"k": 2}}
        m._OPTIMIZE["auto"] = True
    # Sanity check on the premise this test isolates: score is strictly
    # better on its own — the backstop is the ONLY reason the plan is kept.
    assert (optimizer.score({"total_late_days": 100, "makespan_days": 50.0, "ontime_breach": 100.0}) <
            optimizer.score({"total_late_days": 500, "makespan_days": 50.0, "ontime_breach": 500.0}))

    m._auto_apply_result()

    note = book_store.load_auto_note()
    assert note is not None
    assert "protect the worst" in note["text"]
    assert "15" in note["text"]              # regression amount: 61 - 46

    # Not applied: the previously-saved ranks are untouched.
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks


# --------------------------------------------------------------------------- #
# IST display (server runs UTC; the notes reference a named local clock time)
# --------------------------------------------------------------------------- #
def test_auto_note_timestamp_is_ist(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    before = datetime.utcnow() + timedelta(hours=5, minutes=30)
    m._auto_note_write("a test note")
    after = datetime.utcnow() + timedelta(hours=5, minutes=30)
    note = book_store.load_auto_note()
    at = datetime.fromisoformat(note["at"])
    assert before - timedelta(seconds=5) <= at <= after + timedelta(seconds=5)


def test_auto_apply_result_stamp_is_ist(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api(); _seed_book()
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["result"] = {"best": None}
        m._OPTIMIZE["auto"] = True
    before = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%H:%M")
    m._auto_apply_result()
    after = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%H:%M")
    # "no plan" branch doesn't stamp a time, so drive the "still best" branch too:
    book_store.save_plan_priority({"k": 1}, {"saved_at": "t"})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 999,
                                          "makespan_days": 999,
                                          "ontime_breach": 999.0},
                                 "ranks": {},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}
        m._OPTIMIZE["auto"] = True
    m._auto_apply_result()
    note = book_store.load_auto_note()
    assert before in note["text"] or after in note["text"]
