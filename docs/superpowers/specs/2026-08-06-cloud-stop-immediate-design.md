# "Stop & keep best" must stop a cloud run immediately — design

**Date:** 2026-08-06
**Status:** design approved, not implemented
**Scope:** the cloud wait loop in `api/main.py` only. No engine, scheduler, UI, workflow or worker change.

---

## 1. The bug

Pressing **Stop & keep best** during a cloud optimize run does nothing for up to 40
minutes, and then produces no plan.

`api/main.py`, inside the cloud wait loop:

```python
                time.sleep(2)
                with _OPTIMIZE_LOCK:
                    if (_OPTIMIZE["state"] != "running"
                            or _OPTIMIZE["job_id"] != job_id):
                        return
                    timed_out = (time.monotonic() > deadline
                                 or _OPTIMIZE.get("cloud_failed"))
                    was_cancelled = _OPTIMIZE["cancel"]
                    if timed_out:                    # <-- cancel is only acted on IN HERE
                        if was_cancelled:
                            _OPTIMIZE.update(state="failed", cancel=False,
                                             error="stopped: the cloud run did not "
                                                   "answer before the timeout")
                            return
```

`_optimize_cancel()` sets `_OPTIMIZE["cancel"] = True`. The loop reads it every two
seconds into `was_cancelled` — and then only acts on it inside `if timed_out:`. So a
cancel raised at minute 2 sits unhonoured until the `OPTIMIZE_CLOUD_TIMEOUT_MIN`
deadline (default **40 minutes**) expires.

**Not a regression.** `git blame` dates these lines to **2026-07-15**. The
2026-08-06 objective merge did not touch `api/main.py` (verified: `git diff` of that
merge against the file is empty). The bug simply had not been hit, because every prior
cloud run finished in 9–11 minutes and nobody needed to stop one.

## 2. The incident that exposed it (2026-08-06)

A cloud contest dispatched 20 matrix shards. GitHub's free tier allocated 17 runners
and never found capacity for shards **14, 18 and 19** — their GitHub job records carry
an **empty step list**, i.e. they never executed a single step, and were cancelled after
waiting 17–21 minutes.

The app therefore waited on results that would never arrive. Live status at the time:

```
state=running  mode=cloud  evals=14586/16800  elapsed=1811s
cancelled=False  stopping=True  best=EMPTY
```

`stopping: True` with `cancelled: False` is the signature of this bug: the request was
recorded, and nothing acted on it.

**17 of 20 shards had already reported and were on the point of being discarded.**
That is 87% of a 16,800-plan contest thrown away.

### 2.1 Why the existing worker-cancel mechanism did not save it

Cancel propagation to *workers* already exists and works. A worker heartbeats to
`POST /optimize/progress`, whose response carries `{"cancel": bool}`; on seeing it the
worker sets `state["cancel"] = True`, which reaches the search as `should_cancel`, and
the worker posts its best-so-far
(`scripts/cloud_optimize_worker.py:97-98, 113, 124`). None of that is broken.

The gap is **app-side completion**. The shard collector only finalizes when
`len(_OPTIMIZE["shards"]) >= req.shard_total` — i.e. when **all 20** shards have
reported. Shards 14, 18 and 19 never started, so they can never report, so that
condition can never be met. The 17 delivered shard results therefore sit in
`_OPTIMIZE["shards"]` with nothing willing to finalize them: the collector is waiting
for a count it will never reach, and the wait loop is ignoring the cancel that should
have released them.

This is why the bug bites specifically when shards die. With all 20 healthy, the
collector finalizes on its own and the unhonoured cancel is invisible.

## 3. Owner requirement

> "It should immediately stop and give the best option till it is calculated to that
> point."

Plus, ruled the same day: when **no** shards have arrived, Stop ends the run cleanly and
starts nothing. Stop always means stop; it must never silently begin a fresh
computation.

## 4. Design

Make `cancel` its own trigger in the wait loop, evaluated on the existing two-second
poll rather than only inside the timeout branch. Three cases:

| On cancel | Action |
|---|---|
| Shards have arrived | Atomically claim `shards_finalizing`, call `_finalize_from_shards(job_id)`, end. |
| No shards | End immediately with a plain message. The applied plan is untouched. |
| Shards arrived but the merge yields no eligible winner | End cleanly. Do **not** fall back to local. |

### 4.1 Why the salvage path already works

`_finalize_from_shards(job_id)` needs no change. Its own docstring states it is *"Safe
to call from the collector (all-arrived) and the watchdog (partial)"* — it merges
`list(_OPTIMIZE["shards"].values())`, whatever subset is present, and sums their
`evals`. Partial salvage is an existing, exercised capability that the cancel path
simply never reached.

### 4.2 The one subtlety: `cloud_failed` must not trigger a local run

When the merged shard set has no eligible winner, `_finalize_from_shards` sets
`_OPTIMIZE["cloud_failed"] = True`. That flag exists so the **watchdog** falls back to a
local search. Under cancel that is exactly wrong — it would turn Stop into "start a
fresh 15-minute computation".

The cancel path must therefore consume that outcome itself: after calling
`_finalize_from_shards`, if the job is still `running` (i.e. no winner was produced),
end it with the same clean message as the no-shards case rather than leaving
`cloud_failed` for the watchdog.

### 4.3 The atomic claim is reused, not reinvented

`shards_finalizing` is claimed under `_OPTIMIZE_LOCK` in the same locked block that
reads it, exactly as the timeout branch does. This is mutually exclusive with the
`/optimize/shard-result` collector's own claim of the same flag, so a shard landing at
the same instant as the cancel can never cause a double finalize. The cancel path
follows that established pattern rather than introducing a second mechanism.

### 4.4 State on completion

A cancelled run that produced a winner ends in the same state a normal completion does,
so the existing Apply flow works unchanged and the result is visibly a partial one:
`cancelled` is set on the result, which `_optimize_status()` already surfaces, and the
UI already renders. A cancelled run that produced nothing ends with `state="failed"` and
an `error` string, matching how the existing timeout-with-cancel case reports.

## 5. What deliberately does NOT change

- **`_finalize_from_shards`** — already partial-safe (§4.1).
- **`/optimize/shard-result`** — already guards on `state == "running"`, `job_id` match
  and `not shards_finalizing`, so results still arriving from live runners after a
  cancel are ignored safely. No race is introduced by leaving them running. Its
  all-arrived completion condition (`len(shards) >= shard_total`) is also left alone:
  it is correct for the healthy case, and the cancel path is what covers the case where
  the total can never be reached (§2.1).
- **`/optimize/progress` and the worker cancel loop** — already correct (§2.1). Workers
  learn about a cancel and post their best-so-far unaided. Nothing on the worker side
  changes, and `scripts/cloud_optimize_worker.py` is not touched.
- **The GitHub run is not cancelled.** Unnecessary: late results are ignored, and Actions
  minutes are free on a public repo. Cancelling would require a new API call and broader
  token permissions for no behavioural benefit.
- **`max-parallel: 20`** in `.github/workflows/optimize.yml` — owner decision 2026-08-06,
  explicitly keep at 20. The workflow is not touched.
- **Local-mode Stop** — already works: `optimizer.optimize` polls `should_cancel()`
  between evaluations and keeps the best so far. Untouched.
- **The timeout path** — its behaviour is unchanged for a non-cancelled run.

## 6. Scope

`api/main.py`, the cloud wait loop, plus one new test file. Nothing else.

**Not reusing the existing `fix-optimize-stop-cancel` branch.** It exists but is based on
a much older `main` — it predates the commitment-feature gate and the operator-rotation
removal, and merging it would drag back stale code. The fix is written fresh against
current `main`.

## 7. Testing

| Case | Expected |
|---|---|
| Cancel with shards present | `_finalize_from_shards` called once; run ends with a winner built from the arrived shards |
| Cancel with no shards | Run ends immediately; clear message; **no local fallback started**; applied plan untouched |
| Cancel where the merge yields no winner | Run ends cleanly; **`cloud_failed` does not trigger a local run** |
| Cancel races a shard landing | Exactly one finalize — the `shards_finalizing` claim holds |
| Timeout without cancel | Unchanged: salvage if shards, else local fallback |
| Local-mode cancel | Unchanged |

The first three are the requirement; the fourth is the concurrency guard; the last two
are regression guards on paths this change must not disturb.

**Full suite must stay green** (currently 776 passed, 1 skipped) and the golden trace
must pass without regeneration.

**Environment note for implementation:** use `/usr/bin/python3`, not plain `python3`.
The homebrew Python on this machine has numpy 2.5.1, which removed `numpy.short`;
openpyxl 3.1.5 still references it, so `import openpyxl` raises there and pytest cannot
load `conftest.py`.

## 8. Risk

Low, and lower than leaving it. The change adds a branch to a loop that currently
ignores a user request; it cannot make a run worse than being stuck for 40 minutes and
yielding nothing.

The one thing to get right is §4.2 — swallowing `cloud_failed` under cancel. Get that
wrong and Stop starts a local search, which is the exact behaviour the owner rejected.

`main` auto-deploys to a live factory, so this ships as a deliberate release like any
other. Unlike the objective change, it alters no scheduling logic: it cannot move a plan,
only end a run sooner and salvage what a run already produced.
