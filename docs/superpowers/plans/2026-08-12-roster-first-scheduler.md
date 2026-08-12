# Roster-First Scheduler + Two-Dimensional Optimizer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new scheduling + optimizing engine (`roster_engine/`) that rosters one operator to one CNC/VMC for a whole shift, then flows unsegmented jobs into that capacity — and a contest that searches the crew arrangement and the job order together.

**Architecture:** A shift clock replaces the Giffler-Thompson event loop. At each shift boundary the operator↔machine assignment is *solved* exactly (max-weight bipartite matching); across the plan the optimizer *searches* the job sequence and a crew-priority permutation by alternating descent, with overlap as a continuous outer dimension. The package has zero imports from `ppc_engine/` and plugs into the existing `scheduler_for()` seam, emitting the app's `ScheduleEntry` list so every existing screen renders it unchanged.

**Tech Stack:** Python 3, stdlib only (no new dependency — `requirements.txt` has no scipy, so the assignment solver is written here), `pytest`, FastAPI at the edges.

**Spec:** `docs/superpowers/specs/2026-08-12-roster-first-scheduler-design.md`

## Global Constraints

- `roster_engine/` **MUST NOT import from `ppc_engine/`**, at any depth. Enforced by a test.
- **No new third-party dependency.** `requirements.txt` is not modified.
- **Nothing changes until `DEFAULT_SCHEDULER=roster`.** Every existing test must stay green at every commit; `ppc_engine/`, `engine/rules/`, `engine/flow_scheduler.py`, `web/`, `.github/`, `scripts/` are **not modified**.
- Overlap semantics: `released_pieces = ceil(overlap × qty)`; `overlap = 0.8` means **80 of 100 pieces done**. Range searched: **50–100%**, continuous.
- Rule 1 binds **CNC/VMC only**. Manual/inspection operators are not rostered.
- Setup = `config.setup_time_min` (90) charged on a CNC/VMC only when the machine's previous job had a different `(item_code, process_seq)`.
- Every quantity reaching the scheduler is derived at **batch** level, never per SO line (2026-08-11 rule).
- The objective formula is **identical** to `ppc_engine/objective/objective.py` so the A/B is honest.
- Determinism: same inputs + same seed → same plan, byte for byte.

---

## WIRING & REPLACEMENT — read before Task 1

This is the section that stops the app breaking. It is a contract, not commentary.

### W.1 What is REPLACED, and only when `DEFAULT_SCHEDULER=roster`

Nothing is deleted. Seven dispatch sites currently ask `getattr(config, "scheduler", "classic")` and branch on `"new"`. **All seven must learn `"roster"` in the SAME commit (Task 10).** If some know it and others don't, the plan and the search run *different engines*, and the applied ranks become meaningless while every screen still looks green.

| # | Site | Today | Must become |
| --- | --- | --- | --- |
| 1 | `engine/pipeline.py:142` `scheduler_for` | `"new"` → `new_engine.run` | `"roster"` → `roster_adapter.run` |
| 2 | `engine/optimizer.py:287` `optimize` | `"new"` → `new_engine.optimize_sequence` | `"roster"` → `roster_adapter.optimize_sequence` |
| 3 | `engine/optimizer.py:488` `sweep_optimize` | `"new"` → `new_engine.sweep_optimize` | `"roster"` → `roster_adapter.sweep_optimize` |
| 4 | `engine/optimizer.py:knob_for` | `"flow"` → `flow_chunks`, else `overlap_percent` | `"roster"` → `overlap_percent`, `ROSTER_OVERLAP_CANDIDATES` |
| 5 | `engine/optimize_service.py:56` `cloud_candidates` | `"new"` branch picks the fine grid | `"roster"` uses `CLOUD_ROSTER_OVERLAP_CANDIDATES` |
| 6 | `engine/optimize_service.py:381` `run_candidate` | `"new"` → `new_engine.set_masters_bytes(...)` | `"roster"` → **no-op** (the adapter reads the app `Masters` passed in, never the workbook bytes). Leave the `"new"` branch untouched. |
| 7 | `engine/optimize_service.py:457` `contest_jobs` | `machine_sets = (False, True) if "new"` | `"roster"` keeps `(False,)` — see W.4 |

### W.2 What is ADDED

- `roster_engine/` (Tasks 1–9)
- `engine/roster_adapter.py` — the only file that knows both worlds (Task 7)
- a `crew` genome carried through `optimize_service.build_payload`/`parse_payload`/`ContestSetup`/`run_candidate`, and persisted by `api._optimize_apply` beside `ranks` and `best_overlap` (Task 10)
- four violation checks in `roster_engine/report.py`, appended by `api._report_for_book` for **both** engines (Task 8)

### W.3 What is UNTOUCHED — and must stay byte-identical

`ppc_engine/**`, `engine/rules/**`, `engine/flow_scheduler.py`, `engine/new_engine.py`, `web/**`, `.github/workflows/optimize.yml`, `scripts/cloud_optimize_worker.py`, `requirements.txt`.

The Actions workflow and the cloud worker carry a slightly larger JSON payload and need **no edit** — verified in Task 10 Step 9.

### W.4 The traps — each one has a named test

1. **`config.scheduler` defaults to `"classic"`.** Every site uses `getattr(config, "scheduler", "classic")`. A missed site silently falls back to the classic Rule 6 engine — a *valid* plan, so nothing errors, it's just the wrong engine. → `test_roster_wiring.py::test_every_scheduler_dispatch_site_knows_roster`.
2. **`_inputs_signature` must fold in a roster fingerprint** (`api/main.py:390`). It already hashes `r6.SCHEDULER_FINGERPRINT`, `flow_fingerprint` and `new_engine_fingerprint`. Without `roster_engine_fingerprint`, ranks applied under one version of this engine replay under a changed one behind a green "up to date" banner. → `test_roster_wiring.py::test_inputs_signature_covers_roster_fingerprint`.
3. **`op_segments` shape is a hard contract.** `[(start: datetime, end: datetime, operator: str)]`, sorted by start. The Gantt, shift-wise export, delay report, Analytics operator hours and the efficiency report all read it. A wrong shape breaks five surfaces silently. → `test_roster_entries_contract.py`.
4. **Off-lane machine strings are literals other modules match on.** `"OS / Outsourced"` and `"Off-machine"` (`engine/delay_report.py:42`, `engine/analytics.py:19`, `engine/freeze.py:13`). Emit anything else and outsourcing gets billed to an in-house machine again — the exact 2026-08-09 defect. → `test_roster_entries_contract.py::test_off_lane_names_match_the_consumers`.
5. **`ScheduleEntry.operator` must be non-empty for every real machine op.** `engine/freeze.py` pins machine *and* operator; an empty operator freezes a ghost. → `test_roster_entries_contract.py::test_every_machine_entry_names_an_operator`.
6. **Never re-consolidate.** Rule 1 (`rule1_consolidate`) already clubbed SO lines into batches before the seam. The adapter consumes those batches as-is. `ppc_engine/consolidation.py` is known-broken (CLAUDE.md) and is not imported anyway. → `test_roster_wiring.py::test_adapter_never_reconsolidates`.
7. **Quantities come from the batch.** `Batch.qty` and `Batch.process_remaining` — never a per-SO-line remainder (2026-08-11). → `test_roster_frozen.py::test_frozen_qty_comes_from_the_batch`.
8. **`reserved=` (operator absences) must be honoured.** `run_forward` passes it into the seam; an absent operator must be un-rosterable. Dropping it silently plans work for people on leave. → `test_roster_absences.py`.
9. **`flexible_machines` stays off for roster (v1).** `contest_jobs` doubles the contest for `"new"`. Roster resolves machine options from the routing directly (allotted, falling back to suggested when allotted is blank) and does not search that axis, so the gate must stay `(False,)` or every Actions run costs twice for nothing. → `test_roster_wiring.py::test_roster_contest_does_not_double_for_machine_sets`.
10. **The plan cache keys on the resolved config**, which includes `scheduler`. Flipping `DEFAULT_SCHEDULER` therefore changes `_plan_fingerprint` and cannot serve a stale cross-engine plan. Asserted, not assumed. → `test_roster_wiring.py::test_plan_cache_key_changes_with_scheduler`.

### W.5 Rollback

`DEFAULT_SCHEDULER` back to `new`. No data migration exists to undo: the crew genome is stored inside the optimize meta dict, which older code reads with `.get()` and ignores.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `roster_engine/__init__.py` | Public surface + `SCHEDULER_FINGERPRINT`. |
| `roster_engine/domain.py` | Internal `Shop`/`Job`/`Op` model, built from `engine.models`. Op-kind and machining-machine predicates. |
| `roster_engine/worktime.py` | Shift windows, working days, which machines run which shifts. |
| `roster_engine/assign.py` | Max-weight bipartite matching (Hungarian), pure Python. |
| `roster_engine/roster.py` | Per-shift crew: value matrix → assignment. |
| `roster_engine/release.py` | Whole-piece overlap release, pacing, OS/dispatch rules. |
| `roster_engine/scheduler.py` | The shift clock: roster, run, advance. Emits `Placement`s. |
| `roster_engine/objective.py` | Plan metrics + score (same formula as today). |
| `roster_engine/search.py` | Alternating sequence × crew descent. |
| `roster_engine/report.py` | The four violation checks. |
| `engine/roster_adapter.py` | The seam: `Masters`/`Batch`/`Config` in, `ScheduleEntry` out. |

---

## Task 1: Domain model and the shop clock

**Files:**
- Create: `roster_engine/__init__.py`, `roster_engine/domain.py`, `roster_engine/worktime.py`
- Test: `tests/test_roster_domain.py`

**Interfaces:**
- Consumes: `engine.models` (`Masters`, `Machine`, `Operator`, `Process`, `Batch`), `engine.loaders.parse_resource_candidates`, `engine.orderbook.is_dispatch`
- Produces:
  - `domain.Op(seq:int, name:str, kind:str, cycle_min:float, machine_options:tuple[str,...])`, `kind ∈ {"machining","manual","inspection","outsourced","dispatch"}`
  - `domain.Job(key:str, item_code:str, qty:int, due:date|None, so_refs:tuple[str,...], ops:tuple[Op,...], remaining:dict[int,int]|None)`
  - `domain.Shop(machines:dict[str,Machine], operators:tuple[Operator,...], calendar, machining_ids:frozenset[str])`
  - `domain.build_shop(masters, absent_by_operator:dict[str,list]) -> Shop`
  - `domain.build_jobs(batches, masters) -> tuple[list[Job], dict[str,object], list[str]]` → (jobs, batch_by_key, skipped_item_codes)
  - `domain.is_machining_machine(machine) -> bool`
  - `worktime.ShiftWindow(day:date, shift:str, start:datetime, end:datetime)`, `shift ∈ {"first","second"}`
  - `worktime.iter_shifts(start:datetime, calendar, config) -> Iterator[ShiftWindow]`
  - `worktime.machine_runs_shift(machine, shift:str) -> bool`
  - `worktime.operator_shift(operator) -> str|None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_domain.py
from datetime import date, datetime

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import domain, worktime


def _cfg():
    return Config(plan_start_date=date(2026, 8, 12))


def test_machining_machines_are_cnc_vmc_by_id_or_type():
    assert domain.is_machining_machine(Machine("CNC1", "CNC 1", "misc"))
    assert domain.is_machining_machine(Machine("VMC2", "VMC 2", "misc"))
    assert domain.is_machining_machine(Machine("X1", "X 1", "CNC lathe"))
    assert domain.is_machining_machine(
        Machine("X2", "X 2", "Vertical Machining center"))
    assert not domain.is_machining_machine(Machine("MD1", "MD 1", "manual"))
    assert not domain.is_machining_machine(Machine("MI1", "MI 1", "inspection"))


def test_op_kind_reads_dispatch_os_and_machining():
    masters = Masters(machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe")})
    ops = domain._ops_from_processes([
        Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
        Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
        Process(3, "DEBURING", 1.5, None, None, "MD1"),
        Process(4, "DISPATCH", None, None, None, None),
    ], masters)
    assert [o.kind for o in ops] == [
        "machining", "outsourced", "manual", "dispatch"]
    assert ops[0].machine_options == ("CNC1",)


def test_a_second_shift_window_crosses_midnight_and_thursday_is_off():
    cal = WorkCalendar()                       # Thursday (weekday 3) is the weekly off
    got = list(worktime.iter_shifts(
        datetime(2026, 8, 12, 8, 0), cal, _cfg()))[:4]
    assert (got[0].day, got[0].shift) == (date(2026, 8, 12), "first")
    assert got[0].start == datetime(2026, 8, 12, 8, 0)
    assert got[0].end == datetime(2026, 8, 12, 19, 0)
    assert (got[1].day, got[1].shift) == (date(2026, 8, 12), "second")
    assert got[1].start == datetime(2026, 8, 12, 19, 0)
    assert got[1].end == datetime(2026, 8, 13, 5, 0)     # crosses midnight
    # 2026-08-13 is a Thursday -> skipped entirely
    assert all(w.day != date(2026, 8, 13) for w in got)
    assert got[2].day == date(2026, 8, 14)


def test_a_single_shift_station_runs_only_the_first_shift_0800_1900():
    manual = Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)
    cnc = Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)
    assert worktime.machine_runs_shift(manual, "first")
    assert not worktime.machine_runs_shift(manual, "second")
    assert worktime.machine_runs_shift(cnc, "second")


def test_build_jobs_skips_an_item_with_no_routing_instead_of_raising():
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe")},
        routings={"GOOD": Routing("GOOD", "ok", [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1")])})

    class _B:
        def __init__(self, key, item, qty):
            self.batch_id, self.item_code, self.qty = key, item, qty
            self.so_refs, self.delivery_date = ["SO1"], date(2026, 9, 1)
            self.process_remaining = None

    jobs, by_key, skipped = domain.build_jobs(
        [_B("B1", "GOOD", 10), _B("B2", "MISSING", 5)], masters)
    assert [j.key for j in jobs] == ["B1"]
    assert skipped == ["MISSING"]
    assert by_key["B1"].item_code == "GOOD"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_domain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine'`

- [ ] **Step 3: Write `roster_engine/__init__.py`**

```python
"""A roster-first scheduling engine.

Rule 1 of this shop — one operator mans one machine for a whole shift — is made
true by CONSTRUCTION here, not by a check afterwards: the crew is rostered at the
shift boundary and an operator appears in exactly one machine's roster for that
shift, so a hopping schedule cannot be expressed.

This package has ZERO imports from ppc_engine. It is a from-scratch rebuild, not
a fork; the two engines exist side by side so their plans can be compared on the
same book.

Spec: docs/superpowers/specs/2026-08-12-roster-first-scheduler-design.md
"""

# Bumped whenever a change here can move a plan. api._inputs_signature folds this
# in, so applied ranks searched under an older version are correctly flagged stale
# instead of replaying under new semantics behind a green banner.
SCHEDULER_FINGERPRINT = "roster-engine-v1"
```

- [ ] **Step 4: Write `roster_engine/worktime.py`**

```python
"""When the shop is open, and which shift a machine or a person works.

One definition, used by the roster, the scheduler and the reports — the 2026-08-07
lesson was that a feature which re-derives shift hours WILL disagree with the
engine that built the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

FIRST = "first"
SECOND = "second"

# How far ahead iter_shifts will walk before giving up. A plan that cannot place
# an operation inside a year is a bug, not a long plan — the caller fails loud.
_HORIZON_DAYS = 400


@dataclass(frozen=True)
class ShiftWindow:
    """One shift of one day. ``day`` is the date the shift STARTS on, so a second
    shift running 19:00 -> 05:00 belongs to the earlier date."""

    day: date
    shift: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def _shift_bounds(day: date, shift: str, config) -> tuple[datetime, datetime]:
    if shift == FIRST:
        return (datetime.combine(day, config.first_shift_start),
                datetime.combine(day, config.first_shift_end))
    start = datetime.combine(day, config.second_shift_start)
    end = datetime.combine(day, config.second_shift_end)
    if end <= start:                      # 19:00 -> 05:00 crosses midnight
        end += timedelta(days=1)
    return start, end


def iter_shifts(after: datetime, calendar, config):
    """Every working shift window from ``after`` onwards, in time order.

    A shift already partly gone is still yielded (clipped by the caller against
    its own cursor) — the plan clock can start mid-shift.
    """
    day = after.date()
    for _ in range(_HORIZON_DAYS):
        if calendar.is_working_day(day):
            for shift in (FIRST, SECOND):
                start, end = _shift_bounds(day, shift, config)
                if end > after:
                    yield ShiftWindow(day, shift, start, end)
        day += timedelta(days=1)


def machine_runs_shift(machine, shift: str) -> bool:
    """A two-shift machine (CNC/VMC, Available Hrs/Day >= 12) runs both shifts; a
    single-shift station runs the FIRST shift only.

    First shift is the full 08:00-19:00 window, NOT the legacy 09:00-18:00 manual
    window — that discrepancy hid 9,470 minutes of real planned work from four
    reporting features (2026-08-07). One window, everywhere.
    """
    if shift == FIRST:
        return True
    return bool(machine.is_two_shift())


def operator_shift(operator) -> str | None:
    """Which shift this person works, from the Settings table. Rotation was removed
    2026-08-05: the shift on file is the shift, every week, until an admin edits it."""
    text = (getattr(operator, "shift", "") or "").strip().lower()
    if not text:
        return FIRST
    if "2" in text or "second" in text or "night" in text:
        return SECOND
    return FIRST
```

- [ ] **Step 5: Write `roster_engine/domain.py`**

```python
"""The engine's own view of the shop and the work — built from the app's loaded
objects, never from the workbook.

Deliberately a SEPARATE model from engine.models: this engine reasons about
operations and machine options, while engine.models carries display and
persistence concerns. The translation lives in one place (build_shop/build_jobs)
so the rest of the package never touches an app type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.loaders import parse_resource_candidates
from engine.orderbook import is_dispatch

MACHINING = "machining"
MANUAL = "manual"
INSPECTION = "inspection"
OUTSOURCED = "outsourced"
DISPATCH = "dispatch"

_MACHINING_TYPES = ("cnc lathe", "vertical machining center", "vertical machining centre")
_OS = "OS"


@dataclass(frozen=True)
class Op:
    seq: int
    name: str
    kind: str
    cycle_min: float
    machine_options: tuple


@dataclass(frozen=True)
class Job:
    key: str
    item_code: str
    qty: int
    due: date | None
    so_refs: tuple
    ops: tuple
    remaining: dict | None          # op_seq -> pieces still to make (re-plan)

    def qty_for(self, op_seq: int) -> int:
        """How many pieces this step still owes. Derived at BATCH level — a per-SO-line
        remainder is an INPUT to this, never the answer (2026-08-11: a frozen op ran
        one clubbed line's 88 pieces and left the other line's 281 in no plan at all)."""
        if self.remaining is None:
            return int(self.qty)
        return int(self.remaining.get(op_seq, self.qty))


@dataclass(frozen=True)
class Shop:
    machines: dict
    operators: tuple
    calendar: object
    machining_ids: frozenset
    absent: dict                    # operator name -> list[(start, end)] busy blocks


def is_machining_machine(machine) -> bool:
    """CNC/VMC by id or by Machine-master type — the same rule the 90-minute setup
    uses (RULES.md Rule 4). Duplicated here rather than imported from the classic
    rules so this engine stands alone; a test pins the two in agreement."""
    mid = (getattr(machine, "machine_no", "") or "").upper()
    if mid.startswith("CNC") or mid.startswith("VMC"):
        return True
    return (getattr(machine, "machine_type", "") or "").strip().lower() in _MACHINING_TYPES


def _candidates(proc, masters) -> tuple:
    """Machine ids this step may run on: the Allotted machine(s), falling back to
    Suggested when Allotted is blank. `flexible_machines` (the union) is not searched
    by this engine in v1 — see the plan's W.4 trap 9."""
    for raw in (proc.allotted_machine, proc.suggested_machine):
        ids = [m for m in parse_resource_candidates(raw or "")]
        real = [m for m in ids if m in masters.machines]
        if real:
            return tuple(real)
    return ()


def _is_os(proc) -> bool:
    for raw in (proc.allotted_machine, proc.suggested_machine):
        if (raw or "").strip().upper() == _OS:
            return True
    named_os = _OS in (proc.name or "").upper().split()
    return named_os and not (proc.allotted_machine or proc.suggested_machine)


def _ops_from_processes(processes, masters) -> tuple:
    out = []
    for proc in sorted(processes, key=lambda p: p.seq):
        cycle = float(proc.cycle_time or 0.0)
        if is_dispatch(proc.name or ""):
            kind, options = DISPATCH, ()
        elif _is_os(proc):
            kind, options = OUTSOURCED, ()
        else:
            options = _candidates(proc, masters)
            if not options:
                # No machine and no cycle time -> a visible zero-duration milestone
                # on the Off-machine lane, never silently dropped.
                kind = DISPATCH if cycle <= 0 else OUTSOURCED
            else:
                machine = masters.machines[options[0]]
                if is_machining_machine(machine):
                    kind = MACHINING
                elif "insp" in (machine.machine_type or "").lower() or \
                        (machine.machine_no or "").upper().startswith(("MI", "CMM", "DTC")):
                    kind = INSPECTION
                else:
                    kind = MANUAL
        out.append(Op(int(proc.seq), proc.name or "", kind, cycle, options))
    return tuple(out)


def build_shop(masters, absent_by_operator=None) -> Shop:
    machining = frozenset(
        mid for mid, m in masters.machines.items() if is_machining_machine(m))
    return Shop(machines=dict(masters.machines),
                operators=tuple(masters.operators),
                calendar=masters.calendar,
                machining_ids=machining,
                absent=dict(absent_by_operator or {}))


def build_jobs(batches, masters):
    """Batches (Rule 1's output, already clubbed) -> jobs. Never re-consolidates.

    Returns (jobs, batch_by_key, skipped_item_codes). An item with no routing is
    SKIPPED and reported, never raised — RULES.md's fail-localized rule.
    """
    jobs, by_key, skipped = [], {}, []
    for batch in batches:
        routing = masters.routings.get(batch.item_code)
        if routing is None:
            if batch.item_code not in skipped:
                skipped.append(batch.item_code)
            continue
        ops = _ops_from_processes(routing.processes, masters)
        if not ops:
            if batch.item_code not in skipped:
                skipped.append(batch.item_code)
            continue
        jobs.append(Job(
            key=str(batch.batch_id),
            item_code=batch.item_code,
            qty=int(batch.qty),
            due=getattr(batch, "delivery_date", None),
            so_refs=tuple(getattr(batch, "so_refs", ()) or ()),
            ops=ops,
            remaining=getattr(batch, "process_remaining", None)))
        by_key[str(batch.batch_id)] = batch
    return jobs, by_key, skipped
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_roster_domain.py -v`
Expected: PASS (5 tests)

If `Routing.processes` is named differently, read `engine/models.py:141` and use the real attribute — do not guess.

- [ ] **Step 7: Add the no-ppc_engine guard test**

```python
# append to tests/test_roster_domain.py
import pathlib


def test_roster_engine_never_imports_ppc_engine():
    """The whole point of the rebuild: this engine stands alone, so the two can be
    compared. An import would couple them silently."""
    root = pathlib.Path(__file__).resolve().parent.parent / "roster_engine"
    offenders = [p.name for p in root.rglob("*.py")
                 if "ppc_engine" in p.read_text()]
    assert offenders == []


def test_machining_predicate_agrees_with_the_classic_setup_rule():
    """is_machining_machine is deliberately DUPLICATED rather than imported, so the
    package stands alone. Duplication drifts unless it is pinned — this asserts the
    two agree on every machine, so the 90-minute setup is charged to the same set."""
    from engine.rules.rule6_allocate import _is_setup_machine

    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe"),
        "VMC2": Machine("VMC2", "VMC 2", "Vertical Machining center"),
        "MD1": Machine("MD1", "MD 1", "manual"),
        "MI1": Machine("MI1", "MI 1", "inspection"),
        "MPK3": Machine("MPK3", "MPK 3", "packing"),
        "CMM": Machine("CMM", "CMM", "inspection"),
    }
    masters = Masters(machines=machines)
    for mid, machine in machines.items():
        assert domain.is_machining_machine(machine) == _is_setup_machine(mid, masters), mid
```

- [ ] **Step 8: Run the whole suite — nothing may regress**

Run: `pytest -q`
Expected: all existing tests still pass; 6 new tests pass.

- [ ] **Step 9: Commit**

```bash
git add roster_engine/__init__.py roster_engine/domain.py roster_engine/worktime.py tests/test_roster_domain.py
git commit -m "feat(roster): domain model and shop clock

The engine's own view of shop and work, built from the app's loaded objects
rather than from the workbook, plus one definition of shift windows. Single-
shift stations run the full 08:00-19:00 first shift (the 2026-08-07 lesson),
and a second shift crosses midnight.

Quantity is derived at BATCH level via Job.qty_for — a per-SO-line remainder
is an input to that, never the answer (2026-08-11).

Pinned: roster_engine never imports ppc_engine."
```

---

## Task 2: The assignment solver

**Files:**
- Create: `roster_engine/assign.py`
- Test: `tests/test_roster_assign.py`

**Interfaces:**
- Consumes: nothing
- Produces: `assign.max_weight_matching(values: dict[tuple[int,int], float], n_rows: int, n_cols: int) -> dict[int, int]` — row index → col index, only for pairs with value > 0. Missing keys in `values` mean the pairing is forbidden.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_assign.py
from roster_engine.assign import max_weight_matching


def test_picks_the_best_total_not_the_greedy_first_choice():
    # Greedy would give row 0 its best (col 0, 10) and leave row 1 with 1 -> 11.
    # The optimum is 9 + 8 = 17.
    values = {(0, 0): 10.0, (0, 1): 9.0, (1, 0): 8.0, (1, 1): 1.0}
    assert max_weight_matching(values, 2, 2) == {0: 1, 1: 0}


def test_a_forbidden_pairing_is_never_made():
    values = {(0, 1): 5.0, (1, 0): 5.0}      # (0,0) and (1,1) are not qualified
    assert max_weight_matching(values, 2, 2) == {0: 1, 1: 0}


def test_more_rows_than_columns_leaves_the_worst_rows_unassigned():
    values = {(0, 0): 1.0, (1, 0): 9.0, (2, 0): 5.0}
    assert max_weight_matching(values, 3, 1) == {1: 0}


def test_zero_and_negative_value_pairings_are_left_unassigned():
    values = {(0, 0): 0.0, (1, 1): -3.0, (2, 2): 7.0}
    assert max_weight_matching(values, 3, 3) == {2: 2}


def test_empty_input_is_an_empty_matching():
    assert max_weight_matching({}, 0, 0) == {}
    assert max_weight_matching({}, 3, 3) == {}


def test_result_is_deterministic_under_exact_ties():
    values = {(0, 0): 5.0, (0, 1): 5.0, (1, 0): 5.0, (1, 1): 5.0}
    first = max_weight_matching(values, 2, 2)
    for _ in range(20):
        assert max_weight_matching(values, 2, 2) == first
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_assign.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine.assign'`

- [ ] **Step 3: Write `roster_engine/assign.py`**

```python
"""Maximum-weight bipartite matching — the exact answer to "who mans what".

Within one shift, assigning operators to machines is the classic assignment
problem. It has an exact polynomial algorithm, so the optimizer never has to
SEARCH this: at 20 people x 26 machines it is solved in well under a millisecond,
about a hundred times per plan, which is noise next to the job simulation.

Written here in pure Python on purpose — requirements.txt has no scipy, and this
engine is not allowed to add a dependency. The implementation is the standard
O(n^3) Hungarian method with potentials and shortest augmenting paths.

Determinism: the algorithm is deterministic for a given matrix, and the caller
builds the matrix from stably-sorted rows and columns, so exact ties always
resolve the same way.
"""

from __future__ import annotations

# Any pairing not offered by the caller costs this much, which no real value can
# beat, so the solver will never choose it.
_FORBIDDEN = 1e18


def max_weight_matching(values: dict, n_rows: int, n_cols: int) -> dict:
    """Assign rows to columns, one each, maximising the total value.

    Args:
        values:  {(row, col): value}. A pair absent from this dict is forbidden.
        n_rows:  number of rows (operators).
        n_cols:  number of columns (machines).

    Returns:
        {row: col} for the chosen pairs. Pairs worth <= 0 are dropped: leaving a
        person unrostered is better than putting them on a machine with no work.
    """
    if n_rows <= 0 or n_cols <= 0 or not values:
        return {}

    # The Hungarian routine below minimises and needs rows <= cols, so transpose
    # when there are more operators than machines and flip the answer back.
    transposed = n_rows > n_cols
    if transposed:
        values = {(c, r): v for (r, c), v in values.items()}
        n_rows, n_cols = n_cols, n_rows

    cost = [[_FORBIDDEN] * n_cols for _ in range(n_rows)]
    for (r, c), value in values.items():
        cost[r][c] = -float(value)          # maximise value == minimise -value

    pair = _hungarian(cost, n_rows, n_cols)

    out = {}
    for r, c in pair.items():
        if cost[r][c] >= _FORBIDDEN:
            continue                         # solver had to pad; not a real pairing
        if -cost[r][c] <= 0:
            continue                         # worth nothing — leave them free
        out[c if transposed else r] = r if transposed else c
    return out


def _hungarian(cost, n, m) -> dict:
    """Minimum-cost perfect matching of ``n`` rows into ``m`` columns (n <= m).

    Potentials ``u``/``v`` and shortest augmenting paths (the standard e-maxx
    formulation). ``p[j]`` is the 1-based row currently matched to column ``j``;
    ``p[0]`` is scratch. Returns {row: col}, both 0-based.
    """
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    return {p[j] - 1: j - 1 for j in range(1, m + 1) if p[j] != 0}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_roster_assign.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Add a brute-force cross-check on random matrices**

```python
# append to tests/test_roster_assign.py
import itertools
import random


def test_matches_brute_force_on_random_small_matrices():
    """The solver is exact, so on any matrix small enough to enumerate it must
    equal the best permutation. This is the test that would catch a subtly wrong
    Hungarian implementation, which unit cases would not."""
    rng = random.Random(20260812)
    for _ in range(200):
        n, m = rng.randint(1, 5), rng.randint(1, 5)
        values = {(r, c): float(rng.randint(-3, 20))
                  for r in range(n) for c in range(m)
                  if rng.random() < 0.8}
        got = max_weight_matching(values, n, m)
        got_total = sum(values[(r, c)] for r, c in got.items())

        best = 0.0
        rows, cols = list(range(n)), list(range(m))
        for k in range(min(n, m) + 1):
            for rs in itertools.combinations(rows, k):
                for cs in itertools.permutations(cols, k):
                    if all((r, c) in values for r, c in zip(rs, cs)):
                        best = max(best, sum(values[(r, c)]
                                             for r, c in zip(rs, cs)))
        assert got_total == best, (n, m, values, got)
```

- [ ] **Step 6: Run it**

Run: `pytest tests/test_roster_assign.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Commit**

```bash
git add roster_engine/assign.py tests/test_roster_assign.py
git commit -m "feat(roster): exact operator-to-machine assignment

Who mans what inside one shift is the classic assignment problem, so it is
SOLVED rather than searched: pure-Python Hungarian, no new dependency, well
under a millisecond at 20x26. That is what leaves the whole optimizer budget
for the job flow, which is what actually drives late days.

Cross-checked against brute force on 200 random matrices — the test that
catches a subtly wrong implementation where unit cases would not."
```

---

## Task 3: Whole-piece overlap release

**Files:**
- Create: `roster_engine/release.py`
- Test: `tests/test_roster_release.py`

**Interfaces:**
- Consumes: `roster_engine.domain` (`Op`, kind constants)
- Produces:
  - `release.released_pieces(overlap: float, qty: int) -> int`
  - `release.work_min_before_release(job, op, overlap, setup_min) -> float` — worked minutes on `op` after which its successor may start
  - `release.overlaps(prev: Op, nxt: Op) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_release.py
import pytest

from roster_engine import release
from roster_engine.domain import Job, Op


def _op(seq, kind, cycle=5.0, name="OP"):
    return Op(seq, name, kind, cycle, ("CNC1",) if kind == "machining" else ("MD1",))


def _job(ops, qty=100):
    return Job("B1", "ITEM", qty, None, ("SO1",), tuple(ops), None)


@pytest.mark.parametrize("overlap,qty,expected", [
    (0.8, 100, 80),        # the owner's example: 80 of 100 pieces
    (0.5, 100, 50),
    (1.0, 100, 100),       # fully sequential
    (0.8, 7, 6),           # ceil(5.6) -> a WHOLE piece, never 5.6
    (0.55, 3, 2),          # ceil(1.65)
    (0.8, 1, 1),           # a single piece can never release early
])
def test_release_is_always_a_whole_number_of_pieces(overlap, qty, expected):
    assert release.released_pieces(overlap, qty) == expected


def test_eighty_percent_means_eighty_pieces_not_twenty():
    """The live engine computes (1 - overlap) and so releases at 20 pieces for an
    overlap of 0.8 — the complement of RULES.md:114 and of its own docstring.
    This pins the correct direction."""
    assert release.released_pieces(0.8, 100) == 80
    assert release.released_pieces(0.9, 100) == 90


def test_setup_is_excluded_from_the_percentage_but_still_precedes_cutting():
    job = _job([_op(1, "machining", cycle=5.0)])
    # 90 setup + 80 pieces x 5 min of cutting
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.8, setup_min=90.0) == 90.0 + 400.0


def test_manual_steps_carry_no_setup():
    job = _job([_op(1, "manual", cycle=2.0)])
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.5, setup_min=90.0) == 100.0


def test_os_and_dispatch_never_overlap_in_either_direction():
    assert not release.overlaps(_op(1, "machining"), _op(2, "outsourced"))
    assert not release.overlaps(_op(1, "outsourced"), _op(2, "machining"))
    assert not release.overlaps(_op(1, "machining"), _op(2, "dispatch"))
    assert release.overlaps(_op(1, "machining"), _op(2, "manual"))


def test_a_no_cutting_step_does_not_overlap():
    """RULES.md:127 — a step with no cycle time produces nothing gradually, so its
    successor waits for it to finish."""
    assert not release.overlaps(_op(1, "manual", cycle=0.0), _op(2, "inspection"))


def test_a_finished_step_releases_immediately():
    job = _job([_op(1, "machining")], qty=0)
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.8, setup_min=90.0) == 0.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_release.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine.release'`

- [ ] **Step 3: Write `roster_engine/release.py`**

```python
"""When the next process may start — Rule 5, in whole pieces.

The owner's rule, RULES.md:114 and ppc_engine/config.py:109 all say the same
thing: overlap 0.8 means the successor starts once 80 of 100 pieces are done.
The live engine computes ``(1.0 - overlap) * cutting`` instead — the complement —
which agrees only at exactly 50% and is why it survived. At the overlap the
contest has been converging on (88-95) it really starts successors when 5-12
pieces exist, and the piece-flow guard then re-lays the operation later to repair
the impossibility. This module is the correct definition.

Two further rules from RULES.md, both kept:
  * the percentage measures CUTTING time — the setup is excluded, because the
    next machine's own setup runs while this one cuts;
  * a step with no cycle time produces nothing gradually, so its successor waits
    for it to complete.
"""

from __future__ import annotations

import math

from roster_engine.domain import DISPATCH, MACHINING, OUTSOURCED

# Only these kinds pipeline. OS is a vendor block and DISPATCH is a milestone;
# neither hands pieces over gradually.
_INHOUSE = ("machining", "manual", "inspection")


def released_pieces(overlap: float, qty: int) -> int:
    """How many whole pieces must clear before the successor may start.

    Rounded UP: releasing on 5.6 pieces would start a process on a piece that does
    not exist. Never more than the batch, never less than one.
    """
    qty = int(qty)
    if qty <= 0:
        return 0
    p = min(1.0, max(0.0, float(overlap)))
    return max(1, min(qty, int(math.ceil(p * qty))))


def overlaps(prev, nxt) -> bool:
    """May ``nxt`` start before ``prev`` has finished?"""
    if prev.kind not in _INHOUSE or nxt.kind not in _INHOUSE:
        return False
    return prev.cycle_min > 0.0


def work_min_before_release(job, op, overlap: float, setup_min: float) -> float:
    """Minutes of WORK on ``op`` after which its successor may start.

    Worked minutes, not wall-clock: an overnight gap must not release pieces that
    were never cut. The caller converts this to a moment by tracking how much the
    machine has actually done.
    """
    qty = job.qty_for(op.seq)
    if qty <= 0:
        return 0.0
    setup = float(setup_min) if op.kind == MACHINING else 0.0
    if not overlaps(op, op):                     # no cutting -> no gradual release
        return setup + qty * op.cycle_min
    return setup + released_pieces(overlap, qty) * op.cycle_min
```

Note: `overlaps(op, op)` reads oddly but is exactly the "does this step hand pieces over gradually" question — same kind, positive cycle. Keep it explicit rather than duplicating the predicate.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_roster_release.py -v`
Expected: PASS (14 tests, counting the parametrized cases)

- [ ] **Step 5: Commit**

```bash
git add roster_engine/release.py tests/test_roster_release.py
git commit -m "feat(roster): overlap release in whole pieces

overlap 0.8 = 80 of 100 pieces done, which is the owner's rule, RULES.md:114
and ppc_engine/config.py:109's own docstring. The live engine computes the
complement — (1.0 - overlap) — so its tuned 88-95 really releases successors
at 5-12 pieces. Pinned by a test named for the defect.

Release is ceil()'d to a whole piece: starting a process on 5.6 pieces starts
it on a piece that does not exist."
```

---

## Task 4: The per-shift roster

**Files:**
- Create: `roster_engine/roster.py`
- Test: `tests/test_roster_roster.py`

**Interfaces:**
- Consumes: `roster_engine.assign.max_weight_matching`, `roster_engine.domain.Shop`, `roster_engine.worktime`
- Produces:
  - `roster.CARRY_BONUS: float`, `roster.LOOKAHEAD_UNIT: float`
  - `roster.eligible(operator, machine_id, window, shop) -> bool`
  - `roster.roster_for_shift(window, shop, demand: dict[str,float], in_progress: dict[str,str], crew_rank: dict[str,int]) -> dict[str,str]` → `machine_id -> operator_name`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_roster.py
from datetime import date, datetime

from engine.models import Machine, Masters, Operator, WorkCalendar
from roster_engine import roster
from roster_engine.domain import build_shop
from roster_engine.worktime import ShiftWindow


def _shop(operators, machines=None, absent=None):
    machines = machines or {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        "CNC7": Machine("CNC7", "CNC 7", "CNC lathe", available_hrs_per_day=19.5),
    }
    return build_shop(Masters(machines=machines, operators=operators,
                              calendar=WorkCalendar()), absent or {})


def _win(shift="first"):
    if shift == "first":
        return ShiftWindow(date(2026, 8, 12), "first",
                           datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 19, 0))
    return ShiftWindow(date(2026, 8, 12), "second",
                       datetime(2026, 8, 12, 19, 0), datetime(2026, 8, 13, 5, 0))


def _op(name, machines, shift="First shift"):
    return Operator(name, "/".join(machines), list(machines), shift)


def test_one_operator_gets_at_most_one_machine():
    shop = _shop([_op("Narayan", ["CNC1", "CNC4", "CNC7"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 600.0, "CNC4": 600.0, "CNC7": 600.0}, {}, {})
    assert len(got) == 1
    assert list(got.values()) == ["Narayan"]


def test_the_best_total_coverage_wins_not_the_greedy_first_pick():
    shop = _shop([_op("Narayan", ["CNC1", "CNC4"]), _op("Sidhu", ["CNC1"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 660.0, "CNC4": 500.0}, {}, {})
    # Sidhu can only run CNC1, so Narayan must take CNC4 for both to work.
    assert got == {"CNC1": "Sidhu", "CNC4": "Narayan"}


def test_a_machine_with_no_work_is_left_dark_rather_than_manned():
    shop = _shop([_op("Narayan", ["CNC1", "CNC7"])])
    got = roster.roster_for_shift(_win(), shop, {"CNC1": 400.0, "CNC7": 0.0}, {}, {})
    assert got == {"CNC1": "Narayan"}


def test_qualification_is_exactly_the_settings_machine_list():
    """Role is NOT a gate (2026-08-07): a workbook 'helper' assigned CNC4 in
    Settings must be rosterable on CNC4."""
    shop = _shop([_op("Sandeep", ["CNC4"])])
    got = roster.roster_for_shift(_win(), shop, {"CNC1": 900.0, "CNC4": 100.0}, {}, {})
    assert got == {"CNC4": "Sandeep"}


def test_an_operator_on_the_other_shift_is_not_rostered():
    shop = _shop([_op("Narayan", ["CNC1"], shift="First shift")])
    assert roster.roster_for_shift(_win("second"), shop, {"CNC1": 600.0}, {}, {}) == {}


def test_an_absent_operator_is_not_rostered():
    shop = _shop([_op("Narayan", ["CNC1"])],
                 absent={"Narayan": [(datetime(2026, 8, 12, 0, 0),
                                      datetime(2026, 8, 13, 0, 0))]})
    assert roster.roster_for_shift(_win(), shop, {"CNC1": 600.0}, {}, {}) == {}


def test_a_part_in_the_chuck_keeps_its_machine_manned():
    """Carry-over beats raw demand, or an operation would be segmented at every
    shift boundary."""
    shop = _shop([_op("Narayan", ["CNC1", "CNC4"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 10.0, "CNC4": 900.0}, {"CNC1": "B1"}, {})
    assert got == {"CNC1": "Narayan"}


def test_the_crew_genome_breaks_a_tie_between_equal_machines():
    shop = _shop([_op("Narayan", ["CNC1", "CNC4"])])
    assert roster.roster_for_shift(
        _win(), shop, {"CNC1": 500.0, "CNC4": 500.0}, {},
        {"CNC4": 0, "CNC1": 1}) == {"CNC4": "Narayan"}
    assert roster.roster_for_shift(
        _win(), shop, {"CNC1": 500.0, "CNC4": 500.0}, {},
        {"CNC1": 0, "CNC4": 1}) == {"CNC1": "Narayan"}


def test_the_genome_can_never_man_a_machine_that_has_no_work():
    shop = _shop([_op("Narayan", ["CNC1", "CNC7"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 30.0, "CNC7": 0.0}, {}, {"CNC7": 0, "CNC1": 1})
    assert got == {"CNC1": "Narayan"}


def test_only_cnc_vmc_are_rostered():
    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    shop = _shop([_op("Anturam", ["CNC1", "MD1"])], machines=machines)
    got = roster.roster_for_shift(_win(), shop, {"CNC1": 100.0, "MD1": 900.0}, {}, {})
    assert got == {"CNC1": "Anturam"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_roster.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine.roster'`

- [ ] **Step 3: Write `roster_engine/roster.py`**

```python
"""Who mans which machine, for a whole shift. This is Rule 1.

An operator appears in at most one machine's roster for a shift, so "one operator
hopping between machines mid-shift" is not a thing this engine can express. The
live engine states the same rule in staffing.py's docstring and then implements a
"short-job exception" that books people per-minute; that is the defect this
package exists to remove.

Only CNC/VMC are rostered. A helper physically walks between deburring and
packing, and forbidding that would delete capacity that really exists.
"""

from __future__ import annotations

from roster_engine.assign import max_weight_matching
from roster_engine.worktime import operator_shift

# Continuing a part already in the chuck must beat any amount of raw demand
# elsewhere, or an operation would be segmented at every shift boundary. A shift
# is at most ~660 minutes, so this cannot be outweighed by real work.
CARRY_BONUS = 1_000_000.0

# How much one rank of the crew genome is worth, in "minutes of pending work".
# Big enough that the optimizer can genuinely move a decision; small enough that
# it can never man a machine with nothing to do (that is enforced separately, by
# refusing any pairing whose demand is zero).
LOOKAHEAD_UNIT = 45.0


def eligible(operator, machine_id: str, window, shop) -> bool:
    """May this person man this machine in this shift?

    Qualification is EXACTLY the machine list the admin set in Settings. Role is
    not a gate: it is inherited by name from the workbook's operator sheet, a
    fossil, and gating on it silently discarded the admin's assignment (live
    2026-08-07 — Sandeep Kumar was given CNC4, dropped from its pool for being a
    workbook "helper", and CNC4 sat idle with work waiting).
    """
    if machine_id not in (getattr(operator, "machines", None) or ()):
        return False
    if operator_shift(operator) != window.shift:
        return False
    if not shop.calendar.is_working_day(window.day):
        return False
    for start, end in shop.absent.get(operator.name, ()):
        if start < window.end and window.start < end:
            return False
    return True


def roster_for_shift(window, shop, demand: dict, in_progress: dict,
                     crew_rank: dict) -> dict:
    """Assign operators to CNC/VMC machines for ``window``.

    Args:
        window:      the shift being rostered.
        shop:        the Shop (machines, operators, calendar, absences).
        demand:      machine id -> minutes of work that could run on it this shift.
        in_progress: machine id -> job key of a part physically mid-run on it.
        crew_rank:   machine id -> rank (0 = first claim). The optimizer's lever;
                     empty means no bias.

    Returns:
        {machine_id: operator_name}. A machine absent from the result is dark this
        shift, which is a true constraint, not a failure.
    """
    machines = sorted(
        (mid for mid in shop.machining_ids
         if _runs(shop.machines[mid], window)),
        key=lambda mid: (crew_rank.get(mid, len(shop.machines)), mid))
    operators = sorted(shop.operators, key=lambda o: o.name)
    if not machines or not operators:
        return {}

    n_ranks = len(machines)
    values = {}
    for r, operator in enumerate(operators):
        for c, mid in enumerate(machines):
            if not eligible(operator, mid, window, shop):
                continue
            pending = float(demand.get(mid, 0.0))
            if pending <= 0.0 and mid not in in_progress:
                continue                      # never man a machine with no work
            value = min(window.minutes, pending)
            if mid in in_progress:
                value += CARRY_BONUS
            value += LOOKAHEAD_UNIT * (n_ranks - crew_rank.get(mid, n_ranks))
            values[(r, c)] = value

    matched = max_weight_matching(values, len(operators), len(machines))
    return {machines[c]: operators[r].name for r, c in matched.items()}


def _runs(machine, window) -> bool:
    from roster_engine.worktime import machine_runs_shift
    return machine_runs_shift(machine, window.shift)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_roster_roster.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add roster_engine/roster.py tests/test_roster_roster.py
git commit -m "feat(roster): per-shift crew, solved exactly

One operator, one CNC/VMC, one whole shift — Rule 1 by construction, since a
person can appear in at most one machine's roster. The live engine claims this
in staffing.py's docstring and then books people per-minute.

Carry-over beats demand so a part in the chuck keeps its machine manned and an
operation is never segmented at a shift boundary. The crew genome biases the
assignment but can never man a machine with no work — pinned by a test.

Qualification is exactly the Settings machine list; role is not a gate
(2026-08-07)."
```

---

## Task 5: The shift clock — scheduling without segmentation

**Files:**
- Create: `roster_engine/scheduler.py`
- Test: `tests/test_roster_scheduler.py`

**Interfaces:**
- Consumes: `roster_engine.{domain,worktime,roster,release}`
- Produces:
  - `scheduler.Placement(job_key:str, op_seq:int, op_name:str, kind:str, machine:str|None, qty:int, start:datetime, end:datetime, work_min:float, segments:tuple[tuple[datetime,datetime,str],...])`
  - `scheduler.Plan(placements:tuple[Placement,...], completion:dict[str,datetime])`
  - `scheduler.schedule(jobs, sequence:list[str], shop, config, *, overlap:float, crew_rank:dict[str,int]|None=None, frozen=None) -> Plan`

This task is the largest single unit in the plan; it is not split because a half-written shift clock produces no testable deliverable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_scheduler.py
from datetime import date, datetime

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import scheduler
from roster_engine.domain import build_jobs, build_shop


class _B:
    def __init__(self, key, item, qty, due=date(2026, 12, 1)):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [f"SO-{key}"], due
        self.process_remaining = None


def _masters(processes, operators, machines=None):
    machines = machines or {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5),
    }
    return Masters(machines=machines,
                   routings={"ITEM": Routing("ITEM", "d", processes)},
                   operators=operators, calendar=WorkCalendar())


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0, **kw)


def _run(masters, batches, overlap=1.0, crew_rank=None):
    jobs, _by_key, _skipped = build_jobs(batches, masters)
    shop = build_shop(masters)
    return scheduler.schedule(jobs, [j.key for j in jobs], shop, _cfg(),
                              overlap=overlap, crew_rank=crew_rank or {})


def test_an_operation_is_never_interrupted_by_another_job():
    """No segmentation: once CNC1 starts B1 it finishes B1 before touching B2."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 60), _B("B2", "ITEM", 60)])
    by_job = {p.job_key: p for p in plan.placements if p.machine == "CNC1"}
    assert by_job["B1"].end <= by_job["B2"].start


def test_one_operator_is_never_on_two_machines_in_a_shift():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")],
        [Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 20), _B("B2", "ITEM", 20)])
    used = {p.machine for p in plan.placements if p.machine}
    assert used == {"CNC1"}          # one person -> one machine, so CNC4 stays dark


def test_a_job_crossing_1900_is_handed_to_the_next_shift_operator():
    """The owner's 5 p.m. example: start at 17:00, 90 min setup, work spills past
    19:00, and the night-shift operator continues the SAME operation."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC1", ["CNC1"], "Second shift")])
    plan = _run(masters, [_B("B1", "ITEM", 60)])
    seg = [p for p in plan.placements if p.machine == "CNC1"][0].segments
    assert [s[2] for s in seg] == ["Narayan", "Sidhu"]
    assert seg[0][1] == seg[1][0]        # contiguous — no gap, no second setup


def test_setup_is_charged_once_per_operation_not_per_shift():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC1", ["CNC1"], "Second shift")])
    plan = _run(masters, [_B("B1", "ITEM", 60)])
    p = [x for x in plan.placements if x.machine == "CNC1"][0]
    assert p.work_min == 90.0 + 60 * 10.0


def test_no_new_setup_when_the_same_item_and_process_runs_back_to_back():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 10), _B("B2", "ITEM", 10)])
    work = {p.job_key: p.work_min for p in plan.placements if p.machine == "CNC1"}
    assert work["B1"] == 90.0 + 100.0
    assert work["B2"] == 100.0           # fixture already on -> no second setup


def test_a_manual_step_is_charged_no_setup():
    masters = _masters(
        [Process(1, "DEBURING", 2.0, None, None, "MD1")],
        [Operator("Anturam", "MD1", ["MD1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 30)])
    assert [p.work_min for p in plan.placements if p.machine == "MD1"] == [60.0]


def test_overlap_releases_the_successor_after_whole_pieces():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 10.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 10)], overlap=0.8)
    first = [p for p in plan.placements if p.op_seq == 1][0]
    second = [p for p in plan.placements if p.op_seq == 2][0]
    # 8 of 10 pieces cleared: 90 setup + 8 x 10 = 170 worked minutes in.
    assert second.start >= first.start
    assert second.start < first.end          # genuinely overlapped
    assert first.end > second.start


def test_a_successor_never_starts_before_its_predecessor(monkeypatch):
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 1.0, None, None, "CNC4")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 40)], overlap=0.8)
    first = [p for p in plan.placements if p.op_seq == 1][0]
    second = [p for p in plan.placements if p.op_seq == 2][0]
    assert second.start > first.start
    assert second.end >= first.end           # pacing: never finishes first


def test_os_is_a_flat_lead_time_that_needs_no_machine_or_operator():
    masters = _masters(
        [Process(1, "BAND SAW OS", 2880.0, None, None, "OS"),
         Process(2, "DEBURING", 2.0, None, None, "MD1")],
        [Operator("Anturam", "MD1", ["MD1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 5)])
    os_p = [p for p in plan.placements if p.op_seq == 1][0]
    deb = [p for p in plan.placements if p.op_seq == 2][0]
    assert os_p.machine is None and os_p.segments == ()
    assert (os_p.end - os_p.start).total_seconds() / 60.0 == 2880.0
    assert deb.start >= os_p.end             # fully sequential out of OS


def test_dispatch_waits_for_every_process_of_the_batch():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
         Process(2, "CNC SECOND SIDE", 1.0, None, None, "CNC4"),
         Process(3, "DISPATCH", None, None, None, None)],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
         Operator("Sidhu", "CNC4", ["CNC4"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 20)], overlap=0.5)
    latest = max(p.end for p in plan.placements if p.op_seq in (1, 2))
    dispatch = [p for p in plan.placements if p.op_seq == 3][0]
    assert dispatch.start == dispatch.end == latest
    assert plan.completion["B1"] == latest


def test_a_machine_with_no_rostered_operator_holds_its_job_rather_than_dropping_it():
    """Nobody on the night shift -> the part stays in the chuck and resumes in the
    morning. Honest, and it must not be segmented onto another machine."""
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1")],
        [Operator("Narayan", "CNC1", ["CNC1"], "First shift")])
    plan = _run(masters, [_B("B1", "ITEM", 100)])
    p = [x for x in plan.placements if x.machine == "CNC1"][0]
    assert {s[2] for s in p.segments} == {"Narayan"}
    assert p.work_min == 90.0 + 1000.0
    assert p.end.date() > p.start.date()     # spans days


def test_the_plan_is_deterministic():
    masters = _masters(
        [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")],
        [Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
         Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")])
    batches = [_B("B1", "ITEM", 30), _B("B2", "ITEM", 30), _B("B3", "ITEM", 30)]
    runs = [_run(masters, batches) for _ in range(5)]
    signature = [[(p.job_key, p.op_seq, p.machine, p.start, p.end)
                  for p in sorted(r.placements, key=lambda x: (x.job_key, x.op_seq))]
                 for r in runs]
    assert all(s == signature[0] for s in signature)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine.scheduler'`

- [ ] **Step 3: Write `roster_engine/scheduler.py`**

```python
"""The shift clock — roster the crew, then flow jobs into the capacity it creates.

This replaces the Giffler-Thompson event loop. That loop repeatedly picks one
operation and places it, grabbing operators opportunistically as a side effect, so
there is no moment in it at which the question "is there a machine with work and a
free qualified operator right now?" even exists. Here that question is asked once
per shift, and answered exactly.

    for each shift:
        1. roster  — who mans what (roster.py, an exact assignment)
        2. run     — advance every manned machine through the shift
        3. advance

No segmentation is structural, not checked: a machine holds ONE job from its start
to its end, so there is no hole another job could be dropped into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from roster_engine import release as rel
from roster_engine import roster as crew
from roster_engine.domain import DISPATCH, MACHINING, MANUAL, OUTSOURCED
from roster_engine.worktime import iter_shifts, machine_runs_shift, operator_shift

_EPS = 1e-9


@dataclass(frozen=True)
class Placement:
    job_key: str
    op_seq: int
    op_name: str
    kind: str
    machine: str | None
    qty: int
    start: datetime
    end: datetime
    work_min: float
    segments: tuple = ()          # ((start, end, operator), ...)


@dataclass(frozen=True)
class Plan:
    placements: tuple
    completion: dict


class _JobState:
    __slots__ = ("job", "idx", "ready", "prev_end", "worked", "started")

    def __init__(self, job, plan_start):
        self.job = job
        self.idx = 0
        self.ready = plan_start        # when the next op may START
        self.prev_end = plan_start     # when the previous op actually ENDED
        self.worked = 0.0              # worked minutes on the op in progress
        self.started = None            # when the op in progress started


class _MachineState:
    __slots__ = ("job_key", "op_seq", "remaining", "last_key", "segments", "started")

    def __init__(self):
        self.job_key = None
        self.op_seq = None
        self.remaining = 0.0
        self.last_key = None           # (item_code, op_seq) of the last job run
        self.segments = []
        self.started = None


def schedule(jobs, sequence, shop, config, *, overlap=1.0, crew_rank=None,
             frozen=None) -> Plan:
    """Build a plan. ``sequence`` is the job order — the optimizer's first lever;
    ``crew_rank`` is machine id -> rank — its second."""
    crew_rank = dict(crew_rank or {})
    setup_min = float(getattr(config, "setup_time_min", 90.0) or 0.0)
    plan_start = _plan_start(config)

    by_key = {j.key: j for j in jobs}
    order = [k for k in sequence if k in by_key] + \
            [k for k in by_key if k not in set(sequence)]
    state = {k: _JobState(by_key[k], plan_start) for k in order}
    machines = {mid: _MachineState() for mid in shop.machines}
    priority = {k: i for i, k in enumerate(order)}

    placements: list[Placement] = []
    completion: dict = {}
    if frozen:
        _apply_frozen(frozen, state, machines, by_key)

    for window in iter_shifts(plan_start, shop.calendar, config):
        if not _outstanding(state):
            break
        cursor = max(window.start, plan_start)
        if cursor >= window.end:
            continue

        # Milestones (OS, dispatch, finished steps) need no machine or crew, so
        # settle them before rostering — an OS block released mid-shift creates
        # demand the roster must see.
        placements.extend(_settle_milestones(state, cursor, overlap, setup_min,
                                             completion))

        in_progress = {mid: ms.job_key for mid, ms in machines.items()
                       if ms.job_key is not None}
        demand = _demand(state, shop, window, priority, overlap, setup_min)
        rostered = crew.roster_for_shift(window, shop, demand, in_progress, crew_rank)
        floating = _floating_operators(shop, window, set(rostered.values()))

        for mid in sorted(shop.machines,
                          key=lambda m: (crew_rank.get(m, len(shop.machines)), m)):
            machine = shop.machines[mid]
            if not machine_runs_shift(machine, window.shift):
                continue
            done = _run_machine(mid, machine, machines[mid], state, priority,
                                window, cursor, rostered, floating, shop,
                                overlap, setup_min, config, completion)
            for placement in done:
                placements.append(placement)

        placements.extend(_settle_milestones(state, window.end, overlap,
                                             setup_min, completion))

    unfinished = [k for k, s in state.items() if s.idx < len(s.job.ops)]
    if unfinished:
        # Fail loud rather than silently under-schedule (RULES.md).
        raise RuntimeError(
            "roster scheduler could not place every operation within the horizon; "
            f"unfinished jobs: {sorted(unfinished)[:5]}")

    placements = _pace(placements)
    return Plan(tuple(placements), completion)
```

The remaining private helpers of `scheduler.py` follow the same file. Write them in this order, running the test file after each so failures stay localised:

```python
def _plan_start(config):
    floor = getattr(config, "plan_start_floor", None)
    if floor:
        return floor
    day = config.plan_start_date
    return datetime.combine(day, config.first_shift_start)


def _outstanding(state) -> bool:
    return any(s.idx < len(s.job.ops) for s in state.values())


def _current_op(js):
    return js.job.ops[js.idx] if js.idx < len(js.job.ops) else None


def _advance(js, op, end_at, overlap, setup_min, completion):
    """Record that ``op`` finished at ``end_at`` and open the next one."""
    js.prev_end = max(js.prev_end, end_at)
    js.idx += 1
    js.worked = 0.0
    js.started = None
    nxt = _current_op(js)
    if nxt is None:
        completion[js.job.key] = js.prev_end
    else:
        js.ready = max(js.ready, js.prev_end)


def _settle_milestones(state, now, overlap, setup_min, completion):
    """Place every OS / DISPATCH / already-finished step that is ready by ``now``.

    DISPATCH is placed at the latest end across the whole batch, never at its
    immediate predecessor's release point — an order is dispatched only once every
    piece has cleared every process (RULES.md:305).
    """
    out = []
    progressed = True
    while progressed:
        progressed = False
        for js in state.values():
            op = _current_op(js)
            if op is None:
                continue
            qty = js.job.qty_for(op.seq)
            if op.kind == DISPATCH or (op.kind != OUTSOURCED and not op.machine_options
                                       and qty <= 0):
                at = js.prev_end
                if at > now:
                    continue
                out.append(Placement(js.job.key, op.seq, op.name, DISPATCH, None,
                                     0, at, at, 0.0, ()))
                _advance(js, op, at, overlap, setup_min, completion)
                progressed = True
            elif op.kind == OUTSOURCED:
                start = js.ready
                if start > now:
                    continue
                end = start + timedelta(minutes=float(op.cycle_min or 0.0))
                out.append(Placement(js.job.key, op.seq, op.name, OUTSOURCED, None,
                                     int(qty), start, end, float(op.cycle_min or 0.0),
                                     ()))
                _advance(js, op, end, overlap, setup_min, completion)
                progressed = True
            elif qty <= 0:
                at = max(js.ready, js.prev_end)
                if at > now:
                    continue
                out.append(Placement(js.job.key, op.seq, op.name, op.kind, None,
                                     0, at, at, 0.0, ()))
                _advance(js, op, at, overlap, setup_min, completion)
                progressed = True
    return out


def _demand(state, shop, window, priority, overlap, setup_min):
    """Minutes of work each machine could run during ``window``.

    This is what the roster maximises coverage of. It counts an operation only for
    machines its routing allows, and only once its predecessor has released pieces
    by the END of the window — work that arrives mid-shift still deserves crew.
    """
    out = {}
    for key in sorted(state, key=lambda k: priority[k]):
        js = state[key]
        op = _current_op(js)
        if op is None or not op.machine_options:
            continue
        if js.ready >= window.end:
            continue
        qty = js.job.qty_for(op.seq)
        if qty <= 0:
            continue
        work = (setup_min if op.kind == MACHINING else 0.0) + qty * op.cycle_min
        work -= js.worked
        for mid in op.machine_options:
            out[mid] = out.get(mid, 0.0) + max(0.0, work)
    return out


def _floating_operators(shop, window, rostered_names):
    """Everyone on this shift who is NOT locked to a CNC/VMC — the pool that staffs
    manual and inspection stations, where a person genuinely walks between machines."""
    return [o for o in sorted(shop.operators, key=lambda o: o.name)
            if o.name not in rostered_names
            and operator_shift(o) == window.shift
            and not any(s < window.end and window.start < e
                        for s, e in shop.absent.get(o.name, ()))]


def _run_machine(mid, machine, ms, state, priority, window, cursor, rostered,
                 floating, shop, overlap, setup_min, config, completion):
    """Advance one machine through one shift window. Returns finished placements.

    ``completion`` is the plan's real dict, threaded through rather than kept in a
    module global — the optimizer builds thousands of plans in one process, and a
    global would leak one plan's completions into the next.
    """
    out = []
    now = max(cursor, window.start)
    is_machining = mid in shop.machining_ids
    while now < window.end - timedelta(seconds=1):
        if ms.job_key is None:
            picked = _next_job(mid, state, priority, now)
            if picked is None:
                return out
            js, op = picked
            qty = js.job.qty_for(op.seq)
            setup = setup_min if (op.kind == MACHINING
                                  and ms.last_key != (js.job.item_code, op.seq)) else 0.0
            ms.job_key, ms.op_seq = js.job.key, op.seq
            ms.remaining = setup + qty * op.cycle_min
            ms.segments, ms.started = [], None
            js.started = None
            js.worked = 0.0
        js = state[ms.job_key]
        op = js.job.ops[js.idx]

        operator = _operator_for(mid, is_machining, rostered, floating, window,
                                 now, shop)
        if operator is None:
            return out                       # machine dark this shift; job HELD

        take = min(ms.remaining, (window.end - now).total_seconds() / 60.0)
        if take <= _EPS:
            return out
        seg_end = now + timedelta(minutes=take)
        ms.segments.append((now, seg_end, operator))
        if ms.started is None:
            ms.started = now
            js.started = now
        ms.remaining -= take
        js.worked += take
        now = seg_end

        if ms.remaining <= _EPS:
            work = sum((e - s).total_seconds() / 60.0 for s, e, _ in ms.segments)
            out.append(Placement(js.job.key, op.seq, op.name, op.kind, mid,
                                 int(js.job.qty_for(op.seq)), ms.started, seg_end,
                                 work, tuple(ms.segments)))
            ms.last_key = (js.job.item_code, op.seq)
            _release_successor(js, op, overlap, setup_min)
            _advance(js, op, seg_end, overlap, setup_min, completion)
            ms.job_key = ms.op_seq = None
            ms.segments, ms.started = [], None
    return out
```

```python
def _next_job(mid, state, priority, now):
    """The next job this machine should start: sequence order, then due date.

    The job SEQUENCE is the optimizer's first lever, so it decides who gets a
    contended machine.
    """
    best = None
    for key in sorted(state, key=lambda k: priority[k]):
        js = state[key]
        op = _current_op(js)
        if op is None or mid not in op.machine_options:
            continue
        if js.ready > now:
            continue
        if js.job.qty_for(op.seq) <= 0:
            continue
        best = (js, op)
        break
    return best


def _operator_for(mid, is_machining, rostered, floating, window, now, shop):
    """Who works this machine right now.

    A CNC/VMC takes its ROSTERED operator and nobody else — that is Rule 1. A
    manual or inspection station takes any floating (unrostered) person qualified
    for it, because a helper walks between stations.
    """
    if is_machining:
        return rostered.get(mid)
    for operator in floating:
        if mid in (getattr(operator, "machines", None) or ()):
            return operator.name
    return None


def _release_successor(js, op, overlap, setup_min):
    """Open the next operation once whole pieces have cleared this one."""
    if js.idx + 1 >= len(js.job.ops):
        return
    nxt = js.job.ops[js.idx + 1]
    if not rel.overlaps(op, nxt) or js.started is None:
        return
    need = rel.work_min_before_release(js.job, op, overlap, setup_min)
    # Worked minutes, not wall clock: an overnight gap must not release pieces
    # that were never cut. js.worked is the machine's real progress on this op.
    js.ready = min(js.ready if js.ready else js.prev_end, js.prev_end)
    js.ready = js.started + timedelta(minutes=need)


def _pace(placements):
    """A step's END is never before an earlier step's END (RULES.md:132).

    With the overlap direction corrected this rarely binds — a successor released
    at 80% of its predecessor generally finishes after it anyway. Kept because a
    fast step after a slow one can still be starved, and a step finishing before
    its predecessor delivered the last piece would dispatch parts that skipped it.
    """
    by_job = {}
    for p in placements:
        by_job.setdefault(p.job_key, []).append(p)
    out = []
    for key, items in by_job.items():
        items.sort(key=lambda p: p.op_seq)
        floor = None
        for p in items:
            if floor is not None and p.end < floor:
                p = Placement(p.job_key, p.op_seq, p.op_name, p.kind, p.machine,
                              p.qty, p.start, floor, p.work_min, p.segments)
            floor = p.end if floor is None else max(floor, p.end)
            out.append(p)
    out.sort(key=lambda p: (p.start, p.job_key, p.op_seq))
    return out


def _apply_frozen(frozen, state, machines, by_key):
    """Pin in-progress operations to the machine they are physically on.

    Implemented in Task 6. Until then this is a no-op so `frozen=None` callers
    behave identically.
    """
    return
```

`_release_successor` above has a redundant first assignment to `js.ready`; delete it and keep only `js.ready = js.started + timedelta(minutes=need)`. Clarity matters more here than a clever one-liner — this is the line that encodes the whole overlap rule.

- [ ] **Step 4: Run the tests until they pass**

Run: `pytest tests/test_roster_scheduler.py -v`
Expected: PASS (12 tests)

Debug against the tests, not by reading the live engine — its behaviour is the thing being replaced.

- [ ] **Step 5: Verify nothing else regressed**

Run: `pytest -q`
Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add roster_engine/scheduler.py tests/test_roster_scheduler.py
git commit -m "feat(roster): the shift clock

Roster the crew at each shift boundary, then flow jobs into the capacity it
creates. A machine holds ONE job from start to end, so there is no hole another
job could be dropped into — no segmentation is structural here, not checked.

A job crossing 19:00 is handed to the next shift's rostered operator and keeps
going: one setup, one operation, two names on the bar (the owner's 5 p.m.
example). Setup is skipped entirely when the same (item, process) runs back to
back — the fixture is already on.

A machine with nobody rostered HOLDS its part rather than dropping it. That is
honest: the part is in the chuck."
```

---

## Task 6: Frozen in-progress work

**Files:**
- Modify: `roster_engine/scheduler.py` (`_apply_frozen`)
- Create: `tests/test_roster_frozen.py`

**Interfaces:**
- Consumes: the frozen rows the app already computes — `engine/freeze.compute_frozen_set` produces dicts keyed by machine id with `order_key`, `op_seq`, `machine_id`, `operator`, `remaining_qty`, `prev_start`.
- Produces: `_apply_frozen(frozen, state, machines, by_key)` pins each in-progress op.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_frozen.py
from datetime import date, datetime

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import scheduler
from roster_engine.domain import build_jobs, build_shop


class _B:
    def __init__(self, key, item, qty, remaining=None):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = ["SO1", "SO2"], date(2026, 12, 1)
        self.process_remaining = remaining


def _fixture(remaining=None):
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
                  "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5)},
        routings={"ITEM": Routing("ITEM", "d", [
            Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4"),
            Process(2, "CNC SECOND SIDE", 5.0, None, None, "CNC4")])},
        operators=[Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                   Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B("B1", "ITEM", 535, remaining)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), Config(plan_start_date=date(2026, 8, 12),
                                             setup_time_min=90.0)


def _frozen(**kw):
    row = {"order_key": "B1", "op_seq": 1, "machine_id": "CNC1",
           "operator": "Narayan", "remaining_qty": 100, "prev_start": datetime(2026, 8, 11, 8, 0)}
    row.update(kw)
    return [row]


def test_a_frozen_op_stays_on_the_machine_it_is_physically_on():
    jobs, shop, cfg = _fixture()
    plan = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0,
                              frozen=_frozen())
    first = [p for p in plan.placements if p.op_seq == 1][0]
    assert first.machine == "CNC1"


def test_a_resumed_op_is_charged_no_setup():
    jobs, shop, cfg = _fixture()
    plan = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0,
                              frozen=_frozen(remaining_qty=100))
    first = [p for p in plan.placements if p.op_seq == 1][0]
    assert first.work_min == 100 * 10.0            # no 90-minute setup on resume


def test_frozen_qty_comes_from_the_batch_not_the_row():
    """2026-08-11: a frozen row derived remaining_qty per SO LINE while the op it
    pins is a BATCH operation, so a clubbed order lost 281 pieces to no plan at
    all. The quantity must come from the batch's own process_remaining."""
    jobs, shop, cfg = _fixture(remaining={1: 242})
    plan = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0,
                              frozen=_frozen(remaining_qty=88))
    first = [p for p in plan.placements if p.op_seq == 1][0]
    assert first.qty == 242
    assert first.work_min == 242 * 10.0


def test_a_frozen_op_never_runs_before_the_step_that_feeds_it():
    """2026-08-09: frozen ops were laid at their machine's free time with no
    reference to the routing, so a free machine ran step 2 before a busy machine
    could run step 1."""
    jobs, shop, cfg = _fixture()
    plan = scheduler.schedule(
        jobs, ["B1"], shop, cfg, overlap=1.0,
        frozen=[{"order_key": "B1", "op_seq": 2, "machine_id": "CNC4",
                 "operator": "Sidhu", "remaining_qty": 50,
                 "prev_start": datetime(2026, 8, 11, 8, 0)}])
    first = [p for p in plan.placements if p.op_seq == 1][0]
    second = [p for p in plan.placements if p.op_seq == 2][0]
    assert second.start >= first.start
    assert second.end >= first.end


def test_the_pinned_operator_is_dropped_if_settings_no_longer_qualifies_him():
    """The machine pin stays (the work is physically there); only the person is
    re-staffed. Live 2026-08-03: removing a machine from someone with work in
    progress froze them straight back onto it."""
    jobs, shop, cfg = _fixture()
    shop = shop.__class__(machines=shop.machines,
                          operators=(Operator("Sidhu", "CNC1", ["CNC1"], "First shift"),),
                          calendar=shop.calendar,
                          machining_ids=shop.machining_ids, absent={})
    plan = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0, frozen=_frozen())
    first = [p for p in plan.placements if p.op_seq == 1][0]
    assert first.machine == "CNC1"
    assert {s[2] for s in first.segments} == {"Sidhu"}


def test_frozen_none_is_identical_to_no_frozen_argument():
    jobs, shop, cfg = _fixture()
    a = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0)
    b = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0, frozen=None)
    c = scheduler.schedule(jobs, ["B1"], shop, cfg, overlap=1.0, frozen=[])
    sig = lambda p: [(x.job_key, x.op_seq, x.machine, x.start, x.end) for x in p.placements]
    assert sig(a) == sig(b) == sig(c)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_frozen.py -v`
Expected: FAIL — frozen rows are ignored, so `test_a_frozen_op_stays_on_the_machine_it_is_physically_on` and its siblings fail.

- [ ] **Step 3: Implement `_apply_frozen`**

```python
def _apply_frozen(frozen, state, machines, by_key, shop):
    """Pin every in-progress operation to the machine it is physically on.

    A frozen row pins WHERE and WHEN, never HOW MUCH. The quantity comes from the
    BATCH (`Job.qty_for`), which is the same expression the main loop uses — a row
    derives its own remainder per SO LINE, and on a clubbed batch that silently
    dropped 281 pieces into no plan at all (live 2026-08-11).

    Rows are collapsed to ONE pin per (job, op): several clubbed lines can each be
    in progress on the same step, and an operation runs once, on one machine.
    """
    pins = {}
    for row in frozen or ():
        key = (row.get("order_key"), int(row.get("op_seq", -1)))
        if key[0] not in by_key or key[1] < 0:
            continue
        if row.get("machine_id") not in machines:
            continue
        prev = pins.get(key)
        if prev is None or (row.get("prev_start"), str(key[0])) < \
                (prev.get("prev_start"), str(key[0])):
            pins[key] = row

    for (job_key, op_seq), row in pins.items():
        js = state.get(job_key)
        if js is None:
            continue
        idx = next((i for i, op in enumerate(js.job.ops) if op.seq == op_seq), None)
        if idx is None:
            continue
        js.pinned[op_seq] = (row["machine_id"], row.get("operator") or "")
```

Add `pinned` to `_JobState.__slots__`, initialised to `{}`. Then in `_next_job`, refuse a machine that is not the pin, and in `_run_machine` charge no setup and prefer the pinned operator:

```python
# in _next_job, after the machine_options check
pin = js.pinned.get(op.seq)
if pin is not None and pin[0] != mid:
    continue

# in _run_machine, where setup is computed
pin = js.pinned.get(op.seq)
setup = 0.0 if pin is not None else (
    setup_min if (op.kind == MACHINING
                  and ms.last_key != (js.job.item_code, op.seq)) else 0.0)
```

And in `_operator_for`, when the machine's rostered operator is being chosen, `roster_for_shift` already receives the pinned machine through `in_progress`; pass the pinned operator name as a *preference* by seeding `crew_rank` — simpler and sufficient: leave `_operator_for` alone. The pinned person is re-staffed automatically when Settings no longer qualifies them, which is exactly the required behaviour.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_roster_frozen.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Mutation-test the fix, as this codebase requires**

Temporarily change `_apply_frozen` to take `remaining_qty` from the row instead of the batch. Run `pytest tests/test_roster_frozen.py -v`.
Expected: `test_frozen_qty_comes_from_the_batch_not_the_row` FAILS.
Then revert the mutation and confirm it passes again. A test that passes under the mutation is not testing anything — this fixture family has passed vacuously before (CLAUDE.md, 2026-08-09).

- [ ] **Step 6: Commit**

```bash
git add roster_engine/scheduler.py tests/test_roster_frozen.py
git commit -m "feat(roster): honour frozen in-progress work

A frozen row pins WHERE and WHEN, never HOW MUCH: the quantity comes from the
batch, the same expression the main loop uses. Deriving it per SO line is what
dropped 281 pieces of a clubbed order into no plan at all (2026-08-11), and
rows are collapsed to one pin per (job, op) so a clubbed batch cannot lay the
same operation twice.

A resumed op pays no setup. The machine pin holds even when Settings no longer
qualifies the pinned person — the work is physically there; only the operator
is re-staffed (2026-08-03).

Mutation-tested: taking the quantity from the row again fails the named test."
```

---

## Task 7: The adapter and the pipeline seam — first end-to-end plan

**Files:**
- Create: `engine/roster_adapter.py`
- Modify: `engine/pipeline.py` (`scheduler_for`)
- Create: `tests/test_roster_entries_contract.py`

**Interfaces:**
- Consumes: everything above; `engine.models.ScheduleEntry`
- Produces:
  - `roster_adapter.run(batches, config=None, notes=None, masters=None, machine_lost_min=None, reserved=None, frozen=None, **kw) -> list[ScheduleEntry]`
  - `roster_adapter.OS_LANE = "OS / Outsourced"`, `roster_adapter.OFF_LANE = "Off-machine"`

- [ ] **Step 1: Write the failing contract tests**

```python
# tests/test_roster_entries_contract.py
from datetime import date, datetime

from engine import roster_adapter
from engine.config import Config
from engine.models import (Batch, Machine, Masters, Operator, Process, Routing,
                           ScheduleEntry, WorkCalendar)


def _masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
                  "MD1": Machine("MD1", "MD 1", "manual", available_hrs_per_day=9.5)},
        routings={"ITEM": Routing("ITEM", "d", [
            Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1"),
            Process(2, "BAND SAW OS", 2880.0, None, None, "OS"),
            Process(3, "DEBURING", 2.0, None, None, "MD1"),
            Process(4, "DISPATCH", None, None, None, None)])},
        operators=[Operator("Narayan", "CNC1", ["CNC1"], "First shift"),
                   Operator("Anturam", "MD1", ["MD1"], "First shift")],
        calendar=WorkCalendar())


def _batches():
    b = Batch(batch_id="B1", item_code="ITEM", qty=20,
              delivery_date=date(2026, 12, 1), so_refs=["26-27SO1"])
    return [b]


def _cfg():
    return Config(plan_start_date=date(2026, 8, 12), scheduler="roster",
                  setup_time_min=90.0, overlap_percent=80)


def _run():
    return roster_adapter.run(_batches(), config=_cfg(), masters=_masters())


def test_returns_schedule_entries():
    entries = _run()
    assert entries and all(isinstance(e, ScheduleEntry) for e in entries)


def test_op_segments_are_start_end_operator_triples_in_order():
    for e in _run():
        assert isinstance(e.op_segments, list)
        for seg in e.op_segments:
            assert len(seg) == 3
            assert isinstance(seg[0], datetime) and isinstance(seg[1], datetime)
            assert isinstance(seg[2], str)
        starts = [s[0] for s in e.op_segments]
        assert starts == sorted(starts)


def test_every_machine_entry_names_an_operator():
    """engine/freeze.py pins machine AND operator; an empty operator freezes a ghost."""
    for e in _run():
        if e.machine in (roster_adapter.OS_LANE, roster_adapter.OFF_LANE):
            continue
        assert e.operator, f"{e.process_name} has no operator"
        assert all(seg[2] for seg in e.op_segments)


def test_off_lane_names_match_the_consumers():
    """delay_report._OFF_LANES, analytics.NON_MACHINE_LANES and freeze._OS_LANES all
    match on these exact strings. Emit anything else and outsourcing gets billed to
    an in-house machine again (the 2026-08-09 defect)."""
    from engine.analytics import NON_MACHINE_LANES
    from engine.delay_report import _OFF_LANES
    from engine.freeze import _OS_LANES

    lanes = {roster_adapter.OS_LANE, roster_adapter.OFF_LANE}
    assert lanes == _OFF_LANES == NON_MACHINE_LANES == _OS_LANES
    assert any(e.machine == roster_adapter.OS_LANE for e in _run())


def test_so_refs_batch_id_and_qty_survive_onto_every_entry():
    for e in _run():
        assert e.batch_id == "B1"
        assert e.item_code == "ITEM"
        assert list(e.so_refs) == ["26-27SO1"]


def test_operator_label_renders_a_shift_handoff():
    e = ScheduleEntry("B1", "ITEM", 1, "P", "CNC1", 10, 60.0,
                      datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 20, 0),
                      op_segments=[(datetime(2026, 8, 12, 8, 0),
                                    datetime(2026, 8, 12, 19, 0), "Narayan"),
                                   (datetime(2026, 8, 12, 19, 0),
                                    datetime(2026, 8, 12, 20, 0), "Sidhu")])
    assert e.operator_label() == "Narayan → Sidhu"


def test_an_unrouted_item_is_skipped_not_raised():
    masters = _masters()
    batches = _batches() + [Batch(batch_id="B2", item_code="GHOST", qty=5,
                                  delivery_date=date(2026, 12, 1), so_refs=["SO9"])]
    entries = roster_adapter.run(batches, config=_cfg(), masters=masters)
    assert {e.batch_id for e in entries} == {"B1"}


def test_empty_batches_returns_empty():
    assert roster_adapter.run([], config=_cfg(), masters=_masters()) == []


def test_pipeline_dispatches_roster():
    from engine import pipeline
    assert pipeline.scheduler_for(_cfg()) is roster_adapter.run
```

Read `engine/models.py:80` for `Batch`'s real constructor before running this — use its actual field names, do not guess.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_entries_contract.py -v`
Expected: FAIL — `ImportError: cannot import name 'roster_adapter'`

- [ ] **Step 3: Write `engine/roster_adapter.py`**

```python
"""The seam between the app and roster_engine.

The ONLY file that knows both worlds. Everything upstream (loader, order book,
store, freeze, Rules 1-3) and everything downstream (Gantt, Schedule tab, delay
report, Analytics, shift-wise export, efficiency report) is untouched, because
this returns exactly the ScheduleEntry list they already consume.

Contract notes that are load-bearing elsewhere:
  * ``op_segments`` is [(start, end, operator)] sorted by start — five surfaces
    read it, and a wrong shape breaks them silently.
  * OS and off-machine entries carry the lane STRINGS below as their machine.
    delay_report._OFF_LANES, analytics.NON_MACHINE_LANES and freeze._OS_LANES all
    match on them literally.
  * every real machine entry names an operator; freeze.py pins machine AND
    operator, so an empty name freezes a ghost.
"""

from __future__ import annotations

from engine.models import ScheduleEntry
from roster_engine import SCHEDULER_FINGERPRINT  # noqa: F401  (re-exported)
from roster_engine import scheduler
from roster_engine.domain import DISPATCH, OUTSOURCED, build_jobs, build_shop

OS_LANE = "OS / Outsourced"
OFF_LANE = "Off-machine"


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, frozen=None, **kw):
    """Scheduler seam contract: prioritized ``batches`` -> list[ScheduleEntry].

    The batches arrive already in priority order (Rules 1-3, or a saved
    optimization's rank map), so that order IS the job sequence. Never
    re-consolidated here — Rule 1 already clubbed the SO lines.
    """
    if not batches or masters is None:
        return []
    jobs, batch_by_key, skipped = build_jobs(batches, masters)
    if not jobs:
        return []
    shop = build_shop(masters, _absent_from_reserved(reserved, masters))
    plan = scheduler.schedule(
        jobs, [j.key for j in jobs], shop, config,
        overlap=_overlap(config),
        crew_rank=dict(getattr(config, "crew_rank", None) or {}),
        frozen=_frozen_rows(frozen))
    if notes is not None and skipped:
        notes.append(f"skipped {len(skipped)} item(s) with no routing: "
                     f"{', '.join(sorted(skipped)[:5])}")
    return _entries(plan, batch_by_key)


def _overlap(config) -> float:
    """overlap_percent 0-100 -> a fraction. 80 means 80 of 100 pieces done."""
    raw = float(getattr(config, "overlap_percent", 100) or 0.0)
    return min(1.0, max(0.0, raw / 100.0))


def _absent_from_reserved(reserved, masters):
    """``reserved`` maps machine id / operator name -> busy (start, end) intervals.
    Only the operator entries matter here: an absent person cannot be rostered."""
    if not reserved:
        return {}
    names = {o.name for o in masters.operators}
    return {k: list(v) for k, v in reserved.items() if k in names}


def _frozen_rows(frozen):
    """``frozen`` arrives as {machine_id: [row, ...]} or a flat list. Flatten it."""
    if not frozen:
        return []
    if isinstance(frozen, dict):
        return [row for rows in frozen.values() for row in rows]
    return list(frozen)


def _entries(plan, batch_by_key):
    out = []
    for p in plan.placements:
        batch = batch_by_key.get(p.job_key)
        if batch is None:
            continue
        if p.machine is None:
            machine = OS_LANE if p.kind == OUTSOURCED else OFF_LANE
            segments, operator = [], ""
        else:
            machine = p.machine
            segments = [(s, e, name) for s, e, name in p.segments]
            operator = segments[0][2] if segments else ""
        out.append(ScheduleEntry(
            batch_id=str(batch.batch_id),
            item_code=batch.item_code,
            process_seq=p.op_seq,
            process_name=p.op_name,
            machine=machine,
            qty=p.qty,
            occupancy_min=p.work_min,
            start=p.start,
            end=p.end,
            so_refs=list(getattr(batch, "so_refs", ()) or ()),
            operator=operator,
            op_segments=segments))
    out.sort(key=lambda e: (e.start, e.batch_id, e.process_seq))
    return out
```

- [ ] **Step 4: Add `"roster"` to `scheduler_for`**

In `engine/pipeline.py:142`, immediately after the `"new"` branch:

```python
    if sched == "roster":
        # The roster-first engine (roster_engine/, 2026-08-12 spec): one operator
        # per machine per shift, unsegmented operations, whole-piece overlap.
        from . import roster_adapter
        return roster_adapter.run
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_roster_entries_contract.py -v`
Expected: PASS (10 tests)

- [ ] **Step 6: Verify the whole app still plans on the old engine**

Run: `pytest -q`
Expected: all existing tests pass — `scheduler_for` gained a branch that no existing config selects.

- [ ] **Step 7: Commit**

```bash
git add engine/roster_adapter.py engine/pipeline.py tests/test_roster_entries_contract.py
git commit -m "feat(roster): wire the engine into the scheduler seam

The one file that knows both worlds. Everything upstream and downstream is
untouched, because this returns exactly the ScheduleEntry list the Gantt,
Schedule tab, delay report, Analytics, shift-wise export and efficiency report
already consume.

Three contracts are pinned by test, because breaking any of them fails
SILENTLY: op_segments is [(start, end, operator)] sorted by start (five
surfaces read it); OS/off-machine entries carry the exact lane strings
delay_report, analytics and freeze match on (the 2026-08-09 defect was billing
outsourcing to an in-house machine); and every machine entry names an operator,
because freeze.py pins machine AND operator.

Selected only by scheduler=roster, so no existing plan moves."
```

---

## Task 8: The violation report

**Files:**
- Create: `roster_engine/report.py`
- Modify: `api/main.py` (`_report_for_book`)
- Create: `tests/test_roster_report.py`

**Interfaces:**
- Consumes: `list[ScheduleEntry]`, `Masters`, `Config`
- Produces (all pure, all returning `list[dict]` with `kind`/`ref`/`message`):
  - `report.operator_split_violations(entries, config)`
  - `report.segmentation_violations(entries)`
  - `report.idle_capacity_violations(entries, masters, config)`
  - `report.overlap_rounding_violations(entries, masters)`
  - `report.all_violations(entries, masters, config)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_report.py
from datetime import date, datetime

from engine.config import Config
from engine.models import Machine, Masters, Operator, ScheduleEntry, WorkCalendar
from roster_engine import report


def _cfg():
    return Config(plan_start_date=date(2026, 8, 12))


def _e(machine, seq, start, end, operator, batch="B1", qty=10):
    return ScheduleEntry(batch, "ITEM", seq, f"P{seq}", machine, qty,
                         (end - start).total_seconds() / 60.0, start, end,
                         operator=operator, op_segments=[(start, end, operator)])


def _dt(h, m=0, day=12):
    return datetime(2026, 8, day, h, m)


def test_an_operator_on_two_machines_in_one_shift_is_flagged():
    entries = [_e("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _e("CNC4", 2, _dt(13), _dt(17), "Narayan", batch="B2")]
    rows = report.operator_split_violations(entries, _cfg())
    assert len(rows) == 1
    assert rows[0]["kind"] == "OPERATOR_SPLIT_SHIFT"
    assert "Narayan" in rows[0]["message"]
    assert "CNC1" in rows[0]["message"] and "CNC4" in rows[0]["message"]


def test_the_same_operator_on_the_same_machine_all_shift_is_clean():
    entries = [_e("CNC1", 1, _dt(8), _dt(12), "Narayan"),
               _e("CNC1", 2, _dt(12), _dt(17), "Narayan", batch="B2")]
    assert report.operator_split_violations(entries, _cfg()) == []


def test_a_shift_change_at_1900_is_not_a_split():
    entries = [_e("CNC1", 1, _dt(8), _dt(19), "Narayan"),
               _e("CNC4", 1, _dt(19), _dt(23), "Narayan", batch="B2")]
    assert report.operator_split_violations(entries, _cfg()) == []


def test_an_interrupted_operation_is_flagged():
    interrupted = ScheduleEntry(
        "B1", "ITEM", 1, "P1", "CNC1", 10, 240.0, _dt(8), _dt(18), operator="N",
        op_segments=[(_dt(8), _dt(10), "N"), (_dt(16), _dt(18), "N")])
    other = _e("CNC1", 1, _dt(11), _dt(15), "N", batch="B2")
    rows = report.segmentation_violations([interrupted, other])
    assert len(rows) == 1
    assert rows[0]["kind"] == "OPERATION_SEGMENTED"
    assert "B1" in rows[0]["ref"]


def test_a_shift_gap_inside_one_operation_is_not_segmentation():
    """Nothing else ran on the machine — the part just waited overnight."""
    held = ScheduleEntry(
        "B1", "ITEM", 1, "P1", "CNC1", 10, 600.0, _dt(8), _dt(12, 0, 13),
        operator="N",
        op_segments=[(_dt(8), _dt(19), "N"), (_dt(8, 0, 13), _dt(12, 0, 13), "N")])
    assert report.segmentation_violations([held]) == []


def test_a_fractional_piece_release_is_flagged():
    from engine.models import Process, Routing
    masters = Masters(routings={"ITEM": Routing("ITEM", "d", [
        Process(1, "P1", 10.0, None, None, "CNC1"),
        Process(2, "P2", 10.0, None, None, "CNC4")])})
    # P2 starts 5.5 pieces into P1 (90 setup + 55 min) -> not a whole piece.
    entries = [_e("CNC1", 1, _dt(8), _dt(18), "N"),
               _e("CNC4", 2, _dt(10, 25), _dt(18), "S")]
    rows = report.overlap_rounding_violations(entries, masters)
    assert rows and rows[0]["kind"] == "OVERLAP_FRACTIONAL_PIECE"


def test_all_violations_is_empty_on_a_clean_roster_plan():
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5)},
        operators=[Operator("Narayan", "CNC1", ["CNC1"], "First shift")],
        calendar=WorkCalendar())
    entries = [_e("CNC1", 1, _dt(8), _dt(12), "Narayan")]
    assert report.all_violations(entries, masters, _cfg()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine.report'`

- [ ] **Step 3: Write `roster_engine/report.py`**

```python
"""Invariants CHECKED, not merely intended.

Two rules of this codebase, learned the hard way: a class of defect that shipped
silently once will ship silently again unless an invariant is checked on every
plan (2026-08-07), and a report may never attribute a cause it did not check
(2026-08-09).

All pure, all non-blocking — a live plan must never break because a self-check is
unhappy. Run against BOTH engines, so a side-by-side comparison is evidence
rather than assertion.
"""

from __future__ import annotations

import math

_TOL_MIN = 1.0        # a minute of float slop is not a violation


def _shift_key(when, config):
    """(date, shift) a moment belongs to. The second shift crosses midnight, so an
    02:00 segment belongs to the PREVIOUS day's night shift."""
    t = when.time()
    if config.first_shift_start <= t < config.first_shift_end:
        return when.date(), "first"
    if t < config.first_shift_start:
        from datetime import timedelta
        return (when - timedelta(days=1)).date(), "second"
    return when.date(), "second"


def operator_split_violations(entries, config):
    """Rule 1: nobody mans two machines within one shift."""
    seen = {}
    for e in entries:
        for start, end, name in (e.op_segments or []):
            if not name or not e.machine:
                continue
            seen.setdefault((name, _shift_key(start, config)), set()).add(e.machine)
    rows = []
    for (name, (day, shift)), machines in sorted(
            seen.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if len(machines) > 1:
            rows.append({
                "kind": "OPERATOR_SPLIT_SHIFT", "ref": name,
                "message": (f"{name} is planned on {len(machines)} machines in the "
                            f"{shift} shift of {day:%d-%m-%Y} "
                            f"({', '.join(sorted(machines))}); one operator mans one "
                            f"machine for a whole shift")})
    return rows


def segmentation_violations(entries):
    """Rule 2: an operation's time on its machine is never interrupted by another job.

    A gap is only a violation when SOMETHING ELSE ran on that machine inside it.
    An overnight or unmanned gap is the part waiting in the chuck, which is legal.
    """
    by_machine = {}
    for e in entries:
        if e.machine:
            by_machine.setdefault(e.machine, []).append(e)
    rows = []
    for machine, items in sorted(by_machine.items()):
        for e in items:
            segs = sorted(e.op_segments or [], key=lambda s: s[0])
            if len(segs) < 2:
                continue
            for other in items:
                if other is e:
                    continue
                for a, b in zip(segs, segs[1:]):
                    if a[1] < other.start < b[0] or a[1] < other.end < b[0]:
                        rows.append({
                            "kind": "OPERATION_SEGMENTED",
                            "ref": f"{e.batch_id}/{e.process_seq}",
                            "message": (
                                f"'{e.process_name}' of batch {e.batch_id} is broken up "
                                f"on {machine}: '{other.process_name}' "
                                f"(batch {other.batch_id}) runs inside its gap. An "
                                f"operation must run to completion.")})
                        break
                else:
                    continue
                break
    return rows


def idle_capacity_violations(entries, masters, config):
    """The owner's original complaint, as a number: a machine with ready work,
    dark, while a qualified operator sits unassigned in that shift.

    Reported for BOTH engines. Under the roster engine this is the objective of
    the assignment step, so it should be zero; under the live engine it is the
    measurement of what is being lost.
    """
    from roster_engine.worktime import operator_shift

    busy_machine, busy_operator, shifts = {}, {}, set()
    for e in entries:
        for start, end, name in (e.op_segments or []):
            key = _shift_key(start, config)
            shifts.add(key)
            if e.machine:
                busy_machine.setdefault(key, set()).add(e.machine)
            if name:
                busy_operator.setdefault(key, set()).add(name)

    pending = {}
    for e in entries:
        if e.machine and e.op_segments:
            pending.setdefault(e.machine, []).append(e)

    rows = []
    for key in sorted(shifts):
        day, shift = key
        manned = busy_machine.get(key, set())
        working = busy_operator.get(key, set())
        for operator in masters.operators:
            if operator.name in working or operator_shift(operator) != shift:
                continue
            for mid in sorted(getattr(operator, "machines", None) or ()):
                if mid in manned or mid not in masters.machines:
                    continue
                if not any(e.start > _end_of(key, config) for e in pending.get(mid, [])):
                    continue
                rows.append({
                    "kind": "IDLE_CAPACITY", "ref": f"{mid}@{day:%d-%m-%Y}/{shift}",
                    "message": (
                        f"{mid} is idle for the {shift} shift of {day:%d-%m-%Y} while "
                        f"{operator.name} — qualified for it and on that shift — has no "
                        f"machine, and {mid} has work waiting")})
                break
    return rows


def _end_of(key, config):
    from datetime import datetime, timedelta
    day, shift = key
    if shift == "first":
        return datetime.combine(day, config.first_shift_end)
    end = datetime.combine(day, config.second_shift_end)
    start = datetime.combine(day, config.second_shift_start)
    return end + timedelta(days=1) if end <= start else end


def overlap_rounding_violations(entries, masters):
    """Rule 3: a successor is never released on a fraction of a piece."""
    by_batch = {}
    for e in entries:
        by_batch.setdefault(e.batch_id, []).append(e)
    rows = []
    for batch, items in sorted(by_batch.items()):
        items.sort(key=lambda e: e.process_seq)
        routing = masters.routings.get(items[0].item_code)
        if routing is None:
            continue
        cycles = {p.seq: float(p.cycle_time or 0.0) for p in routing.processes}
        for prev, nxt in zip(items, items[1:]):
            cycle = cycles.get(prev.process_seq, 0.0)
            if cycle <= 0 or not prev.machine or not nxt.machine:
                continue
            if nxt.start >= prev.end:
                continue                       # sequential, nothing to round
            worked = (nxt.start - prev.start).total_seconds() / 60.0
            pieces = (worked - _setup_of(prev, cycle)) / cycle
            if pieces <= 0:
                continue
            if abs(pieces - round(pieces)) > _TOL_MIN / cycle:
                rows.append({
                    "kind": "OVERLAP_FRACTIONAL_PIECE",
                    "ref": f"{batch}/{nxt.process_seq}",
                    "message": (
                        f"'{nxt.process_name}' of batch {batch} starts {pieces:.2f} "
                        f"pieces into '{prev.process_name}'; a process can only start "
                        f"on a whole piece")})
    return rows


def _setup_of(entry, cycle):
    """Setup minutes implied by an entry: its occupancy less its cutting."""
    return max(0.0, entry.occupancy_min - (entry.qty or 0) * cycle)


def all_violations(entries, masters, config):
    if not entries:
        return []
    rows = []
    rows.extend(operator_split_violations(entries, config))
    rows.extend(segmentation_violations(entries))
    rows.extend(idle_capacity_violations(entries, masters, config))
    rows.extend(overlap_rounding_violations(entries, masters))
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_roster_report.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Append the checks to the validation report for BOTH engines**

In `api/main.py::_report_for_book`, after the existing `batch_quantity_violations` block, add:

```python
        # The four roster invariants — run for EVERY engine, not just the roster
        # one, so a side-by-side comparison is evidence rather than assertion. The
        # live engine is expected to produce OPERATOR_SPLIT_SHIFT and
        # OPERATION_SEGMENTED rows; that is the measurement, not a bug in the check.
        try:
            from roster_engine import report as _rr
            rows.extend(_rr.all_violations(
                schedule, masters,
                config if config is not None else _load_plan_config()))
        except Exception:  # noqa: BLE001 — a self-check must never break the report
            pass
```

- [ ] **Step 6: Verify the report still builds on the live engine**

Run: `pytest -q`
Expected: all existing tests pass. If a test asserts the report is empty for a `new`-engine plan, that test is now measuring the live engine's real violations — **do not weaken the check to make it pass.** Update the test to assert the *roster* engine produces none, and record the live engine's count in the commit message.

- [ ] **Step 7: Commit**

```bash
git add roster_engine/report.py api/main.py tests/test_roster_report.py
git commit -m "feat(roster): check the four invariants on every plan

Two rules of this codebase, both learned the hard way: a defect class that
ships silently once ships silently again unless an invariant is CHECKED
(2026-08-07), and a report may never attribute a cause it did not check
(2026-08-09).

OPERATOR_SPLIT_SHIFT, OPERATION_SEGMENTED, IDLE_CAPACITY and
OVERLAP_FRACTIONAL_PIECE run against BOTH engines. The live engine is expected
to produce the first two — that is the measurement of what its extra
throughput is actually buying, not a bug in the check.

IDLE_CAPACITY is the owner's original complaint expressed as a number."
```

---

## Task 9: Objective and the alternating search

**Files:**
- Create: `roster_engine/objective.py`, `roster_engine/search.py`
- Test: `tests/test_roster_search.py`

**Interfaces:**
- Consumes: `roster_engine.scheduler.schedule`
- Produces:
  - `objective.Metrics(lateness_by_order:dict[str,float], makespan_days:float, total_late_days:float, max_late_days:float)`
  - `objective.compute_metrics(plan, jobs, config) -> Metrics`
  - `objective.score(metrics, config) -> float`
  - `search.Result(sequence:list[str], crew_rank:dict[str,int], score:float, metrics, evaluations:int, baseline_score:float, cancelled:bool)`
  - `search.optimize(jobs, shop, config, *, overlap, budget_evals=150, seed=42, on_eval=None, should_cancel=None, frozen=None) -> Result`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roster_search.py
from datetime import date, datetime, timedelta

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import objective, scheduler, search
from roster_engine.domain import build_jobs, build_shop


class _B:
    def __init__(self, key, qty, due):
        self.batch_id, self.item_code, self.qty = key, "ITEM", qty
        self.so_refs, self.delivery_date = [key], due
        self.process_remaining = None


def _fixture(n=6):
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
                  "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5)},
        routings={"ITEM": Routing("ITEM", "d", [
            Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC4")])},
        operators=[Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                   Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B(f"B{i}", 30, date(2026, 8, 20) + timedelta(days=i))
               for i in range(n)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), Config(plan_start_date=date(2026, 8, 12),
                                             setup_time_min=90.0)


def test_score_is_lower_when_orders_land_on_their_dates():
    jobs, shop, cfg = _fixture()
    plan = scheduler.schedule(jobs, [j.key for j in jobs], shop, cfg, overlap=1.0)
    m = objective.compute_metrics(plan, jobs, cfg)
    assert m.makespan_days > 0
    assert set(m.lateness_by_order) == {j.key for j in jobs}
    assert objective.score(m, cfg) >= 0


def test_early_and_late_are_penalised_equally():
    """The owner's symmetric on-time rule (2026-08-06)."""
    cfg = Config(plan_start_date=date(2026, 8, 12))
    early = objective.Metrics({"A": -20.0}, 1.0, 0.0, 0.0)
    late = objective.Metrics({"A": 20.0}, 1.0, 20.0, 20.0)
    assert objective.score(early, cfg) == objective.score(late, cfg)


def test_misses_spread_across_orders_beat_one_hopeless_order():
    cfg = Config(plan_start_date=date(2026, 8, 12))
    spread = objective.Metrics({f"O{i}": 6.0 for i in range(10)}, 1.0, 60.0, 6.0)
    concentrated = objective.Metrics({"O0": 30.0}, 1.0, 30.0, 30.0)
    assert objective.score(spread, cfg) < objective.score(concentrated, cfg)


def test_the_search_never_returns_worse_than_its_starting_point():
    jobs, shop, cfg = _fixture()
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert res.score <= res.baseline_score


def test_the_search_is_deterministic_for_a_seed():
    jobs, shop, cfg = _fixture()
    a = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    b = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert (a.sequence, a.crew_rank, a.score) == (b.sequence, b.crew_rank, b.score)


def test_the_search_returns_both_genomes():
    jobs, shop, cfg = _fixture()
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert sorted(res.sequence) == sorted(j.key for j in jobs)
    assert set(res.crew_rank) == {"CNC1", "CNC4"}
    assert sorted(res.crew_rank.values()) == [0, 1]


def test_the_budget_is_respected():
    jobs, shop, cfg = _fixture()
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=25, seed=7)
    assert res.evaluations <= 25


def test_cancellation_keeps_the_best_so_far():
    jobs, shop, cfg = _fixture()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 5

    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=500, seed=7,
                          should_cancel=stop)
    assert res.cancelled
    assert res.score <= res.baseline_score


def test_the_crew_genome_actually_changes_a_plan():
    """If it cannot move a plan it is a decorative lever, like the operator pin
    that turned out never to reach the planner (2026-08-05)."""
    jobs, shop, cfg = _fixture()
    a = scheduler.schedule(jobs, [j.key for j in jobs], shop, cfg, overlap=1.0,
                           crew_rank={"CNC1": 0, "CNC4": 1})
    b = scheduler.schedule(jobs, [j.key for j in jobs], shop, cfg, overlap=1.0,
                           crew_rank={"CNC4": 0, "CNC1": 1})
    sig = lambda p: sorted((x.job_key, x.machine) for x in p.placements)
    assert sig(a) != sig(b)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_engine.objective'`

- [ ] **Step 3: Write `roster_engine/objective.py`**

```python
"""One number, lower is better — deliberately the SAME formula as the live engine.

Identical scoring is what makes the A/B honest: any difference between the two
plans is then the scheduling, not the yardstick. Written fresh rather than
imported, because this package does not import ppc_engine.

    score = w_ontime  x  sum( (|miss| - band, capped)^2 )     # the whole objective
          + w_makespan x  makespan                            # a strict tie-break

`abs()` is the owner's rule that early and late are equally bad. Squaring is the
owner's rule that misses must be SPREAD: ten orders 6 days out (10 x 2^2 = 40)
beats one order 30 days out ((30-4)^2 = 676). The cap stops one hopeless order
swamping the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_ONTIME_WEIGHT = 1.0
_MAKESPAN_WEIGHT = 0.1
_BAND_DAYS = 4.0
_CAP_DAYS = 60.0


@dataclass(frozen=True)
class Metrics:
    lateness_by_order: dict          # SIGNED days; negative = early
    makespan_days: float
    total_late_days: float
    max_late_days: float


def compute_metrics(plan, jobs, config) -> Metrics:
    starts = [p.start for p in plan.placements] or [_plan_start(config)]
    ends = [p.end for p in plan.placements] or [_plan_start(config)]
    span = (max(ends) - min(starts)).total_seconds() / 86400.0

    lateness, total, worst = {}, 0.0, 0.0
    for job in jobs:
        end = plan.completion.get(job.key)
        if end is None or job.due is None:
            lateness[job.key] = 0.0
            continue
        days = (end.date() - job.due).days
        lateness[job.key] = float(days)
        if days > 0:
            total += days
            worst = max(worst, float(days))
    return Metrics(lateness, span, total, worst)


def score(metrics: Metrics, config) -> float:
    band = float(getattr(config, "ontime_band_days", _BAND_DAYS) or _BAND_DAYS)
    cap = float(getattr(config, "ontime_cap_days", _CAP_DAYS) or _CAP_DAYS)
    breach = 0.0
    for late in metrics.lateness_by_order.values():
        over = abs(late) - band
        if over > 0:
            breach += min(over, cap) ** 2
    return _ONTIME_WEIGHT * breach + _MAKESPAN_WEIGHT * metrics.makespan_days


def _plan_start(config) -> datetime:
    floor = getattr(config, "plan_start_floor", None)
    if floor:
        return floor
    return datetime.combine(config.plan_start_date, config.first_shift_start)
```

- [ ] **Step 4: Write `roster_engine/search.py`**

```python
"""Alternating descent over two genomes: the JOB FLOW and the CREW.

Inside a shift, who mans what is SOLVED exactly (assign.py), so no evaluation is
ever spent on a roster that is locally dominated. What the matching cannot know is
the future — it will man the machine busy at 08:00 and leave dark the machine
whose big batch releases at 11:00. That lookahead is the crew genome, and it is
what this searches, alongside the job order.

A joint random walk over two genomes wastes evaluations; alternating descent does
not. The crew genome is deliberately a PERMUTATION, the same object type as the
sequence, so one set of moves serves both.

Guarantees, matching the live search so the A/B is fair:
  * never worse than the starting point — seeds are evaluated first and kept
  * deterministic — fixed seed + eval budget => the same result every run
  * objective-driven — it knows nothing about how the score is built
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from roster_engine import objective, scheduler

# How the budget splits between the two phases. Starting point, to be MEASURED on
# the owner's book rather than assumed.
_JOB_SHARE = 0.6


@dataclass
class Result:
    sequence: list = field(default_factory=list)
    crew_rank: dict = field(default_factory=dict)
    score: float = float("inf")
    metrics: object = None
    evaluations: int = 0
    baseline_score: float = float("inf")
    cancelled: bool = False


class _Evaluator:
    """Decode + score, memoised — the search revisits genomes constantly."""

    def __init__(self, jobs, shop, config, overlap, frozen, on_eval, should_cancel,
                 budget):
        self._jobs, self._shop, self._config = jobs, shop, config
        self._overlap, self._frozen = overlap, frozen
        self._on_eval, self._should_cancel = on_eval, should_cancel
        self._budget = budget
        self._cache = {}
        self.count = 0
        self.cancelled = False

    @property
    def exhausted(self) -> bool:
        return self.cancelled or self.count >= self._budget

    def __call__(self, sequence, crew_rank):
        key = (tuple(sequence), tuple(sorted(crew_rank.items())))
        if key in self._cache:
            return self._cache[key]
        if self.exhausted:
            return float("inf"), None
        if self._should_cancel and self._should_cancel():
            self.cancelled = True
            return float("inf"), None
        plan = scheduler.schedule(self._jobs, list(sequence), self._shop,
                                  self._config, overlap=self._overlap,
                                  crew_rank=dict(crew_rank), frozen=self._frozen)
        metrics = objective.compute_metrics(plan, self._jobs, self._config)
        value = objective.score(metrics, self._config)
        self.count += 1
        self._cache[key] = (value, metrics)
        if self._on_eval:
            self._on_eval(self.count, metrics)
        return value, metrics


def _seed_sequences(jobs):
    """Dispatch-rule starting points, best-first. EDD is the strongest opener under
    an on-time objective, so it goes first and the search is never worse than it."""
    far = max((j.due for j in jobs if j.due is not None), default=None)
    def due(j):
        return j.due or far
    edd = [j.key for j in sorted(jobs, key=lambda j: (due(j) is None, due(j), j.key))]
    spt = [j.key for j in sorted(jobs, key=lambda j: (_work(j), j.key))]
    lpt = list(reversed(spt))
    return [edd, spt, lpt]


def _work(job) -> float:
    return sum(job.qty_for(op.seq) * op.cycle_min for op in job.ops)


def _moves(genome, rng):
    """Insertion, swap and block moves — the three that carry a permutation search."""
    n = len(genome)
    if n < 2:
        return []
    out = []
    i = rng.randrange(n)
    j = rng.randrange(n)
    if i != j:
        moved = list(genome)
        out.append(moved[:i] + moved[i + 1:][:j] + [moved[i]] + moved[i + 1:][j:])
        swapped = list(genome)
        swapped[i], swapped[j] = swapped[j], swapped[i]
        out.append(swapped)
    if n >= 4:
        a = rng.randrange(n - 2)
        size = rng.randint(2, min(4, n - a))
        block = list(genome)
        chunk = block[a:a + size]
        del block[a:a + size]
        at = rng.randrange(len(block) + 1)
        out.append(block[:at] + chunk + block[at:])
    return [g for g in out if len(g) == n and len(set(g)) == n]


def optimize(jobs, shop, config, *, overlap, budget_evals=150, seed=42,
             on_eval=None, should_cancel=None, frozen=None) -> Result:
    """Search the job sequence and the crew priorities together."""
    if not jobs:
        return Result()
    rng = random.Random(int(seed))
    machines = sorted(shop.machining_ids)
    ev = _Evaluator(jobs, shop, config, overlap, frozen, on_eval, should_cancel,
                    int(budget_evals))

    best_seq, best_crew = None, machines[:]
    best_score, best_metrics = float("inf"), None
    for candidate in _seed_sequences(jobs):
        value, metrics = ev(candidate, _rank(best_crew))
        if value < best_score:
            best_seq, best_score, best_metrics = candidate, value, metrics
        if ev.exhausted:
            break
    baseline = best_score
    if best_seq is None:
        best_seq = [j.key for j in jobs]

    job_budget = int(budget_evals * _JOB_SHARE)
    improved = True
    while improved and not ev.exhausted:
        improved = False
        # Phase J — freeze the crew, hill-climb the job sequence.
        while not ev.exhausted and ev.count < job_budget:
            moved = False
            for candidate in _moves(best_seq, rng):
                value, metrics = ev(candidate, _rank(best_crew))
                if value < best_score:
                    best_seq, best_score, best_metrics = candidate, value, metrics
                    moved = improved = True
                    break
            if not moved:
                break
        # Phase C — freeze the sequence, hill-climb the crew priorities.
        while not ev.exhausted:
            moved = False
            for candidate in _moves(best_crew, rng):
                value, metrics = ev(best_seq, _rank(candidate))
                if value < best_score:
                    best_crew, best_score, best_metrics = candidate, value, metrics
                    moved = improved = True
                    break
            if not moved:
                break
        job_budget = int(budget_evals)      # after the first pass, no phase cap

    return Result(sequence=list(best_seq), crew_rank=_rank(best_crew),
                  score=best_score, metrics=best_metrics, evaluations=ev.count,
                  baseline_score=baseline, cancelled=ev.cancelled)


def _rank(machines) -> dict:
    return {mid: i for i, mid in enumerate(machines)}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_roster_search.py -v`
Expected: PASS (9 tests)

`test_the_crew_genome_actually_changes_a_plan` is the important one. If it fails, the genome is decorative and the whole crew dimension is worthless — fix the scheduler's machine ordering, not the test.

- [ ] **Step 6: Commit**

```bash
git add roster_engine/objective.py roster_engine/search.py tests/test_roster_search.py
git commit -m "feat(roster): alternating sequence x crew search

Inside a shift the roster is solved exactly, so no evaluation is spent on a
locally dominated crew. What the matching cannot know is the future — it mans
the machine busy at 08:00 and leaves dark the one whose batch lands at 11:00.
That lookahead is the crew genome, a permutation, searched by the same moves
as the job sequence and alternating with it.

The objective is deliberately the SAME formula as the live engine, written
fresh rather than imported: any difference between the two plans is then the
scheduling, not the yardstick.

Pinned: the crew genome actually moves a plan. A lever that cannot is
decorative — see the operator pin that never reached the planner (2026-08-05)."
```

---

## Task 10: Optimizer, contest and apply wiring

**Files:**
- Modify: `engine/roster_adapter.py` (add `optimize_sequence`, `sweep_optimize`)
- Modify: `engine/optimizer.py` (`optimize`, `sweep_optimize`, `knob_for`)
- Modify: `engine/optimize_service.py` (`cloud_candidates`, `run_candidate`, `contest_jobs`, `build_payload`, `parse_payload`, `ContestSetup`)
- Modify: `api/main.py` (`_inputs_signature`, `_optimize_apply`, the fingerprint response)
- Create: `tests/test_roster_wiring.py`

**Interfaces:**
- Produces:
  - `roster_adapter.optimize_sequence(so_lines, config, masters, *, reserved=None, budget_evals=150, seed=42, on_progress=None, should_cancel=None, frozen=None) -> engine.optimizer.OptimizeResult` (with `.crew_rank` attached)
  - `roster_adapter.sweep_optimize(...) -> engine.optimizer.SweepResult`
  - `optimizer.ROSTER_OVERLAP_CANDIDATES = (50, 60, 70, 80, 90, 100)`
  - `optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES` — a finer grid across the same 50–100 band

- [ ] **Step 1: Write the failing wiring tests**

```python
# tests/test_roster_wiring.py
import inspect
from dataclasses import replace
from datetime import date

from engine import optimize_service, optimizer, pipeline, roster_adapter
from engine.config import Config


def _cfg(scheduler="roster"):
    return Config(plan_start_date=date(2026, 8, 12), scheduler=scheduler)


def test_every_scheduler_dispatch_site_knows_roster():
    """A missed site silently falls back to the CLASSIC engine — a valid plan, so
    nothing errors; it is just the wrong engine, and the applied ranks become
    meaningless while every screen stays green."""
    assert pipeline.scheduler_for(_cfg()) is roster_adapter.run
    for fn in (optimizer.optimize, optimizer.sweep_optimize):
        assert "roster" in inspect.getsource(fn), fn.__name__
    for fn in (optimize_service.cloud_candidates, optimize_service.run_candidate,
               optimize_service.contest_jobs):
        assert "roster" in inspect.getsource(fn), fn.__name__


def test_knob_for_roster_is_the_overlap_percent():
    knob, candidates = optimizer.knob_for(_cfg())
    assert knob == "overlap_percent"
    assert min(candidates) >= 50 and max(candidates) <= 100


def test_the_overlap_band_searched_is_50_to_100():
    """The owner's band. Under the corrected definition this is the physically
    sane range: 50 = start at half, 100 = fully sequential."""
    for candidates in (optimizer.ROSTER_OVERLAP_CANDIDATES,
                       optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES):
        assert min(candidates) >= 50
        assert max(candidates) <= 100


def test_roster_contest_does_not_double_for_machine_sets():
    """flexible_machines is a new-engine dimension. Leaving the gate open would
    cost every Actions run twice for nothing."""
    payload = {"config": _cfg().to_dict(), "candidates": [50, 80], "seed": 1}
    jobs = optimize_service.contest_jobs(payload)
    assert {flex for _ov, flex, _sd in jobs} == {False}


def test_inputs_signature_covers_roster_fingerprint():
    """Without this, ranks searched under one version of the engine replay under a
    changed one behind a green 'up to date' banner."""
    import api.main as main
    from roster_engine import SCHEDULER_FINGERPRINT
    src = inspect.getsource(main._inputs_signature)
    assert "roster" in src
    a = main._inputs_signature(_cfg())
    assert isinstance(a, str) and a
    assert SCHEDULER_FINGERPRINT


def test_plan_cache_key_changes_with_scheduler():
    import api.main as main
    roster = main._plan_fingerprint(_cfg("roster"))
    new = main._plan_fingerprint(_cfg("new"))
    assert roster != new


def test_the_crew_genome_round_trips_through_the_cloud_payload():
    payload = optimize_service.build_payload(
        {}, [], None, _cfg(), seed=1, candidates=(50, 80),
        budget_per_candidate=10, crew_rank={"CNC1": 0, "CNC4": 1})
    assert payload["crew_rank"] == {"CNC1": 0, "CNC4": 1}
    parsed = optimize_service.parse_payload(payload)
    assert parsed[-1] == {"CNC1": 0, "CNC4": 1}


def test_adapter_never_reconsolidates():
    """Rule 1 already clubbed the SO lines. Re-consolidating would double-club, and
    ppc_engine's consolidate() is known-broken besides (CLAUDE.md)."""
    src = inspect.getsource(roster_adapter)
    assert "consolidat" not in src.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_roster_wiring.py -v`
Expected: FAIL on every test — none of the branches exist yet.

- [ ] **Step 3: Add the optimizer entry points to `engine/roster_adapter.py`**

```python
def optimize_sequence(so_lines, config, masters, *, reserved=None,
                      budget_evals=150, seed=42, on_progress=None,
                      should_cancel=None, frozen=None):
    """Sequence + crew search at the config's overlap.

    The cloud contest sweeps overlaps EXTERNALLY (one shard per candidate) and
    calls this per candidate, so across candidates it becomes the full
    overlap x sequence x crew contest — distributed, on GitHub Actions. Returns
    the app's OptimizeResult so the contest and apply machinery are unchanged,
    with the winning crew genome attached.
    """
    from engine.optimizer import OptimizeResult, plan_metrics, ranks_for
    from engine.rules import rule1_consolidate
    from roster_engine import search as rsearch

    batches = rule1_consolidate.run(list(so_lines), config=config, masters=masters)
    if not batches:
        return OptimizeResult()
    jobs, batch_by_key, _skipped = build_jobs(batches, masters)
    if not jobs:
        return OptimizeResult()
    shop = build_shop(masters, _absent_from_reserved(reserved, masters))

    res = rsearch.optimize(
        jobs, shop, config, overlap=_overlap(config), budget_evals=budget_evals,
        seed=seed, frozen=_frozen_rows(frozen),
        on_eval=(lambda n, m: on_progress(n, m)) if on_progress else None,
        should_cancel=should_cancel)

    ordered = [batch_by_key[k] for k in res.sequence if k in batch_by_key]
    out = OptimizeResult(ranks=ranks_for(ordered), evals=res.evaluations,
                         cancelled=res.cancelled)
    out.crew_rank = dict(res.crew_rank)
    out.best = plan_metrics(_replan(ordered, config, masters, reserved, frozen,
                                    res.crew_rank), so_lines, config)
    return out


def _replan(batches, config, masters, reserved, frozen, crew_rank):
    from dataclasses import replace
    return run(batches, config=replace(config, crew_rank=dict(crew_rank)),
               masters=masters, reserved=reserved, frozen=frozen)


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None,
                   frozen=None):
    """Local fallback: run the sequence+crew search once per overlap candidate and
    keep the best. The cloud path fans the same candidates across Actions shards."""
    from dataclasses import replace

    from engine.optimizer import ROSTER_OVERLAP_CANDIDATES, SweepResult, sweep_contenders

    contenders = sweep_contenders(getattr(config, "overlap_percent", None),
                                  ROSTER_OVERLAP_CANDIDATES)
    per = max(1, int(budget_evals) // max(1, len(contenders)))
    best = None
    for overlap in contenders:
        cfg = replace(config, overlap_percent=int(overlap))
        res = optimize_sequence(so_lines, cfg, masters, reserved=base_reserved,
                                budget_evals=per, seed=seed,
                                on_progress=on_progress,
                                should_cancel=should_cancel, frozen=frozen)
        row = (res.best.get("score", float("inf")) if res.best else float("inf"),
               int(overlap), res)
        if best is None or row[0] < best[0]:
            best = row
        if should_cancel and should_cancel():
            break
    _score, overlap, res = best
    out = SweepResult(ranks=res.ranks, best=res.best, best_overlap=overlap,
                      evals=res.evals, cancelled=res.cancelled)
    out.crew_rank = getattr(res, "crew_rank", {})
    return out
```

Read `engine/optimizer.py` for the real `OptimizeResult` / `SweepResult` / `plan_metrics` / `ranks_for` signatures before writing this — match them exactly rather than the shapes sketched here.

- [ ] **Step 4: Add the `"roster"` branches to `engine/optimizer.py`**

Add near `OVERLAP_CANDIDATES`:

```python
# The owner's overlap band (2026-08-12). Under the corrected definition
# (release.released_pieces) 50 = start at half done, 100 = fully sequential — so
# this is the whole physically sane range, searched continuously by the contest.
ROSTER_OVERLAP_CANDIDATES = (50, 60, 70, 80, 90, 100)
```

In `knob_for`, before the final return:

```python
    if getattr(config, "scheduler", "classic") == "roster":
        return "overlap_percent", ROSTER_OVERLAP_CANDIDATES
```

In `optimize` (line ~287), beside the `"new"` branch:

```python
    if getattr(config, "scheduler", "classic") == "roster":
        from engine import roster_adapter
        return roster_adapter.optimize_sequence(
            so_lines, config, masters, reserved=reserved, budget_evals=budget_evals,
            seed=seed, on_progress=on_progress, should_cancel=should_cancel,
            frozen=frozen)
```

In `sweep_optimize` (line ~488), beside the `"new"` branch:

```python
    if getattr(config, "scheduler", "classic") == "roster":
        from engine import roster_adapter
        return roster_adapter.sweep_optimize(
            so_lines, config, masters, budget_evals=budget_evals, seed=seed,
            on_progress=on_progress, should_cancel=should_cancel,
            base_reserved=base_reserved, frozen=frozen)
```

- [ ] **Step 5: Add the `"roster"` branches to `engine/optimize_service.py`**

```python
# Near CLOUD_OVERLAP_CANDIDATES — a finer grid over the owner's 50-100 band.
CLOUD_ROSTER_OVERLAP_CANDIDATES = (50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100)
```

- in `cloud_candidates` (line ~56): return `CLOUD_ROSTER_OVERLAP_CANDIDATES` when `sched == "roster"`.
- in `run_candidate` (line ~381): the `"new"` branch calls `new_engine.set_masters_bytes`. Add nothing for roster — the adapter reads the `Masters` object `prepare_contest` already built. Add the comment so the omission reads as deliberate:

```python
    # roster: nothing to do — roster_adapter reads the Masters object prepare_contest
    # builds, never the workbook bytes, so a cloud run needs no masters priming.
```

- in `contest_jobs` (line ~457): keep `(False,)` for roster:

```python
    machine_sets = ((False, True)
                    if getattr(config, "scheduler", "classic") == "new" else (False,))
```

Add a comment naming roster explicitly so the test's source check passes and the intent is recorded:

```python
    # "roster" deliberately stays single-pass here: it resolves machine options from
    # the routing and does not search the Allotted/Suggested axis, so opening this
    # gate would double every Actions run for nothing.
```

- in `build_payload`: add `crew_rank=None` to the signature and `"crew_rank": dict(crew_rank or {})` to the returned dict.
- in `parse_payload`: append `payload.get("crew_rank") or {}` to the returned tuple, and update its docstring. **Every caller that unpacks `parse_payload` must be updated in the same commit** — grep for it first.
- in `ContestSetup`: add `crew_rank: dict = field(default_factory=dict)`.

- [ ] **Step 6: Persist and replay the crew genome in `api/main.py`**

In `_inputs_signature`, after `new_engine_fingerprint`:

```python
    from roster_engine import SCHEDULER_FINGERPRINT as _roster_fp
    d["roster_engine_fingerprint"] = _roster_fp
```

In `_optimize_apply`, add to `meta`:

```python
                "crew_rank": res.get("crew_rank") or {},
```

And where the winning overlap is written back into the saved config, also persist the crew genome so every later plan replays the same roster:

```python
        best_crew = res.get("crew_rank")
        if best_crew:
            target = replace(target, crew_rank=dict(best_crew))
```

This requires `crew_rank` on `engine.config.Config` (default `None`), included in `to_dict`/`from_dict`. Add it, and confirm `Config.validate()` accepts `None`.

Add the fingerprint to the response beside the other two (`api/main.py:414`):

```python
    from roster_engine import SCHEDULER_FINGERPRINT as _roster_fp
    d["roster_engine_fingerprint"] = _roster_fp
```

- [ ] **Step 7: Run the wiring tests**

Run: `pytest tests/test_roster_wiring.py -v`
Expected: PASS (8 tests)

- [ ] **Step 8: Run the whole suite**

Run: `pytest -q`
Expected: all existing tests pass. `parse_payload` gained a tuple element — if any existing test unpacks it positionally, that is exactly the breakage this step exists to catch. Fix the caller, not the test.

- [ ] **Step 9: Prove the Actions path needs no edit**

Run: `git diff --stat main -- .github scripts requirements.txt`
Expected: **empty**. The workflow and the cloud worker round-trip the payload opaquely; a new key rides along.

Then confirm the worker's own unpacking still holds:

Run: `grep -n "parse_payload" scripts/cloud_optimize_worker.py engine/optimize_service.py`
Expected: every call site updated for the new tuple length.

- [ ] **Step 10: Commit**

```bash
git add engine/roster_adapter.py engine/optimizer.py engine/optimize_service.py engine/config.py api/main.py tests/test_roster_wiring.py
git commit -m "feat(roster): wire the two-dimensional contest end to end

All seven scheduler dispatch sites learn 'roster' in ONE commit. A missed site
would silently fall back to the classic engine — a valid plan, so nothing
errors; it is just the wrong engine, with the applied ranks meaningless behind
a green screen. Pinned by test.

The crew genome rides the cloud payload beside the sequence ranks and is
persisted by Apply, so every later plan replays the SAME roster — a stable crew
for the floor, not a fresh one on every refresh. _inputs_signature folds in the
roster fingerprint, or ranks searched under one version would replay under a
changed one behind an 'up to date' banner.

Overlap is searched across the owner's full 50-100 band. flexible_machines
stays off for roster, or every Actions run would cost twice for nothing.

.github/ and scripts/ are untouched: the workflow and the cloud worker carry
the larger JSON opaquely."
```

---

## Task 11: End-to-end verification against a real book

**Files:**
- Create: `tests/test_roster_end_to_end.py`

**Interfaces:** consumes everything above.

- [ ] **Step 1: Write the end-to-end tests**

```python
# tests/test_roster_end_to_end.py
"""The whole app, planning on the roster engine, on the generated sample workbook.

This is the test that catches wiring breaks the unit tests cannot see: a plan that
runs but produces entries the Gantt cannot draw, or a report that crashes.
"""

from dataclasses import replace
from datetime import date

import pytest

from engine import gantt, orderbook, pipeline
from engine.analytics import build_analytics
from engine.config import Config
from engine.models import PlanRun
from roster_engine import report as rreport
from tests.sample_workbook import build_masters_and_orders   # existing helper


@pytest.fixture
def book():
    masters, so_lines = build_masters_and_orders()
    return masters, so_lines


def _plan(masters, so_lines, scheduler="roster"):
    config = Config(plan_start_date=date(2026, 8, 12), scheduler=scheduler,
                    overlap_percent=80, setup_time_min=90.0)
    run = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(run, config, masters)
    return trace, config, trace["rule6"]["output"]


def test_the_book_plans_on_the_roster_engine(book):
    masters, so_lines = book
    _trace, _config, entries = _plan(masters, so_lines)
    assert entries


def test_the_roster_plan_has_no_rule_violations(book):
    """The whole point. Zero of all four, on a real book."""
    masters, so_lines = book
    _trace, config, entries = _plan(masters, so_lines)
    rows = rreport.all_violations(entries, masters, config)
    assert rows == [], "\n".join(r["message"] for r in rows[:10])


def test_the_live_engine_violates_rule_1_on_the_same_book(book):
    """Non-vacuity: if the live engine were clean too, the checks would be
    measuring nothing and the roster plan's zero would prove nothing."""
    masters, so_lines = book
    _trace, config, entries = _plan(masters, so_lines, scheduler="new")
    rows = rreport.operator_split_violations(entries, config)
    assert rows, "expected the live engine to hop operators between machines"


def test_the_gantt_renders_the_roster_plan(book):
    masters, so_lines = book
    trace, config, entries = _plan(masters, so_lines)
    view = gantt.build_gantt(entries, masters, config)
    assert view and view.get("rows")


def test_analytics_renders_the_roster_plan(book):
    masters, so_lines = book
    trace, config, entries = _plan(masters, so_lines)
    out = build_analytics(entries, masters, config, trace["rule1"]["output"])
    assert out["by_machine"] and out["by_op"]


def test_every_batch_is_fully_produced(book):
    """No step may be given fewer pieces than its batch owes (2026-08-11)."""
    from engine.new_engine import batch_quantity_violations
    masters, so_lines = book
    trace, _config, entries = _plan(masters, so_lines)
    assert batch_quantity_violations(entries, trace["rule1"]["output"]) == []


def test_routing_order_is_never_inverted(book):
    from engine.new_engine import routing_order_violations
    masters, so_lines = book
    _trace, _config, entries = _plan(masters, so_lines)
    assert routing_order_violations(entries, masters) == []


def test_the_plan_is_reproducible(book):
    masters, so_lines = book
    sig = lambda es: [(e.batch_id, e.process_seq, e.machine, e.start, e.end,
                       e.operator_label()) for e in es]
    a = sig(_plan(masters, so_lines)[2])
    b = sig(_plan(masters, so_lines)[2])
    assert a == b
```

Read `tests/sample_workbook.py` for the real helper name and signature before running — do not assume `build_masters_and_orders`.

- [ ] **Step 2: Run them**

Run: `pytest tests/test_roster_end_to_end.py -v`
Expected: PASS (8 tests)

`test_the_live_engine_violates_rule_1_on_the_same_book` is the non-vacuity guard. If it fails — the live engine is clean on this fixture — the sample workbook has too few operators to expose hopping. Extend the fixture until the live engine does violate the rule, and record what you changed. A clean run here would make every other assertion meaningless.

- [ ] **Step 3: Run the full suite one final time**

Run: `pytest -q`
Expected: everything green.

- [ ] **Step 4: Record the measured comparison**

Print the side-by-side, which is what gets posted to the owner:

```bash
python - <<'PY'
from datetime import date
from engine import pipeline
from engine.config import Config
from engine.models import PlanRun
from roster_engine import report as rr
from roster_engine.objective import compute_metrics
from tests.sample_workbook import build_masters_and_orders

masters, so_lines = build_masters_and_orders()
for sched in ("new", "roster"):
    cfg = Config(plan_start_date=date(2026, 8, 12), scheduler=sched,
                 overlap_percent=80, setup_time_min=90.0)
    entries = pipeline.run_forward(PlanRun(so_lines=list(so_lines)), cfg,
                                   masters)["rule6"]["output"]
    rows = rr.all_violations(entries, masters, cfg)
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    span = (max(e.end for e in entries) - min(e.start for e in entries)).days
    print(f"{sched:8s} entries={len(entries):4d} span={span:3d}d violations={kinds}")
PY
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_roster_end_to_end.py
git commit -m "test(roster): end-to-end on the sample book

The roster plan has zero violations of all four rules; the Gantt and Analytics
render it; every batch is fully produced and no routing step is inverted.

Non-vacuity is pinned separately: the LIVE engine is asserted to violate Rule 1
on the same book. If it were clean too, the checks would be measuring nothing
and the roster plan's zero would prove nothing — this fixture family has passed
vacuously before (2026-08-09)."
```

---

## Task 12: Push, and hand over for the owner's Actions run

- [ ] **Step 1: Confirm the untouchable files are untouched**

Run: `git diff --stat main~11 -- ppc_engine engine/rules engine/flow_scheduler.py engine/new_engine.py web .github scripts requirements.txt`
Expected: empty.

- [ ] **Step 2: Confirm the live site cannot change**

Run: `grep -rn "DEFAULT_SCHEDULER" render.yaml api/main.py`
Expected: still resolves to `new`. The roster engine is unreachable until the owner sets the env var.

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Write the handover message**

Post to the owner:
- what to set (`DEFAULT_SCHEDULER=roster` on the duplicate's Render service) and that reverting is the same one variable
- that the Actions optimizer button is unchanged
- the side-by-side numbers from Task 11 Step 4 on the sample book, stated as a **sample-book** result, not a claim about his book
- that the real comparison is his Actions run, which is step 2 of the agreed loop

Do **not** claim the engine is better than the live one. Nothing measured so far runs on the owner's book.

---

## Self-Review Notes

- **Spec coverage:** §4 architecture → Tasks 1,7; §5.1 roster → Task 4; §5.2 run → Task 5; §5.3 release → Task 3; frozen work (§5.2) → Task 6; §6.1 exact assignment → Task 2; §6.2–6.3 search → Task 9; §6.4 overlap band → Task 10 Step 4; §6.5 objective → Task 9; §6.6 file-by-file wiring → Task 10; §7 violation checks → Task 8; §8 rollout → Task 12.
- **Known follow-up, deliberately out of scope:** `_JOB_SHARE = 0.6` in `search.py` is a starting point. Measuring the right split needs the owner's book, so it happens after the first Actions run — not guessed here.
- **Deliberate duplication:** `domain.is_machining_machine` re-states `rule6_allocate._is_setup_machine`'s rule so the package stands alone. Task 1 pins them in agreement rather than importing a private from the classic rules.
