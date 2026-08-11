# GitHub Actions matrix fan-out for the Optimize contest — design

**Date:** 2026-07-31
**Status:** approved direction (owner), spec for review
**Scope:** WHERE the cloud Optimize contest computes — fan its independent
candidates across ~20 free parallel GitHub-hosted runners instead of one, and
raise the budget so a deep contest finishes in ~15 min. The optimizer itself
(search, scoring, gates, freeze, committed cap, machine-set dimension), the
worker-auth protocol, and the app's single-result finalize contract are
otherwise untouched.

## Why

The repo is now **public**, so GitHub Actions gives **unlimited free minutes and
20 concurrent jobs**. Compute is the measured lever (deep budget 600→2400 was
−4.4% on Test8). The contest is embarrassingly parallel: `run_contest` already
fans its `(overlap, machine_set)` candidates across local CPU cores via an
`mp.Pool`. Moving that fan-out from cores to runners multiplies compute ~10× at
zero cost. Owner decision (2026-07-31): spend it on **more depth** — search far
more plans per optimize, targeting ~15 min wall-clock (owner already accepted
15-20 min), on **every** optimize (manual Deep Search and the daily Done).

## Current seam (verified in code, 2026-07-31)

- `.github/workflows/optimize.yml` — `workflow_dispatch` only, one input
  `job_id`; single job runs `python scripts/cloud_optimize_worker.py`; env
  `APP_URL`, `OPTIMIZE_WORKER_SECRET`, `JOB_ID`. `concurrency: optimize`.
- `scripts/cloud_optimize_worker.py::main` — `GET /optimize/job/{JOB_ID}` →
  payload; `optimize_service.run_contest(payload, processes=cpu_count,
  on_progress, should_cancel)`; heartbeat `POST /optimize/progress`; final
  `POST /optimize/result {job_id, winner_overlap, winner_flexible, ranks, best,
  rows, evals, cancelled}`.
- `engine/optimize_service.py`:
  - `run_candidate(payload, overlap, flexible, *, on_progress, should_cancel) ->
    {overlap, flexible, eligible, best, evals, ranks, cancelled}` — **the
    self-contained unit of work**; rebuilds the book from the payload and
    searches one candidate.
  - `run_contest` builds `jobs = [(payload, ov, flex) for flex in machine_sets
    for ov in contenders]` and pools them; new engine: 12 overlaps × 2
    machine-sets = **24 candidates**.
  - `pick_winner(current_overlap, current_flexible, rows) -> winner row` — pure
    reduce primitive; lowest `optimizer.score(r["best"])`, current wins exact
    ties. Operates on any concatenation of candidate rows.
  - `build_payload(...)`/`parse_payload(...)`, `cloud_budget(config)` (env
    `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` override), `cloud_candidates(config)`.
- `api/main.py` — `_start_optimize` stores `_OPTIMIZE.cloud_payload` + a uuid
  `job_id` + `claimed=False`; `_dispatch_workflow` fires the GitHub dispatch;
  `cloud_job` waits the `ORACLE_CLAIM_TIMEOUT_MIN` claim window then dispatches,
  with an `OPTIMIZE_CLOUD_TIMEOUT_MIN` (40) watchdog → local fallback. The
  result handler `optimize_result_ep` expects **exactly one** POST keyed by
  `job_id`; the first finalizes and the job leaves `running`, so a second 404s.
  **This single-result assumption is the one thing the matrix must adapt.**

## Constraint that shapes the design: public repo = public Actions

On a public repo, **workflow logs and uploaded artifacts are downloadable by
anyone.** The contest payload (the order book: SO numbers, item codes,
quantities, due dates, masters) and the result rows/ranks are **commercially
sensitive**. Therefore:

1. **No order data may cross into GitHub.** Only the random uuid `job_id` is
   passed as a workflow input. The payload is fetched at runtime by each runner
   over the authenticated `GET /optimize/job/{id}` call and lives only in that
   runner's memory.
2. **No GitHub artifacts.** Shard results must NOT be uploaded as artifacts (a
   `needs:`-style GitHub reduce job is rejected for this reason). Each runner
   posts its rows **directly back to the app** over authenticated HTTPS; the
   app merges. Nothing sensitive touches public GitHub storage.
3. **The worker must never print payload/rows/ranks to stdout** (public logs) —
   only counts and the job_id.

This is why the reduce happens **app-side**, not in a GitHub job.

## Design

### 1. Sharding — the worker computes a slice (`scripts/cloud_optimize_worker.py`)

The worker reads two new env vars: `SHARD_INDEX` (0-based) and `SHARD_TOTAL`
(matrix size). It:

- Parses the payload and derives the **full candidate list** in the exact same
  order `run_contest` builds it (`[(ov, flex) for flex in machine_sets for ov in
  contenders]`) — via a new shared helper `optimize_service.contest_jobs(payload)
  -> list[(overlap, flexible)]` so worker and `run_contest` cannot drift.
- Selects its slice by **round-robin**: candidates where `i % SHARD_TOTAL ==
  SHARD_INDEX`. (24 candidates over 20 shards → shards 0-3 get 2, rest get 1.)
- Runs its slice with the existing local pool (`run_contest` restricted to the
  slice, or direct `run_candidate` calls using `processes=min(len(slice),
  cpu_count)`), so a 2-candidate shard still finishes in ~1-candidate wall time
  on a multi-core runner.
- Reports progress via `POST /optimize/progress {job_id, shard_index, evals,
  best}` (see §3).
- Posts its rows via a **new** `POST /optimize/shard-result {job_id,
  shard_index, shard_total, rows, evals, cancelled}` (see §2). `rows` is the
  list of its candidates' `run_candidate` dicts (incl. `ranks`).
- **Absent env vars → today's behavior**: `SHARD_TOTAL` unset/`1` means the
  worker runs the whole contest and posts to the legacy `/optimize/result` (the
  Oracle-box path and the manual `GITHUB_DISPATCH_TOKEN=manual` E2E stay
  byte-identical). The shard path activates only when `SHARD_TOTAL > 1`.

### 2. App-side collector — a new endpoint + merge (`api/main.py`)

- **`POST /optimize/shard-result`** (worker-secret auth, same gatekeeper-bypass
  allowlist as the other worker endpoints). Under `_OPTIMIZE_LOCK`:
  - Ignore if `job_id != _OPTIMIZE["job_id"]` or the job is not `running`
    (stale/late shard → 200 no-op, never 404-crash a straggler).
  - Accumulate into `_OPTIMIZE["shards"][shard_index] = {rows, evals,
    cancelled}` (idempotent on shard_index; a duplicate overwrites, harmless).
  - When **all `shard_total`** shards have reported → **finalize**: concatenate
    every shard's rows, call `pick_winner(current_overlap, current_flexible,
    all_rows)` **once over the global set** (so the "current wins ties"
    privilege is applied globally, not per shard), sum `evals`, OR `cancelled`,
    and run the existing `_finalize_optimize(...)` with the merged winner —
    exactly the shape today's single `/optimize/result` produced.
- **Watchdog finalize (partial-safe):** `cloud_job`'s existing
  `OPTIMIZE_CLOUD_TIMEOUT_MIN` deadline still applies. If the deadline passes
  with **≥1** shard reported, finalize over whatever arrived (each candidate is
  independent, so a missing shard only means those candidates went unsearched —
  the best of the rest is still a valid plan). If **zero** shards arrived → set
  `cloud_failed=True` → local fallback (unchanged).
- **Legacy `/optimize/result` stays** for the whole-contest (Oracle/manual)
  path; it is unchanged. The two finalize routes share `_finalize_optimize`.

### 3. Progress across shards

Each shard posts `/optimize/progress` with its own `shard_index` + `evals`.
`optimize_progress_ep` stores per-shard evals in `_OPTIMIZE["shard_evals"][idx]`
and reports the **sum** across shards as the headline `evals`. The denominator
is the whole-contest total — `len(contest_jobs(payload)) × budget_per_candidate`
(total candidates × per-candidate budget), independent of how candidates are
distributed across shards, so round-robin's uneven 1-or-2-per-shard split does
not skew it. `best` shown = the best-scoring `best` seen so far across shards. The Stop flag in the progress response is unchanged; every shard polls
it and aborts on cancel. No UI change — the same progress bar, now summing.

### 4. The workflow — a matrix (`.github/workflows/optimize.yml`)

- Add `strategy.matrix.shard: [0,1,…,SHARD_TOTAL-1]` (a static list; **20**).
  `max-parallel: 20`. `fail-fast: false` (one shard dying must not cancel the
  rest). Keep `concurrency: optimize` and `workflow_dispatch`/`job_id`.
- Pass `SHARD_INDEX: ${{ matrix.shard }}` and `SHARD_TOTAL: 20` env into the
  worker step. Everything else (checkout, python, `pip install`, secrets)
  unchanged. Per-job `timeout-minutes: 25`.
- `SHARD_TOTAL` is defined once in the YAML; the worker reads it from env and
  echoes it in each shard-result post, so the app learns it from the shards
  (no app/YAML constant to keep in sync beyond the dispatch).

### 5. Depth / budget

- Keep the single knob `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` (env, Render). With
  24 candidates each on ~its own runner, a much larger per-candidate budget now
  fits in ~15 min. **Approach:** ship the matrix, run one real Test8 deep
  end-to-end, measure per-candidate wall time, then set the env to the value
  that lands the whole contest at ~12-15 min. Expected ~2-4× today's 300
  (≈700-1200), i.e. the "much deeper" contest the owner chose. No code constant
  — the number is operational, set on Render.
- Local fallback budget (`_OPT_BUDGETS`) unchanged (Render's 0.1 CPU stays
  survival-mode).

### 6. Dispatch tier (unchanged mechanics, one env note)

`cloud_job`'s tiered dispatch is untouched. Since no Oracle box exists, the
owner sets **`ORACLE_CLAIM_TIMEOUT_MIN=0`** on Render so GitHub dispatches
immediately (no dead 3-min wait). Documented in the deploy note, not code.

## Failure modes

| failure | behavior |
|---|---|
| One shard runner dies | `fail-fast:false` keeps the rest; app finalizes over arrived shards at the watchdog (its candidates unsearched, best-of-rest applies). |
| All shards fail / none report | watchdog → `cloud_failed` → local fallback (unchanged). |
| Reduce (app-side) never reaches all shards | watchdog finalize on partial; ≥1 shard = a valid winner. |
| Dispatch fails (token/API) | existing `_dispatch_workflow` false → local fallback. |
| Stop pressed | every shard reads cancel from its progress response, aborts, posts `cancelled`; app finalizes cancelled over what it has. |
| A late/duplicate shard posts after finalize | job no longer `running` → 200 no-op (never crashes). |
| Public-log exposure | only `job_id` (uuid) crosses to GitHub; payload/rows over authenticated HTTPS only; worker prints no order data. |

## Security

- **Trigger:** `workflow_dispatch` via the API needs `actions:write`
  (`GITHUB_DISPATCH_TOKEN`); forks/strangers cannot dispatch. No `pull_request`
  trigger → no fork-PR runs. Unchanged by the matrix.
- **Secrets:** `OPTIMIZE_WORKER_SECRET`, `APP_URL`, `MONGODB_URI` are repo
  Actions secrets — masked in public logs, unavailable to fork PRs.
- **Data plane:** payload + results travel only over authenticated HTTPS
  (`X-Worker-Secret`, constant-time) between the app and the runners; nothing
  order-derived is uploaded as an artifact or printed to logs. The new
  `/optimize/shard-result` uses the same auth + allowlist as the existing worker
  endpoints; it leaks nothing on a bad/missing secret (403).

## Testing / ship gate

- Unit: `contest_jobs` order matches `run_contest`'s job order; round-robin
  slicing covers every candidate exactly once across shards with no overlap for
  a range of (candidates, shard_total); `/optimize/shard-result` auth (403
  without secret), accumulation, all-arrived finalize == a single whole-contest
  `run_contest` winner on the same book (byte-identical winner), partial-arrival
  watchdog finalize picks the best of arrived, stale/duplicate shard no-ops;
  progress summing; `SHARD_TOTAL` unset → legacy whole-contest path unchanged.
- **Equivalence test (the key one):** for the sample book, the sharded merge
  (run each candidate slice, concatenate rows, `pick_winner`) selects the
  **same winner** as an unsharded `run_contest` — proving the fan-out is
  result-identical, only faster.
- E2E (no GitHub needed): drive the sharded worker locally over the manual seam
  (`GITHUB_DISPATCH_TOKEN=manual`) with `SHARD_TOTAL=4` against a local app on
  the sample book — 4 "shards" post, the app merges and applies; matches the
  unsharded result.
- Ship gate: full suite green; the equivalence + E2E proofs; then one real
  Test8 deep run end-to-end after deploy, measured, budget tuned to ~15 min.

## Out of scope (explicit)

Merging the parked B+C branch; the Oracle box (retired direction); a nightly
scheduled deep search; changing the score/gates/freeze; any UI change beyond the
existing progress bar (which is reused as-is).
