# Design — DISPATCH waits for every process to fully complete

**Date:** 2026-07-09
**Status:** approved (owner), ready to implement
**Branch:** `dispatch-waits-for-all`

## Problem

The Rule 5 overlap lets a downstream step start after 50% of the previous step's cutting
time. When a **fast** step follows a **slow** one (e.g. INSPECTION on 3 stations after
WASHING on one slow station), the fast step finishes *before* the slow one. The DISPATCH
milestone is then placed at the fast step's overlap point — so the order shows as
**dispatched before washing (an earlier, longer step) even finishes**. Physically the last
pieces can't be shipped before they're washed and inspected. Real example (item
`9611416370`, overlap ON): CNC OS 01→06 Jul, WASHING 06→**12** Jul, INSPECTION 08→10 Jul,
DISPATCH lands **10 Jul** — before washing's last pieces (12 Jul).

## Rule (owner-confirmed)

- **Keep the overlap rule unchanged** for every real process — a fast step *may* finish
  before a slower earlier step; that pipelining is desired ("if a step wants to move
  ahead, let it").
- **DISPATCH is the golden gate: it waits for the WHOLE order.** The dispatch milestone is
  placed at the **latest end across all of the batch's preceding processes** (not just its
  immediate predecessor, because with overlap an earlier long step can end last). So the
  order is "dispatched" only once **every piece has cleared every process**.

## Design

One targeted change in `engine/rules/rule6_allocate.run`'s per-batch pre-loop, in the
off-machine branch that emits milestones. When the off-machine step **is a DISPATCH step**
(matched by `orderbook.is_dispatch`, which tolerates the `DISAPTCH` misspelling), place the
milestone at the max end of this batch's already-scheduled entries instead of at
`s["ready"]`:

```python
        elif _is_offmachine(p):
            if is_dispatch(p.name):
                # DISPATCH = the finished-goods gate: wait for the WHOLE order. Overlap
                # can let a later step finish before an earlier long one, so use the
                # LATEST end across all prior processes, not the immediate predecessor.
                at = max((e.end for e in schedule if e.batch_id == s["batch"].batch_id),
                         default=s["ready"])
            else:
                at = s["ready"]
            schedule.append(ScheduleEntry(..., start=at, end=at, ...))
            ...
```

By the time DISPATCH is reached in the pre-loop, all earlier processes of the batch are
already scheduled (an in-house step only advances `s["next"]` after it is scheduled in the
main loop), so `schedule` filtered by `batch_id` holds every preceding process's end. The
`default=s["ready"]` covers a routing whose earlier steps were all skipped (per-process
remaining 0). Non-dispatch off-machine milestones (intermediate OS) keep today's placement.

`is_dispatch` is imported at module level from `engine.orderbook` (no import cycle:
orderbook imports only models + loaders).

## Why other behaviour and the golden trace are unaffected

- Overlap for every real process is untouched — a fast step still finishes early.
- The sample workbook (`tests/sample_workbook.py`) has **no DISPATCH step**, so the branch
  never fires for the golden → `tests/golden_trace.json` unchanged.
- Existing dispatch tests assert only that DISPATCH is a zero-duration `Off-machine`
  milestone (start == end) — still true; they don't pin its exact time.

## Testing

- **Fast-step-before-slow-step + dispatch:** routing SLOW → FAST → DISPATCH, overlap ON.
  Assert the fast step still ends before the slow one (overlap unchanged) **and** DISPATCH
  starts at the **slow step's end** (= max of all prior ends), not the fast step's overlap
  point.
- **Simple monotonic routing:** OP → DISPATCH still lands at OP's end (no regression).
- **Golden trace unchanged; full `pytest` stays green (219).**

## Docs to update

- `RULES.md` — Rule 6 DISPATCH / Rule 5 overlap: note DISPATCH waits for all processes to
  fully complete (overlap stays for every other step).
- `CLAUDE.md` — the `rule6_allocate.py` bullet.
