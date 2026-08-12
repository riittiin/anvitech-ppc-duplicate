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


def test_matches_brute_force_on_random_small_matrices():
    """The solver is exact, so on any matrix small enough to enumerate it must
    equal the best permutation. This is the test that would catch a subtly wrong
    Hungarian implementation, which unit cases would not."""
    rng = random.Random(20260812)
    for _ in range(200):
        n, m = rng.randint(1, 5), rng.randint(1, 5)
        values = {(r, c): float(rng.randint(-3, 20))
                  for r in range(n) for c in range(m)
                  if rng.random() < 0.8}
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
