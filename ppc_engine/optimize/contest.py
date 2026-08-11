"""Overlap contest — discover the best overlap value for THIS order book.

The optimal operation-overlap is book-specific (0.8 wins one book, 0.7 another). So we
don't hardcode it: this runs the sequence optimizer at each candidate overlap and keeps
the best (overlap, sequence) overall.

Fairness of the contest (a measured lesson from the old build, LESSONS.md B7): every
candidate gets the SAME search budget — otherwise a candidate that happened to search
deeper wins on effort, not merit. Candidate 0.0 (no overlap) is always included, so the
contest can only ever match or beat the sequential plan, never lose to it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.optimize.search import OptimizeResult, Sequence, optimize

# Golden ratio conjugate — the fraction golden-section search shrinks the bracket by.
_GR = 0.6180339887498949

# Overlap values to try. 0.0 (sequential) is the fair baseline; the rest bracket the
# 80–90% region the data favours. Cheap to widen/narrow.
DEFAULT_OVERLAP_CANDIDATES = (0.0, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9)


@dataclass
class ContestResult:
    """The winning plan across all overlap candidates.

    Attributes:
        best_overlap:  The overlap value that won (use it to reproduce the plan:
                       decode with ``replace(config, overlap=best_overlap)``).
        best_sequence: The winning order sequence.
        best_score:    Its objective score.
        best_metrics:  Its metrics.
        evaluations:   Total schedule evaluations across the whole contest.
        per_candidate: [(overlap, score, metrics)] for every candidate, for reporting.
    """

    best_overlap: float
    best_sequence: Sequence
    best_score: float
    best_metrics: PlanMetrics
    evaluations: int
    per_candidate: list[tuple[float, float, PlanMetrics]]


def optimize_overlap(
    orders: list[Order],
    masters: Masters,
    config: PlanConfig,
    overlap_candidates: tuple[float, ...] = DEFAULT_OVERLAP_CANDIDATES,
    total_budget: int = 1400,
    seeds: tuple[int, ...] = (0,),
    frozen: list | None = None,
) -> ContestResult:
    """Run the sequence optimizer at each candidate overlap; return the overall best.

    Args:
        orders, masters, config: as for ``optimize``.
        overlap_candidates:      overlap values to contest (0.0 = sequential).
        total_budget:            total schedule evaluations, split EQUALLY across every
                                 (candidate × seed) run — a fair contest.
        seeds:                   RNG seeds to run PER candidate. Each candidate's score
                                 is the BEST it achieves across its seeds, so one
                                 unlucky search can't misrank an overlap value — this
                                 is what makes the pick reliable rather than noisy.
                                 More seeds + more budget → more reliable (the caller
                                 has ample cloud budget). Deterministic for fixed args.
    """
    per = max(1, total_budget // (len(overlap_candidates) * len(seeds)))
    results: list[tuple[float, OptimizeResult]] = []
    total_evals = 0
    for ov in overlap_candidates:
        cfg = replace(config, overlap=ov)
        best_res: OptimizeResult | None = None
        for sd in seeds:
            res = optimize(orders, masters, cfg, budget=per, seed=sd, frozen=frozen)
            total_evals += res.evaluations
            if best_res is None or res.best_score < best_res.best_score:
                best_res = res
        results.append((ov, best_res))

    # Winner = lowest objective score; ties broken toward LESS overlap (simpler plan).
    best_ov, best_res = min(results, key=lambda x: (x[1].best_score, x[0]))
    return ContestResult(
        best_overlap=best_ov,
        best_sequence=best_res.best_sequence,
        best_score=best_res.best_score,
        best_metrics=best_res.best_metrics,
        evaluations=total_evals,
        per_candidate=[(ov, r.best_score, r.best_metrics) for ov, r in results],
    )


# Consolidation windows (days) to contest. 0.0 (no consolidation) is always the fair
# baseline, so the auto-selector can decide consolidation simply doesn't help.
DEFAULT_CONSOLIDATION_CANDIDATES = (0.0, 2.0, 5.0, 10.0, 15.0)


def tune_consolidation(
    orders: list[Order],
    masters: Masters,
    config: PlanConfig,
    candidates: tuple[float, ...] = DEFAULT_CONSOLIDATION_CANDIDATES,
    total_budget: int = 1400,
    seeds: tuple[int, ...] = (0,),
) -> ContestResult:
    """Auto-select the consolidation window for THIS order book.

    Merging same-item nearby-due orders saves setups (can shorten the schedule) but can
    also delay the earlier-due order — so whether it helps, and by how many days, is
    book-specific. This runs the optimizer at each candidate window (equal budget,
    best-of-seeds — a fair contest with 0.0 always included) and keeps the best plan.
    If consolidation doesn't help, it returns window 0.0. Reuses ContestResult with
    ``best_overlap`` holding the winning WINDOW (days).
    """
    per = max(1, total_budget // (len(candidates) * len(seeds)))
    results: list[tuple[float, OptimizeResult]] = []
    total_evals = 0
    for w in candidates:
        cfg = replace(config, consolidation_window=w)
        best: OptimizeResult | None = None
        for sd in seeds:
            res = optimize(orders, masters, cfg, budget=per, seed=sd)
            total_evals += res.evaluations
            if best is None or res.best_score < best.best_score:
                best = res
        results.append((w, best))

    best_w, best_res = min(results, key=lambda x: (x[1].best_score, x[0]))
    return ContestResult(
        best_overlap=best_w,  # the winning consolidation window (days)
        best_sequence=best_res.best_sequence,
        best_score=best_res.best_score,
        best_metrics=best_res.best_metrics,
        evaluations=total_evals,
        per_candidate=[(w, r.best_score, r.best_metrics) for w, r in results],
    )


@dataclass
class TuneResult:
    """The outcome of the smart (golden-section) overlap tuner.

    Attributes:
        best_overlap:  The overlap value found best (a continuous value, e.g. 0.83).
        best_score / best_metrics / best_sequence: the winning plan.
        evaluations:   Total schedule evaluations spent.
        trace:         [(overlap, score)] in the order they were probed — the search
                       path (coarse bracket, then the golden-section closing in).
    """

    best_overlap: float
    best_score: float
    best_metrics: PlanMetrics
    best_sequence: Sequence
    evaluations: int
    trace: list[tuple[float, float]]
    cancelled: bool = False


def tune_overlap(
    orders: list[Order],
    masters: Masters,
    config: PlanConfig,
    lo: float = 0.5,
    hi: float = 0.9,
    seeds: tuple[int, ...] = (0, 1),
    budget_per_eval: int = 200,
    tol: float = 0.02,
    coarse: int = 5,
    on_eval: Callable[[float, float], None] | None = None,
    on_step: Callable[[int, float], None] | None = None,
    frozen: list | None = None,
    should_cancel=None,
) -> TuneResult:
    """Smart 1-D optimizer for the overlap value — homes in on the true optimum.

    Instead of a fixed grid, this treats "best objective score achievable at overlap x"
    as a noisy, single-peaked function of x and finds its minimum with **golden-section
    search**: keep a bracket around the optimum and shrink it ~38% per step by probing
    interior points — spending evaluations *near* the optimum, converging to a
    continuous value (0.72, 0.87, whatever it truly is), not a grid point.

    Robustness (the whole point): a short **coarse pass** first brackets the peak so the
    search can't start on the wrong slope of a noisy function, and every probe is
    **best-of-`seeds`** so one unlucky optimizer run can't send the search astray. The
    returned overlap is the best *ever probed*, not just the final bracket midpoint.

    Honest limits: assumes the overlap→score response is roughly single-peaked
    (physically reasonable — too little overlap gives little gain, too much starves the
    successor). Precision is bounded by the optimizer noise floor; more seeds / larger
    `budget_per_eval` tighten it. Deterministic for fixed arguments.
    """
    cache: dict[float, OptimizeResult] = {}
    trace: list[tuple[float, float]] = []
    total_evals = 0
    best_ever = [float("inf")]  # best score across ALL probes so far (for the live tracker)
    # Polled to make "Stop & keep best" prompt: passed into each probe's optimize() so a
    # probe halts mid-search, and checked between probes so the tuner stops too.
    _stop = should_cancel or (lambda: False)

    def f(x: float) -> float:
        nonlocal total_evals
        x = round(x, 3)
        if x in cache:
            return cache[x].best_score
        best: OptimizeResult | None = None
        for sd in seeds:
            offset = total_evals  # cumulative plans before this probe

            def _step(evals_in_probe, sc, _off=offset):
                if sc < best_ever[0]:
                    best_ever[0] = sc
                if on_step:
                    on_step(_off + evals_in_probe, best_ever[0])

            res = optimize(orders, masters, replace(config, overlap=x),
                           budget=budget_per_eval, seed=sd, on_eval=_step, frozen=frozen,
                           should_cancel=should_cancel)
            total_evals += res.evaluations
            if best is None or res.best_score < best.best_score:
                best = res
        cache[x] = best
        trace.append((x, best.best_score))
        if on_eval:
            on_eval(x, best.best_score)
        return best.best_score

    # 1) Coarse pass to bracket the peak region robustly.
    xs = [round(lo + (hi - lo) * i / (coarse - 1), 3) for i in range(coarse)]
    for x in xs:
        f(x)
        if _stop():
            break

    # 2) Golden-section within [a, b], reusing points across iterations. Skipped if the
    #    caller cancelled during the coarse pass — we then return the best point probed.
    if not _stop():
        bi = min(range(coarse), key=lambda i: cache[xs[i]].best_score)
        a, b = xs[max(0, bi - 1)], xs[min(coarse - 1, bi + 1)]
        c, d = round(b - _GR * (b - a), 3), round(a + _GR * (b - a), 3)
        fc, fd = f(c), f(d)
        while (b - a) > tol and not _stop():
            if fc < fd:
                b, d, fd = d, c, fc
                c = round(b - _GR * (b - a), 3)
                fc = f(c)
            else:
                a, c, fc = c, d, fd
                d = round(a + _GR * (b - a), 3)
                fd = f(d)

    best_x = min(cache, key=lambda x: cache[x].best_score)
    br = cache[best_x]
    return TuneResult(
        best_overlap=best_x,
        best_score=br.best_score,
        best_metrics=br.best_metrics,
        best_sequence=br.best_sequence,
        evaluations=total_evals,
        trace=trace,
        cancelled=_stop(),
    )
