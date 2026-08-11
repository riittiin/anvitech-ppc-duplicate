# Piece-flow correctness — Implementation Plan

> Use with superpowers:executing-plans. Spec: `docs/superpowers/specs/2026-07-25-piece-flow-no-premature-work-design.md`.

**Goal:** No routing step's work finishes before its predecessor's — a starved fast step runs as a batch ending with its predecessor, so no piece is processed before it exists.

**Architecture:** One re-lay in `ppc_engine/scheduler/flow_scheduler.py::decode`; everything downstream (Gantt, machine-wise CSV, analytics, optimizer) inherits it. Block model kept (fast); occupancy/operator rules unchanged.

## Global Constraints
- Only `scheduler=="new"` (ppc_engine) affected; classic/flow untouched; golden byte-identical.
- Operator/machine occupancy totals unchanged; one-operator-per-machine-per-shift preserved (delay the block, never stretch/hold an idle operator).
- `pytest` green + Test8 verified (0 op-segment precedence violations; makespan delta measured) before commit.

---

### Task 1: Invariant test — op WORK segments respect precedence

**Files:** Test: `tests/test_new_engine.py`

- [ ] **Step 1: Failing test** — for a slow→fast routing at high overlap, every op's last
  `op_segment` end ≥ its predecessor's last `op_segment` end (per order, by seq).

```python
def test_op_work_never_finishes_before_its_predecessor(old_book, new_masters):
    """A downstream step's real WORK (op_segments) can't finish before the step feeding it
    — else the machine-wise schedule processes pieces before they exist (the 'deburring
    skipped for the last jobs' bug, at the work level, not just the Gantt span)."""
    from dataclasses import replace
    from engine import new_engine
    from collections import defaultdict
    so_lines, masters = old_book
    cfg = replace(_CONF, overlap_percent=90)
    sched = new_engine.run(rule1_consolidate.run(so_lines, cfg), cfg, None, masters=masters)
    by_batch = defaultdict(list)
    for e in sched:
        if e.op_segments:
            by_batch[e.batch_id].append((e.process_seq, max(s[1] for s in e.op_segments)))
    bad = []
    for es in by_batch.values():
        es.sort()
        for i in range(1, len(es)):
            if es[i][1] < es[i - 1][1]:
                bad.append((es[i - 1], es[i]))
    assert not bad, f"downstream WORK finishes before predecessor: {bad[:3]}"
```

- [ ] **Step 2: Run — expect FAIL** (`pytest tests/test_new_engine.py -k op_work_never -q`): a fast step's segments finish early.

- [ ] **Step 3: Implement the re-lay** in `ppc_engine/scheduler/flow_scheduler.py::decode`, immediately after `placement = placements[key]` and before the commit loop:

```python
        placement = placements[key]
        # Piece-flow guard (2026-07-25 spec): a starved fast op must not finish its WORK
        # before its predecessor delivered the last piece. Re-lay it later (batch-at-end).
        if placement["machine_id"] is not None:
            _r = ready_of[key]
            _guard = 0
            while placement["end"] < prev_end_of[key] and _guard < 5:
                _r = _r + (prev_end_of[key] - placement["end"])
                placement = _place_operation(
                    ops_of[key][idx_of[key]], order_by_key[key], _r,
                    machine_free, staffing, masters, config)
                _guard += 1
```

- [ ] **Step 4: Run — expect PASS** (`pytest tests/test_new_engine.py -q`).

- [ ] **Step 5: Commit.**

---

### Task 2: ppc_engine-level crafted case + Test8 verification

**Files:** Test: `tests/test_new_engine.py` (or a ppc_engine test module)

- [ ] **Step 1: Crafted test** — an order: SLOW machining (big qty×cycle) → FAST manual, overlap 0.9. Assert the fast op's first segment starts ≥ when the slow op has produced its first pieces, its last segment ends ≥ the slow op's end, and the order completion is within a small tolerance of the pre-fix completion (makespan ≈ unchanged).

- [ ] **Step 2: Run + verify** (add to the suite).

- [ ] **Step 3: Manual Test8 verification** (scratch script, not committed): assert 0
  op-segment precedence violations across all orders; B028 DEBURING work now runs after VMC
  produces; print makespan/late-days before vs after (expect small delta); confirm the
  optimizer's winning sequence is stable or explain the shift.

- [ ] **Step 4: Full suite** (`pytest -q`) green, golden byte-identical.

- [ ] **Step 5: Docs** — CLAUDE.md note under `engine/new_engine.py` / the ppc_engine
  scheduler; commit spec + plan.

---

## Self-Review
- Spec coverage: invariant (Task 1), makespan/optimize stability (Task 2), consistency (overlap-independent rule), speed (≤5 re-lays/starved op). ✅
- Types: re-uses `_place_operation` signature verbatim. ✅
- No placeholders: the re-lay code is complete. ✅
