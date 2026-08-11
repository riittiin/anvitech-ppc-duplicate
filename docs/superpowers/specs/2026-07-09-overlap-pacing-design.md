# Design — Overlap pacing: a step can't finish before its predecessor delivers

**Date:** 2026-07-09
**Status:** approved (owner delegated), ready to implement
**Branch:** `dispatch-waits-for-all` (extends the dispatch fix already on it)

## Problem

The Rule 5 overlap lets a downstream step **start** after 50% of the previous step's
cutting time. But it computes that step's duration as if the **whole batch** were
available from its start — so a **fast** step (e.g. INSPECTION on 3 stations) placed after
a **slow** one (WASHING on one station) *finishes before the slow step*. Concretely for
item `9611416370`: WASHING 06→**12** Jul, INSPECTION 08→**10** Jul. The pieces washed on
11–12 Jul can never be inspected (inspection already ended) — they'd be dispatched
un-inspected. Physically impossible: a step can only process pieces as fast as its
predecessor delivers them (it is *starved*).

## Rule (owner-confirmed)

Keep the overlap **start** (steps begin early — the schedule stays compressed/fast), but a
step **cannot finish before its predecessor has delivered the last piece**. A fast step is
paced by its slow predecessor and finishes **just after** it, never before. Combined with
the already-shipped "DISPATCH waits for the whole order", every piece flows
predecessor → successor → dispatch in a consistent order.

## Design

One change in `engine/rules/rule6_allocate.run`, right after `_allocate_op` returns a
step's entries and before they are emitted (~line 437). Hold the step's completion to the
latest end of the batch's already-scheduled earlier processes:

```python
        entries, _blk = _allocate_op(...)
        # Pace by the predecessor: a step may START early (overlap) but cannot FINISH
        # before every earlier process of this batch has delivered the last piece (a fast
        # step is starved by a slow one). Extend the entries' end to that latest prior end
        # — the step stays engaged with this batch until then; its work (occupancy) is
        # unchanged, only its span grows (idle waiting for pieces).
        prev_end = max((e.end for e in schedule if e.batch_id == batch.batch_id), default=None)
        if prev_end is not None and entries:
            naive_end = max(en for _, _, _, en, _ in entries)
            if prev_end > naive_end:
                entries = [(m, q, st, prev_end, op) for (m, q, st, en, op) in entries]
```

- The emission loop then uses the (possibly extended) `entries` unchanged: `machine_free`,
  the `ScheduleEntry.end`, and `slow` all pick up the paced end; `occupancy_min` stays the
  real work (`cyc*q+setup`), so the Gantt bar span grows but the busy-time doesn't.
- Because each step is now held to ≥ every earlier step's end, ends are **monotonic** —
  the immediate predecessor is always the latest earlier step, so "latest prior end" is the
  correct pacing bound.
- The overlap **start** (`s["ready"]`) and the >400 split are untouched. OS steps (handled
  in the pre-loop) already start at their predecessor's full end, so they're consistent.
- Interaction with **DISPATCH waits for all** (same branch): with monotonic ends, dispatch
  lands at the last real step's paced end — the order is dispatched only once everything is
  genuinely done.

## Golden trace WILL change (intentional)

The sample (`tests/sample_workbook.py`) has a fast INSP after a slower `CNC OS`-named
in-house step, so pacing extends INSP's end. This is a deliberate rule change:
**regenerate** the golden (`REGEN_GOLDEN=1 python3 -m pytest -k golden`) and eyeball the
diff — the only changes should be fast steps' ends extended to their predecessors', and
the downstream effects of that (no unrelated rows).

## Testing

- **Pacing:** SLOW → FAST (fast overlaps), overlap ON. Assert `fast.start < slow.end`
  (overlap start kept) **and** `fast.end == slow.end` (paced — can't finish first).
- **Dispatch (updated):** SLOW → FAST → DISPATCH — dispatch lands at the slow step's end
  (= the paced fast end = the latest process).
- **Overlap contrast still holds:** in-house → in-house where the successor is *slower*
  keeps overlapping normally (unchanged).
- **Golden regenerated + eyeballed;** real item `9611416370` verified (INSPECTION ends
  ~12 Jul, DISPATCH after it, all 1500 pcs through every step, overlap start preserved).

## Docs to update

- `RULES.md` — Rule 5 overlap: add the pacing rule (start early, finish no earlier than the
  predecessor).
- `CLAUDE.md` — the `rule6_allocate.py` bullet.
