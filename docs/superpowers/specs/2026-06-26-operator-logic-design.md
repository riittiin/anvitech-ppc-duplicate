# Operator logic + full master ingestion — Design

**Date:** 2026-06-26 · **Branch:** `operator-logic` · **Status:** approved (Model A)

## Goal
Ingest every field of the 3 master sheets and make scheduling honor the shop's
operator + shift reality: each activity needs a qualified operator on the right
shift, and a machine's working window depends on its Available Hrs/Day. Everything
Excel-driven — re-upload reflects.

## Decisions
1. **Operator model = A (coverage gate).** A machine may run during a shift iff ≥1
   operator who (specializes in that machine) AND (is assigned to that shift) exists.
   Operators are NOT tracked as one-at-a-time scarce resources (that's a future B).
2. **Working window by Available Hrs/Day:**
   - `>= two_shift_threshold` (default **12**) → two-shift machine: **08:00–19:00 +
     19:00–05:00**.
   - else (9.5 group) → single-shift: **09:00–18:00** only (the owner's "treat 9.5 as
     9am–6pm"). Applies to band saw / milling / drilling / all manual stations.
   - **blank/None** Available Hrs → default **two-shift** (back-compat). **explicit 0**
     → unschedulable, reported, never fed to the clock.
3. **Specialty → machine match** = a specialty token equals the machine's `machine_no`
   **OR** its normalized `machine_type` (so `Milling M/c`→MM1, `Manual Deburring`→Anturam
   both match via normalized type). Specialty tokens matching neither are **reported**
   (unmatched-specialty) so the owner can fix naming; they have no scheduling effect.
4. **Enforce toggle** `enforce_operator_coverage`: engine default **OFF** (keeps the
   golden trace + existing rule tests byte-identical), web UI default **ON** (mirrors
   the `apply_downtime_to_plan` asymmetry). When ON, an op whose machine has no covered
   window is **not scheduled** and is surfaced in a `NO_OPERATOR_COVERAGE` report; when
   OFF, coverage is advisory (machines use their full eligible window).
5. **Provisional machines** (routing-referenced, not in the master) → default two-shift
   window AND **bypass the coverage gate** (report-only) so nothing silently vanishes.
6. **rule3 slack stays on a fixed reference window** (`WorkClock.from_config`, legacy
   two-shift) so batch priority doesn't shuffle when an operator's shift changes.

## Effective machine window (pure helper)
`machine_windows(masters, config) -> {machine_id: {"intervals": [...], "covered_shifts":
set, "blocked": bool, "reason": str}}` plus an unmatched-specialty list.

Per machine:
- eligible shifts from classification: two-shift → {First, Second}; single → {Manual}.
- covered shifts = shifts S such that ∃ operator with `shift == S` and a specialty
  matching this machine (by no/type). (Manual eligibility is covered by a **First**-shift
  operator — manual runs first shift only.)
- intervals = union of covered eligible shift windows (in minutes-from-midnight; an end
  > 1440 means "crosses midnight"). First=08:00–19:00 `(480,1140)`, Second=19:00–05:00
  `(1140,1740)`, Manual=09:00–18:00 `(540,1080)`. Adjacent merge so both-shift → `(480,1740)`
  = today's window.
- blocked = (intervals empty) and not provisional.

## WorkClock refactor (behavior-preserving)
Generalize from one global `(start_hour,end_hour)` to a **list of day-relative intervals**.
- `WorkClock.from_config(cal, config)` returns the legacy `[(480, 1740)]` → existing
  behavior byte-identical (golden unchanged).
- `WorkClock(cal, intervals)` for per-machine windows.
- `_windows_for_day` yields all of a day's intervals; `_next_window`, `advance`,
  `working_minutes_between` iterate intervals.
- **Empty intervals raise a typed `NoWorkingWindow`** immediately (no 800-day loop);
  rule6 catches it → mark op blocked.

## rule6 wiring (behind the toggle)
- memoized `clock_for(machine_id)` built from that machine's intervals (or the legacy
  clock when the toggle is OFF / machine has no entry).
- candidate feasibility uses each candidate's own clock; an op listing alternatives picks
  the earliest **covered** candidate; fully-uncovered candidates are dropped (noted).
- **rule5 handoff fix:** the next op's `ready` is advanced on the **producer** machine's
  clock (the machine that cut), then snapped onto the consumer's clock.
- **downtime seed** uses the machine's own clock.
- **blocked-op path:** when enforce is ON and an op's every candidate is uncovered →
  emit `NO_OPERATOR_COVERAGE` (op + machine + reason), skip the op (and its batch's
  downstream), never crash.
- `build_machine_view` idle/utilization uses each machine's own clock.

## Model + loader + config + API
- `models.Machine.available_hrs_per_day: float|None` + derived `is_two_shift(threshold)`;
  `models.Operator.shift: str` (First/Second/""); update `as_row()`.
- `loaders._load_machines` reads the Available Hrs/Day column; `_load_operators` reads the
  per-operator **Shift** column. NOTE: the operator table has a "Shift" header (col C) AND
  the side-by-side shift-definitions table has a "Shift" header (col E) — bind the
  operator shift to the **first/leftmost** "shift" match (the operator column). Shift
  start/end times optionally read from the definitions table into config (defaults kept).
- `config`: `enforce_operator_coverage: bool=False`, `two_shift_threshold_hours: float=12.0`,
  `manual_start_hour=9`, `manual_end_hour=18`, `second_shift_end_hour=5` (exists),
  first/second boundary 8/19. Extend `validate()`. Web UI sends `enforce_operator_coverage=True`.
- `api._augment_helpers`: a **coverage table** (machine → window → covered shifts /
  "needs operator") and the `NO_OPERATOR_COVERAGE` + unmatched-specialty surfacing on the
  Rule 6 tab; plumb the toggle through `_load_plan_config`/run.

## Tests (test-first)
- worktime: legacy `from_config` == old behavior (golden unchanged); single-shift window;
  second-only crossing midnight; empty intervals raise `NoWorkingWindow`.
- coverage helper (pure): both-shift covered → merged window; CNC7 second-only; manual op
  whose only operator is second-shift → blocked; specialty by type-name matches; unmatched
  specialty reported; provisional bypass; blank avail → two-shift; explicit 0 → blocked.
- rule6 with `enforce_operator_coverage=True` + operators/shift data: covered machine runs,
  uncovered blocked + reported, alternative picks covered one.
- existing rule6/golden tests stay green (engine default OFF, legacy window).
- sample workbook: add a per-operator **Shift** column + an operator for one 9.5 machine so
  coverage is testable; regenerate the golden (expect NO diff with the toggle OFF).

## Edge cases (must pass)
uncovered machine; CNC7 second-only; manual-second-shift-only → blocked; alternatives with
differing coverage/windows; rule5 overlap across different-window machines; downtime vs
per-machine windows; zero/blank available hrs; specialty naming a non-existent machine
(CNC2); zero operators; provisional machine bypass.

## Out of scope (v1)
Model B (operator as one-at-a-time scarce resource, named-operator assignment, parallelism
throttling); applying operator **leaves** to per-day coverage (leaves are ingested but not
yet applied — noted). Both are clean follow-ons; A's coverage map is what B would consume.
