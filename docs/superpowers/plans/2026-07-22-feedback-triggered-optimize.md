# Feedback-triggered re-optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the twice-weekly cron auto-optimize with a feedback-driven one: the "Done entering — update plan" button starts an auto-applying optimization contest, waits for it with live progress, and refreshes the schedule/Gantt to the winning plan.

**Architecture:** Rewire the *trigger* only. The contest runner, cloud→local fallback, strictly-better auto-apply, and progress polling all already exist. The "Done" button (both roles) calls a new `POST /optimize/done`, which reuses `_try_start_auto()` (now not cloud-only) to start an `auto=True` contest; the frontend polls `/optimize/status` and calls `runPlan(false)` on completion. The Mon/Fri cron, `POST /optimize/scheduled`, and `nextScheduledOptimize()` are removed.

**Tech Stack:** Python 3 + FastAPI (backend), vanilla JS (frontend), pytest. New engine (`scheduler="new"`) is production but this plan touches only the API/UI trigger layer — no engine code changes.

## Global Constraints

- **Do not modify the engine** (`ppc_engine/`, `engine/new_engine.py`, `engine/rules/`, `engine/optimizer.py`, `engine/optimize_service.py`). This is trigger/wiring only.
- **`_try_start_auto()` keeps returning `bool`** — two tests in `tests/test_operator_wiring.py` (`test_scheduled_runs_when_inputs_changed_though_book_same`, `test_scheduled_skips_when_book_and_inputs_both_match`) assert `is True`/`is False` and must keep passing unchanged.
- **`/optimize/done` is NOT admin-gated** — operators (user role) enter feedback, so they must be able to trigger it. It still requires a valid session (the `gatekeeper` middleware enforces that; unauthenticated → 401).
- **Auto-apply stays strictly-better-or-nothing** (`_auto_apply_result`, unchanged).
- **Commit after each task.** We are on branch `main`; per repo rule, do NOT push. Commit locally only.
- Run tests with `python3 -m pytest` (the `python` command is absent on this machine).
- Full suite must stay green (currently 508 passed, 1 skipped).

---

### Task 1: Backend — replace the cron trigger with the feedback trigger

**Files:**
- Modify: `api/main.py` — rewrite `_try_start_auto()` (~line 992), add `POST /optimize/done`, remove `POST /optimize/scheduled` (~line 1818), drop `/optimize/scheduled` from the gatekeeper worker-bypass tuple (~line 117).
- Test: `tests/test_auto_optimize.py` — replace the six `test_scheduled_*` tests and invert `test_optimize_done_endpoint_is_gone`.

**Interfaces:**
- Consumes: `_auto_enabled()`, `_OPTIMIZE`/`_OPTIMIZE_LOCK`, `_applied_plan_meta()`, `_current_book_sig()`, `_inputs_signature()`, `_load_plan_config()`, `_auto_note_write()`, `_start_optimize()`, `_optimize_status()`, `_OPT_BUDGETS` — all already in `api/main.py`.
- Produces: `POST /optimize/done` returning `{"started": bool, "state": str}`; `_try_start_auto() -> bool` (unchanged signature, new internals).

- [ ] **Step 1: Write the failing tests**

Open `tests/test_auto_optimize.py`. Replace the module docstring (lines 1–8) with:

```python
"""Feedback trigger (spec 2026-07-22): the auto contest starts from POST
/optimize/done — the 'Done entering — update plan' button, available to BOTH
roles. It starts an auto-applying contest unless auto is disabled, one is already
running, or nothing material changed since the last applied plan (book + inputs
fingerprint). Unlike the removed Mon/Fri cron it is NOT cloud-only. Admin
mutations (upload, commit, delete, /run persist) still never start a contest on
their own. AUTO_OPTIMIZE=0 (internal test isolation only) disables everything."""
```

Delete these six tests entirely (they tested the removed cron endpoint):
`test_scheduled_requires_worker_secret`, `test_scheduled_starts_contest_with_secret`, `test_scheduled_no_op_when_signature_matches_applied`, `test_scheduled_disabled_by_internal_env`, `test_scheduled_is_cloud_only`, `test_scheduled_running_contest_returns_false`.

Replace `test_optimize_done_endpoint_is_gone` (lines ~129–136) with these tests:

```python
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
                                       "inputs_sig": m._inputs_signature(cfg)})
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/optimize/done")
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert starts == []
    assert "plan unchanged" in (book_store.load_auto_note() or {}).get("text", "")


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_auto_optimize.py -q`
Expected: FAIL — `test_done_*` tests error/fail because `/optimize/done` returns 404/405 (endpoint not added yet).

- [ ] **Step 3: Rewrite `_try_start_auto()`**

In `api/main.py`, replace the whole `_try_start_auto()` function (currently ~lines 992–1024) with:

```python
def _try_start_auto() -> bool:
    """Start an auto-applying re-optimization if it makes sense. Invoked by
    POST /optimize/done (the 'Done entering — update plan' button). Returns True
    iff a contest was started. Returns False — starting nothing — when auto is
    disabled, a contest is already running, or NOTHING material changed since the
    last applied plan (book + inputs fingerprint match; a friendly note is written
    in that case). Unlike the removed Mon/Fri cron this is NOT cloud-only:
    _start_optimize falls back to local compute, so the button always does
    something even with no cloud configured."""
    if not _auto_enabled():
        return False
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            return False                         # one contest at a time
    # Skip only when NOTHING material changed since the last applied plan —
    # "material" includes the inputs fingerprint (masters + settings + operator
    # rotation/edits), not just the book. A legacy applied meta without an
    # inputs_sig doesn't force a run on the missing field alone.
    meta = _applied_plan_meta() or {}
    try:
        book_same = (meta.get("book_sig") == _current_book_sig())
        applied_inputs = meta.get("inputs_sig")
        inputs_same = (applied_inputs is None
                       or applied_inputs == _inputs_signature(_load_plan_config()))
        if book_same and inputs_same:
            _auto_note_write("No new feedback since the last optimization — "
                             "plan unchanged.")
            return False                         # nothing material changed
    except Exception:
        return False
    try:
        _start_optimize(_OPT_BUDGETS["deep"], "auto", background=True, auto=True)
        return True
    except HTTPException:
        return False                             # e.g. nothing to optimize
```

- [ ] **Step 4: Add the `/optimize/done` endpoint and remove `/optimize/scheduled`**

In `api/main.py`, replace the `optimize_scheduled_ep` function (currently ~lines 1818–1825) with:

```python
@app.post("/optimize/done")
def optimize_done_ep(request: Request):
    """'Done entering — update plan': the feedback-driven re-optimization trigger.
    Any logged-in role (operators enter the feedback that motivates it). Starts an
    auto-applying contest unless nothing changed since the last applied plan or one
    is already running. Poll GET /optimize/status for progress; the winner
    auto-applies if strictly better and the next /run reflects it."""
    # No require_admin: the gatekeeper already verified a valid session for any
    # non-public path, and this must be reachable by the user role.
    started = _try_start_auto()
    return {"started": started, "state": _optimize_status()["state"]}
```

Then in the `gatekeeper` middleware (~lines 116–119), drop `/optimize/scheduled` from the worker-bypass tuple so it reads:

```python
    if ((method == "GET" and path.startswith("/optimize/job/"))
            or (method == "POST" and path in ("/optimize/progress",
                                              "/optimize/result"))):
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_auto_optimize.py tests/test_operator_wiring.py -q`
Expected: PASS. The two `test_scheduled_*` tests in `test_operator_wiring.py` still pass (they assert `_try_start_auto()` bool behavior, which is unchanged for their setups — cloud is configured via `GITHUB_DISPATCH_TOKEN=manual`, and removing the cloud-only gate does not change their True/False outcomes).

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_auto_optimize.py
git commit -m "feat: feedback-triggered re-optimization via POST /optimize/done; remove Mon/Fri cron endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Frontend — wire the Done button to the trigger + live progress

**Files:**
- Modify: `web/app.js` — add `_sleep`, `doneOptimize()`, `pollDoneOptimize()`; rewire the `#optimize-done` handler (~lines 1315–1326); remove `nextScheduledOptimize()` (~lines 659–672) and its two call sites (status strip ~line 362, done-status ~line 1324).

**Interfaces:**
- Consumes: `runPlan(false)`, `optimizeProgressLine(st)` (exists ~line 400), `$(id)`, `setStatus`.
- Produces: `doneOptimize()` wired to the Done button; static "Optimization runs when you finish entering feedback" text.

- [ ] **Step 1: Add the sleep helper and the done-optimize functions**

In `web/app.js`, near the other optimize functions (just above `async function startOptimize()`, ~line 516), add:

```javascript
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// "Done entering — update plan": fire a feedback-driven re-optimization, then
// block on live progress until it lands and refresh the whole plan to the winner.
// The contest auto-applies server-side if it's strictly better (both roles).
async function doneOptimize() {
  const st = $("optimize-done-status");
  const doneBtn = $("optimize-done");
  if (doneBtn) doneBtn.disabled = true;
  if (st) st.textContent = "Starting optimization…";
  let started = false;
  try {
    const res = await fetch("/optimize/done", { method: "POST" });
    if (!res.ok) {
      if (st) st.textContent = "Could not start optimization: " + (await res.text());
      if (doneBtn) doneBtn.disabled = false;
      return;
    }
    started = (await res.json()).started;
  } catch (e) {
    if (st) st.textContent = "Could not start optimization: " + e.message;
    if (doneBtn) doneBtn.disabled = false;
    return;
  }
  if (!started) {
    // Nothing changed since the last run (or auto disabled) — just refresh facts.
    await runPlan(false);
    if (st) st.textContent = "Plan updated. No new feedback to re-optimize.";
    if (doneBtn) doneBtn.disabled = false;
    return;
  }
  await pollDoneOptimize(st);
  if (doneBtn) doneBtn.disabled = false;
}

// Poll the shared contest to completion, showing progress next to the Done
// button. The contest auto-applies itself; on completion we refresh everything.
async function pollDoneOptimize(st) {
  for (;;) {
    let status;
    try {
      const r = await fetch("/optimize/status");
      if (!r.ok) { await _sleep(3000); continue; }
      status = await r.json();
    } catch (e) { await _sleep(3000); continue; }
    if (status.state === "running") {
      if (st) st.textContent = "Optimizing… " + optimizeProgressLine(status)
        + " (this can take several minutes)";
      await _sleep(3000);
      continue;
    }
    await runPlan(false);   // pick up the auto-applied winner + new facts
    if (st) {
      st.textContent = status.state === "failed"
        ? "Optimization could not finish: " + (status.error || "unknown error")
          + ". Plan updated with the latest feedback."
        : "Plan re-optimized and updated.";
    }
    return;
  }
}
```

- [ ] **Step 2: Rewire the Done button handler**

In `web/app.js`, replace the current Done handler block (~lines 1315–1326) with:

```javascript
  // "Done entering — update plan": re-optimizes the job order from the day's
  // feedback (both roles), waits for the contest, then refreshes the whole plan.
  const doneBtn = $("optimize-done");
  if (doneBtn) doneBtn.onclick = doneOptimize;
```

- [ ] **Step 3: Remove `nextScheduledOptimize()` and its call sites**

Delete the entire `nextScheduledOptimize(now)` function (~lines 659–672, including its leading comment block ~lines 659–663).

Replace the status-strip segment (~line 362):

```javascript
  segs.push(`<span class="ss-seg">Next optimization: ${escapeHtml(nextScheduledOptimize())}</span>`);
```

with:

```javascript
  segs.push(`<span class="ss-seg">Optimization runs when you finish entering feedback</span>`);
```

(The other former call site, in the old Done handler at line 1324, is already gone after Step 2.)

- [ ] **Step 4: Verify no dangling references**

Run: `grep -n "nextScheduledOptimize" web/app.js`
Expected: no output (all references removed).

- [ ] **Step 5: Smoke-test the wiring in a browser**

Start the app locally: `python3 -m uvicorn api.main:app --port 8099` (run in background).
Log in as admin (`anvitech` / `1930rail`), upload the sample or use the seeded store, go to Capture Actuals, enter one punch, click **Done entering — update plan**. With no cloud configured locally, `_start_optimize` falls back to a capped local search (fast on the sample book). Confirm: the status line shows "Optimizing…", then "Plan re-optimized and updated.", and the Gantt/schedule refresh. Stop the server afterward.

(No JS unit tests exist in this repo — frontend is verified by reading + this smoke test, per the codebase convention.)

- [ ] **Step 6: Commit**

```bash
git add web/app.js
git commit -m "feat: Done button triggers feedback re-optimization with live progress; drop nextScheduledOptimize

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Remove the cron workflow + update CLAUDE.md

**Files:**
- Delete: `.github/workflows/scheduled-optimize.yml`
- Modify: `CLAUDE.md` — the "Scheduled optimize (2026-07-18 …)" bullet and any "Mon & Fri"/`/optimize/scheduled`/`nextScheduledOptimize` references.

- [ ] **Step 1: Delete the cron workflow**

```bash
git rm .github/workflows/scheduled-optimize.yml
```

- [ ] **Step 2: Update CLAUDE.md**

Find the **"Scheduled optimize (2026-07-18 …)"** bullet under the optimizer/`engine/optimizer.py` map entry. Replace its opening sentence(s) so it describes the new trigger. Insert, at the top of that bullet, a dated note:

```markdown
- **Feedback-triggered optimize (2026-07-22,
  `docs/superpowers/specs/2026-07-22-feedback-triggered-optimize-design.md`,
  supersedes the twice-weekly cron below) — the job order re-optimizes when
  feedback is entered.** The **"Done entering — update plan"** button (both roles)
  hits **`POST /optimize/done`** → `_try_start_auto()`, which starts an
  auto-applying contest unless a run is already going or nothing changed since the
  last applied plan (book + inputs fingerprint; writes a "plan unchanged" note).
  It is **NOT cloud-only** (local fallback), so the button always acts. The
  frontend blocks on live progress (`/optimize/status`) then `runPlan(false)`s to
  the auto-applied winner. **Removed:** the Mon/Fri GitHub cron
  (`.github/workflows/scheduled-optimize.yml`), `POST /optimize/scheduled`, and
  `nextScheduledOptimize()`. Auto-apply is still strictly-better-or-nothing
  (`_auto_apply_result`); `AUTO_OPTIMIZE=0` still disables it (test isolation only).
```

Then, in the **rest of that old bullet and anywhere else in CLAUDE.md**, correct now-false statements: any "Monday and Friday at 11:00 IST" / "twice a week" / "`cron: \"30 5 * * 1,5\"`" / "`POST /optimize/scheduled`" / "the Capture Actuals … button … no longer starts a contest" / "`nextScheduledOptimize()`" phrasing must be updated to reflect that the Done button now triggers the contest and the cron is gone. (Search: `grep -n "scheduled\|Mon.*Fri\|nextScheduled\|twice a week\|twice-weekly" CLAUDE.md`.)

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove Mon/Fri optimize cron; document feedback-triggered optimize in CLAUDE.md

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest -q`
Expected: PASS — all tests green (was 508 passed / 1 skipped; the count changes because six `test_scheduled_*` tests were removed and six `test_done_*` added — net roughly the same). No failures, no errors.

- [ ] **Step 2: Confirm no stale references remain**

Run:
```bash
grep -rn "optimize/scheduled\|nextScheduledOptimize" api/ web/ tests/ .github/
grep -rn "scheduled-optimize.yml" .github/ docs/ || true
```
Expected: no references to `/optimize/scheduled` or `nextScheduledOptimize` in `api/`, `web/`, or `tests/`; the workflow file is gone.

- [ ] **Step 3: Final commit (if grep surfaced anything to fix)**

If Step 2 found stragglers, fix them and:

```bash
git add -A
git commit -m "chore: remove remaining references to the retired optimize cron

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Otherwise this task is complete with no commit.
