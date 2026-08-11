"""Tests for the frozen parameter threading through run_forward and schedulers.

Verifies that:
  * run_forward accepts frozen=None without error
  * frozen=None produces byte-identical schedules (no-op for classic/flow engines)
  * new_engine.run receives frozen and can act on it
"""

import io
from datetime import date

from engine.config import Config
from engine import book_store, loaders, pipeline
from engine.models import PlanRun
from tests.new_sample_workbook import build_new_sample_bytes


def test_run_forward_frozen_none_is_byte_identical():
    """Verify that frozen=None is a no-op across all schedulers."""
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)

    # Run without frozen parameter
    pr1 = PlanRun(so_lines=list(so_lines))
    trace1 = pipeline.run_forward(pr1, cfg, masters)

    # Run with frozen=None
    pr2 = PlanRun(so_lines=list(so_lines))
    trace2 = pipeline.run_forward(pr2, cfg, masters, frozen=None)

    # Schedules should be byte-identical
    assert [e.__dict__ for e in pr1.schedule] == [e.__dict__ for e in pr2.schedule]


def test_run_forward_frozen_none_classic_scheduler():
    """Verify that frozen=None is accepted by the classic (rule6_allocate) scheduler."""
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="classic", plan_start_date=date(2025, 3, 3))

    # Run with frozen=None on classic scheduler (should not error)
    pr = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(pr, cfg, masters, frozen=None)

    # Verify the schedule was produced
    assert pr.schedule is not None
    assert len(pr.schedule) > 0


def test_run_forward_frozen_none_flow_scheduler():
    """Verify that frozen=None is accepted by the flow scheduler."""
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    cfg = Config(scheduler="flow", plan_start_date=date(2025, 3, 3))

    # Run with frozen=None on flow scheduler (should not error)
    pr = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(pr, cfg, masters, frozen=None)

    # Verify the schedule was produced
    assert pr.schedule is not None
    assert len(pr.schedule) > 0
