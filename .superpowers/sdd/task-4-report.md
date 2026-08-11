# Task 4 report: Docs — operator efficiency report

## Status
Done.

## Commit
`4927ad4` on branch `operator-efficiency-report` (not pushed) — "Docs: operator
efficiency report in lockstep (Task 4)".

## What was built (docs only, no code/behavior change)
- **CLAUDE.md**:
  - `engine/models.py` bullet: `Actual.operator` — required at capture (400 on
    blank/unknown, validated against the operator master); legacy rows default
    `""` and land in "Unattributed".
  - New `engine/efficiency.py` code-map bullet: the pure `monthly_report(...)`
    formula (Efficiency % = Earned ÷ Attended × 100), the fairness rules
    (good-qty-only, downtime+setup neutral, no-standard excluded from BOTH
    sides, absence as its own column, legacy Unattributed bucket), and the
    review-caught fairness bug (45.5% vs the fair 90.9% on identical punches —
    excluding a no-standard punch from Earned only, while still charging its
    shift window to Attended, deflated the score) with the fix (exclude both
    sides). Noted the owner's no-deletion decision (~5-10 MB/year of 512 MB).
  - `api/main.py` bullet: `POST /actuals` validation clause, and a new
    `GET /efficiency` / `GET /efficiency.csv` clause (admin, `_efficiency_rows`
    / `_validate_year_month`, pure reporting).
  - `web/` bullet: required Operator dropdown on Capture Actuals (fed by
    `GET /operators`), and the Settings **Operator efficiency** block (month
    picker, Preview, Download CSV; `previewEfficiency`/`renderEfficiencyTable`/
    `downloadEfficiencyCsv`).
- **RULES.md**: new "Operator efficiency report" section after Rule 8 (plain
  shop-floor language) — the formula spelled out, a fairness-guarantees table
  (rejects / downtime+setup / no-standard punches / absence / legacy rows),
  the report column list, where to find it (Settings, admin-only), and the
  no-deletion note. Also added the required-Operator-dropdown line to Rule 7's
  captured-fields list for consistency.
- **HANDOFF.md**: new dated block "Latest session (2026-07-18, third session)"
  inserted after the operator-master-rotation block and before the
  2026-07-15/16 block — what shipped per task/commit, the floor instruction
  (Sanjay must pick the operator per entry from now on), the review-caught
  fairness bug as this session's generalizable lesson (check both sides of an
  "exclude X" rule, not just one), and the test count.
- Overwrote the stale `task-4-report.md` (previously documented a different
  plan's Task 4 — the Operators & shifts Settings UI — since task numbering
  restarts per plan; `task-4-brief.md` had already been updated for this
  plan's Task 4 before this report was written).

## Test summary
`python3 -m pytest -q` → **451 passed, 1 skipped** — unchanged from baseline
(docs-only change, no source touched).

## Verification method + outcome
Read the spec (`docs/superpowers/specs/2026-07-18-operator-efficiency-report-design.md`),
the landed code (`engine/efficiency.py`, `Actual.operator` in `engine/models.py`,
the `/actuals`/`/efficiency`/`/efficiency.csv` endpoints in `api/main.py`, the
Settings UI in `web/index.html`/`web/app.js`), and the ledger
(`.superpowers/sdd/progress.md`) before writing each doc sentence — cross-checked
formula wording, column names, and endpoint behavior against the actual
`REPORT_COLUMNS` list and `monthly_report` docstring rather than paraphrasing
from memory. Re-ran the full suite after the doc edits to confirm no
accidental code touch regressed anything.

## Concerns
None. Docs-only; no source files were modified.

## Report path
`/Users/ritinwadekar/Desktop/Anvitech Rebuilt/.superpowers/sdd/task-4-report.md`

---

## Fix pass (final whole-branch review, 2026-07-18)

### Status
Done. All three review findings applied in one commit.

### What changed

1. **CRITICAL — shift vocabulary (`engine/efficiency.py::_norm_shift`).**
   Real captured punches use "1st shift"/"2nd shift" (the Capture form default
   and the `/items` `"shifts"` list in `api/main.py`), but `_norm_shift` only
   matched substrings "first"/"second" — every real punch fell through to the
   540-min manual window instead of the 660-min first / 600-min second window,
   inflating efficiency % and biasing shift comparisons. Broadened the match:
   contains "first"/"1st"/starts with "1" → first; contains "second"/"2nd"/
   starts with "2" → second; everything else (blank/garbage) still falls to
   manual, unchanged.
   **RED-first regression tests** added to `tests/test_efficiency.py`:
   `test_1st_shift_text_uses_first_window_not_manual` (660 min, ~90.9%
   efficiency), `test_2nd_shift_text_uses_second_window_not_manual` (600 min,
   100.0%), `test_mixed_shift_vocabulary_same_day_one_window` ("1st shift" +
   "First shift" same day → one window, not two). Confirmed RED against the
   pre-fix code (`git stash` of just `efficiency.py`) before applying the fix,
   then GREEN after.
   **Normalizer kept separate, not shared**, from
   `engine/operator_coverage.py::_shift_kind`/`_shift_of`: those govern Rule 6
   scheduling off the Operator MASTER's `shift` field, whose real data is
   literally "First shift"/"Second shift" (see `tests/test_operator_coverage.py`)
   — already correctly matched by the existing substring check. The Capture
   form's `Actual.shift` field is a different data domain (free-typed, and now
   spec'd to "1st shift"/"2nd shift"). Broadening `_shift_kind` too would touch
   Rule 6 scheduling for zero benefit and risk regressing its byte-identical
   guarantee, so `efficiency.py` keeps its own normalizer (documented in the
   docstring).

2. **Shift field constrained to a dropdown (`web/app.js`).** The Capture
   form's Shift input (`actualsFormHtml`, ~line 917) was free text defaulting
   to "1st shift". Converted to a `<select id="a-shift">` with exactly the two
   options `/items` advertises, same element id (POST body wiring untouched).
   Added `fillShiftDropdown()` (mirrors the existing `fillSoDropdown()`
   pattern) which repopulates the options from `ITEMS.shifts` once `/items`
   resolves in `wireActualsForm()`, falling back to the same two literals if
   the fetch hasn't landed — so the value stays constrained even before the
   async fetch completes, and stays in sync automatically if the server list
   ever changes. Legacy free-text punches already on file are unaffected and
   handled by fix 1 + the documented manual-window fallback.

3. **Docs one-liners.** Added a sentence each to `CLAUDE.md`'s
   `engine/efficiency.py` bullet and `RULES.md`'s "Operator efficiency report"
   section: shift text is normalized ("1st shift"/"first"/"1", etc.) and the
   Capture form's Shift field is now a dropdown; unrecognized/blank shift text
   still falls to the manual (day) window as a documented fallback.

### Covering tests

- `tests/test_efficiency.py` — 22 passed (19 pre-existing + 3 new).
- `tests/test_operator_wiring.py` — passed, unchanged (proves
  `operator_coverage`/Rule 6 scheduling coverage is untouched by the
  efficiency-only fix).
- `tests/test_absences_engine.py` — passed, unchanged.
- Combined (`test_efficiency.py test_operator_wiring.py
  test_absences_engine.py`): **36 passed**.
- Full suite (`python3 -m pytest -q`): **454 passed, 1 skipped** (451 baseline
  + 3 new efficiency regression tests). Golden test (`-k golden`) passed,
  unaffected.

### Commit
One commit on `operator-efficiency-report`, not pushed, message summarizing
the three fixes, ending `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
