# Delay Justification Report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin-only downloadable `.xlsx` that, per `(SO No, Item Code)` order, accounts for every hour from plan start to completion and attributes each waiting stretch to a concrete cause (machine busy — naming every higher-priority blocking order, off-hours, or crew).

**Architecture:** A new **pure module** `engine/delay_report.py` reconstructs the "why" from the finished plan (schedule + priority order + delivery dates + working calendar) — no scheduler changes. A thin openpyxl serializer in `api/main.py` turns it into a 2-sheet workbook, served by `GET /delay-report.xlsx`. A small `_plan_run_for_report` refactor lets the endpoint reuse `_plan`'s setup.

**Tech Stack:** Python 3, FastAPI, openpyxl (already a dependency), pytest.

## Global Constraints

- Pure module: `def build_delay_report(schedule, so_lines, batches_prioritized, config, masters) -> dict` — no I/O, no globals, deterministic.
- Reads only the finished plan. **No engine/scheduler/plan-output changes** — `plan_metrics`, `build_shiftwise_timeline`, analytics, and the golden trace stay byte-identical.
- Admin-only endpoint (`require_admin`, like `/efficiency.csv`).
- Invariant every task preserves: per order, `Σ RUNNING + Σ all WAIT == span`, and each wait gap's attributed sub-intervals sum exactly to the gap.
- Datetimes stay `datetime` objects in the pure module (formatted to `DD-MM-YYYY HH:MM` only in the serializer). Durations in hours (float, 2 dp). Day figures = `hours / 24`, 1 dp.
- Spec: `docs/superpowers/specs/2026-07-28-delay-justification-report-design.md`.

## Data model (produced by Task 1, used everywhere)

```python
# detail row (flat list, grouped by order, order groups sorted by Days Late desc)
{"SO No": str, "Item Code": str, "State": str,   # "RUNNING" | "WAITING (machine busy)"
                                                   # | "WAITING (off-hours)" | "WAITING (crew)"
 "Process": str, "Machine": str, "Operator": str,
 "From": datetime, "To": datetime, "Hours": float, "Why": str}
# summary row (one per order)
{"SO No": str, "Item Code": str, "Item Name": str, "Ordered Qty": int,
 "SO Delivery Date": date, "Expected Completion": date, "Days Late": int,
 "Working (days)": float, "Waiting: machine (days)": float,
 "Waiting: off-hours (days)": float, "Waiting: crew (days)": float, "Why": str}
# build_delay_report(...) -> {"summary": [summary rows], "detail": [detail rows]}
```

## File structure

- Create `engine/delay_report.py` — the pure attribution module (Tasks 1–4).
- Create `tests/test_delay_report.py` — unit tests (Tasks 1–4).
- Modify `api/main.py` — `_plan_run_for_report`, `_delay_report_xlsx`, `GET /delay-report.xlsx` (Tasks 5–6).
- Modify `tests/test_api.py` (or a new `tests/test_delay_report_api.py`) — endpoint tests (Task 6).
- Modify `web/app.js` — the admin-only download button (Task 7).

---

### Task 1: Timeline skeleton — RUNNING rows, WAIT gaps, span & days-late

**Files:**
- Create: `engine/delay_report.py`
- Test: `tests/test_delay_report.py`

**Interfaces:**
- Produces: `build_delay_report(schedule, so_lines, batches_prioritized, config, masters) -> {"summary": [...], "detail": [...]}`. In this task the detail rows are RUNNING rows plus **un-attributed** WAIT rows (`State="WAITING (unattributed)"`, blank Why); later tasks replace the un-attributed rows with real causes.
- Helpers: `_order_ops(schedule, so, item) -> [ScheduleEntry]`, `_merge(intervals) -> [(dt,dt)]`, `_gaps(start, end, running) -> [(dt,dt)]`.

- [ ] **Step 1: Write the failing test** (`tests/test_delay_report.py`)

```python
from datetime import date, datetime
from engine.delay_report import build_delay_report
from engine.models import (ScheduleEntry, Batch, SOLine, Machine, Masters,
                            WorkCalendar, Routing, Config as _Cfg)  # adjust imports as they exist
from engine.config import Config

def _so_line(so="SO1", item="X", qty=100, due=date(2025, 3, 10)):
    return SOLine(so_no=so, item_code=item, item_name="X", qty=qty, delivery_date=due)

def _entry(so, item, seq, machine, s, e, op="P", qty=100):
    return ScheduleEntry(batch_id="B1", item_code=item, process_seq=seq, process_name="CNC",
                         machine=machine, qty=qty, occupancy_min=(e-s).total_seconds()/60,
                         start=s, end=e, notes="", so_refs=[so], operator=op,
                         op_segments=[(s, e, op)])

def test_on_time_order_has_one_running_block_and_no_late():
    cfg = Config(plan_start_date=date(2025, 3, 3))
    e = _entry("SO1", "X", 1, "M", datetime(2025,3,3,8,0), datetime(2025,3,3,12,0))
    masters = Masters(machines={"M": Machine(machine_no="M", display_name="M",
                       machine_type="CNC lathe", available_hrs_per_day=19.5)},
                      calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="W", customer="", rm_type="",
                                    moq=None, processes=[])
    rep = build_delay_report([e], [_so_line(due=date(2025,3,20))], [], cfg, masters)
    running = [r for r in rep["detail"] if r["State"] == "RUNNING"]
    assert len(running) == 1 and running[0]["Machine"] == "M"
    s = rep["summary"][0]
    assert s["Days Late"] <= 0            # finished 03-03, due 03-20
    # invariant: running + waits cover the span with no gap
    total = sum(r["Hours"] for r in rep["detail"])
    span_h = (datetime(2025,3,3,12,0) - datetime(2025,3,3,0,0)).total_seconds()/3600
    assert abs(total - span_h) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_delay_report.py -v`
Expected: FAIL — `ModuleNotFoundError: engine.delay_report`.

- [ ] **Step 3: Write minimal implementation** (`engine/delay_report.py`)

```python
"""Delay justification — pure, post-hoc reconstruction of WHY each order is delayed
from the finished plan (no scheduler state). See the 2026-07-28 spec."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime

_OFF_LANES = {"OS / Outsourced", "Off-machine"}

def _order_ops(schedule, so, item):
    ops = [e for e in schedule if e.item_code == item and so in (e.so_refs or [])
           and e.machine not in _OFF_LANES and e.end > e.start]
    return sorted(ops, key=lambda e: e.start)

def _merge(iv):
    iv = sorted(iv); out = []
    for s, e in iv:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def _gaps(start, end, running):
    gaps, cur = [], start
    for s, e in running:
        if s > cur:
            gaps.append((cur, min(s, end)))
        cur = max(cur, e)
    if cur < end:
        gaps.append((cur, end))
    return [(a, b) for a, b in gaps if b > a]

def _hours(a, b):
    return (b - a).total_seconds() / 3600.0

def build_delay_report(schedule, so_lines, batches_prioritized, config, masters):
    plan_start = datetime.combine(config.plan_start_date, datetime.min.time())
    # index every order line
    lines = {(l.so_no, l.item_code): l for l in so_lines}
    detail, summary = [], []
    for (so, item), line in lines.items():
        ops = _order_ops(schedule, so, item)
        if not ops:
            continue
        running = _merge([(e.start, e.end) for e in ops])
        completion = max(e.end for e in ops)
        span_start = plan_start
        rows = []
        for e in ops:
            rows.append({"SO No": so, "Item Code": item, "State": "RUNNING",
                         "Process": f"{e.process_seq}. {e.process_name}", "Machine": e.machine,
                         "Operator": e.operator_label(), "From": e.start, "To": e.end,
                         "Hours": round(_hours(e.start, e.end), 2), "Why": ""})
        for (a, b) in _gaps(span_start, completion, running):
            rows.append({"SO No": so, "Item Code": item, "State": "WAITING (unattributed)",
                         "Process": "", "Machine": "", "Operator": "", "From": a, "To": b,
                         "Hours": round(_hours(a, b), 2), "Why": ""})
        rows.sort(key=lambda r: r["From"])
        days_late = (completion.date() - line.delivery_date).days
        summary.append({"SO No": so, "Item Code": item, "Item Name": line.item_name,
                        "Ordered Qty": int(line.qty), "SO Delivery Date": line.delivery_date,
                        "Expected Completion": completion.date(), "Days Late": days_late,
                        "Working (days)": 0.0, "Waiting: machine (days)": 0.0,
                        "Waiting: off-hours (days)": 0.0, "Waiting: crew (days)": 0.0, "Why": ""})
        detail.extend(rows)
    summary.sort(key=lambda s: -s["Days Late"])
    order_pos = {(s["SO No"], s["Item Code"]): i for i, s in enumerate(summary)}
    detail.sort(key=lambda r: (order_pos[(r["SO No"], r["Item Code"])], r["From"]))
    return {"summary": summary, "detail": detail}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_delay_report.py -v`
Expected: PASS. (If `ScheduleEntry`/`SOLine`/`Batch` import paths differ, fix the test imports to match `engine/models.py`.)

- [ ] **Step 5: Commit**

```bash
git add engine/delay_report.py tests/test_delay_report.py
git commit -m "feat(delay-report): timeline skeleton — running rows, wait gaps, span & days-late"
```

---

### Task 2: Machine-busy attribution (name every higher-priority blocker)

**Files:**
- Modify: `engine/delay_report.py`
- Test: `tests/test_delay_report.py`

**Interfaces:**
- Consumes: `_gaps`, the wait rows from Task 1.
- Produces: `_rank_by_key(batches_prioritized) -> {(so,item): int}`; each `WAITING (unattributed)` gap is replaced by one `WAITING (machine busy)` row **per** other op overlapping the gap on the needed machine; the machine-free remainder stays `WAITING (free)` for Task 3.

- [ ] **Step 1: Write the failing test**

```python
def test_wait_names_every_higher_priority_blocker():
    cfg = Config(plan_start_date=date(2025, 3, 3))
    # Our order's op runs 12:00-16:00 on M; two higher-priority orders held M 08:00-12:00.
    ours = _entry("SO1", "X", 1, "M", datetime(2025,3,3,12,0), datetime(2025,3,3,16,0))
    blk1 = _entry("SO2", "Y", 1, "M", datetime(2025,3,3,8,0), datetime(2025,3,3,10,0))
    blk2 = _entry("SO3", "Z", 1, "M", datetime(2025,3,3,10,0), datetime(2025,3,3,12,0))
    masters = Masters(machines={"M": Machine(machine_no="M", display_name="M",
                       machine_type="CNC lathe", available_hrs_per_day=19.5)}, calendar=WorkCalendar())
    for it in ("X","Y","Z"):
        masters.routings[it] = Routing(item_code=it, description="", customer="", rm_type="", moq=None, processes=[])
    # batches_prioritized: SO2, SO3 rank ahead of SO1
    from engine.models import Batch
    bp = [Batch(batch_id="B2", item_code="Y", item_name="Y", qty=1, so_delivery_date=date(2025,3,10), source_so_refs=["SO2"]),
          Batch(batch_id="B3", item_code="Z", item_name="Z", qty=1, so_delivery_date=date(2025,3,10), source_so_refs=["SO3"]),
          Batch(batch_id="B1", item_code="X", item_name="X", qty=1, so_delivery_date=date(2025,3,10), source_so_refs=["SO1"])]
    rep = build_delay_report([ours, blk1, blk2], [_so_line("SO1","X")], bp, cfg, masters)
    busy = [r for r in rep["detail"] if r["State"] == "WAITING (machine busy)" and r["SO No"]=="SO1"]
    whys = " | ".join(r["Why"] for r in busy)
    assert "SO2" in whys and "SO3" in whys and "higher priority" in whys
    # the two blockers' windows sum to the 08:00-12:00 wait (240 min)
    assert abs(sum(r["Hours"] for r in busy) - 4.0) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails** — `python3 -m pytest tests/test_delay_report.py::test_wait_names_every_higher_priority_blocker -v` → FAIL (rows are `WAITING (unattributed)`).

- [ ] **Step 3: Write minimal implementation** — add to `engine/delay_report.py` and call it where the gap loop is:

```python
def _rank_by_key(batches_prioritized):
    rank = {}
    for i, b in enumerate(batches_prioritized or []):
        for so in (b.source_so_refs or []):
            rank[(so, b.item_code)] = i
    return rank

def _machine_of_next_op(ops, gap_end):
    nxt = min((e for e in ops if e.start >= gap_end), key=lambda e: e.start, default=None)
    return nxt.machine if nxt else (ops[-1].machine if ops else "")

def _attribute_machine_busy(gap, machine, schedule, this_rank, rank):
    """Return (busy_rows, free_intervals). busy_rows: one per other op overlapping the gap
    on `machine`. free_intervals: the machine-free remainder of the gap."""
    a, b = gap
    others = sorted([e for e in schedule if e.machine == machine and not (e.end <= a or e.start >= b)],
                    key=lambda e: e.start)
    busy, cur, occupied = [], a, []
    for e in others:
        os, oe = max(e.start, a), min(e.end, b)
        if oe <= os:
            continue
        occupied.append((os, oe))
        bso = (e.so_refs or [""])[0]
        hp = rank.get((bso, e.item_code), 10**9) < this_rank
        why = (f"{machine} busy with {bso} / {e.item_code} — {e.process_name}"
               + (" (higher priority)" if hp else ""))
        busy.append({"State": "WAITING (machine busy)", "Machine": machine, "From": os, "To": oe,
                     "Hours": round(_hours(os, oe), 2), "Why": why})
    free = _gaps(a, b, _merge(occupied))
    return busy, free
```

In `build_delay_report`, replace the un-attributed gap loop with: compute `this_rank = rank.get((so,item), 10**9)`, `machine = _machine_of_next_op(ops, a)` per gap, call `_attribute_machine_busy`, append busy rows (filling SO/Item/Process/Operator=""), and carry `free` intervals forward as `WAITING (free)` rows (temporary; Task 3 classifies them).

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(delay-report): attribute machine contention, naming every higher-priority blocker"`

---

### Task 3: Off-hours vs crew split of the free remainder (+ full invariant)

**Files:**
- Modify: `engine/delay_report.py`
- Test: `tests/test_delay_report.py`

**Interfaces:**
- Consumes: the `free` intervals from Task 2, `rule6_allocate._clock_factory`, `WorkClock._windows_for_day`.
- Produces: each free interval split into `WAITING (off-hours)` and `WAITING (crew)` sub-rows; **no `WAITING (free)`/`unattributed` rows remain**.

- [ ] **Step 1: Write the failing test**

```python
def test_night_gap_is_off_hours_not_crew_and_invariant_holds():
    from engine.rules import rule6_allocate  # for _clock_factory parity
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    # single-shift manual machine BS1 works 09:00-18:00; op runs next day 09:00-11:00,
    # so the 18:00->09:00 gap must be off-hours (not crew).
    masters = Masters(machines={"BS1": Machine(machine_no="BS1", display_name="BS1",
                       machine_type="Band saw", available_hrs_per_day=9.5)}, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="", rm_type="", moq=None, processes=[])
    e = _entry("SO1","X",1,"BS1", datetime(2025,3,4,9,0), datetime(2025,3,4,11,0))
    rep = build_delay_report([e], [_so_line("SO1","X")], [], cfg, masters)
    off = [r for r in rep["detail"] if r["State"]=="WAITING (off-hours)"]
    assert off and all("hours" in r["Why"].lower() for r in off)
    assert not [r for r in rep["detail"] if "unattributed" in r["State"] or "(free)" in r["State"]]
    # invariant: total detail hours == span (plan_start 03-03 00:00 -> completion 03-04 11:00)
    total = sum(r["Hours"] for r in rep["detail"] if r["SO No"]=="SO1")
    span = (datetime(2025,3,4,11,0) - datetime(2025,3,3,0,0)).total_seconds()/3600
    assert abs(total - span) < 1e-3
```

- [ ] **Step 2: Run — FAIL** (free rows not classified).

- [ ] **Step 3: Write minimal implementation**

```python
def _working_subintervals(a, b, clock):
    """Sub-intervals of [a,b] that fall inside the machine's working windows."""
    out = []
    d = a.date()
    while datetime.combine(d, datetime.min.time()) < b:
        for ws, we in clock._windows_for_day(d):
            s, e = max(ws, a), min(we, b)
            if e > s:
                out.append((s, e))
        d = d + __import__("datetime").timedelta(days=1)
    return _merge(out)

def _classify_free(a, b, clock):
    work = _working_subintervals(a, b, clock)
    rows = []
    for s, e in work:
        rows.append({"State": "WAITING (crew)", "From": s, "To": e, "Hours": round(_hours(s,e),2),
                     "Why": "Machine free — waiting for a free qualified operator"})
    for s, e in _gaps(a, b, work):
        rows.append({"State": "WAITING (off-hours)", "From": s, "To": e, "Hours": round(_hours(s,e),2),
                     "Why": "Outside working hours (night / weekly off / holiday)"})
    return rows
```

In `build_delay_report`: build `clock_for, _ = rule6_allocate._clock_factory(masters, config)` once; for each `free` interval call `_classify_free(a, b, clock_for(machine))` and append those rows (filling SO/Item/Process/Operator=""). Remove the temporary `WAITING (free)` emission.

- [ ] **Step 4: Run — PASS.** Then add a test asserting the **whole-report invariant** (Σ hours per order == span) on a mixed case (running + machine-busy + off-hours).
- [ ] **Step 5: Commit** — `git commit -m "feat(delay-report): split free waits into off-hours vs crew; every minute attributed"`

---

### Task 4: Summary aggregation + plain-English "Why"

**Files:**
- Modify: `engine/delay_report.py`
- Test: `tests/test_delay_report.py`

**Interfaces:**
- Produces: summary rows with `Working (days)`, `Waiting: machine/off-hours/crew (days)` filled (= sum of matching detail-row hours ÷ 24, 1 dp) and a `Why` one-liner.

- [ ] **Step 1: Write the failing test**

```python
def test_summary_totals_and_why_match_the_detail():
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    ours = _entry("SO1","X",1,"M", datetime(2025,3,3,12,0), datetime(2025,3,3,16,0))
    blk  = _entry("SO2","Y",1,"M", datetime(2025,3,3,8,0),  datetime(2025,3,3,12,0))
    masters = Masters(machines={"M": Machine(machine_no="M", display_name="M",
                       machine_type="CNC lathe", available_hrs_per_day=19.5)}, calendar=WorkCalendar())
    for it in ("X","Y"): masters.routings[it]=Routing(item_code=it,description="",customer="",rm_type="",moq=None,processes=[])
    from engine.models import Batch
    bp=[Batch(batch_id="B2",item_code="Y",item_name="Y",qty=1,so_delivery_date=date(2025,3,10),source_so_refs=["SO2"]),
        Batch(batch_id="B1",item_code="X",item_name="X",qty=1,so_delivery_date=date(2025,3,4),source_so_refs=["SO1"])]
    rep = build_delay_report([ours,blk],[_so_line("SO1","X",due=date(2025,3,4))],bp,cfg,masters)
    s = next(r for r in rep["summary"] if r["SO No"]=="SO1")
    det_machine = sum(r["Hours"] for r in rep["detail"] if r["SO No"]=="SO1" and r["State"]=="WAITING (machine busy)")
    assert abs(s["Waiting: machine (days)"] - round(det_machine/24,1)) < 1e-9
    assert "machines busy" in s["Why"].lower()
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the aggregation in `build_delay_report`: after detail rows for an order are final, sum hours by state into the four day-buckets; build `Why`: `"On time."` if `Days Late <= 0` else `f"{days_late} days late — " + ", ".join(parts)` where parts list the non-zero buckets as e.g. `"12.0d machines busy (higher-priority orders)"`, `"4.0d off-hours"`, `"2.0d waiting for operators"`.
- [ ] **Step 4: Run — PASS.** Run the whole file: `python3 -m pytest tests/test_delay_report.py -v`.
- [ ] **Step 5: Commit** — `git commit -m "feat(delay-report): summary day-buckets + plain-English why line"`

---

### Task 5: The 2-sheet `.xlsx` serializer

**Files:**
- Modify: `api/main.py` (add `_delay_report_xlsx(report) -> bytes`)
- Test: `tests/test_delay_report_api.py` (new)

**Interfaces:**
- Consumes: `build_delay_report(...)` output.
- Produces: `_delay_report_xlsx(report) -> bytes` — an openpyxl workbook with sheet "Summary" (Task-4 columns) and sheet "Detail" (Task-1 columns, `From`/`To` as `DD-MM-YYYY HH:MM`), a frozen bold header row, sensible column widths, and per-row fills (RUNNING green `C6E0B4`, machine-busy amber `FFE699`, off-hours grey `D9D9D9`, crew orange `F8CBAD`).

- [ ] **Step 1: Write the failing test**

```python
import io, importlib
import pytest
pytest.importorskip("openpyxl")
import openpyxl

def test_delay_xlsx_has_two_sheets_and_headers():
    import api.main as m; importlib.reload(m)
    report = {"summary": [{"SO No":"SO1","Item Code":"X","Item Name":"X","Ordered Qty":1,
              "SO Delivery Date": None, "Expected Completion": None, "Days Late": 5,
              "Working (days)":1.0,"Waiting: machine (days)":4.0,"Waiting: off-hours (days)":0.0,
              "Waiting: crew (days)":0.0,"Why":"5 days late — 4.0d machines busy"}],
              "detail": []}
    wb = openpyxl.load_workbook(io.BytesIO(m._delay_report_xlsx(report)))
    assert wb.sheetnames == ["Summary", "Detail"]
    assert wb["Summary"].cell(1,1).value == "SO No"
```

- [ ] **Step 2: Run — FAIL** (`_delay_report_xlsx` undefined).
- [ ] **Step 3: Implement** `_delay_report_xlsx` in `api/main.py` using `openpyxl.Workbook`, `PatternFill`, `Font`, `freeze_panes`, writing Summary then Detail; format `From`/`To` datetimes via `strftime("%d-%m-%Y %H:%M")`; save to `io.BytesIO`, return `.getvalue()`.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(delay-report): openpyxl 2-sheet serializer (Summary + Detail, colour-coded)"`

---

### Task 6: `GET /delay-report.xlsx` endpoint + `_plan_run_for_report`

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_delay_report_api.py`

**Interfaces:**
- Consumes: `build_delay_report`, `_delay_report_xlsx`.
- Produces: `_plan_run_for_report(config) -> (plan_run, so_lines, masters)` (extract the forward-chain setup already in `_plan` — `_plan` calls it and stays behaviourally identical); `GET /delay-report.xlsx` (admin) returning the workbook bytes.

- [ ] **Step 1: Write the failing test**

```python
from fastapi.testclient import TestClient
from datetime import date
from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A

def _client_admin(m):
    c = TestClient(m.app); c.post("/login", data={"username":"anvitech","password":"1930rail"}); return c

def test_delay_report_admin_only_and_returns_xlsx():
    import api.main as m; importlib.reload(m)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025,3,20))])
    admin = _client_admin(m)
    r = admin.get("/delay-report.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    user = TestClient(m.app); user.post("/login", data={"username":"anvitech_user","password":"anvitech12345678"})
    assert user.get("/delay-report.xlsx").status_code == 403
```

- [ ] **Step 2: Run — FAIL** (404 / helper undefined).
- [ ] **Step 3: Implement** — refactor the plan setup in `_plan` into `_plan_run_for_report(config)` returning `(plan_run, so_lines, masters)` (running `run_forward` with the same ranks/reserved/operator-overlay as `_plan`); `_plan` calls it. Add the endpoint: `require_admin(request)`, `config = _resolve_config(_load_plan_config())`, `plan_run, so_lines, masters = _plan_run_for_report(config)`, `report = delay_report.build_delay_report(plan_run.schedule, so_lines, plan_run.batches_prioritized, config, masters)`, `data = _delay_report_xlsx(report)`, return `Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="delay-justification-{_ist_today().isoformat()}.xlsx"'})`.
- [ ] **Step 4: Run — PASS.** Then run the full suite: `python3 -m pytest -q` (confirm `_plan` refactor kept everything green, golden included).
- [ ] **Step 5: Commit** — `git commit -m "feat(delay-report): GET /delay-report.xlsx (admin) + _plan_run_for_report helper"`

---

### Task 7: Admin-only download button (Schedule view)

**Files:**
- Modify: `web/app.js` (the Schedule download row, near `dl-shiftwise`, ~line 835)

**Interfaces:**
- Consumes: `GET /delay-report.xlsx`.

- [ ] **Step 1:** Add a button to the Schedule download row HTML string: `'<button id="dl-delay" class="admin-only" title="Per-order justification of every delay">⬇ Download delay justification</button>'`.
- [ ] **Step 2:** Wire it near the other `dl-*` handlers: `const dly = $("dl-delay"); if (dly) dly.onclick = () => { window.location.href = "/delay-report.xlsx"; };`
- [ ] **Step 3: Verify** — `node --check web/app.js`; then browser-drive (local server, Test8 uploaded, operator table seeded from Test8, `DEFAULT_SCHEDULER=new`): as admin the button appears and downloads a 2-sheet xlsx; as the user role the button is hidden (`admin-only`).
- [ ] **Step 4: Commit** — `git commit -m "feat(delay-report): admin-only Download delay justification button"`

---

## Real-data smoke check (after Task 6)

On Test8 (new engine, operator logic on, real 20-operator roster): build the report and assert the invariant holds for **every** order (`Σ hours == span`), and that the worst-late orders' detail names concrete higher-priority blockers. Print the top 5 late orders' `Why` lines to eyeball defensibility.

## Self-review notes

- Spec coverage: reason model (Tasks 1–3), Sheet 1 (Task 4), Sheet 2 (Tasks 1–3 rows + Task 5 layout), xlsx (Task 5), endpoint+UI (Tasks 6–7), admin-only (Task 6), all-orders (Task 1 loops all `so_lines`), invariant (Tasks 1/3 tests). Covered.
- The operator (crew) attribution is intentionally best-effort: it labels machine-free working time as "waiting for a qualified operator" without always naming the person — honest, matching the spec's non-goal.
- Import paths in test snippets (`ScheduleEntry`, `SOLine`, `Batch`, `Routing`, `Machine`, `Masters`, `WorkCalendar`) must be adjusted to the real `engine/models.py` names during Task 1.
