# Feedback-triggered re-optimization — design (2026-07-22)

> **Supersedes the cadence half of the 2026-07-18 scheduled-optimize design** (that
> spec has since been pruned as superseded).
> That design deliberately *removed* event-triggered auto-optimization (uploads,
> deletes, the Done button, etc. no longer started a contest) and replaced it with
> a twice-weekly GitHub cron (Mon & Fri 11:00 IST), on the rationale that
> re-sequencing the floor on every change destroys schedule trust. **The owner has
> reset the cadence (2026-07-22):** the re-optimization fires from the **"Done
> entering — update plan"** button, but **only on Thursday** (the weekly off day) —
> the owner enters Wednesday's feedback on Thursday and re-optimizes then, so the new
> schedule is ready for Friday. On every other day the button just refreshes the plan
> with the latest **facts** (no re-sequencing). It blocks until the contest finishes,
> auto-applies the winner, and reflects it in the Gantt / schedule / analytics. The
> auto-apply, cloud→local fallback, one-at-a-time, and book-fingerprint-skip machinery
> from the 2026-07-18 design are **kept and reused** — the *trigger* changes from a
> fixed cron to the Thursday Done click, and the old Mon/Fri cron is removed.

## Why

The owner's operating rhythm: each working morning the previous day's shift output
is punched as feedback. **Thursday is the weekly off day** — Wednesday's shifts are
punched Thursday. The plan should always reflect the latest **facts** (produced /
remaining quantities) as feedback comes in, every day — but the **re-optimization
(re-sequencing) should fire only once a week, on Thursday**: the owner enters
Wednesday's feedback on the off day and re-optimizes then, so the new schedule is
ready for **Friday**, without re-sequencing the floor mid-week (which destroys
schedule trust). A run takes ~8–10 min on the free cloud tier and the owner
explicitly chose to **wait** for it (watching live progress) rather than have it
apply silently.

## Trigger and flow

The **"Done entering — update plan"** button in Capture Actuals (available to both
the admin and the user/operator role) is the trigger. Its behavior is **gated by
the weekday (IST):**

- **On any non-Thursday day:** it does **not** re-optimize — it just calls
  `runPlan(false)`, so the plan/schedule/Gantt refresh with the latest facts. No
  contest, no wait. (This is the plain plan-refresh the button always did.)
- **On Thursday (the weekly optimize day):** it fires the full auto-applying
  re-optimization contest described below.

The manual admin **"Start deep search"** button is unaffected — an admin can run a
manual optimization on any day.

On a **Thursday** click:

1. Start a full optimization contest on the current book — which already includes
   the feedback just punched (the contest loads current actuals).
2. Show **live progress** next to the button (`#optimize-done-status`): plans tried
   / budget and best-so-far, refreshed by polling `GET /optimize/status`. **Pure
   block-and-wait — no Stop button** (owner decision, 2026-07-22): once Done is
   clicked the run goes to completion. (Admins retain the pre-existing Settings
   optimize-panel Stop, which operates on the same shared contest; the feedback
   flow itself adds no cancel control.)
3. On completion, the winner is **auto-applied iff strictly better** than the plan
   currently applied (`_auto_apply_result` — unchanged), and the client calls
   `runPlan(false)` once so the Gantt, schedule, Rule-6 allocation, and analytics
   all refresh together to the new optimized plan.
4. A one-line note is written (`book_store.save_auto_note`, IST-stamped) and shown
   on the Orders tab / `/run`'s `auto_note`: e.g. *"Plan re-optimized 11:04 — 44
   late-days (was 52), overlap 80 → 85"* or *"Checked 11:04 — current plan still
   best (52 late-days)."*

This is a **rewiring**, not new engine code: the contest runner (`_start_optimize`),
cloud→local fallback, progress polling, and strictly-better auto-apply already exist.

### UX caveat (accepted by the owner)

Each run is ~8–10 min on GitHub's free cloud, so every feedback session ends with an
8–10 min wait before the schedule flips. The live progress bar makes this a visible
wait rather than a frozen spinner. Per the owner's block-and-wait decision there is
no Stop control in the feedback flow — the run always completes. (To reduce
*unnecessary* waits, `_try_start_auto` skips starting a contest at all when nothing
material changed since the last one it ran — see Server changes.)

## Server changes

### New endpoint: `POST /optimize/done`

- **Auth:** any logged-in role (no `require_admin`) — same access as the feedback
  form itself. The operators entering feedback must be able to trigger it. CSRF is
  handled by the existing `gatekeeper` middleware for unsafe methods.
- **Weekday gate:** the endpoint first checks `_is_optimize_day()` (today, IST, is
  Thursday — `_ist_today().weekday() == _OPTIMIZE_WEEKDAY`, `_OPTIMIZE_WEEKDAY = 3`).
  If it is **not** the optimize day, it returns `{"started": False, "reason":
  "not_optimize_day", "state": …}` **without** starting or deciding a contest — the
  client then just `runPlan(false)`s (facts refresh). Only on the optimize day does
  it proceed to `_try_start_auto()`. The response carries a `reason`
  (`"not_optimize_day"` | `"started"` | `"skipped"`) so the client can message
  correctly. (The admin manual `/optimize` path is unaffected — no weekday gate.)
- **Fingerprint skip** (optimize day only): if the current book+inputs signature matches **either** the
  last applied optimization's saved signature **or** the last *searched* signature
  (Done clicked twice with no new feedback between), return a "no new feedback — plan
  unchanged" note **without running** a contest. The last-searched marker
  (`anvitech:last_searched`, `book_store.save_last_searched`/`load_last_searched`) is
  written by `_finalize_optimize` after **every** completed contest — captured from
  the book snapshot taken at contest *start*, so a punch mid-run still forces a
  re-run. This is what makes a redundant click skip even when the last contest found
  **no improvement** (nothing gets applied in that case, so the applied-plan
  signature alone would miss it — final-review finding, fixed 2026-07-22). Reuses
  `_current_book_sig()` / `_applied_plan_meta()` / `_inputs_signature`.
- **Start:** otherwise start `_start_optimize(_OPT_BUDGETS["deep"], "auto",
  background=True, auto=True)`. `auto=True` gives auto-apply on completion via
  `_finalize_optimize` → `_auto_apply_result`. `_start_optimize` already handles
  cloud dispatch with a **local fallback** (capped budget), so — unlike the removed
  cron path — this endpoint is **not** cloud-only: the button always does something.
- **One-at-a-time:** if a contest is already running, `_start_optimize` raises 409;
  the endpoint surfaces that and the client attaches to the running run (polls to
  completion, then refreshes). No second run is queued.

The shared "should we run, and start it" logic (fingerprint skip + one-at-a-time +
start-auto) is factored so `/optimize/done` and any future caller use one path;
`_try_start_auto` (cron-only, cloud-gated) is removed.

### Removed

- `.github/workflows/scheduled-optimize.yml` — the Mon/Fri cron. Deleted.
- `POST /optimize/scheduled` + `_try_start_auto()` — the cron's server entry point.
- `nextScheduledOptimize()` (web/app.js) and the two **"Next optimization: <date>"**
  UI strings (status strip + Done-button status). Replaced with a static line, e.g.
  *"Optimization runs when you finish entering feedback."*

### Kept unchanged

- The manual admin **"Start deep search"** button (`POST /optimize`) and its
  Apply/Discard panel — a manual, non-auto-applying run stays available.
- All cloud-worker endpoints (`/optimize/job|progress|result`), the engine, the
  `_plan` replay of saved ranks, and the staleness banner.

## Edge cases

- **Operator (non-admin) clicks Done:** allowed (endpoint not admin-gated).
- **Contest already running:** attach to it and refresh on completion; no queued
  re-run (YAGNI — click Done again if newer feedback must be included).
- **Nothing changed since last run:** skipped with a friendly message, no cloud burn.
- **Cloud unavailable:** capped local search runs so the button still works.
- **Stop mid-run:** keeps best-so-far, applied if better (existing behavior).

## Tests (`tests/test_auto_optimize.py`)

- `/optimize/done` starts an auto-applying contest when the book changed.
- `/optimize/done` skips (no contest started) when the fingerprint matches the last
  applied plan; writes the "no new feedback" note.
- `/optimize/done` is reachable by **both** roles (admin + user), unlike admin-only
  `/optimize`.
- Auto-apply only when strictly better (existing test, retargeted to the new path).
- Remove the tests asserting the twice-weekly `/optimize/scheduled` trigger.
- Full suite stays green (currently 508 passing).

## Docs

- Update the CLAUDE.md scheduled-optimize bullet(s) to describe the feedback trigger
  and the removal of the cron / `/optimize/scheduled` / `nextScheduledOptimize`.
- This spec records the reversal of the 2026-07-18 scheduled-optimize cadence.
