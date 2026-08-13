import os
import subprocess
import sys
import textwrap
from datetime import date, datetime

from engine.models import Machine, Masters, Operator, WorkCalendar
from roster_engine import roster
from roster_engine.domain import build_shop
from roster_engine.worktime import ShiftWindow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


# --------------------------------------------------------------------------- #
# Review round 1 (2026-08-12) — fix-round tests
# --------------------------------------------------------------------------- #

def test_the_crew_genome_never_drops_a_real_pairing_below_zero():
    """Finding 1 (Important). Reviewer's exact repro: the ranks the optimizer
    hands in span its FULL machine list (0..M-1), but roster_for_shift only ever
    sees the subset of machines running THIS shift -- so a rank can legitimately
    exceed n_ranks (a second shift with several single-shift stations; or a
    persisted crew genome replayed after a masters re-upload shrank the machine
    list). The old bias term `LOOKAHEAD_UNIT * (n_ranks - crew_rank.get(mid,
    n_ranks))` went deeply negative in that case and outweighed 400/300 real
    minutes of pending work, so `max_weight_matching` dropped BOTH pairings even
    though a free qualified operator was staring at both machines -- 700 minutes
    of real work simply not run."""
    shop = _shop([_op("Narayan", ["CNC1", "CNC4"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 400.0, "CNC4": 300.0}, {},
        {"CNC1": 12, "CNC4": 13})
    assert got == {"CNC1": "Narayan"}


def test_operator_order_never_changes_the_result():
    """Finding 2 (Important), part 1. A single machine, two operators tied in
    value: which one wins the tie is decided by ROW ORDER in the assignment
    matrix (row 0 wins on an exact tie -- confirmed directly against
    `max_weight_matching`), so the caller-supplied order of `shop.operators`
    must never leak into the outcome. `roster_for_shift`'s own
    `sorted(shop.operators, key=lambda o: o.name)` is what pins it: feeding the
    same two operators in two different input orders must give the identical
    result. Without the sort, input order decides the tie and the two calls
    below disagree (2026-08-12 review finding: `list(shop.operators)` passed
    all 10 tests)."""
    forward = _shop([_op("Amit", ["CNC1"]), _op("Zafar", ["CNC1"])])
    backward = _shop([_op("Zafar", ["CNC1"]), _op("Amit", ["CNC1"])])
    got_forward = roster.roster_for_shift(_win(), forward, {"CNC1": 500.0}, {}, {})
    got_backward = roster.roster_for_shift(_win(), backward, {"CNC1": 500.0}, {}, {})
    assert got_forward == got_backward == {"CNC1": "Amit"}


_HASH_SEED_SCRIPT = textwrap.dedent("""
    from datetime import date, datetime

    from engine.models import Machine, Masters, Operator, WorkCalendar
    from roster_engine import roster
    from roster_engine.domain import build_shop
    from roster_engine.worktime import ShiftWindow

    machines = {
        "CNC1": Machine("CNC1", "CNC 1", "CNC lathe", available_hrs_per_day=19.5),
        "CNC4": Machine("CNC4", "CNC 4", "CNC lathe", available_hrs_per_day=19.5),
        "CNC7": Machine("CNC7", "CNC 7", "CNC lathe", available_hrs_per_day=19.5),
    }
    operators = [Operator("Narayan", "CNC1/CNC4/CNC7", ["CNC1", "CNC4", "CNC7"],
                          "First shift")]
    shop = build_shop(
        Masters(machines=machines, operators=operators, calendar=WorkCalendar()), {})
    window = ShiftWindow(date(2026, 8, 12), "first",
                         datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 19, 0))
    got = roster.roster_for_shift(
        window, shop, {"CNC1": 500.0, "CNC4": 500.0, "CNC7": 500.0}, {}, {})
    print(sorted(got.items()))
""")


def test_machine_iteration_order_never_changes_the_result():
    """Finding 2 (Important), part 2. `Shop.machining_ids` is a frozenset -- its
    iteration order is a function of the interpreter's STRING HASH SEED, not of
    insertion order, so no in-process reorder of the input dict can ever observe
    whether `roster.py`'s `sorted(...)` over `machines` is still there: within
    one pytest process the frozenset order is fixed regardless (2026-08-12
    review finding -- this is exactly why dropping the sort passed all 10
    existing tests). Run the identical scenario -- three machines tied on
    `crew_rank` so ONLY sort order can decide which one the lone operator gets
    -- in separate subprocesses under different `PYTHONHASHSEED`s and require
    byte-identical output. That is only guaranteed when the machine list is
    genuinely sorted (alphabetically, on this tie) before it drives column
    order, never when it is iterated straight off the frozenset."""
    outputs = set()
    for seed in ("0", "1", "2", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", _HASH_SEED_SCRIPT],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True)
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"result varied across PYTHONHASHSEED: {outputs}"
    assert outputs == {"[('CNC1', 'Narayan')]"}


def test_the_crew_genome_can_outweigh_a_small_demand_edge():
    """Finding 3 (Minor). `LOOKAHEAD_UNIT = 0.0` passes every earlier test
    (2026-08-12 review finding) because `machines` is sorted by rank and an
    EXACT demand tie is resolved by column order regardless of the bias's
    actual value -- the rank never has to win through the value. Here CNC4 has
    strictly MORE raw pending work than CNC1 (650 > 630), so a zero-valued
    lookahead would put the lone operator on CNC4; only a real bias favouring
    CNC1's higher priority (rank 0 vs rank 1) can flip that."""
    shop = _shop([_op("Narayan", ["CNC1", "CNC4"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 630.0, "CNC4": 650.0}, {},
        {"CNC1": 0, "CNC4": 1})
    assert got == {"CNC1": "Narayan"}


def test_shift_capacity_caps_pending_so_a_backlog_cannot_outbid_a_carryover():
    """Finding 4 (Minor). `pending` is minutes of PLANNED work and can be an
    enormous multi-shift backlog number; `min(window.minutes, pending)` caps it
    to what ONE shift can physically absorb. CNC4's backlog here (5,000,000 min)
    is deliberately bigger than CARRY_BONUS (1,000,000) -- uncapped it would
    outbid the carry-over machine and steal the operator off a part still
    physically in the chuck, which must never happen. Capped, it is worth at
    most one shift (~660 min), nowhere near CARRY_BONUS (2026-08-12 review
    finding: raw `pending` in place of the cap passed all 10 tests)."""
    shop = _shop([_op("Narayan", ["CNC1", "CNC4"])])
    got = roster.roster_for_shift(
        _win(), shop, {"CNC1": 10.0, "CNC4": 5_000_000.0}, {"CNC1": "B1"}, {})
    assert got == {"CNC1": "Narayan"}


def test_a_non_working_day_returns_no_roster_at_all():
    """Finding 5 (Minor). Thursday is the weekly off
    (`WorkCalendar.weekly_off_weekday == 3`) -- the whole shift is closed,
    machine- and operator-independent, so this belongs as ONE guard at the top
    of `roster_for_shift`, not re-evaluated inside `eligible` for every operator
    x machine pair (2026-08-12 review finding: deleting the check anywhere
    passed all 10 existing tests)."""
    shop = _shop([_op("Narayan", ["CNC1"])])
    thursday = ShiftWindow(date(2026, 8, 13), "first",
                           datetime(2026, 8, 13, 8, 0), datetime(2026, 8, 13, 19, 0))
    assert roster.roster_for_shift(thursday, shop, {"CNC1": 600.0}, {}, {}) == {}
