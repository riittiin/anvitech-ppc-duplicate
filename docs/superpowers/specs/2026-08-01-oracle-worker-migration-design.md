# Oracle always-on Optimize worker — design

**Date:** 2026-08-01
**Status:** approved direction (owner), spec for review
**Scope:** WHERE the Optimize contest computes and HOW MUCH budget it gets. The
optimizer itself (search, scoring, gates, freeze, committed cap), the worker
*protocol*, and the B+C branch (parked, unmerged) are untouched.

## Why

Measured 2026-08-01 (deep-budget proof, Test8): raising the contest budget from the
ship-gate 600 to 2400 improved the CURRENT production system's winner **80,532 →
76,971 (−4.4%)** — the largest gain available today, with zero scheduling-code
changes. Compute is the lever. GitHub Actions (2 vCPU, 2,000 free min/month,
dispatch latency) caps how hard we can push it; Oracle Cloud's always-free tier
offers a **4-core ARM VM (24 GB RAM), no monthly quota, always on** — deep contests
in ~15–20 min. The contest is embarrassingly parallel (independent candidates;
`run_contest(processes=N)` already exists and is verified byte-identical to
sequential), so 4 cores ≈ 4× wall-clock, identical results.

**Owner decisions (2026-08-01):** fallback ladder Oracle → GitHub → local;
**everything deep** (manual deep search AND the daily Done contest — the owner
accepts the Done button blocking ~15–20 min); owner creates the Oracle account/VM
and runs the provided setup script.

## Current seam (verified in code)

`api/main._start_optimize` (cloud path): generates `job_id` (uuid), stores the full
contest payload in `_OPTIMIZE["cloud_payload"]`, calls `_dispatch_workflow`
(GitHub `workflow_dispatch` with the job_id; token `"manual"` skips the call), then
`cloud_job` watches: dispatch failure → local; no result within
`OPTIMIZE_CLOUD_TIMEOUT_MIN` (40) → local. The worker
(`scripts/cloud_optimize_worker.py`) authenticates every call with
`X-Worker-Secret` (`OPTIMIZE_WORKER_SECRET`): `GET /optimize/job/{job_id}` (payload
+ cancel flag), `POST /optimize/progress` (heartbeat; response carries Stop),
`POST /optimize/result`. Budgets: `optimize_service.cloud_budget` = 150/candidate
for the new engine (`CLOUD_NEW_BUDGET_PER_CANDIDATE`), 400 classic.

The GitHub worker is TOLD its job_id. A polling box must ASK — that discovery
endpoint is the only genuinely new protocol piece.

## Design

### 1. App side (api/main.py) — pending endpoint + claim + tiered dispatch

- **`GET /optimize/pending`** (worker-secret auth, same gatekeeper bypass list as
  the other worker endpoints): returns `{"job_id": ...}` when a cloud job is
  running, has a stored `cloud_payload`, and is **unclaimed**; else `{"job_id":
  null}`. Cheap (reads `_OPTIMIZE` under the lock; no store access).
- **Claim tracking:** `_OPTIMIZE["claimed"] = False` at job start; set `True`
  (under the lock) when the payload is actually fetched via
  `GET /optimize/job/{id}`. Idempotent; a GitHub worker claims the same way, so
  the flag is worker-agnostic.
- **Tiered dispatch** in the cloud path: `_start_optimize` no longer dispatches
  GitHub immediately. `cloud_job` first waits up to
  `ORACLE_CLAIM_TIMEOUT_MIN` (env, default **3**) for `claimed` — the Oracle
  poller's pickup window. Unclaimed at the deadline → `_dispatch_workflow`
  (GitHub) exactly as today → the existing overall watchdog
  (`OPTIMIZE_CLOUD_TIMEOUT_MIN`, 40) still falls back to local if no result ever
  arrives. Token `"manual"` behaves as today (no GitHub call — the tier simply
  waits out the watchdog). Nothing else in the watchdog/Stop/result flow changes.
- **Duplicate-worker safety** (Oracle claims late while GitHub also started): the
  existing `/optimize/result` supersede guard already applies — first result
  stored, second gets 409. Documented, no code needed.
- **Enable switch:** none needed. The endpoint existing is harmless; the 3-min
  claim wait only delays the GitHub tier. `ORACLE_CLAIM_TIMEOUT_MIN=0` restores
  today's immediate-GitHub behavior if the box is ever retired.

### 2. Deep budgets — one env knob

- `optimize_service.cloud_budget` gains an env override:
  `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` (int; unset/invalid → current defaults:
  150 new / 400 classic). Set on Render to **300** for the deep class: 12 overlap
  candidates × 2 machine-set waves × 300 = **7,200 plans** ≈ 3× the measured
  −4.4% run's total — comfortably deep, ~15–20 min on 4 cores.
- Applies wherever `cloud_budget` is consumed (payload build), so BOTH the manual
  deep search and the daily Done auto-contest run deep — the owner's
  "everything deep" decision. The Done button's block-and-wait UX is unchanged;
  its wait becomes ~15–20 min (owner-accepted).
- **Local fallback budgets unchanged** (`_OPT_BUDGETS` 1000): Render's 0.1 CPU
  cannot run deep; the fallback stays survival-mode.
- The progress denominator already derives from the payload's
  `budget_per_candidate` — no display change needed.

### 3. The Oracle worker (new, thin)

- **`scripts/oracle_optimize_worker.py`** — a poll loop around the existing worker
  logic: every `POLL_S` (10 s) call `GET /optimize/pending`; on a job_id,
  `subprocess` the EXISTING `scripts/cloud_optimize_worker.py` with
  `JOB_ID=<id>` (env), `n_procs = os.cpu_count()`; when it exits, resume polling.
  Before each job: `git fetch origin main && git reset --hard origin/main`
  (+ `pip install -r requirements.txt` when requirements changed) so the box
  always runs the code Render runs. Stdlib only. Errors never kill the loop
  (log + sleep + continue).
- **`scripts/oracle_worker_setup.sh`** — one-shot VM setup: install
  python3/pip/git, clone the repo (fine-grained **read-only** PAT the owner
  creates — the script prompts; the token lands only in the box's git remote and
  a root-only env file), write `/etc/anvitech-worker.env` (`APP_URL`,
  `OPTIMIZE_WORKER_SECRET`, `REPO_DIR`), install + enable a **systemd service**
  (`anvitech-optimize-worker.service`, `Restart=always`) running the poller.
  Survives reboot and crash.
- **`docs/ORACLE_WORKER.md`** — the owner runbook: create the always-free ARM VM
  (VM.Standard.A1.Flex, 4 OCPU / 24 GB, Ubuntu), open no inbound ports (the
  worker is outbound-only — polls the app; nothing can connect INTO the box),
  create the read-only PAT, paste the script, verify (`systemctl status`, a test
  Optimize run), and how to update/restart/retire the box.

### 4. Failure modes (each explicit)

| failure | behavior |
|---|---|
| Box down / rebooting | claim window (3 min) expires → GitHub tier → local watchdog. Button never dies. |
| Box crashes mid-job | claimed but no result → existing 40-min watchdog → local (same as a dead GitHub run today). |
| Render redeploys new engine code | box `git reset`s to `origin/main` before every job — never runs stale code against a new app. |
| App asleep (free Render) | worker's existing wake-tolerant retries (already built for GitHub) apply unchanged. |
| Both Oracle and GitHub compute one job | first `/optimize/result` wins; second 409s (existing guard). Wasted minutes only. |
| Secret mismatch | 403 on poll; poller logs and keeps polling — visible in `journalctl`, never crashes. |

### 5. Security

- Reuses `OPTIMIZE_WORKER_SECRET` (constant-time compare, existing). The pending
  endpoint leaks only a uuid job id — worthless without the secret.
- The box is outbound-only: no inbound ports, no SSH needed after setup, no app
  credentials beyond the worker secret; repo access is a read-only PAT.
- I never handle the owner's Oracle/GitHub credentials — the runbook has the
  owner create and paste them.

## Testing / ship gate

- Unit: `/optimize/pending` (secret required; null when idle/claimed; id when
  waiting), claim flag set by the job fetch, tiered dispatch (claimed-in-window →
  no GitHub call; unclaimed → dispatched; `ORACLE_CLAIM_TIMEOUT_MIN=0` →
  immediate), budget env override (set/unset/garbage), supersede guard unchanged.
- E2E (no Oracle needed): run `oracle_optimize_worker.py` against a local app
  (`GITHUB_DISPATCH_TOKEN=manual`) on the sample book — poller claims, computes,
  posts; the app applies. This mirrors the existing manual-mode E2E path.
- Ship gate: full suite green; the E2E proof; and after the owner's box is up, a
  real Test8 deep run end-to-end (press deep search → Oracle computes → result
  ~15–20 min → apply) before the GitHub tier is considered secondary.

## Out of scope (explicit)

Nightly scheduled deep search (a later, separate decision); merging the parked
B+C branch; GitHub Actions matrix fan-out (superseded by the box for now);
parallelizing the pin hill-climb (B+C component, parked).
