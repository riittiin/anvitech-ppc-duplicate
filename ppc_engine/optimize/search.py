"""Iterated local search over the order sequence.

This is Layer 2's first optimizer. It is the *local-search* half of the planned
memetic algorithm (OPTIMIZATION.md) — chosen first because at ~0.6s per schedule
evaluation it is far more sample-efficient than a full genetic algorithm: it spends
its scarce evaluations on *guided* moves (pull the most-tardy orders earlier) rather
than random population churn. A GA population layer can wrap this later.

Design guarantees:
  - **Never worse than the best dispatch rule** — the seeds are evaluated first and
    kept as the incumbent; a move is accepted only if it strictly improves.
  - **Deterministic** — a fixed seed + an evaluation budget → the same result every
    run ("what you Apply is what you get").
  - **Objective-driven** — scored only through engine.objective (the symmetric
    on-time term plus a makespan tie-break, 2026-08-06). The search knows nothing
    about how the score is built.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.objective import compute_metrics, score
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.optimize.dispatch_rules import edd_sequence, lpt_sequence, slack_sequence, spt_sequence
from ppc_engine.scheduler import decode

Sequence = list[tuple[str, str]]


@dataclass
class OptimizeResult:
    """The outcome of a search.

    Attributes:
        best_sequence: The winning order sequence.
        best_score:    Its objective score (lower is better).
        best_metrics:  Its metrics (total/max tardiness, makespan, …).
        evaluations:   How many distinct schedules were evaluated (decode calls).
        baseline_score: The best dispatch-rule score before any search (for reporting
                        how much the search added).
    """

    best_sequence: Sequence
    best_score: float
    best_metrics: PlanMetrics
    evaluations: int
    baseline_score: float
    cancelled: bool = False


class _Evaluator:
    """Decodes + scores a sequence, caching by sequence so identical ones are free."""

    def __init__(self, orders: list[Order], masters: Masters, config: PlanConfig,
                 on_eval=None, frozen=None) -> None:
        self._orders = orders
        self._masters = masters
        self._config = config
        self._cache: dict[tuple, tuple[float, PlanMetrics]] = {}
        self.evals = 0  # number of real (non-cached) decodes performed
        # Fired after EVERY real decode with (evals_so_far, score) — used for a live
        # progress tracker (unlike on_progress, which only fires on an improvement).
        self._on_eval = on_eval
        self._frozen = frozen

    def evaluate(self, sequence: Sequence) -> tuple[float, PlanMetrics]:
        key = tuple(sequence)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        self.evals += 1
        sched = decode(self._orders, list(sequence), self._masters, self._config, frozen=self._frozen)
        metrics = compute_metrics(sched, self._orders, self._config.plan_start)
        result = (score(metrics, self._config), metrics)
        self._cache[key] = result
        if self._on_eval:
            self._on_eval(self.evals, result[0])
        return result


def _earlier_positions(i: int, n: int, rng: random.Random) -> list[int]:
    """A few candidate positions earlier than ``i`` to try inserting an order at.

    Kept small (front, midway, just-before, one random) because each try costs a full
    evaluation — we can't afford to scan every position.
    """
    if i == 0:
        return []
    cands = {0, i // 2, i - 1, rng.randint(0, i - 1)}
    return sorted(c for c in cands if 0 <= c < i)


def _local_search(
    seq: Sequence, ev: _Evaluator, budget: int, rng: random.Random, should_cancel=None
) -> tuple[Sequence, float, PlanMetrics]:
    """Hill-climb from ``seq`` by pulling the most-tardy orders earlier.

    First-improvement: as soon as a move helps, take it and re-focus on the new worst
    orders. Stops when no guided move improves, the evaluation budget is spent, or the
    caller cancels (``should_cancel`` polled per evaluation for a prompt Stop).
    """
    _stop = should_cancel or (lambda: False)
    seq = list(seq)
    cur_score, cur_m = ev.evaluate(seq)
    improved = True
    while improved and ev.evals < budget and not _stop():
        improved = False
        # Focus effort on the orders contributing most lateness.
        tard = cur_m.lateness_by_order
        worst = sorted(range(len(seq)), key=lambda i: tard.get(seq[i], 0.0), reverse=True)
        for i in worst[:15]:
            if ev.evals >= budget or _stop():
                break
            for j in _earlier_positions(i, len(seq), rng):
                if ev.evals >= budget or _stop():
                    break
                cand = seq[:]
                cand.insert(j, cand.pop(i))  # move order at i earlier to j
                sc, m = ev.evaluate(cand)
                if sc < cur_score - 1e-9:
                    seq, cur_score, cur_m = cand, sc, m
                    improved = True
                    break
            if improved:
                break
    return seq, cur_score, cur_m


def _perturb(seq: Sequence, rng: random.Random) -> Sequence:
    """A 'kick' to escape a local optimum: a few random segment relocations."""
    s = seq[:]
    n = len(s)
    for _ in range(max(1, n // 20)):
        i = rng.randrange(n)
        j = rng.randrange(n)
        s.insert(j, s.pop(i))
    return s


def optimize(
    orders: list[Order],
    masters: Masters,
    config: PlanConfig,
    budget: int = 300,
    seed: int = 0,
    on_progress=None,
    on_eval=None,
    frozen=None,
    should_cancel=None,
) -> OptimizeResult:
    """Search for the order sequence that minimises the objective.

    Args:
        orders:  The (schedulable) orders to plan.
        masters: The shop.
        config:  Plan config (also holds the objective weights).
        budget:  Max number of schedule evaluations (decode calls). Higher = better
                 but slower (~0.6s each on the full book).
        seed:    RNG seed — fixes the result for reproducibility.

    Returns:
        An OptimizeResult with the best sequence found and how it compares to the best
        dispatch-rule baseline.
    """
    ev = _Evaluator(orders, masters, config, on_eval=on_eval, frozen=frozen)
    rng = random.Random(seed)
    # Polled per evaluation so "Stop & keep best" halts the search promptly (a lambda
    # reading the cancel flag). None = never cancel (byte-identical to before).
    _stop = should_cancel or (lambda: False)

    # Seed with the dispatch rules; the best of them is our starting incumbent.
    seeds: list[Sequence] = [
        edd_sequence(orders),
        spt_sequence(orders, masters, config),
        slack_sequence(orders, masters, config),
        lpt_sequence(orders, masters, config),  # tail-aware: front-load long-flow orders
    ]
    best_seq, best_score, best_m = None, float("inf"), None
    for s in seeds:
        sc, m = ev.evaluate(s)
        if sc < best_score:
            best_seq, best_score, best_m = s, sc, m
            if on_progress:
                on_progress(ev.evals, best_score)
        if _stop():
            break
    baseline_score = best_score  # best pure dispatch-rule score, before any search

    # Iterated local search: climb, record the best, kick, repeat until budget spent.
    # Stagnation guard: if a whole round adds no NEW evaluations (everything reachable
    # is already cached — e.g. a tiny order set whose permutations are exhausted), the
    # search has converged, so stop instead of spinning forever below the budget.
    incumbent = best_seq
    while ev.evals < budget and not _stop():
        before = ev.evals
        cand, sc, m = _local_search(incumbent, ev, budget, rng, should_cancel=should_cancel)
        if sc < best_score:
            best_seq, best_score, best_m = cand, sc, m
            if on_progress:
                on_progress(ev.evals, best_score)
        if ev.evals >= budget or ev.evals == before or _stop():
            break
        incumbent = _perturb(best_seq, rng)  # kick from the current best

    return OptimizeResult(
        best_sequence=best_seq,
        best_score=best_score,
        best_metrics=best_m,
        evaluations=ev.evals,
        baseline_score=baseline_score,
        cancelled=_stop(),
    )
