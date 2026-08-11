# Freeze in-progress work, re-optimize the rest — daily restricted optimization

- **Date:** 2026-07-29
- **Status:** Design — approved in brainstorming, pending spec review
- **Author:** owner + Claude
- **Related:** `RULES.md`, `engine/new_engine.py`, `ppc_engine/scheduler/flow_scheduler.py`,
  `api/main.py` (`_plan`, `/optimize/done`, `_try_start_auto`, `_optimize_apply`),
  `engine/book_store.py`, `engine/optimize_service.py`
- **Supersedes for the "Done" cadence:** the 2026-07-22 feedback-triggered (Thursday-gated)
  optimize — the Thursday-only rule is removed by this design.

---

## 1. Why (context / problem)

On the Anvitech floor, the daily plan is followed only to ~70–80% — inefficient operators,
power cuts, an absent operator, tooling problems. That gap creates a **backlog** that must be
re-planned so it's absorbed efficiently. So we want the plan to **re-optimize every day**, as
the day's actuals are entered.

But an **unrestricted** daily re-optimization is unusable: when Tuesday's entry of Monday's
work triggers it, the optimizer re-decides every machine and operator — including for work that
is **physically running right now** on the floor. Yanking a half-machined part onto a different
machine is impossible, and reshuffling the live floor daily destroys schedule trust (the exact
reason the current design re-optimizes the job order only weekly).

**The fix:** a **restricted** daily optimization — a *frozen zone*. Whatever operation is
physically in progress right now is **frozen in place** (its machine, its operator, until its
remaining quantity is done); everything else is re-optimized **around** those frozen blocks.
The restriction is precisely what makes daily re-optimization safe, so we can drop the weekly
gate.

## 2. Goal / non-goals

**Goal:** On every "Done entering — update plan", run an auto-applying optimization that holds
the currently-in-progress operations fixed on their machine + operator and re-optimizes the
remaining backlog around them.

**Non-goals:**
- Not changing the *unrestricted* import-time optimize ("Start deep search", admin) — it stays
  as-is for fresh SO imports when nothing is running.
- Not adding any new floor data entry. The freeze is derived from data the app already has
  (the daily actuals + the last-applied plan).
- Not touching the objective/scoring, overlap tuning, or the sweep-contest machinery beyond
  passing the frozen set through it.

## 3. Locked decisions (from brainstorming)

| # | Decision |
|---|----------|
| 1 | **Cadence:** every "Done entering — update plan" runs the restricted optimize. The Thursday-only gate (`_is_optimize_day`) is **removed**. |
| 2 | **What freezes:** a routing step whose punches show it **partially done** — `0 < good qty < required`. Fully punched = done (already a zero-time milestone). Untouched = free to optimize. |
| 3 | **Machine** of a frozen step: from the **last-applied plan**. |
| 4 | **Operator** of a frozen step: from the **last-applied plan** (the *planned* operator). Absences are handled separately — an operator marked absent is not assigned, and the engine staffs a substitute; the machine stays pinned. |
| 5 | **Remaining quantity:** from the punches (`required − good`). |
| 6 | **Multiple in-progress parts on one machine** are possible; they resume in **previous-plan order**, and **all frozen work on a machine finishes before any newly-optimized work** goes on that machine. |
| 7 | **Setup on resume:** none — the machine is already set up mid-run. |
| 8 | **Shift crossing:** the machine stays pinned; the pinned operator covers only their shift; if the remaining work crosses a shift boundary it **hands off** to a qualified operator on the new shift (the engine's normal per-shift handoff). |
| 9 | **Engine mechanism:** pre-place the frozen ops in the decoder, then schedule everything else around them (Approach A below). |
| 10 | **Persistence:** save the applied schedule (per-op machine/operator/time) and the frozen set at apply time. |
| 11 | **Edge cases (all non-blocking):** no prior plan → behave unrestricted; a frozen step whose saved machine is now missing / re-routed → schedule it normally + report; OS/outsourced steps → never frozen; planned operator absent → machine pinned, engine staffs a substitute. |

## 4. The daily cycle (data flow)

```
Floor enters Monday's actuals   → Save (instant, no re-plan — unchanged)
Floor clicks "Done entering — update plan":
  a. Read the LAST-APPLIED plan (the schedule the floor is following).
  b. From the fresh punches, find every step that is partially done (in progress).
  c. For each, look up its machine + operator from the last-applied plan → the FROZEN SET.
  d. Persist the frozen set (anvitech:frozen_ops).
  e. Run the optimize contest — EVERY candidate plan pins the frozen set.
  f. Auto-apply the winner if strictly better; save its schedule as the new last-applied plan.
Rest of the day: the on-screen plan = saved ranks + the saved frozen set, so what everyone
sees on the tabs matches what was optimized. The next Done recomputes the frozen set fresh.
```

## 5. Computing the frozen set

Input sources:
- **Last-applied schedule** (new persistence, §7): `(order-key, op_seq) → machine, operator,
  start`.
- **Daily actuals** (existing): good qty per `(SO#, item, process)`.
- **Order book / routing** (existing): remaining qty per step (`process_remaining`), and the
  process-name → `op_seq` mapping via the item's routing.

Detection, per active order line and step:
- **In progress (freeze)** ⟺ `good qty > 0` **and** `remaining qty > 0`.
- Fully done (`remaining == 0`) → not frozen (already a milestone). Untouched (`good == 0`) →
  not frozen.

For each in-progress step, resolve the frozen block:
- **machine, operator** ← last-applied schedule for that `(order-key, op_seq)`.
- **remaining qty** ← order book (`process_remaining[op_seq]`).
- If the step is **not found** in the last-applied schedule (a brand-new order, or the plan
  didn't include it), or its saved machine is **OS/off-machine**, or the saved machine no
  longer exists in masters → **do not freeze it**; it schedules normally and is listed in a
  non-blocking report row.

Group the frozen blocks **by machine**, ordered by their **last-applied start time** (=
previous-plan order). This ordering is what the pre-placement pass replays.

### Batch / consolidation mapping (design consideration)

The scheduler works on **batches** (Rule 1 consolidates same-item, near-due SO-lines;
`consolidation_window = 1 day`), and the last-applied schedule is expressed in batch/entry
terms (`ScheduleEntry.batch_id, item_code, process_seq`). Punch detection is per
`(SO#, item, process)`. The frozen set must therefore be keyed to the **same batch identity**
the decoder uses: map the in-progress SO-line → the batch that covers it (via the order book /
`batch.source_so_refs`) → `op_seq` via the routing → the last-applied entry. **Risk to resolve
in the plan:** a batch that consolidates an in-progress SO-line with a not-yet-started one is
*partially* frozen; the frozen block's qty is the in-progress remaining, and the rest of that
batch/step schedules normally. In practice an already-started order is unlikely to consolidate
with a brand-new one, but the mapping and the partial-batch case must be covered by tests.

## 6. Engine mechanism — Approach A: pre-place frozen ops in the decoder

`ppc_engine/scheduler/flow_scheduler.py::decode` gains a `frozen` argument (a per-machine,
previous-plan-ordered list of frozen blocks: `order-key, op_seq, machine_id, operator,
remaining_qty`). Threading follows the existing `reserved=` (absence) path:
`api._plan` / contest → `run_forward` → `pipeline.scheduler_for` → `new_engine.run` → `decode`.

**Before** the Giffler-Thompson loop:
1. For each machine, lay its frozen blocks in previous-plan order, starting at
   `config.plan_start`, on the **pinned machine + operator**, **no setup**, for the block's
   remaining qty. Reuse `_lay_on_machine` with the machine forced and the operator preferred
   (falling back to normal staffing when the pinned operator is unavailable/absent, and handing
   off across shift boundaries exactly as today).
2. Commit those segments to `staffing`; advance `machine_free[machine]` to the end of that
   machine's last frozen block (so **all frozen work precedes new work** on the machine).
3. For each affected order, advance its op index **past** the frozen op and set its `ready` /
   `prev_end` to the frozen block's end (so **downstream steps wait** for the frozen step).

Then the existing GT loop schedules everything else around the pre-placed state — no change to
its logic. Determinism is preserved (the pre-pass is a pure function of its inputs).

`frozen = None`/empty must be **byte-identical** to today (regression-guarded), so a fresh
import or any plan with nothing in progress is unaffected.

**Rejected alternatives:**
- *Reserve machine/operator time windows.* A reservation makes the optimizer **avoid** the
  slot, but a frozen op must **occupy** it — reservation alone places the frozen op elsewhere.
  Insufficient.
- *Two-pass schedule-then-offset.* More complex and doesn't hold the machine busy during the
  second pass.

## 7. Persistence

New durable keys (via `engine/book_store.py` + `engine/storage.py`):

- **`anvitech:last_applied_schedule`** — a compact projection of the applied schedule:
  per operation `(order-key, op_seq) → machine, operator, start, end`. Written **only when an
  optimize result is applied** — both the unrestricted import optimize (`_optimize_apply`) and
  the daily restricted one (auto-apply). **Not** overwritten on ordinary display re-plans (that
  would let it drift with new actuals and stop being "the plan the floor followed").
- **`anvitech:frozen_ops`** — the frozen set computed at the current Done, so the display
  `_plan` reproduces the optimized schedule for the rest of the day. Recomputed at the next
  Done.

**Rejected:** reconstructing yesterday's plan by replaying yesterday's ranking (drifts — uses
today's actuals); capturing the machine at punch time (rejected by owner — no extra data entry).

## 8. Contest, cloud, and display consistency

- The frozen set is passed into **every contest candidate** (same shape as absences), so the
  winner is chosen under the real constraint.
- It **round-trips to the cloud worker** in the optimize payload
  (`optimize_service.build_payload` / `parse_payload`), so a cloud run stays byte-identical to
  a local run.
- The display `_plan` applies the saved frozen set, so the contest result, the auto-applied
  plan, and every tab (Gantt, Schedule, Orders, Analytics, downloads) show the same schedule.

## 9. Cadence change

- `POST /optimize/done` **no longer gates on `_is_optimize_day()`** — it always runs
  `_try_start_auto()` (which still skips when a contest is already running or nothing material
  changed). Every day's Done = restricted optimize.
- `_is_optimize_day` / `_OPTIMIZE_WEEKDAY` are removed (or retired) along with the
  `not_optimize_day` client branch.
- Auto-apply stays **strictly-better-or-nothing** (`_auto_apply_result`).
- "Start deep search" (`POST /optimize`, admin) is unchanged — the unrestricted button for
  fresh imports.

> Note: `_OPTIMIZE_WEEKDAY` is currently the TEMP Sunday (`6`) testing override. This design
> removes the weekday gate entirely; the temp value becomes moot.

## 10. Edge cases (all non-blocking, fail-localized)

| Case | Behaviour |
|---|---|
| No last-applied plan yet (first run / fresh import) | Empty frozen set → unrestricted optimize (matches import behaviour). |
| Frozen step not found in last-applied plan / saved machine now missing / step re-routed | Not frozen; schedules normally; listed in a non-blocking report row. |
| Saved assignment is OS / off-machine | Never frozen (off-site, no in-house machine/operator). |
| Planned operator marked absent | Machine stays pinned; engine staffs a substitute for the frozen block. |
| Multiple frozen blocks on one machine | Previous-plan order; all before new work. |
| Punches show more good qty than the previous plan expected (floor ran ahead on a step) | Trust the punches for remaining qty. |

## 11. Testing (TDD)

Crafted cases + independent validators (mirroring `tests/test_new_engine.py` /
`tests/test_flow_scheduler.py` style):
- A partially-punched step stays on its saved machine + operator, no setup, for its remaining
  qty.
- Multiple frozen blocks on one machine resume in previous-plan order, all before new work.
- A downstream step waits for its frozen predecessor's end.
- `frozen=None` → schedule **byte-identical** to today (regression).
- No last-applied plan → unrestricted.
- Absent planned operator → machine pinned, substitute staffed, no double-booking.
- Batch/consolidation: a partially-frozen batch (in-progress line + fresh line same item)
  behaves correctly.
- Invariants hold: one operator per machine per shift (Rule 1); no operator double-booking;
  no operation finishes before its predecessor.
- Cloud payload round-trip: local == cloud with a frozen set present.
- API: `/optimize/done` starts the restricted optimize on any weekday; the last-applied
  schedule and frozen set persist and are read back.

## 12. Documentation to update (before code, per workflow)

- **`RULES.md`** — add the freeze constraint as an explicit scheduling rule (the frozen zone).
- **`CLAUDE.md`** — the "Done" cadence change (Thursday gate removed), the two new store keys,
  and the `decode(frozen=...)` capability.

## 13. Open items to settle in the implementation plan

1. Exact batch ↔ SO-line mapping for the frozen set, and the partial-batch case (§5).
2. Overlap into a frozen op's successor: keep normal overlap based on the frozen op's remaining
   qty, or make the successor wait for the frozen end. Default proposal: **normal overlap**
   (minimal special-casing); confirm during TDD if it produces anything physically odd.
3. Where the frozen-set computation lives (app layer `api/main.py`, using the order book +
   last-applied schedule) vs. what gets threaded into the pure engine (just the resolved
   blocks) — keep the pure engine free of app/store knowledge.
