# Monthly operator efficiency report

**Date:** 2026-07-18 · **Status:** approved by the owner (this session).

## Owner's decisions (verbatim intent)

1. Every Capture Actuals entry now names the OPERATOR (required dropdown from
   the in-app operator master). Evaluation is purely formula-based and fair —
   never biased by shift, machine, or job mix.
2. Data STAYS in the database — no deletion (storage confirmed: a year of
   punches ≈ 5-10 MB of the 512 MB tier). The report is computed on demand
   for any calendar month and downloaded as CSV (admin only).
3. The fairness formula (approved):

   Efficiency % = Earned ÷ Attended × 100
   - Earned minutes  = Σ standard cycle_time(item, process) × good qty punched
   - Attended minutes = Σ per worked day: that day's punched-shift window
     minutes − ALL recorded downtime minutes − recorded setup minutes
     (downtime and setup are neutral: neither earn nor penalize)
   - Only GOOD quantity earns; rejects earn nothing and surface as reject %.
   - Absence days come from the absence table — a separate column, never
     folded into pace.
   - A punch whose item/process has no cycle-time standard is EXCLUDED from
     both sides and counted in a "no standard" flag column — nobody is judged
     against a standard that doesn't exist.
   - Legacy punches without an operator name fall into an "Unattributed" row.

## Report columns (one row per operator, chosen month)

Operator · Days worked · Days absent · Attended (min) · Earned (min) ·
Efficiency % · Pace vs standard (×, = attended/earned) · Good qty ·
Rejected qty · Reject % · Downtime (min, total + per cause) ·
Setup (min) · Jobs handled · Punches without standard.

## Mechanics

- `Actual` gains an `operator` field (JSON round-trip; legacy rows default "").
- `POST /actuals` requires a non-empty operator that exists in the operator
  master (400 otherwise); the Capture form gets a required dropdown fed by
  `GET /operators` (both roles can submit actuals, unchanged).
- Pure `engine/efficiency.py`: `monthly_report(actuals, absences, masters,
  config, year, month) -> list[dict]` — no storage, no clock. Shift windows
  from config (First = first_shift_start→first_shift_end; Second =
  first_shift_end→second_shift_end next day; day-window operators use the
  manual window when their punch's shift is blank/other). Attended counts a
  day's shift window ONCE per distinct (operator, date, shift) even when
  several jobs were punched; downtime/setup subtract from that day.
- Process→cycle-time lookup mirrors how the loader names routing processes
  (normalized match; reuse existing helpers — no new parser).
- API: `GET /efficiency?year=&month=` (admin, JSON) and
  `GET /efficiency.csv?year=&month=` (admin, CSV download,
  `operator-efficiency-YYYY-MM.csv`).
- UI: small admin-only block (Settings area): month picker (defaults to last
  month) + "Download efficiency report (CSV)" + an on-screen preview table.
- NO optimize triggers, NO deletion, schedule untouched (pure reporting —
  the standing law: report changes never move the plan).

## Invariants & testing

- Golden untouched; plans byte-identical (reporting only).
- Formula unit-tested: multi-job days, both shifts same day, downtime/setup
  neutrality, reject exclusion, no-standard exclusion+flag, absence column,
  legacy unattributed bucket, month boundaries (entry_date month filter).
- API: role gating (user 403 on both endpoints), CSV header/shape, month
  validation (400 on bad year/month).
- Capture: missing/unknown operator → 400; legacy actuals load fine.
