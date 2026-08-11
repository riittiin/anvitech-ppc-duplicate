# Cloud Stop-Immediate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "Stop & keep best" end a cloud optimize run immediately and return a plan built from whatever shards have already reported, instead of doing nothing for 40 minutes and then yielding nothing.

**Architecture:** Extract the cancel handling into one module-level helper, `_cancel_cloud_job(job_id)`, so it is directly testable rather than buried in a closure. The cloud wait loop then checks cancel *before* the timeout logic and delegates. The existing partial-salvage function `_finalize_from_shards` is reused unchanged.

**Tech Stack:** Python 3, FastAPI, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-06-cloud-stop-immediate-design.md`

> **AMENDED DURING EXECUTION (2026-08-06).** This plan prescribed acting on a cancel
> the instant it is seen. Measurement during the final review disproved that: cloud
> workers heartbeat every `PROGRESS_EVERY_S = 5.0`s and learn about a Stop from that
> response, so ending the job on the first 2-second poll kills it before any worker can
> answer — their next heartbeat 404s, they never stop, and their results are dropped.
> Measured against `main` on a healthy run: `main` produced ranks=2/evals=2800, the
> as-planned version produced ranks=0/evals=100, i.e. **worse than no fix at all**.
>
> Owner decision: a **~90-second grace window** (`_CANCEL_GRACE_S`). On first sight of a
> cancel the loop starts a timer, keeps polling so workers can deliver their best-so-far,
> and only calls `_cancel_cloud_job` once the grace expires — bounded by the original
> deadline. Verified to match `main` outcome-for-outcome on healthy runs and to beat it
> on the incident shape (17 of 20 shards salvaged instead of discarded).
>
> The error string in the tasks below also changed to "no usable result had come back
> yet" — the original wording claimed nothing arrived in cases where results had.

## Global Constraints

- **Stop always means stop.** It must never start a fresh computation. Specifically it must never fall back to a local search.
- **Salvage what arrived.** If any shards have reported, the run ends with a plan merged from them. In the incident that prompted this, 17 of 20 shards were discarded.
- **`_finalize_from_shards` is NOT modified.** It is already partial-safe — its docstring says "Safe to call from the collector (all-arrived) and the watchdog (partial)".
- **`/optimize/shard-result` is NOT modified.** Its all-arrived condition (`len(shards) >= shard_total`) is correct for the healthy case; the cancel path covers the case where the total can never be reached.
- **`scripts/cloud_optimize_worker.py` is NOT modified.** Worker-side cancel already works.
- **`.github/workflows/optimize.yml` is NOT modified.** `max-parallel` stays at **20** — owner decision, 2026-08-06.
- **The timeout path's behaviour is unchanged** for a run that was not cancelled.
- **Do not touch** any engine file, the scheduler, the rules, the UI, or any export.
- **Use `/usr/bin/python3`, never plain `python3`.** Homebrew Python here has numpy 2.5.1, which removed `numpy.short`; openpyxl 3.1.5 still references it, so `import openpyxl` raises and pytest cannot load `conftest.py`.
- **Baseline is 776 passed, 1 skipped.** Every task must leave the suite green, and the golden trace must pass without regeneration.
- **Never push.** `main` auto-deploys to a live factory.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `api/main.py` | New `_cancel_cloud_job(job_id)` helper | 1 |
| `tests/test_cloud_stop_immediate.py` | The helper's behaviour, all five cases | 1 |
| `api/main.py` | Wire the helper into the cloud wait loop | 2 |

---

### Task 1: The `_cancel_cloud_job` helper

Extracting this rather than inlining it in the loop is deliberate: the wait loop lives
inside the `cloud_job()` closure in `_start_optimize`, which cannot be called directly
from a test without stubbing a contest. A module-level helper is testable on its own.

**Files:**
- Modify: `api/main.py` — add the helper immediately after `_optimize_cancel` (~line 1628)
- Create: `tests/test_cloud_stop_immediate.py`

**Interfaces:**
- Consumes: the existing module globals `_OPTIMIZE`, `_OPTIMIZE_LOCK`, and the existing function `_finalize_from_shards(job_id)`.
- Produces: `_cancel_cloud_job(job_id) -> None`. Task 2 calls it from the wait loop.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cloud_stop_immediate.py`:

```python
"""Stop & keep best must END a cloud run immediately and keep what arrived.

Bug (2026-07-15, hit live 2026-08-06): the cloud wait loop read `cancel` every two
seconds but only acted on it INSIDE `if timed_out:`, so Stop did nothing until the
40-minute deadline and then produced no plan. In the live incident GitHub allocated
17 of 20 requested runners; the 3 that never started could never report, so the
collector's all-arrived condition could never be met, and 17 delivered shard results
sat with nothing willing to finalize them.

These tests pin the fix: cancel salvages whatever arrived, ends cleanly when nothing
did, and NEVER falls back to a local search.
"""
import pytest

pytest.importorskip("fastapi")


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed(m, *, shards, job_id="job-1", finalizing=False):
    """_OPTIMIZE as a running cloud job carrying `shards`."""
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="running", job_id=job_id, mode="cloud",
                           cancel=True, cloud_failed=False,
                           shards=dict(shards), shard_total=20,
                           shards_finalizing=finalizing, error=None)


def test_cancel_with_shards_finalizes_from_them(monkeypatch):
    """The headline case: 17 of 20 arrived, Stop must build a plan from them."""
    m = _api()
    _seed(m, shards={i: {"rows": [], "evals": 840} for i in range(17)})
    calls = []

    def _fake_finalize(job_id):
        calls.append(job_id)
        with m._OPTIMIZE_LOCK:          # a real finalize ends the job
            m._OPTIMIZE.update(state="done", cancel=False)
    monkeypatch.setattr(m, "_finalize_from_shards", _fake_finalize)

    m._cancel_cloud_job("job-1")

    assert calls == ["job-1"], "must salvage the arrived shards"
    assert m._OPTIMIZE["state"] == "done"


def test_cancel_with_no_shards_ends_cleanly(monkeypatch):
    """Owner ruling: Stop with nothing back ends the run and starts NOTHING."""
    m = _api()
    _seed(m, shards={})
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-1")

    assert calls == [], "nothing to salvage — must not call finalize"
    assert m._OPTIMIZE["state"] == "failed"
    assert m._OPTIMIZE["cancel"] is False
    assert "stopped" in (m._OPTIMIZE["error"] or "").lower()


def test_cancel_never_leaves_cloud_failed_for_the_watchdog(monkeypatch):
    """The subtle one. _finalize_from_shards sets cloud_failed when the merged
    shards yield no eligible winner, and the watchdog reads that flag to START A
    LOCAL SEARCH. Under cancel that would turn Stop into 'begin a fresh 15-minute
    computation', which the owner explicitly rejected."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}})

    def _fake_finalize(job_id):
        with m._OPTIMIZE_LOCK:          # merge found no winner: still running
            m._OPTIMIZE["cloud_failed"] = True
    monkeypatch.setattr(m, "_finalize_from_shards", _fake_finalize)

    m._cancel_cloud_job("job-1")

    assert m._OPTIMIZE["state"] == "failed", "must end, not linger for the watchdog"
    assert m._OPTIMIZE["cloud_failed"] is False, "must not trigger a local fallback"


def test_cancel_does_not_double_finalize(monkeypatch):
    """`shards_finalizing` is an atomic claim shared with the shard collector. If
    the collector already claimed it, cancel must not finalize a second time."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}}, finalizing=True)
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-1")

    assert calls == [], "the collector already owns the finalize"


def test_cancel_on_an_already_finished_job_is_a_noop(monkeypatch):
    """A cancel racing a normal completion must not clobber the finished result."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="done", error=None)
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-1")

    assert calls == []
    assert m._OPTIMIZE["state"] == "done", "a finished job must stay finished"


def test_cancel_for_a_different_job_id_is_a_noop(monkeypatch):
    """Stale cancel from a superseded run must not touch the current one."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}}, job_id="job-CURRENT")
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-OLD")

    assert calls == []
    assert m._OPTIMIZE["state"] == "running", "the current job must be untouched"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/Users/ritinwadekar/Desktop/Anvitech Rebuilt"
/usr/bin/python3 -m pytest tests/test_cloud_stop_immediate.py -v
```

Expected: FAIL — `AttributeError: module 'api.main' has no attribute '_cancel_cloud_job'`.

- [ ] **Step 3: Write the helper**

In `api/main.py`, immediately after the `_optimize_cancel` function (which ends around
line 1627), add:

```python
def _cancel_cloud_job(job_id):
    """Honour a Stop during a CLOUD run: salvage whatever shards arrived, then end.

    Stop means stop (2026-08-06 spec). Before this, the wait loop read `cancel` every
    two seconds and only acted on it inside `if timed_out:`, so a Stop did nothing for
    up to OPTIMIZE_CLOUD_TIMEOUT_MIN (40) minutes and then produced no plan. Live
    2026-08-06: GitHub allocated 17 of 20 requested runners, so the collector's
    all-arrived condition could never be met, and 17 delivered shard results were about
    to be discarded.

    NEVER falls back to local. `_finalize_from_shards` sets `cloud_failed` when the
    merged shards yield no eligible winner, and the watchdog reads that flag to start a
    local search — under cancel that would turn Stop into "start a fresh computation",
    which the owner explicitly rejected. So this clears it.

    `shards_finalizing` is claimed under the lock in the same block that reads it,
    exactly as the timeout branch does, so it stays mutually exclusive with the
    /optimize/shard-result collector's own claim: the two can never both finalize.
    """
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
            return                       # already finished, or a superseded job
        claim = (bool(_OPTIMIZE.get("shards"))
                 and not _OPTIMIZE.get("shards_finalizing"))
        if claim:
            _OPTIMIZE["shards_finalizing"] = True
    if claim:
        _finalize_from_shards(job_id)    # merges whatever subset arrived
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id:
            # Either nothing had arrived, or the merge found no eligible winner.
            # End either way, and clear cloud_failed so no local run starts.
            _OPTIMIZE.update(state="failed", cancel=False, cloud_failed=False,
                             error="stopped: no finished results had come back yet, "
                                   "so the current plan is unchanged")
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
/usr/bin/python3 -m pytest tests/test_cloud_stop_immediate.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run the full suite**

```bash
/usr/bin/python3 -m pytest -q
```

Expected: 782 passed, 1 skipped (776 + the 6 new tests). The helper is not yet wired
into the loop, so nothing else can change.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_cloud_stop_immediate.py
git -c commit.gpgsign=false commit -m "feat: _cancel_cloud_job — salvage arrived shards on Stop

Extracted as a module-level helper because the cloud wait loop lives inside
a closure and cannot be tested directly. Not yet wired in.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire the helper into the cloud wait loop

**Files:**
- Modify: `api/main.py` — the cloud wait loop inside `_start_optimize`'s `cloud_job()`, currently around lines 1461-1521

**Interfaces:**
- Consumes: `_cancel_cloud_job(job_id) -> None` from Task 1.
- Produces: nothing consumed downstream.

- [ ] **Step 1: Read the current loop and locate the exact block**

The loop opens with `deadline = time.monotonic() + cloud["timeout_min"] * 60`. Inside it,
this block currently reads the cancel flag and then ignores it unless timed out:

```python
                    timed_out = (time.monotonic() > deadline
                                 or _OPTIMIZE.get("cloud_failed"))
                    was_cancelled = _OPTIMIZE["cancel"]
                    if timed_out:
                        if was_cancelled:
                            # Cancelled and the cloud never answered → just stop.
                            _OPTIMIZE.update(state="failed", cancel=False,
                                             error="stopped: the cloud run did not "
                                                   "answer before the timeout")
                            return
```

- [ ] **Step 2: Make cancel its own trigger**

Replace exactly that block with:

```python
                    timed_out = (time.monotonic() > deadline
                                 or _OPTIMIZE.get("cloud_failed"))
                    was_cancelled = _OPTIMIZE["cancel"]
                    # Cancel is its OWN trigger, checked on every 2-second poll — not
                    # only when the deadline passes (2026-08-06 spec). The salvage and
                    # the clean-stop both live in _cancel_cloud_job, called below once
                    # this lock is released.
                    if not was_cancelled and timed_out:
```

and re-indent the remainder of the former `if timed_out:` body (the
`have_shards` / `claim_shard_finalize` / `go_local` block) to sit under the new
`if not was_cancelled and timed_out:`. Delete the old inner
`if was_cancelled: ... return` — its job now belongs to `_cancel_cloud_job`, which
handles it identically when no shards arrived and better when some did.

**Do not otherwise change the timeout body.** Its behaviour for a non-cancelled run must
stay exactly as it is.

- [ ] **Step 3: Act on the cancel outside the lock**

Immediately after the `with _OPTIMIZE_LOCK:` block ends and before the existing
`if timed_out:` statement, insert:

```python
                if was_cancelled:
                    _cancel_cloud_job(job_id)
                    return
```

This must come **before** the `if timed_out:` handling so that a cancel arriving in the
same poll as a timeout takes the cancel path — Stop must never start a local run.

- [ ] **Step 4: Verify the timeout guard still reads correctly**

The existing line after the lock block is `if timed_out:`. Because `timed_out` is now
only assigned meaningfully when `not was_cancelled`, and the cancel branch returns
before reaching it, that line is safe as-is. Read the surrounding code and confirm
`claim_shard_finalize` and `go_local` are still assigned on every path that reaches
their use — if the re-indent left any path where they are referenced but unassigned,
fix it. Run the suite; an `UnboundLocalError` here would surface as a failure in
`tests/test_optimize_cloud.py`.

- [ ] **Step 5: Run the targeted tests, then the suite**

```bash
/usr/bin/python3 -m pytest tests/test_cloud_stop_immediate.py tests/test_optimize_cloud.py tests/test_optimize_cancel.py tests/test_shard_result_api.py tests/test_optimize_shard.py -v
/usr/bin/python3 -m pytest -q
/usr/bin/python3 -m pytest -k golden -v
```

Expected: all pass; suite at 782 passed, 1 skipped; golden passes **without**
regeneration. Never run `REGEN_GOLDEN=1`.

If a test in `test_optimize_cloud.py` fails, read it before editing: it exercises the
same loop, and a failure there most likely means the re-indent changed the timeout
path — which this task must not do.

- [ ] **Step 6: Confirm the diff is only what was intended**

```bash
git diff --stat
```

Expected: `api/main.py` only, and a small diff — one condition changed, one block
re-indented, one three-line call inserted, one obsolete branch deleted.

- [ ] **Step 7: Commit**

```bash
git add api/main.py
git -c commit.gpgsign=false commit -m "fix: Stop now ends a cloud run immediately

Cancel becomes its own trigger on the existing 2-second poll instead of
being checked only inside the timeout branch. Checked BEFORE the timeout
handling so a cancel racing the deadline never starts a local run.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Deployment note

`main` auto-deploys to a live factory. Unlike a scheduling change, this **cannot move a
plan** — it only ends a run sooner and salvages what that run already produced. The
applied plan is untouched in every path.

Do not push until the work is reviewed.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 the bug: cancel only honoured inside `if timed_out:` | 2 (Steps 2-3) |
| §2 the incident, 17 of 20 shards | 1 (`test_cancel_with_shards_finalizes_from_them`) |
| §2.1 worker cancel already works; gap is app-side completion | Global Constraints (worker + collector untouched) |
| §3 owner requirement: immediate stop, keep best so far | 1, 2 |
| §4 three cases (shards / none / no winner) | 1 (three named tests) |
| §4.1 `_finalize_from_shards` unchanged | Global Constraints |
| §4.2 must not leave `cloud_failed` for the watchdog | 1 (`test_cancel_never_leaves_cloud_failed_for_the_watchdog`) |
| §4.3 reuse the atomic `shards_finalizing` claim | 1 (helper body + `test_cancel_does_not_double_finalize`) |
| §4.4 state on completion | 1 (helper sets `state="failed"` only when still running) |
| §5 what does not change | Global Constraints + Task 2 Step 2 ("do not otherwise change") |
| §6 scope: `api/main.py` + one test file | File Structure |
| §7 test table, incl. the two regression rows | 1 (six tests), 2 (Step 5 runs the neighbouring cloud tests) |
| §8 risk: get §4.2 right | 1 (that test is named for it) |

No gaps.

**Placeholder scan:** no "TBD", no "add error handling", no "similar to Task N". Task 2
Step 4 asks the implementer to read code and confirm variable assignment after a
re-indent — that is a concrete verification with a named failure mode
(`UnboundLocalError`, surfacing in `test_optimize_cloud.py`), not a vague instruction.

**Type consistency:** `_cancel_cloud_job(job_id)` is the name in Task 1's helper, Task
1's six tests, and Task 2's call site. `_finalize_from_shards(job_id)` matches the
existing signature at `api/main.py:2550`. `shards_finalizing`, `cloud_failed`, `cancel`,
`shards` and `shard_total` are all existing `_OPTIMIZE` keys, spelled as in the current
code. Checked and consistent.
