# Design — Analytics tab (utilization & bottlenecks from the current plan)

**Date:** 2026-07-09
**Status:** design approved (owner); correctness verification required before ship
**Branch:** `analytics-tab`

## Goal

A new **Analytics** tab that, from the latest plan, shows how fully each **machine**,
**operator**, and **process** is used — so the owner can see what's maxed out (bottlenecks)
and what's idle (optimize / rebalance). Live snapshot of the current plan; no history
stored. Numbers must be **real and verifiable**, unbiased across resources.

## Data source

Pure function `engine/analytics.py::build_analytics(schedule, masters, config, batches)`
returns a JSON-able dict. Consumed by `api/main.py` (added to the plan response, like the
Gantt/machine views) and rendered by a new `web/` Analytics tab. All math is in the pure
function so it is unit-testable and has no UI or state.

**Plan window:** `win_start = min(e.start)`, `win_end = max(e.end)` across the schedule.
The window is the calendar span of the whole plan; every utilization denominator uses it.

## The unbiased formula

Every resource is judged against **its own** available capacity in the window, never
against another resource:

```
Utilization % = Busy ÷ Available × 100
```

- **Busy** = sum of the resource's `occupancy_min` (the real machining/handling time).
- **Available** = the resource's own working minutes in `[win_start, win_end]` — its
  shifts and the working calendar (Thursdays/holidays excluded).

A single-shift manual station (≈9.5 h/day) and a two-shift CNC (≈19.5 h/day) are each
measured against their *own* day, so 60% means the same thing for both: "used 60% of what
it could have done." No bias.

## Section A — Machines

Exclude the `OS / Outsourced` and `Off-machine` lanes (not machines). For each real machine
used in the plan:

| Field | Formula |
|---|---|
| Busy (hrs) | `sum(occupancy_min) / 60` over the machine's entries |
| Available (hrs) | `clock_for(machine).working_minutes_between(win_start, win_end) / 60` |
| **Utilization %** | `Busy / Available × 100` (0 if Available == 0) |
| Idle (hrs) | `max(Available − Busy, 0)` |
| Ops | count of entries |
| Pieces | `sum(qty)` |
| Status | 🔴 bottleneck if util ≥ 85 · ⚪ under-used if util ≤ 30 · else healthy |

`clock_for` is Rule 6's own per-machine clock factory (`_clock_factory`), so the available
capacity matches exactly how the machine was scheduled (same shifts, same operator-coverage
window, same calendar). This is why machine utilization is precise and cross-checkable.

**Group rollup by machine type** (from `machine.machine_type`, e.g. all `CNC lathe`,
`Vertical Machining center`, manual): `group util = sum(group busy) / sum(group available)`.
Answers "how loaded are my CNCs / VMCs / manual stations overall."

## Section B — Operators (only when operator logic is on)

For each operator that appears on a scheduled entry:

| Field | Formula |
|---|---|
| Busy (hrs) | `sum(occupancy_min) / 60` over entries where `entry.operator == name` |
| Available (hrs) | `working_days(win) × shift_hours(operator.shift) / 60` |
| Utilization % | `Busy / Available × 100` |
| Ops, Pieces | counts as above |

`working_days(win)` = calendar days in the window that are working days (calendar-aware).
`shift_hours` from config: first shift = `first_shift` window length, second = second-shift
length (manual first-shift = 09:00–18:00). The shift assumption is stated on the tab. Busy
is exact; Available is a transparent shift-capacity estimate (documented on the tab).

## Section C — Processes (work distribution, not %)

A process isn't a capacity-bounded resource, so no utilization %. For each distinct process
name: total machine-hours (`sum(occupancy_min)/60`), **share of all plan work**
(`process hrs / total busy hrs`), ops, pieces, and the machines that run it. Shows which
**operations** consume the most capacity.

## Headline — "so what"

- **Bottleneck:** the highest-utilization machine (and operator) — the constraint to relieve.
- **Opportunities:** resources ≤ 30% — idle capacity to shift work onto.
- **Plan totals:** makespan (window length in working days & calendar days), total busy hrs,
  average machine utilization.

## Layout (frontend)

New "Analytics" tab. Vanilla HTML/JS + CSS bars (no chart library — matches the stack and
the strict CSP). Headline card on top; then three columns/sections (Machines w/ group
rollup, Operators, Processes), each a sorted CSS-bar list + a full detail table + the
existing CSV-download affordance. Bars color-coded by status.

## Architecture / boundaries

- `engine/analytics.py` — new, pure, ~one screen; the only place the math lives.
- `api/main.py` — call `build_analytics(...)` with the plan's schedule/masters/config/
  batches; attach the result to the response next to `gantt`.
- `web/app.js` + `web/style.css` — the Analytics tab renderer (tables via `to_table`-style
  columns, bars via CSS). `app.js` is already large; keep the analytics render in one
  focused function.

## Verification (correctness gate — required before ship)

The owner's explicit requirement: prove the numbers are **real**, not plausible.

1. **Hand-computed unit test:** a tiny fixed plan (2 machines, known cycle×qty+setup, known
   window) where Busy, Available, Utilization %, and group rollup are computed by hand in the
   test and asserted to the minute — so the formula is provably correct, not just non-crashing.
2. **Cross-check:** each machine's Busy (hrs×60) equals `build_machine_view`'s `Busy (min)`
   for the same machine (two independent code paths must agree).
3. **Invariants (property test on real Test4 plan):** every util is in `[0, 100]`; Busy ≤
   Available; `sum(process hrs) == sum(machine busy hrs)`; no NaN/inf; group busy == sum of
   member busy.
4. **Real-data readout:** run the full plan on Test4, print the analytics, and eyeball that
   the bottleneck/idle picture is sane (e.g. the machines we know are heavily loaded rank
   high) — shown to the owner before ship.
5. Full `pytest` green; golden trace unaffected (analytics is a new derived table, not a
   rule output).

## Edge cases

- **Empty plan** → empty analytics (tab shows "Plan first").
- **Provisional machine** (referenced by routing, not in master) → Available may be a
  two-shift default; flag "provisional (capacity estimated)".
- **Operator logic off** → no operators on entries → hide the Operators section (or show
  "operator logic off").
- **Available == 0** (a machine with no covered shift that somehow has entries) → util shown
  as "—", flagged, never divide-by-zero.

## Out of scope (deferred)
- Historical trends / time-series across plans (owner chose current-plan snapshot).
- Cost/throughput economics; what-if simulation ("add a shift to CNC1").
