# Operator Master + Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Operators & shifts live in an app-owned store (Excel sheet ignored after a one-time seed); every Friday unpinned two-shift operators swap shifts automatically; admin edits the table in Settings.

**Architecture:** Pure logic in new `engine/operator_master.py` (seed/rotate/convert); persistence in `book_store` (`anvitech:operators`); ONE wiring point (`api._current_masters` + cloud payload) replaces `masters.operators`; fingerprints extended. Spec: `docs/superpowers/specs/2026-07-18-operator-master-rotation-design.md` (read it fully — it is authoritative).

## Global Constraints
- `python3 -m pytest` green at every task end; golden untouched (no regen). Baseline 364 passed, 1 skipped.
- Commits end `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Branch `operator-master-rotation`. No push.
- Store key exact: `anvitech:operators`. Shift strings exact: "First shift" / "Second shift" / "" (matches `operator_coverage._shift_of` parsing).
- Seeded-from-sample plans must be byte-identical to Excel-loaded plans (regression-pinned).
- Upload NEVER mutates a non-empty operator table (regression-pinned).
- Rotation: Friday-boundary, lazy catch-up, idempotent, pinned/blank-shift exempt.

### Task 1: `engine/operator_master.py` (pure) + store persistence
**Files:** Create `engine/operator_master.py`, modify `engine/book_store.py`, create `tests/test_operator_master.py`.
**Interfaces produced:**
```python
# engine/operator_master.py (pure — no storage, no datetime.now; callers pass today)
def seed_rows_from_masters(masters) -> list[dict]      # [{id,name,machines_raw,shift,pinned:False}]
def last_friday(today: date) -> date                    # most recent Friday <= today
def next_rotation(today: date) -> date                  # first Friday > today
def rotate_table(table: dict, today: date) -> tuple[dict, int]
    # counts Fridays in (week_anchor, today]; flips First<->Second for non-pinned,
    # non-blank rows that many times (parity); returns (new_table, flips_applied);
    # flips_applied==0 => table returned unchanged (same object ok)
def to_operators(rows: list[dict]) -> list[Operator]    # via loaders.parse_resource_candidates
# engine/book_store.py
OPERATORS_KEY = "anvitech:operators"
def load_operator_table() -> dict | None
def save_operator_table(table: dict) -> None
```
Tests: seed copies name/machines/shift + pinned False + uuid ids; rotation flips/pins/blank/catch-up(2 Fridays = net same for unpinned)/idempotent same-day/anchor advances; to_operators parses machines like the loader does (compare against an Excel-loaded operator); store round-trip.

### Task 2: Wiring — masters overlay, seed-once, payload, fingerprints, scheduled-skip
**Files:** Modify `api/main.py`, `engine/optimize_service.py`, tests (`test_operator_wiring.py` new; touch `test_optimize_cloud.py`, `test_auto_optimize.py`).
- OWNER RULE: rotation takes effect at FRIDAY SHIFT 1 → planning applies it AS-OF THE PLAN'S EFFECTIVE START DATE, display as-of today (see the spec's Effectiveness rule — authoritative).
- `api._current_masters()`: after parsing the workbook → seed-once if the store table is empty and the workbook has operators (`week_anchor=last_friday(today)`); then `masters.operators = to_operators(rotate_table(stored, today)[0])` for DISPLAY/default use, re-applied on every call (cache only the parsed workbook). Also advance-persist: if `rotate_table(stored, today)` returns flips>0, save it (lazy catch-up).
- PLANNING as-of plan start: `optimize_service.prepare_contest` already computes `eff` (effective plan start). After that line, when payload/api supplies `operator_rows` (the STORED table rows), apply `masters.operators = to_operators(rotate_table({'week_anchor': stored_anchor, 'operators': rows}, eff)[0])`. Concretely: extend `prepare_contest(..., operator_table=None)` (the full stored dict); when given, apply rotation as-of `eff` onto a COPY of masters.operators. `_plan` passes `book_store.load_operator_table()`; `build_payload` gains `operator_table=` carried verbatim; `parse_payload` returns it; `run_candidate` passes it through — the worker then computes the same as-of-eff rotation (pure fns available engine-side) — byte-identical local/cloud.
- Upload path: no operator writes when table exists (only the empty-store seed). Regression test: seed from sample A, upload workbook B with different operators ⇒ table unchanged, masters.operators still A.
- `optimize_service.build_payload(..., operator_rows=None)` → payload `"operators"`; `parse_payload` returns them; `run_candidate`/`prepare_contest` path: after `load_all`, if rows present replace `masters.operators = to_operators(rows)`. Worker unchanged (payload-driven). Local/cloud equivalence test with a custom table.
- `_inputs_signature`: append the operator table json (sorted, ids excluded — content only: name/machines_raw/shift; pins excluded? PINS affect FUTURE rotation not the current plan ⇒ exclude pins, include shifts) to the hashed blob.
- Scheduled-skip fix (spec DECISION): in `_try_start_auto`, run when `applied_book_sig != current_book_sig` OR `applied_meta.inputs_sig != _inputs_signature(saved config ...)` — reuse the existing staleness comparison from `_plan`'s optimize_meta block; extract a tiny shared helper if needed.
- Byte-identical regression: sample workbook seeded ⇒ `run_forward` schedule identical to pre-seed Excel-loaded masters.

### Task 3: `/operators` endpoints
**Files:** Modify `api/main.py`; create `tests/test_operators_api.py`.
- `GET /operators` (any role): `{"operators": rows, "next_rotation": next_rotation(today).isoformat()}`.
- `POST /operators` (admin): {name, machines_raw, shift} — name non-empty + unique (400), shift in {"First shift","Second shift",""} (400); appends with uuid; returns row.
- `PATCH /operators/{id}` (admin): partial {machines_raw, shift, pinned}; 404 unknown id; shift validated.
- `DELETE /operators/{id}` (admin): 404 unknown; removing may orphan absences (non-blocking, existing report row covers).
- All admin mutations persist via `save_operator_table`; no optimize trigger (scheduled-only rules stand).
- Tests: role gating (user GET 200 / POST 403), CRUD, validation, uniqueness, 404s, absence-orphan after delete.

### Task 4: Settings UI
**Files:** `web/index.html`, `web/app.js` (+`style.css` minimal).
- Section "Operators & shifts" beside absences: table rows = name · machines (text input) · shift (select First/Second/blank="Day (9-18)") · "Stays" checkbox · ✕; add-row (name+machines+shift) + "Add operator"; header line "Shifts rotate every Friday. Next rotation: <DD-MM-YYYY>". Admin-only controls (same `admin-only` gating as absences); list visible read-only to users. PATCH on change (shift select/pin checkbox/machines blur), POST add, DELETE ✕ with confirm(). All via textContent/escapeHtml patterns.
- REQUIRED browser verification both roles (absence-task pattern: local server + sample seed + real Chrome).

### Task 5: Docs + migration rehearsal + final review
- Rehearsal with the REAL stored workbook flow: fresh local store; upload Test5.xlsx (simulates the live store's workbook); plan → note schedule; confirm table seeded (19 ops); re-upload Test5.xlsx ⇒ table untouched; plan byte-identical pre/post-seed; flip one operator's pin + simulate a Friday (monkey: set week_anchor back 7 days via a tiny script) → plan changes shift assignments; absences validation still works.
- Docs: CLAUDE.md (masters bullet: operators are app-owned, Excel sheet seeds once then ignored; rotation; new endpoints; map entry for operator_master.py), RULES.md (operator/shift section plain-language), HANDOFF.md (dated block).
- Full suite; whole-branch review (opus) + fix wave; ledger.
