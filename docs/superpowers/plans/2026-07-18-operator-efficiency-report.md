# Operator Efficiency Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Every punch names its operator; a fair monthly efficiency report (Earned/Attended formula per the spec) is computed on demand and downloadable as CSV (admin). No deletion, no plan impact.

**Architecture:** `Actual.operator` field + required capture dropdown; pure `engine/efficiency.py`; two admin endpoints; small Settings UI. Spec: `docs/superpowers/specs/2026-07-18-operator-efficiency-report-design.md` (authoritative — formula, columns, mechanics).

## Global Constraints
- `python3 -m pytest` green each task; golden untouched. Baseline 412 passed, 1 skipped.
- Commits end `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Branch `operator-efficiency-report`. No push.
- Reporting must be schedule-neutral: NO change to any planning path.
- CSV filename exact: `operator-efficiency-YYYY-MM.csv`. Endpoints exact: `GET /efficiency`, `GET /efficiency.csv` (admin).
- Formula exactly per spec (earned/attended; downtime+setup neutral; good-qty-only; no-standard excluded+flagged; absence separate; unattributed bucket).

### Task 1: `Actual.operator` + required capture dropdown
**Files:** `engine/models.py` (Actual: `operator: str = ""` + to_json/from_json round-trip incl. legacy default), `api/main.py` (`ActualRequest.operator: str = ""`; `/actuals` validates non-empty AND name ∈ operator master → 400; pass through to the stored Actual), `web/app.js` (Capture form: required Operator `<select>` populated from `GET /operators` — both roles see it; wire into the POST body; keep the existing form conventions), tests (`test_rule7*`/actuals API tests updated to send an operator; new: 400 on missing/unknown; legacy from_json without operator loads as "").
Steps: RED → implement → focused (actuals/capture tests) → full suite → commit.

### Task 2: Pure `engine/efficiency.py`
**Files:** Create `engine/efficiency.py`, `tests/test_efficiency.py`.
Interface: `monthly_report(actuals, absences, masters, config, year, month) -> list[dict]` returning the spec's columns (dict keys exactly: "Operator", "Days worked", "Days absent", "Attended (min)", "Earned (min)", "Efficiency %", "Pace vs standard (x)", "Good qty", "Rejected qty", "Reject %", "Downtime (min)", "Setup (min)", "Jobs handled", "No-standard punches") sorted by Efficiency % desc, "Unattributed" row (if any) last.
Mechanics per spec: month filter on entry_date; attended = per distinct (operator, date, shift-normalized) window once − that day's downtime − setup; shift windows from config hours (First/Second/manual per spec); cycle-time lookup via the routing's process list with normalized name match (reuse existing normalize helpers; write `_cycle_for(masters, item_code, process_name)`); efficiency None/"—" when attended ≤ 0 or earned == 0 with no standards.
Tests: every spec bullet (multi-job day counted once; two shifts same day = two windows; downtime/setup neutrality vs a control; rejects earn nothing + reject %; no-standard excluded+flag; absence days from table; unattributed bucket; month boundary; empty month → []).
Steps: RED → implement → GREEN → full suite → commit.

### Task 3: Endpoints + UI
**Files:** `api/main.py` (GET /efficiency + /efficiency.csv, admin via `require_admin`; year/month query validation → 400; CSV via the file's existing CSV conventions — check how the allocation CSV download is built and mirror it), `web/index.html` + `web/app.js` (admin-only Settings block: month input type="month" defaulting to the previous month, Preview table, Download button hitting /efficiency.csv), tests (`test_efficiency_api.py`: role gating, JSON shape, CSV header + filename, bad month 400).
REQUIRED browser verification (both roles; punch a couple of attributed actuals on the sample book and see real numbers in the preview).
Steps: implement (API test-first) → browser verify → full suite → commit.

### Task 4: Docs + final review
- CLAUDE.md (efficiency module + endpoints + capture operator field), RULES.md (plain-language efficiency section: the formula + fairness points), HANDOFF.md (dated block).
- Full suite; whole-branch review (opus) + fix wave; ledger.
