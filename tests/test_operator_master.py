"""Tests for engine/operator_master.py (pure) + its book_store persistence.

See docs/superpowers/specs/2026-07-18-operator-master-rotation-design.md.
"""
from datetime import date

from engine import book_store, operator_master as om


# --------------------------------------------------------------------------- #
# seed_rows_from_masters
# --------------------------------------------------------------------------- #
def test_seed_copies_name_machines_shift_and_defaults_unpinned(loaded):
    _, masters = loaded
    rows = om.seed_rows_from_masters(masters)

    assert len(rows) == len(masters.operators)
    for row, op in zip(rows, masters.operators):
        assert row["name"] == op.name
        assert row["machines_raw"] == op.preferred_machines_raw
        assert row["shift"] == op.shift
        assert row["pinned"] is False
        assert row["id"]  # non-empty


def test_seed_gives_each_row_a_distinct_id(loaded):
    _, masters = loaded
    rows = om.seed_rows_from_masters(masters)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# last_friday / next_rotation
# --------------------------------------------------------------------------- #
def test_last_friday_on_a_friday_is_itself():
    fri = date(2026, 7, 17)
    assert fri.weekday() == 4
    assert om.last_friday(fri) == fri


def test_last_friday_mid_week():
    # Wed 2026-07-15 -> Fri 2026-07-10
    assert om.last_friday(date(2026, 7, 15)) == date(2026, 7, 10)


def test_next_rotation_from_a_friday_is_next_week():
    fri = date(2026, 7, 17)
    assert om.next_rotation(fri) == date(2026, 7, 24)


def test_next_rotation_mid_week():
    assert om.next_rotation(date(2026, 7, 15)) == date(2026, 7, 17)


# --------------------------------------------------------------------------- #
# rotate_table
# --------------------------------------------------------------------------- #
def _row(name, shift, pinned=False, machines_raw="CNC 1"):
    return {"id": name, "name": name, "machines_raw": machines_raw,
            "shift": shift, "pinned": pinned}


def test_rotate_table_is_now_a_no_op_even_across_many_fridays():
    """Rotation was removed 2026-08-05. Whatever is stored is what is used."""
    table = {"week_anchor": "2026-07-03",
             "operators": [
                 {"id": "1", "name": "A", "machines_raw": "CNC1",
                  "shift": "First shift", "pinned": False},
                 {"id": "2", "name": "B", "machines_raw": "CNC2",
                  "shift": "Second shift", "pinned": False},
             ]}
    out, flips = om.rotate_table(table, date(2026, 8, 21))   # seven Fridays later
    assert flips == 0
    assert [r["shift"] for r in out["operators"]] == ["First shift", "Second shift"]


def test_operators_as_of_returns_the_stored_shift_for_any_date():
    table = {"week_anchor": "2026-07-03",
             "operators": [{"id": "1", "name": "A", "machines_raw": "CNC1",
                            "shift": "First shift", "pinned": False}]}
    for day in (date(2026, 7, 1), date(2026, 8, 21), date(2027, 1, 1)):
        assert om.operators_as_of(table, day)[0].shift == "First shift"


# --------------------------------------------------------------------------- #
# to_operators
# --------------------------------------------------------------------------- #
def test_to_operators_matches_excel_loaded_operator_field_by_field(loaded):
    _, masters = loaded
    rows = om.seed_rows_from_masters(masters)
    converted = om.to_operators(rows)

    assert len(converted) == len(masters.operators)
    for got, want in zip(converted, masters.operators):
        assert got.name == want.name
        assert got.preferred_machines_raw == want.preferred_machines_raw
        assert got.machines == want.machines
        assert got.shift == want.shift


# --------------------------------------------------------------------------- #
# book_store round-trip
# --------------------------------------------------------------------------- #
def test_operator_table_store_round_trip():
    assert book_store.load_operator_table() is None
    table = {"week_anchor": "2026-07-10",
             "operators": [_row("A", "First shift")]}
    book_store.save_operator_table(table)
    assert book_store.load_operator_table() == table


def test_operator_table_store_overwrite():
    book_store.save_operator_table({"week_anchor": "2026-07-10", "operators": []})
    table2 = {"week_anchor": "2026-07-17", "operators": [_row("A", "Second shift")]}
    book_store.save_operator_table(table2)
    assert book_store.load_operator_table() == table2
