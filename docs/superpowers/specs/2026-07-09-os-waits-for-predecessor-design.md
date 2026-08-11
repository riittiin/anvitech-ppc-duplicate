# Design — An OS step waits for its in-house predecessor to fully complete

**Date:** 2026-07-09
**Status:** approved (owner), ready to implement
**Branch:** `os-waits-for-predecessor`

## Problem

The Rule 5 overlap lets the next process start after 50% of the previous process's
cutting time — good when the next process is **in-house** (you machine the first half,
then the next station starts). But when the next process is **outsourced (OS)**, this is
physically wrong: you cannot ship parts to the vendor until the whole batch has cleared
the previous in-house step. Today an OS block starts at the predecessor's 50% overlap
point (confirmed with the owner via a simulation last session).

## Rule (owner-confirmed)

**When the next process is an OS step, its immediately-preceding process must run to
100% completion before the OS block starts** — the Rule 5 overlap does **not** apply into
an OS step, regardless of the overlap toggle. So for `process 1 (in-house) → process 2
(OS)`, all of process 1 (whether 20, 40, or 1000 pcs) finishes before outsourcing begins.

(OS steps were already fully sequential on the *output* side — a successor waits for the
whole OS block. This makes them fully sequential on the *input* side too.)

## Design

One targeted change in `engine/rules/rule6_allocate.run`'s per-batch ready-advancement
(currently lines 451-454):

```python
        s["next"] += 1
        if s["next"] < len(s["routing"].processes):
            if _is_os(s["routing"].processes[s["next"]]):
                elapsed = slow[4]        # OS successor: predecessor fully completes first
            else:
                elapsed = r5.elapsed_before_next(slow[4], slow[3], config)
            s["ready"] = slow[1].advance(slow[2], elapsed)
```

`slow` is `(end, clock, start, run_min, occupancy)`; `slow[4]` is the previous step's full
occupancy (cutting × qty + setup). Advancing from the step's start by its full occupancy
lands `s["ready"]` at the step's **end** — i.e. full completion — which the OS branch then
uses as the block's start. For a parallel-split predecessor, `slow` is the **slowest**
half, so "fully complete" correctly means all pieces machined.

**Scope of effect:** only changes the case *overlap ON **and** next step is OS*. In
sequential mode `elapsed_before_next` already returns full occupancy, so no change there.
In-house→in-house overlap is untouched. OS-as-first-process and OS→successor are
unaffected.

## Why the golden trace is unaffected

The generated sample (`tests/sample_workbook.py`) has **no real OS step** — its "CNC OS"
process has a real machine (`CNC1/CNC2`), so `_is_os` is False for it. No in-house→OS
transition exists in the sample, so the new branch never fires. Golden unchanged.

## Testing

- **In-house → OS, overlap ON:** the OS block starts at the predecessor's **end** (full
  completion), not its 50% point.
- **Contrast — in-house → in-house, overlap ON:** the second step still starts early
  (~50%), proving only the OS-successor case changed.
- **In-house → OS, overlap OFF:** OS still starts at predecessor's end (unchanged).
- **Golden trace unchanged; full `pytest` stays green (216).**

## Docs to update

- `RULES.md` — Rule 5 (overlap) / Rule 6 (OS): note that an OS step's predecessor fully
  completes before outsourcing starts (no overlap into an OS step).
- `CLAUDE.md` — the `rule6_allocate.py` bullet.
