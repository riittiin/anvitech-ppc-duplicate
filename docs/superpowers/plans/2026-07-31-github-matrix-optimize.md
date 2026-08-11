# GitHub Actions Matrix Fan-out for the Optimize Contest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fan the cloud Optimize contest's independent (overlap × machine-set) candidates across ~20 free parallel GitHub-hosted runners, each posting its results straight back to the app over the existing authenticated channel, so a deep contest finishes in ~15 min with no order data ever touching public GitHub storage.

**Architecture:** The contest is already a list of independent `(overlap, flexible)` candidates that `run_contest` fans across local CPU cores. This plan (1) extracts three pure helpers — `contest_jobs` (the ordered candidate list), `_run_jobs` (run a set of candidates), `merge_shard_rows` (reduce rows → one winner via the existing `pick_winner`) — and rebuilds `run_contest` from them with byte-identical output; (2) adds `run_contest_slice` that runs one round-robin slice of the candidate list; (3) teaches the worker to run its slice and POST to a new `/optimize/shard-result`; (4) adds an app-side collector that accumulates shard rows and finalizes once all arrive (or partially at the watchdog); (5) turns the workflow into a 20-way matrix. When `SHARD_TOTAL` is unset/1 the whole legacy path (Oracle box + manual E2E) stays byte-identical.

**Tech Stack:** Python 3, FastAPI, pytest, GitHub Actions (matrix, `strategy.job-index`/`strategy.job-total`), stdlib-only worker.

## Global Constraints

- **No order data crosses into GitHub.** Only the random uuid `job_id` is a workflow input. Payload + results travel only over authenticated HTTPS (`X-Worker-Secret`). NO GitHub artifacts. The worker prints only counts and `job_id` — never payload, orders, rows, or ranks.
- **Legacy whole-contest path is byte-identical** when `SHARD_TOTAL` is unset or `1`: the worker calls `run_contest` and posts `/optimize/result` exactly as today (Oracle-box path + `GITHUB_DISPATCH_TOKEN=manual` E2E unchanged).
- **`run_contest` output is byte-identical after the refactor** — existing optimize tests and the golden trace must stay green.
- **`pick_winner` runs ONCE over the global merged row set** (not per shard), so its "current (overlap, machine-set) wins an exact tie" privilege stays correct.
- **Partial-safe:** at the watchdog deadline, ≥1 shard reported → finalize over arrived rows; 0 shards → existing local fallback. A missing shard only means its candidates went unsearched.
- **New `/optimize/shard-result` uses the same worker-secret auth + gatekeeper-bypass allowlist** as the other worker endpoints; 403 without the secret; a stale/duplicate/late shard is a 200 no-op, never a crash.
- **Depth is an operational knob, not code:** per-candidate plan count stays `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` (Render env). `ORACLE_CLAIM_TIMEOUT_MIN=0` is a Render setting, documented, not code.
- Worker stays **stdlib-only**.

Key existing signatures this plan builds on (verified in code):
- `engine/optimize_service.run_candidate(payload, overlap, flexible=False, *, on_progress=None, should_cancel=None) -> {"overlap","flexible","eligible","best","evals","ranks","cancelled"}`
- `engine/optimize_service.pick_winner(current_overlap, current_flexible, rows) -> row|None`
- `engine/optimize_service.run_contest(payload, *, processes=1, on_progress=None, should_cancel=None, poll_seconds=5.0) -> {"winner_overlap","winner_flexible","rows","knob","best","ranks","evals","cancelled"}`
- `engine.optimizer.knob_for(config) -> (knob_name, default_candidates)`, `optimizer.sweep_contenders(cur_value, candidates) -> list[int]`, `optimizer.score(metrics) -> float`
- `api/main._finalize_optimize(job_id, base_config, real_baseline, label, *, winner_overlap, winner_flexible=False, ranks, best, evals, table, cancelled) -> stored(bool)`
- `_OPTIMIZE` dict + `_OPTIMIZE_LOCK` at `api/main.py:932`; worker allowlist at `api/main.py:119-122`; `WorkerProgress`/`WorkerResult` at `api/main.py:2295-2310`; `optimize_progress_ep` at `:2340`; `optimize_result_ep` at `:2354`; `cloud_job` watchdog block at `:1382-1409`.

---

### Task 1: Extract `contest_jobs`, `_run_jobs`, `merge_shard_rows`; rebuild `run_contest` byte-identically

**Files:**
- Modify: `engine/optimize_service.py` (refactor `run_contest` at `:334-402`; add three module-level functions above it)
- Test: `tests/test_optimize_shard.py` (new)

**Interfaces:**
- Consumes: `optimizer.knob_for`, `optimizer.sweep_contenders`, `pick_winner`, `run_candidate`, `_pool_init`, `_pool_run`, `Config.from_dict` (all already in the module).
- Produces:
  - `contest_jobs(payload: dict) -> list[tuple[int, bool]]` — the ordered `(overlap, flexible)` candidate list.
  - `_run_jobs(payload, pairs, *, processes=1, on_progress=None, should_cancel=None, poll_seconds=5.0) -> tuple[list[dict], int, bool]` — returns `(rows, done_evals, cancelled)`; `rows` are full `run_candidate` dicts (with `ranks`).
  - `merge_shard_rows(payload, rows, evals, cancelled) -> dict` — same shape `run_contest` returns.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_optimize_shard.py`:

```python
"""Sharded-contest helpers: contest_jobs order, merge equivalence, and (Task 2)
run_contest_slice union-equivalence to a whole run_contest."""
import base64

from engine import optimize_service as osvc
from engine.config import Config


def _payload(loaded, config, *, budget=6, candidates=(60, 80)):
    """A cloud payload for the sample book at a tiny budget (fast tests)."""
    so_lines, masters = loaded
    orders = {sl.key_str: osvc_order(sl) for sl in so_lines} if False else None
    # Build via the real builder so the payload shape matches production exactly.
    import io
    from tests.sample_workbook import build_sample_bytes
    masters_bytes = build_sample_bytes()
    # Minimal orders/actuals snapshot the builder expects:
    from engine import book_store
    orders_json = {sl.key_str: sl.order_json() for sl in so_lines} if hasattr(so_lines[0], "order_json") else None
    return osvc.build_payload(
        orders=_orders_snapshot(so_lines), actuals=[], masters_bytes=masters_bytes,
        config=config, seed=1, candidates=candidates, budget_per_candidate=budget)


def _orders_snapshot(so_lines):
    """The {key_str: order-dict} snapshot build_payload consumes (mirror how
    api._start_optimize builds it — one Order per (so,item))."""
    from engine.models import Order
    out = {}
    for sl in so_lines:
        o = Order(so_no=sl.so_no, item_code=sl.item_code, qty=sl.qty,
                  due_date=sl.due_date)
        out[o.key_str] = o.to_json()
    return out


def test_contest_jobs_order_matches_run_contest(loaded, config):
    cfg = Config.from_dict({**config.to_dict(), "scheduler": "new"})
    payload = _payload(loaded, cfg)
    jobs = osvc.contest_jobs(payload)
    # new engine → two machine-sets × the contenders, flex-outer/overlap-inner.
    from engine import optimizer
    contenders = optimizer.sweep_contenders(getattr(cfg, optimizer.knob_for(cfg)[0]),
                                            payload["candidates"])
    expected = [(ov, flex) for flex in (False, True) for ov in contenders]
    assert jobs == expected


def test_contest_jobs_classic_single_machineset(loaded, config):
    payload = _payload(loaded, config)  # config fixture is scheduler="classic"
    jobs = osvc.contest_jobs(payload)
    assert all(flex is False for _ov, flex in jobs)


def test_merge_shard_rows_equivalent_to_run_contest(loaded, config):
    cfg = Config.from_dict({**config.to_dict(), "scheduler": "new"})
    payload = _payload(loaded, cfg)
    full = osvc.run_contest(payload, processes=1)
    # Reproduce the rows the contest computed, then merge them ourselves:
    rows = [osvc.run_candidate(payload, ov, flex) for ov, flex in osvc.contest_jobs(payload)]
    merged = osvc.merge_shard_rows(payload, rows,
                                   sum(r["evals"] for r in rows),
                                   any(r["cancelled"] for r in rows))
    assert merged["winner_overlap"] == full["winner_overlap"]
    assert merged["winner_flexible"] == full["winner_flexible"]
    assert merged["ranks"] == full["ranks"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_optimize_shard.py -q`
Expected: FAIL — `AttributeError: module 'engine.optimize_service' has no attribute 'contest_jobs'`.

- [ ] **Step 3: Add the three helpers and rebuild `run_contest`**

In `engine/optimize_service.py`, ADD above `run_contest` (after `_pool_run`):

```python
def contest_jobs(payload: dict) -> list:
    """The ordered (overlap, flexible) candidate list a contest evaluates — the
    SINGLE source of truth for run_contest AND the sharded worker, so they can
    never drift. Order: machine-set outer, overlap inner (matches run_contest)."""
    config = Config.from_dict(payload["config"])
    knob, _ = optimizer.knob_for(config)
    cur_value = getattr(config, knob)
    contenders = optimizer.sweep_contenders(cur_value, payload["candidates"])
    machine_sets = (False, True) if getattr(config, "scheduler", "classic") == "new" else (False,)
    return [(ov, flex) for flex in machine_sets for ov in contenders]


def _run_jobs(payload: dict, pairs: list, *, processes=1, on_progress=None,
              should_cancel=None, poll_seconds=5.0):
    """Run a list of (overlap, flexible) candidates. Returns (rows, done_evals,
    cancelled). processes>1 fans them across subprocesses (shared progress
    counter); processes<=1 runs them sequentially in-process."""
    rows, done_evals, cancelled = [], 0, False
    if processes <= 1:
        for ov, flex in pairs:
            if should_cancel and should_cancel():
                cancelled = True
                break
            base = done_evals

            def cb(evals, best, _base=base):
                if on_progress:
                    on_progress(_base + evals, best)

            row = run_candidate(payload, ov, flex, on_progress=cb, should_cancel=should_cancel)
            rows.append(row)
            done_evals += row.get("evals", 0)
            cancelled = cancelled or bool(row.get("cancelled"))
    else:
        import multiprocessing as mp
        ctx = mp.get_context()
        counter = ctx.Value("i", 0)
        stop = ctx.Value("b", 0)
        jobs = [(payload, ov, flex) for ov, flex in pairs]
        with ctx.Pool(processes=processes, initializer=_pool_init,
                      initargs=(counter, stop)) as pool:
            async_res = pool.map_async(_pool_run, jobs)
            while not async_res.ready():
                async_res.wait(poll_seconds)
                if on_progress:
                    on_progress(counter.value, None)
                if should_cancel and should_cancel():
                    stop.value = 1
            rows = async_res.get()
        done_evals = sum(r.get("evals", 0) for r in rows)
        cancelled = bool(stop.value) or any(r.get("cancelled") for r in rows)
    return rows, done_evals, cancelled


def merge_shard_rows(payload: dict, rows: list, evals: int, cancelled: bool) -> dict:
    """Reduce a set of run_candidate rows (any set of shards, or a whole
    contest) into the single result dict the app finalizes. pick_winner runs
    ONCE over the global row set. Same shape run_contest returns."""
    config = Config.from_dict(payload["config"])
    knob, _ = optimizer.knob_for(config)
    cur_value = getattr(config, knob)
    cur_flex = bool(getattr(config, "flexible_machines", False))
    winner = pick_winner(cur_value, cur_flex, rows)
    table = [{k: r[k] for k in ("overlap", "flexible", "eligible", "best", "evals")
              if k in r} for r in rows]
    if winner is None:
        return {"winner_overlap": cur_value, "winner_flexible": cur_flex, "rows": table,
                "knob": knob, "best": None, "ranks": {}, "evals": evals,
                "cancelled": cancelled}
    return {"winner_overlap": winner["overlap"], "winner_flexible": bool(winner["flexible"]),
            "rows": table, "knob": knob, "best": winner["best"],
            "ranks": winner.get("ranks", {}), "evals": evals, "cancelled": cancelled}
```

Then REPLACE the body of `run_contest` (`:340-402`) with:

```python
    pairs = contest_jobs(payload)
    rows, done_evals, cancelled = _run_jobs(
        payload, pairs, processes=processes, on_progress=on_progress,
        should_cancel=should_cancel, poll_seconds=poll_seconds)
    if on_progress:
        on_progress(done_evals, None)
    return merge_shard_rows(payload, rows, done_evals, cancelled)
```

Keep the `run_contest` signature and docstring. `contest_jobs`'s `machine_sets` gate and `_run_jobs`'s loops reproduce the original exactly.

- [ ] **Step 4: Run the new tests + the existing optimize/golden suites**

Run: `python3 -m pytest tests/test_optimize_shard.py tests/test_optimize_service.py tests/test_optimize_endpoints.py -q -k "golden or contest or shard or optimize"`
Then: `python3 -m pytest tests/test_optimize_service.py tests/test_new_engine.py -q`
Expected: PASS — new tests green AND run_contest behavior unchanged.

- [ ] **Step 5: Commit**

```bash
git add engine/optimize_service.py tests/test_optimize_shard.py
git commit -m "refactor(optimize): extract contest_jobs/_run_jobs/merge_shard_rows; run_contest rebuilt byte-identical"
```

---

### Task 2: `run_contest_slice` — the per-shard compute unit

**Files:**
- Modify: `engine/optimize_service.py` (add `run_contest_slice` after `run_contest`)
- Test: `tests/test_optimize_shard.py` (extend)

**Interfaces:**
- Consumes: `contest_jobs`, `_run_jobs` (Task 1).
- Produces: `run_contest_slice(payload, shard_index: int, shard_total: int, *, processes=1, on_progress=None, should_cancel=None, poll_seconds=5.0) -> {"rows": list[dict], "evals": int, "cancelled": bool}` — runs `contest_jobs(payload)[shard_index::shard_total]` (round-robin) and returns the raw rows (WITH `ranks`), summed evals, and OR-ed cancelled. NOT a picked winner — the app merges.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_optimize_shard.py`:

```python
def test_slices_union_equals_full_contest(loaded, config):
    cfg = Config.from_dict({**config.to_dict(), "scheduler": "new"})
    payload = _payload(loaded, cfg)
    full = osvc.run_contest(payload, processes=1)
    SHARD_TOTAL = 5
    all_rows, all_evals, any_cancel = [], 0, False
    seen_pairs = []
    for idx in range(SHARD_TOTAL):
        out = osvc.run_contest_slice(payload, idx, SHARD_TOTAL, processes=1)
        all_rows.extend(out["rows"])
        all_evals += out["evals"]
        any_cancel = any_cancel or out["cancelled"]
        seen_pairs.extend((r["overlap"], r["flexible"]) for r in out["rows"])
    # every candidate covered exactly once, no overlap
    assert sorted(seen_pairs) == sorted(osvc.contest_jobs(payload))
    merged = osvc.merge_shard_rows(payload, all_rows, all_evals, any_cancel)
    assert merged["winner_overlap"] == full["winner_overlap"]
    assert merged["winner_flexible"] == full["winner_flexible"]
    assert merged["ranks"] == full["ranks"]


def test_slice_more_shards_than_candidates_is_safe(loaded, config):
    payload = _payload(loaded, config, candidates=(60,))  # classic → 1 candidate
    # 4 shards, 1 candidate: shard 0 does it, shards 1-3 are empty (no crash).
    outs = [osvc.run_contest_slice(payload, i, 4, processes=1) for i in range(4)]
    non_empty = [o for o in outs if o["rows"]]
    assert len(non_empty) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_optimize_shard.py::test_slices_union_equals_full_contest -q`
Expected: FAIL — `AttributeError: ... has no attribute 'run_contest_slice'`.

- [ ] **Step 3: Implement `run_contest_slice`**

Add to `engine/optimize_service.py` after `run_contest`:

```python
def run_contest_slice(payload: dict, shard_index: int, shard_total: int, *,
                      processes=1, on_progress=None, should_cancel=None,
                      poll_seconds=5.0) -> dict:
    """One shard of the contest: run the round-robin slice
    contest_jobs(payload)[shard_index::shard_total] and return its RAW rows
    (with ranks) for the app to merge. shard_total<=1 runs every candidate."""
    pairs = contest_jobs(payload)
    if shard_total and shard_total > 1:
        pairs = pairs[shard_index::shard_total]
    rows, done_evals, cancelled = _run_jobs(
        payload, pairs, processes=processes, on_progress=on_progress,
        should_cancel=should_cancel, poll_seconds=poll_seconds)
    return {"rows": rows, "evals": done_evals, "cancelled": cancelled}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_optimize_shard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/optimize_service.py tests/test_optimize_shard.py
git commit -m "feat(optimize): run_contest_slice — round-robin shard, union-equivalent to run_contest"
```

---

### Task 3: Teach the worker to run a shard and post to `/optimize/shard-result`

**Files:**
- Modify: `scripts/cloud_optimize_worker.py`
- Test: `tests/test_cloud_worker_shard.py` (new — unit-test the env parsing + branch selection via monkeypatched `_call`)

**Interfaces:**
- Consumes: `optimize_service.run_contest_slice`, `run_contest`.
- Produces: `_shard_env() -> tuple[int, int]` (returns `(shard_index, shard_total)` from env; defaults `(0, 1)`); `main()` posts `/optimize/shard-result {job_id, shard_index, shard_total, rows, evals, cancelled}` when `shard_total > 1`, else legacy `/optimize/result`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cloud_worker_shard.py`:

```python
"""The cloud worker's shard branch: with SHARD_TOTAL>1 it runs a slice and posts
to /optimize/shard-result; unset/1 keeps the legacy /optimize/result path. We
drive main() with a fake _call so no network/app is needed."""
import importlib
import sys

import pytest


def _load_worker(monkeypatch, tmp_path, shard_index=None, shard_total=None):
    monkeypatch.setenv("APP_URL", "http://app.test")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s")
    monkeypatch.setenv("JOB_ID", "job-xyz")
    if shard_index is not None:
        monkeypatch.setenv("SHARD_INDEX", str(shard_index))
    if shard_total is not None:
        monkeypatch.setenv("SHARD_TOTAL", str(shard_total))
    sys.modules.pop("scripts.cloud_optimize_worker", None)
    return importlib.import_module("scripts.cloud_optimize_worker")


def _fake_payload():
    # Minimal payload contest_jobs/run_contest_slice can consume for the sample book.
    import io
    from tests.sample_workbook import build_sample_bytes
    from engine.loaders import load_all
    from engine.config import Config
    from engine import optimize_service as osvc
    from engine.models import Order
    so_lines, _m = load_all(io.BytesIO(build_sample_bytes()))
    orders = {}
    for sl in so_lines:
        o = Order(so_no=sl.so_no, item_code=sl.item_code, qty=sl.qty, due_date=sl.due_date)
        orders[o.key_str] = o.to_json()
    return osvc.build_payload(orders=orders, actuals=[],
                             masters_bytes=build_sample_bytes(),
                             config=Config(scheduler="classic"), seed=1,
                             candidates=(60, 80), budget_per_candidate=4)


def test_worker_shard_posts_shard_result(monkeypatch, tmp_path):
    w = _load_worker(monkeypatch, tmp_path, shard_index=0, shard_total=2)
    posts = []
    payload = _fake_payload()

    def fake_call(method, path, body=None, **kw):
        if path.startswith("/optimize/job/"):
            return {"payload": payload, "cancel": False}
        posts.append((path, body))
        return {"ok": True, "cancel": False}

    monkeypatch.setattr(w, "_call", fake_call)
    assert w.main() == 0
    result_posts = [p for p in posts if p[0] == "/optimize/shard-result"]
    assert len(result_posts) == 1
    body = result_posts[0][1]
    assert body["job_id"] == "job-xyz" and body["shard_index"] == 0
    assert body["shard_total"] == 2 and isinstance(body["rows"], list)
    # a 2-shard slice of 2 classic candidates → exactly 1 candidate this shard
    assert len(body["rows"]) == 1


def test_worker_no_shard_uses_legacy_result(monkeypatch, tmp_path):
    w = _load_worker(monkeypatch, tmp_path)  # no SHARD_* env → legacy
    posts = []
    payload = _fake_payload()

    def fake_call(method, path, body=None, **kw):
        if path.startswith("/optimize/job/"):
            return {"payload": payload, "cancel": False}
        posts.append((path, body))
        return {"ok": True, "cancel": False}

    monkeypatch.setattr(w, "_call", fake_call)
    assert w.main() == 0
    assert any(p[0] == "/optimize/result" for p in posts)
    assert not any(p[0] == "/optimize/shard-result" for p in posts)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_cloud_worker_shard.py -q`
Expected: FAIL — the worker always posts `/optimize/result` (no shard branch yet).

- [ ] **Step 3: Implement the shard branch**

In `scripts/cloud_optimize_worker.py`, ADD after the `JOB_ID = ...` line (near `:30`):

```python
def _shard_env():
    """(shard_index, shard_total) from the matrix; (0, 1) = whole contest."""
    try:
        idx = int(os.environ.get("SHARD_INDEX", "0"))
    except ValueError:
        idx = 0
    try:
        total = int(os.environ.get("SHARD_TOTAL", "1"))
    except ValueError:
        total = 1
    return idx, max(1, total)
```

In `main()`, REPLACE the `try:` compute-and-post block (`:94-106`) with:

```python
    shard_index, shard_total = _shard_env()
    threading.Thread(target=poster, daemon=True).start()
    try:
        if shard_total > 1:
            out = optimize_service.run_contest_slice(
                payload, shard_index, shard_total, processes=n_procs,
                on_progress=_on_prog, should_cancel=lambda: state["cancel"])
            state["done"] = True
            _call("POST", "/optimize/shard-result", {
                "job_id": JOB_ID, "shard_index": shard_index,
                "shard_total": shard_total, "rows": out["rows"],
                "evals": out["evals"], "cancelled": out["cancelled"]})
            print(f"worker: shard {shard_index}/{shard_total} done — "
                  f"{len(out['rows'])} candidates, {out['evals']} plans", flush=True)
            return 0
        out = optimize_service.run_contest(
            payload, processes=n_procs, on_progress=_on_prog,
            should_cancel=lambda: state["cancel"])
        state["done"] = True
        _call("POST", "/optimize/result", {
            "job_id": JOB_ID, "winner_overlap": out["winner_overlap"],
            "winner_flexible": out.get("winner_flexible", False),
            "ranks": out["ranks"], "best": out["best"], "rows": out["rows"],
            "evals": out["evals"], "cancelled": out["cancelled"]})
        print(f"worker: done — winner overlap {out['winner_overlap']}, "
              f"{out['evals']} plans", flush=True)
        return 0
```

Then in the progress `poster()`, include the shard index so the app can sum per-shard. CHANGE the `body = {...}` line inside `poster` (`:78`) to:

```python
                body = {"job_id": JOB_ID, "evals": state["evals"],
                        "shard_index": _shard_env()[0]}
```

Leave the `except Exception` error-post block posting to `/optimize/result` for the legacy path; for a SHARD failure, post an errored shard-result instead. REPLACE the `except` body (`:107-115`) with:

```python
    except Exception as e:  # noqa: BLE001 — tell the app so it can finalize/fall back
        state["done"] = True
        print(f"worker: FAILED: {e}", flush=True)  # never prints order data
        try:
            if shard_total > 1:
                _call("POST", "/optimize/shard-result",
                      {"job_id": JOB_ID, "shard_index": shard_index,
                       "shard_total": shard_total, "rows": [], "evals": 0,
                       "cancelled": False, "error": str(e)[:500]},
                      tries=2, timeout=30)
            else:
                _call("POST", "/optimize/result",
                      {"job_id": JOB_ID, "error": str(e)[:500]}, tries=2, timeout=30)
        except Exception:
            pass                          # the app's watchdog still covers us
        return 1
```

Note: the print statements already emit only counts and job_id — do NOT add payload/rows to any print.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_cloud_worker_shard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/cloud_optimize_worker.py tests/test_cloud_worker_shard.py
git commit -m "feat(worker): run a contest shard and post /optimize/shard-result; legacy path unchanged"
```

---

### Task 4: App-side collector — `POST /optimize/shard-result` + finalize-when-all-arrived

**Files:**
- Modify: `api/main.py` — worker allowlist (`:119-122`); `_OPTIMIZE` init (`:932-937`); the per-job reset in `_start_optimize` (the `_OPTIMIZE.update(...)` that sets `state="running"`, `job_id`, `claimed=False` — around `:1264-1271`); add `WorkerShardResult` model + `_finalize_from_shards` helper + `POST /optimize/shard-result` endpoint (near the other worker endpoints, after `optimize_result_ep` at `:2377`)
- Test: `tests/test_shard_result_api.py` (new)

**Interfaces:**
- Consumes: `optimize_service.merge_shard_rows`, `_finalize_optimize`, `_OPTIMIZE`, `_OPTIMIZE_LOCK`, `_require_worker`.
- Produces: `POST /optimize/shard-result`; `_finalize_from_shards(job_id) -> None` (merges accumulated shards → `_finalize_optimize`, or sets `cloud_failed` when no eligible winner). New `_OPTIMIZE` keys: `"shards"` (dict `int -> {"rows","evals","cancelled"}`), `"shard_total"` (int|None).

- [ ] **Step 1: Write the failing test**

Create `tests/test_shard_result_api.py`:

```python
"""POST /optimize/shard-result: worker-secret auth, accumulation, and
finalize-when-all-arrived == a single whole-contest run_contest winner."""
import io

import pytest
from fastapi.testclient import TestClient

from engine.loaders import load_all
from engine.config import Config
from engine.models import Order
from engine import optimize_service as osvc
from tests.sample_workbook import build_sample_bytes


def _payload():
    so_lines, _m = load_all(io.BytesIO(build_sample_bytes()))
    orders = {}
    for sl in so_lines:
        o = Order(so_no=sl.so_no, item_code=sl.item_code, qty=sl.qty, due_date=sl.due_date)
        orders[o.key_str] = o.to_json()
    return osvc.build_payload(orders=orders, actuals=[], masters_bytes=build_sample_bytes(),
                             config=Config(scheduler="new"), seed=1,
                             candidates=(60, 80), budget_per_candidate=4)


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
    assert res["overlap"] == full["winner_overlap"]
    assert res.get("flexible") == full["winner_flexible"]


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
```

> Note for the implementer: `_OPTIMIZE["result"]` shape (what `res["overlap"]`/`res["flexible"]` read) is produced by `_finalize_optimize`; confirm those keys against `_finalize_optimize`'s `result` assignment and adjust the asserts to the real key names if they differ (e.g. `winner_overlap`). Do NOT change `_finalize_optimize`'s output shape.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_shard_result_api.py -q`
Expected: FAIL — 404/405 (endpoint does not exist).

- [ ] **Step 3: Implement the endpoint, model, helper, allowlist + reset**

3a. Allowlist — in the gatekeeper (`api/main.py:121-122`), add the new path:

```python
        or (method == "POST" and path in ("/optimize/progress",
                                          "/optimize/result",
                                          "/optimize/shard-result"))):
```

3b. `_OPTIMIZE` init (`:932-937`) — add two keys:

```python
             "claimed": False, "shards": {}, "shard_total": None}
```

3c. Per-job reset — in `_start_optimize`, wherever it sets the running job (the `_OPTIMIZE.update(...)` with `state="running"`, `job_id=...`, `claimed=False`), ALSO set `shards={}, shard_total=None`. (Belt-and-suspenders: a fresh job must never see a prior job's shards.)

3d. Model + endpoint + helper — add after `optimize_result_ep` (`:2377`):

```python
class WorkerShardResult(BaseModel):
    job_id: str
    shard_index: int = 0
    shard_total: int = 1
    rows: list = Field(default_factory=list)
    evals: int = 0
    cancelled: bool = False
    error: Optional[str] = None


def _finalize_from_shards(job_id):
    """Merge every accumulated shard's rows and finalize the job — or set
    cloud_failed when the merged set has no eligible winner. Caller holds no
    lock; this takes it. Safe to call from the collector (all-arrived) and the
    watchdog (partial)."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
            return
        payload = _OPTIMIZE.get("cloud_payload")
        shards = list(_OPTIMIZE.get("shards", {}).values())
        base_config = _OPTIMIZE.get("base_config")
        baseline = _OPTIMIZE.get("baseline")
        label = _OPTIMIZE.get("label")
    all_rows = [r for s in shards for r in s.get("rows", [])]
    total_evals = sum(int(s.get("evals", 0)) for s in shards)
    any_cancel = any(bool(s.get("cancelled")) for s in shards)
    merged = optimize_service.merge_shard_rows(payload, all_rows, total_evals, any_cancel)
    if merged["best"] is None:
        with _OPTIMIZE_LOCK:
            if _OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id:
                _OPTIMIZE["cloud_failed"] = True   # watchdog → local fallback
                _OPTIMIZE["error"] = "no eligible plan from any shard"
        return
    _finalize_optimize(job_id, base_config, baseline, label,
                       winner_overlap=merged["winner_overlap"],
                       winner_flexible=bool(merged["winner_flexible"]),
                       ranks=merged["ranks"], best=merged["best"],
                       evals=merged["evals"], table=merged["rows"],
                       cancelled=merged["cancelled"])


@app.post("/optimize/shard-result")
def optimize_shard_result_ep(req: WorkerShardResult, request: Request):
    """One matrix shard's rows. Accumulate; when all shards for this job have
    reported, merge and finalize. A stale/late/duplicate shard is a 200 no-op."""
    _require_worker(request)
    ready = False
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == req.job_id:
            _OPTIMIZE["shard_total"] = req.shard_total
            _OPTIMIZE["shards"][req.shard_index] = {
                "rows": req.rows, "evals": req.evals, "cancelled": req.cancelled}
            ready = len(_OPTIMIZE["shards"]) >= req.shard_total
    if ready:
        _finalize_from_shards(req.job_id)
    return {"ok": True}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_shard_result_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_shard_result_api.py
git commit -m "feat(api): /optimize/shard-result collector — merge shards and finalize when all arrive"
```

---

### Task 5: Sum progress across shards

**Files:**
- Modify: `api/main.py` — `WorkerProgress` model (`:2295`), `optimize_progress_ep` (`:2340`)
- Test: `tests/test_shard_result_api.py` (extend)

**Interfaces:**
- Consumes: `_OPTIMIZE`, `_OPTIMIZE_LOCK`.
- Produces: `WorkerProgress` gains `shard_index: Optional[int] = None`; when present, the endpoint tracks per-shard evals in `_OPTIMIZE["shard_evals"]` and reports their SUM; legacy (no shard_index) behavior unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_shard_result_api.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_shard_result_api.py::test_progress_sums_across_shards -q`
Expected: FAIL — legacy `max` logic reports 10, then 10, then 25 (not summed).

- [ ] **Step 3: Implement per-shard summing**

3a. Add to `_OPTIMIZE` init (Task 4 already touched this line) — add `"shard_evals": {}`. Also reset it in the per-job reset alongside `shards`.

3b. `WorkerProgress` (`:2295-2298`) — add a field:

```python
class WorkerProgress(BaseModel):
    job_id: str
    evals: int = 0
    best: Optional[dict] = None
    shard_index: Optional[int] = None
```

3c. `optimize_progress_ep` (`:2344-2349`) — REPLACE the body inside the `if _OPTIMIZE["state"] == "running" ...` with:

```python
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == req.job_id:
            if req.shard_index is None:
                _OPTIMIZE["evals"] = max(int(req.evals), _OPTIMIZE["evals"])
            else:
                _OPTIMIZE["shard_evals"][req.shard_index] = int(req.evals)
                _OPTIMIZE["evals"] = sum(_OPTIMIZE["shard_evals"].values())
            if req.best:
                _OPTIMIZE["best"] = req.best
            return {"cancel": bool(_OPTIMIZE["cancel"])}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_shard_result_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_shard_result_api.py
git commit -m "feat(api): sum optimize progress across shards (per-shard evals buckets)"
```

---

### Task 6: Partial-safe watchdog finalize

**Files:**
- Modify: `api/main.py` — `cloud_job`'s timed-out block (`:1390-1408`)
- Test: `tests/test_shard_result_api.py` (extend — call the finalize helper directly with a partial shard set)

**Interfaces:**
- Consumes: `_finalize_from_shards` (Task 4), `_OPTIMIZE`.
- Produces: at the watchdog deadline, if `_OPTIMIZE["shards"]` is non-empty, finalize over the arrived shards instead of local fallback; if empty, the existing local fallback runs.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_shard_result_api.py`:

```python
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
    assert m._OPTIMIZE["result"]["overlap"] in shard0_overlaps
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_shard_result_api.py::test_partial_finalize_uses_arrived_shards -q`
Expected: The helper already exists (Task 4), so this may PASS for the helper — but the WATCHDOG wiring does not yet call it. If it passes, still do Step 3 (the watchdog wiring is the deliverable) and add the wiring assertion below.

Add this second test that exercises the watchdog path decision directly:

```python
def test_watchdog_prefers_shards_over_local(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    out0 = osvc.run_contest_slice(payload, 0, 2, processes=1)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["shard_total"] = 2
        m._OPTIMIZE["shards"][0] = {"rows": out0["rows"], "evals": out0["evals"],
                                     "cancelled": out0["cancelled"]}
    # _shards_available(job_id) reports there is salvageable work → no local burn.
    assert m._shards_available("job-1") is True
```

- [ ] **Step 3: Wire the watchdog to prefer arrived shards**

3a. Add a tiny predicate near `_finalize_from_shards`:

```python
def _shards_available(job_id) -> bool:
    with _OPTIMIZE_LOCK:
        return (_OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id
                and bool(_OPTIMIZE.get("shards")))
```

3b. In `cloud_job`, inside the `if timed_out:` handling (`:1393-1408`), BEFORE the `_OPTIMIZE["mode"] = "local"` fallback branch and AFTER the `was_cancelled` short-circuit, insert a shard-salvage check. REPLACE:

```python
                    if timed_out:
                        if was_cancelled:
                            _OPTIMIZE.update(state="failed", cancel=False,
                                             error="stopped: the cloud run did not "
                                                   "answer before the timeout")
                            return
                        _OPTIMIZE["mode"] = "local"
                        _k, _kc = optimizer.knob_for(setup.search_config)
                        _mult = 2 if getattr(setup.search_config, "scheduler",
                                              "classic") == "new" else 1
                        _OPTIMIZE["budget_evals"] = _mult * optimizer.sweep_total_evals(
                            budget_evals, getattr(setup.search_config, _k), _kc)
                        _OPTIMIZE["evals"] = 0
                if timed_out:
                    local_job()          # cloud never answered → compute here
                    return
```

with:

```python
                    if timed_out:
                        if was_cancelled:
                            _OPTIMIZE.update(state="failed", cancel=False,
                                             error="stopped: the cloud run did not "
                                                   "answer before the timeout")
                            return
                        have_shards = bool(_OPTIMIZE.get("shards"))
                        if not have_shards:
                            _OPTIMIZE["mode"] = "local"
                            _k, _kc = optimizer.knob_for(setup.search_config)
                            _mult = 2 if getattr(setup.search_config, "scheduler",
                                                 "classic") == "new" else 1
                            _OPTIMIZE["budget_evals"] = _mult * optimizer.sweep_total_evals(
                                budget_evals, getattr(setup.search_config, _k), _kc)
                            _OPTIMIZE["evals"] = 0
                if timed_out:
                    if _shards_available(job_id):
                        _finalize_from_shards(job_id)   # salvage what arrived
                    else:
                        local_job()                     # cloud never answered → local
                    return
```

(`_finalize_from_shards` sets `cloud_failed` if the arrived shards have no eligible winner; a follow-up watchdog tick would then fall to local — acceptable and rare.)

- [ ] **Step 4: Run tests + the optimize endpoint suite**

Run: `python3 -m pytest tests/test_shard_result_api.py tests/test_optimize_endpoints.py tests/test_oracle_e2e.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_shard_result_api.py
git commit -m "feat(api): watchdog finalizes over arrived shards before local fallback"
```

---

### Task 7: Matrix workflow + manual-seam E2E + full suite

**Files:**
- Modify: `.github/workflows/optimize.yml`
- Test: `tests/test_matrix_e2e.py` (new — drives the sharded worker over the manual seam against a local app)

**Interfaces:**
- Consumes: everything above.
- Produces: a 20-way matrix workflow; each job runs the worker with `SHARD_INDEX=${{ strategy.job-index }}` / `SHARD_TOTAL=${{ strategy.job-total }}`.

- [ ] **Step 1: Write the failing E2E test**

Create `tests/test_matrix_e2e.py`:

```python
"""End-to-end (no GitHub, no network): run the sharded worker in-process for
SHARD_TOTAL shards against a TestClient app, and assert the app finalizes to the
same winner an unsharded run_contest produces. Mirrors the manual-seam E2E."""
import io

import pytest
from fastapi.testclient import TestClient

from engine.loaders import load_all
from engine.config import Config
from engine.models import Order
from engine import optimize_service as osvc
from tests.sample_workbook import build_sample_bytes


def _payload():
    so_lines, _m = load_all(io.BytesIO(build_sample_bytes()))
    orders = {}
    for sl in so_lines:
        o = Order(so_no=sl.so_no, item_code=sl.item_code, qty=sl.qty, due_date=sl.due_date)
        orders[o.key_str] = o.to_json()
    return osvc.build_payload(orders=orders, actuals=[], masters_bytes=build_sample_bytes(),
                             config=Config(scheduler="new"), seed=1,
                             candidates=(60, 80), budget_per_candidate=4)


def test_matrix_shards_finalize_like_run_contest(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    full = osvc.run_contest(payload, processes=1)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="running", job_id="job-1", cloud_payload=payload,
                           base_config=Config.from_dict(payload["config"]),
                           baseline=None, label="deep", cancel=False,
                           cloud_failed=False, claimed=False, shards={},
                           shard_evals={}, shard_total=None, evals=0, best=None,
                           started_mono=0.0)
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    SHARD_TOTAL = 4
    for idx in range(SHARD_TOTAL):
        out = osvc.run_contest_slice(payload, idx, SHARD_TOTAL, processes=1)
        c.post("/optimize/shard-result", headers=hdr, json={
            "job_id": "job-1", "shard_index": idx, "shard_total": SHARD_TOTAL,
            "rows": out["rows"], "evals": out["evals"], "cancelled": out["cancelled"]})
    assert m._OPTIMIZE["state"] == "done"
    assert m._OPTIMIZE["result"]["overlap"] == full["winner_overlap"]
```

- [ ] **Step 2: Run to verify it passes on the code so far**

Run: `python3 -m pytest tests/test_matrix_e2e.py -q`
Expected: PASS (this proves Tasks 1-6 compose). If it fails, fix the composition before touching the workflow.

- [ ] **Step 3: Convert the workflow to a matrix**

Edit `.github/workflows/optimize.yml`. Keep `on.workflow_dispatch` with the `job_id` input and `concurrency: optimize`. Change the single job into a matrix:

```yaml
jobs:
  optimize:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    strategy:
      fail-fast: false
      max-parallel: 20
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19]
    steps:
      # ... keep the existing checkout / setup-python / pip install steps ...
      - name: Run optimize shard
        env:
          APP_URL: ${{ secrets.APP_URL }}
          OPTIMIZE_WORKER_SECRET: ${{ secrets.OPTIMIZE_WORKER_SECRET }}
          JOB_ID: ${{ inputs.job_id }}
          SHARD_INDEX: ${{ strategy.job-index }}
          SHARD_TOTAL: ${{ strategy.job-total }}
        run: python scripts/cloud_optimize_worker.py
```

Notes for the implementer:
- Preserve the existing checkout / `setup-python` / `pip install -r requirements.txt` steps verbatim; only the job now has a `strategy.matrix` and the run step gains `SHARD_INDEX`/`SHARD_TOTAL`.
- `strategy.job-index` is the 0-based job position; `strategy.job-total` is the matrix size — so `SHARD_TOTAL` self-derives from the matrix length (no drift if the list is later resized).
- The `matrix.shard` list values are unused by the worker (it reads job-index/job-total); the list only needs 20 entries to create 20 jobs.
- `fail-fast: false` is REQUIRED — one shard dying must not cancel the others.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS (the whole suite, including golden + legacy optimize + oracle E2E, stays green).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/optimize.yml tests/test_matrix_e2e.py
git commit -m "feat(ci): fan the optimize contest across a 20-way GitHub Actions matrix"
```

---

## Post-merge (operational — owner + verify, NOT code)

After the branch merges and Render deploys latest commit:
1. Owner sets on Render: `ORACLE_CLAIM_TIMEOUT_MIN=0` (dispatch GitHub immediately — no Oracle box) and `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` (start ~700; tune after measuring).
2. Run one real Test8 deep end-to-end (press Deep Search), watch the 20 matrix jobs in the repo's Actions tab, confirm the app applies the winner, measure wall-clock, and adjust `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` to land ~12-15 min.
3. Confirm no order data appears in any public Actions log or artifact (there should be zero artifacts, and logs show only counts + job_id).

---

## Self-Review

**1. Spec coverage:**
- Sharded worker (SHARD_INDEX/SHARD_TOTAL, round-robin, posts to app) → Tasks 2, 3. ✅
- `contest_jobs` shared helper → Task 1. ✅
- App-side `/optimize/shard-result` collector + global `pick_winner` merge → Task 4. ✅
- Per-shard progress summing → Task 5. ✅
- Partial-safe watchdog finalize → Task 6. ✅
- Matrix workflow (job-index/job-total, fail-fast false, max-parallel 20) → Task 7. ✅
- Legacy whole-contest path byte-identical when SHARD_TOTAL unset/1 → Tasks 1 (run_contest unchanged), 3 (worker legacy branch), verified by existing suites + oracle E2E in Tasks 6-7. ✅
- No order data on public GitHub (only job_id; no artifacts; worker prints counts only) → Task 3 (prints) + Task 7 (workflow passes only job_id + shard indices) + post-merge check. ✅
- Equivalence + manual-seam E2E tests → Tasks 1, 2, 4, 7. ✅
- Depth as env knob; ORACLE_CLAIM_TIMEOUT_MIN=0 → Post-merge section (operational, not code) per spec §5-6. ✅

**2. Placeholder scan:** No TBD/TODO; every code + test step is concrete. The one "confirm the real key names" note (Task 4 Step 1) is a guard against a shape mismatch in a test assertion, with explicit instruction not to change production shape — not a placeholder in production code.

**3. Type consistency:** `contest_jobs -> list[(overlap, flexible)]` consumed identically by `_run_jobs`, `run_contest`, `run_contest_slice`. `run_contest_slice -> {"rows","evals","cancelled"}` consumed by the worker (Task 3) and the collector test (Task 4). `merge_shard_rows(payload, rows, evals, cancelled) -> {winner_overlap, winner_flexible, rows, knob, best, ranks, evals, cancelled}` consumed by `run_contest` (Task 1) and `_finalize_from_shards` (Task 4). `WorkerShardResult` fields match the worker's POST body (Task 3) and the endpoint (Task 4). `_finalize_from_shards`/`_shards_available` defined in Task 4/6 and consumed by the watchdog (Task 6). Consistent.
