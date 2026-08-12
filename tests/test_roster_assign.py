import itertools
import random

from roster_engine.assign import max_weight_matching


def test_picks_the_best_total_not_the_greedy_first_choice():
    # Greedy would give row 0 its best (col 0, 10) and leave row 1 with 1 -> 11.
    # The optimum is 9 + 8 = 17.
    values = {(0, 0): 10.0, (0, 1): 9.0, (1, 0): 8.0, (1, 1): 1.0}
    assert max_weight_matching(values, 2, 2) == {0: 1, 1: 0}


def test_a_forbidden_pairing_is_never_made():
    values = {(0, 1): 5.0, (1, 0): 5.0}      # (0,0) and (1,1) are not qualified
    assert max_weight_matching(values, 2, 2) == {0: 1, 1: 0}


def test_more_rows_than_columns_leaves_the_worst_rows_unassigned():
    values = {(0, 0): 1.0, (1, 0): 9.0, (2, 0): 5.0}
    assert max_weight_matching(values, 3, 1) == {1: 0}


def test_zero_and_negative_value_pairings_are_left_unassigned():
    values = {(0, 0): 0.0, (1, 1): -3.0, (2, 2): 7.0}
    assert max_weight_matching(values, 3, 3) == {2: 2}


def test_empty_input_is_an_empty_matching():
    assert max_weight_matching({}, 0, 0) == {}
    assert max_weight_matching({}, 3, 3) == {}


def test_result_is_deterministic_under_exact_ties():
    values = {(0, 0): 5.0, (0, 1): 5.0, (1, 0): 5.0, (1, 1): 5.0}
    first = max_weight_matching(values, 2, 2)
    for _ in range(20):
        assert max_weight_matching(values, 2, 2) == first


def test_opt_out_prevents_forced_loss_making_assignment():
    """A solver without real per-row opt-out capacity (the 1e18-padding brief,
    or dummy columns removed/collapsed to too few) has to fill every row into
    some real column and would force row 1 onto the -100 cell; the true
    optimum leaves row 1 unmatched and only rosters row 0."""
    # Hand-verified optimum over every possible sub-assignment (2x2, so this
    # is small enough to check by hand rather than trust the solver):
    #   {}                 -> 0
    #   {0:0}              -> 10   <- best
    #   {0:1}              -> 1
    #   {1:0}              -> 5
    #   {1:1}              -> -100
    #   {0:0, 1:1}         -> 10 + (-100) = -90
    #   {0:1, 1:0}         -> 1 + 5 = 6
    values = {(0, 0): 10.0, (0, 1): 1.0, (1, 0): 5.0, (1, 1): -100.0}
    assert max_weight_matching(values, 2, 2) == {0: 0}


def test_matches_brute_force_on_random_small_matrices():
    """The solver is exact, so on any matrix small enough to enumerate it must
    equal the best permutation. This is the test that would catch a subtly wrong
    Hungarian implementation, which unit cases would not.

    Density and value range are drawn per iteration — including fully dense
    (1.0) and negative-heavy bands — so dense, loss-making matrices that
    actually require opt-out capacity occur, not just the sparse
    mostly-positive matrices a fixed (density=0.8, range=(-3,20)) draw almost
    always produces. This combination is not decorative: with only one
    opt-out column (instead of one per row), a dense matrix can need two
    rows to opt out simultaneously, which crowds one of them onto a real
    cell and silently steals it from whichever row actually deserved it —
    confirmed by mutation testing to fail at this seed (iterations 20 and 21)
    when the implementation is weakened that way.
    """
    rng = random.Random(20260812)
    densities = (0.5, 0.8, 1.0)
    value_ranges = ((-20, 5), (-10, 10))
    for _ in range(200):
        n, m = rng.randint(1, 5), rng.randint(1, 5)
        density = rng.choice(densities)
        lo, hi = rng.choice(value_ranges)
        values = {(r, c): float(rng.randint(lo, hi))
                  for r in range(n) for c in range(m)
                  if rng.random() < density}
        got = max_weight_matching(values, n, m)
        got_total = sum(values[(r, c)] for r, c in got.items())

        best = 0.0
        rows, cols = list(range(n)), list(range(m))
        for k in range(min(n, m) + 1):
            for rs in itertools.combinations(rows, k):
                for cs in itertools.permutations(cols, k):
                    if all((r, c) in values for r, c in zip(rs, cs)):
                        best = max(best, sum(values[(r, c)]
                                             for r, c in zip(rs, cs)))
        assert got_total == best, (n, m, values, got)
