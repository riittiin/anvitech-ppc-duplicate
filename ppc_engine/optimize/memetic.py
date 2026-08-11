"""Memetic algorithm — a genetic algorithm whose offspring are polished by local
search. The state-of-the-art shape for this problem class (OPTIMIZATION.md).

Why memetic (GA + local search) rather than plain GA or plain local search:
  - The population + crossover give *diversity* — a way to escape the single-basin
    trap that iterated local search can fall into (a historical sweep, back when the
    objective still carried a separate fairness term, showed the search, not the
    objective, was the bottleneck; the objective is now one symmetric on-time term
    plus a makespan tie-break, 2026-08-06 — see ppc_engine/objective).
  - The local-search polish gives *sample-efficiency* — critical because each schedule
    evaluation costs ~0.6s, so we can't afford a purely random GA.

Guarantees match the ILS optimizer: never worse than the best dispatch-rule seed
(elitism + seeded population), and deterministic (fixed seed + evaluation budget).
"""

from __future__ import annotations

import random

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.optimize.dispatch_rules import edd_sequence, slack_sequence, spt_sequence
from ppc_engine.optimize.search import OptimizeResult, Sequence, _Evaluator, _local_search

# How many local-search moves to spend polishing each new child (small — diversity
# comes from the population, refinement from a light polish).
_POLISH = 12


def _order_crossover(p1: Sequence, p2: Sequence, rng: random.Random) -> Sequence:
    """Order crossover (OX): keep a random slice of parent 1 in place, fill the rest
    with parent 2's order. Produces a valid permutation (every order exactly once) —
    the standard crossover for sequence problems.
    """
    n = len(p1)
    if n < 3:
        return list(p1)
    a, b = sorted(rng.sample(range(n), 2))
    child: list = [None] * n
    child[a:b] = p1[a:b]
    taken = set(p1[a:b])
    fill = [x for x in p2 if x not in taken]
    idx = 0
    for i in list(range(b, n)) + list(range(0, a)):
        child[i] = fill[idx]
        idx += 1
    return child


def _swap_mutate(seq: Sequence, rng: random.Random) -> Sequence:
    """Swap two random positions — a small kick for diversity."""
    s = seq[:]
    if len(s) >= 2:
        i, j = rng.sample(range(len(s)), 2)
        s[i], s[j] = s[j], s[i]
    return s


def _tournament(pop_scored: list[tuple[float, Sequence]], rng: random.Random, k: int = 3) -> Sequence:
    """Pick the best of ``k`` random population members (tournament selection)."""
    contenders = [pop_scored[rng.randrange(len(pop_scored))] for _ in range(k)]
    return min(contenders, key=lambda x: x[0])[1]


def memetic(
    orders: list[Order],
    masters: Masters,
    config: PlanConfig,
    budget: int = 400,
    seed: int = 0,
    pop_size: int = 8,
    mutation_rate: float = 0.3,
) -> OptimizeResult:
    """Search order sequences with a memetic algorithm.

    Args mirror ``search.optimize`` (budget = max schedule evaluations, ~0.6s each).
    Steady-state: each iteration breeds one child, polishes it with local search, and
    replaces the population's worst member if the child is better (keeping the best —
    elitism). Returns the best sequence found and its comparison to the dispatch seeds.
    """
    ev = _Evaluator(orders, masters, config)
    rng = random.Random(seed)

    # Seed the population with the dispatch rules, then fill with random permutations.
    seeds: list[Sequence] = [
        edd_sequence(orders),
        spt_sequence(orders, masters, config),
        slack_sequence(orders, masters, config),
    ]
    population: list[Sequence] = [list(s) for s in seeds]
    while len(population) < pop_size:
        p = list(seeds[0])
        rng.shuffle(p)
        population.append(p)

    # Evaluate the initial population.
    scored: list[tuple[float, Sequence]] = []
    for ind in population:
        sc, _ = ev.evaluate(ind)
        scored.append((sc, ind))
    best_score, best_seq = min(scored, key=lambda x: x[0])
    _, best_m = ev.evaluate(best_seq)
    baseline_score = min(ev.evaluate(s)[0] for s in seeds)  # best pure dispatch seed

    # Breed until the evaluation budget is spent. The stagnation guard stops the loop
    # if many iterations in a row add no NEW evaluations (the reachable space is
    # exhausted — e.g. a tiny order set), so it can never spin forever below budget.
    stagnation = 0
    while ev.evals < budget and stagnation <= 40:
        before = ev.evals
        parent1 = _tournament(scored, rng)
        parent2 = _tournament(scored, rng)
        child = _order_crossover(parent1, parent2, rng)
        if rng.random() < mutation_rate:
            child = _swap_mutate(child, rng)

        # Polish the child with a short burst of guided local search.
        child, csc, cm = _local_search(child, ev, min(budget, ev.evals + _POLISH), rng)

        # Steady-state replacement: the child ousts the current worst if it's better.
        worst_i = max(range(len(scored)), key=lambda i: scored[i][0])
        if csc < scored[worst_i][0]:
            scored[worst_i] = (csc, child)
        if csc < best_score:
            best_score, best_seq, best_m = csc, child, cm

        stagnation = stagnation + 1 if ev.evals == before else 0

    return OptimizeResult(
        best_sequence=best_seq,
        best_score=best_score,
        best_metrics=best_m,
        evaluations=ev.evals,
        baseline_score=baseline_score,
    )
