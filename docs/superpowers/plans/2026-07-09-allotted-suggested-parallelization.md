# Parallelization: Allotted vs Allotted∪Suggested — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Rule 6's machine selection depend on the parallelization toggle — OFF uses the Allotted machine(s) only (falling back to Suggested when Allotted is blank); ON uses the union of Allotted + Suggested.

**Architecture:** The entire behaviour change lives in `_resolve_candidates` in `engine/rules/rule6_allocate.py`, made toggle-aware. Its callers (earliest-free selection, the >400 parallel split, operator logic) consume the result unchanged. `_is_os` is decoupled from it so OS detection stays toggle-independent.

**Tech Stack:** Python 3, pytest, openpyxl. Run tests with `python3 -m pytest` (there is no `python` alias).

## Global Constraints

- **Toggle semantics:** OFF → `parse(allotted) or parse(suggested)` (Allotted only; Suggested fallback if Allotted blank). ON (`config.split_parallel` true) → union, Allotted first, deduped, order-preserving.
- **Keep the >400 physical-split threshold** (`split_min_qty`, default 401) — ON only widens the candidate *set*; a batch is physically split only when it exceeds the threshold. Do NOT change `_allocate_op`'s split math.
- **Ties prefer the Allotted machine** — achieved by listing Allotted first in the union (the downstream tie-break already keeps the first-listed machine).
- **All changes additive; golden trace (`tests/golden_trace.json`) must stay unchanged.** The sample leaves Allotted blank, so OFF (its default) resolves to Suggested = today's behaviour. Do NOT run `REGEN_GOLDEN`.
- **OS / off-machine detection must not depend on the toggle.** `_is_offmachine` stays unchanged; `_is_os`'s real-machine check is computed from Allotted+Suggested directly, not via `_resolve_candidates`.
- **Rules stay pure functions.** Branch: `split-allotted-suggested`. Do NOT push/merge to `main`.
- Baseline before starting: `python3 -m pytest -q` → **210 passed**.

---

### Task 1: Toggle-aware `_resolve_candidates` (+ decouple `_is_os`, thread `config`)

**Files:**
- Modify: `engine/rules/rule6_allocate.py` — `_resolve_candidates` (~line 60), `_is_os` real-machine line (~line 110), and three call sites (~lines 144, 357, 414)
- Test: `tests/test_rule6_allotted.py` (new)

**Interfaces:**
- Consumes: `parse_resource_candidates`, `normalize_process_name` (already imported in rule6); `Config.split_parallel: bool`, `Config.split_min_qty: int`.
- Produces: `_resolve_candidates(proc, config=None) -> list[str]` — toggle-aware machine id list.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rule6_allotted.py`:

```python
"""Rule 6 — parallelization toggle chooses Allotted-only vs Allotted∪Suggested."""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _masters(sug, allot, machines, cyc=1):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type="CNC lathe",
                     available_hrs_per_day=19.5) for m in machines}
    masters = Masters(machines=ms, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None,
                                    processes=[Process(1, "CNC", cyc, cyc, sug, allot)])
    return masters


def _batch(qty):
    return Batch(batch_id="B", item_code="X", item_name="x", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["B"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)


def _used(sched):
    return {e.machine for e in sched}


def test_off_uses_allotted_only():
    # sug CNC3/CNC6, allot CNC3, split OFF -> runs on CNC3, never CNC6.
    m = _masters(sug="CNC3/CNC6", allot="CNC3", machines=("CNC3", "CNC6"))
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=False), masters=m)
    assert _used(sched) == {"CNC3"}


def test_off_blank_allotted_falls_back_to_suggested():
    # allot blank, sug CNC6, split OFF -> still schedules (fallback), on CNC6.
    m = _masters(sug="CNC6", allot=None, machines=("CNC3", "CNC6"))
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=False), masters=m)
    assert _used(sched) == {"CNC6"}


def test_on_uses_union_of_allotted_and_suggested():
    # allot CNC4, sug CNC3/CNC6, split ON, large batch -> a SUGGESTED-only machine
    # (CNC6) AND the allotted (CNC4) both get work: the union is in play.
    m = _masters(sug="CNC3/CNC6", allot="CNC4", machines=("CNC3", "CNC4", "CNC6"))
    sched = rule6_allocate.run([_batch(1000)],
                               config=_cfg(split_parallel=True, split_min_qty=401), masters=m)
    used = _used(sched)
    assert "CNC4" in used and "CNC6" in used   # allotted + suggested-only both used
    assert len(sched) >= 2                       # physically split


def test_on_contrasts_with_off_on_same_routing():
    # Same routing: OFF stays on the allotted machine, ON reaches the suggested-only one.
    m = _masters(sug="CNC3/CNC6", allot="CNC4", machines=("CNC3", "CNC4", "CNC6"))
    off = rule6_allocate.run([_batch(1000)],
                             config=_cfg(split_parallel=False, split_min_qty=401), masters=m)
    assert _used(off) == {"CNC4"}               # OFF -> allotted only


def test_on_keeps_the_over_400_split_threshold():
    # split ON but batch <= 400 -> NOT physically split (avoid a 2nd setup); one entry.
    m = _masters(sug="CNC3/CNC6", allot="CNC4", machines=("CNC3", "CNC4", "CNC6"))
    sched = rule6_allocate.run([_batch(50)],
                               config=_cfg(split_parallel=True, split_min_qty=401), masters=m)
    assert len(sched) == 1


def test_os_detection_is_toggle_independent():
    # An Allotted=OS step is OS regardless; a named-'OS' step WITH a real machine is not.
    os_step = Process(1, "CNC OS", 3600, None, None, "OS")
    assert rule6_allocate._is_os(os_step) is True
    real_named_os = Process(1, "CNC OS", 5, 5, "CNC1/CNC2", None)
    assert rule6_allocate._is_os(real_named_os) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_rule6_allotted.py -v`
Expected: FAILs — today `_resolve_candidates` uses Suggested first, so `test_off_uses_allotted_only` sees `CNC6` used and `test_on_contrasts_with_off_on_same_routing` won't restrict to `CNC4`.

- [ ] **Step 3: Make `_resolve_candidates` toggle-aware**

In `engine/rules/rule6_allocate.py`, replace the whole `_resolve_candidates` function:

```python
def _resolve_candidates(proc, config=None):
    """Ordered REAL machine ids for a process (first = preferred), or [] if none.

    The parallelization toggle (``config.split_parallel``) decides the set:
      * OFF → the **Allotted** machine(s) only (the planned choice); if Allotted is
        blank, fall back to the **Suggested** machine(s) so the step still schedules.
      * ON  → the **union** of Allotted + Suggested (Allotted first, deduped) — every
        machine the item is capable of, so work can spread across all of them.
    Alternatives within a cell ('CNC3/CNC6') are parsed either way. A fully blank cell
    returns [] (the step is never invented onto a phantom station; with no cycle time
    it is an off-machine milestone, else 'needs machine')."""
    allotted = parse_resource_candidates(proc.allotted_machine)
    suggested = parse_resource_candidates(proc.suggested_machine)
    if getattr(config, "split_parallel", False):
        return allotted + [c for c in suggested if c not in allotted]
    return allotted or suggested
```

- [ ] **Step 4: Decouple `_is_os` from the toggle-aware resolver**

In `_is_os`, replace the `real = ...` line (currently `real = [c for c in _resolve_candidates(proc) if c != "OS"]`) with a direct Allotted+Suggested union so OS-ness never depends on the toggle:

```python
    real = [c for c in (parse_resource_candidates(proc.allotted_machine)
                        + parse_resource_candidates(proc.suggested_machine)) if c != "OS"]
    return not real and "OS" in normalize_process_name(proc.name).split()
```

- [ ] **Step 5: Thread `config` into the three remaining call sites**

In the same file, pass `config` to `_resolve_candidates` at each call:
- In `_allocate_op` (~line 144): `listed = _resolve_candidates(proc)` → `listed = _resolve_candidates(proc, config)`  (the function already has a `config` parameter).
- In `run()`'s main loop (~line 357): `candidates = _resolve_candidates(proc)` → `candidates = _resolve_candidates(proc, config)`.
- In `run()`'s emit block (~line 414): `cands_list = _resolve_candidates(proc)` → `cands_list = _resolve_candidates(proc, config)`.

Verify no bare `_resolve_candidates(proc)` calls remain except inside `_resolve_candidates`'s own definition:
Run: `grep -n "_resolve_candidates(proc)" engine/rules/rule6_allocate.py`
Expected: no output (all updated) — the only matches should be `_resolve_candidates(proc, config)`.

- [ ] **Step 6: Run the new tests + the full suite**

Run: `python3 -m pytest tests/test_rule6_allotted.py tests/test_rule6.py tests/test_rule6_os.py tests/test_dispatch_passthrough.py -v`
Expected: PASS (new toggle tests + all existing Rule 6 / OS / dispatch tests).

Run: `python3 -m pytest -q`
Expected: all pass (was 210; +6 new). If the golden test fails, STOP — the sample leaves Allotted blank so OFF must equal today's Suggested behaviour; investigate rather than regenerating.

- [ ] **Step 7: Confirm the golden trace is untouched**

Run: `git status --porcelain tests/golden_trace.json`
Expected: no output (unchanged).

- [ ] **Step 8: Commit**

```bash
git add engine/rules/rule6_allocate.py tests/test_rule6_allotted.py
git commit -m "Rule 6: parallelization toggle picks Allotted vs Allotted∪Suggested

OFF -> Allotted only (Suggested fallback if blank); ON -> union of Allotted +
Suggested. Keeps the >400 physical-split threshold; ties prefer the Allotted
machine. _is_os decoupled so OS detection stays toggle-independent.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Update RULES.md and CLAUDE.md

**Files:**
- Modify: `RULES.md` (Rule 6 — the "Alternative ('preferred') machines" + "Parallel split" subsections)
- Modify: `CLAUDE.md` (the `rule6_allocate.py` code-map bullet)

**Interfaces:** docs only — no code, no test.

- [ ] **Step 1: Update RULES.md**

In `RULES.md`, under Rule 6, locate the **"Alternative ('preferred') machines"** paragraph. Immediately after it (before or merged with the "Parallel split" paragraph), add this text describing the toggle semantics:

```markdown
**Suggested vs Allotted, and what the parallelization toggle spans.** The *Suggested
M/c* cell lists every machine the item is **capable** of using; the *Allotted M/c* cell
is the machine(s) actually **allotted** for the step. Which set the scheduler may use
depends on the parallelization toggle (`split_parallel`):

- **Toggle OFF** → the step uses its **Allotted** machine(s) only (the planned choice).
  If the Allotted cell is blank, it falls back to the Suggested machine(s) so the step
  still schedules.
- **Toggle ON** → the step may use the **union of Allotted + Suggested** (Allotted
  first) — every capable machine — so work load-balances and (for batches over 400)
  splits across all of them.

So with the toggle off, an item with Allotted `CNC4` and Suggested `CNC3/CNC6` runs only
on `CNC4`; with it on, it may run on `CNC4`, `CNC3` and `CNC6`. Ties prefer the Allotted
machine (it is listed first). This is independent of OS/DISPATCH handling, which is
decided before machine selection.
```

Then, in the existing **"Parallel split"** paragraph, ensure the described split set is
"the machines the toggle exposes (Allotted only when off, Allotted+Suggested when on)"
rather than "the Suggested alternatives" — reword that one clause to match.

- [ ] **Step 2: Update CLAUDE.md**

In `CLAUDE.md`, on the `engine/rules/ruleN_*.py` / Rule 6 bullet, add a sentence after the `_allocate_op` description:

```
`_resolve_candidates(proc, config)` is **parallelization-aware**: split OFF → the Allotted machine(s) only (Suggested fallback if blank); split ON → the union of Allotted + Suggested (Allotted first). OS/off-machine detection is independent of the toggle.
```

- [ ] **Step 3: Verify only docs changed**

Run: `git diff --stat RULES.md CLAUDE.md`
Expected: only these two files, additive.

- [ ] **Step 4: Commit**

```bash
git add RULES.md CLAUDE.md
git commit -m "Docs: parallelization toggle Allotted-vs-union in RULES.md/CLAUDE.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Toggle-aware candidate set (OFF=Allotted / ON=union) → Task 1 Step 3. ✅
- Blank-Allotted fallback to Suggested → Task 1 (`test_off_blank_allotted_falls_back_to_suggested`). ✅
- Keep >400 split threshold → Task 1 (`test_on_keeps_the_over_400_split_threshold`); `_allocate_op` untouched. ✅
- Ties prefer Allotted (Allotted first in union) → Task 1 Step 3 (order-preserving). ✅
- OS detection toggle-independent → Task 1 Steps 4 + `test_os_detection_is_toggle_independent`. ✅
- Golden unchanged / full suite green → Task 1 Steps 6-7. ✅
- Docs → Task 2. ✅

**Placeholder scan:** none — every code step shows complete code; the docs step gives verbatim text.

**Type consistency:** `_resolve_candidates(proc, config=None)` signature used consistently at all three call sites (Task 1 Step 5) and inside `_is_os` is *replaced* (not called). `config.split_parallel` / `config.split_min_qty` match `engine/config.py`. Test helper names (`_masters`, `_batch`, `_cfg`, `_used`) are self-consistent within the new test file. ✅
