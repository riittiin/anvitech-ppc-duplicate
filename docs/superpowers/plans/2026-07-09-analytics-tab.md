# Analytics Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Analytics tab that shows, from the current plan, how fully each machine (and machine type), operator, and process is utilized — with bottleneck/under-used flags — so the owner can see where to optimize.

**Architecture:** All math in a new pure `engine/analytics.py::build_analytics(...)` (unit-testable, no UI/state). `api/main.py` attaches its result to the plan response; a new `web/` Analytics tab renders CSS bars + tables. Machine capacity reuses Rule 6's own per-machine clock so utilization matches how the plan was actually scheduled.

**Tech Stack:** Python 3 + pytest engine; vanilla HTML/JS/CSS frontend (no chart library — strict CSP). Run tests: `python3 -m pytest` (no `python` alias).

## Global Constraints

- **Utilization = Busy ÷ Available × 100**, each resource vs **its own** capacity in the plan window `[min(start), max(end)]`. Never compare against another resource's capacity (unbiased).
- **Machine Available** = `clock_for(machine).working_minutes_between(win_start, win_end)` (Rule 6's `_clock_factory` clock — same shifts/coverage/calendar as the schedule).
- **Operator Available** = `working_days(window) × shift_hours`; shift_hours: First = `first_shift_end_hour − first_shift_start_hour` (11), Second = `24 − first_shift_end_hour + second_shift_end_hour` (10). Busy is exact; Available is a stated shift estimate.
- Exclude `"OS / Outsourced"` and `"Off-machine"` lanes (not machines).
- Thresholds: **bottleneck ≥ 85%**, **under-used ≤ 30%**. `Utilization %` is `None` when Available == 0 (never divide by zero).
- All values JSON-friendly; dates rendered DD-MM-YYYY by the existing `to_table`/`_cell`.
- Additive; golden trace unaffected (analytics is a derived table, not a rule output). Branch `analytics-tab`; do NOT push to `main`.
- Baseline: `python3 -m pytest -q` → **223 passed**.

---

### Task 1: `build_analytics` — window + machines + type rollup

**Files:**
- Create: `engine/analytics.py`
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `ScheduleEntry` (`.machine`, `.occupancy_min`, `.qty`, `.start`, `.end`); `masters.machines[mid]` (`.display_name`, `.machine_type`); `rule6_allocate._clock_factory(masters, config) -> (clock_for, _)`; `WorkClock.working_minutes_between`.
- Produces: `build_analytics(schedule, masters, config, batches=None) -> dict` with keys `window`, `machines` (list of dict), `machine_groups` (list of dict), and placeholders `operators=[]`, `processes=[]`, `headline` (partial). Machine dict keys: `Machine, Type, Busy (hrs), Available (hrs), Utilization %, Idle (hrs), Ops, Pieces, Status`.

- [ ] **Step 1: Write the failing hand-computed test**

Create `tests/test_analytics.py`:

```python
"""Analytics — utilization computed from a plan (hand-verified numbers)."""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate
from engine import analytics


def _masters(procs, machines):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type=t, available_hrs_per_day=hrs)
          for m, t, hrs in machines}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    mm.routings["X"] = Routing(item_code="X", description="RING", customer="", rm_type="",
                               moq=None, processes=procs)
    return mm


def _batch(qty=50):
    return Batch(batch_id="B", item_code="X", item_name="RING", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["SO"])


def test_machine_utilization_is_busy_over_available_in_window():
    # One two-shift machine M (19.5h/day) runs a single op; hand-check the numbers.
    # P1 = 10 min/pc x 50 + 90 setup = 590 min busy on M.
    procs = [Process(1, "CNC", 10, 10, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))          # Wed; sequential; op-logic off
    sched = rule6_allocate.run([_batch(50)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    m = next(r for r in a["machines"] if r["Machine"] == "M")
    # Busy = 590 min = 9.83 hrs
    assert m["Busy (hrs)"] == round(590 / 60.0, 1)
    # Available = the machine's working minutes in the plan window (win = the single op's span)
    clock, _ = rule6_allocate._clock_factory(masters, cfg)
    win_start = min(e.start for e in sched); win_end = max(e.end for e in sched)
    avail = clock("M").working_minutes_between(win_start, win_end) / 60.0
    assert m["Available (hrs)"] == round(avail, 1)
    assert m["Utilization %"] == round(m["Busy (hrs)"] / m["Available (hrs)"] * 100.0, 1)
    assert m["Ops"] == 1 and m["Pieces"] == 50


def test_machine_busy_matches_build_machine_view():
    # Cross-check: analytics Busy (hrs) must equal build_machine_view's Busy (min)/60.
    procs = [Process(1, "CNC", 4, 4, "M", None), Process(2, "VMC", 3, 3, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "VMC", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    _, summary = rule6_allocate.build_machine_view(sched, masters, cfg), None
    timeline_summary = rule6_allocate.build_machine_view(sched, masters, cfg)[1]
    mv = {r["Machine"]: r["Busy (min)"] for r in timeline_summary}
    for r in a["machines"]:
        assert round(r["Busy (hrs)"] * 60.0) == round(mv[r["Machine"]])   # same busy, two paths


def test_machine_group_rollup_by_type():
    # Two CNC lathes -> a "CNC lathe" group whose util = sum(busy)/sum(available).
    procs = [Process(1, "CNC A", 5, 5, "M1", None), Process(2, "CNC B", 5, 5, "M2", None)]
    masters = _masters(procs, [("M1", "CNC lathe", 19.5), ("M2", "CNC lathe", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(40)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    g = next(r for r in a["machine_groups"] if r["Type"] == "CNC lathe")
    tot_busy = sum(r["Busy (hrs)"] for r in a["machines"])
    tot_avail = sum(r["Available (hrs)"] for r in a["machines"])
    assert g["Machines"] == 2
    assert g["Utilization %"] == round(tot_busy / tot_avail * 100.0, 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_analytics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.analytics'`.

- [ ] **Step 3: Create `engine/analytics.py` with the machine section**

```python
"""Analytics — utilization & bottlenecks derived from a plan (pure; no state/UI).

Every resource is measured against ITS OWN available capacity in the plan window
[min(start), max(end)], so a manual station and a two-shift CNC are judged fairly.
Machine capacity reuses Rule 6's per-machine clock, so it matches how the plan ran.
"""
from __future__ import annotations

from collections import defaultdict

from .rules.rule6_allocate import _clock_factory

NON_MACHINE_LANES = {"OS / Outsourced", "Off-machine"}
BOTTLENECK_PCT = 85.0
UNDERUSED_PCT = 30.0


def _util(busy_hrs, avail_hrs):
    """Utilization % (Busy/Available), or None when there is no available capacity."""
    return round(busy_hrs / avail_hrs * 100.0, 1) if avail_hrs > 0 else None


def _status(u):
    if u is None:
        return "no capacity"
    if u >= BOTTLENECK_PCT:
        return "bottleneck"
    if u <= UNDERUSED_PCT:
        return "under-used"
    return "healthy"


def _by_util(rows):
    rows.sort(key=lambda r: (r["Utilization %"] is None, -(r["Utilization %"] or 0.0)))
    return rows


def build_analytics(schedule, masters, config, batches=None):
    """Utilization analytics for one plan. Returns a JSON-able dict with keys:
    ``window``, ``machines``, ``machine_groups``, ``operators``, ``processes``, ``headline``."""
    empty = {"window": None, "machines": [], "machine_groups": [],
             "operators": [], "processes": [], "headline": {}}
    if not schedule:
        return empty

    win_start = min(e.start for e in schedule)
    win_end = max(e.end for e in schedule)
    clock_for, _ = _clock_factory(masters, config)

    def disp(mid):
        m = masters.machines.get(mid)
        return m.display_name if m else mid

    def mtype(mid):
        m = masters.machines.get(mid)
        return m.machine_type if (m and m.machine_type) else "Other"

    # --- Machines (exclude non-machine lanes) ---
    by_machine = defaultdict(list)
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue
        by_machine[e.machine].append(e)

    machines = []
    for mid, ops in by_machine.items():
        busy = sum(e.occupancy_min for e in ops) / 60.0
        avail = clock_for(mid).working_minutes_between(win_start, win_end) / 60.0
        u = _util(busy, avail)
        machines.append({
            "Machine": disp(mid), "Type": mtype(mid),
            "Busy (hrs)": round(busy, 1), "Available (hrs)": round(avail, 1),
            "Utilization %": u, "Idle (hrs)": round(max(avail - busy, 0.0), 1),
            "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)),
            "Status": _status(u),
        })
    _by_util(machines)

    # --- Group rollup by machine type ---
    groups = defaultdict(lambda: {"busy": 0.0, "avail": 0.0, "machines": 0})
    for mid, ops in by_machine.items():
        g = groups[mtype(mid)]
        g["busy"] += sum(e.occupancy_min for e in ops) / 60.0
        g["avail"] += clock_for(mid).working_minutes_between(win_start, win_end) / 60.0
        g["machines"] += 1
    machine_groups = _by_util([
        {"Type": t, "Machines": v["machines"],
         "Busy (hrs)": round(v["busy"], 1), "Available (hrs)": round(v["avail"], 1),
         "Utilization %": _util(v["busy"], v["avail"]), "Status": _status(_util(v["busy"], v["avail"]))}
        for t, v in groups.items()
    ])

    total_busy = round(sum(m["Busy (hrs)"] for m in machines), 1)
    return {
        "window": {"start": win_start, "end": win_end,
                   "makespan_days": (win_end.date() - win_start.date()).days + 1},
        "machines": machines,
        "machine_groups": machine_groups,
        "operators": [],
        "processes": [],
        "headline": {"total_busy_hrs": total_busy,
                     "avg_machine_util": _avg_util(machines)},
    }


def _avg_util(rows):
    vals = [r["Utilization %"] for r in rows if r["Utilization %"] is not None]
    return round(sum(vals) / len(vals), 1) if vals else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_analytics.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/analytics.py tests/test_analytics.py
git commit -m "Analytics: build_analytics machines + type rollup (busy/available/util)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `build_analytics` — operators, processes, headline bottleneck/opportunities

**Files:**
- Modify: `engine/analytics.py` (fill the `operators`, `processes`, and full `headline`)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `config.apply_operator_logic`, `config.first_shift_start_hour` (8), `config.first_shift_end_hour` (19), `config.second_shift_end_hour` (5); `masters.operators` (`.name`, `.shift`); `masters.calendar.is_working_day(date)`; `ScheduleEntry.operator`, `.process_name`.
- Produces: `operators` list (`Operator, Busy (hrs), Available (hrs), Utilization %, Ops, Pieces, Status`); `processes` list (`Process, Work (hrs), Share %, Ops, Pieces, Machines`); `headline` gains `window_start`, `window_end`, `makespan_days`, `bottleneck` (top machine dict or None), `underused` (list of machine dicts ≤ 30%).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analytics.py`:

```python
from engine.models import Operator


def test_process_work_share_sums_to_machine_busy():
    procs = [Process(1, "CNC", 4, 4, "M", None), Process(2, "WASH", 2, 2, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "Manual Washing", 9.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    total_proc = round(sum(p["Work (hrs)"] for p in a["processes"]), 1)
    total_machine = round(sum(m["Busy (hrs)"] for m in a["machines"]), 1)
    assert total_proc == total_machine                      # all work accounted for
    assert round(sum(p["Share %"] for p in a["processes"])) == 100   # shares add to 100


def test_operator_utilization_uses_shift_capacity():
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    assert a["operators"], "operator section populated when operator logic is on"
    o = a["operators"][0]
    assert o["Operator"] == "Op One"
    assert o["Busy (hrs)"] > 0 and 0 <= o["Utilization %"] <= 100


def test_headline_flags_bottleneck_and_underused():
    # M is worked hard; N barely -> M near top, N flagged under-used.
    procs = [Process(1, "HEAVY", 20, 20, "M", None), Process(2, "LIGHT", 1, 1, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "Manual Washing", 9.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(200)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    assert a["headline"]["bottleneck"]["Machine"] == a["machines"][0]["Machine"]
    assert all(m["Utilization %"] <= 30 for m in a["headline"]["underused"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_analytics.py -k "process_work or operator_util or headline_flags" -q`
Expected: FAIL — `operators`/`processes` are empty and `headline` lacks `bottleneck`.

- [ ] **Step 3: Implement operators + processes + headline**

In `engine/analytics.py`, add the helper and replace the `operators`, `processes`, and `headline` parts of `build_analytics`'s return:

```python
def _working_days(calendar, start, end):
    from datetime import timedelta
    d, last, n = start.date(), end.date(), 0
    while d <= last:
        if calendar.is_working_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def _shift_hours(shift, config):
    s = (shift or "").lower()
    if "second" in s:                                   # 19:00 -> 05:00 next day
        return float(24 - config.first_shift_end_hour + config.second_shift_end_hour)
    return float(config.first_shift_end_hour - config.first_shift_start_hour)   # first: 08->19
```

Then inside `build_analytics`, before the `return`, build the three pieces:

```python
    # --- Operators (only meaningful when operator logic assigned them) ---
    operators = []
    if getattr(config, "apply_operator_logic", False):
        by_op = defaultdict(list)
        for e in schedule:
            if e.operator:
                by_op[e.operator].append(e)
        wdays = _working_days(masters.calendar, win_start, win_end)
        shift_of = {o.name: o.shift for o in masters.operators}
        for name, ops in by_op.items():
            busy = sum(e.occupancy_min for e in ops) / 60.0
            avail = wdays * _shift_hours(shift_of.get(name, ""), config)
            u = _util(busy, avail)
            operators.append({
                "Operator": name, "Busy (hrs)": round(busy, 1),
                "Available (hrs)": round(avail, 1), "Utilization %": u,
                "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)), "Status": _status(u),
            })
        _by_util(operators)

    # --- Processes (in-house machine work only; where capacity is spent) ---
    by_proc = defaultdict(list)
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue
        by_proc[e.process_name].append(e)
    proc_total = sum(e.occupancy_min for ops in by_proc.values() for e in ops) / 60.0
    processes = sorted(
        [{"Process": name, "Work (hrs)": round(sum(e.occupancy_min for e in ops) / 60.0, 1),
          "Share %": round(sum(e.occupancy_min for e in ops) / 60.0 / proc_total * 100.0, 1)
          if proc_total else 0.0,
          "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)),
          "Machines": ", ".join(sorted({disp(e.machine) for e in ops}))}
         for name, ops in by_proc.items()],
        key=lambda r: -r["Work (hrs)"])
```

And change the returned dict's `operators`, `processes`, and `headline`:

```python
        "operators": operators,
        "processes": processes,
        "headline": {
            "window_start": win_start, "window_end": win_end,
            "makespan_days": (win_end.date() - win_start.date()).days + 1,
            "total_busy_hrs": total_busy,
            "avg_machine_util": _avg_util(machines),
            "bottleneck": machines[0] if machines else None,
            "underused": [m for m in machines
                          if m["Utilization %"] is not None and m["Utilization %"] <= UNDERUSED_PCT],
        },
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_analytics.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/analytics.py tests/test_analytics.py
git commit -m "Analytics: operators, process work-share, and bottleneck headline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire analytics into the plan response + real-data invariants

**Files:**
- Modify: `api/main.py` (near the machine-view block, ~line 311)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `build_analytics`; `plan_run.schedule`, `plan_run.batches_prioritized`, `masters`, `config`.
- Produces: the plan/trace response carries an `analytics` object.

- [ ] **Step 1: Write the invariants test on the real Test4 masters (self-skips if absent)**

Append to `tests/test_analytics.py`:

```python
import os
import pytest
from engine.loaders import load_all
from engine.config import OVERLAP_PERCENT


@pytest.mark.skipif(not os.path.exists("Test4.xlsx"), reason="real data file not present")
def test_analytics_invariants_on_real_plan():
    from engine.models import Batch
    so, masters = load_all("Test4.xlsx")
    cfg = Config(plan_start_date=date(2026, 7, 1), overlap_mode=OVERLAP_PERCENT,
                 overlap_percent=50, split_parallel=True)
    batches = [Batch(batch_id=f"B{i}", item_code=s.item_code, item_name=s.item_name,
                     qty=s.qty or 10, so_delivery_date=s.delivery_date, source_so_refs=[s.so_no])
               for i, s in enumerate(so[:20])]
    sched = rule6_allocate.run(batches, config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg, batches)
    for m in a["machines"]:
        u = m["Utilization %"]
        assert u is None or 0 <= u <= 100                    # in range
        assert m["Busy (hrs)"] <= m["Available (hrs)"] + 0.1  # busy never exceeds capacity
    # every process hour is some machine's busy hour (no work lost or invented)
    assert round(sum(p["Work (hrs)"] for p in a["processes"]), 0) == \
           round(sum(m["Busy (hrs)"] for m in a["machines"]), 0)
```

- [ ] **Step 2: Run to verify it passes (proves the invariants hold on real data)**

Run: `python3 -m pytest tests/test_analytics.py::test_analytics_invariants_on_real_plan -q`
Expected: PASS (or SKIP if `Test4.xlsx` is absent in CI).

- [ ] **Step 3: Attach analytics to the API response**

In `api/main.py`, import and call it. Find the block that builds the machine view (~line 311) and add, right after the `trace["rule6"]["tables"] = [...]` assignment:

```python
        from engine import analytics as _an
        trace["analytics"] = _an.build_analytics(
            plan_run.schedule, masters, config, plan_run.batches_prioritized)
```

(`trace` is the object returned to the frontend; the analytics tab reads `trace.analytics`.)

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass (was 223; +6 analytics tests). Golden unchanged.

- [ ] **Step 5: Commit**

```bash
git add engine/analytics.py api/main.py tests/test_analytics.py
git commit -m "Analytics: expose in the plan response + real-data invariants test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Frontend — the Analytics tab

**Files:**
- Modify: `web/app.js` (add the tab button, route it, and a `renderAnalytics()` function)
- Modify: `web/style.css` (utilization bar + status colours)

**Interfaces:**
- Consumes: `currentTrace.analytics` (or wherever the trace is stored — mirror how `currentGantt` is set). Uses existing helpers `$`, `escapeHtml`, `to_table`-style column rendering; follow the `renderGantt` pattern.

- [ ] **Step 1: Add the Analytics tab button + route**

In `web/app.js`, where the Gantt tab button is created (~line 189), add an Analytics tab button next to it (same pattern), and in the tab router (`renderTab`, ~line 197) add `if (key === "analytics") { renderAnalytics(); return; }`. Store the analytics payload where the trace is captured (mirror `currentGantt = data.gantt`): `currentAnalytics = data.analytics || (data.trace && data.trace.analytics) || null;` — match the exact field the API puts it under (Task 3 uses `trace.analytics`).

- [ ] **Step 2: Add `renderAnalytics()`**

Add this function to `web/app.js` (bars are pure CSS `<div>` widths — no chart library, CSP-safe):

```javascript
function renderAnalytics() {
  const root = $("tab-content");
  const a = currentAnalytics;
  if (!a || !a.machines || !a.machines.length) {
    root.innerHTML = '<div class="rule-header"><h2>Analytics</h2></div>'
      + '<p class="placeholder">Click <strong>Plan</strong> to build analytics.</p>';
    return;
  }
  const h = a.headline || {};
  const cls = (s) => s === "bottleneck" ? "u-hot" : s === "under-used" ? "u-cold" : "u-ok";
  const pct = (v) => v == null ? "—" : v + "%";
  const bar = (v, s) => `<div class="u-bar"><div class="u-fill ${cls(s)}" style="width:${Math.min(v || 0, 100)}%"></div></div>`;
  const barRow = (label, sub, v, s) =>
    `<div class="u-row"><div class="u-lab">${escapeHtml(label)}<span class="u-sub">${escapeHtml(sub)}</span></div>${bar(v, s)}<div class="u-val">${pct(v)}</div></div>`;

  const bott = h.bottleneck ? `${escapeHtml(h.bottleneck.Machine)} ${pct(h.bottleneck["Utilization %"])}` : "—";
  const under = (h.underused || []).map(m => escapeHtml(m.Machine) + " " + pct(m["Utilization %"])).join(" · ") || "none";
  const headline = `<div class="a-headline">
      <div><strong>Bottleneck:</strong> ${bott}</div>
      <div><strong>Under-used:</strong> ${under}</div>
      <div><strong>Plan:</strong> ${h.makespan_days || "?"} days · ${h.total_busy_hrs || 0} busy hrs · avg machine util ${pct(h.avg_machine_util)}</div>
    </div>`;

  const machineBars = a.machines.map(m => barRow(m.Machine, m.Type, m["Utilization %"], m.Status)).join("");
  const groupBars = a.machine_groups.map(g => barRow(g.Type + " (group)", g.Machines + " machines", g["Utilization %"], g.Status)).join("");
  const opBars = a.operators.length
    ? a.operators.map(o => barRow(o.Operator, "operator", o["Utilization %"], o.Status)).join("")
    : '<p class="muted">Operator logic off — no operator utilization.</p>';
  const procBars = a.processes.map(p => barRow(p.Process, p.Machines, p["Share %"], "ok")).join("");

  const table = (rows) => {
    if (!rows.length) return "";
    const cols = Object.keys(rows[0]);
    const th = cols.map(c => `<th>${escapeHtml(c)}</th>`).join("");
    const tr = rows.map(r => "<tr>" + cols.map(c => `<td>${escapeHtml(String(r[c] == null ? "—" : r[c]))}</td>`).join("") + "</tr>").join("");
    return `<table class="a-table"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
  };

  root.innerHTML = `
    <div class="rule-header"><h2>Analytics — from the current plan</h2></div>
    ${headline}
    <div class="a-grid">
      <div class="a-col"><h3>Machines</h3>${machineBars}<h4>By type</h4>${groupBars}${table(a.machines)}</div>
      <div class="a-col"><h3>Operators</h3>${opBars}${table(a.operators)}</div>
      <div class="a-col"><h3>Processes — where the work is</h3>${procBars}${table(a.processes)}</div>
    </div>
    <p class="g-note">Utilization = busy ÷ the resource's own available time in the plan window (Thursdays/holidays excluded). Process % = share of total machine-hours. Operator capacity assumes the standard shift length.</p>`;
}
```

- [ ] **Step 3: Add the CSS**

In `web/style.css`, append:

```css
.a-headline { display:flex; gap:24px; flex-wrap:wrap; background:var(--panel-2); border:1px solid var(--border);
  border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
.a-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:20px; }
.a-col h3 { margin:0 0 8px; } .a-col h4 { margin:12px 0 6px; color:var(--muted); font-size:12px; }
.u-row { display:flex; align-items:center; gap:8px; margin:3px 0; }
.u-lab { width:150px; font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.u-sub { color:var(--muted); margin-left:6px; font-size:10px; }
.u-bar { flex:1; height:14px; background:var(--panel-2); border-radius:3px; overflow:hidden; }
.u-fill { height:100%; } .u-fill.u-hot { background:#e06666; } .u-fill.u-ok { background:#6aa84f; } .u-fill.u-cold { background:#8a8f9a; }
.u-val { width:44px; text-align:right; font-size:12px; }
.a-table { border-collapse:collapse; margin-top:12px; width:100%; font-size:11px; }
.a-table th, .a-table td { border:1px solid var(--border); padding:3px 6px; white-space:nowrap; }
```

- [ ] **Step 4: Browser-verify with real data**

```bash
rm -rf /tmp/an_store
STORE_DIR=/tmp/an_store nohup python3 -m uvicorn api.main:app --port 8023 >/tmp/an_uv.log 2>&1 &
sleep 3
curl -s -c /tmp/an_ck.txt -X POST http://127.0.0.1:8023/login -d "username=anvitech&password=1930rail" >/dev/null
curl -s -b /tmp/an_ck.txt -F "file=@Test4.xlsx" http://127.0.0.1:8023/upload >/dev/null
curl -s -b /tmp/an_ck.txt -X POST http://127.0.0.1:8023/run -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys,json;a=json.load(sys.stdin).get('analytics') or json.load(open('/dev/stdin'));print('machines:',len(a['machines']),'| bottleneck:',a['headline'].get('bottleneck',{}).get('Machine'))" 2>/dev/null || true
```

Then load the Chrome tools, open `http://127.0.0.1:8023/`, log in, click the **Analytics** tab, screenshot, and confirm: the headline shows a bottleneck + under-used list; machine bars are sorted high→low with red/green/grey; operators + processes render; a machine you know is heavily loaded ranks near the top. Tear down: `pkill -f "uvicorn api.main:app --port 8023"; rm -rf /tmp/an_store`.

- [ ] **Step 5: Commit**

```bash
git add web/app.js web/style.css
git commit -m "Analytics tab: utilization bars + tables for machines, operators, processes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Owner-facing correctness readout + docs

**Files:**
- Modify: `CLAUDE.md` (code-map: add `engine/analytics.py`), `RULES.md` (a short "Analytics view" note under Rule 6's outputs)
- No test (verification + docs)

- [ ] **Step 1: Print the real-data analytics readout for owner sign-off**

Run a script that loads Test4, plans the full book, builds analytics, and prints: the machine table sorted by utilization, the group rollup, top processes by share, and the headline. Confirm the numbers are sane (bottleneck is a genuinely busy machine; sums reconcile). Paste this to the owner as the "these are real numbers" evidence before ship.

```bash
python3 - <<'PY'
from datetime import date
from engine.loaders import load_all
from engine.config import Config, OVERLAP_PERCENT
from engine.models import Batch
from engine.rules import rule6_allocate
from engine import analytics
so, masters = load_all("Test4.xlsx")
cfg = Config(plan_start_date=date(2026,7,1), overlap_mode=OVERLAP_PERCENT, overlap_percent=50,
             apply_operator_logic=True, split_parallel=True)
batches=[Batch(batch_id=f"B{i}",item_code=s.item_code,item_name=s.item_name,qty=s.qty or 10,
               so_delivery_date=s.delivery_date,source_so_refs=[s.so_no]) for i,s in enumerate(so)]
a=analytics.build_analytics(rule6_allocate.run(batches,config=cfg,masters=masters),masters,cfg,batches)
print("MACHINES (util%):"); [print(f"  {m['Machine']:<8} {m['Utilization %']}%  busy {m['Busy (hrs)']}h / avail {m['Available (hrs)']}h  [{m['Status']}]") for m in a['machines']]
print("\nGROUPS:"); [print(f"  {g['Type']:<26} {g['Utilization %']}%") for g in a['machine_groups']]
print("\nTOP PROCESSES:"); [print(f"  {p['Process']:<16} {p['Share %']}%  ({p['Work (hrs)']}h)") for p in a['processes'][:6]]
print("\nHEADLINE:", a['headline'].get('bottleneck',{}).get('Machine'),"is the bottleneck; makespan", a['headline']['makespan_days'],"days")
PY
```

- [ ] **Step 2: Update the docs**

- `CLAUDE.md` "Map of the code": add a bullet — `engine/analytics.py — pure ``build_analytics(schedule, masters, config, batches)``: per-machine (+ type rollup), per-operator, per-process utilization = busy ÷ the resource's own available time in the plan window; bottleneck/under-used flags; surfaced as the Analytics tab. Machine capacity reuses Rule 6's ``_clock_factory``.`
- `RULES.md`: under Rule 6 outputs, add a line noting the Analytics view (utilization/bottlenecks derived from the schedule, unbiased per-resource capacity).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md RULES.md
git commit -m "Docs: Analytics tab (engine/analytics.py) in CLAUDE.md/RULES.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** window + unbiased formula → Task 1 Global Constraints + code. Machines + type rollup → Task 1. Operators → Task 2. Processes → Task 2. Headline (bottleneck/opportunities/totals) → Task 2. API wiring → Task 3. Frontend tab (bars+tables, CSP-safe) → Task 4. Verification: hand-computed → Task 1; cross-check vs build_machine_view → Task 1; invariants on real data → Task 3; owner readout → Task 5. Docs → Task 5. Edge cases (empty plan, Available==0 → None, operator-logic-off) → Task 1/2 code. ✅

**Placeholder scan:** none — complete code in every code step.

**Type consistency:** `build_analytics(schedule, masters, config, batches=None)` signature consistent across Tasks 1-4; dict keys (`"Utilization %"`, `"Busy (hrs)"`, `"Machine"`, `"Status"`, `machines`, `machine_groups`, `operators`, `processes`, `headline`) identical between the engine code, the tests, and the frontend renderer; `_util`/`_status`/`_by_util`/`_avg_util`/`_working_days`/`_shift_hours` all defined in Task 1/2. `trace.analytics` field consistent between Task 3 (API) and Task 4 (frontend). ✅
