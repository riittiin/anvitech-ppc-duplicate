# Optimize settings sweep — auto-tune the overlap % (design)

**Date:** 2026-07-15 · **Status:** approved by owner · **Branch:** `optimize-settings-sweep`

## What it is

Today the **Optimize** button searches only the batch *sequence* under whatever
Settings the admin happens to have. The owner wants the settings themselves tuned:
when he clicks Optimize, the engine should also try the **overlap %** values and
return the combination of *sequence + overlap* that yields the best plan. One click,
no new controls to operate.

Owner decisions (2026-07-15):
- **Scorecard:** identical to Optimize — `total_late_days + 10 × makespan_days`
  (delivery gaps dominant). One definition of "best" everywhere.
- **Promises:** sweep even when Committed/Urgent orders exist, but a candidate
  overlap is eligible **only if the committed orders keep their promises at least
  as well as under the current setting** (guard on Pass 1, see below).
- **Scope:** overlap % only (candidates 50–100 step 10, plus the current value).
  The sweep helper is written so another dial is a small addition later.

## How the budget is spent (fair contest — one total budget, equal shares)

> **CONTRACT REWRITTEN (2026-07-15, same day, three times — regression, then
> two owner decisions).** (a) The shipped v1 gave the current setting only
> ~half the budget and a challenger dethroned that weakened result: on the
> real 65-order book (start 11-07) Deep returned **753 late-days / winner
> "Overlap 60"** where the pre-sweep button found **713 at Overlap 80** —
> unequal search depths misrank settings. (b) Owner: **"the best setting must
> win"** → every candidate at full depth (6×400 = 2,400 plans, ~1.5 hr on
> Render). (c) Owner: **too slow — ONE option, ≤ 1,000 plans total**, with as
> little quality loss as possible. Measured facts that shaped the final
> design: a cheap 100-eval ranking round picks the WRONG winner 2 times in 3
> (rank-then-deepen rejected); overlap **90/100 ranked last or next-to-last in
> every contest ever measured** on both real books → dropped from the
> candidate list. Result: 4 contenders × 250 plans ≈ the 2,400-plan contest
> (identical winner+plan on Test6@15-07; same winner −16 late-days on
> Test5@15-07; on Test6@11-07 picks 713 late-d/39.7 d vs full depth's
> 717/36.8) at 42 % of the compute.

> **CLOUD COMPUTE (same day, owner decision #3):** 1,000 plans is a compromise
> the owner rejected — he wants the FULL 2,400-plan contest, and Render's
> 0.1-CPU free tier can't parallelize (researched — no parallel capacity
> exists at $0 on Render). Solution: the contest runs on a **free GitHub
> Actions runner** (2 vCPU, 2,000 free min/month, the repo's own account).
> `_start_optimize` dispatches `optimize.yml` (workflow_dispatch) with a job
> id; the runner's `scripts/cloud_optimize_worker.py` fetches the book
> snapshot from `GET /optimize/job/{id}`, runs
> `optimize_service.run_contest` (contenders fanned across cores,
> per-eval progress via a shared counter), heartbeats `POST
> /optimize/progress` (the response carries the admin's Stop), and posts
> `POST /optimize/result`. All three endpoints authenticate with the
> `X-Worker-Secret` header (`OPTIMIZE_WORKER_SECRET`, constant-time compare,
> gatekeeper bypass). **Fallbacks — the button must always work:** dispatch
> failure → compute locally immediately; worker error report → local
> immediately; no answer within `OPTIMIZE_CLOUD_TIMEOUT_MIN` (default 20) →
> local. Cloud disabled (env vars unset) → pure local, exactly as before.
> `GITHUB_DISPATCH_TOKEN=manual` skips the GitHub call (manual/local worker).
> Cloud contest: `optimize_service.CLOUD_OVERLAP_CANDIDATES = (50…100)` ×
> `CLOUD_BUDGET_PER_CANDIDATE = 400` = the full 2,400. Local fallback: the
> 1,000-total split below. Deterministic ⇒ a cloud run is byte-identical to
> the same contest run anywhere.

1. **One button:** `budget_evals` is the TOTAL for LOCAL compute (1,000 ≈ 40
   min on the 0.1-CPU Render tier; legacy "quick" requests map to the same
   budget). With cloud compute configured the button runs the full 2,400-plan
   contest on GitHub instead (~8–10 min).
2. **Fair contest:** the budget splits EQUALLY across the contenders — the
   current overlap plus `OVERLAP_CANDIDATES = (50, 60, 70, 80)` — via
   `sweep_contenders`; `budget // n` plans each (1,000 → 250 each; a current
   setting outside the list, e.g. 90, joins as a 5th contender → 200 each).
   The best-scoring (sequence, overlap) pair wins outright.
3. **The current setting runs first** — an early **Stop** still leaves the
   user's own setting fully searched — and wins **exact ties** (no Settings
   churn). That is its only privilege; it has no depth advantage.
4. **Deterministic:** all seeds fixed, budgets are eval counts; cancellation is
   polled between evaluations across the whole sweep and keeps the best
   (sequence, overlap) found so far.

## Promise guard (Pass-1 eligibility)

With protected (Committed/Urgent) orders present, for each candidate overlap `v`:
run the unchanged Pass 1 (protected orders only) under `v` and compute
`promise_slip_metrics`. The candidate is eligible only if BOTH its total
promise-slip-days and its broken-promise count are **≤ the current setting's**
Pass-1 values. Ineligible candidates are skipped (recorded in the sweep table).
The open-pass search for an eligible candidate uses reservations from *that
candidate's* Pass 1, so the searched plan is exactly the plan Apply would produce.
An all-open book has no guard (every candidate eligible, `reserved=None`).

## Apply persists both — and stays transparent

- Apply saves the winning **ranks** (as today, `anvitech:plan_priority`) AND writes
  the winning **overlap % into the saved plan config** (`anvitech:plan_config`), the
  single source every Plan already loads. The Settings panel therefore openly shows
  the new value — no hidden state; users plan with it automatically.
- The persisted `inputs_sig` fingerprint is computed against the **winning** config,
  so the staleness banner stays quiet right after Apply and fires exactly when the
  admin later hand-edits Settings or re-uploads masters.
- The Optimize result panel adds one line: *"Best setting found: Overlap X%
  (currently Y%)"* (or notes the current setting already wins). `/optimize/status`
  exposes `best_overlap` for the UI.

## Where the code goes

- `engine/optimizer.py` — pure `sweep_optimize(so_lines, config, masters, *,
  budget_evals, seed, on_progress, should_cancel, candidate_setup, candidates)`:
  orchestrates probe-then-deepen over `replace(config, overlap_percent=v)`; the
  `candidate_setup(cfg) -> (reserved, eligible)` hook keeps API concerns (Pass 1,
  promise guard) out of the pure module. Returns the winning overlap, the merged
  best `OptimizeResult`, and a per-candidate table.
- `api/main.py` — `_start_optimize` calls `sweep_optimize` with a candidate_setup
  that builds Pass-1 reservations + enforces the promise guard; the result carries
  `best_overlap` + `inputs_sig` (winning config); `_optimize_apply` persists the
  overlap into the saved plan config.
- `web/app.js` — one line in the Optimize result panel; no new controls.

## Safety / testing

- No applied optimization → every plan byte-identical (golden untouched).
- New `tests/test_optimize_sweep.py`: sweep picks the better overlap on a book where
  overlap measurably changes the score; determinism (same inputs → same winner);
  never-worse (current always a candidate); ineligible candidates skipped; Apply
  persists overlap + `optimize_meta.inputs_changed` stays False right after Apply;
  promise guard rejects an overlap that breaks a committed promise.
- Full suite + golden must stay green; browser-verify the result panel line.
