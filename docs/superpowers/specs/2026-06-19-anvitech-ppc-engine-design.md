# Anvitech PPC Engine — Design Spec

**Date:** 2026-06-19
**Status:** Approved
**Related:** [`RULES.md`](../../../RULES.md) (the rule sequence this implements)

---

## 1. Purpose

Anvitech is a precision-engineering job shop. They need a **Production Planning &
Control (PPC) engine** that takes customer sales orders and automatically
schedules them onto machines — optimizing for delivery dates while respecting
capacity, the working calendar, and shifts — then continuously re-plans as actual
production comes in.

The engine implements the 9 sequenced rules in `RULES.md`. The defining
non-functional requirement: **it must be easy to test and debug rule-by-rule.**
Each rule's exact input and output must be visible in the frontend so the user
can find where the logic "goes round."

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Runtime | Python engine + browser frontend (web app) |
| Web framework | FastAPI (thin API layer) |
| Frontend | Lightweight HTML/JS, **per-rule tabs** |
| Data source | Read `Test2.xlsx` directly, **read-only** |
| Writable data | `data/actuals.json` only (daily actuals) |
| Scope | All 9 rules built; Rules 8–9 **reuse** the Rules 1–7 functions |

## 3. Architecture

Three cleanly separated layers:

1. **`engine/`** — pure Python logic + Excel loading. Runs standalone, fully
   testable without a browser.
2. **`api/`** — thin FastAPI wrapper exposing the engine + trace as JSON.
3. **`web/`** — per-rule tabs that render the trace.

### Project structure

```
anvitech-ppc/
├── Test2.xlsx                  # read-only source (masters + SOs)
├── data/
│   └── actuals.json            # daily actuals store (Rule 8) — only writable data
├── engine/
│   ├── loaders.py              # read Test2.xlsx → typed Python objects
│   ├── models.py               # dataclasses: Batch, Process, Machine, ScheduleEntry...
│   ├── config.py               # tunable params + validation
│   ├── rules/
│   │   ├── rule1_consolidate.py
│   │   ├── rule2_sort_by_date.py
│   │   ├── rule3_tiebreak_process_time.py
│   │   ├── rule4_setup_time.py        # calc helper
│   │   ├── rule5_overlap_mode.py      # calc helper
│   │   ├── rule6_allocate.py          # consumes 4,5,7 + masters
│   │   ├── rule7_parallel_machine.py  # calc helper
│   │   ├── rule8_capture_actuals.py
│   │   └── rule9_rerun_mrp.py         # calls rule1..rule7 again
│   └── pipeline.py             # runs rules in order, SNAPSHOTS each in/out → trace
├── api/
│   └── main.py                 # FastAPI: /run, /trace, /actuals, /rerun
├── web/
│   └── (HTML/JS: per-rule tabs)
└── tests/
    └── test_rule1.py ... test_rule9.py + test_pipeline_golden.py
```

### Core principle: every rule is a pure function

```python
def run(input_data, config, masters) -> output_data
```

No rule reads global state or talks to the UI. No rule calls another rule. Only
`pipeline.py` knows the order. The one deliberate exception: **Rule 9 re-invokes
Rules 1–7** to re-plan — it imports and calls those functions, never duplicates
them. This is what makes Rules 8–9 inherit any fix made to Rules 1–7.

## 4. Data flow

A single `PlanRun` carrier object flows through the pipeline; each rule reads one
field and writes the next, so output→input holds exactly as in `RULES.md`.

```
PlanRun
  .so_lines        ← loaded from Test2.xlsx
  → Rule 1 → .batches              (consolidated)
  → Rule 2 → .batches_sorted       (by SO delivery date)
  → Rule 3 → .batches_prioritized  (+ tiebreak, reads routing master)
  → Rule 6 → .schedule             (non-delay; uses Rule 4/5/7 + masters)
            + machine-wise view    (per-machine queue + utilization, derived from .schedule)
  → Rule 8 → .actuals              (merged from data/actuals.json)
  → Rule 9 → triggers a fresh PlanRun with updated qty
```

- **Forward pipeline (output→input):** `1 → 2 → 3 → 6 → 8 → 9 (loop)`
- **Consumed inside Rule 6:** Rules 4, 5, 7
- **Rule 3** also reads the routing master (to compute total process time)

### Two write paths
- `Test2.xlsx` → **read-only** (masters + original SOs).
- `data/actuals.json` → the **only** thing the app writes (daily entries).

## 5. The per-rule trace mechanism (powers the tabs)

`pipeline.py` wraps every rule call and snapshots it:

```python
def run_rule(trace, name, fn, input_data, **kw):
    output = fn(input_data, **kw)
    trace[name] = {
        "input":  to_table(input_data),    # rows+columns, JSON-serializable
        "output": to_table(output),
        "config": kw.get("config"),        # e.g. window=10, overlap=50%
        "notes":  [],                      # human-readable decisions the rule logged
    }
    return output
```

Every run automatically captures, per rule: **input table, output table, config
used, and decision notes** (e.g. *"SO 121 + 121A consolidated — delivery dates 4
days apart"*). The frontend renders `trace[ruleN].input` and `.output` as two
tables in tab N. Visibility is built into the pipeline — rules need no special UI
code.

## 6. How Rules 8–9 attach to 1–7

- **Rule 8** (`rule8_capture_actuals`): saves a full Daily Production Entry
  (date, shift, SO, item code, auto-prompted item name, process dropdown, qty
  produced/rejected, actual setup time, and six downtime categories — no power,
  no operator, tool problem, machine breakdown, no load, other work — plus
  remarks) to `data/actuals.json`. Good qty = produced − rejected drives Rule 9;
  downtime is rolled up per item code.
- **Rule 9** (`rule9_rerun_mrp`): reads actuals, computes balance per SO
  (`SO qty − actual completed`), then **calls `rule1..rule7` again** with the
  updated quantities to produce a fresh schedule.

Because Rule 9 delegates, fixing Rules 1–7 automatically propagates to the loop.

## 7. Configurable parameters

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process | 90 min | Rule 4 |
| Operation overlap mode | Sequential / 50% overlap | Rule 5 |
| Parallel machine trigger | batch size > 400 | Rule 7 |

Validated at run start (e.g. window ≥ 0, overlap in 0–100%).

## 8. Error handling — fail loud, fail localized

- **Loader validation (before rules run):** collect *all* problems into one
  report rather than crashing on first. Known issues to handle:
  - Routings referencing machines not yet in `Machine master` (`CNC7`, `VMC3`,
    `CNC6`) are **expected** — the master is incomplete and will be completed later.
    Treat each as a **provisional machine**: register it so allocation proceeds,
    list it in a non-blocking `PENDING_MASTER_DATA` report, never drop the row or
    stop the pipeline. Adding the machine to the Excel master later must require
    **no code change**. Apply the same forgiving handling to other master
    references (operators, routings) that may be filled in later.
  - SO item codes with no routing in `Item's process Master` → `NO_ROUTING`:
    **report "no routing found" for that order and move ahead.** Skip only that one
    order (it can't be scheduled — there is no recipe), record it in the report, and
    continue planning every other order normally. Non-blocking, fail-localized; the
    plan run does not stop.
  - Time-unit inconsistency (minutes vs tiny decimals) → loader normalizes to one
    unit and logs any coerced value.
- **Per-rule guards:** each rule validates its input contract and raises a typed
  `RuleError(rule_name, record_id, message)`. The pipeline catches it, writes it
  into that rule's trace entry, and **stops the chain** — the frontend tab shows
  a red error with the offending row; downstream tabs show "not reached."
- **Config validation** at run start.
- **Non-crashing UI:** the API always returns the trace built so far, so a broken
  Rule 3 still shows Rules 1–2 outputs plus the Rule 3 error.

## 9. Testing

- **Unit test per rule** (`test_rule1.py` … `test_rule9.py`): pure function, so
  `input table → assert output table`. Seeded with the worked examples already in
  the Excel:
  - Rule 1: SO 121 + 121A (4 days apart → consolidated) vs 121A's 10-Apr line
    (outside 10 days → separate) — annotated in the SO Remarks column.
  - Rule 3: `61240807-01` vs `61249291-01`, same delivery date → higher process
    time wins (the sheet's own example).
- **Golden snapshot** (`test_pipeline_golden.py`): run all rules on a fixed
  subset of `Test2.xlsx`, compare the trace to a committed expected trace. Any
  logic change shows as a diff.
- **Rule 9 reuse test:** re-running MRP with actuals = 0 reproduces the original
  Rules 1–7 schedule (proves 8–9 delegate to 1–7).

## 10. API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /run?config=...` | Run Rules 1–7, return full trace |
| `GET /trace/{run_id}` | Fetch a past run's trace (for the tabs) |
| `POST /actuals` | Save a daily entry (Rule 8) |
| `POST /rerun` | Rule 9: re-plan from actuals + balance |

## 11. Out of scope (for now)

- Database persistence (reading Excel directly suffices; revisit if the actuals
  loop outgrows JSON).
- User authentication / multi-user.
- Operator-level shift assignment optimization beyond what the masters specify.
