"""Task 19 (owner pivot, 2026-07-16): `_plan` is ALWAYS single-pass — lanes
(open/committed) are pure status labels with no scheduling effect. (The
Urgent lane was removed 2026-07-29; see tests/test_no_urgent.py.)

Covers:
  * saved ranks replay in ONE pass over a committed book (no two-pass, no
    joint/legacy distinction, no promise re-validation / fallback);
  * `_incumbent_metrics` scores over ALL active lines;
  * THE pivot regression at the api level: a book with committed + promised
    orders produces a byte-identical `/run` schedule (rule6 output + expected
    ends) to the SAME book all-open. Lanes cannot move the plan.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order
from tests.sample_workbook import ITEM_A, ITEM_B, build_sample_bytes

KEY_A = f"SO1\x1f{ITEM_A}"


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def test_saved_ranks_replay_single_pass_on_a_committed_book():
    """A committed book with saved ranks replays them in one pass and still
    produces a schedule — no fallback key, no two-pass namespacing."""
    m = _api()
    _seed_book()
    book_store.set_commitment("SO1", ITEM_A, "committed", date(2025, 3, 20), "t")

    m._start_optimize(budget_evals=12, label="deep", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority() is not None

    result = m._plan(m._load_plan_config())
    assert "joint_fallback" not in result
    assert "recovery_meta" not in result
    assert result["optimize_meta"]["active"] is True
    assert result["gantt"]["rows"], "the replayed plan must still produce a schedule"
    # Single pass → no "O-"-namespaced batch ids anywhere (that was two-pass only).
    assert not any(str(v).startswith("O-")
                   for r in result["trace"]["rule6"]["output"]["rows"] for v in r)


def test_incumbent_metrics_cover_all_active_lines():
    m = _api()
    _seed_book()
    book_store.set_commitment("SO1", ITEM_A, "committed", date(2025, 3, 20), "t")

    metrics = m._incumbent_metrics()
    assert metrics["orders"] == 2, (
        f"expected metrics over both active lines, got orders={metrics['orders']}")


def test_committed_book_plans_byte_identical_to_all_open():
    """The pivot regression: committing + promising orders must NOT change the
    plan. The rule6 schedule and every expected end are byte-identical whether
    the orders are committed or all open."""
    m = _api()

    def _plan_committed():
        book_store.clear_plan_priority()
        book_store.save_masters_bytes(build_sample_bytes())
        book_store.add_orders([
            Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
                  commitment="committed", promised_date=date(2025, 3, 20),
                  committed_at="t"),
            Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21),
                  commitment="committed", promised_date=date(2025, 3, 21),
                  committed_at="t"),
            Order("SO3", ITEM_A, ITEM_A, 8, date(2025, 3, 25)),
        ])
        return m._plan(m._load_plan_config())

    def _plan_all_open():
        book_store.clear_plan_priority()
        book_store.save_masters_bytes(build_sample_bytes())
        book_store.add_orders([
            Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
            Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
            Order("SO3", ITEM_A, ITEM_A, 8, date(2025, 3, 25)),
        ])
        return m._plan(m._load_plan_config())

    committed = _plan_committed()
    book_store.delete_all()
    m = _api()
    open_plan = _plan_all_open()

    assert committed["trace"]["rule6"]["output"] == open_plan["trace"]["rule6"]["output"], (
        "committing orders must not change the rule6 schedule")
    assert committed["expected_end"] == open_plan["expected_end"], (
        "committing orders must not change any expected completion date")
