# OS Reserved-Time Modeling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make outsourcing (OS) steps in the item process master reserve their cycle-time as a flat, continuous 24×7, unlimited-parallel, operator-less block on the schedule/Gantt, and treat the `DISAPTCH` misspelling as the dispatch gate.

**Architecture:** Additive changes to the pure engine. Rule 6 gains an OS branch (detected by the machine cell = `OS`) that reserves a wall-clock block before machine allocation; the loader stops treating `OS` as a machine; the order-book's finished-goods gate gains a robust dispatch matcher. No loader sheet-name or masters changes — the uploaded workbook stays Test4-format. With no OS/`DISAPTCH` steps present, behaviour is byte-identical (golden trace unchanged).

**Tech Stack:** Python 3, pytest, openpyxl. Run tests with `python3 -m pytest` (there is no `python` alias).

## Global Constraints

- **Order identity is the `(SO No, Item Code)` pair** — never key by SO# alone.
- **Rules are pure functions** — no global state, no rule calling another rule.
- **All changes additive; golden trace (`tests/golden_trace.json`) must stay unchanged.** Only regenerate it (`REGEN_GOLDEN=1 python3 -m pytest -k golden`) for an intentional logic change — this feature is NOT one.
- **Time basis for OS:** flat per-batch (the cycle-time value, NOT × qty), **no** 90-min setup, **continuous 24×7 wall-clock** (do NOT route through `WorkClock`), **no operator**, **not** added to `machine_free` (unlimited parallel).
- **OS detection keys on the machine cell** (`Allotted`/`Suggested` = `OS`); the process name only marks OS when no real machine is assigned (so the sample's `"CNC OS"` in-house step is NOT OS).
- **Branch:** `os-reserved-time`. Do NOT merge or push to `main` — the owner does that explicitly.
- Baseline before starting: `python3 -m pytest -q` → **198 passed**.

---

### Task 1: Robust dispatch gate matcher (`DISPATCH` / `Dispatch` / `DISAPTCH`)

**Files:**
- Modify: `engine/orderbook.py` (add `is_dispatch`, use it in `finished_gate` ~line 40)
- Test: `tests/test_orderbook.py`

**Interfaces:**
- Produces: `orderbook.is_dispatch(name: str) -> bool` — True for DISPATCH and the DISAPTCH misspelling (case/space-insensitive).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orderbook.py`:

```python
from engine.orderbook import is_dispatch, finished_gate
from engine.models import Routing, Process


def _routing(*names):
    procs = [Process(seq=i + 1, name=n, cycle_time=1, total_time=1,
                     suggested_machine="M", allotted_machine=None)
             for i, n in enumerate(names)]
    return Routing(item_code="X", description="", customer="", rm_type="", moq=None,
                   processes=procs)


def test_is_dispatch_matches_misspelling():
    assert is_dispatch("DISPATCH")
    assert is_dispatch("Dispatch")
    assert is_dispatch("DISAPTCH")      # transposed misspelling in the real data
    assert not is_dispatch("BANDSAW OS")
    assert not is_dispatch("PACKING")


def test_finished_gate_uses_misspelled_dispatch():
    # DISAPTCH is the gate even when it is NOT the last step.
    r = _routing("OP", "DISAPTCH", "STRAGGLER")
    assert finished_gate(r) == "DISAPTCH"


def test_finished_gate_falls_back_to_last_step_without_dispatch():
    r = _routing("OP", "PACKING")
    assert finished_gate(r) == "PACKING"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_orderbook.py::test_is_dispatch_matches_misspelling -v`
Expected: FAIL with `ImportError: cannot import name 'is_dispatch'`.

- [ ] **Step 3: Write minimal implementation**

In `engine/orderbook.py`, near the top helpers (after the `_norm` line ~line 23), add:

```python
import re as _re

DISPATCH_NAMES = {"DISPATCH", "DISAPTCH"}   # accepts the real-data misspelling


def is_dispatch(name) -> bool:
    """True if a process name is the DISPATCH gate — tolerant of case, spaces and
    the transposed misspelling 'DISAPTCH' seen in the real workbook."""
    return _re.sub(r"[^A-Z0-9]", "", str(name or "").upper()) in DISPATCH_NAMES
```

Then in `finished_gate`, replace the loop body test:

```python
    for p in routing.processes:
        if is_dispatch(p.name):
            return p.name
    return routing.processes[-1].name
```

(Remove the now-unused `DISPATCH = "DISPATCH"` constant only if nothing else references it — run `grep -rn "orderbook.DISPATCH\b\|[^_]DISPATCH\b" engine/ tests/` first; if referenced elsewhere, leave it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_orderbook.py -v`
Expected: PASS (new tests + all existing orderbook tests).

- [ ] **Step 5: Commit**

```bash
git add engine/orderbook.py tests/test_orderbook.py
git commit -m "Rule 8 gate: recognize the DISAPTCH misspelling as DISPATCH

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `OS` is never registered as a (provisional) machine

**Files:**
- Modify: `engine/loaders.py` — `_register_provisional` (~line 364)
- Test: `tests/test_loaders.py`

**Interfaces:**
- Consumes: `loaders._validate(masters, so_lines)`, `loaders.normalize_resource_id`.
- Produces: no new public symbol — `OS` is skipped during provisional registration.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_loaders.py`:

```python
from engine.loaders import _validate, normalize_resource_id
from engine.models import Masters, Routing, Process


def test_os_is_not_registered_as_a_machine():
    # An outsourced step (Allotted = OS) must NOT create a phantom 'OS' machine
    # or a PENDING_MASTER_DATA report — OS is a sentinel, not a resource.
    proc = Process(seq=1, name="CNC OS", cycle_time=7200, total_time=None,
                   suggested_machine=None, allotted_machine="OS")
    masters = Masters(routings={"X": Routing(item_code="X", description="", customer="",
                                             rm_type="", moq=None, processes=[proc])})
    _validate(masters, [])
    assert normalize_resource_id("OS") not in masters.machines
    assert not any(r["ref"] == "OS" for r in masters.report)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_loaders.py::test_os_is_not_registered_as_a_machine -v`
Expected: FAIL — `OS` is currently registered as a provisional machine.

- [ ] **Step 3: Write minimal implementation**

In `engine/loaders.py`, at the top of `_register_provisional` (right after `canonical = normalize_resource_id(raw_label)`), skip the OS sentinel:

```python
def _register_provisional(masters: Masters, raw_label: str):
    canonical = normalize_resource_id(raw_label)
    if not canonical or canonical == "OS":
        return   # 'OS' marks outsourcing, not a machine — never a provisional resource
    if canonical in masters.machines:
        return
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_loaders.py -v`
Expected: PASS (new test + existing loader tests).

- [ ] **Step 5: Commit**

```bash
git add engine/loaders.py tests/test_loaders.py
git commit -m "Loader: never treat the OS sentinel as a (provisional) machine

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: OS detection helper `_is_os` (Rule 6)

**Files:**
- Modify: `engine/rules/rule6_allocate.py` — add `_is_os` near `_is_offmachine` (~line 83)
- Test: `tests/test_rule6_os.py` (new)

**Interfaces:**
- Consumes: `parse_resource_candidates`, `normalize_process_name` (already imported at rule6 line 29); `_resolve_candidates(proc)`.
- Produces: `rule6_allocate._is_os(proc) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rule6_os.py`:

```python
"""Rule 6 — OS (outsourcing) steps reserve their cycle-time as a continuous block."""
from datetime import date, datetime, timedelta

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _P(seq, name, cyc, sug=None, allot=None):
    return Process(seq=seq, name=name, cycle_time=cyc, total_time=None,
                   suggested_machine=sug, allotted_machine=allot)


def test_is_os_detects_allotted_os():
    assert rule6_allocate._is_os(_P(1, "CNC OS", 7200, sug=None, allot="OS"))


def test_is_os_name_only_when_no_real_machine():
    # name has 'OS' and no machine -> OS
    assert rule6_allocate._is_os(_P(1, "BANDSAW OS", None, sug=None, allot=None))
    # name has 'OS' BUT a real machine is assigned -> NOT OS (the sample's 'CNC OS')
    assert not rule6_allocate._is_os(_P(1, "CNC OS", 5, sug="CNC1/CNC2", allot=None))
    # ordinary step -> NOT OS
    assert not rule6_allocate._is_os(_P(1, "BANDSAW", 3, sug="BS1", allot=None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rule6_os.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_is_os'`.

- [ ] **Step 3: Write minimal implementation**

In `engine/rules/rule6_allocate.py`, add just below `_is_offmachine` (before `_offmachine_lane`):

```python
def _is_os(proc):
    """True if this process is an OUTSOURCED (OS) step.

    Marked by the machine cell being the sentinel ``OS`` (Allotted or Suggested =
    OS), or — only when NO real machine is assigned — by an ``OS`` word in the
    process name. Keyed on the machine cell on purpose: a step merely NAMED
    '... OS' but given a real machine (e.g. the sample's 'CNC OS' on CNC1/CNC2) is
    an in-house step, NOT outsourcing."""
    if "OS" in parse_resource_candidates(proc.allotted_machine) \
            or "OS" in parse_resource_candidates(proc.suggested_machine):
        return True
    real = [c for c in _resolve_candidates(proc) if c != "OS"]
    return not real and "OS" in normalize_process_name(proc.name).split()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_rule6_os.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/rules/rule6_allocate.py tests/test_rule6_os.py
git commit -m "Rule 6: add _is_os detector (machine-cell keyed, name only if no machine)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Schedule OS steps as a reserved continuous block

**Files:**
- Modify: `engine/rules/rule6_allocate.py` — the per-batch advance pre-loop (~lines 289-308) and the summary notes (~line 407)
- Test: `tests/test_rule6_os.py`

**Interfaces:**
- Consumes: `_is_os` (Task 3), `_qty_for`, `ScheduleEntry`, `timedelta` (already imported at rule6 line 25).
- Produces: OS `ScheduleEntry` rows with `machine == "OS / Outsourced"`, `operator == ""`, `occupancy_min == cycle_time`, `end == start + cycle_time` (wall-clock), and the batch's next process ready at that `end`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule6_os.py`:

```python
def _masters(procs, machines=("M",)):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type="CNC lathe",
                     available_hrs_per_day=19.5) for m in machines}
    masters = Masters(machines=ms, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None, processes=procs)
    return masters


def _batch(qty=10, item="X"):
    return Batch(batch_id="B", item_code=item, item_name="x", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["B"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)   # 2025-03-05 is a Wednesday


def test_os_block_is_flat_not_multiplied_by_qty():
    # 7200-min OS turnaround is 7200 whether the order is 8 pieces or 800.
    procs = [_P(1, "CNC OS", 7200, allot="OS")]
    for q in (8, 800):
        sched = rule6_allocate.run([_batch(q)], config=_cfg(), masters=_masters(procs))
        os_e = [e for e in sched if e.process_seq == 1][0]
        assert os_e.occupancy_min == 7200          # flat, no ×qty, no setup
        assert os_e.machine == "OS / Outsourced"
        assert os_e.operator == ""


def test_os_block_is_continuous_across_a_thursday():
    # 1440-min (1 day) OS starting Wed 08:00 ends Thu 08:00 exactly — it does NOT
    # skip Anvitech's Thursday off (the vendor runs 24x7).
    procs = [_P(1, "CNC OS", 1440, allot="OS")]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    os_e = [e for e in sched if e.process_seq == 1][0]
    assert os_e.start == datetime(2025, 3, 5, 8, 0)
    assert os_e.end == os_e.start + timedelta(minutes=1440)   # == Thu 2025-03-06 08:00


def test_successor_waits_for_full_os_block():
    procs = [_P(1, "CNC OS", 600, allot="OS"), _P(2, "OP", 1, sug="M")]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    os_e = [e for e in sched if e.process_seq == 1][0]
    op_e = [e for e in sched if e.process_seq == 2][0]
    assert op_e.start >= os_e.end            # next process starts only after OS returns


def test_two_orders_can_be_at_os_in_parallel():
    # Unlimited OS capacity: two batches' OS blocks overlap in wall-clock (OS is not
    # a constraining resource).
    procs = [_P(1, "CNC OS", 600, allot="OS")]
    m = _masters(procs)
    b1, b2 = _batch(item="X"), _batch(item="X")
    b2.batch_id = "B2"
    sched = rule6_allocate.run([b1, b2], config=_cfg(), masters=m)
    os_entries = [e for e in sched if e.process_seq == 1]
    assert len(os_entries) == 2
    assert os_entries[0].start == os_entries[1].start      # both start together


def test_blank_cycle_os_is_zero_duration_milestone():
    procs = [_P(1, "PAINTING OS", None, allot="OS"), _P(2, "OP", 1, sug="M")]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    os_e = [e for e in sched if e.process_seq == 1][0]
    assert os_e.occupancy_min == 0 and os_e.start == os_e.end
    assert os_e.machine == "OS / Outsourced"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_rule6_os.py -v`
Expected: FAIL — OS steps currently get read as a machine `OS` (occupancy = cyc×qty+setup) or blocked, not reserved.

- [ ] **Step 3: Write minimal implementation**

In `engine/rules/rule6_allocate.py`:

(a) Just before the guard loop, alongside `offmachine`/`done_steps`/`needs_machine` (~line 276), add a tracker list:

```python
    os_reserved: list = []   # (item_code, seq, name, minutes) of OS turnaround blocks
```

(b) In the per-batch advance pre-loop, insert an OS branch as the FIRST condition (before `_is_offmachine`). Replace:

```python
                p = s["routing"].processes[s["next"]]
                if _is_offmachine(p):
```

with:

```python
                p = s["routing"].processes[s["next"]]
                if _is_os(p):
                    q = _qty_for(s["batch"], p)
                    if q <= 0:                      # already cleared on the floor — skip
                        done_steps.append((s["batch"].item_code, p.seq, p.name))
                        s["next"] += 1
                        continue
                    cyc = p.cycle_time or 0.0
                    start = s["ready"]
                    end = start + timedelta(minutes=cyc) if cyc > 0 else start
                    schedule.append(ScheduleEntry(
                        batch_id=s["batch"].batch_id, item_code=s["batch"].item_code,
                        process_seq=p.seq, process_name=p.name, machine="OS / Outsourced",
                        qty=q, occupancy_min=cyc, start=start, end=end,
                        notes=(f"outsourced (OS) — reserves {cyc:g} min of vendor "
                               f"turnaround (continuous, no in-house machine/operator)"
                               if cyc > 0 else
                               "outsourced (OS) — no turnaround time set yet; shown "
                               "as a milestone"),
                        so_refs=list(s["batch"].source_so_refs), operator="",
                    ))
                    if cyc > 0:
                        os_reserved.append((s["batch"].item_code, p.seq, p.name, cyc))
                        s["ready"] = end            # successor waits for the full block
                    else:
                        offmachine.append((s["batch"].item_code, p.seq, p.name))
                    s["next"] += 1
                elif _is_offmachine(p):
```

(Leave the rest of the `elif`/`else` chain unchanged.)

(c) After the existing `if offmachine:` notes block (~line 407-413), add:

```python
    if os_reserved:
        names = sorted({nm for _, _, nm, _ in os_reserved})
        notes.append(
            f"Reserved {len(os_reserved)} outsourced (OS) step(s) as continuous "
            f"turnaround blocks — no in-house machine or operator; the next process "
            f"waits for each to return (e.g. {', '.join(names[:5])})."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_rule6_os.py tests/test_dispatch_passthrough.py -v`
Expected: PASS — new OS tests AND all existing dispatch/off-machine tests (DISPATCH still "Off-machine" zero-duration; "BANDSAW OS" no-time still "OS / Outsourced" zero-duration and does not push the real op).

- [ ] **Step 5: Commit**

```bash
git add engine/rules/rule6_allocate.py tests/test_rule6_os.py
git commit -m "Rule 6: OS steps reserve a flat continuous turnaround block

Outsourced (OS) steps now reserve their cycle-time as a wall-clock block
(24x7, flat per batch, no setup, no operator, unlimited parallel) on the
'OS / Outsourced' lane; the next process waits for it. Blank-cycle OS stays
a zero-duration milestone until a number is entered.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Exclude OS / off-machine lanes from the machine-utilization view

**Files:**
- Modify: `engine/rules/rule6_allocate.py` — `build_machine_view` (~line 458-471)
- Test: `tests/test_rule6_os.py`

**Interfaces:**
- Consumes: `build_machine_view(schedule, masters, config) -> (timeline, summary)`.
- Produces: `timeline`/`summary` no longer contain the `"OS / Outsourced"` or `"Off-machine"` lanes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule6_os.py`:

```python
def test_os_lane_excluded_from_machine_view():
    procs = [_P(1, "OP", 1, sug="M"), _P(2, "CNC OS", 600, allot="OS")]
    m = _masters(procs)
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=m)
    timeline, summary = rule6_allocate.build_machine_view(sched, m, _cfg())
    lanes = {r["Machine"] for r in summary}
    assert "OS / Outsourced" not in lanes      # not a machine — kept off utilization
    assert "M" in lanes                          # real machines still reported
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rule6_os.py::test_os_lane_excluded_from_machine_view -v`
Expected: FAIL — `"OS / Outsourced"` currently appears in the summary.

- [ ] **Step 3: Write minimal implementation**

In `build_machine_view`, where it builds `by_machine` (~line 469-471), skip the non-machine lanes:

```python
    NON_MACHINE_LANES = {"OS / Outsourced", "Off-machine"}
    by_machine: dict[str, list] = {}
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue   # outsourcing / dispatch lanes are not machines
        by_machine.setdefault(e.machine, []).append(e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_rule6_os.py tests/test_rule6.py -v`
Expected: PASS (new test + all existing Rule 6 tests, incl. `test_machine_view_reports_zero_idle_when_continuous`).

- [ ] **Step 5: Commit**

```bash
git add engine/rules/rule6_allocate.py tests/test_rule6_os.py
git commit -m "Rule 6: keep OS/off-machine lanes out of the machine-utilization view

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite + golden verification

**Files:** none (verification only).

- [ ] **Step 1: Run the entire suite**

Run: `python3 -m pytest -q`
Expected: **all pass** — the original 198 plus the new OS/dispatch/loader tests, with **no** golden-trace failure. If the golden test fails, STOP: something changed rule1/2/3/6 output for the OS-free sample — investigate rather than regenerating.

- [ ] **Step 2: Confirm the golden trace is untouched**

Run: `git status --porcelain tests/golden_trace.json`
Expected: **no output** (file unmodified). Do NOT run `REGEN_GOLDEN=1` — this feature must not change the golden.

---

### Task 7: Update RULES.md and CLAUDE.md

**Files:**
- Modify: `RULES.md` (Rule 6 "Off-machine steps — DISPATCH / OS" subsection)
- Modify: `CLAUDE.md` (the `rule6_allocate.py` / `loaders.py` / `orderbook.py` code-map bullets)

**Interfaces:** docs only — no code, no test.

- [ ] **Step 1: Update RULES.md**

In `RULES.md`, replace the **"Off-machine steps — DISPATCH / OS (shown as milestones, never ignored)"** paragraph (under Rule 6) with a version that distinguishes the two cases. Use this text:

```markdown
**Off-machine steps — DISPATCH vs OS / outsourcing.** A process that runs off any
in-house machine is handled in one of two ways:

- **DISPATCH** (the final "consider it done / shipped" gate) and any other step with
  **no machine and no cycle time** → a **zero-duration milestone** on the
  "Off-machine" lane (or "OS / Outsourced" if its name has an `OS` word). It consumes
  no machine, operator or time. The dispatch gate is matched tolerantly — `DISPATCH`,
  `Dispatch`, and the real-data misspelling `DISAPTCH` all count.
- **OS / outsourcing with a turnaround time** — a step marked `OS` in its Allotted (or
  Suggested) machine cell, carrying a **cycle-time value in minutes** (e.g. `7200`).
  This is scheduled as a **reserved continuous block**: it holds that many minutes of
  vendor turnaround **flat per batch** (NOT × qty, no 90-min setup), runs **continuous
  24×7** (it ignores Anvitech's Thursday-off and shift hours — the vendor works on its
  own clock), takes **no in-house machine or operator**, and has **unlimited parallel
  capacity** (any number of orders can be at OS at once). The next process **waits for
  the full block to finish** (no overlap). Shown as an `OS` bar on the "OS / Outsourced"
  Gantt lane; kept out of the machine-utilization table (it is not a machine). If the
  cycle-time cell is **blank**, the step is a zero-duration milestone until a number is
  entered — then it reserves that block automatically, no code change. An OS step that
  arrives early is closed via Capture Actuals (its per-process remaining hits 0 and the
  next Plan skips it). A blank machine *with* a real cycle time is still NOT off-machine
  — it surfaces as "needs machine" so genuinely missing data fails loud.
```

- [ ] **Step 2: Update CLAUDE.md**

In `CLAUDE.md`, under "Map of the code":
- On the `engine/rules/ruleN_*.py` / Rule 6 bullet, after the `_is_offmachine` description, add: `` `_is_os` (outsourced step — Allotted/Suggested = `OS`, or an `OS` word in the name when no real machine) reserves the **cycle-time as a flat, continuous 24×7, unlimited-parallel, operator-less block** on the "OS / Outsourced" lane; the successor waits for it. A blank OS cycle stays a zero-duration milestone. OS/off-machine lanes are excluded from the machine-utilization view.``
- On the `engine/loaders.py` bullet, add: `the `OS` sentinel is never registered as a (provisional) machine.`
- On the `engine/orderbook.py` bullet (near `finished_gate`), add: `the DISPATCH gate is matched via `is_dispatch` (tolerates the `DISAPTCH` misspelling).`

- [ ] **Step 3: Verify docs render and nothing else changed**

Run: `git diff --stat RULES.md CLAUDE.md`
Expected: only these two files changed, additive.

- [ ] **Step 4: Commit**

```bash
git add RULES.md CLAUDE.md
git commit -m "Docs: OS reserved-time block + DISAPTCH gate in RULES.md/CLAUDE.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- OS detection (machine-cell keyed) → Task 3. ✅
- OS reserved continuous block (flat, 24×7, no operator, unlimited, successor waits) → Task 4. ✅
- Blank-cycle OS = zero-duration pass-through → Task 4 (`test_blank_cycle_os_is_zero_duration_milestone`). ✅
- `OS` never a machine → Task 2. ✅
- Robust DISPATCH/DISAPTCH gate → Task 1. ✅
- OS/off-machine lanes excluded from utilization → Task 5. ✅
- Golden unchanged / full suite green → Task 6. ✅
- Docs (RULES.md/CLAUDE.md) → Task 7. ✅
- Early-arrival via feedback loop → no code (existing per-process remaining); noted in Task 7 docs. ✅
- Content-based sheet detection / masters fallback → explicitly OUT of scope (owner pastes into Test4). Not a task. ✅
- Red-marking (`Production1_flagged.xlsx`) → already delivered (one-off), out of this plan. ✅

**Placeholder scan:** none — every code step shows complete code.

**Type consistency:** `_is_os(proc)` (Task 3) used in Task 4; `is_dispatch(name)` (Task 1) used only in `finished_gate`; `"OS / Outsourced"` / `"Off-machine"` lane strings consistent across Tasks 4-5 and match existing `_offmachine_lane`; `os_reserved` tuple shape `(item, seq, name, minutes)` consistent between its append (Task 4b) and its note (Task 4c). ✅
