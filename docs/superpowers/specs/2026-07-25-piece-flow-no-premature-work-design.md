# Piece-flow correctness: no step's work before its input exists (2026-07-25)

## Problem

The production ("new") engine schedules each routing step as **one continuous block**.
With overlap a downstream block starts early; a *fast* step (deburring, chamfer,
inspection, packing) finishes its whole quantity **before** its slow predecessor (VMC)
has produced the last pieces. Reproduced on Test8 (B028): VMC FIRST SIDE's real work
runs 28-07 → 04-08, but DEBURING's real work windows are 29-07 + 31-07 (done by 31-07) —
so on the **machine-wise / shift-wise schedule** the last VMC pieces are deburred before
they physically exist ("deburring skipped for the last jobs").

The 2026-07-25 span-pacing fix (`new_engine._entries_from_schedule`) corrected the
**Gantt bar / completion date** (extended each op's displayed `end` to its predecessor's),
but the **work windows (`op_segments`) still precede piece availability**. This spec
fixes the underlying schedule.

## The rule (one invariant)

> **A step's work may not finish before its predecessor's work finishes.** A fast
> downstream step, instead of doing all its work early, runs as a **batch that finishes
> with (just after) its predecessor** — so every piece is processed only after it exists.

Operationally realistic: you don't staff the deburring station for 6 days to do 20 h of
work; you let pieces accumulate and deburr the batch near the end. Keeps the new engine's
**operator-stability** (one contiguous block, one operator per machine per shift) and its
**speed** (still block scheduling — NO per-piece chunking; owner-chosen over the ~5×-slower
full piece-flow).

## Behaviour & impact (measured claims to verify)

- **Makespan / lateness (MEASURED on Test8, correcting the initial "hours" estimate):**
  optimized makespan ~52.5 d → **55.56 d (+~3 d, ~6%)**; late-days ~1214 → **1323 (+~109,
  ~9%)**. This is a **correction, not a regression**: the old numbers were physically
  infeasible — they "beat" the schedule by processing downstream pieces before the
  predecessor produced them. Forbidding that (reality) makes some orders genuinely finish
  later. The delta is the truth the old plan hid; the owner makes delivery commitments off
  these numbers, so the honest ones are the ones to keep. (Batch-at-end also concentrates
  downstream machine use late → some cross-order contention; full piece-flow would recover
  a little *makespan* but lands on ~the same late-days at 5× compute — not worth it.)
- **Optimizer:** it scores by completion; completions become physically honest (a touch
  later), so the search may pick a marginally different sequence — consistent, not
  destabilising. Verified: sequence + makespan delta on Test8 before/after.
- **Consistency across overlap (70/80/90/93…):** the invariant is overlap-independent —
  enforced at every step for any overlap. Lower overlap trips it less, higher more; the
  rule is identical, and the optimizer keeps tuning overlap freely.
- **Speed:** O(≤5) extra placement attempts per *starved* op only; negligible.

## Design

In `ppc_engine/scheduler/flow_scheduler.py::decode`, the loop already tracks
`prev_end_of[key]` = the order's last-scheduled op's paced completion. After the winning
op is placed and *before* it is committed:

```
if op is in-house (has a machine) AND placement["end"] < prev_end_of[key]:
    # starved fast op — re-lay it later so its WORK finishes >= the predecessor's end
    delayed_ready = ready_of[key]
    for _ in range(5):                       # converges fast; shift boundaries need a few
        delayed_ready += (prev_end_of[key] - placement["end"])
        placement = _place_operation(op, order, delayed_ready, machine_free, staffing, masters, config)
        if placement["end"] >= prev_end_of[key]:
            break
```

Then commit `placement` exactly as today (staffing, `machine_free`, segments, load), and
compute `paced_end`/`ready_of` from the *re-laid* placement (so the next op's overlap uses
the delayed start). `_place_operation` reads `machine_free`/`staffing` read-only, so
re-laying the winner is safe; the block simply lands later, same machine, same operator
rule, same occupancy.

**Why this is enough:** a starved op (work < predecessor's remaining) laid to end at
`prev_end` processes its pieces in `[prev_end − work, prev_end]`; the predecessor produced
them in `[pred_start, prev_end]` with `prev_end − work > pred_start`, so every piece is
already produced when processed. OS/DISPATCH steps are already fully sequential (they can't
precede) and are skipped by the `has a machine` guard.

## WIRING MAP

### Wires INTO
| Point | File | Change |
|---|---|---|
| Decode placement loop | `ppc_engine/scheduler/flow_scheduler.py::decode` (after the Giffler-Thompson winner pick, before commit) | the re-lay block above |

### Depends on (unchanged contracts)
| Dependency | Why |
|---|---|
| `_place_operation(op, order, ready, machine_free, staffing, …)` | re-used verbatim for the delayed re-lay (read-only on shared state) |
| `prev_end_of[key]` | the predecessor's paced completion — the target the op must not finish before |
| `_lay_on_machine` forward-lay + `iter_windows` | later `ready` ⇒ later block (monotonic), so the loop converges |

### Consumers (get the corrected schedule for free)
| Consumer | Via | Effect |
|---|---|---|
| `new_engine._entries_from_schedule` → Gantt / **machine-wise & shift-wise CSV** | `op_segments` now land in the correct (post-production) window | the printed floor schedule no longer shows work before its pieces exist |
| `analytics` (operator/machine load) | `op_segments` / `occupancy_min` unchanged in TOTAL, only shifted later | utilization totals unchanged; timing correct |
| `optimizer` (`plan_metrics`) | completion honest | small, consistent sequence/number shift — measured, not assumed |

### Unchanged (explicitly)
- Occupancy (machine + operator busy minutes) — same work, later placement.
- One-operator-per-machine-per-shift — same block, one operator; no over-commitment (we do
  NOT stretch a segment to hold an idle operator).
- The span-pacing in `new_engine._entries_from_schedule` — stays as a belt-and-suspenders
  (after this fix, op ends already respect precedence; pacing becomes a no-op but is kept).
- `scheduler="classic"`/`"flow"` — untouched; golden trace byte-identical.

## Tests / regression
- `tests/test_new_engine.py` — new invariant: for every order, each op's **op_segment work
  end ≥ its predecessor's op_segment work end** (not just the entry `end`). RED on Test8-style
  data today, GREEN after.
- ppc_engine-level: a crafted slow→fast routing under high overlap; assert the fast op's
  segments start/finish after the slow op's production, and completion ≈ unchanged.
- Test8 verification (manual, in the plan): B028 DEBURING work now runs after VMC produces;
  0 op-segment precedence violations across all orders; makespan delta reported.

## Out of scope
- Full per-piece flow / chunking (owner deferred — 5× slower).
- Spreading a starved op's work thinly across the window (batch-at-end is the chosen model).
