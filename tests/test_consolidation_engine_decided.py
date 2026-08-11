"""Consolidation is engine-decided (overhaul 2026-07-24): default 1, forced at the
planning boundary, and normalized in the inputs signature so a stale saved 10 can't
reintroduce the ~6% regression."""
import pytest

pytest.importorskip("fastapi")
from engine.config import Config
import api.main as m


def test_resolve_forces_consolidation_to_one():
    cfg = Config(consolidation_window_days=10, plan_start_date=None)
    assert m._resolve_config(cfg).consolidation_window_days == 1


def test_inputs_signature_ignores_saved_consolidation():
    a = m._inputs_signature(Config(consolidation_window_days=10, scheduler="new"))
    b = m._inputs_signature(Config(consolidation_window_days=1, scheduler="new"))
    assert a == b
