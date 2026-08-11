"""The machine-flexibility knob (Config.flexible_machines) — new_engine._new_masters
loads the ppc masters at the chosen flexibility (Allotted-only vs Allotted+Suggested
union), and run/optimize_sequence/tune all read it from the config. See
docs/superpowers/specs/2026-07-29-machine-set-optimize-dimension-design.md.
"""

import pytest
from dataclasses import replace

from engine import book_store, new_engine
from engine.optimizer import SweepResult, score
from engine.rules import rule1_consolidate
from tests.new_sample_workbook import build_new_sample_bytes
from tests.test_new_engine import _CONF, _old_book


@pytest.fixture(autouse=True)
def _new_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    new_engine._MASTERS_CACHE.clear()
    book_store.save_masters_bytes(build_new_sample_bytes())


def _machine_options_count(flexible):
    m = new_engine._new_masters(flexible)
    r = m.routings["NEW-A-01"]           # Item A: CNC FIRST SIDE, Allotted "CNC1", Suggested "CNC1/CNC2"
    op = next(o for o in r.operations if o.name == "CNC FIRST SIDE")
    return len(op.machine_options)


def test_union_adds_suggested_machines():
    assert _machine_options_count(False) == 1      # Allotted only
    assert _machine_options_count(True) == 2       # Allotted + Suggested (CNC1, CNC2)


def test_cache_distinguishes_flexibility():
    a = new_engine._new_masters(False)
    b = new_engine._new_masters(True)
    assert a is not b                               # not a stale same-hash cache hit
    assert new_engine._new_masters(False) is a      # each flavour cached


def test_run_places_op_on_suggested_machine_only_when_flexible():
    """The base 2-order sample (Item A immediate, Item B behind a 240-min OS lead time)
    never actually contends for a CNC machine, and the sample workbook has only ONE
    first-shift operator (Alpha) qualified for CNC1/CNC2 — so even with CNC2 in the
    machine_options union, a second concurrent order gets no benefit from it (Alpha
    can't run two machines at once; see debug trace in the task report). Build two
    SEPARATE Item A orders (delivery dates > the 10-day consolidation window apart, so
    Rule 1 keeps them as two batches) both ready for "CNC FIRST SIDE" at plan_start, AND
    add a second first-shift CNC1/CNC2-qualified operator ("Echo") to the app-owned
    operator set so real parallel machine capacity is actually usable: only a
    Suggested-only machine (CNC2) lets the second order run in parallel instead of
    queueing behind the first on the sole Allotted machine (CNC1)."""
    from datetime import date as _date
    from dataclasses import replace as _replace
    from engine.models import SOLine, Operator
    _, masters = _old_book()
    masters = _replace(masters, operators=list(masters.operators) +
                       [Operator(name="Echo", preferred_machines_raw="CNC1, CNC2", shift="First shift")])
    so_lines = [
        SOLine(so_no="F1", item_code="NEW-A-01", item_name="x", qty=5, delivery_date=_date(2025, 3, 10)),
        SOLine(so_no="F2", item_code="NEW-A-01", item_name="x", qty=5, delivery_date=_date(2025, 3, 25)),
    ]
    batches = rule1_consolidate.run(so_lines, _CONF)
    assert len(batches) == 2, "test needs two separate (unconsolidated) batches"

    def machines(cfg):
        return {e.machine for e in new_engine.run(batches, config=cfg, masters=masters)}

    only = machines(replace(_CONF, flexible_machines=False))
    both = machines(replace(_CONF, flexible_machines=True))
    assert "CNC2" not in only          # Allotted-only never reaches CNC2 for these ops
    assert "CNC2" in both              # union lets the scheduler use the Suggested CNC2


def test_flexible_false_is_byte_identical_to_default():
    so_lines, masters = _old_book()
    batches = rule1_consolidate.run(so_lines, _CONF)
    base = [(e.item_code, e.process_seq, e.machine, e.start, e.end)
            for e in new_engine.run(batches, config=_CONF, masters=masters)]
    flag = [(e.item_code, e.process_seq, e.machine, e.start, e.end)
            for e in new_engine.run(batches, config=replace(_CONF, flexible_machines=False), masters=masters)]
    assert base == flag


def test_frozen_op_on_suggested_machine_pins_in_allotted_only_pass():
    """A frozen op whose machine is a Suggested-only machine (CNC2) must still pin there
    even when the pass loads Allotted-only options (CNC2 not in the op's options)."""
    so_lines, masters = _old_book()
    batches = rule1_consolidate.run(so_lines, _CONF)
    so_no = next(sl.so_no for sl in so_lines if sl.item_code == "NEW-A-01")
    frozen = [{"so_no": so_no, "item_code": "NEW-A-01", "process": "CNC FIRST SIDE",
               "op_seq": 1, "machine": "CNC2", "operator": "Alpha",
               "remaining_qty": 1, "prev_start": "2025-03-03T08:00:00"}]
    sched = new_engine.run(batches, config=replace(_CONF, flexible_machines=False),
                           masters=masters, frozen=frozen)
    e = next(x for x in sched if x.item_code == "NEW-A-01" and x.process_seq == 1)
    assert e.machine == "CNC2"        # pinned despite CNC2 not being an Allotted-only option


def test_sweepresult_has_flexible_machines_field():
    assert SweepResult().flexible_machines is False


def test_local_sweep_reports_winning_machine_set_and_counts_both_passes():
    so_lines, masters = _old_book()
    seen = []
    sw = new_engine.sweep_optimize(so_lines, _CONF, masters, budget_evals=40, seed=1,
                                   on_progress=lambda n, b: seen.append(n))
    assert isinstance(sw.flexible_machines, bool)
    assert sw.result.ranks                       # a real winner
    assert max(seen) > 0                          # progress advanced across both passes
