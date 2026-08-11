# In-app Operator & Shift master + Friday rotation

**Date:** 2026-07-18 · **Status:** approved by the owner (this session).

## The owner's rules (verbatim intent)

1. **Operators live in the app, not Excel.** Re-uploading a workbook must NEVER
   touch operators — the week-2 stale-sheet overwrite problem is impossible by
   construction. The Excel "Operator & shift Master" sheet is ignored after a
   one-time seed.
2. **Every Friday the shifts rotate**: shift 1 ↔ shift 2 for every two-shift
   operator, automatically ("we always assume they will change"). A per-operator
   **"stays on current shift" pin** exempts individuals until unpinned.
3. Admin can add/edit/remove operators in the app (new hires, machine
   qualifications, manual shift set). User role: read-only view.

## Store (app-owned, durable)

`anvitech:operators` (kv JSON):
```
{"week_anchor": "<ISO date of the last applied rotation Friday>",
 "operators": [{"id": uuid, "name": str, "machines_raw": str,
                "shift": "First shift"|"Second shift"|"", "pinned": bool}]}
```
- `machines_raw` uses the same text format as the Excel column (e.g.
  "CNC 1/CNC 2"); parsing reuses `loaders.parse_resource_candidates` /
  `normalize_resource_id` — no new parser.
- Blank shift = day-window/manual operator (rotation skips them, same as the
  engine's existing `_shift_of` default).

## Seeding (one-time, zero retyping)

On any masters access when the store table is EMPTY and a stored workbook
exists: seed from the workbook's operator sheet (name/machines/shift), set
`week_anchor` = the most recent Friday ≤ today, write the store. Thereafter the
sheet is never read into planning. (A later upload with an empty store — e.g.
fresh install — seeds from that upload, once.)

## Rotation (lazy, never missed, idempotent)

**Effectiveness rule (owner, 2026-07-18): the swap takes effect at Friday
SHIFT 1.** Operationally: a plan whose schedule BEGINS on/after Friday uses the
rotated shifts — even if it is computed on Thursday (the off day). So rotation
is applied **as-of the plan's effective start date**, not the wall clock:

- Planning/contests: effective operators = `rotate_table(stored, as_of=
  effective_plan_start_date)` — a PURE view; nothing persisted by planning.
- Display (`GET /operators`, the Settings panel): as_of = today.
- Persistence: the stored `week_anchor` advances lazily whenever any request
  observes today ≥ an unapplied Friday (idempotent catch-up; two missed
  Fridays = two flips, net no-op for unpinned).
- The cloud payload carries the ALREADY-EFFECTIVE operator rows (computed
  as-of the plan start at payload build) — the worker applies them directly,
  no anchor logic worker-side. Local == cloud byte-identical.

## Wiring (ONE application point)

`api._current_masters()` (and the cloud worker via payload) replaces
`masters.operators` with the store table (converted to `Operator` objects)
after `load_all`. Every consumer — operator_coverage, Rule 6, analytics,
shift-wise, gantt — inherits automatically. The loader's operator-sheet
ingestion stays only for seeding. Cloud payload carries the operator rows
(`parse_payload` applies them); local/cloud byte-identical.

## Fingerprints & reports

- `_inputs_signature` blob gains the operator table (masters sha no longer
  covers operators) — an applied optimization correctly flags `inputs_changed`
  after rotation/edits.
- `book_signature` unchanged (book state only) — the scheduled run's freshness
  check compares inputs via the applied meta as today.
  DECISION: rotation must not be skippable — the scheduled endpoint's
  fingerprint-skip compares book_sig only; after rotation book_sig is equal but
  inputs differ ⇒ extend the skip check: run when EITHER book_sig or the
  current `_inputs_signature` differs from the applied meta's.
- `/absences` validates names against the store table (it already validates
  against `masters.operators` — inherited).
- Orphan absences for removed operators: existing non-blocking report row.
- Report rows for unknown machine ids in `machines_raw`: reuse the loader's
  provisional-machine forgiveness (parse, never fatal).

## API / UI

- `GET /operators` (any role): `{operators: [...], next_rotation: "<ISO Friday>"}`.
- `POST /operators` (admin): add {name, machines_raw, shift}.
- `PATCH /operators/{id}` (admin): any of {machines_raw, shift, pinned}.
- `DELETE /operators/{id}` (admin): remove (absences referencing the name become
  orphans — non-blocking, as today).
- Settings section "Operators & shifts": table (name, machines, shift dropdown,
  "stays" checkbox, ✕), add row, "Next rotation: Friday DD-MM-YYYY". Admin
  edits; user read-only. No event-trigger on edits (scheduled-optimize rules
  stand; the Friday contest picks changes up).

## Invariants & testing

- Upload NEVER mutates the operator table once seeded (regression test:
  seed → re-upload workbook with a DIFFERENT operator sheet → table unchanged).
- Rotation: pinned stays, unpinned flips, blank-shift skipped, catch-up flips,
  idempotent same-day.
- Seeded table == Excel-loaded operators ⇒ plans byte-identical to today
  (golden untouched; the sample-workbook tests seed-from-sample and must
  produce identical schedules).
- Cloud == local with the payload-carried table.
- Live migration: first deploy boot seeds the current 19 operators unchanged —
  the first plan after deploy is byte-identical to the last plan before it
  (verified in rehearsal with the real stored workbook).
