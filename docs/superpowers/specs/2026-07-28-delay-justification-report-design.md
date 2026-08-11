# Delay Justification Report — design (2026-07-28)

## Purpose & context

The Anvitech directors reviewed the software and said it is **not transparent**: the
Gantt shows some orders finishing near their delivery date and others a month late, but
the plan gives **no justification** for the delay. Delay is acceptable (resources are
genuinely constrained), but *every hour of it must be explainable* — which machine was
busy, which operator was busy, and **which higher-priority order got ahead of it**.

This feature adds a downloadable, self-explanatory **Excel (`.xlsx`)** that, for **every
(SO No, Item Code) order**, accounts for its whole timeline and attributes each stretch
of waiting to a concrete cause, naming the specific orders that blocked it.

It is a **read-only report** derived from the finished plan. It changes **nothing** about
scheduling.

## Approach (chosen)

**Reconstruct the "why" post-hoc from the finished plan** — no scheduler changes. The
plan already records every operation's machine, operator, start/end, the priority order it
used, and each order's delivery date. A new **pure module** analyses that to produce, per
order, a timeline of RUNNING and WAITING intervals with each wait attributed to a cause.
The Excel layer is a thin serializer on top.

Rejected: instrumenting the scheduler to log reasons as it runs — more precise but edits
the vendored `ppc_engine`, risks the schedule, and isn't needed for a defensible report.

## The reason model — every minute is accounted for

For each order, from the **plan start** to its **expected completion** (its last
operation's end), time is partitioned into non-overlapping intervals, each exactly one of:

| State | Meaning | Named in the row |
|---|---|---|
| **RUNNING** | one of the order's own operations is on a machine | process, machine, operator(s), pieces |
| **WAIT — machine busy** | the machine the next operation needs is occupied by other orders | the blocking SO/item/process, its machine, its window, and `(higher priority)` if it ranks ahead |
| **WAIT — off-hours** | that machine is free but it's non-working time (night with no shift / Thursday weekly-off / holiday) | the calendar reason |
| **WAIT — crew** | machine free and within working hours, but no qualified operator was available | "waiting for a free qualified operator"; names busy qualified operators where determinable |

`RUNNING + all WAIT = the full span`, so **every hour is justified**.

## Attribution algorithm (the pure module)

`engine/delay_report.py`

```
build_delay_report(schedule, so_lines, batches_prioritized, config, masters) -> DelayReport
```

Where `DelayReport` = `{summary: [SummaryRow], detail: {(so,item): [TimelineRow]}}`.

**Per order `(so, item)`:**

1. **Own operations** = schedule entries with `item_code == item` and `so in e.so_refs`.
   These are the order's RUNNING intervals `[e.start, e.end]` (merged where they overlap,
   since Rule 5 overlap can interleave an order's own steps). Skip OS/off-machine
   milestone lanes for RUNNING (they consume no machine) but keep DISPATCH end as the
   completion gate.
2. **Span** = `[plan_start_dt, completion]`, where `completion = max(e.end)` over the
   order's ops and `plan_start_dt = config.plan_start_date @ 00:00` (already resolved to a
   real date at the API boundary). `Days late = (completion.date() − delivery_date).days`
   (negative ⇒ early/on-time).
3. **WAIT intervals** = the complement of the merged RUNNING intervals within the span.
   Each wait gap `[w0, w1]` immediately precedes the order's **next** operation to start
   (the op whose `start == w1`); call its machine `M`.
4. **Attribute each wait gap `[w0, w1]`** by walking `M`'s occupancy and clock:
   - **machine busy** — every *other* schedule entry on `M` overlapping `[w0, w1]`
     produces one `WAIT — machine busy` row: blocking `(so, item, process)`, its
     `[start, end]∩[w0,w1]`, and `(higher priority)` when the blocker's batch precedes
     this order's batch in `batches_prioritized`.
   - **off-hours** — the sub-intervals of `[w0, w1]` where `M` is free (no op) **and**
     `M`'s working clock (Rule 6 `_clock_factory` / `WorkClock`) says non-working →
     one `WAIT — off-hours` row per contiguous sub-interval.
   - **crew** — sub-intervals where `M` is free **and** within working hours → one
     `WAIT — crew` row; look up `operator_coverage.qualified_operators(M, t, …)` and, if
     those people are all busy in the schedule at that time, name them; else leave the
     category only. (Best-effort; never invents an operator.)

   The three cover `[w0, w1]` exactly (machine-busy ∪ (free∧non-working) ∪
   (free∧working) = the whole gap).

**Determinism & purity:** no I/O, no globals; identical inputs → identical output. All
datetimes come straight from the schedule.

**Complexity:** O(ops²) worst case per plan (~400 ops ⇒ ~160k interval checks) — fine;
pre-index the schedule by machine to keep it well under a second.

## Sheet 1 — Summary (one row per SO No + Item Code)

Columns: `SO No · Item Code · Item Name · Ordered Qty · SO Delivery Date · Expected
Completion · Days Late · Working (days) · Waiting: machine (days) · Waiting: off-hours
(days) · Waiting: crew (days) · Why` — where **Why** is a plain-English one-liner:
`"On time."` or e.g. `"18 days late — 12d machines busy (higher-priority orders), 4d
off-hours, 2d waiting for operators."` Sorted by Days Late descending (worst first, the
ones directors question). Day figures are calendar-day equivalents (`hours ÷ 24`, 1 dp).

## Sheet 2 — Detail (all orders; a block per SO No + Item Code)

For each order, an ordered block of timeline rows. Columns:
`SO No · Item Code · State · Process · Machine · Operator · From · To · Duration (hrs) ·
Why / Blocked by`

- **RUNNING** rows: the order's own op — `Process`, `Machine`, `Operator` (full handoff via
  `ScheduleEntry.operator_label()`), `From`/`To` = op start/end, blank "Why".
- **WAIT — machine busy** rows: `State = "WAITING (machine busy)"`, `Machine = M`,
  `Why = "CNC1 busy with SO 26-27SO85 / <item> — CNC FIRST SIDE (higher priority)"`,
  one per blocking op (**lists every blocking order**, per owner decision).
- **WAIT — off-hours** / **WAIT — crew** rows likewise, with the calendar/crew reason.

`From`/`To` are `DD-MM-YYYY HH:MM`. Rows are **colour-coded**: RUNNING green, machine-busy
amber, off-hours grey, crew orange — so it reads visually. A blank separator row (or a bold
order header row) between order blocks.

## Excel generation

Server-side with **openpyxl** (already a dependency — used to *read* uploads; here we
*write*). Build a `Workbook` with two sheets, apply header styling + per-row fills + column
widths + a frozen header row, save to a `BytesIO`, return it. Kept in a small helper
`api`-side (e.g. `_delay_report_xlsx(report) -> bytes`) so the pure module stays
Excel-agnostic and unit-testable without openpyxl.

## Endpoint & UI (wiring map)

- **`GET /delay-report.xlsx`** (admin only — `require_admin`, like `/efficiency.csv`).
  It reconstructs the current plan exactly as `/run` does — same saved config, active
  orders, actuals, absences, operator overlay, applied optimization ranks — to get the
  `schedule`, `batches_prioritized`, and `so_lines`, then calls `build_delay_report(...)`
  and `_delay_report_xlsx(...)`. Returns `Response(bytes, media_type=
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  Content-Disposition: attachment; filename="delay-justification-YYYY-MM-DD.xlsx")`.
  - To avoid duplicating `_plan`'s setup, extract a small internal helper
    `_plan_run_for_report(config) -> (plan_run, so_lines, masters)` that both `_plan` and
    this endpoint call (the forward-chain setup already in `_plan`). This is a targeted,
    in-scope refactor — `_plan` keeps its behaviour byte-identical.
- **UI:** a **"⬇ Download delay justification"** button in the Schedule view's download row
  (`web/app.js`, beside the schedule / machine-wise / shift-wise buttons), CSS-gated
  `admin-only`. It navigates to `/delay-report.xlsx` (a normal file download, like the
  efficiency CSV) — no on-screen table.

**Touches, and why it's safe:** new pure module (no deps on scheduling internals beyond
reading the schedule); new endpoint + `_plan_run_for_report` helper (a refactor of
existing setup, `_plan` unchanged); one openpyxl serializer; one admin-only UI button.
**No engine, scheduler, or plan-output changes** — `build_shiftwise_timeline`,
`plan_metrics`, analytics, and the golden trace are all untouched.

## Testing plan (TDD)

Pure-module tests (`tests/test_delay_report.py`) with crafted schedules where the answer is
unambiguous:

1. An on-time order (no waits) → one RUNNING block, `Days Late ≤ 0`, "On time."
2. An order blocked purely by a **higher-priority order** on its machine → a single
   `WAIT — machine busy` row naming that order, `(higher priority)` set; span = run + wait.
3. An order blocked by **off-hours** (a night gap on a single-shift machine) → a
   `WAIT — off-hours` row of the right length; no phantom machine-busy row.
4. A wait split across **multiple blockers** → every blocking order listed, windows
   summing exactly to the gap ("lists every blocking order").
5. **Invariant:** for every order, `Σ(RUNNING) + Σ(all WAIT) == span`, and each wait gap's
   attributed sub-intervals sum exactly to the gap (no unaccounted minutes, no
   double-count).
6. Summary aggregation matches the detail (working/machine/off-hours/crew day totals).

Plus a real-data smoke check on Test8 (new engine, operator logic on): the invariant holds
for all orders, and worst-late orders name concrete higher-priority blockers. Full `pytest`
green; golden untouched (this path isn't in the forward pipeline).

## Non-goals (YAGNI)

- No per-operator *root-cause* beyond best-effort naming (machine contention + off-hours are
  the precise, defensible core; crew is secondary).
- No in-app on-screen table, no charts inside Excel beyond row colours, no configurability
  (one report, current plan). No CSV variant.
- Does not change priorities or suggest fixes — it *explains*, it doesn't *optimize*.
