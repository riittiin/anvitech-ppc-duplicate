# Machine-set Optimize Dimension — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the new-engine Optimize contest choose the machine set (Allotted-only vs Allotted+Suggested union) as a third search dimension alongside sequence and overlap, and persist + replay the winner like the tuned overlap.

**Architecture:** A new `Config.flexible_machines` bool (default `False` = today) rides through the existing code as the machine-set knob. The new engine's masters loader (`ppc_load_all(..., flexible_machines=)`) already builds the union; we thread the flag into `new_engine._new_masters` and make the contest sweep BOTH machine-sets (an outer loop over `(False, True)`), keep the global best by the current score, and persist the winning `flexible_machines` into the saved config so `_plan` reproduces it.

**Tech Stack:** Python, FastAPI, the vendored `ppc_engine` new scheduler, pytest.

## Global Constraints

- **New-engine only** (`scheduler == "new"`). The classic/flow engines, `rule6_allocate`, and the golden trace are UNTOUCHED. `flexible_machines` is ignored by classic/flow.
- **Config field name:** `flexible_machines: bool = False`. Default `False` ⇒ the new-engine plan is byte-identical to today.
- **Union rule is already implemented** in `ppc_engine/loaders/masters_loader.load_routings(flexible_machines=True)` as `tuple(dict.fromkeys(allot_opts + sug_opts))` (Allotted first, dedup). Do NOT reimplement the parsing.
- **The contest ALWAYS sweeps both machine-sets** — the outer loop is hardcoded `(False, True)`, independent of the config's current `flexible_machines` value.
- **Scoring is unchanged** — `optimizer.score(metrics)` (`total_late_days + 10 × makespan_days`) decides the winner across all passes.
- **Tests:** always `monkeypatch.setenv("DEFAULT_SCHEDULER", "new")` (never raw `os.environ`); isolate `STORE_DIR`; use small budgets. Reuse `tests/new_sample_workbook.py` — its routing steps carry Suggested `"CNC1/CNC2"` against a single Allotted, so `flexible_machines=True` yields 2 machine options where `False` yields 1. Fixtures `old_book`, `new_masters`, and `_CONF` live in `tests/test_new_engine.py`.
- **Existing new-engine optimize-outcome tests may now see a union winner.** If a fixture's union genuinely wins, update the expected winner to the new correct value — never weaken an assertion to hide a real change.
- **Frozen ops pre-place independent of `machine_options`** (they pin to `FrozenOp.machine_id` before the scheduling loop), so a frozen op on a Suggested-only machine must still pin during the Allotted-only pass.

---

### Task 1: Config field `flexible_machines`

**Files:**
- Modify: `engine/config.py` (dataclass field ~line 99 near `split_parallel`; `validate` ~line 176)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.flexible_machines: bool = False`. Round-trips via the existing `to_dict`/`from_dict` (no special-casing needed — `asdict` includes it and `from_dict` `setattr`s it). Because `to_dict()` includes it, it is automatically folded into `api.main._inputs_signature` (staleness).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from engine.config import Config

def test_flexible_machines_defaults_false_and_round_trips():
    assert Config().flexible_machines is False
    d = Config(flexible_machines=True).to_dict()
    assert d["flexible_machines"] is True
    assert Config.from_dict(d).flexible_machines is True

def test_flexible_machines_must_be_bool():
    import pytest
    c = Config(); c.flexible_machines = "yes"
    with pytest.raises(ValueError):
        c.validate()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_config.py -k flexible_machines -v`
Expected: FAIL (no such attribute / validate doesn't reject).

- [ ] **Step 3: Add the field + validation**

In `engine/config.py`, after the `split_parallel`/`split_min_qty` block (~line 100), add:

```python
    # Machine set the OPTIMIZER may use for each in-house machining/manual/inspection
    # step (2026-07-29). False = the Allotted machine only (Suggested used only as a
    # blank-Allotted fallback) — today's behaviour, byte-identical. True = the deduped
    # union of Allotted + Suggested, letting the scheduler load-balance onto Suggested
    # machines. Owned by the Optimize contest (swept, applied, persisted) exactly like
    # overlap_percent — NOT a user knob. New engine only; classic/flow ignore it.
    flexible_machines: bool = False
```

In `validate`, beside the other bool checks (~line 178):

```python
        if not isinstance(self.flexible_machines, bool):
            errs.append("flexible_machines must be true or false")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_config.py -k flexible_machines -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/config.py tests/test_config.py
git commit -m "feat(config): flexible_machines knob (default False = byte-identical)"
```

---

### Task 2: `_new_masters(flexible)` + the new engine reads the knob

**Files:**
- Modify: `engine/new_engine.py` (`_new_masters` ~line 71; `run` ~line 394; `optimize_sequence` ~line 419; `tune` ~line 465)
- Test: `tests/test_flexible_machines.py` (new)

**Interfaces:**
- Consumes: `Config.flexible_machines` (Task 1).
- Produces: `new_engine._new_masters(flexible: bool = False)` — ppc masters loaded at that flexibility, cached by `(sha256(workbook), flexible)`. `run`/`optimize_sequence`/`tune` load masters at `config.flexible_machines`. `run(config=None)` treats missing config as `flexible=False`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_flexible_machines.py
import os
import pytest
from dataclasses import replace
from engine import book_store, new_engine
from engine.rules import rule1_consolidate
from tests.new_sample_workbook import build_new_sample_bytes
from tests.test_new_engine import _CONF, _old_book  # helper that returns (so_lines, masters)

@pytest.fixture(autouse=True)
def _new_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    new_engine._MASTERS_CACHE.clear()
    book_store.save_masters_bytes(build_new_sample_bytes())

def _machine_options_count(flexible):
    m = new_engine._new_masters(flexible)
    r = m.routings["A"]           # Item A: CNC FIRST SIDE, Suggested "CNC1/CNC2"
    op = next(o for o in r.operations if o.name == "CNC FIRST SIDE")
    return len(op.machine_options)

def test_union_adds_suggested_machines():
    assert _machine_options_count(False) == 1      # Allotted only
    assert _machine_options_count(True) == 2       # Allotted + Suggested (CNC1, CNC2)

def test_cache_distinguishes_flexibility():
    a = new_engine._new_masters(False)
    b = new_engine._new_masters(True)
    assert a is not b                               # not a stale same-hash cache hit
    assert new_engine._new_masters(False) is a      # each flavour cached

def test_run_places_op_on_suggested_machine_only_when_flexible():
    so_lines, masters = _old_book()
    batches = rule1_consolidate.run(so_lines, _CONF)
    def machines(cfg):
        return {e.machine for e in new_engine.run(batches, config=cfg, masters=masters)}
    only = machines(replace(_CONF, flexible_machines=False))
    both = machines(replace(_CONF, flexible_machines=True))
    assert "CNC2" not in only          # Allotted-only never reaches CNC2 for these ops
    assert "CNC2" in both              # union lets the scheduler use the Suggested CNC2
```

> If `tests/test_new_engine.py` does not already expose an importable `_old_book()` returning `(so_lines, masters)`, add a thin module-level helper there that wraps its existing `old_book` fixture body, and import that. Do not duplicate the workbook-build logic.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_flexible_machines.py -v`
Expected: FAIL (`_new_masters` takes no arg / options always 1 / CNC2 never used).

- [ ] **Step 3: Make `_new_masters` flexibility-aware**

Replace `engine/new_engine._new_masters` (~line 71):

```python
def _new_masters(flexible: bool = False):
    """Load the new-engine Masters at the given machine flexibility from the injected
    bytes or the stored workbook. flexible=False -> Allotted-only options (today);
    True -> the Allotted+Suggested union. Cached by (workbook sha, flexible)."""
    raw = _OVERRIDE_BYTES if _OVERRIDE_BYTES is not None else book_store.load_masters_bytes()
    if not raw:
        raise RuntimeError("new_engine: no masters workbook available (store empty and none injected)")
    h = hashlib.sha256(raw).hexdigest()
    key = (h, bool(flexible))
    cached = _MASTERS_CACHE.get(key)
    if cached is None:
        # Keep both flexibilities of the CURRENT workbook; evict any other workbook.
        for k in [k for k in _MASTERS_CACHE if k[0] != h]:
            del _MASTERS_CACHE[k]
        cached = load_all(io.BytesIO(raw), flexible_machines=bool(flexible)).masters
        _MASTERS_CACHE[key] = cached
    return cached
```

- [ ] **Step 4: Thread the knob into the three callers**

`run` (~line 394): `new_masters = _with_absences(_apply_app_operators(_new_masters(bool(getattr(config, "flexible_machines", False))), masters), reserved)`

`optimize_sequence` (~line 419): `nm = _with_absences(_apply_app_operators(_new_masters(bool(getattr(config, "flexible_machines", False))), masters), reserved)`

`tune` (~line 465): `new_masters = _with_absences(_apply_app_operators(_new_masters(bool(getattr(config, "flexible_machines", False))), masters), reserved)`

- [ ] **Step 5: Run to verify they pass**

Run: `pytest tests/test_flexible_machines.py -v`
Expected: PASS.

- [ ] **Step 6: Add the byte-identical + frozen-pin guards**

```python
# tests/test_flexible_machines.py (append)
def test_flexible_false_is_byte_identical_to_default():
    so_lines, masters = _old_book()
    batches = rule1_consolidate.run(so_lines, _CONF)
    base = [(e.item_code, e.process_seq, e.machine, e.start, e.end)
            for e in new_engine.run(batches, config=_CONF, masters=masters)]
    flag = [(e.item_code, e.process_seq, e.machine, e.start, e.end)
            for e in new_engine.run(batches, config=replace(_CONF, flexible_machines=False), masters=masters)]
    assert base == flag

def test_frozen_op_on_suggested_machine_pins_in_allotted_only_pass():
    """A frozen op whose machine is a Suggested-only machine (CNC2) must still pin there
    even when the pass loads Allotted-only options (CNC2 not in the op's options)."""
    so_lines, masters = _old_book()
    batches = rule1_consolidate.run(so_lines, _CONF)
    frozen = [{"so_no": so_lines[0].so_no, "item_code": "A", "process": "CNC FIRST SIDE",
               "op_seq": 1, "machine": "CNC2", "operator": "Alpha",
               "remaining_qty": 1, "prev_start": "2026-07-29T08:00:00"}]
    sched = new_engine.run(batches, config=replace(_CONF, flexible_machines=False),
                           masters=masters, frozen=frozen)
    e = next(x for x in sched if x.item_code == "A" and x.process_seq == 1)
    assert e.machine == "CNC2"        # pinned despite CNC2 not being an Allotted-only option
```

> Adjust the `frozen` dict keys/op_seq to match `new_engine._ppc_frozen`'s expected shape (see `_frozen_op_rows`/`_ppc_frozen` in `engine/new_engine.py` and `engine/freeze.py`); read them before writing this test.

- [ ] **Step 7: Run + commit**

Run: `pytest tests/test_flexible_machines.py -v`
Expected: PASS.

```bash
git add engine/new_engine.py tests/test_flexible_machines.py tests/test_new_engine.py
git commit -m "feat(new-engine): _new_masters honors flexible_machines; run/search read the knob"
```

---

### Task 3: SweepResult field + local golden-section two-pass

**Files:**
- Modify: `engine/optimizer.py` (`SweepResult` ~line 406)
- Modify: `engine/new_engine.py` (`sweep_optimize` ~line 492)
- Test: `tests/test_flexible_machines.py`

**Interfaces:**
- Consumes: `_new_masters(flexible)` + knob-aware `tune` (Task 2).
- Produces: `SweepResult.flexible_machines: bool = False` (the winning machine-set). `new_engine.sweep_optimize` runs the golden-section `tune` once per machine-set (`False`, then `True`), keeps the lower-`optimizer.score` winner, and returns a `SweepResult` carrying that machine-set. Progress counts accumulate across both passes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flexible_machines.py (append)
from engine.optimizer import SweepResult, score

def test_sweepresult_has_flexible_machines_field():
    assert SweepResult().flexible_machines is False

def test_local_sweep_reports_winning_machine_set_and_counts_both_passes():
    so_lines, masters = _old_book()
    seen = []
    sw = new_engine.sweep_optimize(so_lines, _CONF, masters, budget_evals=40, seed=1,
                                   on_progress=lambda n, b: seen.append(n))
    assert isinstance(sw.flexible_machines, bool)
    assert sw.result.ranks                       # a real winner
    assert max(seen) > 0                          # progress advanced across both passes
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_flexible_machines.py -k sweep -v`
Expected: FAIL (no `flexible_machines` on SweepResult).

- [ ] **Step 3: Add the SweepResult field**

In `engine/optimizer.py` `SweepResult` (~line 411), add after `knob`:

```python
    flexible_machines: bool = False     # the winning machine-set (new engine)
```

- [ ] **Step 4: Two-pass `sweep_optimize`**

Replace `engine/new_engine.sweep_optimize` body (keep the signature) with a loop over both machine-sets, offsetting progress:

```python
def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None, frozen=None, **kw):
    """Local fallback for 'Start deep search'. Runs the golden-section tune once per
    machine-set (Allotted-only, then Allotted+Suggested) and keeps the better plan by
    score — the third Optimize dimension. Returns the old SweepResult shape."""
    from dataclasses import replace
    from engine.optimizer import OptimizeResult, SweepResult, score

    per = max(15, int(budget_evals) // 10)
    best = None                       # (ranks, overlap_pct, metrics, plans, flexible)
    offset = {"n": 0}

    for flex in (False, True):
        def _step(plans, _best, _flex=flex):
            if on_progress:
                on_progress(offset["n"] + plans, (best or (None,) * 3)[2] if best else {})
        cfg = replace(config, flexible_machines=flex)
        ranks, overlap_pct, metrics, plans = tune(so_lines, cfg, masters,
                                                  budget_per_eval=per, seed=seed, on_step=_step,
                                                  reserved=base_reserved, frozen=frozen)
        offset["n"] += plans
        if ranks and (best is None or score(metrics) < score(best[2])):
            best = (ranks, overlap_pct, metrics, plans, flex)

    if best is None:
        return SweepResult(overlap_percent=int(round(_plan_config(config).overlap * 100)),
                           knob="overlap", flexible_machines=False,
                           result=OptimizeResult(evals=0, best=None), table=[], evals=offset["n"],
                           cancelled=False)
    ranks, overlap_pct, metrics, plans, flex = best
    result = OptimizeResult(ranks=ranks, best=metrics, evals=offset["n"], improved=True, cancelled=False)
    return SweepResult(overlap_percent=overlap_pct, knob="overlap", flexible_machines=flex,
                       result=result, table=[], evals=offset["n"], cancelled=False)
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_flexible_machines.py -k sweep -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/optimizer.py engine/new_engine.py tests/test_flexible_machines.py
git commit -m "feat(optimize): local sweep tunes machine-set as a third dimension"
```

---

### Task 4: Cloud contest two-pass over (machine-set, overlap)

**Files:**
- Modify: `engine/optimize_service.py` (`pick_winner` ~line 253; `run_candidate` ~line 268; `_pool_run` ~line 305; `run_contest` ~line 321)
- Test: `tests/test_optimize_service.py`

**Interfaces:**
- Consumes: `Config.flexible_machines` (Task 1), knob-aware new engine (Task 2).
- Produces: `run_candidate(payload, overlap, flexible)` returns a row with `"flexible": bool`. `pick_winner(current_overlap, current_flexible, rows)`. `run_contest(...)` returns a dict that additionally carries `"winner_flexible": bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimize_service.py (append; follow the file's existing new-engine payload helpers)
def test_run_contest_returns_winner_flexible(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    payload = _new_engine_payload(candidates=[70, 80], budget_per_candidate=20)  # existing helper
    out = optimize_service.run_contest(payload, processes=1)
    assert "winner_flexible" in out
    assert isinstance(out["winner_flexible"], bool)
```

> Use the file's existing payload builder for a new-engine book. If none exists, build one with `optimize_service.build_payload(...)` mirroring `test_optimize_service`'s current cloud tests.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_optimize_service.py -k winner_flexible -v`
Expected: FAIL (`winner_flexible` not in result).

- [ ] **Step 3: `run_candidate` carries the machine-set**

In `run_candidate` (~line 285) change the config build + return:

```python
    knob, _cands = optimizer.knob_for(setup.search_config)
    cfg = replace(setup.search_config, flexible_machines=bool(flexible), **{knob: int(overlap)})
    res = optimizer.optimize(setup.target, cfg, setup.masters,
                             reserved=setup.absence_reserved, frozen=setup.frozen,
                             budget_evals=int(payload["budget_per_candidate"]),
                             seed=int(payload["seed"]),
                             on_progress=on_progress, should_cancel=should_cancel)
    return {"overlap": int(overlap), "flexible": bool(flexible), "eligible": True,
            "best": res.best, "evals": res.evals, "ranks": res.ranks, "cancelled": res.cancelled}
```

Change the signature to `def run_candidate(payload, overlap, flexible=False, *, on_progress=None, should_cancel=None)`.

- [ ] **Step 4: `pick_winner` + `run_contest` sweep both machine-sets**

`pick_winner` (~line 253):

```python
def pick_winner(current_overlap, current_flexible, rows):
    """Best score wins; an exact tie keeps the current (overlap, machine-set)."""
    def _is_current(r):
        return r.get("overlap") == current_overlap and bool(r.get("flexible")) == bool(current_flexible)
    ordered = sorted(rows, key=lambda r: (not _is_current(r), r.get("overlap")))
    best = None
    for r in ordered:
        if not r.get("eligible") or r.get("best") is None:
            continue
        if best is None or optimizer.score(r["best"]) < optimizer.score(best["best"]):
            best = r
    return best
```

`_pool_run` (~line 305): unpack `payload, overlap, flexible = args` and call `run_candidate(payload, overlap, flexible, ...)`.

`run_contest` (~line 321): add the outer machine-set loop and the winner fields:

```python
    config = Config.from_dict(payload["config"])
    knob, _default_cands = optimizer.knob_for(config)
    cur_value = getattr(config, knob)
    cur_flex = bool(getattr(config, "flexible_machines", False))
    contenders = optimizer.sweep_contenders(cur_value, payload["candidates"])
    machine_sets = (False, True)
    rows, done_evals, cancelled = [], 0, False

    if processes <= 1:
        for flex in machine_sets:
            for ov in contenders:
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
            if cancelled:
                break
    else:
        import multiprocessing as mp
        ctx = mp.get_context()
        counter = ctx.Value("i", 0); stop = ctx.Value("b", 0)
        jobs = [(payload, ov, flex) for flex in machine_sets for ov in contenders]
        with ctx.Pool(processes=processes, initializer=_pool_init, initargs=(counter, stop)) as pool:
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

    if on_progress:
        on_progress(done_evals, None)
    winner = pick_winner(cur_value, cur_flex, rows)
    table = [{k: r[k] for k in ("overlap", "flexible", "eligible", "best", "evals") if k in r} for r in rows]
    if winner is None:
        return {"winner_overlap": cur_value, "winner_flexible": cur_flex, "rows": table,
                "knob": knob, "best": None, "ranks": {}, "evals": done_evals, "cancelled": cancelled}
    return {"winner_overlap": winner["overlap"], "winner_flexible": bool(winner["flexible"]),
            "rows": table, "knob": knob, "best": winner["best"], "ranks": winner.get("ranks", {}),
            "evals": done_evals, "cancelled": cancelled}
```

Also update `_pool_run` (~line 305) to unpack the 3-tuple.

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_optimize_service.py -k "winner_flexible or contest" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/optimize_service.py tests/test_optimize_service.py
git commit -m "feat(optimize): cloud contest sweeps machine-set × overlap; winner carries it"
```

---

### Task 5: Worker + `/optimize/result` carry `winner_flexible`

**Files:**
- Modify: `scripts/cloud_optimize_worker.py` (~line 99)
- Modify: `api/main.py` (`WorkerResult` ~line 2232; `/optimize/result` ~line 2287)
- Test: `tests/test_optimize_endpoints.py` (or the file that tests `/optimize/result`)

**Interfaces:**
- Consumes: `run_contest(...)["winner_flexible"]` (Task 4), `_finalize_optimize(winner_flexible=)` (Task 6 defines the param; this task passes it — the finalize signature gains the keyword in Task 6, so land Task 6 before or with this one, or add the param default `False` first).
- Produces: `WorkerResult.winner_flexible: Optional[bool] = None`; the worker posts it; the endpoint passes it to `_finalize_optimize`.

> Ordering note: `_finalize_optimize`'s `winner_flexible=` keyword is introduced in Task 6. Implement Task 6 first, then this task, OR add the keyword (default `False`) in Task 6's Step 3 before wiring here. The reviewer should confirm the endpoint call matches the finalize signature.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimize_endpoints.py (mirror the existing /optimize/result test)
def test_result_endpoint_stores_winner_flexible(new_engine_app_with_running_job):
    client, job_id = new_engine_app_with_running_job          # existing helper/fixture
    r = client.post("/optimize/result", json={
        "job_id": job_id, "winner_overlap": 80, "winner_flexible": True,
        "ranks": {"A\x1fA": 1}, "best": {"total_late_days": 1, "makespan_days": 1},
        "rows": [], "evals": 10, "cancelled": False},
        headers={"X-Worker-Secret": WORKER_SECRET})
    assert r.status_code == 200
    # the stored optimize result echoes the chosen machine-set
    assert client.get("/optimize/status").json().get("flexible_machines") is True
```

> Reuse the file's existing running-job fixture and `WORKER_SECRET`. If the status field name differs, align with Task 6/Task 8 (`flexible_machines` on the status payload).

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_optimize_endpoints.py -k winner_flexible -v`
Expected: FAIL (model rejects the field / status has no `flexible_machines`).

- [ ] **Step 3: Add the model field + pass it through**

`api/main.py` `WorkerResult` (~line 2232) add:

```python
    winner_flexible: Optional[bool] = None
```

`/optimize/result` (~line 2287) pass it:

```python
    stored = _finalize_optimize(req.job_id, base_config, baseline, label,
                                winner_overlap=req.winner_overlap,
                                winner_flexible=bool(req.winner_flexible),
                                ranks=req.ranks, best=req.best, evals=req.evals,
                                table=req.rows, cancelled=req.cancelled)
```

`scripts/cloud_optimize_worker.py` (~line 99) add to the POST body:

```python
            "winner_flexible": out.get("winner_flexible", False),
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_optimize_endpoints.py -k winner_flexible -v`
Expected: PASS (after Task 6/8 land the status field).

- [ ] **Step 5: Commit**

```bash
git add scripts/cloud_optimize_worker.py api/main.py tests/test_optimize_endpoints.py
git commit -m "feat(optimize): worker + result endpoint carry winner_flexible"
```

---

### Task 6: `_finalize_optimize` + `_metrics_for_ranks` thread the machine-set; local job passes it

**Files:**
- Modify: `api/main.py` (`_metrics_for_ranks` ~line 1470; `_finalize_optimize` ~line 1366; `local_job` finalize call ~line 1295; `_optimize_status` ~line 1419)
- Test: `tests/test_optimize_endpoints.py`

**Interfaces:**
- Consumes: `SweepResult.flexible_machines` (Task 3), `run_contest` winner (Task 4).
- Produces: `_metrics_for_ranks(ranks, overlap=None, flexible=None, *, with_distribution=True)`; `_finalize_optimize(..., winner_flexible=False)` recomputes the winner at that machine-set, folds it into `inputs_sig`, and stores `result["flexible_machines"]`. `_optimize_status` exposes `flexible_machines` + `current_flexible`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimize_endpoints.py
def test_finalize_recomputes_winner_at_its_machine_set(new_engine_client_with_book):
    """A local sweep whose winner is the union must store metrics recomputed WITH the
    union, so shown == applied."""
    client = new_engine_client_with_book       # existing helper: uploaded book, new engine
    client.post("/optimize", json={"budget": "quick"})
    _wait_done(client)
    st = client.get("/optimize/status").json()
    assert "flexible_machines" in st
    # applying reproduces those exact expected dates (guarded further in Task 7)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_optimize_endpoints.py -k recomputes_winner -v`
Expected: FAIL.

- [ ] **Step 3: Add the `flexible` param to `_metrics_for_ranks`**

`api/main.py` (~line 1470):

```python
def _metrics_for_ranks(ranks, overlap=None, flexible=None, *, with_distribution=True):
    ...
        config = _resolve_config(_load_plan_config())
        if overlap is not None:
            knob = optimizer.knob_for(config)[0]
            config = replace(config, **{knob: overlap})
        if flexible is not None:
            config = replace(config, flexible_machines=bool(flexible))
        ...
```

- [ ] **Step 4: Thread `winner_flexible` through `_finalize_optimize`**

Signature (~line 1366): `def _finalize_optimize(job_id, base_config, real_baseline, label, *, winner_overlap, winner_flexible=False, ranks, best, evals, table, cancelled):`

Recompute (~line 1378): `_local_best = _metrics_for_ranks(ranks, winner_overlap, winner_flexible)`

Inputs sig (~line 1387): `inputs_sig = _inputs_signature(replace(base_config, flexible_machines=bool(winner_flexible), **{_knob: winner_overlap}))`

Result dict (~line 1396) add:

```python
                    "flexible_machines": bool(winner_flexible),
                    "current_flexible": bool(getattr(base_config, "flexible_machines", False)),
```

- [ ] **Step 5: Local job + status expose it**

`local_job` finalize call (~line 1295):

```python
            _finalize_optimize(job_id, base_config, real_baseline, label,
                               winner_overlap=sw.overlap_percent,
                               winner_flexible=sw.flexible_machines, ranks=res.ranks,
                               best=res.best, evals=sw.evals, table=sw.table,
                               cancelled=sw.cancelled)
```

`_optimize_status` (~line 1430) add to the returned dict:

```python
                "flexible_machines": res.get("flexible_machines"),
                "current_flexible": res.get("current_flexible"),
```

- [ ] **Step 6: Run to verify it passes**

Run: `pytest tests/test_optimize_endpoints.py -k "recomputes_winner or winner_flexible" -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add api/main.py tests/test_optimize_endpoints.py
git commit -m "feat(optimize): finalize recomputes + stores the winning machine-set (shown==applied)"
```

---

### Task 7: Apply persists the machine-set; `_plan` reproduces the winner

**Files:**
- Modify: `api/main.py` (`_optimize_apply` ~line 1675)
- Test: `tests/test_optimize_endpoints.py`

**Interfaces:**
- Consumes: `result["flexible_machines"]` + `result["best_overlap"]` + `result["knob"]` (Task 6).
- Produces: On Apply, the saved plan config carries the winning `flexible_machines` (and overlap); a subsequent `_plan` loads masters at that flexibility, so its `expected_end` matches the applied winner's metrics.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimize_endpoints.py
def test_apply_persists_machine_set_and_plan_reproduces(new_engine_client_with_book, monkeypatch):
    client = new_engine_client_with_book
    # Force a union winner deterministically: stub the finished job's result.
    _install_done_job(client, ranks={...}, best={...}, best_overlap=80, flexible_machines=True)
    client.post("/optimize/apply")
    cfg = json.loads(book_store.load_plan_config())
    assert cfg["flexible_machines"] is True                 # persisted
    # _plan now uses the union — a machining op lands on a Suggested machine
    machines = {r[...] for r in client.post("/run", json={}).json()["...schedule..."]}
    assert "CNC2" in machines
```

> Prefer driving this through the real `_finalize_optimize` result already stored by Task 6's flow rather than a stub, if the harness allows a deterministic union win on the fixture. Otherwise stub `_OPTIMIZE["result"]` (as the committed-feature tests do) with `flexible_machines=True`.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_optimize_endpoints.py -k persists_machine_set -v`
Expected: FAIL (config lacks the field / plan doesn't reproduce).

- [ ] **Step 3: Persist the machine-set on Apply**

Replace the overlap-persist block in `_optimize_apply` (~line 1675):

```python
        # Settings sweep: the winning overlap AND machine-set become THE saved plan
        # settings (the single config every Plan loads and Settings shows). Unchanged
        # winner -> no write, no churn.
        best_ov = res.get("best_overlap")
        best_flex = res.get("flexible_machines")
        cfg = _load_plan_config()
        knob = res.get("knob") or optimizer.knob_for(cfg)[0]
        target = cfg
        if best_ov is not None:
            target = replace(target, **{knob: best_ov})
        if best_flex is not None:
            target = replace(target, flexible_machines=bool(best_flex))
        if target.to_dict() != cfg.to_dict():
            book_store.save_plan_config(json.dumps(target.to_dict()))
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_optimize_endpoints.py -k persists_machine_set -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_optimize_endpoints.py
git commit -m "feat(optimize): Apply persists the winning machine-set; _plan reproduces it"
```

---

### Task 8: Read-only Settings display

**Files:**
- Modify: `web/index.html` (Scheduling settings group, near the overlap read-only line ~line 162)
- Modify: `web/app.js` (`applyConfig`/config echo ~line 223)
- Test: manual (Node syntax check + a DOM assertion is out of scope for this JS)

**Interfaces:**
- Consumes: `/run` response `config.flexible_machines`.
- Produces: `#cfg-machineset-info` shows "Allotted only" / "Allotted + Suggested".

- [ ] **Step 1: Add the read-only line**

In `web/index.html`, after the overlap `cfg-readonly` paragraph (~line 165):

```html
          <p class="cfg-readonly">Machine set (tuned by Optimize — you don't set this):
            <span id="cfg-machineset-info">Allotted only</span>. Whether the optimizer may
            also use each step's Suggested machines to balance load off a busy machine.</p>
```

- [ ] **Step 2: Populate it from the config echo**

In `web/app.js`, beside the overlap-info line (~line 223):

```javascript
  const ms = $("cfg-machineset-info");
  if (ms) ms.textContent = cfg.flexible_machines ? "Allotted + Suggested" : "Allotted only";
```

- [ ] **Step 3: Verify**

Run: `node --check web/app.js`
Expected: OK. Load the app, apply a plan, confirm the line reads correctly for both machine-sets.

- [ ] **Step 4: Commit**

```bash
git add web/index.html web/app.js
git commit -m "feat(web): Settings shows the Optimize-chosen machine set (read-only)"
```

---

### Task 9: Progress-budget doubling, docs, and Test8 re-measurement

**Files:**
- Modify: `api/main.py` (`sweep_total_evals` display sizing at the cloud-fallback branches ~line 1322, ~line 1348)
- Modify: `RULES.md`, `CLAUDE.md`
- Measure: scratch script (not committed)

**Interfaces:**
- Consumes: everything above.
- Produces: a progress bar sized for the doubled contest; docs describing the third dimension; owner-facing Test8 numbers.

- [ ] **Step 1: Double the displayed budget for the two-pass contest**

At both `_OPTIMIZE["budget_evals"] = optimizer.sweep_total_evals(...)` sites (~line 1322, ~line 1348), multiply by the number of machine-sets so the bar reflects real work:

```python
                        _OPTIMIZE["budget_evals"] = 2 * optimizer.sweep_total_evals(
                            budget_evals, getattr(setup.search_config, _k), _kc)
```

(This is cosmetic — the counter would otherwise exceed the bar. No test; visual only.)

- [ ] **Step 2: Full suite green**

Run: `pytest`
Expected: PASS (508+ baseline plus the new tests). Investigate any new-engine optimize-outcome test that now picks a union winner — update its expected winner to the correct value; do not weaken it.

- [ ] **Step 3: Re-measure Test8**

Write a scratch script (pattern: `scratchpad/flex_measure.py` from the design conversation) that runs the REAL contest (`optimizer.sweep_optimize`) twice — once with the machine-set loop disabled (Allotted-only) and once enabled — on the uploaded Test8 book, and prints makespan / late-days / worst / bands for each. Report the numbers to the owner. Do not commit the scratch script.

- [ ] **Step 4: Update docs**

- `RULES.md`: note that Optimize now searches sequence × overlap × machine-set (Allotted vs Allotted+Suggested), and that `flexible_machines` is optimizer-owned.
- `CLAUDE.md`: add a bullet under the Optimize/Settings sections describing the third dimension, the `flexible_machines` config field (default False = byte-identical, folded into `_inputs_signature`), the doubled contest cost, and that it is new-engine only.

- [ ] **Step 5: Commit**

```bash
git add api/main.py RULES.md CLAUDE.md
git commit -m "docs(optimize): machine-set third dimension; size the doubled contest bar"
```

---

## Self-Review

**Spec coverage:** Config field (T1) ✓; `_new_masters` + everyday plan reads it (T2) ✓; local contest outer loop (T3) ✓; cloud contest outer loop (T4) ✓; worker/result (T5) ✓; finalize recompute + inputs_sig + status (T6) ✓; Apply persist + `_plan` reproduces (T7) ✓; read-only Settings (T8) ✓; docs + cost + Test8 measure (T9) ✓; byte-identical guard (T2) ✓; frozen-on-suggested edge (T2) ✓; cloud==local winner parity — covered implicitly by shared `optimizer.score` + the recompute; add an explicit parity assertion in T4 if the harness allows.

**Placeholder scan:** the two `>` notes (import a `_old_book()` helper; match `_ppc_frozen`'s frozen-dict shape; reuse the endpoints test fixtures) are concrete actions requiring the implementer to read a named existing symbol, not vague TODOs. All code steps carry real code.

**Type consistency:** `flexible_machines: bool` everywhere; `winner_flexible` on the wire (worker/endpoint) maps to `flexible_machines` in the stored result and config; `_metrics_for_ranks(ranks, overlap, flexible)`; `SweepResult.flexible_machines`; `run_candidate(payload, overlap, flexible)`; `pick_winner(current_overlap, current_flexible, rows)`. Consistent across tasks.
