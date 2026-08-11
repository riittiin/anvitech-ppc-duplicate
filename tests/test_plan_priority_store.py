"""book_store persistence of the applied Optimize run (ranks + metadata)."""
from engine import book_store
from engine.pipeline import KEY_SEP


RANKS = {f"SO1{KEY_SEP}X": 1, f"SO2{KEY_SEP}Y..": 2}   # dotted item = MongoDB-hostile, must survive
META = {"saved_at": "2026-07-13T10:00:00", "budget": "quick", "seed": 42,
        "baseline": {"makespan_days": 42.5}, "best": {"makespan_days": 38.7}}


def test_round_trip():
    book_store.save_plan_priority(RANKS, META)
    data = book_store.load_plan_priority()
    assert data["ranks"] == RANKS
    assert data["meta"] == META


def test_absent_returns_none():
    assert book_store.load_plan_priority() is None


def test_clear_removes_it():
    book_store.save_plan_priority(RANKS, META)
    book_store.clear_plan_priority()
    assert book_store.load_plan_priority() is None


def test_corrupt_or_empty_value_treated_as_absent():
    from engine.storage import get_store
    get_store().kv_set(book_store.PLAN_PRIORITY_KEY, "not json{")
    assert book_store.load_plan_priority() is None
    get_store().kv_set(book_store.PLAN_PRIORITY_KEY, "{\"ranks\": {}}")
    assert book_store.load_plan_priority() is None      # empty ranks = nothing applied
