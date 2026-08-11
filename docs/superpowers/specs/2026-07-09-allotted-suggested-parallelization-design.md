# Design — Parallelization uses Allotted vs Allotted∪Suggested

**Date:** 2026-07-09
**Status:** approved (owner), ready to plan
**Branch:** `split-allotted-suggested`

## Context

The item process master has two machine columns per process:
- **Suggested M/c** = every machine the item is *capable* of running on (the full set;
  may list alternatives like `CNC3/CNC6`).
- **Allotted M/c** = the machine(s) actually *allotted* / planned for that step.

Today Rule 6 chooses machines from **Suggested (or Allotted if Suggested is blank)**,
regardless of the parallelization toggle. The owner wants the **parallelization toggle
(`config.split_parallel`, the UI "distribute across machines" tick) to decide the set**:

| Parallelization | Machines the step may use |
|---|---|
| **OFF** | **Allotted only** (the planned choice). If Allotted is blank → fall back to Suggested. |
| **ON** | **Union of Allotted + Suggested** (Allotted first) — every capable machine. |

Worked example (allotted `CNC4`, suggested `CNC3/CNC6`, 1000 pcs):
- OFF → all 1000 on `CNC4`.
- ON → spread across `CNC4`, `CNC3`, `CNC6` to finish soonest.

### Data (Test4, 644 process blocks with a real machine)
- 572 have Suggested == Allotted → **no change** either way.
- 72 differ (65 of them: Allotted is one of the Suggested alternatives, e.g. sug
  `CNC3/CNC6`, allot `CNC3`) → **this is where the rule changes behaviour**.
- 8 have Allotted blank (Suggested only) → covered by the blank-Allotted fallback.

## Decisions (confirmed with the owner)

1. **OFF + blank Allotted → fall back to Suggested** (so the step still schedules; the
   8 blank-Allotted steps are not stranded).
2. **Keep the >400 split threshold.** ON widens the *candidate set*; a batch is only
   *physically split* when it exceeds `split_min_qty` (401). Smaller batches go whole to
   the least-busy machine in the set (no wasted second setup) — unchanged rule.
3. **Ties → the Allotted machine wins** (listed first in the union), keeping plans
   deterministic and respecting "Allotted = the planned choice".

## Design

The entire change lives in **`engine/rules/rule6_allocate._resolve_candidates`**, which
becomes **toggle-aware**. Everything downstream (earliest-free selection, the >400
parallel split, operator logic, the Gantt) consumes its result unchanged.

### `_resolve_candidates(proc, config)` — new logic

```
allotted  = parse_resource_candidates(proc.allotted_machine)
suggested = parse_resource_candidates(proc.suggested_machine)
if config.split_parallel:            # ON  → union, Allotted first, deduped, order-preserving
    return allotted + [c for c in suggested if c not in allotted]
else:                                # OFF → Allotted only; fall back to Suggested if blank
    return allotted or suggested
```

- A fully blank cell → `[]` (unchanged: the step is never invented onto a phantom
  station; if it also has no cycle time it's an off-machine milestone, else "needs machine").
- `config` is threaded to the three call sites in `run()`/`_allocate_op` that currently
  call `_resolve_candidates(proc)`.

### OS / off-machine detection stays toggle-independent

`_is_os` currently asks "does this step have a real machine?" via `_resolve_candidates`.
Once that resolver is toggle-aware, `_is_os` must **not** use it (OS-ness cannot depend
on the split toggle). Change `_is_os`'s real-machine check to look at the **union of
Allotted + Suggested directly**:

```
real = [c for c in (parse_resource_candidates(proc.allotted_machine)
                    + parse_resource_candidates(proc.suggested_machine)) if c != "OS"]
return not real and "OS" in normalize_process_name(proc.name).split()
```

`_is_offmachine` already keys on "any machine present" (`suggested or allotted`) and is
toggle-independent — **left unchanged**.

## Why the golden trace and existing tests are unaffected

The generated sample (`tests/sample_workbook.py`) sets **only the Suggested column**
(Allotted is always blank). So:
- OFF (the golden's default config) → `allotted or suggested` = **suggested** = today's
  `suggested or allotted`. **Identical** → golden unchanged.
- ON (the inspection-split test) → union(blank, suggested) = **suggested** = today's set.
  Identical.

The new behaviour only differs when Allotted is filled *and* differs from Suggested —
which the sample never has. New tests supply that case.

## Testing

- **OFF uses Allotted only:** routing with sug `CNC3/CNC6`, allot `CNC3`, split off →
  the step schedules on `CNC3` and **never** `CNC6`.
- **OFF + blank Allotted → Suggested:** sug `CNC3/CNC6`, allot blank, split off → runs on
  a suggested machine (not stranded).
- **ON uses the union:** allot `CNC4`, sug `CNC3/CNC6`, split on, 1000 pcs → all three
  machines appear in the split; **>400 splits**, **≤400 goes whole** to the least-busy of
  the three.
- **OS detection toggle-independent:** an `OS`-allotted step is OS with split on and off;
  a named-`OS` step with a real machine is never OS, either way.
- **Golden trace unchanged;** full `pytest` stays green (currently 210).

## Docs to update alongside the code

- `RULES.md` — Rule 6 alternative-machines / parallel-split section: document the
  Allotted-vs-union toggle semantics and the blank-Allotted fallback.
- `CLAUDE.md` — the `rule6_allocate.py` bullet.
