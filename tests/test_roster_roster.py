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
